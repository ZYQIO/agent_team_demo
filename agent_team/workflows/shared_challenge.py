from __future__ import annotations

import json
import time
from typing import Any, Dict

from ..core import Task, utc_now
from ..runtime.adjudication import (
    build_targeted_evidence_question,
    compute_adjudication,
    compute_evidence_bonus,
    derive_evidence_focus_areas,
)
from .team_shared import (
    get_latest_agent_reply,
    get_team_member_names,
)


def _payload_question(task: Task, key: str, default: str) -> str:
    value = task.payload.get(key, default)
    return str(value or default)


def handle_peer_challenge(context: Any, task: Task) -> Dict[str, Any]:
    question_round1 = _payload_question(
        task=task,
        key="round1_question",
        default="Identify one weak assumption in the current workflow findings and propose one concrete fix.",
    )
    question_round2 = _payload_question(
        task=task,
        key="round2_question",
        default="Critique the other analyst's proposal and suggest one improvement.",
    )
    question_round3 = _payload_question(
        task=task,
        key="round3_question",
        default="Provide a revised final proposal with measurable checks and rollout order.",
    )
    request_targets = get_team_member_names(context=context, agent_type="analyst")
    if not request_targets:
        request_targets = get_team_member_names(
            context=context,
            exclude=[context.profile.name],
        )
    timeout_sec = float(task.payload.get("wait_seconds", context.runtime_config.peer_wait_seconds))

    def collect_replies(expected_subject: str, max_wait_sec: float) -> Dict[str, str]:
        deadline = time.time() + max_wait_sec
        replies: Dict[str, str] = {}
        while time.time() < deadline and len(replies) < len(request_targets):
            incoming = context.mailbox.pull_matching(
                context.profile.name,
                lambda m: m.subject == expected_subject and m.task_id == task.task_id,
            )
            for message in incoming:
                replies[message.sender] = message.body
                context.logger.log(
                    "peer_challenge_reply_received",
                    requester=context.profile.name,
                    from_agent=message.sender,
                    task_id=task.task_id,
                    round_subject=expected_subject,
                )
            if len(replies) == len(request_targets):
                break
            time.sleep(0.1)
        return replies

    round1_payload = {
        "round": 1,
        "question": question_round1,
        "requested_by": context.profile.name,
        "requested_at": utc_now(),
    }
    for recipient in request_targets:
        context.mailbox.send(
            sender=context.profile.name,
            recipient=recipient,
            subject="peer_challenge_round1_request",
            body=json.dumps(round1_payload, ensure_ascii=False),
            task_id=task.task_id,
        )
    context.logger.log(
        "peer_challenge_round_started",
        requester=context.profile.name,
        targets=request_targets,
        task_id=task.task_id,
        round=1,
    )
    round1_replies = collect_replies("peer_challenge_round1_reply", timeout_sec)

    round2_payload_template = {
        "round": 2,
        "question": question_round2,
        "requested_by": context.profile.name,
        "requested_at": utc_now(),
    }
    for recipient in request_targets:
        peers = [name for name in request_targets if name != recipient]
        peer_name = peers[0] if peers else ""
        payload = dict(round2_payload_template)
        payload["peer_name"] = peer_name
        payload["peer_round1_reply"] = round1_replies.get(peer_name, "")
        context.mailbox.send(
            sender=context.profile.name,
            recipient=recipient,
            subject="peer_challenge_round2_request",
            body=json.dumps(payload, ensure_ascii=False),
            task_id=task.task_id,
        )
    context.logger.log(
        "peer_challenge_round_started",
        requester=context.profile.name,
        targets=request_targets,
        task_id=task.task_id,
        round=2,
    )
    round2_replies = collect_replies("peer_challenge_round2_reply", timeout_sec)

    missing_round1 = [name for name in request_targets if name not in round1_replies]
    missing_round2 = [name for name in request_targets if name not in round2_replies]
    record: Dict[str, Any] = {
        "targets": request_targets,
        "round1": {
            "question": question_round1,
            "received_replies": round1_replies,
            "missing_replies": missing_round1,
        },
        "round2": {
            "question": question_round2,
            "received_replies": round2_replies,
            "missing_replies": missing_round2,
        },
        "timeout_sec": timeout_sec,
    }
    provisional = compute_adjudication(record, context.runtime_config)
    record["provisional_adjudication"] = provisional

    auto_round3 = bool(
        task.payload.get("auto_round3_on_challenge", context.runtime_config.auto_round3_on_challenge)
    )
    if auto_round3 and provisional.get("verdict") in {"challenge", "reject"}:
        round3_payload_template = {
            "round": 3,
            "question": question_round3,
            "requested_by": context.profile.name,
            "requested_at": utc_now(),
        }
        context.logger.log(
            "peer_challenge_round3_triggered",
            requester=context.profile.name,
            task_id=task.task_id,
            provisional_verdict=provisional.get("verdict"),
            provisional_score=provisional.get("score"),
        )
        for recipient in request_targets:
            peers = [name for name in request_targets if name != recipient]
            peer_name = peers[0] if peers else ""
            payload = dict(round3_payload_template)
            payload["peer_name"] = peer_name
            payload["peer_round2_reply"] = round2_replies.get(peer_name, "")
            context.mailbox.send(
                sender=context.profile.name,
                recipient=recipient,
                subject="peer_challenge_round3_request",
                body=json.dumps(payload, ensure_ascii=False),
                task_id=task.task_id,
            )
        context.logger.log(
            "peer_challenge_round_started",
            requester=context.profile.name,
            targets=request_targets,
            task_id=task.task_id,
            round=3,
        )
        round3_replies = collect_replies("peer_challenge_round3_reply", timeout_sec)
        missing_round3 = [name for name in request_targets if name not in round3_replies]
        record["round3"] = {
            "question": question_round3,
            "received_replies": round3_replies,
            "missing_replies": missing_round3,
        }
        record["post_round3_adjudication"] = compute_adjudication(record, context.runtime_config)

    context.shared_state.set("peer_challenge", record)
    return record


