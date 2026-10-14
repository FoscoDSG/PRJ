"""CSV connector. The watermark is the file digest, so a reload is idempotent."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any

from services.ingest.base import rows_from_dicts
from services.text import stable


def read_csv(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    raw = file_path.read_bytes()
    watermark = hashlib.sha256(raw).hexdigest()
    with file_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        records = []
        for row in reader:
            records.append({key: _cell(value) for key, value in row.items() if key is not None})
    columns, rows = rows_from_dicts(records)
    return {"columns": columns, "rows": rows, "watermark": watermark, "source_kind": "csv"}


def _cell(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return stable(text)
