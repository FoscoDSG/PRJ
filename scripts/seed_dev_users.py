"""Seed local users from the environment. Development only.

Passwords are read from IEOL_SEED_<ROLE>_PASSWORD. This file stores none.
"""

from __future__ import annotations

import os
import sys

from ontology.loader import load_merged
from services.db import ROLES, connect, ensure_ready


def main() -> None:
    if os.environ.get("IEOL_DEV") != "1":
        print(
            "Seed di sviluppo rifiutato. Imposta IEOL_DEV=1. "
            "In produzione gli utenti si creano all'avvio dalle variabili IEOL_SEED_*_PASSWORD.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    missing = [role for role in ROLES if not os.environ.get(f"IEOL_SEED_{role.upper()}_PASSWORD")]
    if missing:
        names = ", ".join(f"IEOL_SEED_{role.upper()}_PASSWORD" for role in missing)
        print(f"Variabili password assenti: {names}", file=sys.stderr)
        raise SystemExit(1)
    model = load_merged()
    with connect() as conn:
        ensure_ready(conn, model.packages)
    print("Utenti di sviluppo verificati. Le password restano solo nell'ambiente.")


if __name__ == "__main__":
    main()
