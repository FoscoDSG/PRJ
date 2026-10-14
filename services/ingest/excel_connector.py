"""Excel connector. Reads the first sheet with the same row contract as CSV."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from services.ingest.base import rows_from_dicts
from services.text import stable


def read_excel(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    watermark = hashlib.sha256(file_path.read_bytes()).hexdigest()
    workbook = load_workbook(file_path, read_only=True, data_only=True)
    sheet = workbook.active
    matrix = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not matrix:
        return {"columns": [], "rows": [], "watermark": watermark, "source_kind": "excel"}
    headers = [str(cell).strip() if cell is not None else "" for cell in matrix[0]]
    records = []
    for raw in matrix[1:]:
        if raw is None or all(cell is None or str(cell).strip() == "" for cell in raw):
            continue
        record = {}
        for header, cell in zip(headers, raw):
            if header:
                record[header] = None if cell is None else stable(cell)
        records.append(record)
    columns, rows = rows_from_dicts(records)
    return {"columns": columns, "rows": rows, "watermark": watermark, "source_kind": "excel"}
