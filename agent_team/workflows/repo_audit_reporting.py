from __future__ import annotations

import json
from typing import Any, Dict, List

from ..core import Task, utc_now


def handle_llm_synthesis(context: Any, _task: Task) -> Dict[str, Any]:
    extension_result = context.board.get_task_result("extension_audit") or {}
    large_file_result = context.board.get_task_result("large_file_audit") or {}
    dynamic_plan = context.board.get_task_result("repo_dynamic_planning") or {}
    extension_followup = context.board.get_task_result("extension_hotspot_followup") or {}
    directory_followup = context.board.get_task_result("directory_hotspot_followup") or {}
    peer_challenge_result = context.board.get_task_result("peer_challenge") or {}
    lead_adjudication = context.board.get_task_result("lead_adjudication") or {}
    lead_re_adjudication = context.board.get_task_result("lead_re_adjudication") or lead_adjudication
    evidence_pack = context.board.get_task_result("evidence_pack") or {}
    repository_inventory = context.shared_state.get("repository_inventory", [])
    large_files = context.shared_state.get("repository_large_files", [])

    synthesis_input = {
        "goal": context.goal,
        "extension_result": extension_result,
        "large_file_result": large_file_result,
        "dynamic_plan": dynamic_plan,
        "extension_followup": extension_followup,
        "directory_followup": directory_followup,
        "peer_challenge_result": peer_challenge_result,
        "lead_adjudication_initial": lead_adjudication,
        "evidence_pack": evidence_pack,
        "lead_adjudication_final": lead_re_adjudication,
        "repository_file_count": len(repository_inventory),
        "oversized_file_paths": [item.get("path", "") for item in large_files[:20]],
    }

    system_prompt = (
        "You are a principal repository reviewer. "
        "Return concise actionable recommendations. "
        "Use markdown bullet points and prioritize by operational impact."
    )
    user_prompt = (
        "Analyze the repository audit findings below and provide:\n"
        "1) Top 3 priorities\n"
        "2) Short-term fixes (this week)\n"
        "3) Longer-term guardrails\n\n"
        f"Findings JSON:\n{json.dumps(synthesis_input, ensure_ascii=False, indent=2)}"
    )

    llm_text = context.provider.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    synthesis = {
        "provider": context.provider.metadata.to_dict(),
        "content": llm_text,
    }
    context.shared_state.set("llm_synthesis", synthesis)
    return {
        "provider": context.provider.metadata.to_dict(),
        "preview": llm_text[:500],
    }


