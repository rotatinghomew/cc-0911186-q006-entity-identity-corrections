from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_batches (
    source TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    cursor INTEGER NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (source, batch_id)
);
CREATE TABLE IF NOT EXISTS source_progress (
    source TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (source, business_key)
);
CREATE TABLE IF NOT EXISTS manifests (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
-- 统一学习者实体
CREATE TABLE IF NOT EXISTS person (
    person_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    merged_into TEXT,
    created_at TEXT NOT NULL,
    created_revision_id TEXT
);
-- 人工核验修订单（仅追加）
CREATE TABLE IF NOT EXISTS identity_revision (
    revision_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
-- 身份映射版本：业务有效期(valid_*) + 系统记录期(record_*)，双时间 SCD2
CREATE TABLE IF NOT EXISTS identity_mapping (
    mapping_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    person_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    record_from TEXT NOT NULL,
    record_to TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mapping_lookup
    ON identity_mapping(source, source_key);
-- 映射冲突登记（来源标识复用/一标多主）
CREATE TABLE IF NOT EXISTS mapping_conflict (
    conflict_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    person_a TEXT NOT NULL,
    person_b TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    detected_revision_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_revision_id TEXT
);
-- 事实归属解析轨迹（仅追加；当前版本 valid_to=FOREVER）
CREATE TABLE IF NOT EXISTS fact_attribution (
    attribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    person_id TEXT,
    mapping_id TEXT,
    resolution TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revision_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_attribution_fact
    ON fact_attribution(source, business_key);
-- 无法确认身份的事实待查池
CREATE TABLE IF NOT EXISTS suspense_entry (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    opened_revision_id TEXT,
    cleared_at TEXT,
    cleared_revision_id TEXT,
    cleared_person_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_suspense_open
    ON suspense_entry(source, business_key) WHERE cleared_at IS NULL;
-- 报表冻结范围
CREATE TABLE IF NOT EXISTS report_freeze (
    report_name TEXT NOT NULL,
    period TEXT NOT NULL,
    campus_id TEXT NOT NULL DEFAULT '*',
    frozen_at TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    manifest_version INTEGER NOT NULL,
    PRIMARY KEY (report_name, period, campus_id)
);
-- 冻结时逐行固化的身份依据
CREATE TABLE IF NOT EXISTS frozen_fact_basis (
    report_name TEXT NOT NULL,
    period TEXT NOT NULL,
    campus_id TEXT NOT NULL,
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    person_id TEXT,
    mapping_id TEXT,
    resolution TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    revision_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frozen_basis_fact
    ON frozen_fact_basis(source, business_key);
"""

FOREVER = "9999-12-31T23:59:59"


def open_store(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection):
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(value: str):
    return json.loads(value)
