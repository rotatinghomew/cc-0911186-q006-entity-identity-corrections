"""报表冻结与重算。

冻结：把当时每个事实的归属依据逐行固化到 frozen_fact_basis，并登记 report_freeze。
重算：已冻结期间拒绝重算；未冻结期间按当前归属重新汇总并发布新版本 manifest。
"""

from __future__ import annotations

from .identity import now_ts
from .publish import publish
from .storage import FOREVER, decode, transaction


def period_of(occurred_at: str) -> str:
    return occurred_at[:7]


def _latest_revision(connection) -> str | None:
    row = connection.execute(
        "SELECT revision_id FROM identity_revision ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return row["revision_id"] if row else None


def _report_rows(connection, period: str, campus_id: str) -> list[dict]:
    rows = []
    for fact in connection.execute("SELECT * FROM facts ORDER BY source,business_key"):
        payload = decode(fact["payload"])
        if period_of(payload["occurred_at"]) != period:
            continue
        if campus_id != "*" and payload.get("campus_id", "*") != campus_id:
            continue
        attr = connection.execute(
            "SELECT * FROM fact_attribution WHERE source=? AND business_key=? AND valid_to=?",
            (fact["source"], fact["business_key"], FOREVER),
        ).fetchone()
        rows.append({
            "source": fact["source"],
            "business_key": fact["business_key"],
            "value": payload["value"],
            "campus_id": payload.get("campus_id", "*"),
            "person_id": attr["person_id"] if attr else None,
            "mapping_id": attr["mapping_id"] if attr else None,
            "resolution": attr["resolution"] if attr else "unmapped",
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    attributed = [r for r in rows if r["resolution"] == "attributed"]
    return {
        "row_count": len(rows),
        "headcount": len({r["person_id"] for r in attributed}),
        "amount": round(sum(r["value"] for r in rows), 6),
        "attributed_amount": round(sum(r["value"] for r in attributed), 6),
        "unresolved_row_count": sum(1 for r in rows if r["resolution"] != "attributed"),
    }


def freeze_report(connection, report_name: str, period: str,
                  campus_id: str = "*") -> dict:
    existing = connection.execute(
        "SELECT 1 FROM report_freeze WHERE report_name=? AND period=? AND campus_id=?",
        (report_name, period, campus_id),
    ).fetchone()
    if existing:
        raise ValueError(f"报表已冻结: {report_name}/{period}/{campus_id}")
    rows = _report_rows(connection, period, campus_id)
    ts = now_ts()
    revision_id = _latest_revision(connection)
    next_version = connection.execute(
        "SELECT COALESCE(MAX(version),0)+1 AS v FROM manifests WHERE name=?",
        (report_name,),
    ).fetchone()["v"]
    with transaction(connection):
        connection.execute(
            "INSERT INTO report_freeze(report_name,period,campus_id,frozen_at,revision_id,manifest_version) "
            "VALUES(?,?,?,?,?,?)",
            (report_name, period, campus_id, ts, revision_id, next_version),
        )
        for row in rows:
            connection.execute(
                "INSERT INTO frozen_fact_basis(report_name,period,campus_id,source,business_key,"
                "person_id,mapping_id,resolution,frozen_at,revision_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (report_name, period, campus_id, row["source"], row["business_key"],
                 row["person_id"], row["mapping_id"], row["resolution"], ts, revision_id),
            )
    payload = {
        "rows": rows,
        "summary": summarize(rows),
        "frozen": True,
        "period": period,
        "campus_id": campus_id,
        "as_of_revision_id": revision_id,
    }
    publish(connection, report_name, next_version, [payload])
    return {"version": next_version, "summary": payload["summary"],
            "as_of_revision_id": revision_id}


def is_frozen(connection, report_name: str, period: str, campus_id: str = "*") -> bool:
    row = connection.execute(
        "SELECT 1 FROM report_freeze WHERE report_name=? AND period=? "
        "AND (campus_id='*' OR campus_id=?)",
        (report_name, period, campus_id),
    ).fetchone()
    return row is not None


def recompute_report(connection, report_name: str, period: str,
                     campus_id: str = "*") -> dict:
    freeze = connection.execute(
        "SELECT * FROM report_freeze WHERE report_name=? AND period=? AND campus_id=?",
        (report_name, period, campus_id),
    ).fetchone()
    if freeze:
        raise ValueError(
            f"期间已冻结，不能重算；冻结依据修订 {freeze['revision_id']}，"
            f"manifest v{freeze['manifest_version']}"
        )
    rows = _report_rows(connection, period, campus_id)
    next_version = connection.execute(
        "SELECT COALESCE(MAX(version),0)+1 AS v FROM manifests WHERE name=?",
        (report_name,),
    ).fetchone()["v"]
    payload = {
        "rows": rows,
        "summary": summarize(rows),
        "frozen": False,
        "period": period,
        "campus_id": campus_id,
        "as_of_revision_id": _latest_revision(connection),
    }
    publish(connection, report_name, next_version, [payload])
    return {"version": next_version, "summary": payload["summary"]}


def frozen_basis(connection, report_name: str, period: str,
                 campus_id: str = "*") -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM frozen_fact_basis WHERE report_name=? AND period=? AND campus_id=? "
            "ORDER BY source,business_key",
            (report_name, period, campus_id),
        )
    ]
