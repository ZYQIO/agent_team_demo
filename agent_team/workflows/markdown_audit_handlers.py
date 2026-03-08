from __future__ import annotations

from typing import Any, Dict

from .markdown_audit_analysis import (
    handle_discover_markdown,
    handle_dynamic_planning,
    handle_heading_audit,
    handle_heading_structure_followup,
    handle_length_audit,
    handle_length_risk_followup,
)
from .markdown_audit_challenge import (
    handle_evidence_pack,
    handle_lead_adjudication,
    handle_lead_re_adjudication,
    handle_peer_challenge,
)
from .markdown_audit_reporting import (
    handle_llm_synthesis,
    handle_recommendation_pack,
)
from .markdown_audit_shared import (
    get_latest_agent_reply,
    get_team_member_names,
    get_team_profiles,
)


def build_markdown_audit_handlers() -> Dict[str, Any]:
    return {
        "discover_markdown": handle_discover_markdown,
        "heading_audit": handle_heading_audit,
        "length_audit": handle_length_audit,
        "dynamic_planning": handle_dynamic_planning,
        "heading_structure_followup": handle_heading_structure_followup,
        "length_risk_followup": handle_length_risk_followup,
        "peer_challenge": handle_peer_challenge,
        "lead_adjudication": handle_lead_adjudication,
        "evidence_pack": handle_evidence_pack,
        "lead_re_adjudication": handle_lead_re_adjudication,
        "llm_synthesis": handle_llm_synthesis,
        "recommendation_pack": handle_recommendation_pack,
    }