def handle_lead_adjudication(context: Any, _task: Task) -> Dict[str, Any]:
    peer_challenge = context.board.get_task_result("peer_challenge") or {}
    result = compute_adjudication(peer_challenge=peer_challenge, config=context.runtime_config)
    context.shared_state.set("lead_adjudication", result)
    context.mailbox.broadcast(
        sender=context.profile.name,
        subject="lead_verdict",
        body=json.dumps(result, ensure_ascii=False),
    )
    context.logger.log("lead_adjudication_published", **result)
    return result


def handle_evidence_pack(context: Any, task: Task) -> Dict[str, Any]:
    initial_adjudication = context.board.get_task_result("lead_adjudication") or {}
    peer_challenge = context.board.get_task_result("peer_challenge") or {}
    initial_verdict = str(initial_adjudication.get("verdict", ""))
    default_targets = get_team_member_names(context=context, agent_type="analyst")
    targets = list(initial_adjudication.get("targets", default_targets))
    focus_areas = derive_evidence_focus_areas(initial_adjudication)
    if initial_verdict != "challenge":
        result = {
            "triggered": False,
            "reason": f"initial verdict is {initial_verdict or 'unknown'}",
            "targets": targets,
            "focus_areas": focus_areas,
            "received_replies": {},
            "missing_replies": targets,
        }
        context.shared_state.set("evidence_pack", result)
        return result

    timeout_sec = float(task.payload.get("wait_seconds", context.runtime_config.evidence_wait_seconds))
    per_target_questions: Dict[str, str] = {}
    for recipient in targets:
        peers = [name for name in targets if name != recipient]
        peer_name = peers[0] if peers else ""
        peer_objection = get_latest_agent_reply(peer_challenge, peer_name)
        target_previous_reply = get_latest_agent_reply(peer_challenge, recipient)
        question = build_targeted_evidence_question(
            focus_areas=focus_areas,
            peer_name=peer_name,
            peer_objection=peer_objection,
        )
        request_payload = {
            "question": question,
            "focus_areas": focus_areas,
            "requested_by": context.profile.name,
            "requested_at": utc_now(),
            "source_verdict": initial_verdict,
            "source_score": initial_adjudication.get("score"),
            "peer_name": peer_name,
            "peer_objection": peer_objection,
            "target_previous_reply": target_previous_reply,
        }
        context.mailbox.send(
            sender=context.profile.name,
            recipient=recipient,
            subject="evidence_request",
            body=json.dumps(request_payload, ensure_ascii=False),
            task_id=task.task_id,
        )
        per_target_questions[recipient] = question
    context.logger.log(
        "evidence_round_started",
        requester=context.profile.name,
        task_id=task.task_id,
        targets=targets,
        focus_areas=focus_areas,
    )

    deadline = time.time() + timeout_sec
    replies: Dict[str, str] = {}
    while time.time() < deadline and len(replies) < len(targets):
        incoming = context.mailbox.pull_matching(
            context.profile.name,
            lambda m: m.subject == "evidence_reply" and m.task_id == task.task_id,
        )
        for message in incoming:
            replies[message.sender] = message.body
            context.logger.log(
                "evidence_reply_received",
                requester=context.profile.name,
                from_agent=message.sender,
                task_id=task.task_id,
            )
        if len(replies) == len(targets):
            break
        time.sleep(0.1)

    missing = [name for name in targets if name not in replies]
    result = {
        "triggered": True,
        "question": "Targeted evidence requests were sent per agent.",
        "focus_areas": focus_areas,
        "per_target_questions": per_target_questions,
        "targets": targets,
        "received_replies": replies,
        "missing_replies": missing,
        "timeout_sec": timeout_sec,
        "source_verdict": initial_verdict,
        "source_score": initial_adjudication.get("score"),
    }
    context.shared_state.set("evidence_pack", result)
    return result


