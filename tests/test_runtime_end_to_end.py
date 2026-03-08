#!/usr/bin/env python3

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, Optional


TEST_DIR = pathlib.Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import agent_team_runtime as runtime


class StaticResultBoard:
    def __init__(self, results: Dict[str, Dict[str, Any]]) -> None:
        self._results = dict(results)

    def get_task_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._results.get(task_id)


class RuntimeEndToEndTests(unittest.TestCase):
    def test_cli_run_writes_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output"
            target_dir.mkdir(parents=True, exist_ok=True)

            (target_dir / "no_heading.md").write_text(
                "plain line\nanother line\n",
                encoding="utf-8",
            )
            long_lines = "\n".join([f"# Section {index}" for index in range(1, 220)])
            (target_dir / "long_doc.md").write_text(long_lines + "\n", encoding="utf-8")

            cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--peer-wait-seconds",
                "1",
                "--evidence-wait-seconds",
                "1",
            ]
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=90,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}",
            )

            expected_artifacts = [
                output_dir / "events.jsonl",
                output_dir / "task_board.json",
                output_dir / "shared_state.json",
                output_dir / "file_locks.json",
                output_dir / "run_summary.json",
                output_dir / "final_report.md",
                output_dir / runtime.CHECKPOINT_FILENAME,
            ]
            for path in expected_artifacts:
                self.assertTrue(path.exists(), msg=f"missing artifact: {path}")

            task_board = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
            task_states = {item["task_id"]: item["status"] for item in task_board["tasks"]}
            self.assertTrue(task_states, "task board should not be empty")
            self.assertTrue(
                all(state == "completed" for state in task_states.values()),
                msg=f"unexpected task states: {task_states}",
            )
            self.assertTrue(
                all("allowed_agent_types" in item for item in task_board["tasks"]),
                msg="task board should persist allowed_agent_types",
            )
            self.assertIn("dynamic_planning", task_states)
            self.assertIn("heading_structure_followup", task_states)
            self.assertIn("length_risk_followup", task_states)

            summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
            provider = summary.get("provider", {})
            self.assertEqual(provider.get("provider"), "heuristic")
            self.assertIn("runtime_config", summary)
            self.assertIn("checkpoint_history_dir", summary)

            shared_state = json.loads((output_dir / "shared_state.json").read_text(encoding="utf-8"))
            self.assertIn("markdown_inventory", shared_state)
            self.assertIn("llm_synthesis", shared_state)

            events = []
            with (output_dir / "events.jsonl").open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
            event_names = {item.get("event") for item in events}
            self.assertIn("run_started", event_names)
            self.assertIn("lead_adjudication_published", event_names)
            self.assertIn("run_finished", event_names)
            self.assertIn(runtime.HOOK_EVENT_TASK_COMPLETED, event_names)
            self.assertIn("task_inserted", event_names)
            self.assertIn("task_dependency_added", event_names)

            report_text = (output_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("## Dynamic Tasking", report_text)
            self.assertIn("## Evidence Pack", report_text)
            self.assertIn("## Lead Adjudication", report_text)

    def test_cli_repo_audit_workflow_writes_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_repo"
            output_dir = root / "runtime_output_repo"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "README.md").write_text("# Title\nBody\n", encoding="utf-8")
            (target_dir / "src").mkdir(parents=True, exist_ok=True)
            (target_dir / "src" / "app.py").write_text("\n".join(["print('hello')"] * 80), encoding="utf-8")
            (target_dir / "config.json").write_text("{\"enabled\": true}\n", encoding="utf-8")

            cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--workflow-pack",
                "repo-audit",
                "--peer-wait-seconds",
                "1",
                "--evidence-wait-seconds",
                "1",
            ]
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=90,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}",
            )

            expected_artifacts = [
                output_dir / "events.jsonl",
                output_dir / "task_board.json",
                output_dir / "shared_state.json",
                output_dir / "file_locks.json",
                output_dir / "run_summary.json",
                output_dir / "final_report.md",
                output_dir / runtime.CHECKPOINT_FILENAME,
            ]
            for path in expected_artifacts:
                self.assertTrue(path.exists(), msg=f"missing artifact: {path}")

            task_board = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
            task_states = {item["task_id"]: item["status"] for item in task_board["tasks"]}
            self.assertTrue(all(state == "completed" for state in task_states.values()))
            self.assertIn("repo_dynamic_planning", task_states)
            self.assertIn("repo_recommendation_pack", task_states)
            self.assertIn("extension_hotspot_followup", task_states)
            self.assertIn("directory_hotspot_followup", task_states)

            summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary.get("workflow", {}).get("pack"), "repo-audit")

            shared_state = json.loads((output_dir / "shared_state.json").read_text(encoding="utf-8"))
            self.assertIn("repository_inventory", shared_state)
            self.assertIn("repository_extension_summary", shared_state)
            self.assertIn("llm_synthesis", shared_state)

            events = []
            with (output_dir / "events.jsonl").open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
            event_names = {item.get("event") for item in events}
            self.assertIn("run_started", event_names)
            self.assertIn("lead_adjudication_published", event_names)
            self.assertIn("run_finished", event_names)
            self.assertIn("task_inserted", event_names)

            report_text = (output_dir / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("## Repository Findings", report_text)
            self.assertIn("## Dynamic Tasking", report_text)
            self.assertIn("## Evidence Pack", report_text)
            self.assertIn("## Lead Adjudication", report_text)

    def test_cli_rejects_invalid_teammate_memory_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "doc.md").write_text("# Title\nBody\n", encoding="utf-8")

            cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--teammate-memory-turns",
                "0",
            ]
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("--teammate-memory-turns must be > 0", completed.stderr)

    def test_cli_rejects_conflicting_rewind_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "doc.md").write_text("# Title\nBody\n", encoding="utf-8")

            cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--rewind-to-history-index",
                "0",
                "--rewind-to-event-index",
                "0",
            ]
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("mutually exclusive", completed.stderr)

    def test_cli_tmux_mode_completes_with_worker_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_tmux"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# Title\nbody\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--teammate-mode",
                "tmux",
                "--peer-wait-seconds",
                "1",
                "--evidence-wait-seconds",
                "1",
            ]
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}",
            )
            self.assertIn("teammate_mode=tmux", completed.stdout)

            events = []
            with (output_dir / "events.jsonl").open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
            event_names = {item.get("event") for item in events}
            self.assertIn("teammate_mode_tmux_enabled", event_names)
            self.assertIn("tmux_worker_session_recovery_sweep", event_names)
            self.assertIn("tmux_worker_task_dispatched", event_names)
            self.assertIn("tmux_worker_task_completed", event_names)
            self.assertIn("tmux_worker_session_cleanup_sweep", event_names)

            diagnostics_path = output_dir / "tmux_worker_diagnostics.jsonl"
            self.assertTrue(diagnostics_path.exists())
            diagnostics_lines = diagnostics_path.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(diagnostics_lines), 1)
            first_record = json.loads(diagnostics_lines[0])
            self.assertIn(first_record.get("result"), {"success", "execution_failed", "invalid_json"})
            self.assertIn("transport_used", first_record)
            self.assertIn("tmux_timed_out", first_record)
            self.assertIn("tmux_preferred_session_name", first_record)
            self.assertIn("tmux_session_name_strategy", first_record)
            self.assertIn("tmux_preferred_session_found_preflight", first_record)
            self.assertIn("tmux_preferred_session_retried", first_record)
            self.assertIn("tmux_preferred_session_reused", first_record)
            self.assertIn("tmux_preferred_session_reuse_attempted", first_record)
            self.assertIn("tmux_preferred_session_reuse_result", first_record)
            self.assertIn("tmux_preferred_session_reuse_error", first_record)
            self.assertIn("tmux_preferred_session_reuse_authorized", first_record)
            self.assertIn("tmux_preferred_session_reused_existing", first_record)
            self.assertIn("tmux_reuse_retention_requested", first_record)
            self.assertIn("tmux_session_retained_for_reuse", first_record)
            self.assertIn("tmux_cleanup_result", first_record)
            self.assertIn("execution_timed_out", first_record)
            self.assertIn("timeout_phase", first_record)
            self.assertIn("tmux_spawn_attempts", first_record)
            self.assertIn("tmux_spawn_retried", first_record)
            self.assertIn("tmux_stale_session_cleanup_attempted", first_record)
            self.assertIn("tmux_stale_session_cleanup_result", first_record)
            self.assertIn("tmux_stale_session_cleanup_retry_attempted", first_record)
            self.assertIn("tmux_stale_session_cleanup_retry_result", first_record)
            self.assertIn("tmux_cleanup_retry_attempted", first_record)
            self.assertIn("tmux_cleanup_retry_result", first_record)
            self.assertIn("tmux_orphan_sessions_found", first_record)
            self.assertIn("tmux_orphan_sessions_cleaned", first_record)

            cleanup_summary_path = output_dir / "tmux_session_cleanup_summary.json"
            self.assertTrue(cleanup_summary_path.exists())
            cleanup_summary = json.loads(cleanup_summary_path.read_text(encoding="utf-8"))
            self.assertEqual(cleanup_summary.get("sessions"), ["agent_analyst_alpha", "agent_analyst_beta"])
            self.assertIn("cleaned", cleanup_summary)
            self.assertIn("already_exited", cleanup_summary)
            self.assertIn("failed", cleanup_summary)
            self.assertIn(cleanup_summary.get("skipped", ""), {"", "tmux_unavailable"})

            recovery_summary_path = output_dir / "tmux_session_recovery_summary.json"
            self.assertTrue(recovery_summary_path.exists())
            recovery_summary = json.loads(recovery_summary_path.read_text(encoding="utf-8"))
            self.assertIn("workers", recovery_summary)
            self.assertIn("recovered", recovery_summary)
            self.assertIn("missing", recovery_summary)
            self.assertIn("inactive", recovery_summary)
            self.assertIn("failed", recovery_summary)
            self.assertIn(recovery_summary.get("skipped", ""), {"", "no_leases", "tmux_unavailable"})

            leases_path = output_dir / "tmux_session_leases.json"
            self.assertTrue(leases_path.exists())
            leases = json.loads(leases_path.read_text(encoding="utf-8"))
            self.assertIn("analyst_alpha", leases)
            self.assertIn("analyst_beta", leases)
            self.assertIn(
                leases["analyst_alpha"].get("status"),
                {"cleanup_skipped_tmux_unavailable", "cleanup_swept", "cleanup_failed"},
            )
            self.assertIn("reuse_authorized", leases["analyst_alpha"])
            self.assertIn("session_name", leases["analyst_alpha"])

            summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                pathlib.Path(summary.get("tmux_session_cleanup_summary_path", "")).resolve(),
                cleanup_summary_path.resolve(),
            )
            self.assertEqual(
                pathlib.Path(summary.get("tmux_session_recovery_summary_path", "")).resolve(),
                recovery_summary_path.resolve(),
            )
            self.assertEqual(
                pathlib.Path(summary.get("tmux_session_leases_path", "")).resolve(),
                leases_path.resolve(),
            )

    def test_cli_resume_from_checkpoint_completes_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_resume"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            partial_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--max-completed-tasks",
                "3",
            ]
            partial = subprocess.run(
                partial_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                partial.returncode,
                0,
                msg=f"stdout:\n{partial.stdout}\n\nstderr:\n{partial.stderr}",
            )

            checkpoint = output_dir / runtime.CHECKPOINT_FILENAME
            self.assertTrue(checkpoint.exists(), "checkpoint should exist after partial run")
            task_board_partial = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
            statuses_partial = {item["task_id"]: item["status"] for item in task_board_partial["tasks"]}
            self.assertTrue(
                any(status != "completed" for status in statuses_partial.values()),
                msg=f"expected partial completion, got={statuses_partial}",
            )

            resume_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--resume-from",
                str(checkpoint),
            ]
            resumed = subprocess.run(
                resume_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                resumed.returncode,
                0,
                msg=f"stdout:\n{resumed.stdout}\n\nstderr:\n{resumed.stderr}",
            )
            self.assertIn("tasks incomplete: 0", resumed.stdout)

            task_board = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
            statuses = {item["task_id"]: item["status"] for item in task_board["tasks"]}
            self.assertTrue(all(status == "completed" for status in statuses.values()))

            summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary.get("resume_from"), str(checkpoint.resolve()))

    def test_cli_rewind_to_history_index_restarts_from_earlier_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_rewind"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            initial_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
            ]
            initial = subprocess.run(
                initial_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                initial.returncode,
                0,
                msg=f"stdout:\n{initial.stdout}\n\nstderr:\n{initial.stderr}",
            )
            history_dir = output_dir / runtime.CHECKPOINT_HISTORY_DIRNAME
            history_files = sorted(history_dir.glob("checkpoint_*.json"))
            self.assertTrue(history_files, "expected checkpoint history files")

            rewind_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--rewind-to-history-index",
                "0",
                "--max-completed-tasks",
                "1",
            ]
            rewound = subprocess.run(
                rewind_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                rewound.returncode,
                0,
                msg=f"stdout:\n{rewound.stdout}\n\nstderr:\n{rewound.stderr}",
            )
            self.assertIn("rewind_history_index: 0", rewound.stdout)
            self.assertIn("run_interrupted: max_completed_tasks reached", rewound.stdout)

            task_board = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
            statuses = {item["task_id"]: item["status"] for item in task_board["tasks"]}
            self.assertTrue(any(status != "completed" for status in statuses.values()))

            summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary.get("rewind_history_index"), 0)
            self.assertIn("checkpoint_000000.json", str(summary.get("resume_from", "")))

    def test_cli_rewind_to_event_index_restarts_from_mapped_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_rewind_event"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            initial_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
            ]
            initial = subprocess.run(
                initial_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                initial.returncode,
                0,
                msg=f"stdout:\n{initial.stdout}\n\nstderr:\n{initial.stderr}",
            )

            rewind_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--rewind-to-event-index",
                "0",
                "--max-completed-tasks",
                "1",
            ]
            rewound = subprocess.run(
                rewind_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                rewound.returncode,
                0,
                msg=f"stdout:\n{rewound.stdout}\n\nstderr:\n{rewound.stderr}",
            )
            self.assertIn("rewind_event_index: 0", rewound.stdout)
            self.assertIn("run_interrupted: max_completed_tasks reached", rewound.stdout)

            summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary.get("rewind_event_index"), 0)
            resolution = summary.get("rewind_event_resolution", {})
            self.assertIsInstance(resolution, dict)
            self.assertIn("resolved_history_index", resolution)

    def test_cli_rewind_branch_writes_to_branch_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_rewind_branch"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            initial_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
            ]
            initial = subprocess.run(
                initial_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                initial.returncode,
                0,
                msg=f"stdout:\n{initial.stdout}\n\nstderr:\n{initial.stderr}",
            )

            rewind_branch_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
                "--rewind-to-history-index",
                "0",
                "--rewind-branch",
                "--max-completed-tasks",
                "1",
            ]
            rewound = subprocess.run(
                rewind_branch_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                rewound.returncode,
                0,
                msg=f"stdout:\n{rewound.stdout}\n\nstderr:\n{rewound.stderr}",
            )

            source_board = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
            source_statuses = {item["task_id"]: item["status"] for item in source_board["tasks"]}
            self.assertTrue(
                all(status == "completed" for status in source_statuses.values()),
                msg=f"source output should remain completed, got={source_statuses}",
            )

            branches_dir = output_dir / "branches"
            self.assertTrue(branches_dir.exists(), "expected branches directory for rewind branch run")
            branch_dirs = sorted([path for path in branches_dir.iterdir() if path.is_dir()])
            self.assertTrue(branch_dirs, "expected at least one rewind branch directory")
            branch_output = branch_dirs[-1]

            branch_summary = json.loads((branch_output / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(branch_summary.get("rewind_history_index"), 0)
            self.assertEqual(branch_summary.get("rewind_source_output_dir"), str(output_dir.resolve()))
            self.assertIn("checkpoint_000000.json", str(branch_summary.get("rewind_source_checkpoint", "")))
            self.assertTrue(branch_summary.get("branch_run_id"))
            self.assertIsInstance(branch_summary.get("rewind_seed_event_count"), int)
            self.assertGreater(branch_summary.get("rewind_seed_event_count", 0), 0)

            branch_events = []
            with (branch_output / "events.jsonl").open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        branch_events.append(json.loads(line))
            branch_event_names = {item.get("event") for item in branch_events}
            self.assertIn("run_branch_events_seeded", branch_event_names)
            self.assertGreaterEqual(
                sum(1 for item in branch_events if item.get("event") == "run_started"),
                2,
            )

            branch_resume_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(branch_output),
                "--provider",
                "heuristic",
                "--resume-from",
                str(branch_output / runtime.CHECKPOINT_FILENAME),
            ]
            branch_resumed = subprocess.run(
                branch_resume_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                branch_resumed.returncode,
                0,
                msg=f"stdout:\n{branch_resumed.stdout}\n\nstderr:\n{branch_resumed.stderr}",
            )

            branch_summary_resumed = json.loads(
                (branch_output / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(branch_summary_resumed.get("rewind_history_index"), 0)
            self.assertEqual(
                branch_summary_resumed.get("rewind_source_output_dir"),
                str(output_dir.resolve()),
            )
            self.assertIn(
                "checkpoint_000000.json",
                str(branch_summary_resumed.get("rewind_source_checkpoint", "")),
            )
            self.assertTrue(branch_summary_resumed.get("branch_run_id"))
            self.assertEqual(
                branch_summary_resumed.get("rewind_seed_event_count"),
                branch_summary.get("rewind_seed_event_count"),
            )

    def test_cli_history_replay_report_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_replay"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            initial_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
            ]
            initial = subprocess.run(
                initial_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                initial.returncode,
                0,
                msg=f"stdout:\n{initial.stdout}\n\nstderr:\n{initial.stderr}",
            )

            replay_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--output",
                str(output_dir),
                "--history-replay-report",
            ]
            replay = subprocess.run(
                replay_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                replay.returncode,
                0,
                msg=f"stdout:\n{replay.stdout}\n\nstderr:\n{replay.stderr}",
            )
            self.assertIn("history_replay_report", replay.stdout)

            report_path = output_dir / "checkpoint_replay.md"
            self.assertTrue(report_path.exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("# Checkpoint History Replay", report_text)
            self.assertIn("## Timeline", report_text)

    def test_cli_event_replay_report_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            output_dir = root / "runtime_output_event_replay"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# A\nx\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            initial_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--target",
                str(target_dir),
                "--output",
                str(output_dir),
                "--provider",
                "heuristic",
            ]
            initial = subprocess.run(
                initial_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                initial.returncode,
                0,
                msg=f"stdout:\n{initial.stdout}\n\nstderr:\n{initial.stderr}",
            )

            replay_cmd = [
                sys.executable,
                str(MODULE_DIR / "agent_team_runtime.py"),
                "--output",
                str(output_dir),
                "--event-replay-report",
                "--event-replay-max-transitions",
                "300",
            ]
            replay = subprocess.run(
                replay_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(
                replay.returncode,
                0,
                msg=f"stdout:\n{replay.stdout}\n\nstderr:\n{replay.stderr}",
            )
            self.assertIn("event_replay_report", replay.stdout)

            report_path = output_dir / "event_replay.md"
            self.assertTrue(report_path.exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("# Event Replay Report", report_text)
            self.assertIn("## Status Counts", report_text)


class RuntimeHandlerFlowTests(unittest.TestCase):
    def _build_context(
        self,
        output_dir: pathlib.Path,
        board_results: Dict[str, Dict[str, Any]],
        config: Optional[runtime.RuntimeConfig] = None,
    ) -> runtime.AgentContext:
        logger = runtime.EventLogger(output_dir=output_dir)
        mailbox = runtime.Mailbox(
            participants=["lead", "reviewer_gamma", "analyst_alpha", "analyst_beta"],
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
        board = StaticResultBoard(results=board_results)
        return runtime.AgentContext(
            profile=runtime.AgentProfile(name="reviewer_gamma", skills={"review"}),
            target_dir=output_dir,
            output_dir=output_dir,
            goal="test",
            provider=provider,
            runtime_config=config or runtime.RuntimeConfig(evidence_wait_seconds=0.3),
            board=board,  # type: ignore[arg-type]
            mailbox=mailbox,
            file_locks=file_locks,
            shared_state=shared_state,
            logger=logger,
        )

    def test_evidence_pack_triggers_with_targeted_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            board_results = {
                "lead_adjudication": {
                    "verdict": "challenge",
                    "score": 58,
                    "targets": ["analyst_alpha", "analyst_beta"],
                    "rubric": {
                        "completeness": 0.5,
                        "rebuttal_coverage": 0.4,
                        "argument_depth": 0.3,
                    },
                },
                "peer_challenge": {
                    "round2": {
                        "received_replies": {
                            "analyst_alpha": "Need stronger KPI threshold.",
                            "analyst_beta": "Parser-only validation may overfit.",
                        }
                    }
                },
            }
            context = self._build_context(output_dir=output_dir, board_results=board_results)
            task = runtime.Task(
                task_id="evidence_pack",
                title="Collect supplemental evidence",
                task_type="evidence_pack",
                required_skills={"review"},
                dependencies=[],
                payload={"wait_seconds": 0.3},
                locked_paths=[],
            )

            context.mailbox.send(
                sender="analyst_alpha",
                recipient="reviewer_gamma",
                subject="evidence_reply",
                body="A" * 260,
                task_id=task.task_id,
            )
            context.mailbox.send(
                sender="analyst_beta",
                recipient="reviewer_gamma",
                subject="evidence_reply",
                body="B" * 260,
                task_id=task.task_id,
            )

            result = runtime.handle_evidence_pack(context=context, task=task)
            self.assertTrue(result["triggered"])
            self.assertEqual(set(result["received_replies"].keys()), {"analyst_alpha", "analyst_beta"})
            self.assertIn("coverage", result["focus_areas"])
            self.assertIn("rebuttal", result["focus_areas"])
            self.assertIn("depth", result["focus_areas"])
            self.assertEqual(result["missing_replies"], [])
            self.assertEqual(len(result["per_target_questions"]), 2)

    def test_lead_re_adjudication_applies_bonus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            config = runtime.RuntimeConfig(
                adjudication_accept_threshold=70,
                adjudication_challenge_threshold=50,
                re_adjudication_max_bonus=20,
                re_adjudication_weight_coverage=0.7,
                re_adjudication_weight_depth=0.3,
            )
            board_results = {
                "lead_adjudication": {
                    "verdict": "challenge",
                    "score": 60,
                    "targets": ["analyst_alpha", "analyst_beta"],
                    "thresholds": {"accept": 70, "challenge": 50},
                    "weights": {
                        "completeness": 0.45,
                        "rebuttal_coverage": 0.35,
                        "argument_depth": 0.20,
                    },
                },
                "evidence_pack": {
                    "triggered": True,
                    "targets": ["analyst_alpha", "analyst_beta"],
                    "received_replies": {
                        "analyst_alpha": "A" * 280,
                        "analyst_beta": "B" * 280,
                    },
                    "missing_replies": [],
                },
            }
            context = self._build_context(
                output_dir=output_dir,
                board_results=board_results,
                config=config,
            )
            task = runtime.Task(
                task_id="lead_re_adjudication",
                title="Lead re-adjudication",
                task_type="lead_re_adjudication",
                required_skills={"lead"},
                dependencies=[],
                payload={},
                locked_paths=[],
            )
            result = runtime.handle_lead_re_adjudication(context=context, _task=task)

            self.assertTrue(result["re_adjudicated"])
            self.assertGreater(result["final_score"], 60)
            self.assertEqual(result["initial_score"], 60)
            self.assertEqual(result["final_verdict"], "accept")
            self.assertGreater(result["evidence_bonus"], 0)


if __name__ == "__main__":
    unittest.main()
