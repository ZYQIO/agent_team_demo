from __future__ import annotations

from typing import Any, Dict, List

from ..config import RuntimeConfig


def compute_adjudication(peer_challenge: Dict[str, Any], config: RuntimeConfig) -> Dict[str, Any]:
    targets = list(peer_challenge.get("targets", []))
    target_count = max(1, len(targets))
    round1 = peer_challenge.get("round1", {})
    round2 = peer_challenge.get("round2", {})
    round3 = peer_challenge.get("round3", {})

    round1_replies = round1.get("received_replies", {})
    round2_replies = round2.get("received_replies", {})
    round3_replies = round3.get("received_replies", {})

    completeness = len(round1_replies) / target_count
    round2_coverage = len(round2_replies) / target_count
    round3_coverage = len(round3_replies) / target_count if round3 else 0.0
    rebuttal_coverage = (
        (round2_coverage + round3_coverage) / 2.0 if round3 else round2_coverage
    )

    depth_pool: List[str] = list(round2_replies.values())
    if round3:
        depth_pool.extend(round3_replies.values())
    avg_rebuttal_len = sum(len(reply) for reply in depth_pool) / max(1, len(depth_pool))
    argument_depth = min(avg_rebuttal_len / 180.0, 1.0)

    w1 = max(0.0, float(config.adjudication_weight_completeness))
    w2 = max(0.0, float(config.adjudication_weight_rebuttal_coverage))
    w3 = max(0.0, float(config.adjudication_weight_argument_depth))
    weight_sum = max(1e-9, w1 + w2 + w3)
    nw1 = w1 / weight_sum
    nw2 = w2 / weight_sum
    nw3 = w3 / weight_sum

    score = int(round((nw1 * completeness + nw2 * rebuttal_coverage + nw3 * argument_depth) * 100))
    accept_threshold = int(config.adjudication_accept_threshold)
    challenge_threshold = min(int(config.adjudication_challenge_threshold), accept_threshold - 1)

    if score >= accept_threshold:
        verdict = "accept"
        rationale = "Debate evidence and rebuttal quality meet the configured acceptance bar."
    elif score >= challenge_threshold:
        verdict = "challenge"
        rationale = "Debate is partially sufficient; run an additional iteration before accepting."
    else:
        verdict = "reject"
        rationale = "Debate quality is below threshold; recommendations are not ready."

    return {
        "verdict": verdict,
        "score": score,
        "rubric": {
            "completeness": round(completeness, 3),
            "rebuttal_coverage": round(rebuttal_coverage, 3),
            "argument_depth": round(argument_depth, 3),
            "round2_coverage": round(round2_coverage, 3),
            "round3_coverage": round(round3_coverage, 3),
        },
        "thresholds": {
            "accept": accept_threshold,
            "challenge": challenge_threshold,
        },
        "weights": {
            "completeness": round(nw1, 3),
            "rebuttal_coverage": round(nw2, 3),
            "argument_depth": round(nw3, 3),
        },
        "rationale": rationale,
        "targets": targets,
    }


def compute_evidence_bonus(evidence_pack: Dict[str, Any], config: RuntimeConfig) -> Dict[str, Any]:
    targets = list(evidence_pack.get("targets", []))
    replies = evidence_pack.get("received_replies", {})
    target_count = max(1, len(targets))
    coverage = len(replies) / target_count
    avg_len = sum(len(reply) for reply in replies.values()) / max(1, len(replies))
    depth = min(avg_len / 220.0, 1.0)

    w_cov = max(0.0, float(config.re_adjudication_weight_coverage))
    w_depth = max(0.0, float(config.re_adjudication_weight_depth))
    weight_sum = max(1e-9, w_cov + w_depth)
    nw_cov = w_cov / weight_sum
    nw_depth = w_depth / weight_sum

    max_bonus = max(0, int(config.re_adjudication_max_bonus))
    bonus_ratio = nw_cov * coverage + nw_depth * depth
    bonus = int(round(bonus_ratio * max_bonus))
    return {
        "bonus": bonus,
        "metrics": {
            "coverage": round(coverage, 3),
            "depth": round(depth, 3),
            "avg_length": round(avg_len, 1),
        },
        "weights": {
            "coverage": round(nw_cov, 3),
            "depth": round(nw_depth, 3),
        },
        "max_bonus": max_bonus,
    }


def derive_evidence_focus_areas(adjudication: Dict[str, Any]) -> List[str]:
    rubric = adjudication.get("rubric", {})
    completeness = float(rubric.get("completeness", 0.0))
    rebuttal_coverage = float(rubric.get("rebuttal_coverage", 0.0))
    argument_depth = float(rubric.get("argument_depth", 0.0))

    focus: List[str] = []
    if completeness < 1.0:
        focus.append("coverage")
    if rebuttal_coverage < 0.85:
        focus.append("rebuttal")
    if argument_depth < 0.75:
        focus.append("depth")
    return focus or ["depth"]


def build_targeted_evidence_question(
    focus_areas: List[str],
    peer_name: str,
    peer_objection: str,
) -> str:
    ask_lines: List[str] = []
    ask_lines.append("Lead supplemental evidence request.")
    ask_lines.append(f"Focus areas: {', '.join(focus_areas)}")
    ask_lines.append("Deliverables:")
    if "coverage" in focus_areas:
        ask_lines.append("- Provide explicit acceptance checks with measurable thresholds.")
    if "rebuttal" in focus_areas:
        ask_lines.append("- Address the peer objection directly and state why your approach still holds.")
    if "depth" in focus_areas:
        ask_lines.append("- Provide phased rollout + rollback criteria with concrete metrics.")
    if peer_name:
        ask_lines.append(f"Peer to address: {peer_name}")
    if peer_objection:
        ask_lines.append(f"Peer objection snippet: {peer_objection[:220]}")
    return "\n".join(ask_lines)
