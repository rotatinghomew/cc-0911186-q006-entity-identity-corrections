import tempfile
import unittest
from pathlib import Path

from lakeapp.attribution import current_attribution, open_suspense
from lakeapp.contract import parse_event
from lakeapp.derive import apply_events
from lakeapp.freeze import freeze_report, frozen_basis, recompute_report
from lakeapp.identity import (
    apply_revision,
    ensure_person,
    register_conflict,
    resolve_conflict,
)
from lakeapp.ingest import receive
from lakeapp.query import explain_identity, revision_impact
from lakeapp.storage import FOREVER, open_store


def ingest(conn, source, batch_id, cursor, key, occurred_at, value=1.0, campus="C1"):
    records = [{
        "business_key": key, "kind": "upsert", "value": value,
        "occurred_at": occurred_at, "campus_id": campus,
    }]
    events = receive(conn, source, batch_id, cursor, records)
    apply_events(conn, events)
    return events


class IdentityMappingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.conn = open_store(Path(self.tempdir.name) / "data.db")

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def _bind(self, source, key, person, valid_from, revision_id=None):
        return apply_revision(
            self.conn, "create",
            {"source": source, "source_key": key, "person_id": person,
             "valid_from": valid_from},
            reason="initial-bind", revision_id=revision_id,
        )

    def test_unmapped_fact_enters_suspense_instead_of_disappearing(self):
        ingest(self.conn, "enrollment", "b1", 1, "ext-1", "2026-09-10T08:00:00")
        attr = current_attribution(self.conn, "enrollment", "ext-1")
        self.assertEqual(attr["resolution"], "unmapped")
        self.assertIsNone(attr["person_id"])
        self.assertEqual(len(open_suspense(self.conn)), 1)
        # 事实本身仍然存在
        rows = self.conn.execute("SELECT 1 FROM facts WHERE business_key='ext-1'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_correction_recomputes_open_period_and_keeps_history(self):
        self._bind("enrollment", "ext-1", "P1", "2026-01-01T00:00:00", "rev-1")
        ingest(self.conn, "enrollment", "b1", 1, "ext-1", "2026-09-10T08:00:00")
        self.assertEqual(
            current_attribution(self.conn, "enrollment", "ext-1")["person_id"], "P1")

        apply_revision(
            self.conn, "correct",
            {"source": "enrollment", "source_key": "ext-1",
             "from_person": "P1", "to_person": "P2",
             "valid_from": "2026-09-01T00:00:00"},
            reason="manual-review-found-wrong-person", revision_id="rev-2",
        )
        current = current_attribution(self.conn, "enrollment", "ext-1")
        self.assertEqual(current["person_id"], "P2")
        self.assertEqual(current["revision_id"], "rev-2")

        # 旧归属版本仍在，可回放“当时归 P1”
        lineage = explain_identity(self.conn, "enrollment", "ext-1")
        resolutions = [h["resolution"] + ":" + str(h["person_id"])
                       for h in lineage["attribution_history"]]
        self.assertIn("attributed:P1", resolutions)
        self.assertIn("attributed:P2", resolutions)
        self.assertEqual({r["revision_id"] for r in lineage["revisions"]},
                         {"rev-1", "rev-2"})

        impact = revision_impact(self.conn, "rev-2")
        self.assertEqual(impact["impacted_fact_count"], 1)
        self.assertTrue(impact["items"][0]["recompute_allowed"])
        self.assertFalse(impact["items"][0]["frozen"])

    def test_frozen_report_keeps_identity_basis_while_open_period_recomputes(self):
        self._bind("payment", "pay-1", "P1", "2026-01-01T00:00:00", "rev-1")
        ingest(self.conn, "payment", "b1", 1, "pay-1", "2026-08-15T10:00:00", value=100.0)
        freeze_report(self.conn, "monthly", "2026-08")
        basis = frozen_basis(self.conn, "monthly", "2026-08")
        self.assertEqual(basis[0]["person_id"], "P1")
        self.assertEqual(basis[0]["revision_id"], "rev-1")

        # 冻结后修正映射
        apply_revision(
            self.conn, "correct",
            {"source": "payment", "source_key": "pay-1",
             "from_person": "P1", "to_person": "P2",
             "valid_from": "2026-08-01T00:00:00"},
            reason="late-review", revision_id="rev-2",
        )
        # 冻结期拒绝重算
        with self.assertRaises(ValueError):
            recompute_report(self.conn, "monthly", "2026-08")
        # 冻结依据仍是 P1 / rev-1
        basis_after = frozen_basis(self.conn, "monthly", "2026-08")
        self.assertEqual(basis_after[0]["person_id"], "P1")
        self.assertEqual(basis_after[0]["revision_id"], "rev-1")

        # 同标识在未冻结期间(9月)的事实按新归属重算
        ingest(self.conn, "payment", "b2", 2, "pay-2", "2026-09-15T10:00:00", value=50.0)
        self._bind("payment", "pay-2", "P2", "2026-01-01T00:00:00", "rev-3")
        # 重新触发 pay-2 归属
        from lakeapp.attribution import attribute_fact
        attribute_fact(self.conn, "payment", "pay-2", "2026-09-15T10:00:00")
        result = recompute_report(self.conn, "monthly", "2026-09")
        self.assertEqual(result["summary"]["headcount"], 1)

        impact = revision_impact(self.conn, "rev-2")
        self.assertEqual(impact["frozen_impact_count"], 1)
        self.assertEqual(impact["items"][0]["frozen"], True)
        self.assertFalse(impact["items"][0]["recompute_allowed"])

    def test_reused_source_key_resolves_by_occurred_time(self):
        # 同一终端编号先给 P1，后复用给 P2，业务有效期不重叠
        self._bind("attendance", "dev-9", "P1", "2026-01-01T00:00:00", "rev-1")
        apply_revision(
            self.conn, "correct",
            {"source": "attendance", "source_key": "dev-9",
             "from_person": "P1", "to_person": "P2",
             "valid_from": "2026-09-01T00:00:00"},
            reason="device-reassigned", revision_id="rev-2",
        )
        ingest(self.conn, "attendance", "b1", 1, "dev-9", "2026-08-20T09:00:00")
        ingest(self.conn, "attendance", "b2", 2, "dev-9", "2026-09-20T09:00:00")
        # facts 以 business_key 为主键，两条同键事实会 upsert 成一条；
        # 这里直接验证解析器对不同发生时间的判定
        from lakeapp.identity import resolve_current
        old = resolve_current(self.conn, "attendance", "dev-9", "2026-08-20T09:00:00")
        new = resolve_current(self.conn, "attendance", "dev-9", "2026-09-20T09:00:00")
        self.assertEqual(old["person_id"], "P1")
        self.assertEqual(new["person_id"], "P2")

    def test_conflicting_mappings_go_to_suspense_and_never_auto_merge(self):
        self._bind("enrollment", "ext-7", "P1", "2026-01-01T00:00:00", "rev-1")
        # 人为登记一标多主冲突（两个真实学习者不能被系统自动合并）
        ensure_person(self.conn, "P3", "rev-2")
        register_conflict(self.conn, "enrollment", "ext-7", "P1", "P3",
                          "rev-2", valid_from="2026-09-01T00:00:00",
                          valid_to=FOREVER)
        # 制造重叠映射：直接通过 create 之外的路径——撤销后重新挂 P3 与 P1 共存
        apply_revision(
            self.conn, "create",
            {"source": "enrollment", "source_key": "ext-8", "person_id": "P1",
             "valid_from": "2026-01-01T00:00:00"},
            reason="bind", revision_id="rev-3",
        )
        # 重叠有效期绑定不同 person 必须被拒绝
        with self.assertRaises(ValueError):
            apply_revision(
                self.conn, "create",
                {"source": "enrollment", "source_key": "ext-8", "person_id": "P3",
                 "valid_from": "2026-06-01T00:00:00"},
                reason="duplicate-bind", revision_id="rev-4",
            )
        # 冲突登记仍在，且为 open；人工解决前不合并
        conflicts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM mapping_conflict WHERE status='open'")]
        self.assertTrue(any(c["person_b"] == "P3" for c in conflicts))
        resolve_conflict(self.conn, conflicts[0]["conflict_id"], "rev-9")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM mapping_conflict WHERE conflict_id=?",
                (conflicts[0]["conflict_id"],)).fetchone()["status"],
            "resolved",
        )

    def test_revoked_mapping_keeps_fact_in_suspense(self):
        self._bind("payment", "pay-9", "P1", "2026-01-01T00:00:00", "rev-1")
        ingest(self.conn, "payment", "b1", 1, "pay-9", "2026-09-10T08:00:00")
        self.assertEqual(
            current_attribution(self.conn, "payment", "pay-9")["resolution"],
            "attributed")
        apply_revision(
            self.conn, "revoke",
            {"source": "payment", "source_key": "pay-9"},
            reason="identity-unverifiable", revision_id="rev-2",
        )
        attr = current_attribution(self.conn, "payment", "pay-9")
        self.assertEqual(attr["resolution"], "revoked")
        self.assertIsNone(attr["person_id"])
        # 事实未消失，且在待查池中
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM facts WHERE business_key='pay-9'").fetchone())
        self.assertEqual(len(open_suspense(self.conn)), 1)

        # 重新核验确认归属后，事实可恢复归属并清出待查
        apply_revision(
            self.conn, "create",
            {"source": "payment", "source_key": "pay-9", "person_id": "P1",
             "valid_from": "2026-01-01T00:00:00"},
            reason="re-verified", revision_id="rev-3",
        )
        from lakeapp.attribution import attribute_fact
        attribute_fact(self.conn, "payment", "pay-9", "2026-09-10T08:00:00",
                       reason="manual", revision_id="rev-3")
        self.assertEqual(
            current_attribution(self.conn, "payment", "pay-9")["person_id"], "P1")
        self.assertEqual(open_suspense(self.conn), [])

    def test_manual_merge_is_explicit_and_reroutes_facts(self):
        self._bind("enrollment", "e-1", "P1", "2026-01-01T00:00:00", "rev-1")
        self._bind("payment", "p-1", "P2", "2026-01-01T00:00:00", "rev-2")
        ingest(self.conn, "enrollment", "b1", 1, "e-1", "2026-09-01T08:00:00")
        ingest(self.conn, "payment", "b2", 2, "p-1", "2026-09-02T08:00:00")
        apply_revision(
            self.conn, "merge",
            {"keep_person_id": "P1", "absorbed_person_id": "P2"},
            reason="confirmed-same-learner", revision_id="rev-3",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM person WHERE person_id='P2'")
            .fetchone()["status"],
            "merged",
        )
        self.assertEqual(
            current_attribution(self.conn, "payment", "p-1")["person_id"], "P1")
        impact = revision_impact(self.conn, "rev-3")
        self.assertEqual(impact["impacted_fact_count"], 1)
        self.assertEqual(impact["items"][0]["from_person_id"], "P2")
        self.assertEqual(impact["items"][0]["to_person_id"], "P1")

    def test_split_assigns_distinct_periods_to_new_person(self):
        self._bind("attendance", "d-1", "P1", "2026-01-01T00:00:00", "rev-1")
        apply_revision(
            self.conn, "split",
            {"from_person": "P1", "new_person_id": "P9",
             "assignments": [
                 {"source": "attendance", "source_key": "d-1",
                  "valid_from": "2026-07-01T00:00:00"},
             ]},
            reason="two-real-learners", revision_id="rev-2",
        )
        from lakeapp.identity import resolve_current
        self.assertEqual(
            resolve_current(self.conn, "attendance", "d-1",
                            "2026-06-30T23:00:00")["person_id"],
            "P1")
        self.assertEqual(
            resolve_current(self.conn, "attendance", "d-1",
                            "2026-07-02T08:00:00")["person_id"],
            "P9")

    def test_explain_identity_lineage_is_complete(self):
        self._bind("enrollment", "ext-1", "P1", "2026-01-01T00:00:00", "rev-1")
        ingest(self.conn, "enrollment", "b1", 1, "ext-1", "2026-09-10T08:00:00")
        freeze_report(self.conn, "monthly", "2026-09")
        lineage = explain_identity(self.conn, "enrollment", "ext-1")
        self.assertIsNotNone(lineage["fact"])
        self.assertTrue(lineage["attribution_history"])
        self.assertTrue(lineage["mapping_versions"])
        self.assertTrue(lineage["revisions"])
        self.assertEqual(lineage["frozen_basis"][0]["person_id"], "P1")
        # 修订载荷可回放
        self.assertEqual(lineage["revisions"][0]["payload"]["person_id"], "P1")


if __name__ == "__main__":
    unittest.main()
