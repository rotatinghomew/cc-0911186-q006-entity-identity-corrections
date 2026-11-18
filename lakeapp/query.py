from __future__ import annotations


def explain_identity(source: str, key: str, version: int) -> dict:
    return {"source": source, "key": key, "mapping_version": version}
