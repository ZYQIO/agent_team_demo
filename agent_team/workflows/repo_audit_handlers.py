from __future__ import annotations

from typing import Any, Dict

from .repo_audit_analysis import (
    handle_directory_hotspot_followup,
    handle_discover_repository,
    handle_extension_audit,
    handle_extension_hotspot_followup,
    handle_large_file_audit,
    handle_repo_dynamic_planning,
)
from .repo_audit_reporting import (
    handle_llm_synthesis,
    handle_repo_recommendation_pack,
)
from .shared_challenge import (
    handle_evidence_pack,
    handle_lead_adjudication,
    handle_lead_re_adjudication,
    handle_peer_challenge,
)


def build_repo_audit_handlers() -> Dict[str, Any]:
    return {
        "discover_repository": handle_discover_repository,
        "extension_audit": handle_extension_audit,
        "large_file_audit": handle_large_file_audit,
        "repo_dynamic_planning": handle_repo_dynamic_planning,
        "extension_hotspot_followup": handle_extension_hotspot_followup,
        "directory_hotspot_followup": handle_directory_hotspot_followup,
        "peer_challenge": handle_peer_challenge,
        "lead_adjudication": handle_lead_adjudication,
        "evidence_pack": handle_evidence_pack,
        "lead_re_adjudication": handle_lead_re_adjudication,
        "llm_synthesis": handle_llm_synthesis,
        "repo_recommendation_pack": handle_repo_recommendation_pack,
    }