def handle_repo_recommendation_pack(context: Any, _task: Task) -> Dict[str, Any]:
    repository_inventory = context.shared_state.get("repository_inventory", [])
    extension_summary = context.shared_state.get("repository_extension_summary", {})
    large_files = context.shared_state.get("repository_large_files", [])
    large_file_result = context.board.get_task_result("large_file_audit") or {}
    dynamic_plan = context.shared_state.get("repo_dynamic_plan", {})
    extension_followup = context.shared_state.get("repo_extension_hotspots", {})
    directory_followup = context.shared_state.get("repo_directory_hotspots", {})
    peer_challenge = context.shared_state.get("peer_challenge", {})
    peer_round1 = peer_challenge.get("round1", {})
    peer_round2 = peer_challenge.get("round2", {})
    peer_round3 = peer_challenge.get("round3", {})
    peer_round1_replies = peer_round1.get("received_replies", {})
    peer_round2_replies = peer_round2.get("received_replies", {})
    peer_round3_replies = peer_round3.get("received_replies", {})
    peer_round1_missing = peer_round1.get("missing_replies", [])
    peer_round2_missing = peer_round2.get("missing_replies", [])
    peer_round3_missing = peer_round3.get("missing_replies", [])
    provisional_adjudication = peer_challenge.get("provisional_adjudication", {})
    post_round3_adjudication = peer_challenge.get("post_round3_adjudication", {})
    lead_adjudication = context.shared_state.get("lead_adjudication", {})
    evidence_pack = context.shared_state.get("evidence_pack", {})
    lead_re_adjudication = context.shared_state.get("lead_re_adjudication", lead_adjudication)
    llm_synthesis = context.shared_state.get("llm_synthesis", {})
    llm_text = llm_synthesis.get("content", "").strip()
    llm_provider = llm_synthesis.get("provider", {})

    report_path = context.output_dir / "final_report.md"
    lines: List[str] = []
    lines.append("# Agent Team Report")
    lines.append("")
    lines.append(f"- Generated at: {utc_now()}")
    lines.append(f"- Goal: {context.goal}")
    lines.append(f"- Repository files scanned: {len(repository_inventory)}")
    lines.append("")
    lines.append("## Repository Findings")
    lines.append("")
    lines.append(f"- Unique extensions: {extension_summary.get('unique_extensions', 0)}")
    lines.append(f"- Files without extension: {extension_summary.get('files_without_extension', 0)}")
    lines.append(f"- Oversized files: {large_file_result.get('oversized_files', 0)}")
    top_extensions = extension_summary.get("top_extensions", [])
    for row in top_extensions[:3]:
        lines.append(
            f"- Extension {row.get('extension')}: files={row.get('file_count')} "
            f"lines={row.get('total_lines')} bytes={row.get('total_bytes')}"
        )
    if large_files:
        for row in large_files[:3]:
            lines.append(
                f"- Oversized {row.get('path')}: lines={row.get('line_count')} bytes={row.get('byte_count')}"
            )
    lines.append("")
    lines.append("## Dynamic Tasking")
    lines.append("")
    lines.append(f"- Enabled: {dynamic_plan.get('enabled', False)}")
    inserted_tasks = dynamic_plan.get("inserted_tasks", [])
    if inserted_tasks:
        lines.append(f"- Inserted tasks: {', '.join(inserted_tasks)}")
    else:
        lines.append("- Inserted tasks: none")
    if dynamic_plan.get("peer_challenge_dependencies_added"):
        lines.append(
            "- Added peer challenge dependencies: "
            f"{', '.join(dynamic_plan.get('peer_challenge_dependencies_added', []))}"
        )
    if extension_followup:
        hotspot_rows = extension_followup.get("extension_hotspots", [])
        lines.append(f"- Extension hotspot rows: {len(hotspot_rows)}")
        for row in hotspot_rows[:3]:
            lines.append(
                f"- Extension hotspot {row.get('extension')}: files={row.get('file_count')} "
                f"lines={row.get('total_lines')}"
            )
    if directory_followup:
        busiest_rows = directory_followup.get("busiest_directories", [])
        lines.append(f"- Directory hotspot rows: {len(busiest_rows)}")
        for row in busiest_rows[:3]:
            lines.append(
                f"- Directory hotspot {row.get('top_level_dir')}: files={row.get('file_count')} "
                f"lines={row.get('total_lines')}"
            )
    lines.append("")
    lines.append("## Peer Challenge Round")
    lines.append("")
    lines.append(f"- Round 1 question: {peer_round1.get('question', 'N/A')}")
    lines.append(f"- Round 1 replies: {len(peer_round1_replies)}")
    for sender, reply in peer_round1_replies.items():
        lines.append(f"- R1 {sender}: {reply}")
    if peer_round1_missing:
        lines.append(f"- Round 1 missing replies: {', '.join(peer_round1_missing)}")
    lines.append(f"- Round 2 question: {peer_round2.get('question', 'N/A')}")
    lines.append(f"- Round 2 replies: {len(peer_round2_replies)}")
    for sender, reply in peer_round2_replies.items():
        lines.append(f"- R2 {sender}: {reply}")
    if peer_round2_missing:
        lines.append(f"- Round 2 missing replies: {', '.join(peer_round2_missing)}")
    if peer_round3:
        lines.append(f"- Round 3 question: {peer_round3.get('question', 'N/A')}")
        lines.append(f"- Round 3 replies: {len(peer_round3_replies)}")
        for sender, reply in peer_round3_replies.items():
            lines.append(f"- R3 {sender}: {reply}")
        if peer_round3_missing:
            lines.append(f"- Round 3 missing replies: {', '.join(peer_round3_missing)}")
    if provisional_adjudication:
        lines.append(
            f"- Provisional adjudication: {provisional_adjudication.get('verdict')} "
            f"(score={provisional_adjudication.get('score')})"
        )
    if post_round3_adjudication:
        lines.append(
            f"- Post-round3 adjudication: {post_round3_adjudication.get('verdict')} "
            f"(score={post_round3_adjudication.get('score')})"
        )
    lines.append("")
    lines.append("## Evidence Pack")
    lines.append("")
    lines.append(f"- Triggered: {evidence_pack.get('triggered', False)}")
    evidence_focus_areas = evidence_pack.get("focus_areas", [])
    if evidence_focus_areas:
        lines.append(f"- Focus areas: {', '.join(evidence_focus_areas)}")
    if evidence_pack.get("triggered"):
        lines.append(f"- Question: {evidence_pack.get('question', 'N/A')}")
        per_target_questions = evidence_pack.get("per_target_questions", {})
        for target, question in per_target_questions.items():
            compact = " ".join(str(question).splitlines())
            lines.append(f"- Prompt {target}: {compact[:180]}")
        evidence_replies = evidence_pack.get("received_replies", {})
        lines.append(f"- Replies received: {len(evidence_replies)}")
        for sender, reply in evidence_replies.items():
            lines.append(f"- Evidence {sender}: {reply}")
        evidence_missing = evidence_pack.get("missing_replies", [])
        if evidence_missing:
            lines.append(f"- Missing evidence replies: {', '.join(evidence_missing)}")
    else:
        lines.append(f"- Reason: {evidence_pack.get('reason', 'not required')}")
    lines.append("")
    lines.append("## Lead Adjudication")
    lines.append("")
    lines.append(f"- Initial Verdict: {lead_adjudication.get('verdict', 'N/A')}")
    lines.append(f"- Initial Score: {lead_adjudication.get('score', 'N/A')}")
    lines.append(f"- Final Verdict: {lead_re_adjudication.get('verdict', 'N/A')}")
    lines.append(f"- Final Score: {lead_re_adjudication.get('score', 'N/A')}")
    lines.append(f"- Rationale: {lead_re_adjudication.get('rationale', 'N/A')}")
    if "evidence_bonus" in lead_re_adjudication:
        lines.append(f"- Evidence Bonus: {lead_re_adjudication.get('evidence_bonus')}")
    lead_thresholds = lead_re_adjudication.get("thresholds", {})
    lead_weights = lead_re_adjudication.get("weights", {})
    if lead_thresholds:
        lines.append(
            f"- Thresholds: accept>={lead_thresholds.get('accept')} / "
            f"challenge>={lead_thresholds.get('challenge')}"
        )
    if lead_weights:
        lines.append(
            f"- Weights: completeness={lead_weights.get('completeness')} "
            f"rebuttal={lead_weights.get('rebuttal_coverage')} depth={lead_weights.get('argument_depth')}"
        )
    lines.append("")
    lines.append("## LLM Synthesis")
    lines.append("")
    lines.append(
        f"- Provider: {llm_provider.get('provider', 'unknown')} / "
        f"model={llm_provider.get('model', 'unknown')} / mode={llm_provider.get('mode', 'unknown')}"
    )
    lines.append("")
    if llm_text:
        lines.extend(llm_text.splitlines())
    else:
        lines.append("- No LLM synthesis content generated.")
    lines.append("")
    lines.append("## Recommended Actions")
    lines.append("")
    if large_files:
        lines.append("1. Break down or archive the largest files first:")
        for item in large_files[:10]:
            lines.append(f"- {item['path']} ({item['line_count']} lines / {item['byte_count']} bytes)")
    else:
        lines.append("1. No oversized files crossed the configured thresholds.")
    lines.append("")
    if top_extensions:
        lines.append("2. Standardize tooling and ownership around the dominant file types:")
        for row in top_extensions[:5]:
            lines.append(f"- {row['extension']}: {row['file_count']} files / {row['total_lines']} lines")
    else:
        lines.append("2. Extension mix is limited; keep the repository conventions lightweight.")
    lines.append("")
    lines.append("3. Add repository-level guardrails for file growth, layout drift, and ownership.")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "repository_files": len(repository_inventory),
        "oversized_files": len(large_files),
    }
