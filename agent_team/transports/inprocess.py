from __future__ import annotations

import json
import pathlib
import threading
import time
from types import ModuleType
from typing import Any, Callable, Dict, List, Mapping

from ..core import HOOK_EVENT_TEAMMATE_IDLE, TEAMMATE_IDLE_HOOK_INTERVAL_SEC, Message, Task
from ..runtime.task_context import ScopedSharedState, build_task_context_snapshot


class InProcessTeammateAgent(threading.Thread):
    def __init__(
        self,
        context: Any,
        stop_event: threading.Event,
        handlers: Mapping[str, Callable[[Any, Task], Dict[str, Any]]],
        get_lead_name_fn: Callable[[Any], str],
        profile_has_skill_fn: Callable[[Any, str], bool],
        traceback_module: ModuleType,
        claim_tasks: bool = True,
    ) -> None:
        super().__init__(name=context.profile.name, daemon=True)
        self.context = context
        self.stop_event = stop_event
        self.claim_tasks = claim_tasks
        self._handlers = handlers
        self._get_lead_name_fn = get_lead_name_fn
        self._profile_has_skill_fn = profile_has_skill_fn
        self._traceback_module = traceback_module
        self._local_memory: List[Dict[str, str]] = []
        self._refresh_session_state()

    def _refresh_session_state(self) -> None:
        if self.context.session_registry is None:
            return
        self.context.session_state = self.context.session_registry.session_for(self.context.profile.name)
        raw_memory = self.context.session_state.get("provider_memory", [])
        if not isinstance(raw_memory, list):
            self._local_memory = []
            return
        self._local_memory = [
            {
                "topic": str(item.get("topic", "") or ""),
                "reply": str(item.get("reply", "") or ""),
            }
            for item in raw_memory
            if isinstance(item, dict)
        ]

    def _reply_with_provider(
        self,
        topic: str,
        prompt: str,
        fallback_reply: str,
    ) -> str:
        if not self.context.runtime_config.teammate_provider_replies:
            return fallback_reply

        self._refresh_session_state()
        memory_turns = max(1, int(self.context.runtime_config.teammate_memory_turns))
        recent_memory = self._local_memory[-memory_turns:]
        memory_text = "\n".join(
            [f"- [{item.get('topic', 'unknown')}] {item.get('reply', '')[:180]}" for item in recent_memory]
        )
        if not memory_text:
            memory_text = "- none"

        system_prompt = (
            "You are a teammate analyst in a multi-agent workflow. "
            "Return one concise paragraph with concrete, testable recommendations."
        )
        user_prompt = (
            f"Agent: {self.context.profile.name}\n"
            f"Agent type: {self.context.profile.agent_type}\n"
            f"Topic: {topic}\n"
            "Recent local memory:\n"
            f"{memory_text}\n\n"
            "Task prompt:\n"
            f"{prompt}\n"
            "Output style: concise, specific, and directly actionable."
        )
        try:
            generated = self.context.provider.complete(system_prompt=system_prompt, user_prompt=user_prompt).strip()
            if not generated:
                return fallback_reply
            if self.context.session_registry is not None:
                self.context.session_state = self.context.session_registry.record_provider_reply(
                    agent_name=self.context.profile.name,
                    topic=topic,
                    reply=generated,
                    memory_turns=memory_turns,
                )
                self._refresh_session_state()
            else:
                self._local_memory.append({"topic": topic, "reply": generated})
                self._local_memory = self._local_memory[-memory_turns:]
            self.context.logger.log(
                "teammate_provider_reply_generated",
                agent=self.context.profile.name,
                topic=topic,
                provider=self.context.provider.metadata.provider,
                model=self.context.provider.metadata.model,
            )
            self.context.logger.log(
                "teammate_session_memory_updated",
                agent=self.context.profile.name,
                topic=topic,
                memory_turns=memory_turns,
                cached_replies=len(self._local_memory),
            )
            return generated
        except Exception as exc:
            self.context.logger.log(
                "teammate_provider_reply_fallback",
                agent=self.context.profile.name,
                topic=topic,
                error=f"{type(exc).__name__}: {exc}",
            )
            return fallback_reply

    def _run_task(self, task: Task) -> None:
        lock_paths = [str(pathlib.Path(path).resolve()) for path in task.locked_paths]
        if lock_paths and not self.context.file_locks.acquire(self.context.profile.name, lock_paths):
            self.context.board.defer(
                task_id=task.task_id,
                owner=self.context.profile.name,
                reason="file lock unavailable",
            )
            time.sleep(0.1)
            return

        self.context.logger.log(
            "task_started",
            task_id=task.task_id,
            agent=self.context.profile.name,
            task_type=task.task_type,
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject="task_started",
            body=f"{self.context.profile.name} started {task.task_id}",
            task_id=task.task_id,
        )
        handler = self._handlers.get(task.task_type)
        if handler is None:
            error = f"no handler registered for task_type={task.task_type}"
            self.context.board.fail(task_id=task.task_id, owner=self.context.profile.name, error=error)
            self.context.mailbox.send(
                sender=self.context.profile.name,
                recipient=self._get_lead_name_fn(self.context),
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            if lock_paths:
                self.context.file_locks.release(self.context.profile.name, lock_paths)
            return

        original_shared_state = self.context.shared_state
        task_context = build_task_context_snapshot(self.context, task)
        scoped_shared_state = ScopedSharedState(
            _underlying=original_shared_state,
            _visible_keys=set(task_context.get("visible_shared_state_keys", [])),
        )
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.bind_task(
                agent_name=self.context.profile.name,
                task=task,
                transport="in-process",
                task_context=task_context,
            )
        self.context.task_context = task_context
        self.context.shared_state = scoped_shared_state
        self.context.logger.log(
            "task_context_prepared",
            agent=self.context.profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            scope=str(task_context.get("scope", "")),
            visible_shared_state_keys=list(task_context.get("visible_shared_state_keys", [])),
            visible_shared_state_key_count=int(task_context.get("visible_shared_state_key_count", 0)),
            omitted_shared_state_key_count=int(task_context.get("omitted_shared_state_key_count", 0)),
            dependency_task_ids=list(task_context.get("dependencies", [])),
            transport="in-process",
        )
        try:
            result = handler(self.context, task)
            if self.context.session_registry is not None:
                self.context.session_state = self.context.session_registry.record_task_result(
                    agent_name=self.context.profile.name,
                    task=task,
                    transport="in-process",
                    success=True,
                    status="ready",
                )
            self.context.board.complete(task_id=task.task_id, owner=self.context.profile.name, result=result)
            self.context.mailbox.send(
                sender=self.context.profile.name,
                recipient=self._get_lead_name_fn(self.context),
                subject="task_completed",
                body=f"{task.task_id} done",
                task_id=task.task_id,
            )
        except Exception as exc:  # pragma: no cover - defensive path
            error = f"{type(exc).__name__}: {exc}"
            if self.context.session_registry is not None:
                self.context.session_state = self.context.session_registry.record_task_result(
                    agent_name=self.context.profile.name,
                    task=task,
                    transport="in-process",
                    success=False,
                    status="error",
                )
            self.context.board.fail(task_id=task.task_id, owner=self.context.profile.name, error=error)
            self.context.mailbox.send(
                sender=self.context.profile.name,
                recipient=self._get_lead_name_fn(self.context),
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            self.context.logger.log(
                "task_exception",
                task_id=task.task_id,
                agent=self.context.profile.name,
                traceback=self._traceback_module.format_exc(),
            )
        finally:
            self.context.shared_state = original_shared_state
            self.context.task_context = {}
            if lock_paths:
                self.context.file_locks.release(self.context.profile.name, lock_paths)

    def _auto_reply_peer_challenge(self, message: Message) -> None:
        question = message.body
        round_id = 1
        peer_name = ""
        peer_reply = ""
        try:
            parsed = json.loads(message.body)
            if isinstance(parsed, dict):
                question = str(parsed.get("question", message.body))
                round_id = int(parsed.get("round", 1))
                peer_name = str(parsed.get("peer_name", ""))
                peer_reply = str(parsed.get("peer_round1_reply", parsed.get("peer_round2_reply", "")))
        except json.JSONDecodeError:
            pass

        heading_issues = self.context.shared_state.get("heading_issues", [])
        length_issues = self.context.shared_state.get("length_issues", [])
        is_heading_specialist = self._profile_has_skill_fn(self.context.profile, "inventory")
        is_length_specialist = (
            self.context.profile.agent_type == "analyst" and not is_heading_specialist
        )
        if round_id == 1:
            if is_heading_specialist:
                reply = (
                    f"Concern on question '{question}': heading audit may miss files with non-standard markdown "
                    f"heading style. Suggest adding regex fallback and markdown lint rules. "
                    f"Current heading-gap files={len(heading_issues)}."
                )
            elif is_length_specialist:
                reply = (
                    f"Concern on question '{question}': line-count threshold is static and may over/under flag files. "
                    f"Suggest percentile-based threshold plus topic density score. "
                    f"Current long-file findings={len(length_issues)}."
                )
            else:
                reply = (
                    f"Concern on question '{question}': combine heading and length checks into a weighted quality score."
                )
            response_subject = "peer_challenge_round1_reply"
        else:
            if round_id == 2:
                if is_heading_specialist:
                    reply = (
                        f"Rebuttal to {peer_name}: static-threshold concern is valid, but complexity can be controlled by "
                        f"starting with two-tier thresholds. Improvement: use heading density as second signal. "
                        f"Peer said: {peer_reply[:220]}"
                    )
                elif is_length_specialist:
                    reply = (
                        f"Rebuttal to {peer_name}: heading-style concern is valid, but regex-only rules can create false "
                        f"positives. Improvement: combine parser-based checks with lint config baselines. "
                        f"Peer said: {peer_reply[:220]}"
                    )
                else:
                    reply = (
                        f"Rebuttal to {peer_name}: align both proposals into a single quality score with weighted signals."
                    )
                response_subject = "peer_challenge_round2_reply"
            else:
                if is_heading_specialist:
                    reply = (
                        f"Final proposal for '{question}': implement heading parser + lint fallback, "
                        f"acceptance check = 100% files with at least one heading, rollout in 2 phases. "
                        f"Resolved critique from {peer_name}: {peer_reply[:180]}"
                    )
                elif is_length_specialist:
                    reply = (
                        f"Final proposal for '{question}': switch to percentile thresholds (P85 line count) plus "
                        f"topic-density signal, acceptance check = <5% false positives in pilot. "
                        f"Resolved critique from {peer_name}: {peer_reply[:180]}"
                    )
                else:
                    reply = (
                        f"Final proposal for '{question}': combine both approaches into weighted scoring with CI gates."
                    )
                response_subject = "peer_challenge_round3_reply"

        provider_prompt = (
            f"Question: {question}\n"
            f"Round: {round_id}\n"
            f"Peer name: {peer_name or 'none'}\n"
            f"Peer context: {peer_reply[:260] if peer_reply else 'none'}\n"
            f"Current fallback proposal: {reply}"
        )
        reply = self._reply_with_provider(
            topic=f"peer_challenge_round{round_id}",
            prompt=provider_prompt,
            fallback_reply=reply,
        )

        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=message.sender,
            subject=response_subject,
            body=reply,
            task_id=message.task_id,
        )
        self.context.logger.log(
            "peer_challenge_reply_sent",
            sender=self.context.profile.name,
            recipient=message.sender,
            task_id=message.task_id,
        )

    def _auto_reply_evidence_request(self, message: Message) -> None:
        question = message.body
        source_score = "unknown"
        focus_areas: List[str] = []
        peer_name = ""
        peer_objection = ""
        target_previous_reply = ""
        try:
            parsed = json.loads(message.body)
            if isinstance(parsed, dict):
                question = str(parsed.get("question", message.body))
                source_score = str(parsed.get("source_score", "unknown"))
                focus_areas = [str(x) for x in parsed.get("focus_areas", [])]
                peer_name = str(parsed.get("peer_name", ""))
                peer_objection = str(parsed.get("peer_objection", ""))
                target_previous_reply = str(parsed.get("target_previous_reply", ""))
        except json.JSONDecodeError:
            pass

        heading_issues = self.context.shared_state.get("heading_issues", [])
        length_issues = self.context.shared_state.get("length_issues", [])
        if not focus_areas:
            focus_areas = ["depth"]

        role_note = ""
        is_heading_specialist = self._profile_has_skill_fn(self.context.profile, "inventory")
        is_length_specialist = (
            self.context.profile.agent_type == "analyst" and not is_heading_specialist
        )
        if is_heading_specialist:
            role_note = (
                f"Domain: heading quality. Current heading issues={len(heading_issues)} "
                f"(source score={source_score})."
            )
        elif is_length_specialist:
            role_note = (
                f"Domain: file length governance. Current long files={len(length_issues)} "
                f"(source score={source_score})."
            )
        else:
            role_note = f"Domain: synthesis. Source score={source_score}."

        segments: List[str] = [f"Evidence response for question: {question}", role_note]
        if target_previous_reply:
            segments.append(f"Previous proposal context: {target_previous_reply[:200]}")
        if "coverage" in focus_areas:
            segments.append(
                "Coverage evidence: define explicit acceptance checks, sample size, and pass/fail threshold."
            )
        if "rebuttal" in focus_areas:
            segments.append(
                f"Rebuttal evidence: directly address objection from {peer_name or 'peer'}: "
                f"{peer_objection[:180]}"
            )
        if "depth" in focus_areas:
            segments.append(
                "Depth evidence: provide phased rollout timeline, monitoring KPIs, and rollback trigger."
            )
        if is_heading_specialist:
            segments.append(
                "Plan: parser+linter dual validation; KPI=100% files with top-level heading; rollback if lint noise >20%."
            )
        elif is_length_specialist:
            segments.append(
                "Plan: percentile threshold (P85) pilot; KPI=false positives <5%; rollback if >10%."
            )
        else:
            segments.append("Plan: combine both tracks into staged rollout with CI quality gates.")
        reply = " ".join(segments)
        provider_prompt = (
            f"Evidence question: {question}\n"
            f"Focus areas: {', '.join(focus_areas)}\n"
            f"Peer name: {peer_name or 'none'}\n"
            f"Peer objection: {peer_objection[:220] if peer_objection else 'none'}\n"
            f"Previous reply: {target_previous_reply[:220] if target_previous_reply else 'none'}\n"
            f"Current fallback proposal: {reply}"
        )
        reply = self._reply_with_provider(
            topic="evidence_reply",
            prompt=provider_prompt,
            fallback_reply=reply,
        )

        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=message.sender,
            subject="evidence_reply",
            body=reply,
            task_id=message.task_id,
        )
        self.context.logger.log(
            "evidence_reply_sent",
            sender=self.context.profile.name,
            recipient=message.sender,
            task_id=message.task_id,
        )

    def run(self) -> None:
        session_transport = "in-process"
        if (
            not self.claim_tasks
            and self.context.runtime_config.teammate_mode == "tmux"
            and self.context.profile.agent_type == "analyst"
        ):
            session_transport = str(self.context.session_state.get("transport", "") or "")
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.record_status(
                agent_name=self.context.profile.name,
                transport=session_transport,
                status="ready",
            )
        self.context.logger.log(
            "teammate_session_started",
            agent=self.context.profile.name,
            transport=str(self.context.session_state.get("transport", "") or session_transport or "in-process"),
            session_id=str(self.context.session_state.get("session_id", "") or ""),
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject="agent_ready",
            body=f"{self.context.profile.name} online with skills {sorted(self.context.profile.skills)}",
        )
        last_idle_hook_emit_ts = 0.0
        while not self.stop_event.is_set():
            messages = self.context.mailbox.pull(self.context.profile.name)
            for message in messages:
                if self.context.session_registry is not None:
                    self.context.session_state = self.context.session_registry.record_message_seen(
                        agent_name=self.context.profile.name,
                        message=message,
                    )
                self.context.logger.log(
                    "agent_mail_seen",
                    agent=self.context.profile.name,
                    from_agent=message.sender,
                    subject=message.subject,
                )
                if message.subject in {
                    "peer_challenge_round1_request",
                    "peer_challenge_round2_request",
                    "peer_challenge_round3_request",
                }:
                    self._auto_reply_peer_challenge(message)
                if message.subject == "evidence_request":
                    self._auto_reply_evidence_request(message)

            if self.claim_tasks:
                task = self.context.board.claim_next(
                    agent_name=self.context.profile.name,
                    agent_skills=self.context.profile.skills,
                    agent_type=self.context.profile.agent_type,
                )
                if task is not None:
                    self._run_task(task)
                    continue

            if self.context.board.all_terminal():
                break
            now = time.time()
            if now - last_idle_hook_emit_ts >= TEAMMATE_IDLE_HOOK_INTERVAL_SEC:
                self.context.logger.log(HOOK_EVENT_TEAMMATE_IDLE, agent=self.context.profile.name)
                last_idle_hook_emit_ts = now
            time.sleep(0.1)

        self.context.file_locks.release(self.context.profile.name)
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.record_status(
                agent_name=self.context.profile.name,
                transport=session_transport,
                status="stopped",
            )
        self.context.logger.log(
            "teammate_session_stopped",
            agent=self.context.profile.name,
            transport=str(self.context.session_state.get("transport", "") or session_transport or "in-process"),
            session_id=str(self.context.session_state.get("session_id", "") or ""),
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject="agent_stopped",
            body=f"{self.context.profile.name} stopped",
        )
