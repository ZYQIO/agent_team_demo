from __future__ import annotations

from typing import Any, Dict, List, Sequence


def get_latest_agent_reply(peer_challenge: Dict[str, Any], agent_name: str) -> str:
    for round_key in ("round3", "round2", "round1"):
        round_data = peer_challenge.get(round_key, {})
        received = round_data.get("received_replies", {})
        if agent_name in received:
            return str(received[agent_name])
    return ""


def get_team_profiles(context: Any) -> List[Dict[str, Any]]:
    raw_profiles = context.shared_state.get("team_profiles", [])
    if not isinstance(raw_profiles, list):
        return []
    profiles: List[Dict[str, Any]] = []
    for item in raw_profiles:
        if isinstance(item, dict):
            profiles.append(item)
    return profiles


def get_team_member_names(
    context: Any,
    agent_type: str = "",
    exclude: Sequence[str] | None = None,
) -> List[str]:
    excluded = {name for name in (exclude or [])}
    names: List[str] = []
    for profile in get_team_profiles(context):
        name = str(profile.get("name", "") or "")
        if not name or name in excluded:
            continue
        if agent_type and str(profile.get("agent_type", "")) != agent_type:
            continue
        names.append(name)
    return names
