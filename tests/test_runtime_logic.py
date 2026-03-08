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
            exact_orphan_session = session_prefix
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
                        stdout=f"{exact_orphan_session}\n{orphan_session}\nunrelated_session\n",
                        stderr="",
                    )
                if command[:2] == ["tmux", "kill-session"] and command[-1] in {
                    exact_orphan_session,
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
            self.assertEqual(lifecycle.get("tmux_orphan_sessions_found"), 2)
            self.assertEqual(lifecycle.get("tmux_orphan_sessions_cleaned"), 2)
            self.assertEqual(lifecycle.get("tmux_orphan_sessions_failed"), 0)
            self.assertEqual(lifecycle.get("tmux_orphan_failed_sessions"), [])
            self.assertEqual(
                calls,
                [
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    ["tmux", "kill-session", "-t", exact_orphan_session],
                    ["tmux", "kill-session", "-t", orphan_session],
                    ["tmux", "new-session", "-d", "-s", active_session, mock.ANY],
                    ["tmux", "kill-session", "-t", active_session],
                ],
            )

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
                "tmux_preferred_session_retried": True,
                "tmux_preferred_session_reused": True,
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
                "tmux_preferred_session_retried": True,
                "tmux_preferred_session_reused": True,
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


if __name__ == "__main__":
    unittest.main()
