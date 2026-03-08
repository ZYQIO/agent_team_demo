#!/usr/bin/env python3

import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


TEST_DIR = pathlib.Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import agent_team_runtime as runtime
from agent_team.runtime.engine import run_lead_tasks_once
from agent_team.workflows import build_workflow_lead_task_order, build_workflow_runtime_metadata


class RuntimeLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = runtime.RuntimeConfig()

    def test_compute_adjudication_accept_full(self) -> None:
        peer_challenge = {
            "targets": ["a", "b"],
            "round1": {"received_replies": {"a": "x", "b": "y"}},
            "round2": {"received_replies": {"a": "z" * 220, "b": "k" * 220}},
        }
        result = runtime.compute_adjudication(peer_challenge, self.config)
        self.assertEqual(result["verdict"], "accept")
        self.assertGreaterEqual(result["score"], 90)
        self.assertAlmostEqual(result["rubric"]["completeness"], 1.0, places=3)

    def test_compute_adjudication_challenge_partial(self) -> None:
        config = runtime.RuntimeConfig(
            adjudication_accept_threshold=80,
            adjudication_challenge_threshold=40,
        )
        peer_challenge = {
            "targets": ["a", "b"],
            "round1": {"received_replies": {"a": "x"}},
            "round2": {"received_replies": {"a": "short"}},
        }
        result = runtime.compute_adjudication(peer_challenge, config)
        self.assertIn(result["verdict"], {"challenge", "reject"})
        self.assertTrue(0 <= result["score"] <= 100)

    def test_compute_evidence_bonus(self) -> None:
        config = runtime.RuntimeConfig(
            re_adjudication_max_bonus=20,
            re_adjudication_weight_coverage=0.7,
            re_adjudication_weight_depth=0.3,
        )
        evidence_pack = {
            "targets": ["a", "b"],
            "received_replies": {"a": "x" * 250, "b": "y" * 250},
        }
        result = runtime.compute_evidence_bonus(evidence_pack, config)
        self.assertEqual(result["max_bonus"], 20)
        self.assertGreaterEqual(result["bonus"], 15)
        self.assertLessEqual(result["bonus"], 20)

    def test_derive_evidence_focus_areas(self) -> None:
        adjudication = {
            "rubric": {
                "completeness": 0.5,
                "rebuttal_coverage": 0.4,
                "argument_depth": 0.3,
            }
        }
        focus = runtime.derive_evidence_focus_areas(adjudication)
        self.assertIn("coverage", focus)
        self.assertIn("rebuttal", focus)
        self.assertIn("depth", focus)

    def test_build_targeted_evidence_question(self) -> None:
        question = runtime.build_targeted_evidence_question(
            focus_areas=["coverage", "rebuttal", "depth"],
            peer_name="analyst_beta",
            peer_objection="Need better threshold strategy",
        )
        self.assertIn("Focus areas", question)
        self.assertIn("analyst_beta", question)
        self.assertIn("threshold strategy", question)

    def test_taskboard_claim_respects_allowed_agent_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = runtime.EventLogger(output_dir=pathlib.Path(tmp))
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="review_task",
                        title="Review-only task",
                        task_type="review",
                        required_skills={"analysis"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    )
                ],
                logger=logger,
            )

            denied = board.claim_next(
                agent_name="analyst_alpha",
                agent_skills={"analysis"},
                agent_type="analyst",
            )
            self.assertIsNone(denied)

            claimed = board.claim_next(
                agent_name="reviewer_gamma",
                agent_skills={"analysis", "review"},
                agent_type="reviewer",
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.task_id, "review_task")

    def test_task_completed_hook_event_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="finish_me",
                        title="Finish me",
                        task_type="review",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    )
                ],
                logger=logger,
            )
            task = board.claim_next(
                agent_name="reviewer_gamma",
                agent_skills={"review"},
                agent_type="reviewer",
            )
            self.assertIsNotNone(task)
            board.complete(task_id="finish_me", owner="reviewer_gamma", result={"ok": True})

            event_names = []
            with logger.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    payload = json.loads(line)
                    event_names.append(payload.get("event"))

            self.assertIn("task_completed", event_names)
            self.assertIn(runtime.HOOK_EVENT_TASK_COMPLETED, event_names)

    def test_dynamic_planning_inserts_followup_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="peer_challenge",
                        title="Peer challenge",
                        task_type="peer_challenge",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    )
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma", "analyst_alpha", "analyst_beta"],
                logger=logger,
            )
            shared_state = runtime.SharedState()
            shared_state.set("heading_issues", [{"path": "a.md"}])
            shared_state.set("length_issues", [{"path": "b.md"}])
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(enable_dynamic_tasks=True),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )
            result = runtime.handle_dynamic_planning(
                context=context,
                _task=runtime.Task(
                    task_id="dynamic_planning",
                    title="plan",
                    task_type="dynamic_planning",
                    required_skills={"review"},
                    dependencies=[],
                    payload={},
                    locked_paths=[],
                    allowed_agent_types={"reviewer"},
                ),
            )
            self.assertEqual(
                set(result["inserted_tasks"]),
                {"heading_structure_followup", "length_risk_followup"},
            )
            snapshot = board.snapshot()
            peer = [task for task in snapshot["tasks"] if task["task_id"] == "peer_challenge"][0]
            self.assertIn("heading_structure_followup", peer["dependencies"])
            self.assertIn("length_risk_followup", peer["dependencies"])

    def test_workflow_pack_declares_lead_task_order(self) -> None:
        self.assertEqual(
            build_workflow_lead_task_order("markdown-audit"),
            ["lead_adjudication", "lead_re_adjudication"],
        )

    def test_workflow_pack_exposes_runtime_metadata(self) -> None:
        metadata = build_workflow_runtime_metadata("markdown-audit")
        self.assertEqual(
            metadata.lead_task_order,
            ("lead_adjudication", "lead_re_adjudication"),
        )
        self.assertEqual(metadata.report_task_ids, ("recommendation_pack",))

    def test_run_lead_tasks_once_uses_workflow_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="lead_re_adjudication",
                        title="Lead re-adjudicates",
                        task_type="lead_re_adjudication",
                        required_skills={"lead"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"lead"},
                    ),
                    runtime.Task(
                        task_id="lead_adjudication",
                        title="Lead adjudicates",
                        task_type="lead_adjudication",
                        required_skills={"lead"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"lead"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead"], logger=logger)
            shared_state = runtime.SharedState()
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )
            call_order = []

            def _make_handler(task_name: str):
                def _handler(_context, _task):
                    call_order.append(task_name)
                    return {"task_name": task_name}

                return _handler

            handlers = {
                "lead_re_adjudication": _make_handler("lead_re_adjudication"),
                "lead_adjudication": _make_handler("lead_adjudication"),
            }

            ran_any = run_lead_tasks_once(
                lead_context=context,
                lead_task_order=["lead_re_adjudication", "lead_adjudication"],
                handlers=handlers,
            )

            self.assertTrue(ran_any)
            self.assertEqual(call_order, ["lead_re_adjudication", "lead_adjudication"])

    def test_teammate_provider_reply_generation_with_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(
                participants=["lead", "analyst_alpha"],
                logger=logger,
            )
            shared_state = runtime.SharedState()
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(
                    teammate_provider_replies=True,
                    teammate_memory_turns=2,
                ),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )
            worker = runtime.TeammateAgent(context=context, stop_event=threading.Event())
            reply = worker._reply_with_provider(
                topic="peer_challenge_round1",
                prompt="Identify one weak assumption and one concrete fix.",
                fallback_reply="fallback",
            )

            self.assertNotEqual(reply, "fallback")
            self.assertGreater(len(worker._local_memory), 0)

            event_names = []
            with logger.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    payload = json.loads(line)
                    event_names.append(payload.get("event"))
            self.assertIn("teammate_provider_reply_generated", event_names)

    def test_tmux_worker_payload_discover_and_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "docs"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\n", encoding="utf-8")

            discover = runtime.run_tmux_worker_payload(
                {
                    "task_type": "discover_markdown",
                    "task_payload": {},
                    "target_dir": str(target_dir),
                    "output_dir": str(output_dir),
                    "shared_state": {},
                }
            )
            self.assertIn("state_updates", discover)
            inventory = discover["state_updates"].get("markdown_inventory", [])
            self.assertEqual(len(inventory), 2)

            length = runtime.run_tmux_worker_payload(
                {
                    "task_type": "length_audit",
                    "task_payload": {"line_threshold": 2},
                    "target_dir": str(target_dir),
                    "output_dir": str(output_dir),
                    "shared_state": {"markdown_inventory": inventory},
                }
            )
            self.assertEqual(length["result"]["line_threshold"], 2)
            self.assertGreaterEqual(length["result"]["long_files"], 1)

    def test_tmux_worker_fallback_to_subprocess_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            config = runtime.RuntimeConfig(
                teammate_mode="tmux",
                tmux_fallback_on_error=True,
            )
            tmux_failed = subprocess.CompletedProcess(
                args=["tmux"],
                returncode=124,
                stdout="",
                stderr="tmux timeout",
            )
            subprocess_ok = subprocess.CompletedProcess(
                args=["python"],
                returncode=0,
                stdout=json.dumps({"result": {"ok": True}, "state_updates": {}}),
                stderr="",
            )
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime, "_execute_worker_tmux", return_value=tmux_failed
            ), mock.patch.object(runtime, "_execute_worker_subprocess", return_value=subprocess_ok):
                result = runtime._run_tmux_worker_task(
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                    output_dir=output_dir,
                    runtime_config=config,
                    payload={"task_type": "discover_markdown"},
                    worker_name="analyst_alpha",
                    logger=logger,
                    timeout_sec=1,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result.get("transport"), "tmux->subprocess_fallback")

    def test_task_from_dict_normalizes_in_progress_state(self) -> None:
        restored = runtime.task_from_dict(
            {
                "task_id": "x",
                "title": "t",
                "task_type": "heading_audit",
                "required_skills": ["analysis"],
                "dependencies": [],
                "payload": {},
                "locked_paths": [],
                "allowed_agent_types": ["analyst"],
                "status": "in_progress",
                "owner": "analyst_alpha",
            }
        )
        self.assertEqual(restored.status, "pending")
        self.assertIsNone(restored.owner)

    def test_resolve_checkpoint_by_history_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            history_dir = output_dir / runtime.CHECKPOINT_HISTORY_DIRNAME
            history_dir.mkdir(parents=True, exist_ok=True)
            path0 = history_dir / "checkpoint_000000.json"
            path1 = history_dir / "checkpoint_000001.json"
            path0.write_text("{}", encoding="utf-8")
            path1.write_text("{}", encoding="utf-8")

            resolved = runtime.resolve_checkpoint_by_history_index(output_dir=output_dir, history_index=1)
            self.assertEqual(resolved, path1)

            with self.assertRaises(ValueError):
                runtime.resolve_checkpoint_by_history_index(output_dir=output_dir, history_index=9)

    def test_resolve_checkpoint_by_event_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            history_dir = output_dir / runtime.CHECKPOINT_HISTORY_DIRNAME
            history_dir.mkdir(parents=True, exist_ok=True)
            for idx, event_count in enumerate([2, 5, 9]):
                payload = {
                    "version": runtime.CHECKPOINT_VERSION,
                    "saved_at": runtime.utc_now(),
                    "history_index": idx,
                    "event_count": event_count,
                    "task_board": {"tasks": []},
                }
                (history_dir / f"checkpoint_{idx:06d}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            resolved = runtime.resolve_checkpoint_by_event_index(output_dir=output_dir, event_index=6)
            self.assertEqual(resolved["resolved_history_index"], 1)
            self.assertEqual(resolved["resolved_checkpoint_event_count"], 5)
            self.assertEqual(resolved["resolution"], "at_or_before")

    def test_event_logger_recover_next_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir, truncate=True)
            logger.log("a")
            logger.log("b")
            resumed_logger = runtime.EventLogger(output_dir=output_dir, truncate=False)
            resumed_logger.log("c")
            lines = (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            payloads = [json.loads(line) for line in lines if line.strip()]
            self.assertEqual([item.get("event_index") for item in payloads], [0, 1, 2])

    def test_seed_branch_events_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            events_path = source / "events.jsonl"
            with events_path.open("w", encoding="utf-8") as fh:
                for idx in range(5):
                    fh.write(json.dumps({"event": "x", "event_index": idx}) + "\n")

            meta = runtime.seed_branch_events_from_source(
                source_output_dir=source,
                target_output_dir=target,
                max_event_index=2,
            )
            self.assertTrue(meta["seeded"])
            self.assertEqual(meta["seeded_count"], 3)

            target_lines = (target / "events.jsonl").read_text(encoding="utf-8").splitlines()
            target_payloads = [json.loads(line) for line in target_lines if line.strip()]
            self.assertEqual([item.get("event_index") for item in target_payloads], [0, 1, 2])

    def test_replay_task_states_from_events(self) -> None:
        events = [
            {"event_index": 0, "event": "task_inserted", "task_id": "a", "title": "A"},
            {"event_index": 1, "event": "task_claimed", "task_id": "a", "agent": "analyst_alpha"},
            {"event_index": 2, "event": "task_completed", "task_id": "a", "owner": "analyst_alpha"},
            {"event_index": 3, "event": "task_inserted", "task_id": "b", "title": "B"},
            {"event_index": 4, "event": "task_claimed", "task_id": "b", "agent": "analyst_beta"},
            {"event_index": 5, "event": "task_failed", "task_id": "b", "owner": "analyst_beta"},
        ]
        replay = runtime.replay_task_states_from_events(events=events, max_transitions=20)
        tasks = replay.get("tasks", {})
        self.assertEqual(tasks["a"]["status"], "completed")
        self.assertEqual(tasks["b"]["status"], "failed")
        status_counts = replay.get("status_counts", {})
        self.assertEqual(status_counts.get("completed"), 1)
        self.assertEqual(status_counts.get("failed"), 1)

    def test_build_runtime_config_rewind_branch_requires_rewind_index(self) -> None:
        args = type(
            "Args",
            (),
            {
                "rewind_to_history_index": -1,
                "rewind_to_event_index": -1,
                "history_replay_start_index": -1,
                "history_replay_end_index": -1,
                "event_replay_max_transitions": 200,
                "rewind_branch": True,
                "rewind_branch_output": "",
                "max_completed_tasks": 0,
                "peer_wait_seconds": 1.0,
                "evidence_wait_seconds": 1.0,
                "adjudication_challenge_threshold": 50,
                "adjudication_accept_threshold": 75,
                "adjudication_weight_completeness": 0.45,
                "adjudication_weight_rebuttal_coverage": 0.35,
                "adjudication_weight_argument_depth": 0.2,
                "re_adjudication_max_bonus": 15,
                "teammate_memory_turns": 4,
                "re_adjudication_weight_coverage": 0.6,
                "re_adjudication_weight_depth": 0.4,
                "teammate_mode": "in-process",
                "enable_dynamic_tasks": True,
                "teammate_provider_replies": False,
                "auto_round3_on_challenge": True,
                "tmux_worker_timeout_sec": 120,
                "tmux_fallback_on_error": True,
            },
        )()
        with self.assertRaises(ValueError):
            runtime.build_runtime_config_from_args(args)

    def test_write_history_replay_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            history_dir = output_dir / runtime.CHECKPOINT_HISTORY_DIRNAME
            history_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(3):
                payload = {
                    "version": runtime.CHECKPOINT_VERSION,
                    "saved_at": runtime.utc_now(),
                    "resume_from": "",
                    "interrupted_reason": "",
                    "task_board": {
                        "tasks": [
                            {"task_id": "a", "status": "completed" if idx > 0 else "pending"},
                            {"task_id": "b", "status": "completed" if idx > 1 else "pending"},
                        ]
                    },
                }
                (history_dir / f"checkpoint_{idx:06d}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            report_path = output_dir / "checkpoint_replay.md"
            meta = runtime.write_history_replay_report(
                output_dir=output_dir,
                report_path=report_path,
                start_index=0,
                end_index=2,
            )
            self.assertEqual(meta["snapshot_count"], 3)
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("# Checkpoint History Replay", text)
            self.assertIn("## Timeline", text)

    def test_taskboard_fail_propagates_to_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="a",
                        title="A",
                        task_type="analysis",
                        required_skills={"analysis"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                    ),
                    runtime.Task(
                        task_id="b",
                        title="B",
                        task_type="review",
                        required_skills={"review"},
                        dependencies=["a"],
                        payload={},
                        locked_paths=[],
                    ),
                ],
                logger=logger,
            )
            task = board.claim_next(
                agent_name="analyst_alpha",
                agent_skills={"analysis"},
                agent_type="analyst",
            )
            self.assertIsNotNone(task)
            board.fail(task_id="a", owner="analyst_alpha", error="boom")
            snapshot = board.snapshot()
            statuses = {item["task_id"]: item["status"] for item in snapshot["tasks"]}
            self.assertEqual(statuses["a"], "failed")
            self.assertEqual(statuses["b"], "failed")

    def test_build_profiles_accepts_custom_team_config(self) -> None:
        custom_team = runtime.TeamConfig(
            lead_name="lead",
            agents=[
                runtime.TeamAgentConfig(
                    name="doc_analyst",
                    skills=["analysis"],
                    agent_type="analyst",
                ),
                runtime.TeamAgentConfig(
                    name="doc_reviewer",
                    skills=["review", "writer"],
                    agent_type="reviewer",
                ),
            ],
        )
        profiles = runtime.build_profiles(team_config=custom_team)
        self.assertEqual([profile.name for profile in profiles], ["doc_analyst", "doc_reviewer"])
        self.assertEqual(profiles[0].agent_type, "analyst")
        self.assertIn("analysis", profiles[0].skills)

    def test_build_agent_team_config_from_args_loads_json_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = pathlib.Path(tmp) / "agent-team.json"
            config_path.write_text(
                json.dumps(
                    {
                        "host": {"kind": "codex"},
                        "model": {"provider_name": "openai", "model": "gpt-4.1-mini"},
                        "team": {
                            "agents": [
                                {
                                    "name": "doc_analyst",
                                    "agent_type": "analyst",
                                    "skills": ["analysis"],
                                }
                            ]
                        },
                        "workflow": {"pack": "markdown-audit", "preset": "custom"},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {
                    "config": str(config_path),
                    "host_kind": "",
                    "workflow_pack": "",
                    "workflow_preset": "",
                    "provider": "heuristic",
                    "model": "heuristic-v1",
                    "openai_api_key_env": "OPENAI_API_KEY",
                    "openai_base_url": "https://api.openai.com/v1",
                    "require_llm": False,
                    "provider_timeout_sec": 60,
                },
            )()
            team_config = runtime.build_agent_team_config_from_args(
                args=args,
                runtime_config=runtime.RuntimeConfig(),
            )
            self.assertEqual(team_config.host.kind, "codex")
            self.assertEqual(team_config.model.provider_name, "openai")
            self.assertEqual(team_config.model.model, "gpt-4.1-mini")
            self.assertEqual(team_config.workflow.preset, "custom")
            self.assertEqual(team_config.team.agents[0].name, "doc_analyst")


if __name__ == "__main__":
    unittest.main()
