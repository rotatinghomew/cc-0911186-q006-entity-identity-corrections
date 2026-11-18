from __future__ import annotations


def resolve_mapping(candidates: list[str]) -> str | None:
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else None


def frozen_change_allowed(period: str, frozen_before: str) -> bool:
    return period >= frozen_before
