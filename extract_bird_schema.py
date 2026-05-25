#!/usr/bin/env python3
"""Extract BIRD SQLite schemas as JSON and optional LLM prompt files."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_TRAIN_DATABASES = Path("/Users/nav/Downloads/train/train_databases")


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def read_description_csvs(database_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Read BIRD database_description CSVs keyed by table then original column."""
    description_dir = database_dir / "database_description"
    descriptions: dict[str, dict[str, dict[str, str]]] = {}

    if not description_dir.is_dir():
        return descriptions

    for csv_path in sorted(description_dir.glob("*.csv")):
        table_name = csv_path.stem
        table_descriptions: dict[str, dict[str, str]] = {}
        for row in read_csv_dicts(csv_path):
            original_column_name = (row.get("original_column_name") or "").strip()
            if not original_column_name:
                continue
            table_descriptions[original_column_name] = {
                "semantic_name": (row.get("column_name") or "").strip(),
                "description": (row.get("column_description") or "").strip(),
                "data_format": (row.get("data_format") or "").strip(),
                "value_description": (row.get("value_description") or "").strip(),
            }
        descriptions[table_name] = table_descriptions

    return descriptions


def read_csv_dicts(csv_path: Path) -> list[dict[str, str]]:
    encodings = ("utf-8-sig", "utf-16", "cp1252", "latin-1")
    last_error: UnicodeDecodeError | None = None

    for encoding in encodings:
        try:
            with csv_path.open(newline="", encoding=encoding) as handle:
                return list(csv.DictReader(handle))
        except UnicodeDecodeError as error:
            last_error = error

    if last_error:
        raise last_error
    return []


def get_row_count(connection: sqlite3.Connection, table_name: str) -> int | None:
    try:
        cursor = connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}")
        return int(cursor.fetchone()[0])
    except sqlite3.Error:
        return None


def get_tables(connection: sqlite3.Connection) -> list[dict[str, str]]:
    cursor = connection.execute(
        """
        SELECT name, type, sql
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    )
    return [
        {"name": name, "type": object_type, "create_sql": create_sql or ""}
        for name, object_type, create_sql in cursor.fetchall()
    ]


def get_columns(
    connection: sqlite3.Connection,
    table_name: str,
    table_descriptions: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    cursor = connection.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    columns: list[dict[str, Any]] = []

    for cid, name, data_type, not_null, default_value, primary_key_position in cursor.fetchall():
        column: dict[str, Any] = {
            "cid": cid,
            "name": name,
            "type": data_type or "",
            "not_null": bool(not_null),
            "default": default_value,
            "primary_key_position": primary_key_position,
        }
        description = table_descriptions.get(name)
        if description:
            column["bird_description"] = description
        columns.append(column)

    return columns


def get_foreign_keys(connection: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    cursor = connection.execute(f"PRAGMA foreign_key_list({quote_identifier(table_name)})")
    foreign_keys: list[dict[str, Any]] = []

    for row in cursor.fetchall():
        (
            fk_id,
            sequence,
            referenced_table,
            from_column,
            to_column,
            on_update,
            on_delete,
            match,
        ) = row
        foreign_keys.append(
            {
                "id": fk_id,
                "sequence": sequence,
                "from_table": table_name,
                "from_column": from_column,
                "to_table": referenced_table,
                "to_column": to_column,
                "on_update": on_update,
                "on_delete": on_delete,
                "match": match,
            }
        )

    return foreign_keys


def extract_schema(sqlite_path: Path) -> dict[str, Any]:
    database_dir = sqlite_path.parent
    descriptions = read_description_csvs(database_dir)

    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = []
        all_foreign_keys = []
        for table in get_tables(connection):
            table_name = table["name"]
            foreign_keys = get_foreign_keys(connection, table_name)
            all_foreign_keys.extend(foreign_keys)
            tables.append(
                {
                    **table,
                    "row_count": get_row_count(connection, table_name),
                    "columns": get_columns(
                        connection,
                        table_name,
                        descriptions.get(table_name, {}),
                    ),
                    "foreign_keys": foreign_keys,
                }
            )

        return {
            "db_id": database_dir.name,
            "sqlite_path": str(sqlite_path),
            "tables": tables,
            "foreign_keys": all_foreign_keys,
        }
    finally:
        connection.close()


def find_sqlite_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]

    sqlite_files = sorted(
        file_path
        for pattern in ("*.sqlite", "*.sqlite3", "*.db")
        for file_path in path.rglob(pattern)
        if file_path.is_file()
    )
    return sqlite_files


def build_llm_prompt(schema: dict[str, Any], question: str | None = None) -> str:
    question_text = question or "<USER_QUESTION_HERE>"
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    return f"""You are an expert SQLite text-to-SQL assistant.
Use only the tables, columns, primary keys, and foreign keys in this schema JSON.
Return one valid SQLite query and no extra explanation.

Schema JSON:
```json
{schema_json}
```

Question:
{question_text}
"""


def write_json(data: Any, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract table, column, and foreign-key metadata from BIRD SQLite databases."
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=DEFAULT_TRAIN_DATABASES,
        help="Path to one SQLite file, one database folder, or the train_databases folder.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("schema_json"),
        help="Output JSON file for one DB, or output directory for multiple DBs.",
    )
    parser.add_argument(
        "--prompt-output",
        type=Path,
        help="Optional prompt file for one DB, or prompt output directory for multiple DBs.",
    )
    parser.add_argument(
        "--question",
        help="Optional natural-language question to include in generated prompt files.",
    )
    args = parser.parse_args()

    sqlite_files = find_sqlite_files(args.database_path)
    if not sqlite_files:
        raise SystemExit(f"No SQLite database files found under: {args.database_path}")

    extracting_multiple = len(sqlite_files) > 1
    schemas: list[dict[str, Any]] = []

    for sqlite_path in sqlite_files:
        schema = extract_schema(sqlite_path)
        schemas.append(schema)

        if extracting_multiple or args.output.suffix.lower() != ".json":
            output_path = args.output / f"{schema['db_id']}_schema.json"
        else:
            output_path = args.output
        write_json(schema, output_path)

        if args.prompt_output:
            prompt = build_llm_prompt(schema, args.question)
            if extracting_multiple or args.prompt_output.suffix.lower() != ".txt":
                prompt_path = args.prompt_output / f"{schema['db_id']}_prompt.txt"
            else:
                prompt_path = args.prompt_output
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")

    index_path = args.output / "all_schemas_index.json" if extracting_multiple else None
    if index_path:
        write_json(
            [
                {
                    "db_id": schema["db_id"],
                    "sqlite_path": schema["sqlite_path"],
                    "table_count": len(schema["tables"]),
                    "foreign_key_count": len(schema["foreign_keys"]),
                    "schema_json": str(args.output / f"{schema['db_id']}_schema.json"),
                }
                for schema in schemas
            ],
            index_path,
        )

    print(f"Extracted {len(schemas)} database schema(s).")
    print(f"Schema output: {args.output}")
    if args.prompt_output:
        print(f"Prompt output: {args.prompt_output}")


if __name__ == "__main__":
    main()
