"""事实归属解析结果落地：仅追加的归属轨迹与待查池。

事实本身（facts）不可变；归属变化只通过关闭当前 fact_attribution 行、
插入新行表达，任何时点的归属依据都可回放。
"""

from __future__ import annotations

from .identity import now_ts, resolve_current
from .storage import FOREVER, transaction


def _open_suspense(connection, source: str, business_key: str, reason: str,
                   revision_id: str | None, ts: str) -> None:
    open_row = connection.execute(
        "SELECT entry_id FROM suspense_entry WHERE source=? AND business_key=? AND cleared_at IS NULL",
        (source, business_key),
    ).fetchone()
    if open_row:
        return
    connection.execute(
        "INSERT INTO suspense_entry(source,business_key,reason,opened_at,opened_revision_id) "
        "VALUES(?,?,?,?,?)",
        (source, business_key, reason, ts, revision_id),
    )


def _clear_suspense(connection, source: str, business_key: str,
                    person_id: str, revision_id: str | None, ts: str) -> None:
    connection.execute(
        "UPDATE suspense_entry SET cleared_at=?, cleared_revision_id=?, cleared_person_id=? "
        "WHERE source=? AND business_key=? AND cleared_at IS NULL",
        (ts, revision_id, person_id, source, business_key),
    )


def attribute_fact(connection, source: str, business_key: str, occurred_at: str,
                   reason: str = "ingest", revision_id: str | None = None,
                   now_ts: str | None = None) -> dict:
    """解析一条事实的当前归属并追加归属版本，返回解析结果。

    - 唯一命中 active 映射：attributed，清理待查；
    - unmapped/conflict/revoked：不归属 person，进入待查池，事实不删除、不静默合并。
    """
    ts = now_ts or _utcnow()
    result = resolve_current(connection, source, business_key, occurred_at)
    with transaction(connection):
        current = connection.execute(
            "SELECT * FROM fact_attribution WHERE source=? AND business_key=? AND valid_to=?",
            (source, business_key, FOREVER),
        ).fetchone()
        same = (
            current is not None
            and current["resolution"] == result["resolution"]
            and (current["person_id"] or None) == (result["person_id"] or None)
            and (current["mapping_id"] or None) == (result["mapping_id"] or None)
        )
        if not same:
            if current is not None:
                connection.execute(
                    "UPDATE fact_attribution SET valid_to=? WHERE attribution_id=?",
                    (ts, current["attribution_id"]),
                )
            connection.execute(
                "INSERT INTO fact_attribution(source,business_key,person_id,mapping_id,"
                "resolution,valid_from,valid_to,reason,created_at,revision_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (source, business_key, result["person_id"], result["mapping_id"],
                 result["resolution"], ts, FOREVER, reason, ts, revision_id),
            )
        if result["resolution"] == "attributed":
            _clear_suspense(connection, source, business_key,
                            result["person_id"], revision_id, ts)
        else:
            _open_suspense(connection, source, business_key,
                           result["resolution"], revision_id, ts)
    return result


def _utcnow() -> str:
    return now_ts()


def current_attribution(connection, source: str, business_key: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM fact_attribution WHERE source=? AND business_key=? AND valid_to=?",
        (source, business_key, FOREVER),
    ).fetchone()
    return dict(row) if row else None


def attribution_history(connection, source: str, business_key: str) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM fact_attribution WHERE source=? AND business_key=? ORDER BY attribution_id",
            (source, business_key),
        )
    ]


def open_suspense(connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM suspense_entry WHERE cleared_at IS NULL ORDER BY entry_id"
        )
    ]
