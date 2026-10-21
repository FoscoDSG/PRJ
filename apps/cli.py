"""Command line slice: migrations, fixture, recommendation, dry-run, and evidence expiry."""

from __future__ import annotations

import argparse
import json

from ontology.loader import load_merged
from services.db import (
    ROLES,
    app_connect,
    app_database_url,
    connect,
    database_url,
    ensure_ready,
    reset_password,
)
from services.migrate import (
    database_name,
    demo_database_url,
    ensure_database,
    migrate,
    require_migrated,
    reset_demo_database,
)
from services.companies import assign_user_company, create_company
from services.errors import AppError
from services.pii import redact
from services.pipeline import adjust_on_hand, approve, execute_recommendation, load_fixtures
from services.queries import list_inbox
from services.testset import build_testset, evaluate_testset
from services.text import stable
from services.workflow.engine import expire_stale


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetta operativa dell'order management")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_cmd = sub.add_parser(
        "migrate",
        help="Applica le migrazioni pendenti sul database di DATABASE_URL",
    )
    migrate_cmd.add_argument(
        "--rotate-evidence-key",
        action="store_true",
        help="Sostituisce l'unica riga di app.evidence_hmac_key con IEOL_EVIDENCE_HMAC_KEY",
    )
    reset = sub.add_parser(
        "reset-password",
        help="Reimposta la password di un ruolo da IEOL_SEED_<RUOLO>_PASSWORD",
    )
    reset.add_argument("role", choices=ROLES)
    sub.add_parser(
        "expire-stale",
        help="Segna scadute le raccomandazioni con evidenze cambiate, sul database di IEOL_APP_DATABASE_URL",
    )
    demo = sub.add_parser(
        "demo",
        help="Carica la fixture nel database dedicato della demo, propone e fa il dry-run",
    )
    demo.add_argument(
        "--reset",
        action="store_true",
        help="Cancella gli schemi app e src solo nel database della demo. Le migrazioni si applicano con ieol migrate",
    )
    sub.add_parser("expire", help="Nel database della demo, cambia la giacenza e fa scadere la raccomandazione")
    sub.add_parser("execute", help="Nel database della demo, esegue il dry-run già approvato, in modo idempotente")
    company = sub.add_parser(
        "company",
        help="Crea un'azienda o assegna un utente. Solo da qui, come owner di DATABASE_URL. Non c'è una pagina né un'API.",
    )
    company_commands = company.add_subparsers(dest="company_command", required=True)
    create = company_commands.add_parser(
        "create",
        help="Crea un'azienda. Un'azienda pilota si crea solo dopo la firma dell'accordo sui dati: il comando non verifica la firma.",
    )
    create.add_argument("--name", required=True)
    create.add_argument(
        "--extension",
        default=None,
        help="Pacchetto di estensione di questa azienda. Senza questo argomento l'azienda non ne ha.",
    )
    create_demo = company_commands.add_parser(
        "create-demo",
        help="Crea l'azienda demo e imposta is_demo. Il nome non decide l'esclusione dalle misure.",
    )
    create_demo.add_argument("--name", required=True)
    create_demo.add_argument("--extension", default=None)
    assign = company_commands.add_parser(
        "assign",
        help="Assegna l'azienda a un utente che non ne ha una. Un secondo assegnamento è rifiutato.",
    )
    assign.add_argument("--username", required=True)
    assign.add_argument("--company-id", required=True)
    testset = sub.add_parser("testset", help="Costruisce o valuta il test set etichettato")
    testset_commands = testset.add_subparsers(dest="testset_command", required=True)
    testset_commands.add_parser("build", help="Congela i casi da decisioni umane e dalla fixture manoscritta")
    testset_commands.add_parser("eval", help="Calcola n, errori e intervalli, e registra la soglia")
    args = parser.parse_args(argv)
    if args.command == "migrate":
        try:
            with connect() as conn:
                applied = migrate(conn, rotate_evidence_key=args.rotate_evidence_key)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"applied": applied}, ensure_ascii=False))
        return
    if args.command == "reset-password":
        try:
            with connect() as conn:
                reset_password(conn, args.role)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"role": args.role, "reset": True}, ensure_ascii=False))
        return
    if args.command == "expire-stale":
        try:
            with app_connect() as conn:
                require_migrated(conn)
                expired = expire_stale(conn)
                conn.commit()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"expired": expired}, ensure_ascii=False))
        return
    if args.command == "testset":
        try:
            with app_connect() as conn:
                require_migrated(conn)
                if args.testset_command == "build":
                    result = build_testset(conn)
                else:
                    result = evaluate_testset(conn)
                conn.commit()
        except (RuntimeError, AppError) as exc:
            message = exc.message if isinstance(exc, AppError) else str(exc)
            raise SystemExit(message) from exc
        print(json.dumps(redact(stable(result)), ensure_ascii=False))
        return
    if args.command == "company":
        try:
            with connect() as conn:
                require_migrated(conn)
                if args.company_command == "create":
                    result = create_company(conn, args.name, demo=False, extension=args.extension)
                elif args.company_command == "create-demo":
                    result = create_company(conn, args.name, demo=True, extension=args.extension)
                else:
                    result = assign_user_company(conn, args.username, args.company_id)
                conn.commit()
        except (RuntimeError, AppError) as exc:
            message = exc.message if isinstance(exc, AppError) else str(exc)
            raise SystemExit(message) from exc
        print(json.dumps(result, ensure_ascii=False))
        return
    model = load_merged()
    try:
        main_url = database_url()
        demo_url = demo_database_url(main_url)
        ensure_database(demo_url)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if args.command == "demo" and args.reset:
        with connect(demo_url) as conn:
            try:
                reset_demo_database(
                    conn,
                    main_dbname=database_name(main_url),
                    expected_dbname=database_name(demo_url),
                )
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
    with app_connect(app_database_url(database_name(demo_url))) as conn:
        try:
            if args.command == "demo":
                ensure_ready(conn, model.packages)
            else:
                require_migrated(conn)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if args.command == "demo":
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
                preview = approve(
                    conn,
                    item["id"],
                    actor="approver",
                    role="approver",
                    evidence_hash=item["evidence_hash"],
                    allow_missing_company=True,
                )
            conn.commit()
            print(
                json.dumps(
                    redact(
                        stable(
                            {
                                "shortage_order": item["order_key"],
                                "problem": item["problem"],
                                "requires_cfo": item["requires_cfo"],
                                "recommendations": summary["recommendations"],
                                "dry_run": preview["dry_run"],
                            }
                        )
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.command == "expire":
            expired = adjust_on_hand(conn, model, "ART-X", 500, role="admin")
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
