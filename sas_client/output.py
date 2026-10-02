from __future__ import annotations

import json
import sys
from typing import Any, Iterable


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def table(rows: Iterable[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    values = [[_display(row.get(key)) for key, _label in columns] for row in rows]
    headers = [label for _key, label in columns]
    widths = [len(header) for header in headers]
    for row in values:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    rule = "  ".join("-" * width for width in widths)
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)), rule]
    lines.extend("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in values)
    return "\n".join(lines)


def emit(value: Any, output: str = "table",
         columns: list[tuple[str, str]] | None = None) -> None:
    if output == "json":
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
        return
    rows = value if isinstance(value, list) else [value]
    if columns and all(isinstance(item, dict) for item in rows):
        print(table(rows, columns))
    elif isinstance(value, str):
        sys.stdout.write(value)
        if value and not value.endswith("\n"):
            sys.stdout.write("\n")
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
