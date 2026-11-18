from __future__ import annotations

import argparse
import json

from .attribution import open_suspense
from .derive import apply_events, facts
from .freeze import freeze_report, frozen_basis, recompute_report
from .identity import apply_revision, ensure_person, list_revisions
from .ingest import progress, receive
from .publish import publish, read_manifest
from .query import explain_identity, revision_impact
from .storage import open_store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地分层数据服务")
    parser.add_argument("database")
    parser.add_argument(
        "action",
        choices=[
            "receive", "facts", "progress", "publish", "read",
            "person", "revision", "revisions", "conflicts", "suspense",
            "explain", "impact", "freeze", "recompute", "frozen-basis",
        ],
    )
    parser.add_argument("payload", nargs="?")
    args = parser.parse_args(argv)
    connection = open_store(args.database)
    try:
        data = json.loads(args.payload or "{}")
        if args.action == "receive":
            events = receive(connection, data["source"], data["batch_id"], data["cursor"], data["records"])
            print(json.dumps({"received": len(events), "changed": apply_events(connection, events)}, ensure_ascii=False))
        elif args.action == "facts":
            print(json.dumps(facts(connection), ensure_ascii=False))
        elif args.action == "progress":
            print(json.dumps(progress(connection), ensure_ascii=False))
        elif args.action == "publish":
            publish(connection, data["name"], data["version"], facts(connection))
            print("ok")
        elif args.action == "read":
            print(json.dumps(read_manifest(connection, data["name"], data["version"]), ensure_ascii=False))
        elif args.action == "person":
            ensure_person(connection, data["person_id"])
            connection.commit()
            print("ok")
        elif args.action == "revision":
            rev = apply_revision(
                connection,
                data["action"],
                data["payload"],
                data.get("reason", ""),
                data.get("created_by", "manual-review"),
                data.get("revision_id"),
            )
            print(json.dumps({"revision_id": rev}, ensure_ascii=False))
        elif args.action == "revisions":
            print(json.dumps(list_revisions(connection), ensure_ascii=False))
        elif args.action == "conflicts":
            rows = [dict(r) for r in connection.execute(
                "SELECT * FROM mapping_conflict ORDER BY conflict_id")]
            print(json.dumps(rows, ensure_ascii=False))
        elif args.action == "suspense":
            print(json.dumps(open_suspense(connection), ensure_ascii=False))
        elif args.action == "explain":
            print(json.dumps(
                explain_identity(connection, data["source"], data["business_key"]),
                ensure_ascii=False))
        elif args.action == "impact":
            print(json.dumps(
                revision_impact(connection, data["revision_id"],
                                data.get("report_name", "monthly")),
                ensure_ascii=False))
        elif args.action == "freeze":
            print(json.dumps(
                freeze_report(connection, data["report_name"], data["period"],
                              data.get("campus_id", "*")),
                ensure_ascii=False))
        elif args.action == "recompute":
            print(json.dumps(
                recompute_report(connection, data["report_name"], data["period"],
                                 data.get("campus_id", "*")),
                ensure_ascii=False))
        else:
            print(json.dumps(
                frozen_basis(connection, data["report_name"], data["period"],
                             data.get("campus_id", "*")),
                ensure_ascii=False))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
