"""Offline structural checks for committed PostgreSQL migration files.

This intentionally does not claim to replace parsing/execution by PostgreSQL.
It catches ordering, delimiter, destructive-legacy-table, extension, and obvious
credential mistakes without opening a database connection.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


FILENAME_RE = re.compile(r"^(\d{14})_[a-z0-9_]+\.sql$")
FORBIDDEN_PATTERNS = (
    re.compile(r"\bdrop\s+table\s+(?:public\.)?listings\b", re.IGNORECASE),
    re.compile(r"\balter\s+table\s+(?:public\.)?listings\b", re.IGNORECASE),
    re.compile(r"\btruncate\s+(?:table\s+)?(?:public\.)?listings\b", re.IGNORECASE),
    re.compile(r"postgres(?:ql)?://", re.IGNORECASE),
)

POSTGIS_EXTENSION_RE = re.compile(
    r"^\s*create\s+extension\s+if\s+not\s+exists\s+postgis\s*;\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _code_without_comments_or_strings(sql: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(sql):
        if not in_string and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = len(sql) if newline < 0 else newline + 1
            output.append("\n")
            continue
        character = sql[index]
        if character == "'":
            if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
            output.append(" ")
        elif not in_string:
            output.append(character)
        index += 1
    if in_string:
        raise ValueError("unterminated SQL string literal")
    return "".join(output)


def check_migrations(migrations_dir: Path) -> list[Path]:
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise ValueError(f"No SQL migrations found in {migrations_dir}")
    timestamps: set[str] = set()
    for path in files:
        match = FILENAME_RE.fullmatch(path.name)
        if not match:
            raise ValueError(f"Invalid migration filename: {path.name}")
        if match.group(1) in timestamps:
            raise ValueError(f"Duplicate migration timestamp: {match.group(1)}")
        timestamps.add(match.group(1))
        sql = path.read_text(encoding="utf-8")
        if not sql.rstrip().endswith(";"):
            raise ValueError(f"Migration must end with a semicolon: {path.name}")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(sql):
                raise ValueError(
                    f"Forbidden migration content in {path.name}: {pattern.pattern}"
                )
        extensions = re.findall(r"\bcreate\s+extension\b[^;]*;", sql, re.IGNORECASE)
        if any(not POSTGIS_EXTENSION_RE.fullmatch(extension) for extension in extensions):
            raise ValueError(f"Forbidden extension in {path.name}; only PostGIS is allowed")
        code = _code_without_comments_or_strings(sql)
        balance = 0
        for character in code:
            if character == "(":
                balance += 1
            elif character == ")":
                balance -= 1
                if balance < 0:
                    raise ValueError(f"Unbalanced parentheses in {path.name}")
        if balance:
            raise ValueError(f"Unbalanced parentheses in {path.name}")
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir", type=Path, default=Path("supabase/migrations")
    )
    args = parser.parse_args()
    files = check_migrations(args.migrations_dir)
    print(f"Migration structural checks passed: {len(files)} file(s)")


if __name__ == "__main__":
    main()
