"""身份映射：双时间 SCD2 版本、人工核验修订与归属解析。

时间轴约定：
- valid_from/valid_to：映射的业务有效期（半开区间），按事实 occurred_at 命中；
- record_from/record_to：映射在系统中的记录期（半开区间），按修订落库时间开闭；
- FOREVER 表示至今。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .storage import FOREVER, decode, encode, transaction


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _overlaps(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    return a_from < b_to and b_from < a_to


def ensure_person(connection, person_id: str, revision_id: str | None = None,
                  status: str = "active") -> None:
    existing = connection.execute(
        "SELECT person_id FROM person WHERE person_id=?", (person_id,)
    ).fetchone()
    if not existing:
        connection.execute(
            "INSERT INTO person(person_id,status,merged_into,created_at,created_revision_id) "
            "VALUES(?,?,NULL,?,?)",
            (person_id, status, now_ts(), revision_id),
        )


def current_mapping_rows(connection, source: str, source_key: str) -> list:
    """该来源标识的全部映射版本（含已关闭版本）。

    当前认知下的解析按业务有效期与行状态判定；record_to 过滤仅用于 as-of 回看。
    """
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM identity_mapping WHERE source=? AND source_key=? "
            "ORDER BY valid_from",
            (source, source_key),
        )
    ]


def _overlapping_other_person(connection, source: str, source_key: str,
                              person_id: str, valid_from: str, valid_to: str) -> list:
    return [
        row
        for row in current_mapping_rows(connection, source, source_key)
        if row["record_to"] == FOREVER
        and row["status"] == "active"
        and row["person_id"] != person_id
        and _overlaps(valid_from, valid_to, row["valid_from"], row["valid_to"])
    ]


def register_conflict(connection, source: str, source_key: str, person_a: str,
                      person_b: str, revision_id: str,
                      valid_from: str | None = None, valid_to: str | None = None) -> str:
    """登记一标多主冲突。冲突不自动二选一，事实解析时进入待查池。"""
    conflict_id = _new_id("conflict")
    connection.execute(
        "INSERT INTO mapping_conflict(conflict_id,source,source_key,person_a,person_b,"
        "valid_from,valid_to,detected_revision_id,status) VALUES(?,?,?,?,?,?,?,?, 'open')",
        (conflict_id, source, source_key, person_a, person_b, valid_from, valid_to, revision_id),
    )
    return conflict_id


def resolve_conflict(connection, conflict_id: str, revision_id: str) -> None:
    connection.execute(
        "UPDATE mapping_conflict SET status='resolved', resolved_revision_id=? WHERE conflict_id=?",
        (revision_id, conflict_id),
    )


def _close_row(connection, row: dict, ts: str, status: str, valid_to: str | None = None) -> None:
    connection.execute(
        "UPDATE identity_mapping SET record_to=?, status=?, valid_to=? WHERE mapping_id=?",
        (ts, status, valid_to or row["valid_to"], row["mapping_id"]),
    )


def _open_row(connection, revision_id: str, source: str, source_key: str, person_id: str,
              valid_from: str, valid_to: str, ts: str, mapping_id: str | None = None) -> str:
    mapping_id = mapping_id or _new_id("map")
    connection.execute(
        "INSERT INTO identity_mapping(mapping_id,revision_id,source,source_key,person_id,"
        "valid_from,valid_to,record_from,record_to,status) VALUES(?,?,?,?,?,?,?,?,?, 'active')",
        (mapping_id, revision_id, source, source_key, person_id,
         valid_from, valid_to, ts, FOREVER),
    )
    return mapping_id


def _insert_revision(connection, action: str, reason: str, payload: dict,
                     created_by: str, revision_id: str | None, ts: str) -> str:
    revision_id = revision_id or _new_id("rev")
    connection.execute(
        "INSERT INTO identity_revision(revision_id,action,reason,payload,created_at,created_by) "
        "VALUES(?,?,?,?,?,?)",
        (revision_id, action, reason, encode(payload), ts, created_by),
    )
    return revision_id


def _reresolve(connection, keys: set[tuple[str, str]], reason: str,
               revision_id: str, ts: str) -> None:
    # 延迟导入，避免 identity 与 attribution 循环依赖
    from .attribution import attribute_fact

    for source, source_key in sorted(keys):
        fact = connection.execute(
            "SELECT payload FROM facts WHERE source=? AND business_key=?",
            (source, source_key),
        ).fetchone()
        if not fact:
            continue
        occurred_at = decode(fact["payload"])["occurred_at"]
        attribute_fact(connection, source, source_key, occurred_at,
                       reason=reason, revision_id=revision_id, now_ts=ts)


# ---------- 各类修订 ----------

def _create(connection, rev: str, p: dict, ts: str) -> set:
    source, key = p["source"], p["source_key"]
    person_id, valid_from = p["person_id"], p["valid_from"]
    valid_to = p.get("valid_to", FOREVER)
    ensure_person(connection, person_id, rev)
    overlap = _overlapping_other_person(connection, source, key, person_id, valid_from, valid_to)
    if overlap:
        raise ValueError(
            f"来源标识 {source}/{key} 在重叠有效期已归属他人，须先发起修正或冲突核验"
        )
    _open_row(connection, rev, source, key, person_id, valid_from, valid_to, ts,
              p.get("mapping_id"))
    return {(source, key)}


def _correct(connection, rev: str, p: dict, ts: str) -> set:
    source, key = p["source"], p["source_key"]
    from_person, to_person = p["from_person"], p["to_person"]
    valid_from = p["valid_from"]
    rows = [r for r in current_mapping_rows(connection, source, key)
            if r["status"] == "active" and r["person_id"] == from_person]
    if not rows:
        raise ValueError(f"待修正映射不存在: {source}/{key} -> {from_person}")
    ensure_person(connection, to_person, rev)
    touched = set()
    for row in rows:
        valid_to = min(row["valid_to"], p.get("valid_to", FOREVER))
        _close_row(connection, row, ts, "superseded", valid_to=min(valid_to, valid_from))
        new_id = _open_row(connection, rev, source, key, to_person,
                           valid_from, row["valid_to"], ts)
        touched.add((source, key))
        others = _overlapping_other_person(connection, source, key, to_person,
                                           valid_from, row["valid_to"])
        for other in others:
            register_conflict(connection, source, key, to_person, other["person_id"],
                              rev, valid_from, other["valid_to"])
        # 新行自身也纳入 touched（new_id 同键，集合已覆盖）
    return touched


def _revoke(connection, rev: str, p: dict, ts: str) -> set:
    source, key = p["source"], p["source_key"]
    rows = [r for r in current_mapping_rows(connection, source, key) if r["status"] == "active"]
    if not rows:
        raise ValueError(f"待撤销映射不存在: {source}/{key}")
    for row in rows:
        _close_row(connection, row, ts, "revoked")
    return {(source, key)}


def _merge(connection, rev: str, p: dict, ts: str) -> set:
    keep, absorbed = p["keep_person_id"], p["absorbed_person_id"]
    if keep == absorbed:
        raise ValueError("不能合并同一实体")
    target = connection.execute("SELECT * FROM person WHERE person_id=?", (absorbed,)).fetchone()
    if not target or target["status"] != "active":
        raise ValueError(f"被合并实体不存在或非活跃: {absorbed}")
    ensure_person(connection, keep, rev)
    connection.execute(
        "UPDATE person SET status='merged', merged_into=? WHERE person_id=?",
        (keep, absorbed),
    )
    touched: set[tuple[str, str]] = set()
    rows = [r for r in connection.execute(
        "SELECT * FROM identity_mapping WHERE person_id=? AND record_to=?",
        (absorbed, FOREVER),
    ).fetchall()]
    for row in rows:
        row = dict(row)
        _close_row(connection, row, ts, "superseded")
        _open_row(connection, rev, row["source"], row["source_key"], keep,
                  row["valid_from"], row["valid_to"], ts)
        touched.add((row["source"], row["source_key"]))
        others = _overlapping_other_person(connection, row["source"], row["source_key"],
                                           keep, row["valid_from"], row["valid_to"])
        for other in others:
            register_conflict(connection, row["source"], row["source_key"],
                              keep, other["person_id"], rev,
                              row["valid_from"], other["valid_to"])
    return touched


def _split(connection, rev: str, p: dict, ts: str) -> set:
    from_person, new_person = p["from_person"], p["new_person_id"]
    ensure_person(connection, new_person, rev, status="active")
    touched: set[tuple[str, str]] = set()
    for item in p["assignments"]:
        source, key, valid_from = item["source"], item["source_key"], item["valid_from"]
        rows = [r for r in current_mapping_rows(connection, source, key)
                if r["status"] == "active" and r["person_id"] == from_person]
        if not rows:
            raise ValueError(f"拆分来源标识在原实体下无有效映射: {source}/{key}")
        for row in rows:
            _close_row(connection, row, ts, "superseded",
                       valid_to=min(row["valid_to"], valid_from))
            _open_row(connection, rev, source, key, new_person,
                      valid_from, row["valid_to"], ts)
            touched.add((source, key))
    return touched


_ACTIONS = {
    "create": _create,
    "correct": _correct,
    "revoke": _revoke,
    "merge": _merge,
    "split": _split,
}


def apply_revision(connection, action: str, payload: dict, reason: str,
                   created_by: str = "manual-review",
                   revision_id: str | None = None) -> str:
    """登记并执行一次人工核验修订，返回修订单号。

    修订在单事务内完成：写修订单 -> 开闭映射版本 -> 重算受影响事实的归属。
    """
    if action not in _ACTIONS:
        raise ValueError(f"不支持的修订动作: {action}")
    ts = now_ts()
    with transaction(connection):
        rev = _insert_revision(connection, action, reason, payload,
                               created_by, revision_id, ts)
        touched = _ACTIONS[action](connection, rev, payload, ts)
        _reresolve(connection, touched, reason=f"revision:{rev}",
                   revision_id=rev, ts=ts)
        return rev


# ---------- 归属解析 ----------

def _rows_at_record_time(connection, source: str, source_key: str,
                         as_of: str | None) -> list[dict]:
    if as_of is None:
        return current_mapping_rows(connection, source, source_key)
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM identity_mapping WHERE source=? AND source_key=? "
            "AND record_from<=? AND record_to>? ORDER BY valid_from",
            (source, source_key, as_of, as_of),
        )
    ]


def _resolve(rows: list[dict], occurred_at: str, merged_persons: set[str] | None = None) -> dict:
    hit = [r for r in rows if r["valid_from"] <= occurred_at < r["valid_to"]]
    if not hit:
        return {"resolution": "unmapped", "person_id": None, "mapping_id": None}
    # superseded 是被新版本取代的旧版本：在自身业务有效期内仍可解析历史事实；
    # 仅 revoked 表示身份依据被核验撤销。
    usable = [r for r in hit if r["status"] in ("active", "superseded")]
    # 已合并实体的旧映射由吸收方承接，不再作为独立候选
    merged_persons = merged_persons or set()
    usable = [r for r in usable if r["person_id"] not in merged_persons]
    persons = {r["person_id"] for r in usable}
    if len(persons) > 1:
        return {"resolution": "conflict", "person_id": None,
                "mapping_id": None, "candidate_persons": sorted(persons)}
    if len(persons) == 1:
        chosen = sorted(usable, key=lambda r: r["valid_from"])[-1]
        return {"resolution": "attributed", "person_id": chosen["person_id"],
                "mapping_id": chosen["mapping_id"]}
    return {"resolution": "revoked", "person_id": None, "mapping_id": None}


def _merged_persons(connection) -> set[str]:
    return {
        row["person_id"]
        for row in connection.execute(
            "SELECT person_id FROM person WHERE status='merged'"
        )
    }


def resolve_current(connection, source: str, source_key: str,
                    occurred_at: str) -> dict:
    """按系统当前认知解析事实归属。"""
    return _resolve(
        _rows_at_record_time(connection, source, source_key, None),
        occurred_at,
        _merged_persons(connection),
    )


def resolve_as_of(connection, source: str, source_key: str,
                  occurred_at: str, as_of: str) -> dict:
    """按某修订时点的认知回看事实归属（冻结快照的理论基础）。"""
    merged = set()
    for mapping in _rows_at_record_time(connection, source, source_key, as_of):
        person = connection.execute(
            "SELECT status FROM person WHERE person_id=?",
            (mapping["person_id"],),
        ).fetchone()
        if person and person["status"] == "merged":
            merged.add(mapping["person_id"])
    return _resolve(
        _rows_at_record_time(connection, source, source_key, as_of),
        occurred_at,
        merged,
    )


def list_revisions(connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT revision_id,action,reason,created_at,created_by,payload "
            "FROM identity_revision ORDER BY created_at"
        )
    ]


def frozen_change_allowed(period: str, frozen_before: str) -> bool:
    """期间不早于冻结边界时允许变动（保留基线工具函数）。"""
    return period >= frozen_before
