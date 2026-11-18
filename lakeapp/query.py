"""血缘与影响面查询：事实为何归属某人、一次修订影响哪些可重算报表。"""

from __future__ import annotations

from .attribution import attribution_history
from .freeze import is_frozen, period_of
from .storage import decode


def explain_identity(connection, source: str, business_key: str) -> dict:
    """回放一条事实的完整身份依据：归属轨迹 + 映射版本 + 修订单 + 冲突/待查 + 冻结依据。"""
    fact = connection.execute(
        "SELECT * FROM facts WHERE source=? AND business_key=?",
        (source, business_key),
    ).fetchone()
    history = attribution_history(connection, source, business_key)
    mapping_versions = [
        dict(row)
        for row in connection.execute(
            "SELECT m.* FROM identity_mapping m WHERE m.source=? AND m.source_key=? "
            "ORDER BY m.record_from",
            (source, business_key),
        )
    ]
    mapping_ids = [m["mapping_id"] for m in mapping_versions]
    revisions: list[dict] = []
    for rev in connection.execute("SELECT * FROM identity_revision ORDER BY created_at"):
        rev = dict(rev)
        if rev["revision_id"] in {m["revision_id"] for m in mapping_versions} or \
                rev["revision_id"] in {h["revision_id"] for h in history if h["revision_id"]}:
            rev["payload"] = decode(rev["payload"])
            revisions.append(rev)
    conflicts = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM mapping_conflict WHERE source=? AND source_key=? ORDER BY conflict_id",
            (source, business_key),
        )
    ]
    suspense = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM suspense_entry WHERE source=? AND business_key=? ORDER BY entry_id",
            (source, business_key),
        )
    ]
    frozen = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM frozen_fact_basis WHERE source=? AND business_key=? "
            "ORDER BY frozen_at",
            (source, business_key),
        )
    ]
    return {
        "source": source,
        "business_key": business_key,
        "fact": dict(fact) if fact else None,
        "attribution_history": history,
        "mapping_versions": mapping_versions,
        "revisions": revisions,
        "conflicts": conflicts,
        "suspense": suspense,
        "frozen_basis": frozen,
    }


def revision_impact(connection, revision_id: str, report_name: str = "monthly") -> dict:
    """列出一次修订引发的归属变化，以及每项影响落在冻结/可重算的哪个报表期间。"""
    changes = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM fact_attribution WHERE revision_id=? ORDER BY attribution_id",
            (revision_id,),
        )
    ]
    impacted: list[dict] = []
    for new_row in changes:
        prior = connection.execute(
            "SELECT * FROM fact_attribution WHERE source=? AND business_key=? "
            "AND attribution_id < ? ORDER BY attribution_id DESC LIMIT 1",
            (new_row["source"], new_row["business_key"], new_row["attribution_id"]),
        ).fetchone()
        prior = dict(prior) if prior else None
        if prior and prior["person_id"] == new_row["person_id"] \
                and prior["resolution"] == new_row["resolution"]:
            continue
        fact = connection.execute(
            "SELECT payload FROM facts WHERE source=? AND business_key=?",
            (new_row["source"], new_row["business_key"]),
        ).fetchone()
        payload = decode(fact["payload"]) if fact else {}
        period = period_of(payload.get("occurred_at", "")) if payload else None
        campus_id = payload.get("campus_id", "*")
        frozen = is_frozen(connection, report_name, period, campus_id) if period else False
        impacted.append({
            "source": new_row["source"],
            "business_key": new_row["business_key"],
            "period": period,
            "campus_id": campus_id,
            "from_person_id": prior["person_id"] if prior else None,
            "to_person_id": new_row["person_id"],
            "from_resolution": prior["resolution"] if prior else None,
            "to_resolution": new_row["resolution"],
            "frozen": frozen,
            "recompute_allowed": not frozen,
        })
    frozen_count = sum(1 for item in impacted if item["frozen"])
    return {
        "revision_id": revision_id,
        "report_name": report_name,
        "impacted_fact_count": len(impacted),
        "frozen_impact_count": frozen_count,
        "recomputable_impact_count": len(impacted) - frozen_count,
        "items": impacted,
    }
