#!/usr/bin/env python3

import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


TEST_DIR = pathlib.Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import agent_team_runtime as runtime
import agent_team.transports.host as host_transport
from agent_team.host import build_host_adapter
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

    def test_file_backed_mailbox_delivers_messages_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            mailbox_dir = output_dir / "mailbox"
            sender_mailbox = runtime.Mailbox(
                participants=["lead", "analyst_alpha"],
                logger=logger,
                storage_dir=mailbox_dir,
                clear_storage=True,
            )
            recipient_mailbox = runtime.Mailbox(
                participants=["lead", "analyst_alpha"],
                logger=logger,
                storage_dir=mailbox_dir,
            )

            sender_mailbox.send(
                sender="lead",
                recipient="analyst_alpha",
                subject="assignment",
                body="inspect headings",
                task_id="heading_audit",
            )
            pulled = recipient_mailbox.pull("analyst_alpha")

            self.assertEqual(len(pulled), 1)
            self.assertEqual(pulled[0].sender, "lead")
            self.assertEqual(pulled[0].subject, "assignment")
            self.assertEqual(pulled[0].task_id, "heading_audit")
            self.assertEqual(sender_mailbox.model_name(), "asynchronous file-backed inbox")

    def test_file_backed_mailbox_pull_matching_preserves_unmatched_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            mailbox_dir = output_dir / "mailbox"
            sender_mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=mailbox_dir,
                clear_storage=True,
            )
            recipient_mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=mailbox_dir,
            )
            secondary_recipient_mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=mailbox_dir,
            )

            sender_mailbox.send(
                sender="lead",
                recipient="reviewer_gamma",
                subject="peer_challenge_round1_request",
                body="round1",
                task_id="peer_challenge",
            )
            sender_mailbox.send(
                sender="lead",
                recipient="reviewer_gamma",
                subject="lead_verdict",
                body="accept",
                task_id="lead_adjudication",
            )

            matched = recipient_mailbox.pull_matching(
                "reviewer_gamma",
                lambda message: message.subject == "peer_challenge_round1_request",
            )
            remaining = secondary_recipient_mailbox.pull("reviewer_gamma")

            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].task_id, "peer_challenge")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].subject, "lead_verdict")

    def test_file_backed_mailbox_transport_view_shares_storage_without_duplicate_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            mailbox_dir = output_dir / "mailbox"
            root_mailbox = runtime.Mailbox(
                participants=["lead", "analyst_alpha"],
                logger=logger,
                storage_dir=mailbox_dir,
                clear_storage=True,
            )
            transport_mailbox = root_mailbox.transport_view()

            self.assertIsNot(root_mailbox, transport_mailbox)
            self.assertEqual(root_mailbox.storage_dir, transport_mailbox.storage_dir)

            root_mailbox.send(
                sender="lead",
                recipient="analyst_alpha",
                subject="assignment",
                body="inspect headings",
                task_id="heading_audit",
            )
            first_pull = transport_mailbox.pull("analyst_alpha")
            second_pull = root_mailbox.pull("analyst_alpha")

            self.assertEqual(len(first_pull), 1)
            self.assertEqual(first_pull[0].subject, "assignment")
            self.assertEqual(second_pull, [])

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

    def test_dynamic_planning_returns_plan_proposal_when_approval_required(self) -> None:
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
            shared_state.set("policies", {"teammate_plan_required": True})
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
            self.assertIn("task_mutations", result)
            self.assertEqual(
                set(item.get("task_id") for item in result["task_mutations"]["insert_tasks"]),
                {"heading_structure_followup", "length_risk_followup"},
            )
            snapshot = board.snapshot()
            task_ids = {task["task_id"] for task in snapshot["tasks"]}
            self.assertEqual(task_ids, {"peer_challenge"})

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

    def test_apply_requested_plan_approvals_applies_pending_mutations(self) -> None:
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
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            runtime.queue_plan_approval_request(
                shared_state=shared_state,
                logger=logger,
                requested_by="reviewer_gamma",
                task_id="dynamic_planning",
                task_type="dynamic_planning",
                transport="in-process",
                result={
                    "enabled": True,
                    "inserted_tasks": ["heading_structure_followup"],
                    "peer_challenge_dependencies_added": ["heading_structure_followup"],
                },
                state_updates={
                    "dynamic_plan": {
                        "enabled": True,
                        "inserted_tasks": ["heading_structure_followup"],
                        "peer_challenge_dependencies_added": ["heading_structure_followup"],
                    }
                },
                task_mutations={
                    "insert_tasks": [
                        {
                            "task_id": "heading_structure_followup",
                            "title": "Run heading structure follow-up audit",
                            "task_type": "heading_structure_followup",
                            "required_skills": ["analysis"],
                            "dependencies": ["dynamic_planning"],
                            "payload": {"top_n": 8},
                            "locked_paths": [],
                            "allowed_agent_types": ["analyst"],
                        }
                    ],
                    "add_dependencies": [
                        {"task_id": "peer_challenge", "dependency_id": "heading_structure_followup"}
                    ],
                },
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="approve pending plan",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(),
                board=board,
                mailbox=runtime.Mailbox(participants=["lead"], logger=logger),
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
            )

            resolution = runtime.apply_requested_plan_approvals(
                lead_context=lead_context,
                approve_task_ids=["dynamic_planning"],
                decision_source="test",
            )

            self.assertEqual(resolution["applied_task_ids"], ["dynamic_planning"])
            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertIn("heading_structure_followup", board_snapshot)
            self.assertIn("heading_structure_followup", board_snapshot["peer_challenge"]["dependencies"])
            self.assertEqual(
                shared_state.get("dynamic_plan", {}).get("inserted_tasks"),
                ["heading_structure_followup"],
            )
            interaction = runtime.get_lead_interaction_state(shared_state)
            applied_request = interaction.get("plan_approval_requests", {}).get("dynamic_planning", {})
            self.assertEqual(applied_request.get("status"), runtime.PLAN_APPROVAL_STATUS_APPLIED)

    def test_consume_lead_commands_reads_new_commands_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            shared_state = runtime.SharedState()

            command_path = runtime.ensure_lead_command_channel(
                output_dir=output_dir,
                shared_state=shared_state,
            )
            command_path.write_text(
                "\n".join(
                    [
                        json.dumps({"command": "approve_plan", "task_id": "dynamic_planning"}, ensure_ascii=False),
                        json.dumps({"command": "reject_plan", "task_ids": ["repo_dynamic_planning"]}, ensure_ascii=False),
                        json.dumps({"command": "request_teammate_status", "agent": "reviewer_gamma"}, ensure_ascii=False),
                        json.dumps({"command": "request_teammate_plan", "agent": "analyst_alpha"}, ensure_ascii=False),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            first = runtime.consume_lead_commands(
                output_dir=output_dir,
                shared_state=shared_state,
                logger=logger,
            )
            second = runtime.consume_lead_commands(
                output_dir=output_dir,
                shared_state=shared_state,
                logger=logger,
            )

            self.assertEqual(first["approve_task_ids"], ["dynamic_planning"])
            self.assertEqual(first["reject_task_ids"], ["repo_dynamic_planning"])
            self.assertEqual(first["plan_request_agents"], ["analyst_alpha"])
            self.assertEqual(first["status_request_agents"], ["reviewer_gamma"])
            self.assertEqual(first["consumed_count"], 4)
            self.assertEqual(second["consumed_count"], 0)
            interaction = runtime.get_lead_interaction_state(shared_state)
            self.assertEqual(interaction.get("command_cursor"), 4)
            self.assertEqual(len(interaction.get("recent_commands", [])), 4)
            self.assertEqual(interaction.get("recent_commands", [])[-1].get("agent"), "analyst_alpha")

    def test_parse_interactive_plan_command_supports_embedded_lead_prompt(self) -> None:
        self.assertEqual(
            runtime.parse_interactive_plan_command("approve dynamic_planning"),
            {
                "action": "approve_plan",
                "raw": "approve dynamic_planning",
                "task_id": "dynamic_planning",
            },
        )
        self.assertEqual(
            runtime.parse_interactive_plan_command("reject repo_dynamic_planning"),
            {
                "action": "reject_plan",
                "raw": "reject repo_dynamic_planning",
                "task_id": "repo_dynamic_planning",
            },
        )
        self.assertEqual(
            runtime.parse_interactive_plan_command("approve-all"),
            {
                "action": "approve_all_pending_plans",
                "raw": "approve-all",
                "task_id": "",
            },
        )
        self.assertEqual(
            runtime.parse_interactive_plan_command("show dynamic_planning"),
            {
                "action": "show_task",
                "raw": "show dynamic_planning",
                "task_id": "dynamic_planning",
            },
        )
        self.assertEqual(
            runtime.parse_interactive_plan_command("status reviewer_gamma"),
            {
                "action": "request_teammate_status",
                "raw": "status reviewer_gamma",
                "task_id": "reviewer_gamma",
            },
        )
        self.assertEqual(
            runtime.parse_interactive_plan_command("plan analyst_alpha"),
            {
                "action": "request_teammate_plan",
                "raw": "plan analyst_alpha",
                "task_id": "analyst_alpha",
            },
        )
        self.assertEqual(
            runtime.parse_interactive_plan_command("pause"),
            {
                "action": "pause",
                "raw": "pause",
                "task_id": "",
            },
        )

    def test_describe_plan_approval_request_includes_preview_and_state_keys(self) -> None:
        description = runtime.describe_plan_approval_request(
            {
                "task_id": "dynamic_planning",
                "task_type": "dynamic_planning",
                "requested_by": "reviewer_gamma",
                "transport": "host",
                "status": runtime.PLAN_APPROVAL_STATUS_PENDING,
                "result": {"enabled": True},
                "state_updates": {"dynamic_plan": {"enabled": True}},
                "proposed_task_ids": ["heading_structure_followup"],
                "proposed_dependency_ids": ["dynamic_planning"],
                "proposed_tasks_preview": [
                    {
                        "task_id": "heading_structure_followup",
                        "task_type": "heading_structure_followup",
                        "title": "Run heading follow-up",
                        "allowed_agent_types": ["analyst"],
                        "dependencies": ["dynamic_planning"],
                    }
                ],
                "proposed_dependencies_preview": [
                    {"task_id": "peer_challenge", "dependency_id": "heading_structure_followup"}
                ],
            }
        )

        joined = "\n".join(description)
        self.assertIn("task_id=dynamic_planning", joined)
        self.assertIn("result_keys=enabled", joined)
        self.assertIn("state_update_keys=dynamic_plan", joined)
        self.assertIn("heading_structure_followup[heading_structure_followup]", joined)
        self.assertIn("peer_challenge+=heading_structure_followup", joined)

    def test_record_lead_command_tracks_interactive_source(self) -> None:
        shared_state = runtime.SharedState()

        record = runtime.record_lead_command(
            shared_state=shared_state,
            command="approve_plan",
            task_ids=["dynamic_planning"],
            raw="approve dynamic_planning",
            source="interactive",
        )

        self.assertEqual(record.get("source"), "interactive")
        interaction = runtime.get_lead_interaction_state(shared_state)
        recent = interaction.get("recent_commands", [])
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].get("command"), "approve_plan")
        self.assertEqual(recent[0].get("source"), "interactive")

    def test_request_teammate_statuses_sends_mail_to_known_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            mailbox = runtime.Mailbox(participants=["lead", "reviewer_gamma"], logger=logger)
            shared_state = runtime.SharedState()
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            registry.ensure_profile(profile=reviewer, transport="in-process", status="ready")
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
                goal="request teammate status",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(),
                board=runtime.TaskBoard(tasks=[], logger=logger),
                mailbox=mailbox,
                file_locks=runtime.FileLockRegistry(logger=logger),
                shared_state=shared_state,
                logger=logger,
                session_registry=registry,
            )

            resolution = runtime.request_teammate_statuses(
                lead_context=lead_context,
                agent_names=["reviewer_gamma", "unknown_agent"],
                decision_source="test",
            )

            self.assertEqual(resolution.get("status_request_agents"), ["reviewer_gamma"])
            self.assertEqual(resolution.get("invalid_status_request_agents"), ["unknown_agent"])
            reviewer_mail = mailbox.pull("reviewer_gamma")
            self.assertEqual(len(reviewer_mail), 1)
            self.assertEqual(reviewer_mail[0].subject, runtime.LEAD_STATUS_REQUEST_SUBJECT)
            self.assertEqual(reviewer_mail[0].sender, "lead")

    def test_request_teammate_plans_sends_mail_to_known_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            mailbox = runtime.Mailbox(participants=["lead", "analyst_alpha"], logger=logger)
            shared_state = runtime.SharedState()
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            registry.ensure_profile(profile=analyst, transport="in-process", status="ready")
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
                goal="request teammate plan",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(),
                board=runtime.TaskBoard(tasks=[], logger=logger),
                mailbox=mailbox,
                file_locks=runtime.FileLockRegistry(logger=logger),
                shared_state=shared_state,
                logger=logger,
                session_registry=registry,
            )

            resolution = runtime.request_teammate_plans(
                lead_context=lead_context,
                agent_names=["analyst_alpha", "unknown_agent"],
                decision_source="test",
            )

            self.assertEqual(resolution.get("plan_request_agents"), ["analyst_alpha"])
            self.assertEqual(resolution.get("invalid_plan_request_agents"), ["unknown_agent"])
            analyst_mail = mailbox.pull("analyst_alpha")
            self.assertEqual(len(analyst_mail), 1)
            self.assertEqual(analyst_mail[0].subject, runtime.LEAD_PLAN_REQUEST_SUBJECT)
            self.assertEqual(analyst_mail[0].sender, "lead")

    def test_write_live_lead_interaction_artifacts_persists_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "plan_approval_controls",
                {
                    "approve_all_pending": False,
                    "approve_task_ids": [],
                    "reject_task_ids": [],
                    "lead_command_wait_seconds": 15.0,
                },
            )

            runtime.ensure_lead_command_channel(
                output_dir=output_dir,
                shared_state=shared_state,
            )
            runtime.queue_plan_approval_request(
                shared_state=shared_state,
                logger=logger,
                requested_by="reviewer_gamma",
                task_id="dynamic_planning",
                task_type="dynamic_planning",
                transport="in-process",
                result={"enabled": True},
                state_updates={"dynamic_plan": {"enabled": True}},
                task_mutations={
                    "insert_tasks": [
                        {
                            "task_id": "heading_structure_followup",
                            "title": "Run heading structure follow-up audit",
                            "task_type": "heading_structure_followup",
                            "required_skills": ["analysis"],
                            "dependencies": ["dynamic_planning"],
                            "payload": {"top_n": 8},
                            "locked_paths": [],
                            "allowed_agent_types": ["analyst"],
                        }
                    ]
                },
            )
            logger.log(
                "mail_sent",
                sender="lead",
                recipient="reviewer_gamma",
                subject="plan_review_requested",
                task_id="dynamic_planning",
            )
            logger.log(
                "mail_sent",
                sender="reviewer_gamma",
                recipient="lead",
                subject="plan_review_ack",
                task_id="dynamic_planning",
            )
            logger.log(
                "mail_sent",
                sender="lead",
                recipient="reviewer_gamma",
                subject=runtime.LEAD_STATUS_REQUEST_SUBJECT,
                body=json.dumps({"requested_by": "lead"}, ensure_ascii=False),
                task_id="",
            )
            logger.log(
                "mail_sent",
                sender="reviewer_gamma",
                recipient="lead",
                subject=runtime.LEAD_STATUS_REPLY_SUBJECT,
                body=json.dumps(
                    {
                        "summary": "reviewer_gamma status=ready current_task=none last_task=dynamic_planning(completed) transport=in-process"
                    },
                    ensure_ascii=False,
                ),
                task_id="",
            )
            logger.log(
                "mail_sent",
                sender="lead",
                recipient="reviewer_gamma",
                subject=runtime.LEAD_PLAN_REQUEST_SUBJECT,
                body=json.dumps({"requested_by": "lead"}, ensure_ascii=False),
                task_id="",
            )
            logger.log(
                "mail_sent",
                sender="reviewer_gamma",
                recipient="lead",
                subject=runtime.LEAD_PLAN_REPLY_SUBJECT,
                body=json.dumps(
                    {
                        "summary": "reviewer_gamma focus=Waiting for the next assignment after dynamic_planning (completed). next=Stay ready for the next task or follow-up question from lead."
                    },
                    ensure_ascii=False,
                ),
                task_id="",
            )

            written = runtime.write_live_lead_interaction_artifacts(
                output_dir=output_dir,
                shared_state=shared_state,
                logger=logger,
            )

            self.assertTrue((output_dir / runtime.LEAD_INTERACTION_FILENAME).exists())
            self.assertTrue((output_dir / runtime.LEAD_INTERACTION_REPORT_FILENAME).exists())
            snapshot = written.get("snapshot", {})
            self.assertEqual(snapshot.get("pending_plan_approval_count"), 1)
            self.assertEqual(snapshot.get("pending_plan_approval_task_ids"), ["dynamic_planning"])
            self.assertEqual(snapshot.get("recent_team_message_count"), 6)
            pending_request = snapshot.get("plan_approval_requests", [])[0]
            self.assertEqual(
                pending_request.get("proposed_tasks_preview", [])[0].get("task_id"),
                "heading_structure_followup",
            )
            self.assertIn("reviewer_gamma focus=", snapshot.get("recent_team_messages", [])[-1].get("body_preview", ""))
            report_text = (output_dir / runtime.LEAD_INTERACTION_REPORT_FILENAME).read_text(encoding="utf-8")
            self.assertIn("## Pending Approvals", report_text)
            self.assertIn("dynamic_planning", report_text)
            self.assertIn("heading_structure_followup", report_text)
            self.assertIn("plan_review_requested", report_text)
            self.assertIn("plan_review_ack", report_text)
            self.assertIn("reviewer_gamma status=ready", report_text)
            self.assertIn("reviewer_gamma focus=", report_text)

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
                    "task_mutations": {},
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

    def test_tmux_worker_payload_prefers_task_context_visible_state(self) -> None:
        length = runtime.run_tmux_worker_payload(
            {
                "task_type": "length_audit",
                "task_payload": {"line_threshold": 2},
                "task_context": {
                    "visible_shared_state": {
                        "markdown_inventory": [
                            {"path": "a.md", "line_count": 3, "heading_count": 1},
                            {"path": "b.md", "line_count": 1, "heading_count": 0},
                        ]
                    }
                },
                "shared_state": {
                    "markdown_inventory": [
                        {"path": "ignored.md", "line_count": 1, "heading_count": 1},
                    ]
                },
            }
        )
        self.assertEqual(length["result"]["long_files"], 1)
        self.assertEqual(length["result"]["examples"], ["a.md"])

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

    def test_execute_worker_tmux_injects_session_environment_into_shell_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = pathlib.Path(tmp)
            session_prefix = "agent_analyst_alpha"
            session_name = session_prefix
            ipc_dir = workdir / "_tmux_worker_ipc"
            stdout_file = ipc_dir / f"{session_name}.stdout.txt"
            stderr_file = ipc_dir / f"{session_name}.stderr.txt"
            status_file = ipc_dir / f"{session_name}.status.txt"
            session_root = workdir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha"
            session_tmp = session_root / "tmp"
            shell_commands = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                if command[:2] == ["tmux", "list-sessions"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout="",
                        stderr="no server running on /tmp/tmux-501/default",
                    )
                if command[:2] == ["tmux", "new-session"]:
                    shell_commands.append(str(command[-1]))
                    ipc_dir.mkdir(parents=True, exist_ok=True)
                    stdout_file.write_text("env ok", encoding="utf-8")
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
                    worker_env={
                        "AGENT_TEAM_SESSION_ID": "session-alpha",
                        "AGENT_TEAM_SESSION_DIR": str(session_root),
                        "AGENT_TEAM_SESSION_WORKDIR": str(session_root),
                        "AGENT_TEAM_SESSION_HOME": str(session_root / "home"),
                        "HOME": str(session_root / "home"),
                        "TMPDIR": str(session_tmp),
                    },
                    session_workspace_root=str(session_root),
                    session_workspace_workdir=str(session_root),
                    session_workspace_home_dir=str(session_root / "home"),
                    session_workspace_tmp_dir=str(session_tmp),
                )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(len(shell_commands), 1)
            self.assertIn("AGENT_TEAM_SESSION_ID=session-alpha", shell_commands[0])
            self.assertIn(f"AGENT_TEAM_SESSION_DIR={shlex.quote(str(session_root))}", shell_commands[0])
            self.assertIn(f"AGENT_TEAM_SESSION_WORKDIR={shlex.quote(str(session_root))}", shell_commands[0])
            self.assertIn(f"HOME={shlex.quote(str(session_root / 'home'))}", shell_commands[0])
            self.assertIn(f"TMPDIR={shlex.quote(str(session_tmp))}", shell_commands[0])
            self.assertIn("AGENT_TEAM_TRANSPORT_SESSION=agent_analyst_alpha", shell_commands[0])
            lifecycle = getattr(completed, "tmux_lifecycle", {})
            self.assertEqual(lifecycle.get("tmux_session_workspace_root"), str(session_root))
            self.assertEqual(lifecycle.get("tmux_session_workspace_workdir"), str(session_root))
            self.assertEqual(lifecycle.get("tmux_session_workspace_home_dir"), str(session_root / "home"))
            self.assertEqual(lifecycle.get("tmux_session_workspace_tmp_dir"), str(session_tmp))
            self.assertTrue(lifecycle.get("tmux_session_workspace_isolated"))

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
            session_registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_registry.activate_for_run(profile=analyst_profiles[0], transport="tmux")
            lead_context.session_registry = session_registry
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
                        "tmux_session_name": "agent_analyst_alpha",
                        "tmux_preferred_session_name": "agent_analyst_alpha",
                        "tmux_session_workspace_root": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha"
                        ),
                        "tmux_session_workspace_workdir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "target_snapshot"
                        ),
                        "tmux_session_workspace_home_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "home"
                        ),
                        "tmux_session_workspace_target_dir": str(
                            output_dir
                            / "_tmux_session_workspaces"
                            / "analyst_alpha"
                            / "session-alpha"
                            / "target_snapshot"
                        ),
                        "tmux_session_workspace_tmp_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "tmp"
                        ),
                        "tmux_session_workspace_scope": "tmux_session_workspace",
                        "tmux_session_workspace_isolated": True,
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
            session = session_registry.session_for("analyst_alpha")
            self.assertEqual(session.get("transport_session_name"), "agent_analyst_alpha")
            self.assertEqual(session.get("workspace_scope"), "tmux_session_workspace")
            self.assertTrue(session.get("workspace_isolation_active"))
            self.assertTrue(str(session.get("workspace_workdir", "")).endswith("target_snapshot"))
            self.assertTrue(str(session.get("workspace_home_dir", "")).endswith("home"))
            self.assertTrue(str(session.get("workspace_target_dir", "")).endswith("target_snapshot"))
            self.assertTrue(session.get("reuse_authorized"))
            self.assertEqual(session.get("transport_reuse_count"), 1)

    def test_cleanup_tmux_analyst_sessions_sweeps_preferred_sessions(self) -> None:
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
            analyst_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="analyst_beta", skills={"analysis"}, agent_type="analyst"),
            ]
            calls = []

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                calls.append(command)
                if command == ["tmux", "kill-session", "-t", "agent_analyst_alpha"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                if command == ["tmux", "kill-session", "-t", "agent_analyst_beta"]:
                    return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="can't find session")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ):
                summary = runtime.cleanup_tmux_analyst_sessions(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                )

            self.assertEqual(summary["cleaned"], 1)
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
            self.assertEqual(
                calls,
                [
                    ["tmux", "kill-session", "-t", "agent_analyst_alpha"],
                    ["tmux", "kill-session", "-t", "agent_analyst_beta"],
                ],
            )

    def test_cleanup_tmux_analyst_sessions_can_defer_for_resume(self) -> None:
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
                        "workspace_root": str(output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha"),
                        "workspace_target_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "target_snapshot"
                        ),
                        "workspace_tmp_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "tmp"
                        ),
                        "workspace_scope": "tmux_session_workspace",
                        "workspace_isolation_active": True,
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
            analyst_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            ]

            with mock.patch.object(runtime.tmux_transport.subprocess, "run") as tmux_run:
                summary = runtime.cleanup_tmux_analyst_sessions(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                )

            tmux_run.assert_not_called()
            self.assertEqual(summary["skipped"], "deferred_for_resume")
            self.assertEqual(summary["deferred_reason"], "max_completed_tasks reached (3)")
            lease_entry = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            self.assertEqual(lease_entry.get("status"), "retained")

    def test_recover_tmux_analyst_sessions_marks_retained_session_available(self) -> None:
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
                        "workspace_root": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha"
                        ),
                        "workspace_workdir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "target_snapshot"
                        ),
                        "workspace_home_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "home"
                        ),
                        "workspace_target_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "target_snapshot"
                        ),
                        "workspace_tmp_dir": str(
                            output_dir / "_tmux_session_workspaces" / "analyst_alpha" / "session-alpha" / "tmp"
                        ),
                        "workspace_scope": "tmux_session_workspace",
                        "workspace_isolation_active": True,
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
            analyst_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            ]
            session_registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_registry.activate_for_run(profile=analyst_profiles[0], transport="tmux")
            lead_context.session_registry = session_registry

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                if command == ["tmux", "has-session", "-t", "agent_analyst_alpha"]:
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ):
                summary = runtime.recover_tmux_analyst_sessions(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                    resume_from=output_dir / "run_checkpoint.json",
                )

            self.assertEqual(summary["recovered"], ["analyst_alpha"])
            lease_entry = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            self.assertEqual(lease_entry.get("status"), "recovered_available")
            self.assertTrue(lease_entry.get("reuse_authorized"))
            self.assertEqual(lease_entry.get("recovery_result"), "available")
            session = session_registry.session_for("analyst_alpha")
            self.assertEqual(session.get("transport"), "tmux")
            self.assertEqual(session.get("transport_session_name"), "agent_analyst_alpha")
            self.assertEqual(session.get("workspace_scope"), "tmux_session_workspace")
            self.assertTrue(session.get("workspace_isolation_active"))
            self.assertTrue(str(session.get("workspace_root", "")).endswith("session-alpha"))
            self.assertTrue(str(session.get("workspace_workdir", "")).endswith("target_snapshot"))
            self.assertTrue(str(session.get("workspace_home_dir", "")).endswith("home"))
            self.assertTrue(str(session.get("workspace_target_dir", "")).endswith("target_snapshot"))
            self.assertEqual(
                shared_state.get("tmux_session_recovery_summary", {}).get("recovered"),
                ["analyst_alpha"],
            )

    def test_recover_tmux_analyst_sessions_marks_missing_retained_session(self) -> None:
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
            analyst_profiles = [
                runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
                runtime.AgentProfile(name="analyst_beta", skills={"analysis"}, agent_type="analyst"),
            ]

            def fake_tmux_run(command, stdout=None, stderr=None, text=None, check=None):
                if command == ["tmux", "has-session", "-t", "agent_analyst_alpha"]:
                    return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="can't find session")
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(runtime.tmux_transport.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                runtime.tmux_transport.subprocess, "run", side_effect=fake_tmux_run
            ):
                summary = runtime.recover_tmux_analyst_sessions(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                )

            self.assertEqual(summary["missing"], ["analyst_alpha"])
            self.assertEqual(summary["inactive"], ["analyst_beta"])
            alpha_lease = shared_state.get("tmux_session_leases", {}).get("analyst_alpha", {})
            beta_lease = shared_state.get("tmux_session_leases", {}).get("analyst_beta", {})
            self.assertEqual(alpha_lease.get("status"), "recovered_missing")
            self.assertFalse(alpha_lease.get("reuse_authorized"))
            self.assertEqual(alpha_lease.get("recovery_result"), "missing")
            self.assertEqual(beta_lease.get("status"), "recovery_inactive")
            self.assertEqual(beta_lease.get("recovery_result"), "inactive")

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

            def fake_recovery(lead_context, analyst_profiles, resume_from):
                calls.append(
                    {
                        "lead": lead_context.profile.name,
                        "analysts": [profile.name for profile in analyst_profiles],
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
                    cleanup_tmux_analyst_sessions_fn=lambda **_kwargs: {"cleaned": 0},
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                calls,
                [
                    {
                        "lead": "lead",
                        "analysts": ["analyst_alpha", "analyst_beta"],
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

            def fake_cleanup(lead_context, analyst_profiles):
                cleanup_calls.append(
                    {
                        "lead": lead_context.profile.name,
                        "analysts": [profile.name for profile in analyst_profiles],
                    }
                )
                return {"cleaned": len(analyst_profiles)}

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
                [{"lead": "lead", "analysts": ["analyst_alpha", "analyst_beta"]}],
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

            def fake_cleanup(lead_context, analyst_profiles):
                cleanup_flags.append(
                    {
                        "deferred": bool(lead_context.shared_state.get("tmux_cleanup_deferred_for_resume", False)),
                        "reason": str(lead_context.shared_state.get("tmux_cleanup_deferred_reason", "")),
                        "analysts": [profile.name for profile in analyst_profiles],
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
                        "analysts": ["analyst_alpha", "analyst_beta"],
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

    def test_run_tmux_worker_task_builds_isolated_worker_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target_docs"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "guide.md").write_text("# Guide\nBody\n", encoding="utf-8")
            output_dir = target_dir / "artifacts"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "should_ignore.md").write_text("# Ignore\n", encoding="utf-8")
            logger = runtime.EventLogger(output_dir=output_dir)
            config = runtime.RuntimeConfig(teammate_mode="subprocess")
            subprocess_ok = subprocess.CompletedProcess(
                args=["python"],
                returncode=0,
                stdout=json.dumps({"result": {"ok": True}, "state_updates": {}}),
                stderr="",
            )
            captured_payload = {}

            def fake_run_subprocess(**kwargs):
                nonlocal captured_payload
                worker_payload_path = pathlib.Path(kwargs["command"][3])
                captured_payload = json.loads(worker_payload_path.read_text(encoding="utf-8"))
                return subprocess_ok

            with mock.patch.object(runtime, "_execute_worker_subprocess", side_effect=fake_run_subprocess) as run_subprocess:
                result = runtime._run_tmux_worker_task(
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                    output_dir=output_dir,
                    runtime_config=config,
                    payload={
                        "task_type": "discover_markdown",
                        "target_dir": str(target_dir),
                        "session_state": {
                            "session_id": "session-alpha",
                        },
                    },
                    worker_name="analyst_alpha",
                    logger=logger,
                    timeout_sec=1,
                )

            self.assertTrue(result["ok"])
            worker_env = run_subprocess.call_args.kwargs["worker_env"]
            self.assertEqual(worker_env["AGENT_TEAM_SESSION_ID"], "session-alpha")
            self.assertEqual(worker_env["AGENT_TEAM_AGENT"], "analyst_alpha")
            self.assertTrue(worker_env["AGENT_TEAM_SESSION_DIR"].endswith("session-alpha"))
            self.assertTrue(pathlib.Path(worker_env["AGENT_TEAM_SESSION_DIR"]).exists())
            self.assertTrue(pathlib.Path(worker_env["AGENT_TEAM_SESSION_TMP_DIR"]).exists())
            self.assertTrue(pathlib.Path(worker_env["AGENT_TEAM_SESSION_HOME"]).exists())
            isolated_target_dir = pathlib.Path(worker_env["AGENT_TEAM_WORKSPACE_TARGET_DIR"])
            self.assertTrue(isolated_target_dir.exists())
            self.assertTrue(pathlib.Path(worker_env["AGENT_TEAM_SESSION_WORKDIR"]).samefile(isolated_target_dir))
            self.assertEqual(pathlib.Path(worker_env["HOME"]), pathlib.Path(worker_env["AGENT_TEAM_SESSION_HOME"]))
            self.assertEqual(worker_env["TMPDIR"], worker_env["AGENT_TEAM_SESSION_TMP_DIR"])
            self.assertTrue(pathlib.Path(run_subprocess.call_args.kwargs["workdir"]).samefile(isolated_target_dir))
            self.assertEqual(captured_payload["target_dir"], str(isolated_target_dir))
            self.assertTrue((isolated_target_dir / "guide.md").exists())
            self.assertFalse((isolated_target_dir / "artifacts").exists())
            diagnostics = result.get("diagnostics", {})
            self.assertEqual(diagnostics.get("tmux_session_workspace_scope"), "tmux_session_workspace")
            self.assertTrue(diagnostics.get("tmux_session_workspace_isolated"))
            self.assertTrue(
                pathlib.Path(str(diagnostics.get("tmux_session_workspace_workdir", ""))).samefile(isolated_target_dir)
            )
            self.assertTrue(str(diagnostics.get("tmux_session_workspace_home_dir", "")).endswith("home"))
            self.assertEqual(diagnostics.get("tmux_session_workspace_target_dir"), str(isolated_target_dir))
            self.assertEqual(diagnostics.get("tmux_session_workspace_target_status"), "created")
            self.assertIn("AGENT_TEAM_SESSION_ID", diagnostics.get("tmux_session_env_keys", []))
            self.assertIn("HOME", diagnostics.get("tmux_session_env_keys", []))

    def test_run_tmux_worker_task_ignores_codex_tmp_in_target_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "repo"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "guide.md").write_text("# Guide\nBody\n", encoding="utf-8")
            (target_dir / ".codex_tmp").mkdir(parents=True, exist_ok=True)
            (target_dir / ".codex_tmp" / "ephemeral.txt").write_text("ignore me\n", encoding="utf-8")
            output_dir = target_dir / "artifacts"
            output_dir.mkdir(parents=True, exist_ok=True)
            logger = runtime.EventLogger(output_dir=output_dir)
            config = runtime.RuntimeConfig(teammate_mode="subprocess")
            subprocess_ok = subprocess.CompletedProcess(
                args=["python"],
                returncode=0,
                stdout=json.dumps({"result": {"ok": True}, "state_updates": {}}),
                stderr="",
            )

            with mock.patch.object(runtime, "_execute_worker_subprocess", return_value=subprocess_ok) as run_subprocess:
                result = runtime._run_tmux_worker_task(
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                    output_dir=output_dir,
                    runtime_config=config,
                    payload={
                        "task_type": "discover_markdown",
                        "target_dir": str(target_dir),
                        "session_state": {
                            "session_id": "session-alpha",
                        },
                    },
                    worker_name="analyst_alpha",
                    logger=logger,
                    timeout_sec=1,
                )

            self.assertTrue(result["ok"])
            isolated_target_dir = pathlib.Path(run_subprocess.call_args.kwargs["worker_env"]["AGENT_TEAM_WORKSPACE_TARGET_DIR"])
            self.assertFalse((isolated_target_dir / ".codex_tmp").exists())
            self.assertTrue((isolated_target_dir / "guide.md").exists())
            diagnostics = result.get("diagnostics", {})
            self.assertEqual(diagnostics.get("tmux_session_workspace_target_status"), "created")

    def test_run_tmux_worker_payload_plans_dynamic_reviewer_followups(self) -> None:
        result = runtime.run_tmux_worker_payload(
            {
                "task_type": "dynamic_planning",
                "task_payload": {},
                "shared_state": {
                    "heading_issues": [{"path": "a.md"}],
                    "length_issues": [{"path": "b.md"}],
                },
                "board_snapshot": {
                    "tasks": [
                        {"task_id": "dynamic_planning"},
                        {"task_id": "peer_challenge"},
                    ]
                },
                "runtime_config": {"enable_dynamic_tasks": True},
            }
        )

        self.assertEqual(
            result.get("result", {}).get("inserted_tasks"),
            ["heading_structure_followup", "length_risk_followup"],
        )
        insert_tasks = result.get("task_mutations", {}).get("insert_tasks", [])
        self.assertEqual(
            [item.get("task_id") for item in insert_tasks],
            ["heading_structure_followup", "length_risk_followup"],
        )
        add_dependencies = result.get("task_mutations", {}).get("add_dependencies", [])
        self.assertEqual(
            [item.get("dependency_id") for item in add_dependencies],
            ["heading_structure_followup", "length_risk_followup"],
        )

    def test_run_tmux_worker_payload_plans_repo_dynamic_reviewer_followups(self) -> None:
        result = runtime.run_tmux_worker_payload(
            {
                "task_type": "repo_dynamic_planning",
                "task_payload": {},
                "shared_state": {
                    "repository_inventory": [
                        {"top_level_dir": "src"},
                        {"top_level_dir": "docs"},
                    ],
                    "repository_extension_summary": {"unique_extensions": 3},
                    "repository_large_files": [{"path": "big.bin"}],
                },
                "board_snapshot": {
                    "tasks": [
                        {"task_id": "repo_dynamic_planning"},
                        {"task_id": "peer_challenge"},
                    ]
                },
                "runtime_config": {"enable_dynamic_tasks": True},
            }
        )

        self.assertEqual(
            result.get("result", {}).get("inserted_tasks"),
            ["extension_hotspot_followup", "directory_hotspot_followup"],
        )
        insert_tasks = result.get("task_mutations", {}).get("insert_tasks", [])
        self.assertEqual(
            [item.get("task_id") for item in insert_tasks],
            ["extension_hotspot_followup", "directory_hotspot_followup"],
        )

    def test_run_tmux_worker_payload_writes_markdown_recommendation_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            report_path = output_dir / "final_report.md"
            result = runtime.run_tmux_worker_payload(
                {
                    "task_type": "recommendation_pack",
                    "goal": "Audit markdown quality",
                    "output_dir": str(output_dir),
                    "task_context": {
                        "visible_shared_state": {
                            "dynamic_plan": {"enabled": True, "inserted_tasks": ["heading_structure_followup"]},
                            "markdown_inventory": [{"path": "docs/a.md"}],
                            "heading_issues": [{"path": "docs/a.md"}],
                            "length_issues": [{"path": "docs/b.md", "line_count": 240}],
                            "peer_challenge": {},
                            "lead_adjudication": {"verdict": "challenge", "score": 61},
                            "evidence_pack": {"triggered": False, "reason": "not required"},
                            "lead_re_adjudication": {"verdict": "accept", "score": 79, "rationale": "covered"},
                            "llm_synthesis": {
                                "content": "- Fix headings first",
                                "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                            },
                        }
                    },
                    "board_snapshot": {
                        "tasks": [
                            {"task_id": "heading_audit", "result": {"files_without_headings": 1}},
                            {"task_id": "length_audit", "result": {"long_files": 1}},
                        ]
                    },
                }
            )

            written_report_path = pathlib.Path(str(result.get("result", {}).get("report_path", "")))
            self.assertEqual(written_report_path.name, report_path.name)
            self.assertTrue(written_report_path.exists())
            self.assertTrue(report_path.exists())
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("# Agent Team Report", report)
            self.assertIn("## Recommended Actions", report)
            self.assertIn("- Fix headings first", report)

    def test_run_tmux_worker_payload_writes_repo_recommendation_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            report_path = output_dir / "final_report.md"
            result = runtime.run_tmux_worker_payload(
                {
                    "task_type": "repo_recommendation_pack",
                    "goal": "Audit repository layout",
                    "output_dir": str(output_dir),
                    "task_context": {
                        "visible_shared_state": {
                            "repository_inventory": [{"path": "src/app.py"}, {"path": "docs/readme.md"}],
                            "repository_extension_summary": {
                                "unique_extensions": 2,
                                "files_without_extension": 0,
                                "top_extensions": [{"extension": ".py", "file_count": 3, "total_lines": 180}],
                            },
                            "repository_large_files": [{"path": "src/big.py", "line_count": 360, "byte_count": 22000}],
                            "repo_dynamic_plan": {"enabled": True, "inserted_tasks": ["extension_hotspot_followup"]},
                            "repo_extension_hotspots": {"extension_hotspots": [{"extension": ".py", "file_count": 3, "total_lines": 180}]},
                            "repo_directory_hotspots": {"busiest_directories": [{"top_level_dir": "src", "file_count": 3, "total_lines": 180}]},
                            "peer_challenge": {},
                            "lead_adjudication": {"verdict": "challenge", "score": 63},
                            "evidence_pack": {"triggered": False, "reason": "not required"},
                            "lead_re_adjudication": {"verdict": "accept", "score": 81, "rationale": "covered"},
                            "llm_synthesis": {
                                "content": "- Reduce oversized files",
                                "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                            },
                        }
                    },
                    "board_snapshot": {
                        "tasks": [
                            {"task_id": "large_file_audit", "result": {"oversized_files": 1}},
                        ]
                    },
                }
            )

            written_report_path = pathlib.Path(str(result.get("result", {}).get("report_path", "")))
            self.assertEqual(written_report_path.name, report_path.name)
            self.assertTrue(written_report_path.exists())
            self.assertTrue(report_path.exists())
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("# Agent Team Report", report)
            self.assertIn("## Recommended Actions", report)
            self.assertIn("- Reduce oversized files", report)

    def test_run_tmux_worker_payload_runs_markdown_llm_synthesis_in_worker(self) -> None:
        result = runtime.run_tmux_worker_payload(
            {
                "task_type": "llm_synthesis",
                "goal": "Audit markdown quality",
                "output_dir": ".",
                "model_config": {
                    "provider_name": "heuristic",
                    "model": "heuristic-v1",
                    "openai_api_key_env": "OPENAI_API_KEY",
                    "openai_base_url": "https://api.openai.com/v1",
                    "require_llm": False,
                    "timeout_sec": 5,
                },
                "task_context": {
                    "visible_shared_state": {
                        "workflow": {"pack": "markdown-audit"},
                        "heading_issues": [{"path": "docs/a.md"}],
                        "length_issues": [{"path": "docs/b.md", "line_count": 240}],
                        "dynamic_plan": {"enabled": True, "inserted_tasks": ["heading_structure_followup"]},
                        "peer_challenge": {},
                        "lead_adjudication": {"verdict": "challenge", "score": 60},
                        "evidence_pack": {"triggered": False, "reason": "not required"},
                        "lead_re_adjudication": {"verdict": "accept", "score": 78, "rationale": "covered"},
                    }
                },
                "board_snapshot": {
                    "tasks": [
                        {"task_id": "heading_audit", "result": {"files_without_headings": 1}},
                        {"task_id": "length_audit", "result": {"long_files": 1}},
                        {"task_id": "dynamic_planning", "result": {"enabled": True}},
                    ]
                },
            }
        )

        llm_synthesis = result.get("state_updates", {}).get("llm_synthesis", {})
        self.assertEqual(llm_synthesis.get("provider", {}).get("provider"), "heuristic")
        self.assertTrue(llm_synthesis.get("content", "").strip())
        self.assertEqual(result.get("result", {}).get("provider", {}).get("provider"), "heuristic")
        self.assertTrue(result.get("result", {}).get("preview", "").strip())

    def test_run_tmux_worker_payload_runs_repo_llm_synthesis_in_worker(self) -> None:
        result = runtime.run_tmux_worker_payload(
            {
                "task_type": "llm_synthesis",
                "goal": "Audit repository layout",
                "output_dir": ".",
                "task_context": {
                    "visible_shared_state": {
                        "workflow": {"pack": "repo-audit"},
                        "agent_team_config": {
                            "model": {
                                "provider_name": "heuristic",
                                "model": "heuristic-v1",
                                "openai_api_key_env": "OPENAI_API_KEY",
                                "openai_base_url": "https://api.openai.com/v1",
                                "require_llm": False,
                                "timeout_sec": 5,
                            }
                        },
                        "repository_inventory": [{"path": "src/app.py"}, {"path": "docs/readme.md"}],
                        "repository_large_files": [{"path": "src/big.py", "line_count": 360, "byte_count": 22000}],
                        "repository_extension_summary": {
                            "unique_extensions": 2,
                            "files_without_extension": 0,
                            "top_extensions": [{"extension": ".py", "file_count": 3, "total_lines": 180}],
                        },
                        "repo_dynamic_plan": {"enabled": True, "inserted_tasks": ["extension_hotspot_followup"]},
                        "peer_challenge": {},
                        "lead_adjudication": {"verdict": "challenge", "score": 63},
                        "evidence_pack": {"triggered": False, "reason": "not required"},
                        "lead_re_adjudication": {"verdict": "accept", "score": 81, "rationale": "covered"},
                    }
                },
                "board_snapshot": {
                    "tasks": [
                        {"task_id": "extension_audit", "result": {"unique_extensions": 2}},
                        {"task_id": "large_file_audit", "result": {"oversized_files": 1}},
                        {"task_id": "repo_dynamic_planning", "result": {"enabled": True}},
                    ]
                },
            }
        )

        llm_synthesis = result.get("state_updates", {}).get("llm_synthesis", {})
        self.assertEqual(llm_synthesis.get("provider", {}).get("provider"), "heuristic")
        self.assertIn("Priority actions:", llm_synthesis.get("content", ""))
        self.assertEqual(result.get("result", {}).get("provider", {}).get("provider"), "heuristic")

    def test_tmux_worker_payload_supports_lead_adjudication(self) -> None:
        result = runtime.tmux_transport.run_tmux_worker_payload(
            {
                "task_id": "lead_adjudication",
                "task_type": "lead_adjudication",
                "task_payload": {},
                "runtime_config": runtime.RuntimeConfig().to_dict(),
                "goal": "test",
                "target_dir": ".",
                "output_dir": ".",
                "profile": {"name": "lead", "skills": ["lead"], "agent_type": "lead"},
                "task_context": {
                    "visible_shared_state": {
                        "team_profiles": [
                            {"name": "analyst_alpha", "agent_type": "analyst"},
                            {"name": "analyst_beta", "agent_type": "analyst"},
                        ]
                    }
                },
                "board_snapshot": {
                    "tasks": [
                        {
                            "task_id": "peer_challenge",
                            "result": {
                                "targets": ["analyst_alpha", "analyst_beta"],
                                "round1": {
                                    "received_replies": {
                                        "analyst_alpha": "x",
                                        "analyst_beta": "y",
                                    }
                                },
                                "round2": {
                                    "received_replies": {
                                        "analyst_alpha": "z" * 220,
                                        "analyst_beta": "k" * 220,
                                    }
                                },
                            },
                        }
                    ]
                },
            }
        )

        self.assertEqual(result["result"]["verdict"], "accept")
        self.assertEqual(result["state_updates"]["lead_adjudication"]["verdict"], "accept")

    def test_tmux_worker_payload_supports_lead_re_adjudication(self) -> None:
        result = runtime.tmux_transport.run_tmux_worker_payload(
            {
                "task_id": "lead_re_adjudication",
                "task_type": "lead_re_adjudication",
                "task_payload": {},
                "runtime_config": runtime.RuntimeConfig(re_adjudication_max_bonus=20).to_dict(),
                "goal": "test",
                "target_dir": ".",
                "output_dir": ".",
                "profile": {"name": "lead", "skills": ["lead"], "agent_type": "lead"},
                "board_snapshot": {
                    "tasks": [
                        {
                            "task_id": "lead_adjudication",
                            "result": {
                                "verdict": "challenge",
                                "score": 70,
                                "thresholds": {"accept": 75, "challenge": 50},
                                "weights": {"completeness": 0.4},
                                "targets": ["analyst_alpha", "analyst_beta"],
                            },
                        },
                        {
                            "task_id": "evidence_pack",
                            "result": {
                                "triggered": True,
                                "targets": ["analyst_alpha", "analyst_beta"],
                                "received_replies": {
                                    "analyst_alpha": "x" * 240,
                                    "analyst_beta": "y" * 240,
                                },
                            },
                        },
                    ]
                },
            }
        )

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
                target=runtime.tmux_transport._serve_mailbox_bridge,
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
                json.dumps(
                    {
                        "event": "lead_adjudication_published",
                        "fields": {"verdict": "accept", "score": 80},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            runtime.tmux_transport._replay_worker_bridge_events(
                context=context,
                event_bridge_path=event_bridge_path,
            )

            self.assertFalse(event_bridge_path.exists())
            events = (root / "out" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("lead_adjudication_published", events)


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

    def test_build_team_progress_snapshot_summarizes_agent_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="analyst_done",
                        title="Analyst done",
                        task_type="analysis",
                        required_skills={"analysis"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                    runtime.Task(
                        task_id="review_failed",
                        title="Review failed",
                        task_type="review",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                    runtime.Task(
                        task_id="lead_ready",
                        title="Lead ready",
                        task_type="lead_task",
                        required_skills={"lead"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"lead"},
                    ),
                    runtime.Task(
                        task_id="analyst_blocked",
                        title="Analyst blocked",
                        task_type="analysis",
                        required_skills={"analysis"},
                        dependencies=["lead_ready"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                ],
                logger=logger,
            )
            analyst_task = board.claim_next(
                agent_name="analyst_alpha",
                agent_skills={"analysis"},
                agent_type="analyst",
            )
            self.assertIsNotNone(analyst_task)
            board.complete(
                task_id="analyst_done",
                owner="analyst_alpha",
                result={"ok": True},
            )
            reviewer_task = board.claim_next(
                agent_name="reviewer_gamma",
                agent_skills={"review"},
                agent_type="reviewer",
            )
            self.assertIsNotNone(reviewer_task)
            board.fail(
                task_id="review_failed",
                owner="reviewer_gamma",
                error="expected failure",
            )
            logger.log(
                "mail_sent",
                sender="lead",
                recipient="analyst_alpha",
                subject="assignment",
                body="inspect docs",
            )

            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "team",
                {
                    "lead_name": "lead",
                    "mailbox_model": "asynchronous pull-based inbox",
                },
            )
            shared_state.set(
                "team_profiles",
                [
                    {
                        "name": "analyst_alpha",
                        "skills": ["analysis"],
                        "agent_type": "analyst",
                    },
                    {
                        "name": "reviewer_gamma",
                        "skills": ["review", "writer"],
                        "agent_type": "reviewer",
                    },
                ],
            )

            snapshot = runtime.build_team_progress_snapshot(
                board=board,
                shared_state=shared_state,
                logger=logger,
            )
            self.assertEqual(snapshot["task_status_counts"]["completed"], 1)
            self.assertEqual(snapshot["task_status_counts"]["failed"], 1)
            self.assertEqual(snapshot["task_status_counts"]["pending"], 1)
            self.assertEqual(snapshot["task_status_counts"]["blocked"], 1)

            agents = {item["name"]: item for item in snapshot["agents"]}
            self.assertEqual(agents["lead"]["available_tasks"], 1)
            self.assertEqual(agents["analyst_alpha"]["tasks_completed"], 1)
            self.assertEqual(agents["analyst_alpha"]["blocked_tasks"], 1)
            self.assertEqual(agents["analyst_alpha"]["messages_received"], 1)
            self.assertEqual(agents["reviewer_gamma"]["tasks_failed"], 1)

    def test_teammate_session_registry_tracks_agent_activity(self) -> None:
        shared_state = runtime.SharedState()
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        profile = runtime.AgentProfile(
            name="analyst_alpha",
            skills={"analysis"},
            agent_type="analyst",
        )
        registry.ensure_profile(profile=profile, transport="in-process", status="created")
        task = runtime.Task(
            task_id="heading_audit",
            title="heading",
            task_type="heading_audit",
            required_skills={"analysis"},
            dependencies=[],
            payload={},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        )
        registry.record_status(agent_name="analyst_alpha", transport="in-process", status="ready")
        registry.bind_task(
            agent_name="analyst_alpha",
            task=task,
            transport="in-process",
            task_context={
                "visible_shared_state_keys": ["lead_name", "markdown_inventory"],
                "visible_shared_state_key_count": 2,
            },
        )
        registry.record_message_seen(
            agent_name="analyst_alpha",
            message=runtime.Message(
                message_id="m1",
                sent_at=runtime.utc_now(),
                sender="lead",
                recipient="analyst_alpha",
                subject="assignment",
                body="inspect headings",
                task_id="heading_audit",
            ),
        )
        registry.record_provider_reply(
            agent_name="analyst_alpha",
            topic="peer_challenge_round1",
            reply="Use parser fallback",
            memory_turns=4,
        )
        registry.record_task_result(
            agent_name="analyst_alpha",
            task=task,
            transport="in-process",
            success=True,
            status="ready",
        )

        session = registry.session_for("analyst_alpha")
        self.assertTrue(session["session_id"])
        self.assertEqual(session["tasks_started"], 1)
        self.assertEqual(session["tasks_completed"], 1)
        self.assertEqual(session["messages_seen"], 1)
        self.assertEqual(session["provider_replies"], 1)
        self.assertEqual(session["last_visible_shared_state_key_count"], 2)
        self.assertEqual(len(session["provider_memory"]), 1)
        self.assertEqual(session["provider_memory"][0]["topic"], "peer_challenge_round1")

    def test_teammate_session_registry_applies_telemetry_events(self) -> None:
        shared_state = runtime.SharedState()
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        telemetry_events = [
            {
                "agent": "reviewer_gamma",
                "agent_type": "reviewer",
                "skills": ["review", "writer"],
                "transport": "host",
                "event_type": "status",
                "status": "ready",
            },
            {
                "agent": "reviewer_gamma",
                "agent_type": "reviewer",
                "skills": ["review", "writer"],
                "transport": "host",
                "event_type": "bind_task",
                "task_id": "peer_challenge",
                "task_type": "peer_challenge",
                "visible_shared_state_keys": ["lead_name", "peer_challenge"],
                "visible_shared_state_key_count": 2,
            },
            {
                "agent": "reviewer_gamma",
                "agent_type": "reviewer",
                "skills": ["review", "writer"],
                "transport": "host",
                "event_type": "message_seen",
                "from_agent": "lead",
                "subject": "session_task_assignment",
                "task_id": "peer_challenge",
            },
            {
                "agent": "reviewer_gamma",
                "agent_type": "reviewer",
                "skills": ["review", "writer"],
                "transport": "host",
                "event_type": "provider_reply",
                "topic": "peer_challenge_round1",
                "reply": "reply",
                "memory_turns": 4,
            },
            {
                "agent": "reviewer_gamma",
                "agent_type": "reviewer",
                "skills": ["review", "writer"],
                "transport": "host",
                "event_type": "task_result",
                "task_id": "peer_challenge",
                "task_type": "peer_challenge",
                "success": True,
                "status": "ready",
            },
        ]

        for telemetry in telemetry_events:
            registry.apply_telemetry(telemetry)

        session = registry.session_for("reviewer_gamma")
        self.assertEqual(session["transport"], "host")
        self.assertEqual(session["tasks_started"], 1)
        self.assertEqual(session["tasks_completed"], 1)
        self.assertEqual(session["messages_seen"], 1)
        self.assertEqual(session["provider_replies"], 1)
        self.assertEqual(session["last_visible_shared_state_key_count"], 2)
        self.assertEqual(session["current_task_id"], "")
        self.assertEqual(len(session["provider_memory"]), 1)
        self.assertEqual(session["provider_memory"][0]["topic"], "peer_challenge_round1")

    def test_teammate_session_registry_preserves_session_id_on_resume(self) -> None:
        shared_state = runtime.SharedState()
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        profile = runtime.AgentProfile(
            name="analyst_alpha",
            skills={"analysis"},
            agent_type="analyst",
        )

        initialized = registry.activate_for_run(profile=profile, transport="in-process")
        resumed = registry.activate_for_run(
            profile=profile,
            transport="in-process",
            resume_from="D:/tmp/run_checkpoint.json",
        )

        self.assertEqual(initialized["lifecycle_event"], "initialized")
        self.assertEqual(resumed["lifecycle_event"], "resumed")
        self.assertEqual(resumed["session_id"], initialized["session_id"])
        self.assertEqual(resumed["run_activations"], 2)
        self.assertEqual(resumed["initialization_count"], 1)
        self.assertEqual(resumed["resume_count"], 1)
        self.assertEqual(resumed["last_resume_from"], "D:/tmp/run_checkpoint.json")

    def test_build_teammate_sessions_snapshot_summarizes_registry(self) -> None:
        shared_state = runtime.SharedState()
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
        registry.activate_for_run(profile=analyst, transport="tmux")
        registry.activate_for_run(profile=reviewer, transport="in-process")
        registry.record_status(agent_name="analyst_alpha", transport="tmux", status="retained")
        registry.record_status(agent_name="reviewer_gamma", transport="in-process", status="ready")
        registry.activate_for_run(
            profile=analyst,
            transport="tmux",
            resume_from="D:/tmp/run_checkpoint.json",
        )

        snapshot = runtime.build_teammate_sessions_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_count"], 2)
        self.assertEqual(snapshot["transport_counts"]["tmux"], 1)
        self.assertEqual(snapshot["transport_counts"]["in-process"], 1)
        self.assertEqual(snapshot["status_counts"]["resumed"], 1)
        self.assertEqual(snapshot["status_counts"]["ready"], 1)
        self.assertEqual(snapshot["lifecycle_counts"]["run_activations"], 3)
        self.assertEqual(snapshot["lifecycle_counts"]["initializations"], 2)
        self.assertEqual(snapshot["lifecycle_counts"]["resumes"], 1)

    def test_run_team_resume_logs_teammate_session_resumed_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "out"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            resume_from = output_dir / runtime.CHECKPOINT_FILENAME
            checkpoint_sessions = {
                "analyst_alpha": {
                    "session_id": "session-alpha",
                    "agent": "analyst_alpha",
                    "agent_type": "analyst",
                    "skills": ["analysis"],
                    "transport": "in-process",
                    "status": "stopped",
                    "started_at": runtime.utc_now(),
                    "last_active_at": runtime.utc_now(),
                    "initialization_count": 1,
                    "run_activations": 1,
                },
                "analyst_beta": {
                    "session_id": "session-beta",
                    "agent": "analyst_beta",
                    "agent_type": "analyst",
                    "skills": ["analysis"],
                    "transport": "in-process",
                    "status": "stopped",
                    "started_at": runtime.utc_now(),
                    "last_active_at": runtime.utc_now(),
                    "initialization_count": 1,
                    "run_activations": 1,
                },
                "reviewer_gamma": {
                    "session_id": "session-reviewer",
                    "agent": "reviewer_gamma",
                    "agent_type": "reviewer",
                    "skills": ["review", "writer"],
                    "transport": "in-process",
                    "status": "stopped",
                    "started_at": runtime.utc_now(),
                    "last_active_at": runtime.utc_now(),
                    "initialization_count": 1,
                    "run_activations": 1,
                },
            }
            resume_from.write_text(
                json.dumps(
                    {
                        "version": runtime.CHECKPOINT_VERSION,
                        "saved_at": runtime.utc_now(),
                        "goal": "resume",
                        "target_dir": str(target_dir),
                        "output_dir": str(output_dir),
                        "runtime_config": runtime.RuntimeConfig().to_dict(),
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
                                }
                            ]
                        },
                        "shared_state": {
                            "teammate_sessions": checkpoint_sessions,
                        },
                    }
                ),
                encoding="utf-8",
            )
            workflow_pack = mock.Mock()
            workflow_pack.build_handlers.return_value = {}
            workflow_pack.build_tasks.return_value = []
            workflow_pack.runtime_metadata = mock.Mock(lead_task_order=(), report_task_ids=())

            def fake_worker_factory(**_kwargs):
                return threading.Thread(target=lambda: None)

            with mock.patch("agent_team.runtime.engine.resolve_workflow_pack", return_value=workflow_pack):
                exit_code = runtime.run_team_impl(
                    goal="resume",
                    target_dir=target_dir,
                    output_dir=output_dir,
                    runtime_config=runtime.RuntimeConfig(),
                    provider_name="heuristic",
                    model="heuristic-v1",
                    openai_api_key_env="OPENAI_API_KEY",
                    openai_base_url="https://api.openai.com/v1",
                    require_llm=False,
                    provider_timeout_sec=5,
                    resume_from=resume_from,
                    teammate_agent_factory=fake_worker_factory,
                    run_tmux_analyst_task_once_fn=lambda **_kwargs: False,
                    cleanup_tmux_analyst_sessions_fn=lambda **_kwargs: {"cleaned": 0},
                    runtime_script=pathlib.Path(runtime.__file__).resolve(),
                )

            self.assertEqual(exit_code, 0)
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            resumed_events = [item for item in events if item.get("event") == "teammate_session_resumed"]
            self.assertEqual(len(resumed_events), 3)
            self.assertEqual(
                {item.get("session_id") for item in resumed_events},
                {"session-alpha", "session-beta", "session-reviewer"},
            )
            self.assertTrue(all(item.get("resume_from") == str(resume_from) for item in resumed_events))

    def test_teammate_transport_for_profile_supports_subprocess_mode(self) -> None:
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
        config = runtime.RuntimeConfig(teammate_mode="subprocess")

        self.assertEqual(
            runtime.teammate_transport_for_profile(profile=analyst, runtime_config=config),
            "subprocess",
        )
        self.assertEqual(
            runtime.teammate_transport_for_profile(profile=reviewer, runtime_config=config),
            "in-process",
        )

    def test_build_session_boundary_snapshot_distinguishes_tmux_and_runtime_sessions(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "generic-cli",
                "session_transport": "thread",
                "capabilities": {
                    "independent_sessions": False,
                    "workspace_isolation": False,
                },
                "limits": ["session_isolation_emulated"],
            },
        )
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
        registry.ensure_profile(profile=analyst, transport="tmux", status="retained")
        registry.ensure_profile(profile=reviewer, transport="in-process", status="ready")

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_count"], 2)
        self.assertEqual(snapshot["boundary_mode_counts"]["tmux_worker_session"], 1)
        self.assertEqual(snapshot["boundary_mode_counts"]["runtime_emulated_session"], 1)
        self.assertEqual(snapshot["boundary_strength_counts"]["medium"], 1)
        self.assertEqual(snapshot["boundary_strength_counts"]["emulated"], 1)

    def test_build_session_boundary_snapshot_captures_workspace_isolation_metadata(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "generic-cli",
                "session_transport": "process",
                "capabilities": {
                    "independent_sessions": False,
                    "workspace_isolation": False,
                },
                "limits": ["session_isolation_emulated"],
            },
        )
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        registry.activate_for_run(profile=analyst, transport="subprocess")
        registry.record_boundary(
            agent_name="analyst_alpha",
            transport="subprocess",
            transport_session_name="worker_subprocess_analyst_alpha",
            workspace_root="D:/tmp/session-alpha",
            workspace_workdir="D:/tmp/session-alpha/target_snapshot",
            workspace_home_dir="D:/tmp/session-alpha/home",
            workspace_target_dir="D:/tmp/session-alpha/target_snapshot",
            workspace_tmp_dir="D:/tmp/session-alpha/tmp",
            workspace_scope="tmux_session_workspace",
            workspace_isolation_active=True,
            reuse_authorized=True,
            transport_reuse_count=2,
        )

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["boundary_mode_counts"]["worker_subprocess_session"], 1)
        session = snapshot["sessions"][0]
        self.assertEqual(session["transport_session_name"], "worker_subprocess_analyst_alpha")
        self.assertEqual(session["workspace_scope"], "tmux_session_workspace")
        self.assertTrue(session["workspace_isolation_active"])
        self.assertEqual(session["workspace_workdir"], "D:/tmp/session-alpha/target_snapshot")
        self.assertEqual(session["workspace_home_dir"], "D:/tmp/session-alpha/home")
        self.assertEqual(session["workspace_target_dir"], "D:/tmp/session-alpha/target_snapshot")
        self.assertEqual(session["transport_reuse_count"], 2)
        self.assertIn("session_workspace_scoped_tmpdir", session["notes"])
        self.assertIn("session_workspace_scoped_workdir", session["notes"])
        self.assertIn("session_workspace_scoped_home", session["notes"])
        self.assertIn("session_workspace_scoped_target_dir", session["notes"])

    def test_tmux_mailbox_helper_does_not_override_worker_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(participants=["lead", "analyst_alpha"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="tmux")
            context = runtime.AgentContext(
                profile=profile,
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
                session_state=session_state,
                session_registry=registry,
            )
            stop_event = threading.Event()
            stop_event.set()
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=stop_event,
                claim_tasks=False,
                handlers={},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )

            agent.run()

            session = registry.session_for("analyst_alpha")
            self.assertEqual(session["transport"], "tmux")
            self.assertEqual(session["status"], "stopped")
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            started = [item for item in events if item.get("event") == "teammate_session_started"]
            stopped = [item for item in events if item.get("event") == "teammate_session_stopped"]
            self.assertEqual(started[-1].get("transport"), "tmux")
            self.assertEqual(stopped[-1].get("transport"), "tmux")

    def test_subprocess_mailbox_helper_does_not_override_worker_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(participants=["lead", "analyst_alpha"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="subprocess")
            context = runtime.AgentContext(
                profile=profile,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="subprocess"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                session_state=session_state,
                session_registry=registry,
            )
            stop_event = threading.Event()
            stop_event.set()
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=stop_event,
                claim_tasks=False,
                handlers={},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )

            agent.run()

            session = registry.session_for("analyst_alpha")
            self.assertEqual(session["transport"], "subprocess")
            self.assertEqual(session["status"], "stopped")
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            started = [item for item in events if item.get("event") == "teammate_session_started"]
            stopped = [item for item in events if item.get("event") == "teammate_session_stopped"]
            self.assertEqual(started[-1].get("transport"), "subprocess")
            self.assertEqual(stopped[-1].get("transport"), "subprocess")

    def test_non_claiming_mailbox_helper_leaves_non_request_messages_for_task_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma", "analyst_alpha"],
                logger=logger,
            )
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="host")
            context = runtime.AgentContext(
                profile=profile,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                session_state=session_state,
                session_registry=registry,
            )
            mailbox.send(
                sender="analyst_alpha",
                recipient="reviewer_gamma",
                subject="evidence_reply",
                body="leave this for the requester",
                task_id="evidence_pack",
            )
            mailbox.send(
                sender="lead",
                recipient="reviewer_gamma",
                subject="peer_challenge_round1_request",
                body="identify one weak assumption",
                task_id="peer_challenge",
            )
            stop_event = threading.Event()
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=stop_event,
                claim_tasks=False,
                handlers={},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )

            agent.run()

            reviewer_mail = mailbox.pull("reviewer_gamma")
            lead_mail = mailbox.pull("lead")

            self.assertEqual(len(reviewer_mail), 1)
            self.assertEqual(reviewer_mail[0].subject, "evidence_reply")
            self.assertTrue(
                any(message.subject == "peer_challenge_round1_reply" for message in lead_mail),
            )

    def test_mailbox_reviewer_tasks_remain_in_process_in_subprocess_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(participants=["lead", "reviewer_gamma"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="reviewer_gamma", skills={"review", "writer", "llm"}, agent_type="reviewer")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="subprocess")
            context = runtime.AgentContext(
                profile=profile,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="subprocess"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=threading.Event(),
                claim_tasks=True,
                handlers={},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )

            self.assertEqual(
                agent._task_transport(
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
                ),
                "in-process",
            )
            self.assertEqual(
                agent._task_transport(
                    runtime.Task(
                        task_id="evidence_pack",
                        title="Evidence pack",
                        task_type="evidence_pack",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    )
                ),
                "in-process",
            )
            self.assertEqual(
                agent._task_transport(
                    runtime.Task(
                        task_id="llm_synthesis",
                        title="LLM synthesis",
                        task_type="llm_synthesis",
                        required_skills={"llm"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    )
                ),
                "subprocess",
            )
            self.assertFalse(
                set(runtime.MAILBOX_REVIEWER_TASK_TYPES) & set(runtime.SUBPROCESS_REVIEWER_TASK_TYPES)
            )

    def test_reviewer_tasks_use_tmux_transport_in_tmux_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(tasks=[], logger=logger)
            mailbox = runtime.Mailbox(participants=["lead", "reviewer_gamma"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="reviewer_gamma", skills={"review", "writer", "llm"}, agent_type="reviewer")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="tmux")
            context = runtime.AgentContext(
                profile=profile,
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
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=threading.Event(),
                claim_tasks=True,
                handlers={},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )

            self.assertEqual(
                agent._task_transport(
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
                ),
                "tmux",
            )
            self.assertEqual(
                agent._task_transport(
                    runtime.Task(
                        task_id="llm_synthesis",
                        title="LLM synthesis",
                        task_type="llm_synthesis",
                        required_skills={"llm"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    )
                ),
                "tmux",
            )

    def test_reviewer_dynamic_planning_can_run_in_subprocess_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="dynamic_planning",
                        title="Plan follow-up work",
                        task_type="dynamic_planning",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                    runtime.Task(
                        task_id="peer_challenge",
                        title="Challenge",
                        task_type="peer_challenge",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead", "reviewer_gamma"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set("heading_issues", [{"path": "a.md"}])
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="in-process")
            context = runtime.AgentContext(
                profile=profile,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="subprocess"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=threading.Event(),
                claim_tasks=True,
                handlers={"dynamic_planning": runtime.handle_dynamic_planning},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )
            claimed = board.claim_specific(
                task_id="dynamic_planning",
                agent_name=profile.name,
                agent_skills=profile.skills,
                agent_type=profile.agent_type,
            )
            self.assertIsNotNone(claimed)
            fake_execution = {
                "ok": True,
                "transport": "subprocess",
                "payload": {
                    "result": {
                        "enabled": True,
                        "inserted_tasks": ["heading_structure_followup"],
                        "peer_challenge_dependencies_added": ["heading_structure_followup"],
                    },
                    "state_updates": {
                        "dynamic_plan": {
                            "enabled": True,
                            "inserted_tasks": ["heading_structure_followup"],
                            "peer_challenge_dependencies_added": ["heading_structure_followup"],
                        }
                    },
                    "task_mutations": {
                        "insert_tasks": [
                            {
                                "task_id": "heading_structure_followup",
                                "title": "Run heading structure follow-up audit",
                                "task_type": "heading_structure_followup",
                                "required_skills": ["analysis"],
                                "dependencies": ["dynamic_planning"],
                                "payload": {"top_n": 8},
                                "locked_paths": [],
                                "allowed_agent_types": ["analyst"],
                            }
                        ],
                        "add_dependencies": [
                            {
                                "task_id": "peer_challenge",
                                "dependency_id": "heading_structure_followup",
                            }
                        ],
                    },
                },
                "diagnostics": {
                    "tmux_session_workspace_root": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer"),
                    "tmux_session_workspace_workdir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "target_snapshot"),
                    "tmux_session_workspace_home_dir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "home"),
                    "tmux_session_workspace_target_dir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "target_snapshot"),
                    "tmux_session_workspace_tmp_dir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "tmp"),
                    "tmux_session_workspace_scope": "tmux_session_workspace",
                    "tmux_session_workspace_isolated": True,
                },
            }
            with mock.patch.object(runtime.tmux_transport, "run_tmux_worker_task", return_value=fake_execution):
                agent._run_task(claimed)

            board_snapshot = board.snapshot()
            task_statuses = {item["task_id"]: item for item in board_snapshot.get("tasks", [])}
            self.assertEqual(task_statuses["dynamic_planning"]["status"], "completed")
            self.assertIn("heading_structure_followup", task_statuses)
            self.assertIn("heading_structure_followup", task_statuses["peer_challenge"]["dependencies"])
            session = registry.session_for("reviewer_gamma")
            self.assertTrue(session.get("workspace_isolation_active"))
            self.assertEqual(session.get("task_history", [])[-1].get("transport"), "subprocess")
            self.assertEqual(shared_state.get("dynamic_plan", {}).get("inserted_tasks"), ["heading_structure_followup"])
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_names = {item.get("event") for item in events}
            self.assertIn("subprocess_worker_task_dispatched", event_names)
            self.assertIn("subprocess_worker_task_completed", event_names)

    def test_reviewer_dynamic_planning_queues_plan_approval_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="dynamic_planning",
                        title="Plan follow-up work",
                        task_type="dynamic_planning",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                    runtime.Task(
                        task_id="peer_challenge",
                        title="Challenge",
                        task_type="peer_challenge",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead", "reviewer_gamma"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set("heading_issues", [{"path": "a.md"}])
            shared_state.set("length_issues", [{"path": "b.md"}])
            shared_state.set("policies", {"teammate_plan_required": True})
            provider = mock.Mock()
            profile = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            session_state = registry.activate_for_run(profile=profile, transport="in-process")
            context = runtime.AgentContext(
                profile=profile,
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
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=threading.Event(),
                claim_tasks=True,
                handlers={"dynamic_planning": runtime.handle_dynamic_planning},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )
            claimed = board.claim_specific(
                task_id="dynamic_planning",
                agent_name=profile.name,
                agent_skills=profile.skills,
                agent_type=profile.agent_type,
            )
            self.assertIsNotNone(claimed)

            agent._run_task(claimed)

            board_snapshot = {item["task_id"]: item for item in board.snapshot().get("tasks", [])}
            self.assertEqual(board_snapshot["dynamic_planning"]["status"], "completed")
            self.assertNotIn("heading_structure_followup", board_snapshot)
            self.assertTrue(board_snapshot["dynamic_planning"]["result"].get("approval_required"))
            interaction = runtime.get_lead_interaction_state(shared_state)
            pending_request = interaction.get("plan_approval_requests", {}).get("dynamic_planning", {})
            self.assertEqual(pending_request.get("status"), runtime.PLAN_APPROVAL_STATUS_PENDING)
            self.assertEqual(
                set(pending_request.get("proposed_task_ids", [])),
                {"heading_structure_followup", "length_risk_followup"},
            )
            lead_mail = mailbox.pull("lead")
            self.assertTrue(
                any(message.subject == "plan_approval_requested" for message in lead_mail),
            )

    def test_reviewer_llm_synthesis_subprocess_passes_model_config_and_updates_shared_state(self) -> None:
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
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                    runtime.Task(
                        task_id="llm_synthesis",
                        title="Synthesize findings",
                        task_type="llm_synthesis",
                        required_skills={"llm"},
                        dependencies=["heading_audit", "length_audit"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            heading_task = board.claim_specific(
                task_id="heading_audit",
                agent_name="analyst_alpha",
                agent_skills={"analysis"},
                agent_type="analyst",
            )
            length_task = board.claim_specific(
                task_id="length_audit",
                agent_name="analyst_beta",
                agent_skills={"analysis"},
                agent_type="analyst",
            )
            self.assertIsNotNone(heading_task)
            self.assertIsNotNone(length_task)
            board.complete("heading_audit", owner="analyst_alpha", result={"files_without_headings": 1})
            board.complete("length_audit", owner="analyst_beta", result={"long_files": 1})
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
            )
            shared_state = runtime.SharedState()
            shared_state.set(
                "agent_team_config",
                runtime.build_agent_team_config(
                    runtime_config=runtime.RuntimeConfig(teammate_mode="subprocess"),
                    host_kind="generic-cli",
                    provider_name="heuristic",
                    model="heuristic-v1",
                    openai_api_key_env="OPENAI_API_KEY",
                    openai_base_url="https://api.openai.com/v1",
                    require_llm=False,
                    provider_timeout_sec=5,
                    workflow_pack="markdown-audit",
                ).to_dict(),
            )
            shared_state.set("workflow", {"pack": "markdown-audit", "preset": "default", "options": {}})
            shared_state.set("heading_issues", [{"path": "docs/a.md"}])
            shared_state.set("length_issues", [{"path": "docs/b.md", "line_count": 240}])
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            profile = runtime.AgentProfile(
                name="reviewer_gamma",
                skills={"review", "writer", "llm"},
                agent_type="reviewer",
            )
            session_state = registry.ensure_profile(profile=profile, transport="in-process", status="ready")
            context = runtime.AgentContext(
                profile=profile,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="subprocess"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            agent = runtime.InProcessTeammateAgent(
                context=context,
                stop_event=threading.Event(),
                claim_tasks=True,
                handlers={"llm_synthesis": runtime.handle_llm_synthesis},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )
            claimed = board.claim_specific(
                task_id="llm_synthesis",
                agent_name=profile.name,
                agent_skills=profile.skills,
                agent_type=profile.agent_type,
            )
            self.assertIsNotNone(claimed)
            fake_execution = {
                "ok": True,
                "transport": "subprocess",
                "payload": {
                    "result": {
                        "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                        "preview": "Priority actions: fix headings first",
                    },
                    "state_updates": {
                        "llm_synthesis": {
                            "provider": {"provider": "heuristic", "model": "heuristic-v1", "mode": "local"},
                            "content": "Priority actions: fix headings first",
                        }
                    },
                },
                "diagnostics": {
                    "tmux_session_workspace_root": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer"),
                    "tmux_session_workspace_workdir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "target_snapshot"),
                    "tmux_session_workspace_home_dir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "home"),
                    "tmux_session_workspace_target_dir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "target_snapshot"),
                    "tmux_session_workspace_tmp_dir": str(output_dir / "_tmux_session_workspaces" / "reviewer_gamma" / "session-reviewer" / "tmp"),
                    "tmux_session_workspace_scope": "tmux_session_workspace",
                    "tmux_session_workspace_isolated": True,
                },
            }
            with mock.patch.object(runtime.tmux_transport, "run_tmux_worker_task", return_value=fake_execution) as run_worker:
                agent._run_task(claimed)

            dispatched_payload = run_worker.call_args.kwargs["payload"]
            self.assertEqual(dispatched_payload.get("model_config", {}).get("provider_name"), "heuristic")
            self.assertEqual(dispatched_payload.get("model_config", {}).get("model"), "heuristic-v1")
            llm_task = next(item for item in board.snapshot()["tasks"] if item["task_id"] == "llm_synthesis")
            self.assertEqual(llm_task["status"], "completed")
            session = registry.session_for("reviewer_gamma")
            self.assertTrue(session.get("workspace_isolation_active"))
            self.assertEqual(session.get("task_history", [])[-1].get("task_type"), "llm_synthesis")
            self.assertEqual(session.get("task_history", [])[-1].get("transport"), "subprocess")
            self.assertEqual(
                shared_state.get("llm_synthesis", {}).get("provider", {}).get("provider"),
                "heuristic",
            )
            self.assertIn(
                "Priority actions: fix headings first",
                shared_state.get("llm_synthesis", {}).get("content", ""),
            )


    def test_build_host_enforcement_snapshot_marks_subprocess_mode_transport_managed(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "codex",
                "session_transport": "tooling-session",
                "capabilities": {
                    "independent_sessions": False,
                    "workspace_isolation": False,
                    "auto_context_files": True,
                },
                "limits": ["session_isolation_emulated"],
            },
        )
        shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="subprocess").to_dict())
        shared_state.set("policies", {"allow_host_managed_context": True})

        snapshot = runtime.build_host_enforcement_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_enforcement"], "transport_managed")
        self.assertEqual(snapshot["workspace_enforcement"], "transport_managed")
        self.assertFalse(snapshot["host_native_session_active"])
        self.assertIn("subprocess_transport_manages_session_boundaries", snapshot["notes"])
        self.assertIn("transport_isolation_partial_to_selected_worker_tasks", snapshot["notes"])

    def test_build_host_enforcement_snapshot_defaults_to_runtime_managed_when_host_only_advertises_capabilities(
        self,
    ) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                    "auto_context_files": True,
                },
                "limits": [],
            },
        )
        shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="in-process").to_dict())
        shared_state.set("policies", {"allow_host_managed_context": True})

        snapshot = runtime.build_host_enforcement_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_enforcement"], "runtime_managed")
        self.assertEqual(snapshot["workspace_enforcement"], "runtime_managed")
        self.assertFalse(snapshot["host_native_session_active"])
        self.assertFalse(snapshot["host_native_workspace_active"])
        self.assertIn("host_independent_sessions_advertised_only", snapshot["notes"])
        self.assertIn("host_workspace_isolation_advertised_only", snapshot["notes"])
        self.assertIn("host_managed_context_not_bound_to_runtime", snapshot["notes"])

    def test_build_host_runtime_metadata_records_claude_environment_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_dir = pathlib.Path(tmp)
            claudecode_dir = home_dir / ".claudecode"
            claudecode_dir.mkdir(parents=True, exist_ok=True)
            (claudecode_dir / "relay").write_text(
                json.dumps({"host": "relay07.example.com"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (home_dir / ".claude.json").write_text(
                json.dumps({"hasAvailableSubscription": False}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            with mock.patch("agent_team.host.pathlib.Path.home", return_value=home_dir):
                with mock.patch("agent_team.host.shutil.which", return_value="C:\\tools\\claude.cmd"):
                    with mock.patch.dict("agent_team.host.os.environ", {}, clear=True):
                        metadata = build_host_adapter(
                            runtime.default_host_config("claude-code")
                        ).runtime_metadata()

        environment = metadata.get("environment", {})
        self.assertEqual(environment.get("kind"), "claude-code")
        self.assertTrue(environment.get("cli_installed"))
        self.assertEqual(environment.get("relay_host"), "relay07.example.com")
        self.assertEqual(environment.get("relay_host_normalized"), "relay07.example.com")
        self.assertEqual(environment.get("relay_source"), "relay_file")
        self.assertFalse(environment.get("official_relay_active"))
        self.assertFalse(environment.get("subscription_available"))
        self.assertFalse(environment.get("native_session_prerequisites_ready"))
        self.assertEqual(
            environment.get("native_session_prerequisite_reason"),
            "unsupported_relay",
        )
        self.assertIn("claude_code_prerequisites_unsupported_relay", metadata.get("limits", []))

    def test_build_host_runtime_metadata_blocks_third_party_relay_even_with_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_dir = pathlib.Path(tmp)
            claudecode_dir = home_dir / ".claudecode"
            claudecode_dir.mkdir(parents=True, exist_ok=True)
            (claudecode_dir / "relay").write_text(
                json.dumps({"host": "relay07.example.com"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (home_dir / ".claude.json").write_text(
                json.dumps({"hasAvailableSubscription": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            with mock.patch("agent_team.host.pathlib.Path.home", return_value=home_dir):
                with mock.patch("agent_team.host.shutil.which", return_value="C:\\tools\\claude.cmd"):
                    with mock.patch.dict("agent_team.host.os.environ", {}, clear=True):
                        metadata = build_host_adapter(
                            runtime.default_host_config("claude-code")
                        ).runtime_metadata()

        environment = metadata.get("environment", {})
        self.assertTrue(environment.get("subscription_available"))
        self.assertFalse(environment.get("official_relay_active"))
        self.assertFalse(environment.get("native_session_prerequisites_ready"))
        self.assertEqual(environment.get("native_session_prerequisite_reason"), "unsupported_relay")
        self.assertIn("claude_code_prerequisites_unsupported_relay", metadata.get("limits", []))

    def test_host_session_backend_metadata_selects_claude_exec_when_prerequisites_ready(self) -> None:
        with mock.patch.object(
            host_transport,
            "probe_host_environment",
            return_value={
                "kind": "claude-code",
                "native_session_prerequisites_ready": True,
                "native_session_prerequisite_reason": "subscription_available",
            },
        ):
            metadata = host_transport.host_session_backend_metadata(host_kind="claude-code")

        self.assertEqual(metadata.get("backend"), host_transport.HOST_SESSION_BACKEND_CLAUDE_EXEC)
        self.assertEqual(metadata.get("source"), "host")
        self.assertTrue(metadata.get("host_managed"))
        self.assertTrue(metadata.get("session_isolation_active"))

    def test_parse_claude_stream_output_extracts_session_id_and_result(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": "claude-session-123",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "session_id": "claude-session-123",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": '{"status":"ok","result_path":"C:/tmp/from-assistant.json"}',
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "claude-session-123",
                        "result": '{"status":"ok","result_path":"C:/tmp/final.json"}',
                    },
                    ensure_ascii=False,
                ),
            ]
        )

        parsed = host_transport._parse_claude_stream_output(stdout)

        self.assertEqual(parsed.get("session_id"), "claude-session-123")
        self.assertEqual(parsed.get("assistant_text"), '{"status":"ok","result_path":"C:/tmp/from-assistant.json"}')
        self.assertEqual(parsed.get("result_text"), '{"status":"ok","result_path":"C:/tmp/final.json"}')

    def test_spawn_host_session_worker_selects_claude_backend_when_ready(self) -> None:
        lead_context = mock.Mock()
        lead_context.shared_state = runtime.SharedState()
        lead_context.shared_state.set("host", {"kind": "claude-code"})
        profile = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")

        with mock.patch.object(
            host_transport,
            "host_session_backend_metadata",
            return_value={"backend": host_transport.HOST_SESSION_BACKEND_CLAUDE_EXEC},
        ):
            with mock.patch.object(
                host_transport,
                "_spawn_claude_host_session_worker",
                return_value="claude-worker",
            ):
                with mock.patch.object(
                    host_transport,
                    "_spawn_codex_host_session_worker",
                    return_value="codex-worker",
                ):
                    with mock.patch.object(
                        host_transport,
                        "_spawn_external_process_host_session_worker",
                        return_value="external-worker",
                    ):
                        worker = host_transport._spawn_host_session_worker(
                            lead_context=lead_context,
                            profile=profile,
                        )

        self.assertEqual(worker, "claude-worker")

    def test_build_host_enforcement_snapshot_includes_claude_environment_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_dir = pathlib.Path(tmp)
            claudecode_dir = home_dir / ".claudecode"
            claudecode_dir.mkdir(parents=True, exist_ok=True)
            (home_dir / ".claude.json").write_text(
                json.dumps({"hasAvailableSubscription": False}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_config = runtime.RuntimeConfig(teammate_mode="host")
            adapter = build_host_adapter(runtime.default_host_config("claude-code"))

            with mock.patch("agent_team.host.pathlib.Path.home", return_value=home_dir):
                with mock.patch("agent_team.host.shutil.which", return_value="C:\\tools\\claude.cmd"):
                    with mock.patch.dict("agent_team.host.os.environ", {}, clear=True):
                        host_metadata = adapter.runtime_metadata()
                        host_enforcement = adapter.runtime_enforcement(
                            runtime_config=runtime_config,
                            policies={"allow_host_managed_context": True},
                        )

            shared_state = runtime.SharedState()
            shared_state.set("host", host_metadata)
            shared_state.set("runtime_config", runtime_config.to_dict())
            shared_state.set("policies", {"allow_host_managed_context": True})
            shared_state.set("host_runtime_enforcement", host_enforcement)

            snapshot = runtime.build_host_enforcement_snapshot(shared_state=shared_state)

        environment = snapshot.get("host", {}).get("environment", {})
        self.assertEqual(environment.get("kind"), "claude-code")
        self.assertEqual(environment.get("relay_host"), "gaccode.com")
        self.assertEqual(environment.get("relay_source"), "canonical_default")
        self.assertTrue(environment.get("official_relay_active"))
        self.assertFalse(environment.get("native_session_prerequisites_ready"))
        self.assertEqual(
            environment.get("native_session_prerequisite_reason"),
            "subscription_unavailable",
        )
        self.assertIn("claude_code_canonical_relay_defaulted", snapshot["notes"])
        self.assertIn("claude_code_official_relay_active", snapshot["notes"])
        self.assertIn("claude_code_prerequisites_subscription_unavailable", snapshot["notes"])

    def test_build_host_enforcement_snapshot_preserves_claude_host_backend_as_host_managed(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                    "auto_context_files": True,
                },
                "limits": [],
            },
        )
        shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
        shared_state.set("policies", {"allow_host_managed_context": True})
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "claude-code",
                "configured_session_transport": "session",
                "requested_teammate_mode": "host",
                "session_enforcement": "host_managed",
                "workspace_enforcement": "runtime_managed",
                "host_native_session_active": True,
                "host_native_workspace_active": False,
                "host_managed_context_requested": True,
                "host_managed_context_active": True,
                "effective_boundary_source": "host",
                "effective_boundary_strength": "strong",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                    "auto_context_files": True,
                },
                "limits": [],
                "notes": ["host_transport_manages_session_boundaries"],
                "host_session_backend": "claude_exec",
                "host_session_backend_source": "host",
                "host_session_backend_host_managed": True,
                "host_session_backend_session_isolation_active": True,
                "host_session_backend_workspace_isolation_active": False,
            },
        )

        snapshot = runtime.build_host_enforcement_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_enforcement"], "host_managed")
        self.assertEqual(snapshot["workspace_enforcement"], "runtime_managed")
        self.assertTrue(snapshot["host_native_session_active"])
        self.assertFalse(snapshot["host_native_workspace_active"])
        self.assertTrue(snapshot["host_managed_context_active"])
        self.assertEqual(snapshot["host_session_backend"], "claude_exec")
        self.assertEqual(snapshot["host_session_backend_source"], "host")
        self.assertTrue(snapshot["host_session_backend_host_managed"])
        self.assertIn("host_session_backend_claude_exec", snapshot["notes"])
        self.assertIn("host_transport_manages_session_boundaries", snapshot["notes"])

    def test_build_host_enforcement_snapshot_downgrades_external_process_host_backend(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                    "auto_context_files": True,
                },
                "limits": [],
            },
        )
        shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
        shared_state.set("policies", {"allow_host_managed_context": True})
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "claude-code",
                "configured_session_transport": "session",
                "requested_teammate_mode": "host",
                "session_enforcement": "host_managed",
                "workspace_enforcement": "host_managed",
                "host_native_session_active": True,
                "host_native_workspace_active": True,
                "host_managed_context_requested": True,
                "host_managed_context_active": True,
                "effective_boundary_source": "host",
                "effective_boundary_strength": "strong",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                    "auto_context_files": True,
                },
                "limits": [],
                "notes": ["host_transport_manages_session_boundaries"],
                "host_session_backend": "external_process",
                "host_session_backend_source": "transport",
                "host_session_backend_session_isolation_active": True,
                "host_session_backend_workspace_isolation_active": False,
            },
        )

        snapshot = runtime.build_host_enforcement_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_enforcement"], "transport_managed")
        self.assertEqual(snapshot["workspace_enforcement"], "runtime_managed")
        self.assertFalse(snapshot["host_native_session_active"])
        self.assertFalse(snapshot["host_native_workspace_active"])
        self.assertEqual(snapshot["host_session_backend"], "external_process")
        self.assertIn("host_session_backend_external_process", snapshot["notes"])
        self.assertIn("requested_host_sessions_backed_by_transport_process", snapshot["notes"])

    def test_build_host_enforcement_snapshot_preserves_codex_host_backend_as_host_managed(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "codex",
                "session_transport": "tooling-session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": False,
                    "auto_context_files": True,
                },
                "limits": [],
            },
        )
        shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
        shared_state.set("policies", {"allow_host_managed_context": True})
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "codex",
                "configured_session_transport": "tooling-session",
                "requested_teammate_mode": "host",
                "session_enforcement": "host_managed",
                "workspace_enforcement": "runtime_managed",
                "host_native_session_active": True,
                "host_native_workspace_active": False,
                "host_managed_context_requested": True,
                "host_managed_context_active": True,
                "effective_boundary_source": "host",
                "effective_boundary_strength": "strong",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": False,
                    "auto_context_files": True,
                },
                "limits": [],
                "notes": ["host_transport_manages_session_boundaries"],
                "host_session_backend": "codex_exec",
                "host_session_backend_source": "host",
                "host_session_backend_host_managed": True,
                "host_session_backend_session_isolation_active": True,
                "host_session_backend_workspace_isolation_active": False,
            },
        )

        snapshot = runtime.build_host_enforcement_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["session_enforcement"], "host_managed")
        self.assertEqual(snapshot["workspace_enforcement"], "runtime_managed")
        self.assertTrue(snapshot["host_native_session_active"])
        self.assertFalse(snapshot["host_native_workspace_active"])
        self.assertTrue(snapshot["host_managed_context_active"])
        self.assertEqual(snapshot["host_session_backend"], "codex_exec")
        self.assertEqual(snapshot["host_session_backend_source"], "host")
        self.assertTrue(snapshot["host_session_backend_host_managed"])
        self.assertIn("host_session_backend_codex_exec", snapshot["notes"])
        self.assertIn("host_transport_manages_session_boundaries", snapshot["notes"])

    def test_build_session_boundary_snapshot_marks_host_external_process_sessions_as_transport_backed(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
            },
        )
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "claude-code",
                "configured_session_transport": "session",
                "requested_teammate_mode": "host",
                "session_enforcement": "transport_managed",
                "workspace_enforcement": "runtime_managed",
                "host_native_session_active": False,
                "host_native_workspace_active": False,
                "host_managed_context_requested": True,
                "host_managed_context_active": False,
                "effective_boundary_source": "transport",
                "effective_boundary_strength": "medium",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
                "notes": [
                    "host_session_backend_external_process",
                    "requested_host_sessions_backed_by_transport_process",
                ],
                "host_session_backend": "external_process",
                "host_session_backend_source": "transport",
                "host_session_backend_session_isolation_active": True,
                "host_session_backend_workspace_isolation_active": False,
            },
        )
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
        registry.ensure_profile(profile=reviewer, transport="host", status="ready")
        registry.record_boundary(
            agent_name="reviewer_gamma",
            transport="host",
            transport_session_name="claude-code:reviewer_gamma",
            transport_backend="external_process",
            workspace_isolation_active=False,
        )

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["boundary_mode_counts"]["worker_subprocess_session"], 1)
        session = snapshot["sessions"][0]
        self.assertEqual(session["transport"], "host")
        self.assertEqual(session["transport_backend"], "external_process")
        self.assertEqual(session["boundary_mode"], "worker_subprocess_session")
        self.assertIn("session_isolation_backed_by_host_external_process", session["notes"])

    def test_build_session_boundary_snapshot_treats_codex_host_backend_as_host_native(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "codex",
                "session_transport": "tooling-session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": False,
                },
                "limits": [],
            },
        )
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "codex",
                "configured_session_transport": "tooling-session",
                "requested_teammate_mode": "host",
                "session_enforcement": "host_managed",
                "workspace_enforcement": "runtime_managed",
                "host_native_session_active": True,
                "host_native_workspace_active": False,
                "host_managed_context_requested": True,
                "host_managed_context_active": False,
                "effective_boundary_source": "host",
                "effective_boundary_strength": "strong",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": False,
                },
                "limits": [],
                "notes": ["host_transport_manages_session_boundaries"],
                "host_session_backend": "codex_exec",
                "host_session_backend_source": "host",
                "host_session_backend_host_managed": True,
                "host_session_backend_session_isolation_active": True,
                "host_session_backend_workspace_isolation_active": False,
            },
        )
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        registry.ensure_profile(profile=analyst, transport="host", status="ready")
        registry.apply_telemetry(
            {
                "agent": "analyst_alpha",
                "agent_type": "analyst",
                "skills": ["analysis"],
                "event_type": "status",
                "transport": "host",
                "transport_backend": "codex_exec",
                "transport_session_name": "codex:analyst_alpha",
                "session_id": "thread-codex-123",
                "status": "ready",
            }
        )

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["boundary_mode_counts"]["host_native_session"], 1)
        session = snapshot["sessions"][0]
        self.assertEqual(session["transport_backend"], "codex_exec")
        self.assertEqual(session["session_id"], "thread-codex-123")
        self.assertEqual(session["transport_session_name"], "codex:analyst_alpha")
        self.assertEqual(session["boundary_mode"], "host_native_session")
        self.assertIn("session_isolation_backed_by_host_transport", session["notes"])

    def test_build_session_boundary_snapshot_treats_claude_host_backend_as_host_native(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
            },
        )
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "claude-code",
                "configured_session_transport": "session",
                "requested_teammate_mode": "host",
                "session_enforcement": "host_managed",
                "workspace_enforcement": "runtime_managed",
                "host_native_session_active": True,
                "host_native_workspace_active": False,
                "host_managed_context_requested": True,
                "host_managed_context_active": False,
                "effective_boundary_source": "host",
                "effective_boundary_strength": "strong",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
                "notes": ["host_transport_manages_session_boundaries"],
                "host_session_backend": "claude_exec",
                "host_session_backend_source": "host",
                "host_session_backend_host_managed": True,
                "host_session_backend_session_isolation_active": True,
                "host_session_backend_workspace_isolation_active": False,
            },
        )
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
        registry.ensure_profile(profile=reviewer, transport="host", status="ready")
        registry.apply_telemetry(
            {
                "agent": "reviewer_gamma",
                "agent_type": "reviewer",
                "skills": ["review"],
                "event_type": "status",
                "transport": "host",
                "transport_backend": "claude_exec",
                "transport_session_name": "claude-code:reviewer_gamma",
                "session_id": "claude-session-123",
                "status": "ready",
            }
        )

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["boundary_mode_counts"]["host_native_session"], 1)
        session = snapshot["sessions"][0]
        self.assertEqual(session["transport_backend"], "claude_exec")
        self.assertEqual(session["session_id"], "claude-session-123")
        self.assertEqual(session["transport_session_name"], "claude-code:reviewer_gamma")
        self.assertEqual(session["boundary_mode"], "host_native_session")
        self.assertIn("session_isolation_backed_by_host_transport", session["notes"])

    def test_build_session_boundary_snapshot_requires_active_host_enforcement_for_host_native_mode(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
            },
        )
        shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="in-process").to_dict())
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        registry.ensure_profile(profile=analyst, transport="in-process", status="ready")

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["boundary_mode_counts"]["runtime_emulated_session"], 1)
        session = snapshot["sessions"][0]
        self.assertEqual(session["host_session_enforcement"], "runtime_managed")
        self.assertFalse(session["host_native_session_active"])
        self.assertIn("host_independent_sessions_advertised_only", session["notes"])

    def test_build_session_boundary_snapshot_prefers_host_native_sessions(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set(
            "host",
            {
                "kind": "claude-code",
                "session_transport": "session",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
            },
        )
        shared_state.set(
            "host_runtime_enforcement",
            {
                "host_kind": "claude-code",
                "configured_session_transport": "session",
                "requested_teammate_mode": "host",
                "session_enforcement": "host_managed",
                "workspace_enforcement": "host_managed",
                "host_native_session_active": True,
                "host_native_workspace_active": True,
                "host_managed_context_requested": True,
                "host_managed_context_active": True,
                "effective_boundary_source": "host",
                "effective_boundary_strength": "strong",
                "capabilities": {
                    "independent_sessions": True,
                    "workspace_isolation": True,
                },
                "limits": [],
                "notes": ["host_transport_manages_session_boundaries"],
            },
        )
        registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
        analyst = runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst")
        registry.ensure_profile(profile=analyst, transport="host", status="ready")

        snapshot = runtime.build_session_boundary_snapshot(shared_state=shared_state)

        self.assertEqual(snapshot["boundary_mode_counts"]["host_native_session"], 1)
        self.assertEqual(snapshot["boundary_strength_counts"]["strong"], 1)

    def test_run_host_teammate_task_once_executes_task_and_records_host_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="host_review",
                        title="Host review",
                        task_type="host_review",
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
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                        "auto_context_files": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            shared_state.set("team_profiles", [{"name": "reviewer_gamma", "agent_type": "reviewer", "skills": ["review"]}])
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            registry.ensure_profile(profile=reviewer, transport="host", status="ready")
            observed: dict = {}
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host transport test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_registry=registry,
            )

            def _handler(context, task):
                observed["shared_mailbox_object"] = context.mailbox is lead_context.mailbox
                observed["worker_mailbox_storage_dir"] = str(context.mailbox.storage_dir)
                context.shared_state.set("host_result", {"task_id": task.task_id, "transport": "host"})
                return {"ok": True, "transport": "host"}

            ran = runtime.run_host_teammate_task_once(
                lead_context=lead_context,
                teammate_profiles=[reviewer],
                handlers={"host_review": _handler},
            )

            self.assertTrue(ran)
            task_snapshot = board.snapshot()["tasks"][0]
            self.assertEqual(task_snapshot["status"], "completed")
            self.assertEqual(shared_state.get("host_result", {}).get("transport"), "host")
            self.assertFalse(observed["shared_mailbox_object"])
            self.assertEqual(
                pathlib.Path(observed["worker_mailbox_storage_dir"]).resolve(),
                (output_dir / "_mailbox").resolve(),
            )
            session = registry.session_for("reviewer_gamma")
            self.assertEqual(session.get("transport"), "host")
            self.assertEqual(session.get("transport_backend"), "inprocess_thread")
            self.assertFalse(session.get("workspace_isolation_active"))
            self.assertEqual(session.get("transport_session_name"), "claude-code:reviewer_gamma")
            self.assertEqual(str(session.get("workspace_root", "") or ""), "")
            boundaries = runtime.build_session_boundary_snapshot(shared_state=shared_state)
            self.assertEqual(boundaries.get("boundary_mode_counts", {}).get("runtime_emulated_session", 0), 1)
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_names = {item.get("event") for item in events}
            self.assertIn("host_worker_task_dispatched", event_names)
            self.assertIn("host_worker_task_completed", event_names)
            dispatch_events = [item for item in events if item.get("event") == "host_worker_task_dispatched"]
            self.assertEqual(dispatch_events[-1].get("execution_mode"), "inline")

    def test_host_mailbox_reviewer_tasks_dispatch_to_session_thread(self) -> None:
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
                    ),
                    runtime.Task(
                        task_id="evidence_pack",
                        title="Evidence pack",
                        task_type="evidence_pack",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            shared_state.set(
                "team_profiles",
                [{"name": "reviewer_gamma", "agent_type": "reviewer", "skills": ["review"]}],
            )
            file_locks = runtime.FileLockRegistry(logger=logger)
            worker_file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            session_state = registry.activate_for_run(profile=reviewer, transport="host")
            stop_event = threading.Event()
            started = threading.Event()
            release = threading.Event()
            execution_threads: list = []

            def _peer_handler(context, task):
                execution_threads.append((task.task_id, threading.current_thread().name))
                started.set()
                self.assertIsNot(context.mailbox, mailbox)
                self.assertEqual(context.profile.name, "reviewer_gamma")
                context.shared_state.set(
                    "peer_challenge_record",
                    {"task_id": task.task_id, "mode": "session_thread"},
                )
                self.assertTrue(release.wait(timeout=2.0))
                return {"ok": True, "task_id": task.task_id}

            def _evidence_handler(context, task):
                execution_threads.append((task.task_id, threading.current_thread().name))
                self.assertIsNot(context.mailbox, mailbox)
                context.shared_state.set(
                    "evidence_pack_record",
                    {"task_id": task.task_id, "mode": "session_thread"},
                )
                return {"ok": True, "task_id": task.task_id}

            worker_context = runtime.AgentContext(
                profile=reviewer,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host mailbox task test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox.transport_view(),
                file_locks=worker_file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            worker = runtime.InProcessTeammateAgent(
                context=worker_context,
                stop_event=stop_event,
                claim_tasks=False,
                handlers={
                    "peer_challenge": _peer_handler,
                    "evidence_pack": _evidence_handler,
                },
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host mailbox task test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_registry=registry,
            )
            setattr(lead_context, "_host_worker_threads", {"reviewer_gamma": worker})

            worker.start()
            try:
                first_dispatch = runtime.run_host_teammate_task_once(
                    lead_context=lead_context,
                    teammate_profiles=[reviewer],
                    handlers={
                        "peer_challenge": _peer_handler,
                        "evidence_pack": _evidence_handler,
                    },
                )
                self.assertTrue(first_dispatch)
                self.assertTrue(started.wait(timeout=1.0))
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["peer_challenge"]["status"], "in_progress")
                self.assertEqual(board_snapshot["evidence_pack"]["status"], "pending")

                second_dispatch = runtime.run_host_teammate_task_once(
                    lead_context=lead_context,
                    teammate_profiles=[reviewer],
                    handlers={
                        "peer_challenge": _peer_handler,
                        "evidence_pack": _evidence_handler,
                    },
                )
                self.assertFalse(second_dispatch)
                session = registry.session_for("reviewer_gamma")
                self.assertEqual(session.get("current_task_id"), "peer_challenge")
                self.assertEqual(session.get("tasks_started"), 1)

                release.set()
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    events = [
                        json.loads(line)
                        for line in logger.path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if any(
                        item.get("event") == "assigned_task_result_published"
                        and item.get("task_id") == "peer_challenge"
                        for item in events
                    ):
                        break
                    time.sleep(0.05)
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["peer_challenge"]["status"], "in_progress")
                self.assertEqual(shared_state.get("peer_challenge_record"), None)
                runtime.apply_host_session_telemetry_messages(lead_context)
                self.assertEqual(runtime.apply_host_session_result_messages(lead_context), 1)
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["peer_challenge"]["status"], "completed")
                self.assertEqual(
                    shared_state.get("peer_challenge_record", {}).get("mode"),
                    "session_thread",
                )
                session = registry.session_for("reviewer_gamma")
                self.assertEqual(session.get("tasks_completed"), 1)

                third_dispatch = runtime.run_host_teammate_task_once(
                    lead_context=lead_context,
                    teammate_profiles=[reviewer],
                    handlers={
                        "peer_challenge": _peer_handler,
                        "evidence_pack": _evidence_handler,
                    },
                )
                self.assertTrue(third_dispatch)

                deadline = time.time() + 2.0
                while time.time() < deadline:
                    events = [
                        json.loads(line)
                        for line in logger.path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if any(
                        item.get("event") == "assigned_task_result_published"
                        and item.get("task_id") == "evidence_pack"
                        for item in events
                    ):
                        break
                    time.sleep(0.05)
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["evidence_pack"]["status"], "in_progress")
                self.assertEqual(shared_state.get("evidence_pack_record"), None)
                runtime.apply_host_session_telemetry_messages(lead_context)
                self.assertEqual(runtime.apply_host_session_result_messages(lead_context), 1)
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["evidence_pack"]["status"], "completed")
                self.assertEqual(
                    shared_state.get("evidence_pack_record", {}).get("mode"),
                    "session_thread",
                )
                session = registry.session_for("reviewer_gamma")
                history_task_types = [item.get("task_type") for item in session.get("task_history", [])]
                self.assertIn("peer_challenge", history_task_types)
                self.assertIn("evidence_pack", history_task_types)
            finally:
                stop_event.set()
                worker.join(timeout=2.0)

            self.assertEqual(
                execution_threads,
                [
                    ("peer_challenge", "reviewer_gamma"),
                    ("evidence_pack", "reviewer_gamma"),
                ],
            )
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            dispatch_events = [
                item for item in events
                if item.get("event") == "host_worker_task_dispatched"
            ]
            self.assertEqual(len(dispatch_events), 2)
            self.assertTrue(
                all(item.get("execution_mode") == "session_thread" for item in dispatch_events),
            )
            assignment_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_ASSIGNMENT_SUBJECT
            ]
            self.assertEqual(len(assignment_messages), 2)
            self.assertEqual(
                {item.get("task_id") for item in assignment_messages},
                {"peer_challenge", "evidence_pack"},
            )
            assignment_receipts = [
                item
                for item in events
                if item.get("event") == "assigned_task_message_received"
            ]
            self.assertEqual(len(assignment_receipts), 2)
            self.assertEqual(
                {item.get("task_id") for item in assignment_receipts},
                {"peer_challenge", "evidence_pack"},
            )
            result_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_RESULT_SUBJECT
            ]
            self.assertEqual(len(result_messages), 2)
            self.assertEqual(
                {item.get("task_id") for item in result_messages},
                {"peer_challenge", "evidence_pack"},
            )
            telemetry_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TELEMETRY_SUBJECT
            ]
            self.assertGreaterEqual(len(telemetry_messages), 4)
            completion_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_completed"
                and item.get("execution_mode") == "session_thread"
            ]
            self.assertEqual(len(completion_events), 2)
            self.assertTrue(
                all(item.get("completion_contract") == "mailbox_message" for item in completion_events),
            )

    def test_host_mailbox_reviewer_failure_result_is_applied_by_lead(self) -> None:
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
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            shared_state.set(
                "team_profiles",
                [{"name": "reviewer_gamma", "agent_type": "reviewer", "skills": ["review"]}],
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
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")
            session_state = registry.activate_for_run(profile=reviewer, transport="host")
            stop_event = threading.Event()
            worker_context = runtime.AgentContext(
                profile=reviewer,
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host mailbox failure test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox.transport_view(),
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )

            def _failing_handler(_context, _task):
                raise RuntimeError("boom")

            worker = runtime.InProcessTeammateAgent(
                context=worker_context,
                stop_event=stop_event,
                claim_tasks=False,
                handlers={"peer_challenge": _failing_handler},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host mailbox failure test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_registry=registry,
            )
            setattr(lead_context, "_host_worker_threads", {"reviewer_gamma": worker})

            worker.start()
            try:
                dispatched = runtime.run_host_teammate_task_once(
                    lead_context=lead_context,
                    teammate_profiles=[reviewer],
                    handlers={"peer_challenge": _failing_handler},
                )
                self.assertTrue(dispatched)
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    events = [
                        json.loads(line)
                        for line in logger.path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if any(
                        item.get("event") == "assigned_task_result_published"
                        and item.get("task_id") == "peer_challenge"
                        for item in events
                    ):
                        break
                    time.sleep(0.05)
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["peer_challenge"]["status"], "in_progress")
                runtime.apply_host_session_telemetry_messages(lead_context)
                self.assertEqual(runtime.apply_host_session_result_messages(lead_context), 1)
                board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                self.assertEqual(board_snapshot["peer_challenge"]["status"], "failed")
                self.assertIn("RuntimeError: boom", board_snapshot["peer_challenge"]["error"])
                session = registry.session_for("reviewer_gamma")
                self.assertEqual(session.get("tasks_failed"), 1)
            finally:
                stop_event.set()
                worker.join(timeout=2.0)

            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            result_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_RESULT_SUBJECT
            ]
            self.assertEqual(len(result_messages), 1)
            telemetry_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TELEMETRY_SUBJECT
            ]
            self.assertGreaterEqual(len(telemetry_messages), 2)
            failure_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_failed"
                and item.get("execution_mode") == "session_thread"
            ]
            self.assertEqual(len(failure_events), 1)
            self.assertEqual(failure_events[0].get("completion_contract"), "mailbox_message")

    def test_host_recommendation_pack_dispatches_to_session_thread_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            report_path = (output_dir / "final_report.md").resolve()
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="recommendation_pack",
                        title="Write recommendation report",
                        task_type="recommendation_pack",
                        required_skills={"review", "writer"},
                        dependencies=[],
                        payload={},
                        locked_paths=[str(report_path)],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            shared_state.set(
                "team_profiles",
                [{"name": "reviewer_gamma", "agent_type": "reviewer", "skills": ["review", "writer"]}],
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
            reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review", "writer"}, agent_type="reviewer")

            class _FakeHostWorker:
                worker_backend = "external_process"

                def __init__(self) -> None:
                    self.assigned_task_id = ""

                def can_accept_assigned_task(self) -> bool:
                    return not self.assigned_task_id

                def reserve_assigned_task(self, task_id: str) -> bool:
                    if self.assigned_task_id:
                        return False
                    self.assigned_task_id = str(task_id)
                    return True

                def release_assigned_task(self, task_id: str = "") -> None:
                    if task_id and self.assigned_task_id and self.assigned_task_id != str(task_id):
                        return
                    self.assigned_task_id = ""

            worker = _FakeHostWorker()
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host report task test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
            )
            setattr(lead_context, "_host_worker_threads", {"reviewer_gamma": worker})

            dispatched = runtime.run_host_teammate_task_once(
                lead_context=lead_context,
                teammate_profiles=[reviewer],
                handlers={"recommendation_pack": lambda _context, _task: {"report_path": str(report_path)}},
            )
            self.assertTrue(dispatched)

            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertEqual(board_snapshot["recommendation_pack"]["status"], "in_progress")
            self.assertEqual(file_locks.snapshot().get(str(report_path)), "reviewer_gamma")
            self.assertEqual(worker.assigned_task_id, "recommendation_pack")

            mailbox.send(
                sender="reviewer_gamma",
                recipient="lead",
                subject=runtime.SESSION_TASK_RESULT_SUBJECT,
                body=json.dumps(
                    {
                        "contract": "session_task_result",
                        "contract_version": 1,
                        "transport": "host",
                        "execution_mode": "session_thread",
                        "task_id": "recommendation_pack",
                        "task_type": "recommendation_pack",
                        "worker": "reviewer_gamma",
                        "success": True,
                        "result": {"report_path": str(report_path)},
                        "error": "",
                        "state_updates": {},
                    },
                    ensure_ascii=False,
                ),
                task_id="recommendation_pack",
            )
            self.assertEqual(runtime.apply_host_session_result_messages(lead_context), 1)

            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertEqual(board_snapshot["recommendation_pack"]["status"], "completed")
            self.assertEqual(
                pathlib.Path(str(board_snapshot["recommendation_pack"]["result"]["report_path"])).resolve(),
                report_path,
            )
            self.assertEqual(file_locks.snapshot(), {})
            self.assertEqual(worker.assigned_task_id, "")

            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            dispatch_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_dispatched"
                and item.get("task_id") == "recommendation_pack"
            ]
            self.assertEqual(len(dispatch_events), 1)
            self.assertEqual(dispatch_events[0].get("execution_mode"), "session_thread")
            completion_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_completed"
                and item.get("task_id") == "recommendation_pack"
            ]
            self.assertEqual(len(completion_events), 1)
            self.assertEqual(completion_events[0].get("execution_mode"), "session_thread")
            assignment_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_ASSIGNMENT_SUBJECT
                and item.get("task_id") == "recommendation_pack"
            ]
            self.assertEqual(len(assignment_messages), 1)
            result_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_RESULT_SUBJECT
                and item.get("task_id") == "recommendation_pack"
            ]
            self.assertGreaterEqual(len(result_messages), 1)

    def test_host_discover_markdown_session_worker_applies_state_updates_by_lead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "output"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# Title\nbody\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")

            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="discover_markdown",
                        title="Scan markdown files",
                        task_type="discover_markdown",
                        required_skills={"inventory"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "analyst_alpha"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            shared_state.set(
                "team_profiles",
                [{"name": "analyst_alpha", "agent_type": "analyst", "skills": ["inventory", "analysis"]}],
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
            registry = runtime.TeammateSessionRegistry(shared_state=shared_state)
            analyst = runtime.AgentProfile(
                name="analyst_alpha",
                skills={"inventory", "analysis"},
                agent_type="analyst",
            )
            session_state = registry.activate_for_run(profile=analyst, transport="host")
            stop_event = threading.Event()
            worker_context = runtime.AgentContext(
                profile=analyst,
                target_dir=target_dir,
                output_dir=output_dir,
                goal="host analyst task test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox.transport_view(),
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_state=session_state,
                session_registry=registry,
            )
            worker = runtime.InProcessTeammateAgent(
                context=worker_context,
                stop_event=stop_event,
                claim_tasks=False,
                handlers={"discover_markdown": runtime.handle_discover_markdown},
                get_lead_name_fn=runtime.get_lead_name,
                profile_has_skill_fn=runtime.profile_has_skill,
                traceback_module=runtime.traceback,
            )
            setattr(worker, "worker_backend", "external_process")
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=target_dir,
                output_dir=output_dir,
                goal="host analyst task test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
                session_registry=registry,
            )
            setattr(lead_context, "_host_worker_threads", {"analyst_alpha": worker})

            worker.start()
            try:
                dispatched = runtime.run_host_teammate_task_once(
                    lead_context=lead_context,
                    teammate_profiles=[analyst],
                    handlers={"discover_markdown": runtime.handle_discover_markdown},
                )
                self.assertTrue(dispatched)

                deadline = time.time() + 2.0
                while time.time() < deadline:
                    runtime.apply_host_session_telemetry_messages(lead_context)
                    runtime.apply_host_session_result_messages(lead_context)
                    board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
                    if board_snapshot["discover_markdown"]["status"] == "completed":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("host discover_markdown task did not complete through session worker")
            finally:
                stop_event.set()
                worker.join(timeout=2.0)

            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertEqual(board_snapshot["discover_markdown"]["status"], "completed")
            self.assertEqual(board_snapshot["discover_markdown"]["result"]["markdown_files"], 2)
            markdown_inventory = shared_state.get("markdown_inventory", [])
            self.assertEqual(len(markdown_inventory), 2)
            self.assertEqual(
                {item.get("path") for item in markdown_inventory if isinstance(item, dict)},
                {"a.md", "b.md"},
            )
            session = registry.session_for("analyst_alpha")
            self.assertEqual(session.get("tasks_completed"), 1)

            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            dispatch_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_dispatched"
                and item.get("task_id") == "discover_markdown"
            ]
            self.assertEqual(len(dispatch_events), 1)
            self.assertEqual(dispatch_events[0].get("execution_mode"), "session_thread")
            self.assertEqual(dispatch_events[0].get("session_worker_backend"), "external_process")
            completion_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_completed"
                and item.get("task_id") == "discover_markdown"
            ]
            self.assertEqual(len(completion_events), 1)
            self.assertEqual(completion_events[0].get("execution_mode"), "session_thread")
            self.assertEqual(completion_events[0].get("session_worker_backend"), "external_process")
            assignment_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_ASSIGNMENT_SUBJECT
                and item.get("task_id") == "discover_markdown"
            ]
            self.assertEqual(len(assignment_messages), 1)
            result_messages = [
                item
                for item in events
                if item.get("event") == "mail_sent"
                and item.get("subject") == runtime.SESSION_TASK_RESULT_SUBJECT
                and item.get("task_id") == "discover_markdown"
            ]
            self.assertGreaterEqual(len(result_messages), 1)

    def test_host_dynamic_planning_result_applies_task_mutations_by_lead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="dynamic_planning",
                        title="Plan follow-up work",
                        task_type="dynamic_planning",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                    runtime.Task(
                        task_id="peer_challenge",
                        title="Challenge",
                        task_type="peer_challenge",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set("heading_issues", [{"path": "a.md"}])
            shared_state.set("length_issues", [{"path": "b.md", "line_count": 220}])
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            shared_state.set(
                "team_profiles",
                [{"name": "reviewer_gamma", "agent_type": "reviewer", "skills": ["review"]}],
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
            reviewer = runtime.AgentProfile(name="reviewer_gamma", skills={"review"}, agent_type="reviewer")

            class _FakeHostWorker:
                worker_backend = "external_process"

                def __init__(self) -> None:
                    self.assigned_task_id = ""

                def can_accept_assigned_task(self) -> bool:
                    return not self.assigned_task_id

                def reserve_assigned_task(self, task_id: str) -> bool:
                    if self.assigned_task_id:
                        return False
                    self.assigned_task_id = str(task_id)
                    return True

                def release_assigned_task(self, task_id: str = "") -> None:
                    if task_id and self.assigned_task_id and self.assigned_task_id != str(task_id):
                        return
                    self.assigned_task_id = ""

            worker = _FakeHostWorker()
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host dynamic planning test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
            )
            setattr(lead_context, "_host_worker_threads", {"reviewer_gamma": worker})

            dispatched = runtime.run_host_teammate_task_once(
                lead_context=lead_context,
                teammate_profiles=[reviewer],
                handlers={"dynamic_planning": runtime.handle_dynamic_planning},
            )
            self.assertTrue(dispatched)

            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertEqual(board_snapshot["dynamic_planning"]["status"], "in_progress")
            self.assertEqual(worker.assigned_task_id, "dynamic_planning")

            mailbox.send(
                sender="reviewer_gamma",
                recipient="lead",
                subject=runtime.SESSION_TASK_RESULT_SUBJECT,
                body=json.dumps(
                    {
                        "contract": "session_task_result",
                        "contract_version": 1,
                        "transport": "host",
                        "execution_mode": "session_thread",
                        "task_id": "dynamic_planning",
                        "task_type": "dynamic_planning",
                        "worker": "reviewer_gamma",
                        "success": True,
                        "result": {
                            "enabled": True,
                            "inserted_tasks": ["heading_structure_followup", "length_risk_followup"],
                            "peer_challenge_dependencies_added": ["heading_structure_followup", "length_risk_followup"],
                        },
                        "error": "",
                        "state_updates": {
                            "dynamic_plan": {
                                "enabled": True,
                                "inserted_tasks": ["heading_structure_followup", "length_risk_followup"],
                                "peer_challenge_dependencies_added": ["heading_structure_followup", "length_risk_followup"],
                            }
                        },
                        "task_mutations": {
                            "insert_tasks": [
                                {
                                    "task_id": "heading_structure_followup",
                                    "title": "Run heading structure follow-up audit",
                                    "task_type": "heading_structure_followup",
                                    "required_skills": ["analysis"],
                                    "dependencies": ["dynamic_planning"],
                                    "payload": {"top_n": 8},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["analyst"],
                                },
                                {
                                    "task_id": "length_risk_followup",
                                    "title": "Run length risk follow-up audit",
                                    "task_type": "length_risk_followup",
                                    "required_skills": ["analysis"],
                                    "dependencies": ["dynamic_planning"],
                                    "payload": {"line_threshold": 180, "top_n": 8},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["analyst"],
                                },
                            ],
                            "add_dependencies": [
                                {"task_id": "peer_challenge", "dependency_id": "heading_structure_followup"},
                                {"task_id": "peer_challenge", "dependency_id": "length_risk_followup"},
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                task_id="dynamic_planning",
            )
            self.assertEqual(runtime.apply_host_session_result_messages(lead_context), 1)

            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertEqual(board_snapshot["dynamic_planning"]["status"], "completed")
            self.assertIn("heading_structure_followup", board_snapshot)
            self.assertIn("length_risk_followup", board_snapshot)
            self.assertEqual(
                set(board_snapshot["peer_challenge"]["dependencies"]),
                {"heading_structure_followup", "length_risk_followup"},
            )
            self.assertEqual(
                shared_state.get("dynamic_plan", {}).get("inserted_tasks"),
                ["heading_structure_followup", "length_risk_followup"],
            )
            self.assertEqual(worker.assigned_task_id, "")

            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            completion_events = [
                item
                for item in events
                if item.get("event") == "host_worker_task_completed"
                and item.get("task_id") == "dynamic_planning"
            ]
            self.assertEqual(len(completion_events), 1)
            self.assertEqual(completion_events[0].get("execution_mode"), "session_thread")
            self.assertEqual(completion_events[0].get("insert_task_count"), 2)
            self.assertEqual(completion_events[0].get("add_dependency_count"), 2)

    def test_host_dynamic_planning_result_queues_plan_approval_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="dynamic_planning",
                        title="Plan follow-up work",
                        task_type="dynamic_planning",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                    runtime.Task(
                        task_id="peer_challenge",
                        title="Challenge",
                        task_type="peer_challenge",
                        required_skills={"review"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(
                participants=["lead", "reviewer_gamma"],
                logger=logger,
                storage_dir=output_dir / "_mailbox",
                clear_storage=True,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set("heading_issues", [{"path": "a.md"}])
            shared_state.set("length_issues", [{"path": "b.md", "line_count": 220}])
            shared_state.set("policies", {"teammate_plan_required": True})
            shared_state.set(
                "host",
                {
                    "kind": "claude-code",
                    "session_transport": "session",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "note": "",
                },
            )
            shared_state.set(
                "host_runtime_enforcement",
                {
                    "host_kind": "claude-code",
                    "configured_session_transport": "session",
                    "requested_teammate_mode": "host",
                    "session_enforcement": "host_managed",
                    "workspace_enforcement": "host_managed",
                    "host_native_session_active": True,
                    "host_native_workspace_active": True,
                    "host_managed_context_requested": True,
                    "host_managed_context_active": True,
                    "effective_boundary_source": "host",
                    "effective_boundary_strength": "strong",
                    "capabilities": {
                        "independent_sessions": True,
                        "workspace_isolation": True,
                    },
                    "limits": [],
                    "notes": ["host_transport_manages_session_boundaries"],
                },
            )
            shared_state.set("runtime_config", runtime.RuntimeConfig(teammate_mode="host").to_dict())
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )

            class _FakeHostWorker:
                worker_backend = "external_process"

                def __init__(self) -> None:
                    self.assigned_task_id = "dynamic_planning"

                def can_accept_assigned_task(self) -> bool:
                    return not self.assigned_task_id

                def reserve_assigned_task(self, task_id: str) -> bool:
                    if self.assigned_task_id:
                        return False
                    self.assigned_task_id = str(task_id)
                    return True

                def release_assigned_task(self, task_id: str = "") -> None:
                    if task_id and self.assigned_task_id and self.assigned_task_id != str(task_id):
                        return
                    self.assigned_task_id = ""

            worker = _FakeHostWorker()
            lead_context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="lead", skills={"lead"}, agent_type="lead"),
                target_dir=output_dir,
                output_dir=output_dir,
                goal="host dynamic planning approval test",
                provider=provider,
                runtime_config=runtime.RuntimeConfig(teammate_mode="host"),
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                runtime_script=pathlib.Path(runtime.__file__).resolve(),
            )
            setattr(lead_context, "_host_worker_threads", {"reviewer_gamma": worker})

            claimed = board.claim_specific(
                task_id="dynamic_planning",
                agent_name="reviewer_gamma",
                agent_skills={"review"},
                agent_type="reviewer",
            )
            self.assertIsNotNone(claimed)

            mailbox.send(
                sender="reviewer_gamma",
                recipient="lead",
                subject=runtime.SESSION_TASK_RESULT_SUBJECT,
                body=json.dumps(
                    {
                        "contract": "session_task_result",
                        "contract_version": 1,
                        "transport": "host",
                        "execution_mode": "session_thread",
                        "task_id": "dynamic_planning",
                        "task_type": "dynamic_planning",
                        "worker": "reviewer_gamma",
                        "success": True,
                        "result": {
                            "enabled": True,
                            "inserted_tasks": ["heading_structure_followup", "length_risk_followup"],
                            "peer_challenge_dependencies_added": ["heading_structure_followup", "length_risk_followup"],
                        },
                        "error": "",
                        "state_updates": {
                            "dynamic_plan": {
                                "enabled": True,
                                "inserted_tasks": ["heading_structure_followup", "length_risk_followup"],
                                "peer_challenge_dependencies_added": ["heading_structure_followup", "length_risk_followup"],
                            }
                        },
                        "task_mutations": {
                            "insert_tasks": [
                                {
                                    "task_id": "heading_structure_followup",
                                    "title": "Run heading structure follow-up audit",
                                    "task_type": "heading_structure_followup",
                                    "required_skills": ["analysis"],
                                    "dependencies": ["dynamic_planning"],
                                    "payload": {"top_n": 8},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["analyst"],
                                },
                                {
                                    "task_id": "length_risk_followup",
                                    "title": "Run length risk follow-up audit",
                                    "task_type": "length_risk_followup",
                                    "required_skills": ["analysis"],
                                    "dependencies": ["dynamic_planning"],
                                    "payload": {"line_threshold": 180, "top_n": 8},
                                    "locked_paths": [],
                                    "allowed_agent_types": ["analyst"],
                                },
                            ],
                            "add_dependencies": [
                                {"task_id": "peer_challenge", "dependency_id": "heading_structure_followup"},
                                {"task_id": "peer_challenge", "dependency_id": "length_risk_followup"},
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                task_id="dynamic_planning",
            )
            self.assertEqual(runtime.apply_host_session_result_messages(lead_context), 1)

            board_snapshot = {item["task_id"]: item for item in board.snapshot()["tasks"]}
            self.assertEqual(board_snapshot["dynamic_planning"]["status"], "completed")
            self.assertNotIn("heading_structure_followup", board_snapshot)
            self.assertTrue(board_snapshot["dynamic_planning"]["result"].get("approval_required"))
            interaction = runtime.get_lead_interaction_state(shared_state)
            pending_request = interaction.get("plan_approval_requests", {}).get("dynamic_planning", {})
            self.assertEqual(pending_request.get("status"), runtime.PLAN_APPROVAL_STATUS_PENDING)
            self.assertEqual(worker.assigned_task_id, "")


    def test_build_context_boundary_summary_rolls_up_prepared_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            logger.log(
                "task_context_prepared",
                agent="analyst_alpha",
                task_id="heading_audit",
                task_type="heading_audit",
                scope="task_scoped_shared_state_view",
                visible_shared_state_keys=["lead_name", "markdown_inventory"],
                visible_shared_state_key_count=2,
                omitted_shared_state_key_count=3,
                dependency_task_ids=["discover_markdown"],
                transport="in-process",
            )
            logger.log(
                "task_context_prepared",
                agent="analyst_beta",
                task_id="length_audit",
                task_type="length_audit",
                scope="task_scoped_shared_state_view",
                visible_shared_state_keys=["lead_name", "markdown_inventory", "runtime_config"],
                visible_shared_state_key_count=3,
                omitted_shared_state_key_count=4,
                dependency_task_ids=["discover_markdown"],
                transport="tmux",
            )

            summary = runtime.build_context_boundary_summary(logger=logger)

            self.assertEqual(summary["context_count"], 2)
            self.assertEqual(len(summary["records"]), 2)
            self.assertIn("analyst_alpha", summary["agents"])
            self.assertIn("analyst_beta", summary["agents"])
            self.assertEqual(summary["agents"]["analyst_alpha"]["context_count"], 1)
            self.assertEqual(summary["agents"]["analyst_beta"]["transports"], ["tmux"])
            self.assertEqual(
                summary["agents"]["analyst_beta"]["max_visible_shared_state_key_count"],
                3,
            )

    def test_build_task_context_snapshot_scopes_shared_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="discover_markdown",
                        title="discover",
                        task_type="discover_markdown",
                        required_skills={"inventory"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                    runtime.Task(
                        task_id="heading_audit",
                        title="heading",
                        task_type="heading_audit",
                        required_skills={"analysis"},
                        dependencies=["discover_markdown"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                    ),
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead", "analyst_alpha"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set("team_profiles", [{"name": "analyst_alpha", "agent_type": "analyst", "skills": ["analysis"]}])
            shared_state.set("markdown_inventory", [{"path": "a.md", "line_count": 3, "heading_count": 1}])
            shared_state.set("unrelated_secret", {"token": "hidden"})
            context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="analyst_alpha", skills={"analysis"}, agent_type="analyst"),
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
            snapshot = runtime.build_task_context_snapshot(
                context=context,
                task=runtime.Task(
                    task_id="heading_audit",
                    title="heading",
                    task_type="heading_audit",
                    required_skills={"analysis"},
                    dependencies=["discover_markdown"],
                    payload={},
                    locked_paths=[],
                    allowed_agent_types={"analyst"},
                ),
            )
            self.assertIn("markdown_inventory", snapshot["visible_shared_state"])
            self.assertNotIn("unrelated_secret", snapshot["visible_shared_state"])
            self.assertIn("unrelated_secret", snapshot["omitted_shared_state_keys"])
            self.assertIn("discover_markdown", snapshot["dependency_results"])

    def test_build_task_context_snapshot_includes_visible_task_results_for_llm_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = pathlib.Path(tmp)
            logger = runtime.EventLogger(output_dir=output_dir)
            llm_task = runtime.Task(
                task_id="llm_synthesis",
                title="Synthesize findings",
                task_type="llm_synthesis",
                required_skills={"review", "llm"},
                dependencies=["heading_audit", "length_audit", "peer_challenge", "lead_re_adjudication"],
                payload={},
                locked_paths=[],
                allowed_agent_types={"reviewer"},
            )
            board = runtime.TaskBoard(
                tasks=[
                    runtime.Task(
                        task_id="discover_markdown",
                        title="discover",
                        task_type="discover_markdown",
                        required_skills={"inventory"},
                        dependencies=[],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                        status="completed",
                        result={"files": 3},
                    ),
                    runtime.Task(
                        task_id="heading_audit",
                        title="heading",
                        task_type="heading_audit",
                        required_skills={"analysis"},
                        dependencies=["discover_markdown"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                        status="completed",
                        result={"files_without_headings": 1},
                    ),
                    runtime.Task(
                        task_id="length_audit",
                        title="length",
                        task_type="length_audit",
                        required_skills={"analysis"},
                        dependencies=["discover_markdown"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                        status="completed",
                        result={"long_files": 2},
                    ),
                    runtime.Task(
                        task_id="dynamic_planning",
                        title="plan",
                        task_type="dynamic_planning",
                        required_skills={"review"},
                        dependencies=["heading_audit", "length_audit"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                        status="completed",
                        result={"enabled": True, "inserted_tasks": ["heading_structure_followup"]},
                    ),
                    runtime.Task(
                        task_id="heading_structure_followup",
                        title="followup",
                        task_type="heading_structure_followup",
                        required_skills={"analysis"},
                        dependencies=["dynamic_planning"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                        status="completed",
                        result={"lowest_heading_density": [{"path": "docs/a.md"}]},
                    ),
                    runtime.Task(
                        task_id="length_risk_followup",
                        title="length followup",
                        task_type="length_risk_followup",
                        required_skills={"analysis"},
                        dependencies=["dynamic_planning"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"analyst"},
                        status="completed",
                        result={"high_risk_long_files": [{"path": "docs/b.md"}]},
                    ),
                    runtime.Task(
                        task_id="peer_challenge",
                        title="challenge",
                        task_type="peer_challenge",
                        required_skills={"review"},
                        dependencies=["dynamic_planning"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                        status="completed",
                        result={"summary": "challenge done"},
                    ),
                    runtime.Task(
                        task_id="lead_adjudication",
                        title="adjudicate",
                        task_type="lead_adjudication",
                        required_skills={"lead"},
                        dependencies=["peer_challenge"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"lead"},
                        status="completed",
                        result={"verdict": "challenge"},
                    ),
                    runtime.Task(
                        task_id="evidence_pack",
                        title="evidence",
                        task_type="evidence_pack",
                        required_skills={"review"},
                        dependencies=["lead_adjudication"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"reviewer"},
                        status="completed",
                        result={"triggered": False},
                    ),
                    runtime.Task(
                        task_id="lead_re_adjudication",
                        title="re-adjudicate",
                        task_type="lead_re_adjudication",
                        required_skills={"lead"},
                        dependencies=["evidence_pack"],
                        payload={},
                        locked_paths=[],
                        allowed_agent_types={"lead"},
                        status="completed",
                        result={"verdict": "accept"},
                    ),
                    llm_task,
                ],
                logger=logger,
            )
            mailbox = runtime.Mailbox(participants=["lead", "reviewer_gamma"], logger=logger)
            file_locks = runtime.FileLockRegistry(logger=logger)
            provider, _ = runtime.build_provider(
                provider_name="heuristic",
                model="heuristic-v1",
                openai_api_key_env="OPENAI_API_KEY",
                openai_base_url="https://api.openai.com/v1",
                require_llm=False,
                timeout_sec=5,
            )
            shared_state = runtime.SharedState()
            shared_state.set("lead_name", "lead")
            shared_state.set("heading_issues", [{"path": "docs/a.md"}])
            shared_state.set("length_issues", [{"path": "docs/b.md", "line_count": 220}])
            shared_state.set("unrelated_secret", {"token": "hidden"})
            context = runtime.AgentContext(
                profile=runtime.AgentProfile(name="reviewer_gamma", skills={"review", "llm"}, agent_type="reviewer"),
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

            snapshot = runtime.build_task_context_snapshot(
                context=context,
                task=llm_task,
            )

            visible_results = snapshot["visible_task_results"]
            self.assertIn("heading_audit", visible_results)
            self.assertIn("length_audit", visible_results)
            self.assertIn("dynamic_planning", visible_results)
            self.assertIn("heading_structure_followup", visible_results)
            self.assertIn("length_risk_followup", visible_results)
            self.assertIn("lead_adjudication", visible_results)
            self.assertIn("lead_re_adjudication", visible_results)
            self.assertNotIn("discover_markdown", visible_results)
            self.assertTrue(
                set(visible_results.keys()).issubset(set(snapshot["visible_task_result_ids"])),
            )

    def test_scoped_shared_state_hides_non_visible_keys(self) -> None:
        shared_state = runtime.SharedState()
        shared_state.set("visible", 1)
        shared_state.set("hidden", 2)
        scoped = runtime.ScopedSharedState(
            _underlying=shared_state,
            _visible_keys={"visible"},
        )
        self.assertEqual(scoped.get("visible"), 1)
        self.assertIsNone(scoped.get("hidden"))
        scoped.set("new_key", 3)
        self.assertEqual(scoped.get("new_key"), 3)

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

    def test_run_host_session_task_entrypoint_executes_assigned_task_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir = root / "target"
            output_dir = root / "output"
            mailbox_dir = output_dir / "_mailbox"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a.md").write_text("# Title\nbody\n", encoding="utf-8")
            (target_dir / "b.md").write_text("plain\nplain\nplain\n", encoding="utf-8")
            payload_path = root / "host_session_task.json"
            result_path = root / "host_session_task.result.json"
            task = runtime.Task(
                task_id="discover_markdown",
                title="Scan markdown files",
                task_type="discover_markdown",
                required_skills={"inventory"},
                dependencies=[],
                payload={},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
            payload_path.write_text(
                json.dumps(
                    {
                        "contract": "host_session_task",
                        "contract_version": 1,
                        "profile": {
                            "name": "analyst_alpha",
                            "skills": ["inventory", "analysis"],
                            "agent_type": "analyst",
                        },
                        "goal": "scan markdown",
                        "target_dir": str(target_dir),
                        "output_dir": str(output_dir),
                        "runtime_script": str((MODULE_DIR / "agent_team_runtime.py").resolve()),
                        "workflow_pack": "markdown-audit",
                        "runtime_config": runtime.RuntimeConfig(teammate_mode="host").to_dict(),
                        "model_config": {
                            "provider_name": "heuristic",
                            "model": "heuristic-v1",
                            "openai_api_key_env": "OPENAI_API_KEY",
                            "openai_base_url": "https://api.openai.com/v1",
                            "require_llm": False,
                            "timeout_sec": 5,
                        },
                        "participants": ["lead", "analyst_alpha"],
                        "mailbox_storage_dir": str(mailbox_dir),
                        "shared_state": {
                            "lead_name": "lead",
                            "runtime_config": runtime.RuntimeConfig(teammate_mode="host").to_dict(),
                            "team_profiles": [
                                {
                                    "name": "analyst_alpha",
                                    "agent_type": "analyst",
                                    "skills": ["inventory", "analysis"],
                                }
                            ],
                        },
                        "session_state": {},
                        "assignment": {
                            "contract": "session_task_assignment",
                            "contract_version": 1,
                            "transport": "host",
                            "execution_mode": "session_thread",
                            "task": task.to_dict(),
                            "task_context": {
                                "scope": "task_specific",
                                "visible_shared_state_keys": ["lead_name", "runtime_config", "team_profiles"],
                                "visible_shared_state_key_count": 3,
                                "omitted_shared_state_key_count": 0,
                                "visible_shared_state": {
                                    "lead_name": "lead",
                                    "runtime_config": runtime.RuntimeConfig(teammate_mode="host").to_dict(),
                                    "team_profiles": [
                                        {
                                            "name": "analyst_alpha",
                                            "agent_type": "analyst",
                                            "skills": ["inventory", "analysis"],
                                        }
                                    ],
                                },
                                "visible_task_results": {},
                                "dependency_results": {},
                                "board_task_ids": ["discover_markdown"],
                                "dependencies": [],
                            },
                        },
                        "result_path": str(result_path),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            exit_code = runtime.run_host_session_task_entrypoint(payload_path)

            self.assertEqual(exit_code, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(result.get("success"))
            self.assertEqual(result.get("result", {}).get("markdown_files"), 2)
            inventory = result.get("state_updates", {}).get("markdown_inventory", [])
            self.assertEqual(len(inventory), 2)

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