def handle_lead_re_adjudication(context: Any, _task: Task) -> Dict[str, Any]:
    initial = context.board.get_task_result("lead_adjudication") or {}
    evidence_pack = context.board.get_task_result("evidence_pack") or {}
    initial_verdict = str(initial.get("verdict", ""))
    initial_score = int(initial.get("score", 0))
    thresholds = initial.get(
        "thresholds",
        {
            "accept": int(context.runtime_config.adjudication_accept_threshold),
            "challenge": int(context.runtime_config.adjudication_challenge_threshold),
        },
    )
    accept_threshold = int(thresholds.get("accept", context.runtime_config.adjudication_accept_threshold))
    challenge_threshold = int(
        thresholds.get("challenge", context.runtime_config.adjudication_challenge_threshold)
    )

    if initial_verdict != "challenge" or not evidence_pack.get("triggered"):
        result = dict(initial)
        result.update(
            {
                "re_adjudicated": False,
                "initial_verdict": initial_verdict,
                "initial_score": initial_score,
                "evidence_bonus": 0,
                "final_verdict": initial_verdict,
                "final_score": initial_score,
            }
        )
        context.shared_state.set("lead_re_adjudication", result)
        context.logger.log("lead_re_adjudication_skipped", verdict=initial_verdict, score=initial_score)
        return result

    bonus_eval = compute_evidence_bonus(evidence_pack=evidence_pack, config=context.runtime_config)
    bonus = int(bonus_eval.get("bonus", 0))
    final_score = min(100, initial_score + bonus)
    if final_score >= accept_threshold:
        final_verdict = "accept"
    elif final_score >= challenge_threshold:
        final_verdict = "challenge"
    else:
        final_verdict = "reject"

    result = {
        "re_adjudicated": True,
        "initial_verdict": initial_verdict,
        "initial_score": initial_score,
        "evidence_bonus": bonus,
        "evidence_evaluation": bonus_eval,
        "final_verdict": final_verdict,
        "final_score": final_score,
        "verdict": final_verdict,
        "score": final_score,
        "thresholds": {"accept": accept_threshold, "challenge": challenge_threshold},
        "weights": initial.get("weights", {}),
        "rationale": (
            f"Re-adjudicated after supplemental evidence. Bonus={bonus} "
            f"(initial={initial_score}, final={final_score})."
        ),
        "targets": initial.get("targets", []),
    }
    context.shared_state.set("lead_re_adjudication", result)
    context.mailbox.broadcast(
        sender=context.profile.name,
        subject="lead_re_verdict",
        body=json.dumps(result, ensure_ascii=False),
    )
    context.logger.log("lead_re_adjudication_published", **result)
    return result
