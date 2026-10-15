"""Command line slice: fixture, recommendation, dry-run, and evidence expiry."""

from __future__ import annotations

import argparse
import json

from ontology.loader import load_merged
from services.db import apply_schema, connect, ensure_ready, migrate
from services.pipeline import adjust_on_hand, approve, execute_recommendation, load_fixtures
from services.queries import list_inbox
from services.text import stable


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetta operativa dell'order management")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Carica la fixture su un database già migrato")
    demo.add_argument("--reset", action="store_true", help="Distrugge i dati e ricrea il database. Non usarlo se i dati vanno conservati")
    sub.add_parser("migrate", help="Applica le migrazioni. Una seconda esecuzione non cambia nulla")
    sub.add_parser("expire", help="Cambia la giacenza e fa scadere la raccomandazione")
    sub.add_parser("execute", help="Esegue il dry-run già approvato, in modo idempotente")
    args = parser.parse_args(argv)
    model = load_merged()
    with connect() as conn:
        if args.command == "migrate":
            applied = migrate(conn)
            print(json.dumps({"applied": applied}))
            return
        if args.command == "demo":
            if args.reset:
                apply_schema(conn, seed_demo=True)
            else:
                ensure_ready(conn, model.packages)
            summary = load_fixtures(conn, actor="mapper", role="mapper", model=model)
            inbox = list_inbox(conn, "approver")
            if not inbox:
                raise SystemExit("Nessuna raccomandazione: lo shortage non è stato rilevato")
            item = next((row for row in inbox if row["status"] in ("proposed", "modified")), None)
            if item is None:
                item = next((row for row in inbox if row["status"] == "dry_run_ok"), None)
                if item is None:
                    raise SystemExit("Nessuna raccomandazione: lo shortage non è stato rilevato")
                preview = {"dry_run": item["dry_run"]}
            else:
                preview = approve(conn, item["id"], actor="approver", role="approver")
            conn.commit()
            print(
                json.dumps(
                    stable(
                        {
                            "shortage_order": item["order_key"],
                            "problem": item["problem"],
                            "requires_cfo": item["requires_cfo"],
                            "recommendations": summary["recommendations"],
                            "dry_run": preview["dry_run"],
                        }
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.command == "expire":
            expired = adjust_on_hand(conn, model, "ART-X", 500)
            conn.commit()
            print(json.dumps({"expired": expired}, indent=2))
            return
        if args.command == "execute":
            inbox = list_inbox(conn, "approver")
            ready = next((item for item in inbox if item["status"] == "dry_run_ok"), None)
            if ready is None:
                raise SystemExit("Nessun dry-run da eseguire. Lancia prima ieol demo")
            first = execute_recommendation(conn, ready["id"], actor="approver", role="approver", model=model)
            second = execute_recommendation(conn, ready["id"], actor="approver", role="approver", model=model)
            conn.commit()
            print(
                json.dumps(
                    stable(
                        {
                            "event_id": first.get("event_id"),
                            "documents": len(first.get("documents") or []),
                            "second_replayed": bool(second.get("replayed")),
                        }
                    ),
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
