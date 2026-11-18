from __future__ import annotations

from .attribution import attribute_fact
from .contract import Event
from .identity import now_ts
from .storage import FOREVER, encode, transaction


def apply_events(connection, events: list[Event]) -> int:
    changed = 0
    with transaction(connection):
        for event in events:
            if event.kind == "retract":
                connection.execute(
                    "DELETE FROM facts WHERE source=? AND business_key=?",
                    (event.source, event.business_key),
                )
                current = connection.execute(
                    "SELECT attribution_id FROM fact_attribution "
                    "WHERE source=? AND business_key=? AND valid_to=?",
                    (event.source, event.business_key, FOREVER),
                ).fetchone()
                ts = now_ts()
                if current:
                    connection.execute(
                        "UPDATE fact_attribution SET valid_to=? WHERE attribution_id=?",
                        (ts, current["attribution_id"]),
                    )
                connection.execute(
                    "INSERT INTO fact_attribution(source,business_key,person_id,mapping_id,"
                    "resolution,valid_from,valid_to,reason,created_at,revision_id) "
                    "VALUES(?,?,NULL,NULL,'retracted',?,?, 'source-retract',?,NULL)",
                    (event.source, event.business_key, ts, FOREVER, ts),
                )
                connection.execute(
                    "UPDATE suspense_entry SET cleared_at=?, cleared_revision_id=? "
                    "WHERE source=? AND business_key=? AND cleared_at IS NULL",
                    (ts, None, event.source, event.business_key),
                )
            else:
                connection.execute(
                    "INSERT INTO facts(source,business_key,payload,source_batch,version) VALUES(?,?,?,?,1) "
                    "ON CONFLICT(source,business_key) DO UPDATE SET payload=excluded.payload,"
                    "source_batch=excluded.source_batch,version=facts.version+1",
                    (event.source, event.business_key,
                     encode({"value": event.value, "occurred_at": event.occurred_at,
                             "campus_id": event.campus_id}),
                     event.batch_id),
                )
            changed += 1
    # 归属解析在事实事务提交后执行（内部自带事务，失败可单独重试）
    for event in events:
        if event.kind == "upsert":
            attribute_fact(connection, event.source, event.business_key, event.occurred_at)
    return changed


def facts(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT source,business_key,payload,source_batch,version FROM facts ORDER BY source,business_key")]
