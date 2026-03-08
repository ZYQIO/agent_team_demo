from __future__ import annotations

from typing import Any, Dict

from ..core import Task
from .shared_challenge import (
    handle_evidence_pack as shared_handle_evidence_pack,
    handle_lead_adjudication as shared_handle_lead_adjudication,
    handle_lead_re_adjudication as shared_handle_lead_re_adjudication,
    handle_peer_challenge as shared_handle_peer_challenge,
)


def handle_peer_challenge(context: Any, task: Task) -> Dict[str, Any]:
    return shared_handle_peer_challenge(context=context, task=task)


def handle_lead_adjudication(context: Any, _task: Task) -> Dict[str, Any]:
    return shared_handle_lead_adjudication(context=context, _task=_task)


def handle_evidence_pack(context: Any, task: Task) -> Dict[str, Any]:
    return shared_handle_evidence_pack(context=context, task=task)


def handle_lead_re_adjudication(context: Any, _task: Task) -> Dict[str, Any]:
    return shared_handle_lead_re_adjudication(context=context, _task=_task)
