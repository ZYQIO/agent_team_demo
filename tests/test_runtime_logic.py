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
        self.assertEqual(
            build_workflow_lead_task_order("repo-audit"),
            ["lead_adjudication", "lead_re_adjudication"],
        )

    def test_workflow_pack_exposes_runtime_metadata(self) -> None:
        metadata = build_workflow_runtime_metadata("markdown-audit")
        self.assertEqual(
            metadata.lead_task_order,
            ("lead_adjudication", "lead_re_adjudication"),
        )
        self.assertEqual(metadata.report_task_ids, ("recommendation_pack",))
        repo_metadata = build_workflow_runtime_metadata("repo-audit")
        self.assertEqual(
            repo_metadata.lead_task_order,
            ("lead_adjudication", "lead_re_adjudication"),
        )
        self.assertEqual(repo_metadata.report_task_ids, ("repo_recommendation_pack",))

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

    def test_run_lead_tasks_once_uses_external_runner_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="lead_adjudication",
                        title="Lead adjudicates",
                        task_type="lead_adjudication",
                        required_skills={"lead"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"lead"},
                    )
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
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )
            handler_called = []

            def handler(_context, _task):
                handler_called.append(True)
                return {"should_not": "run"}

            ran_any = run_lead_tasks_once(
                lead_context=context,
                lead_task_order=["lead_adjudication"],
                handlers={"lead_adjudication": handler},
                external_task_runner=lambda _context, task: {
                    "ok": True,
                    "result": {"verdict": "accept", "score": 81},
                    "state_updates": {"lead_adjudication": {"verdict": "accept", "score": 81}},
                    "board_mutations": {},
                    "task_id": task.task_id,
                },
            )

            self.assertTrue(ran_any)
            self.assertEqual(handler_called, [])
            self.assertEqual(board.get_task_result("lead_adjudication"), {"verdict": "accept", "score": 81})
            self.assertEqual(shared_state.get("lead_adjudication", {}).get("verdict"), "accept")

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

    def test_tmux_worker_payload_repo_discover_and_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "repo"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "README.md").write_text("# Title\nbody\n", encoding="utf-8")
            (target_dir / "src").mkdir(parents=True, exist_ok=True)
            (target_dir / "src" / "app.py").write_text("\n".join(["print('x')"] * 40), encoding="utf-8")
            (target_dir / "config.json").write_text("{\"ok\": true}\n", encoding="utf-8")

            discover = runtime.run_tmux_worker_payload(
                {
                    "task_type": "discover_repository",
                    "task_payload": {},
                    "target_dir": str(target_dir),
                    "output_dir": str(output_dir),
                    "shared_state": {},
                }
            )
            inventory = discover["state_updates"].get("repository_inventory", [])
            self.assertEqual(len(inventory), 3)

            extension_audit = runtime.run_tmux_worker_payload(
                {
                    "task_type": "extension_audit",
                    "task_payload": {},
                    "target_dir": str(target_dir),
                    "output_dir": str(output_dir),
                    "shared_state": {"repository_inventory": inventory},
                }
            )
            self.assertGreaterEqual(extension_audit["result"]["unique_extensions"], 2)

            large_file = runtime.run_tmux_worker_payload(
                {
                    "task_type": "large_file_audit",
                    "task_payload": {"line_threshold": 20, "byte_threshold": 10},
                    "target_dir": str(target_dir),
                    "output_dir": str(output_dir),
                    "shared_state": {"repository_inventory": inventory},
                }
            )
            self.assertEqual(large_file["result"]["line_threshold"], 20)
            self.assertGreaterEqual(large_file["result"]["oversized_files"], 1)

    def test_repo_dynamic_planning_inserts_followup_tasks(self) -> None:
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
            shared_state.set(
                "repository_inventory",
                [
                    {
                        "path": "README.md",
                        "extension": ".md",
                        "line_count": 10,
                        "byte_count": 100,
                        "top_level_dir": ".",
                    },
                    {
                        "path": "src/app.py",
                        "extension": ".py",
                        "line_count": 350,
                        "byte_count": 2000,
                        "top_level_dir": "src",
                    },
                ],
            )
            shared_state.set("repository_extension_summary", {"unique_extensions": 2})
            shared_state.set(
                "repository_large_files",
                [{"path": "src/app.py", "line_count": 350, "byte_count": 2000}],
            )
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

            from agent_team.workflows.repo_audit_analysis import handle_repo_dynamic_planning

            result = handle_repo_dynamic_planning(
                context=context,
                _task=runtime.Task(
                    task_id="repo_dynamic_planning",
                    title="plan",
                    task_type="repo_dynamic_planning",
                    required_skills={"review"},
                    dependencies=[],
                    payload={},
                    locked_paths=[],
                    allowed_agent_types={"reviewer"},
                ),
            )
            self.assertEqual(
                set(result["inserted_tasks"]),
                {"extension_hotspot_followup", "directory_hotspot_followup"},
            )
            snapshot = board.snapshot()
            peer = [task for task in snapshot["tasks"] if task["task_id"] == "peer_challenge"][0]
            self.assertIn("extension_hotspot_followup", peer["dependencies"])
            self.assertIn("directory_hotspot_followup", peer["dependencies"])

    def test_execute_worker_tmux_timeout_records_lifecycle_and_cleans_ipc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            session_name = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{session_name}.stdout.txt"
            stderr_file = ipc_dir / f"{session_name}.stderr.txt"
            status_file = ipc_dir / f"{session_name}.status.txt"

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout="",
                        stderr="no server running on /tmp/tmux-501/default",
                    )
                if command[:2] == ["tmux", "new-session"]:
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("partial worker stdout", encoding="utf-8")
                    stderr_file.write_text("", encoding="utf-8")
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "kill-session"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run), mock.patch.object(
                runtime.tmux_transport.time, "time", side_effect=[100.0, 102.0]
            ), mock.patch.object(runtime.tmux_transport.time, "sleep", return_value=None):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                )

            self.assertEqual(completed.returncode, 124)
            self.assertEqual(completed.stdout, "partial worker stdout")
            self.assertIn("timed out", completed.stderr)
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertEqual(lifecycle.get("tmux_session_name"), session_name)
            self.assertEqual(lifecycle.get("tmux_preferred_session_name"), session_prefix)
            self.assertEqual(lifecycle.get("tmux_session_name_strategy"), "preferred")
            self.assertFalse(lifecycle.get("tmux_preferred_session_retried"))
            self.assertTrue(lifecycle.get("tmux_preferred_session_reused"))
            self.assertTrue(lifecycle.get("tmux_session_started"))
            self.assertFalse(lifecycle.get("tmux_status_observed"))
            self.assertTrue(lifecycle.get("tmux_timed_out"))
            self.assertEqual(lifecycle.get("tmux_cleanup_result"), "killed")
            self.assertEqual(lifecycle.get("tmux_ipc_cleanup_result"), "removed")
            self.assertEqual(lifecycle.get("tmux_ipc_files_removed"), 2)
            self.assertFalse(stdout_file.exists())
            self.assertFalse(stderr_file.exists())
            self.assertFalse(status_file.exists())

    def test_execute_worker_tmux_retries_duplicate_session_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            first_session = session_prefix
            second_session = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            second_stdout = ipc_dir / f"{second_session}.stdout.txt"
            second_stderr = ipc_dir / f"{second_session}.stderr.txt"
            second_status = ipc_dir / f"{second_session}.status.txt"
            killed_sessions = []
            spawn_calls = 0

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                nonlocal spawn_calls
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout="",
                        stderr="no server running on /tmp/tmux-501/default",
                    )
                if command[:2] == ["tmux", "new-session"]:
                    session_name = command[4]
                    if session_name == first_session and spawn_calls == 0:
                        spawn_calls += 1
                        return subprocess.CompletedProcess(
                            args=command,
                            returncode=1,
                            stdout="",
                            stderr=f"duplicate session: {first_session}",
                        )
                    if session_name == second_session:
                        spawn_calls += 1
                        ipc_dir.mkdir(parents=True, exist_ok=True)
                        second_stdout.write_text("worker ok", encoding="utf-8")
                        second_stderr.write_text("", encoding="utf-8")
                        second_status.write_text("0", encoding="utf-8")
                        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "kill-session"]:
                    killed_sessions.append(command[-1])
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ), mock.patch.object(runtime.tmux_transport.time, "sleep", return_value=None):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "worker ok")
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertEqual(lifecycle.get("tmux_session_name"), second_session)
            self.assertEqual(lifecycle.get("tmux_preferred_session_name"), session_prefix)
            self.assertEqual(lifecycle.get("tmux_session_name_strategy"), "preferred")
            self.assertTrue(lifecycle.get("tmux_preferred_session_retried"))
            self.assertTrue(lifecycle.get("tmux_preferred_session_reused"))
            self.assertTrue(lifecycle.get("tmux_session_started"))
            self.assertEqual(lifecycle.get("tmux_spawn_attempts"), 2)
            self.assertTrue(lifecycle.get("tmux_spawn_retried"))
            self.assertIn("duplicate session", lifecycle.get("tmux_spawn_retry_reason", ""))
            self.assertTrue(lifecycle.get("tmux_stale_session_cleanup_attempted"))
            self.assertEqual(lifecycle.get("tmux_stale_session_name"), first_session)
            self.assertEqual(lifecycle.get("tmux_stale_session_cleanup_result"), "killed")
            self.assertFalse(lifecycle.get("tmux_stale_session_cleanup_retry_attempted"))
            self.assertEqual(lifecycle.get("tmux_cleanup_result"), "killed")
            self.assertEqual(lifecycle.get("tmux_ipc_cleanup_result"), "removed")
            self.assertEqual(killed_sessions, [first_session, second_session])
            self.assertFalse(second_stdout.exists())
            self.assertFalse(second_stderr.exists())
            self.assertFalse(second_status.exists())

    def test_execute_worker_tmux_retries_active_cleanup_when_session_still_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            session_name = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{session_name}.stdout.txt"
            stderr_file = ipc_dir / f"{session_name}.stderr.txt"
            status_file = ipc_dir / f"{session_name}.status.txt"
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout="",
                        stderr="no server running on /tmp/tmux-501/default",
                    )
                if command[:2] == ["tmux", "new-session"]:
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("worker ok", encoding="utf-8")
                    stderr_file.write_text("", encoding="utf-8")
                    status_file.write_text("0", encoding="utf-8")
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "kill-session"] and len(calls) == 3:
                    return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="permission denied")
                if command[:2] == ["tmux", "has-session"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "kill-session"] and len(calls) == 5:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "worker ok")
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertEqual(lifecycle.get("tmux_session_name"), session_name)
            self.assertEqual(lifecycle.get("tmux_session_name_strategy"), "preferred")
            self.assertTrue(lifecycle.get("tmux_preferred_session_reused"))
            self.assertEqual(lifecycle.get("tmux_cleanup_result"), "recovered_after_retry")
            self.assertTrue(lifecycle.get("tmux_cleanup_retry_attempted"))
            self.assertEqual(lifecycle.get("tmux_cleanup_retry_result"), "killed")
            self.assertTrue(lifecycle.get("tmux_session_exists_after_cleanup"))
            self.assertEqual(
                calls,
                [
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    ["tmux", "new-session", "-d", "-s", session_name, mock.ANY],
                    ["tmux", "kill-session", "-t", session_name],
                    ["tmux", "has-session", "-t", session_name],
                    ["tmux", "kill-session", "-t", session_name],
                ],
            )

    def test_execute_worker_tmux_cleans_orphan_sessions_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            orphan_session = f"{session_prefix}_orphaned"
            active_session = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{active_session}.stdout.txt"
            stderr_file = ipc_dir / f"{active_session}.stderr.txt"
            status_file = ipc_dir / f"{active_session}.status.txt"
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout=f"{orphan_session}\nunrelated_session\n",
                        stderr="",
                    )
                if command[:2] == ["tmux", "kill-session"] and command[-1] in {
                    orphan_session,
                    active_session,
                }:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "new-session"]:
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("worker ok", encoding="utf-8")
                    stderr_file.write_text("", encoding="utf-8")
                    status_file.write_text("0", encoding="utf-8")
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                )

            self.assertEqual(completed.returncode, 0)
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertTrue(lifecycle.get("tmux_orphan_cleanup_attempted"))
            self.assertEqual(lifecycle.get("tmux_session_name"), active_session)
            self.assertEqual(lifecycle.get("tmux_session_name_strategy"), "preferred")
            self.assertFalse(lifecycle.get("tmux_preferred_session_found_preflight"))
            self.assertEqual(lifecycle.get("tmux_orphan_sessions_found"), 1)
            self.assertEqual(lifecycle.get("tmux_orphan_sessions_cleaned"), 1)
            self.assertEqual(lifecycle.get("tmux_orphan_sessions_failed"), 0)
            self.assertEqual(lifecycle.get("tmux_orphan_failed_sessions"), [])
            self.assertEqual(
                calls,
                [
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    ["tmux", "kill-session", "-t", orphan_session],
                    ["tmux", "new-session", "-d", "-s", active_session, mock.ANY],
                    ["tmux", "kill-session", "-t", active_session],
                ],
            )

    def test_execute_worker_tmux_reuses_existing_preferred_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            session_name = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{session_name}.stdout.txt"
            stderr_file = ipc_dir / f"{session_name}.stderr.txt"
            status_file = ipc_dir / f"{session_name}.status.txt"
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout=f"{session_name}\n",
                        stderr="",
                    )
                if command[:2] == ["tmux", "new-session"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout="",
                        stderr=f"duplicate session: {session_name}",
                    )
                if command[:2] == ["tmux", "respawn-pane"]:
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("reused session ok", encoding="utf-8")
                    stderr_file.write_text("", encoding="utf-8")
                    status_file.write_text("0", encoding="utf-8")
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "kill-session"] and command[-1] == session_name:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                    allow_existing_session_reuse=True,
                )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "reused session ok")
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertEqual(lifecycle.get("tmux_session_name"), session_name)
            self.assertTrue(lifecycle.get("tmux_preferred_session_found_preflight"))
            self.assertEqual(lifecycle.get("tmux_session_name_strategy"), "preferred_reused_existing")
            self.assertTrue(lifecycle.get("tmux_preferred_session_reuse_attempted"))
            self.assertEqual(lifecycle.get("tmux_preferred_session_reuse_result"), "respawned")
            self.assertEqual(lifecycle.get("tmux_preferred_session_reuse_error"), "")
            self.assertTrue(lifecycle.get("tmux_preferred_session_reused_existing"))
            self.assertTrue(lifecycle.get("tmux_preferred_session_reused"))
            self.assertFalse(lifecycle.get("tmux_preferred_session_retried"))
            self.assertEqual(
                calls,
                [
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    ["tmux", "new-session", "-d", "-s", session_name, mock.ANY],
                    ["tmux", "respawn-pane", "-k", "-t", f"{session_name}:0.0", mock.ANY],
                    ["tmux", "kill-session", "-t", session_name],
                ],
            )

    def test_execute_worker_tmux_cleans_exact_session_when_reuse_not_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            session_name = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{session_name}.stdout.txt"
            stderr_file = ipc_dir / f"{session_name}.stderr.txt"
            status_file = ipc_dir / f"{session_name}.status.txt"
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout=f"{session_name}\n",
                        stderr="",
                    )
                if command[:2] == ["tmux", "kill-session"] and command[-1] == session_name:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command[:2] == ["tmux", "new-session"]:
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("fresh session ok", encoding="utf-8")
                    stderr_file.write_text("", encoding="utf-8")
                    status_file.write_text("0", encoding="utf-8")
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                )

            self.assertEqual(completed.returncode, 0)
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertTrue(lifecycle.get("tmux_preferred_session_found_preflight"))
            self.assertFalse(lifecycle.get("tmux_preferred_session_reuse_authorized"))
            self.assertFalse(lifecycle.get("tmux_preferred_session_reuse_attempted"))
            self.assertFalse(lifecycle.get("tmux_preferred_session_reused_existing"))
            self.assertEqual(
                calls,
                [
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    ["tmux", "kill-session", "-t", session_name],
                    ["tmux", "new-session", "-d", "-s", session_name, mock.ANY],
                    ["tmux", "kill-session", "-t", session_name],
                ],
            )

    def test_execute_worker_tmux_retains_preferred_session_for_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            session_name = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{session_name}.stdout.txt"
            stderr_file = ipc_dir / f"{session_name}.stderr.txt"
            status_file = ipc_dir / f"{session_name}.status.txt"
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout="",
                        stderr="no server running on /tmp/tmux-501/default",
                    )
                if command[:2] == ["tmux", "new-session"]:
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("worker ok", encoding="utf-8")
                    stderr_file.write_text("", encoding="utf-8")
                    status_file.write_text("0", encoding="utf-8")
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run):
                completed = runtime._execute_worker_tmux(
                    command=[sys.executable, "-c", "print('hi')"],
                    workdir=workdir,
                    session_prefix=session_prefix,
                    timeout_sec=1,
                    retain_session_for_reuse=True,
                )

            self.assertEqual(completed.returncode, 0)
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertTrue(lifecycle.get("tmux_reuse_retention_requested"))
            self.assertTrue(lifecycle.get("tmux_session_retained_for_reuse"))
            self.assertEqual(lifecycle.get("tmux_cleanup_result"), "leased_for_reuse")
            self.assertTrue(lifecycle.get("tmux_session_exists_after_cleanup"))
            self.assertFalse(stdout_file.exists())
            self.assertFalse(stderr_file.exists())
            self.assertFalse(status_file.exists())
            self.assertEqual(
                calls,
                [
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    ["tmux", "new-session", "-d", "-s", session_name, mock.ANY],
                ],
            )

    def test_run_tmux_analyst_task_once_sets_reuse_hint_when_future_tasks_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="heading_audit",
                        title="Heading audit",
                        task_type="heading_audit",
                        required_skills={"analysis"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                    runtime.Task(
                        task_id="length_audit",
                        title="Length audit",
                        task_type="length_audit",
                        required_skills={"analysis"},
                        dependencies=[],
                        payload={"line_threshold": 10},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead", "analyst_alpha"], logger=logger)
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
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )
            analyst_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            ]
            retain_flags = []
            allow_reuse_flags = []

            def fake_run_worker_task(**kwargs):
                retain_flags.append(bool(kwargs.get("retain_session_for_reuse", False)))
                allow_reuse_flags.append(bool(kwargs.get("allow_existing_session_reuse", False)))
                return {
                    "ok": True,
                    "transport": "tmux",
                    "payload": {"result": {"ok": True}, "state_updates": {}},
                    "diagnostics": {
                        "tmux_session_retained_for_reuse": kwargs.get("retain_session_for_reuse", False),
                        "tmux_preferred_session_reused_existing": kwargs.get(
                            "allow_existing_session_reuse", False
                        ),
                    },
                }

            ran = runtime.tmux_transport.run_tmux_analyst_task_once(
                lead_context=lead_context,
                analyst_profiles=analyst_profiles,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                run_worker_task_fn=fake_run_worker_task,
                supported_task_types=runtime.TMUX_ANALYST_TASK_TYPES,
                worker_timeout_sec=10,
            )

            self.assertTrue(ran)
            self.assertEqual(retain_flags, [True])
            self.assertEqual(allow_reuse_flags, [False])
            lease_entry = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            self.assertEqual(lease_entry.get("status"), "retained")
            self.assertTrue(lease_entry.get("retained_for_reuse"))

    def test_run_tmux_analyst_task_once_authorizes_reuse_from_lease_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="heading_audit",
                        title="Heading audit",
                        task_type="heading_audit",
                        required_skills={"analysis"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    )
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead", "analyst_alpha"], logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set(
                "tmux_session_leases",
                {
                    "analyst_alpha": {
                        "worker": "analyst_alpha",
                        "session_name": "agent_analyst_alpha",
                        "status": "retained",
                        "reuse_count": 0,
                    }
                },
            )
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )
            analyst_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            ]
            allow_reuse_flags = []

            def fake_run_worker_task(**kwargs):
                allow_reuse_flags.append(bool(kwargs.get("allow_existing_session_reuse", False)))
                return {
                    "ok": True,
                    "transport": "tmux",
                    "payload": {"result": {"ok": True}, "state_updates": {}},
                    "diagnostics": {
                        "tmux_preferred_session_reused_existing": True,
                        "tmux_preferred_session_reuse_authorized": kwargs.get(
                            "allow_existing_session_reuse", False
                        ),
                        "tmux_preferred_session_name": "agent_analyst_alpha",
                        "tmux_cleanup_result": "killed",
                    },
                }

            ran = runtime.tmux_transport.run_tmux_analyst_task_once(
                lead_context=lead_context,
                analyst_profiles=analyst_profiles,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                run_worker_task_fn=fake_run_worker_task,
                supported_task_types=runtime.TMUX_ANALYST_TASK_TYPES,
                worker_timeout_sec=10,
            )

            self.assertTrue(ran)
            self.assertEqual(allow_reuse_flags, [True])
            lease_entry = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            self.assertEqual(lease_entry.get("status"), "released")
            self.assertTrue(lease_entry.get("reuse_authorized"))
            self.assertTrue(lease_entry.get("reused_existing"))
            self.assertEqual(lease_entry.get("reuse_count"), 1)

    def test_cleanup_tmux_worker_sessions_sweeps_preferred_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            shared_state = runtime.SharedState()
            shared_state.set(
                "tmux_session_leases",
                {
                    "analyst_alpha": {
                        "worker": "analyst_alpha",
                        "session_name": "agent_analyst_alpha",
                        "status": "retained",
                    },
                    "analyst_beta": {
                        "worker": "analyst_beta",
                        "session_name": "agent_analyst_beta",
                        "status": "retained",
                    },
                    "reviewer_gamma": {
                        "worker": "reviewer_gamma",
                        "session_name": "agent_reviewer_gamma",
                        "status": "retained",
                    },
                },
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=mock.Mock(),
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=mock.Mock(),
                mailbox=mock.Mock(),
                file_locks=mock.Mock(),
                shared_state=shared_state,
                logger=logger,
            )
            worker_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="analyst_beta", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer"),
            ]
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command == ["tmux", "kill-session", "-t", "agent_analyst_alpha"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command == ["tmux", "kill-session", "-t", "agent_analyst_beta"]:
                    return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="can't find session")
                if command == ["tmux", "kill-session", "-t", "agent_reviewer_gamma"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ):
                summary = runtime.cleanup_tmux_worker_sessions(
                    lead_context=lead_context,
                    worker_profiles=worker_profiles,
                )

            self.assertEqual(summary["cleaned"], 2)
            self.assertEqual(summary["already_exited"], 1)
            self.assertEqual(summary["failed"], [])
            self.assertEqual(
                shared_state.get("tmux_session_cleanup_summary"),
                summary,
            )
            leases = shared_state.get("tmux_session_leases", {})
            self.assertEqual(leases.get("analyst_alpha", {}).get("status"), "cleanup_swept")
            self.assertEqual(leases.get("analyst_alpha", {}).get("last_cleanup_result"), "killed")
            self.assertEqual(leases.get("analyst_beta", {}).get("status"), "cleanup_swept")
            self.assertEqual(leases.get("analyst_beta", {}).get("last_cleanup_result"), "already_exited")
            self.assertEqual(leases.get("reviewer_gamma", {}).get("status"), "cleanup_swept")
            self.assertEqual(leases.get("reviewer_gamma", {}).get("last_cleanup_result"), "killed")
            self.assertEqual(
                calls,
                [
                    ["tmux", "kill-session", "-t", "agent_analyst_alpha"],
                    ["tmux", "kill-session", "-t", "agent_analyst_beta"],
                    ["tmux", "kill-session", "-t", "agent_reviewer_gamma"],
                ],
            )

    def test_cleanup_tmux_worker_sessions_can_defer_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            shared_state = runtime.SharedState()
            shared_state.set("tmux_cleanup_deferred_for_resume", True)
            shared_state.set("tmux_cleanup_deferred_reason", "max_completed_tasks reached (3)")
            shared_state.set(
                "tmux_session_leases",
                {
                    "analyst_alpha": {
                        "worker": "analyst_alpha",
                        "session_name": "agent_analyst_alpha",
                        "status": "retained",
                    }
                },
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=mock.Mock(),
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=mock.Mock(),
                mailbox=mock.Mock(),
                file_locks=mock.Mock(),
                shared_state=shared_state,
                logger=logger,
            )
            worker_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer"),
            ]

            with mock.patch.object(runtime.tmux_transport.subprocess, "run") as tmux_run:
                summary = runtime.cleanup_tmux_worker_sessions(
                    lead_context=lead_context,
                    worker_profiles=worker_profiles,
                )

            tmux_run.assert_not_called()
            self.assertEqual(summary["skipped"], "deferred_for_resume")
            self.assertEqual(summary["deferred_reason"], "max_completed_tasks reached (3)")
            lease_entry = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            self.assertEqual(lease_entry.get("status"), "retained")

    def test_recover_tmux_worker_sessions_marks_retained_session_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            shared_state = runtime.SharedState()
            shared_state.set(
                "tmux_session_leases",
                {
                    "analyst_alpha": {
                        "worker": "analyst_alpha",
                        "session_name": "agent_analyst_alpha",
                        "status": "retained",
                    }
                },
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=mock.Mock(),
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=mock.Mock(),
                mailbox=mock.Mock(),
                file_locks=mock.Mock(),
                shared_state=shared_state,
                logger=logger,
            )
            worker_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer"),
            ]

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                if command == ["tmux", "has-session", "-t", "agent_analyst_alpha"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ):
                summary = runtime.recover_tmux_worker_sessions(
                    lead_context=lead_context,
                    worker_profiles=worker_profiles,
                    resume_from=output_dir / "run_checkpoint.json",
                )

            self.assertEqual(summary["recovered"], ["analyst_alpha"])
            lease_entry = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            self.assertEqual(lease_entry.get("status"), "recovered_available")
            self.assertTrue(lease_entry.get("reuse_authorized"))
            self.assertEqual(lease_entry.get("recovery_result"), "available")
            self.assertEqual(
                shared_state.get("tmux_session_recovery_summary", {}).get("recovered"),
                ["analyst_alpha"],
            )

    def test_recover_tmux_worker_sessions_marks_missing_retained_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            shared_state = runtime.SharedState()
            shared_state.set(
                "tmux_session_leases",
                {
                    "analyst_alpha": {
                        "worker": "analyst_alpha",
                        "session_name": "agent_analyst_alpha",
                        "status": "retained",
                    },
                    "analyst_beta": {
                        "worker": "analyst_beta",
                        "session_name": "agent_analyst_beta",
                        "status": "cleanup_swept",
                    },
                    "reviewer_gamma": {
                        "worker": "reviewer_gamma",
                        "session_name": "agent_reviewer_gamma",
                        "status": "retained",
                    },
                },
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=mock.Mock(),
                runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                board=mock.Mock(),
                mailbox=mock.Mock(),
                file_locks=mock.Mock(),
                shared_state=shared_state,
                logger=logger,
            )
            worker_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="analyst_beta", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer"),
            ]

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                if command == ["tmux", "has-session", "-t", "agent_analyst_alpha"]:
                    return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="can't find session")
                if command == ["tmux", "has-session", "-t", "agent_reviewer_gamma"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ):
                summary = runtime.recover_tmux_worker_sessions(
                    lead_context=lead_context,
                    worker_profiles=worker_profiles,
                )

            self.assertEqual(summary["missing"], ["analyst_alpha"])
            self.assertEqual(summary["inactive"], ["analyst_beta"])
            self.assertEqual(summary["recovered"], ["reviewer_gamma"])
            alpha_lease = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            beta_lease = shared_state.get("tmux_session_leases", {}).get("analyst_beta", {})
            reviewer_lease = shared_state.get("tmux_session_leases", {}).get("reviewer_gamma", {})
            self.assertEqual(alpha_lease.get("status"), "recovered_missing")
            self.assertFalse(alpha_lease.get("reuse_authorized"))
            self.assertEqual(alpha_lease.get("recovery_result"), "missing")
            self.assertEqual(beta_lease.get("status"), "recovery_inactive")
            self.assertEqual(beta_lease.get("recovery_result"), "inactive")
            self.assertEqual(reviewer_lease.get("status"), "recovered_available")
            self.assertEqual(reviewer_lease.get("recovery_result"), "available")

    def test_tmux_worker_payload_supports_dynamic_planning_board_mutations(self) -> None:
        payload = {
            "task_id": "dynamic_planning",
            "task_type": "dynamic_planning",
            "task_payload": {},
            "runtime_config": runtime.RuntimeConfig(enable_dynamic_tasks=True).to_dict(),
            "workflow_pack": "markdown-audit",
            "goal": "test",
            "target_dir": ".",
            "output_dir": ".",
            "profile": {"name": "reviewer_gamma", "skills": ["review"], "agent_type": "reviewer"},
            "provider_config": {"provider_name": "heuristic", "model": "heuristic-v1"},
            "task_ids": ["dynamic_planning", "peer_challenge"],
            "task_results": {},
            "shared_state": {
                "heading_issues": [{"path": "a.md"}],
                "length_issues": [{"path": "b.md"}],
            },
        }

        result = runtime.tmux_transport.run_tmux_worker_payload(payload)

        self.assertEqual(
            set(result["result"]["inserted_tasks"]),
            {"heading_structure_followup", "length_risk_followup"},
        )
        mutations = result["board_mutations"]
        added_task_ids = {item["task_id"] for item in mutations["add_tasks"]}
        self.assertEqual(added_task_ids, {"heading_structure_followup", "length_risk_followup"})
        added_dependencies = {(item["task_id"], item["dependency_id"]) for item in mutations["add_dependencies"]}
        self.assertEqual(
            added_dependencies,
            {
                ("peer_challenge", "heading_structure_followup"),
                ("peer_challenge", "length_risk_followup"),
            },
        )

    def test_tmux_worker_payload_supports_markdown_llm_synthesis(self) -> None:
        payload = {
            "task_id": "llm_synthesis",
            "task_type": "llm_synthesis",
            "task_payload": {},
            "runtime_config": runtime.RuntimeConfig().to_dict(),
            "workflow_pack": "markdown-audit",
            "goal": "test",
            "target_dir": ".",
            "output_dir": ".",
            "profile": {"name": "reviewer_gamma", "skills": ["review", "llm"], "agent_type": "reviewer"},
            "provider_config": {"provider_name": "heuristic", "model": "heuristic-v1"},
            "task_ids": [
                "heading_audit",
                "length_audit",
                "dynamic_planning",
                "peer_challenge",
                "lead_adjudication",
                "lead_re_adjudication",
                "evidence_pack",
            ],
            "task_results": {
                "heading_audit": {"files_without_headings": 1},
                "length_audit": {"long_files": 1},
                "dynamic_planning": {"inserted_tasks": ["length_risk_followup"]},
                "peer_challenge": {"targets": ["analyst_alpha"]},
                "lead_adjudication": {"verdict": "accept", "score": 82},
                "lead_re_adjudication": {"verdict": "accept", "score": 84},
                "evidence_pack": {"triggered": False},
            },
            "shared_state": {
                "heading_issues": [{"path": "a.md"}],
                "length_issues": [{"path": "b.md"}],
            },
        }

        result = runtime.tmux_transport.run_tmux_worker_payload(payload)

        self.assertIn("preview", result["result"])
        self.assertIn("llm_synthesis", result["state_updates"])

    def test_tmux_worker_payload_supports_lead_adjudication(self) -> None:
        payload = {
            "task_id": "lead_adjudication",
            "task_type": "lead_adjudication",
            "task_payload": {},
            "runtime_config": runtime.RuntimeConfig().to_dict(),
            "workflow_pack": "markdown-audit",
            "goal": "test",
            "target_dir": ".",
            "output_dir": ".",
            "profile": {"name": "lead", "skills": ["lead"], "agent_type": "lead"},
            "provider_config": {"provider_name": "heuristic", "model": "heuristic-v1"},
            "task_ids": ["peer_challenge", "lead_adjudication"],
            "task_results": {
                "peer_challenge": {
                    "targets": ["analyst_alpha", "analyst_beta"],
                    "round1": {"received_replies": {"analyst_alpha": "x", "analyst_beta": "y"}},
                    "round2": {
                        "received_replies": {
                            "analyst_alpha": "z" * 220,
                            "analyst_beta": "k" * 220,
                        }
                    },
                }
            },
            "shared_state": {},
        }

        result = runtime.tmux_transport.run_tmux_worker_payload(payload)

        self.assertEqual(result["result"]["verdict"], "accept")
        self.assertEqual(result["state_updates"]["lead_adjudication"]["verdict"], "accept")

    def test_tmux_worker_payload_supports_lead_re_adjudication(self) -> None:
        payload = {
            "task_id": "lead_re_adjudication",
            "task_type": "lead_re_adjudication",
            "task_payload": {},
            "runtime_config": runtime.RuntimeConfig(re_adjudication_max_bonus=20).to_dict(),
            "workflow_pack": "markdown-audit",
            "goal": "test",
            "target_dir": ".",
            "output_dir": ".",
            "profile": {"name": "lead", "skills": ["lead"], "agent_type": "lead"},
            "provider_config": {"provider_name": "heuristic", "model": "heuristic-v1"},
            "task_ids": ["lead_adjudication", "evidence_pack", "lead_re_adjudication"],
            "task_results": {
                "lead_adjudication": {
                    "verdict": "challenge",
                    "score": 70,
                    "thresholds": {"accept": 75, "challenge": 50},
                    "weights": {"completeness": 0.4},
                    "targets": ["analyst_alpha", "analyst_beta"],
                },
                "evidence_pack": {
                    "triggered": True,
                    "targets": ["analyst_alpha", "analyst_beta"],
                    "received_replies": {
                        "analyst_alpha": "x" * 240,
                        "analyst_beta": "y" * 240,
                    },
                },
            },
            "shared_state": {},
        }

        result = runtime.tmux_transport.run_tmux_worker_payload(payload)

        self.assertTrue(result["result"]["re_adjudicated"])
        self.assertGreaterEqual(result["result"]["final_score"], 75)
        self.assertEqual(result["state_updates"]["lead_re_adjudication"]["verdict"], "accept")

    def test_mailbox_bridge_proxy_round_trips_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            requests_dir = root / "requests"
            responses_dir = root / "responses"
            logger = runtime.EventLogger(output_dir=root / "out")
            mailbox = runtime.Mailbox(
                participants=["reviewer_gamma", "analyst_alpha"],
                logger=logger,
            )
            context = type(
                "Context",
                (),
                {
                    "profile": runtime.AgentProfile(
                        name="reviewer_gamma",
                        skills={"review"},
                        agent_type="reviewer",
                    ),
                    "mailbox": mailbox,
                    "logger": logger,
                },
            )()
            stop_event = threading.Event()
            server = threading.Thread(
                target=runtime._serve_mailbox_bridge,
                kwargs={
                    "context": context,
                    "requests_dir": requests_dir,
                    "responses_dir": responses_dir,
                    "stop_event": stop_event,
                },
                daemon=True,
            )
            server.start()
            proxy = runtime.tmux_transport._WorkerMailboxBridge(
                requests_dir=requests_dir,
                responses_dir=responses_dir,
            )

            proxy.send(
                sender="reviewer_gamma",
                recipient="analyst_alpha",
                subject="hello",
                body="world",
                task_id="peer_challenge",
            )
            pulled = mailbox.pull("analyst_alpha")
            self.assertEqual(len(pulled), 1)
            self.assertEqual(pulled[0].subject, "hello")

            mailbox.send(
                sender="analyst_alpha",
                recipient="reviewer_gamma",
                subject="peer_challenge_round1_reply",
                body="reply body",
                task_id="peer_challenge",
            )
            matched = proxy.pull_matching(
                "reviewer_gamma",
                lambda message: message.subject == "peer_challenge_round1_reply"
                and message.task_id == "peer_challenge",
            )
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].body, "reply body")

            stop_event.set()
            server.join(timeout=2.0)

    def test_worker_event_bridge_replays_into_main_logger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=root / "out")
            context = type("Context", (), {"logger": logger})()
            event_bridge_path = root / "worker_events.jsonl"
            event_bridge_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event": "lead_adjudication_published",
                                "fields": {"verdict": "accept", "score": 80},
                            },
                            ensure_ascii=False,
                        ),
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            runtime._replay_worker_bridge_events(context=context, event_bridge_path=event_bridge_path)

            self.assertFalse(event_bridge_path.exists())
            events = (root / "out" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("lead_adjudication_published", events)

    def test_run_team_tmux_invokes_recovery_callback_before_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            resume_from = output_dir / runtime.CHECKPOINT_FILENAME
            output_dir.mkdir(parents=True, exist_ok=True)
            resume_from.write_text(
                json.dumps(
                    {
                        "version": runtime.CHECKPOINT_VERSION,
                        "saved_at": runtime.utc_now(),
                        "goal": "test",
                        "target_dir": str(target_dir),
                        "output_dir": str(output_dir),
                        "runtime_config": runtime.RuntimeConfig(teammate_mode="tmux").to_dict(),
                        "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                        "task_board": {
                            "tasks": [
                                {
                                    "task_id": "done",
                                    "title": "Done",
                                    "task_type": "lead_adjudication",
                                    "required_skills": ["lead"],
                                    "dependencies": [],
                                    "payload": {},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["lead"],
                                    "status": "completed",
                                    "owner": "lead",
                                },
                                {
                                    "task_id": "pending",
                                    "title": "Pending",
                                    "task_type": "lead_re_adjudication",
                                    "required_skills": ["lead"],
                                    "dependencies": [],
                                    "payload": {},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["lead"],
                                    "status": "pending",
                                    "owner": None,
                                },
                            ]
                        },
                        "shared_state": {},
                    }
                ),
                encoding="utf-8",
            )
            workflow_pack = mock.Mock()
            workflow_pack.build_handlers.return_value = {}
            workflow_pack.build_tasks.return_value = []
            workflow_pack.runtime_metadata = mock.Mock(lead_task_order=(), report_task_ids=())
            calls = []

            def fake_worker_factory(**_kwargs):
                return threading.Thread(target=lambda: None)

            def fake_recovery(lead_context, worker_profiles, resume_from):
                calls.append(
                    {
                        "lead": lead_context.profile.name,
                        "workers": [profile.name for profile in worker_profiles],
                        "resume_from": str(resume_from) if resume_from else "",
                    }
                )
                return {"recovered": []}

            with mock.patch("agent_team.runtime.engine.resolve_workflow_pack", return_value=workflow_pack):
                exit_code = runtime.run_team_impl(
                    goal="test",
                    target_dir=target_dir,
                    output_dir=output_dir,
                    runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                    provider_name="heuristic",
                    model="heuristic-v1",
                    openai_api_key_env="OPENAI_API_KEY",
                    openai_base_url="https://api.openai.com/v1",
                    require_llm=False,
                    provider_timeout_sec=5,
                    resume_from=resume_from,
                    max_completed_tasks=1,
                    teammate_agent_factory=fake_worker_factory,
                    run_tmux_analyst_task_once_fn=lambda **_kwargs: False,
                    recover_tmux_analyst_sessions_fn=fake_recovery,
                    cleanup_tmux_analyst_sessions_fn=lambda *_args, **_kwargs: {"cleaned": 0},
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                calls,
                [
                    {
                        "lead": "lead",
                        "workers": ["lead", "analyst_alpha", "analyst_beta", "reviewer_gamma"],
                        "resume_from": str(resume_from),
                    }
                ],
            )

    def test_run_team_tmux_invokes_cleanup_callback_on_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            workflow_pack = mock.Mock()
            workflow_pack.build_handlers.return_value = {}
            workflow_pack.build_tasks.return_value = []
            workflow_pack.runtime_metadata = mock.Mock(lead_task_order=(), report_task_ids=())
            cleanup_calls = []

            def fake_worker_factory(**_kwargs):
                return threading.Thread(target=lambda: None)

            def fake_cleanup(lead_context, worker_profiles):
                cleanup_calls.append(
                    {
                        "lead": lead_context.profile.name,
                        "workers": [profile.name for profile in worker_profiles],
                    }
                )
                return {"cleaned": len(worker_profiles)}

            with mock.patch("agent_team.runtime.engine.resolve_workflow_pack", return_value=workflow_pack):
                exit_code = runtime.run_team_impl(
                    goal="test",
                    target_dir=target_dir,
                    output_dir=output_dir,
                    runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                    provider_name="heuristic",
                    model="heuristic-v1",
                    openai_api_key_env="OPENAI_API_KEY",
                    openai_base_url="https://api.openai.com/v1",
                    require_llm=False,
                    provider_timeout_sec=5,
                    teammate_agent_factory=fake_worker_factory,
                    run_tmux_analyst_task_once_fn=lambda **_kwargs: False,
                    cleanup_tmux_analyst_sessions_fn=fake_cleanup,
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                cleanup_calls,
                [{"lead": "lead", "workers": ["lead", "analyst_alpha", "analyst_beta", "reviewer_gamma"]}],
            )

    def test_run_team_tmux_defers_cleanup_when_paused_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            resume_from = output_dir / runtime.CHECKPOINT_FILENAME
            output_dir.mkdir(parents=True, exist_ok=True)
            resume_from.write_text(
                json.dumps(
                    {
                        "version": runtime.CHECKPOINT_VERSION,
                        "saved_at": runtime.utc_now(),
                        "goal": "test",
                        "target_dir": str(target_dir),
                        "output_dir": str(output_dir),
                        "runtime_config": runtime.RuntimeConfig(teammate_mode="tmux").to_dict(),
                        "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                        "task_board": {
                            "tasks": [
                                {
                                    "task_id": "done",
                                    "title": "Done",
                                    "task_type": "lead_adjudication",
                                    "required_skills": ["lead"],
                                    "dependencies": [],
                                    "payload": {},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["lead"],
                                    "status": "completed",
                                    "owner": "lead",
                                },
                                {
                                    "task_id": "pending",
                                    "title": "Pending",
                                    "task_type": "lead_re_adjudication",
                                    "required_skills": ["lead"],
                                    "dependencies": [],
                                    "payload": {},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["lead"],
                                    "status": "pending",
                                    "owner": None,
                                },
                            ]
                        },
                        "shared_state": {},
                    }
                ),
                encoding="utf-8",
            )
            workflow_pack = mock.Mock()
            workflow_pack.build_handlers.return_value = {}
            workflow_pack.build_tasks.return_value = []
            workflow_pack.runtime_metadata = mock.Mock(lead_task_order=(), report_task_ids=())
            cleanup_flags = []

            def fake_worker_factory(**_kwargs):
                return threading.Thread(target=lambda: None)

            def fake_cleanup(lead_context, worker_profiles):
                cleanup_flags.append(
                    {
                        "deferred": bool(lead_context.shared_state.get("tmux_cleanup_deferred_for_resume", False)),
                        "reason": str(lead_context.shared_state.get("tmux_cleanup_deferred_reason", "")),
                        "workers": [profile.name for profile in worker_profiles],
                    }
                )
                return {"cleaned": 0}

            with mock.patch("agent_team.runtime.engine.resolve_workflow_pack", return_value=workflow_pack):
                exit_code = runtime.run_team_impl(
                    goal="test",
                    target_dir=target_dir,
                    output_dir=output_dir,
                    runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                    provider_name="heuristic",
                    model="heuristic-v1",
                    openai_api_key_env="OPENAI_API_KEY",
                    openai_base_url="https://api.openai.com/v1",
                    require_llm=False,
                    provider_timeout_sec=5,
                    resume_from=resume_from,
                    max_completed_tasks=1,
                    teammate_agent_factory=fake_worker_factory,
                    run_tmux_analyst_task_once_fn=lambda **_kwargs: False,
                    cleanup_tmux_analyst_sessions_fn=fake_cleanup,
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                cleanup_flags,
                [
                    {
                        "deferred": True,
                        "reason": "max_completed_tasks reached (1)",
                        "workers": ["lead", "analyst_alpha", "analyst_beta", "reviewer_gamma"],
                    }
                ],
            )

    def test_run_team_tmux_logs_cleanup_callback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            workflow_pack = mock.Mock()
            workflow_pack.build_handlers.return_value = {}
            workflow_pack.build_tasks.return_value = []
            workflow_pack.runtime_metadata = mock.Mock(lead_task_order=(), report_task_ids=())

            def fake_worker_factory(**_kwargs):
                return threading.Thread(target=lambda: None)

            def fake_cleanup(_lead_context, _analyst_profiles):
                raise RuntimeError("cleanup boom")

            with mock.patch("agent_team.runtime.engine.resolve_workflow_pack", return_value=workflow_pack):
                exit_code = runtime.run_team_impl(
                    goal="test",
                    target_dir=target_dir,
                    output_dir=output_dir,
                    runtime_config=runtime.RuntimeConfig(teammate_mode="tmux"),
                    provider_name="heuristic",
                    model="heuristic-v1",
                    openai_api_key_env="OPENAI_API_KEY",
                    openai_base_url="https://api.openai.com/v1",
                    require_llm=False,
                    provider_timeout_sec=5,
                    teammate_agent_factory=fake_worker_factory,
                    run_tmux_analyst_task_once_fn=lambda **_kwargs: False,
                    cleanup_tmux_analyst_sessions_fn=fake_cleanup,
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                )

            self.assertEqual(exit_code, 0)
            event_names = []
            with (output_dir / "events.jsonl").open("r", encoding="utf-8") as fh:
                for line in fh:
                    payload = json.loads(line)
                    event_names.append(payload.get("event"))
            self.assertIn("tmux_worker_session_cleanup_failed", event_names)

    def test_cleanup_stale_tmux_session_retries_when_session_still_exists(self) -> None:
        calls = []

        def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
            calls.append(command)
            if command[:2] == ["tmux", "kill-session"] and len(calls) == 1:
                return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="permission denied")
            if command[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
            if command[:2] == ["tmux", "kill-session"] and len(calls) == 3:
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {command}")

        with mock.patch.object(runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run):
            result = runtime.tmux_transport._cleanup_stale_tmux_session("agent_analyst_alpha_deadbeef")

        self.assertTrue(result.get("attempted"))
        self.assertEqual(result.get("session_name"), "agent_analyst_alpha_deadbeef")
        self.assertEqual(result.get("result"), "recovered_after_retry")
        self.assertTrue(result.get("retry_attempted"))
        self.assertEqual(result.get("retry_result"), "killed")
        self.assertTrue(result.get("session_exists_after_cleanup"))
        self.assertEqual(
            calls,
            [
                ["tmux", "kill-session", "-t", "agent_analyst_alpha_deadbeef"],
                ["tmux", "has-session", "-t", "agent_analyst_alpha_deadbeef"],
                ["tmux", "kill-session", "-t", "agent_analyst_alpha_deadbeef"],
            ],
        )

    def test_worker_subprocess_timeout_returns_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            config = runtime.RuntimeConfig(teammate_mode="subprocess")

            with mock.patch.object(
                runtime,
                "_execute_worker_subprocess",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["python"],
                    timeout=1,
                    output="partial stdout",
                    stderr="subprocess too slow",
                ),
            ):
                result = runtime._run_tmux_worker_task(
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                    output_dir=output_dir,
                    runtime_config=config,
                    payload={"task_type": "discover_markdown"},
                    worker_name="analyst_alpha",
                    logger=logger,
                    timeout_sec=1,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result.get("transport"), "subprocess")
            self.assertIn("timed out", result.get("error", ""))
            diagnostics = result.get("diagnostics", {})
            self.assertTrue(diagnostics.get("execution_timed_out"))
            self.assertEqual(diagnostics.get("timeout_transport"), "subprocess")
            self.assertEqual(diagnostics.get("timeout_phase"), "primary_subprocess")
            self.assertEqual(diagnostics.get("result"), "timeout")

            diagnostics_path = output_dir / "tmux_worker_diagnostics.jsonl"
            payload = json.loads(diagnostics_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertTrue(payload.get("execution_timed_out"))
            self.assertEqual(payload.get("timeout_phase"), "primary_subprocess")
            self.assertEqual(payload.get("result"), "timeout")

            event_names = []
            with logger.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        event_names.append(json.loads(line).get("event"))
            self.assertIn("tmux_worker_transport_timeout", event_names)

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
            tmux_failed.tmux_lifecycle = {
                "tmux_session_name": "agent_analyst_alpha_deadbeef",
                "tmux_preferred_session_name": "agent_analyst_alpha",
                "tmux_session_name_strategy": "preferred",
                "tmux_preferred_session_found_preflight": True,
                "tmux_preferred_session_retried": True,
                "tmux_preferred_session_reused": True,
                "tmux_preferred_session_reuse_attempted": False,
                "tmux_preferred_session_reuse_result": "",
                "tmux_preferred_session_reuse_error": "",
                "tmux_preferred_session_reused_existing": False,
                "tmux_session_started": True,
                "tmux_orphan_cleanup_attempted": True,
                "tmux_orphan_server_running": True,
                "tmux_orphan_sessions_found": 1,
                "tmux_orphan_sessions_cleaned": 1,
                "tmux_orphan_sessions_failed": 0,
                "tmux_orphan_failed_sessions": [],
                "tmux_orphan_cleanup_error": "",
                "tmux_spawn_attempts": 2,
                "tmux_spawn_retried": True,
                "tmux_spawn_retry_reason": "duplicate session: agent_analyst_alpha_deadbeef",
                "tmux_spawn_error": "",
                "tmux_stale_session_cleanup_attempted": True,
                "tmux_stale_session_name": "agent_analyst_alpha_deadbeef",
                "tmux_stale_session_cleanup_result": "killed",
                "tmux_stale_session_cleanup_error": "",
                "tmux_stale_session_cleanup_retry_attempted": True,
                "tmux_stale_session_cleanup_retry_result": "killed",
                "tmux_stale_session_cleanup_retry_error": "",
                "tmux_stale_session_exists_after_cleanup": True,
                "tmux_stale_session_cleanup_verification_error": "",
                "tmux_cleanup_retry_attempted": True,
                "tmux_cleanup_retry_result": "killed",
                "tmux_cleanup_retry_error": "",
                "tmux_session_exists_after_cleanup": True,
                "tmux_cleanup_verification_error": "",
                "tmux_status_observed": False,
                "tmux_timed_out": True,
                "tmux_cleanup_result": "killed",
                "tmux_cleanup_error": "",
                "tmux_ipc_cleanup_result": "removed",
                "tmux_ipc_cleanup_error": "",
                "tmux_ipc_files_removed": 2,
            }
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
            diagnostics = result.get("diagnostics", {})
            self.assertTrue(diagnostics.get("fallback_used"))
            self.assertIn("tmux_returncode=124", diagnostics.get("fallback_reason", ""))
            self.assertTrue(diagnostics.get("tmux_timed_out"))
            self.assertEqual(diagnostics.get("tmux_cleanup_result"), "killed")
            self.assertEqual(diagnostics.get("tmux_ipc_cleanup_result"), "removed")
            self.assertEqual(diagnostics.get("tmux_orphan_sessions_found"), 1)
            self.assertEqual(diagnostics.get("tmux_orphan_sessions_cleaned"), 1)
            self.assertEqual(diagnostics.get("tmux_spawn_attempts"), 2)
            self.assertTrue(diagnostics.get("tmux_spawn_retried"))
            self.assertTrue(diagnostics.get("tmux_stale_session_cleanup_attempted"))
            self.assertEqual(diagnostics.get("tmux_stale_session_cleanup_result"), "killed")
            self.assertTrue(diagnostics.get("tmux_stale_session_cleanup_retry_attempted"))
            self.assertEqual(diagnostics.get("tmux_stale_session_cleanup_retry_result"), "killed")
            self.assertTrue(diagnostics.get("tmux_cleanup_retry_attempted"))
            self.assertEqual(diagnostics.get("tmux_cleanup_retry_result"), "killed")

            diagnostics_path = output_dir / "tmux_worker_diagnostics.jsonl"
            self.assertTrue(diagnostics_path.exists())
            lines = diagnostics_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload.get("transport_used"), "tmux->subprocess_fallback")
            self.assertEqual(payload.get("result"), "success")
            self.assertTrue(payload.get("tmux_timed_out"))
            self.assertEqual(payload.get("tmux_cleanup_result"), "killed")
            self.assertEqual(payload.get("tmux_orphan_sessions_found"), 1)
            self.assertEqual(payload.get("tmux_orphan_sessions_cleaned"), 1)
            self.assertEqual(payload.get("tmux_spawn_attempts"), 2)
            self.assertTrue(payload.get("tmux_spawn_retried"))
            self.assertTrue(payload.get("tmux_stale_session_cleanup_attempted"))
            self.assertEqual(payload.get("tmux_stale_session_cleanup_result"), "killed")
            self.assertTrue(payload.get("tmux_stale_session_cleanup_retry_attempted"))
            self.assertEqual(payload.get("tmux_stale_session_cleanup_retry_result"), "killed")
            self.assertTrue(payload.get("tmux_cleanup_retry_attempted"))
            self.assertEqual(payload.get("tmux_cleanup_retry_result"), "killed")

    def test_tmux_worker_fallback_timeout_returns_structured_failure(self) -> None:
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
            tmux_failed.tmux_lifecycle = {
                "tmux_session_name": "agent_analyst_alpha_deadbeef",
                "tmux_preferred_session_name": "agent_analyst_alpha",
                "tmux_session_name_strategy": "preferred",
                "tmux_preferred_session_found_preflight": True,
                "tmux_preferred_session_retried": True,
                "tmux_preferred_session_reused": True,
                "tmux_preferred_session_reuse_attempted": False,
                "tmux_preferred_session_reuse_result": "",
                "tmux_preferred_session_reuse_error": "",
                "tmux_preferred_session_reused_existing": False,
                "tmux_session_started": True,
                "tmux_orphan_cleanup_attempted": True,
                "tmux_orphan_server_running": True,
                "tmux_orphan_sessions_found": 1,
                "tmux_orphan_sessions_cleaned": 1,
                "tmux_orphan_sessions_failed": 0,
                "tmux_orphan_failed_sessions": [],
                "tmux_orphan_cleanup_error": "",
                "tmux_spawn_attempts": 2,
                "tmux_spawn_retried": True,
                "tmux_spawn_retry_reason": "duplicate session: agent_analyst_alpha_deadbeef",
                "tmux_spawn_error": "",
                "tmux_stale_session_cleanup_attempted": True,
                "tmux_stale_session_name": "agent_analyst_alpha_deadbeef",
                "tmux_stale_session_cleanup_result": "killed",
                "tmux_stale_session_cleanup_error": "",
                "tmux_stale_session_cleanup_retry_attempted": True,
                "tmux_stale_session_cleanup_retry_result": "killed",
                "tmux_stale_session_cleanup_retry_error": "",
                "tmux_stale_session_exists_after_cleanup": True,
                "tmux_stale_session_cleanup_verification_error": "",
                "tmux_cleanup_retry_attempted": True,
                "tmux_cleanup_retry_result": "killed",
                "tmux_cleanup_retry_error": "",
                "tmux_session_exists_after_cleanup": True,
                "tmux_cleanup_verification_error": "",
                "tmux_status_observed": False,
                "tmux_timed_out": True,
                "tmux_cleanup_result": "killed",
                "tmux_cleanup_error": "",
                "tmux_ipc_cleanup_result": "removed",
                "tmux_ipc_cleanup_error": "",
                "tmux_ipc_files_removed": 2,
            }

            with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime, "_execute_worker_tmux", return_value=tmux_failed
            ), mock.patch.object(
                runtime,
                "_execute_worker_subprocess",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["python"],
                    timeout=1,
                    output="fallback partial stdout",
                    stderr="fallback subprocess too slow",
                ),
            ):
                result = runtime._run_tmux_worker_task(
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                    output_dir=output_dir,
                    runtime_config=config,
                    payload={"task_type": "discover_markdown"},
                    worker_name="analyst_alpha",
                    logger=logger,
                    timeout_sec=1,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result.get("transport"), "tmux->subprocess_fallback")
            self.assertIn("timed out", result.get("error", ""))
            diagnostics = result.get("diagnostics", {})
            self.assertTrue(diagnostics.get("fallback_used"))
            self.assertTrue(diagnostics.get("tmux_timed_out"))
            self.assertTrue(diagnostics.get("execution_timed_out"))
            self.assertEqual(diagnostics.get("timeout_transport"), "tmux->subprocess_fallback")
            self.assertEqual(diagnostics.get("timeout_phase"), "fallback_subprocess")
            self.assertEqual(diagnostics.get("result"), "timeout")
            self.assertEqual(diagnostics.get("tmux_orphan_sessions_found"), 1)
            self.assertEqual(diagnostics.get("tmux_orphan_sessions_cleaned"), 1)
            self.assertTrue(diagnostics.get("tmux_stale_session_cleanup_attempted"))
            self.assertEqual(diagnostics.get("tmux_stale_session_cleanup_result"), "killed")
            self.assertTrue(diagnostics.get("tmux_stale_session_cleanup_retry_attempted"))
            self.assertEqual(diagnostics.get("tmux_stale_session_cleanup_retry_result"), "killed")
            self.assertTrue(diagnostics.get("tmux_cleanup_retry_attempted"))
            self.assertEqual(diagnostics.get("tmux_cleanup_retry_result"), "killed")

            diagnostics_path = output_dir / "tmux_worker_diagnostics.jsonl"
            payload = json.loads(diagnostics_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertTrue(payload.get("execution_timed_out"))
            self.assertEqual(payload.get("timeout_transport"), "tmux->subprocess_fallback")
            self.assertEqual(payload.get("result"), "timeout")
            self.assertEqual(payload.get("tmux_orphan_sessions_found"), 1)
            self.assertEqual(payload.get("tmux_orphan_sessions_cleaned"), 1)
            self.assertTrue(payload.get("tmux_stale_session_cleanup_attempted"))
            self.assertEqual(payload.get("tmux_stale_session_cleanup_result"), "killed")
            self.assertTrue(payload.get("tmux_stale_session_cleanup_retry_attempted"))
            self.assertEqual(payload.get("tmux_stale_session_cleanup_retry_result"), "killed")
            self.assertTrue(payload.get("tmux_cleanup_retry_attempted"))
            self.assertEqual(payload.get("tmux_cleanup_retry_result"), "killed")

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

    def test_host_adapter_prepares_isolated_workspace_and_context_for_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "README.md").write_text("# Title\n", encoding="utf-8")
            adapter = runtime.build_host_adapter(runtime.default_host_config("claude-code"))

            session = adapter.prepare_agent_session(
                output_dir=output_dir,
                target_dir=target_dir,
                agent_name="analyst_alpha",
                agent_type="analyst",
                goal="audit repo",
                workflow_pack="markdown-audit",
                workflow_preset="default",
            )

            self.assertTrue(session["workspace_isolated"])
            self.assertTrue(session["auto_context_enabled"])
            self.assertNotEqual(session["effective_target_dir"], str(target_dir.resolve()))
            self.assertTrue(pathlib.Path(session["effective_target_dir"]).exists())
            self.assertTrue(pathlib.Path(session["context_file"]).exists())
            context_text = pathlib.Path(session["context_file"]).read_text(encoding="utf-8")
            self.assertIn("Host kind: claude-code", context_text)
            self.assertIn("Workflow pack: markdown-audit", context_text)

    def test_build_agent_context_uses_host_effective_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            logger = runtime.EventLogger(output_dir=output_dir)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(participants=["lead"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            host_session = {
                "effective_target_dir": str((output_dir / "workspace" / "target").resolve()),
                "context_file": str((output_dir / "AGENT_TEAM_CONTEXT.md").resolve()),
            }

            context = runtime.build_agent_context(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=target_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                host_session=host_session,
            )

            self.assertEqual(context.target_dir, pathlib.Path(host_session["effective_target_dir"]).resolve())
            self.assertEqual(context.host_session["context_file"], host_session["context_file"])

    def test_tmux_worker_context_preserves_host_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            host_target = root / "workspace" / "target"
            host_target.mkdir(parents=True, exist_ok=True)
            host_session = {
                "effective_target_dir": str(host_target.resolve()),
                "context_file": str((root / "AGENT_TEAM_CONTEXT.md").resolve()),
                "host_kind": "claude-code",
            }
            payload = {
                "runtime_config": runtime.RuntimeConfig().to_dict(),
                "shared_state": {},
                "task_results": {},
                "task_ids": [],
                "profile": {"name": "reviewer_gamma", "skills": ["review"], "agent_type": "reviewer"},
                "provider_config": {"provider_name": "heuristic", "model": "heuristic-v1"},
                "target_dir": str(root / "fallback_target"),
                "output_dir": str(root / "out"),
                "goal": "test",
                "host_session": host_session,
            }

            context = runtime.tmux_transport._build_worker_context(payload)

            self.assertEqual(context.target_dir, host_target.resolve())
            self.assertEqual(context.host_session["context_file"], host_session["context_file"])
            self.assertEqual(context.host_session["host_kind"], "claude-code")

    def test_apply_resume_runtime_defaults_uses_checkpoint_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkpoint = root / "run_checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "version": runtime.CHECKPOINT_VERSION,
                        "saved_at": runtime.utc_now(),
                        "goal": "resume",
                        "target_dir": str(root),
                        "output_dir": str(root),
                        "runtime_config": runtime.RuntimeConfig(
                            teammate_mode="tmux",
                            peer_wait_seconds=1,
                            evidence_wait_seconds=1,
                            auto_round3_on_challenge=False,
                        ).to_dict(),
                        "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                        "task_board": {"tasks": []},
                        "shared_state": {},
                    }
                ),
                encoding="utf-8",
            )
            base_config = runtime.build_agent_team_config(
                runtime_config=runtime.RuntimeConfig(),
                host_kind="generic-cli",
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                provider_timeout_sec=60,
                workflow_pack="markdown-audit",
            )

            effective = runtime.apply_resume_runtime_defaults(
                agent_team_config=base_config,
                resume_from=checkpoint,
            )

            self.assertEqual(effective.runtime.teammate_mode, "tmux")
            self.assertEqual(effective.runtime.peer_wait_seconds, 1)
            self.assertEqual(effective.runtime.evidence_wait_seconds, 1)
            self.assertFalse(effective.runtime.auto_round3_on_challenge)

    def test_apply_resume_runtime_defaults_preserves_explicit_runtime_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkpoint = root / "run_checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "version": runtime.CHECKPOINT_VERSION,
                        "saved_at": runtime.utc_now(),
                        "goal": "resume",
                        "target_dir": str(root),
                        "output_dir": str(root),
                        "runtime_config": runtime.RuntimeConfig(
                            teammate_mode="tmux",
                            peer_wait_seconds=1,
                            evidence_wait_seconds=1,
                            auto_round3_on_challenge=False,
                        ).to_dict(),
                        "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                        "task_board": {"tasks": []},
                        "shared_state": {},
                    }
                ),
                encoding="utf-8",
            )
            base_config = runtime.build_agent_team_config(
                runtime_config=runtime.RuntimeConfig(
                    peer_wait_seconds=7,
                    evidence_wait_seconds=9,
                ),
                host_kind="generic-cli",
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                provider_timeout_sec=60,
                workflow_pack="markdown-audit",
            )

            effective = runtime.apply_resume_runtime_defaults(
                agent_team_config=base_config,
                resume_from=checkpoint,
            )

            self.assertEqual(effective.runtime.teammate_mode, "tmux")
            self.assertEqual(effective.runtime.peer_wait_seconds, 7)
            self.assertEqual(effective.runtime.evidence_wait_seconds, 9)
            self.assertFalse(effective.runtime.auto_round3_on_challenge)


if __name__ == "__main__":
    unittest.main()
