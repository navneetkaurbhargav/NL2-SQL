#!/usr/bin/env python3
"""Run a BIRD-style NL -> SQL -> execution trace -> analytical report pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlparse
from openai import OpenAI


DEFAULT_SCHEMA_DIR = Path("schema_json")
DEFAULT_TRAIN_DATABASES = Path("/Users/nav/Downloads/train/train_databases")
DEFAULT_MODEL = "gpt-5.1"
FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(ALTER|ATTACH|CREATE|DELETE|DETACH|DROP|INSERT|PRAGMA|REINDEX|REPLACE|UPDATE|VACUUM)\b",
    re.IGNORECASE,
)


@dataclass
class QueryAttempt:
    query_name: str
    purpose: str
    sql: str
    attempt: int
    status: str
    runtime_ms: int | None = None
    row_count_returned: int | None = None
    truncated: bool = False
    columns: list[str] | None = None
    rows: list[dict[str, Any]] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_name": self.query_name,
            "purpose": self.purpose,
            "sql": self.sql,
            "attempt": self.attempt,
            "status": self.status,
            "runtime_ms": self.runtime_ms,
            "row_count_returned": self.row_count_returned,
            "truncated": self.truncated,
            "columns": self.columns or [],
            "rows": self.rows or [],
            "error": self.error,
        }


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def resolve_schema_path(db_id: str, schema_dir: Path) -> Path:
    schema_path = schema_dir / f"{db_id}_schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema JSON not found: {schema_path}")
    return schema_path


def resolve_sqlite_path(schema: dict[str, Any], train_databases: Path) -> Path:
    schema_path = Path(schema.get("sqlite_path", ""))
    if schema_path.exists():
        return schema_path

    db_id = schema["db_id"]
    candidate = train_databases / db_id / f"{db_id}.sqlite"
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        f"SQLite DB not found. Tried schema path {schema_path} and {candidate}"
    )


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep prompt size sane while preserving table, column, key, and description data."""
    tables = []
    for table in schema["tables"]:
        tables.append(
            {
                "name": table["name"],
                "row_count": table.get("row_count"),
                "columns": [
                    {
                        "name": column["name"],
                        "type": column.get("type", ""),
                        "not_null": column.get("not_null", False),
                        "primary_key_position": column.get("primary_key_position", 0),
                        "description": column.get("bird_description", {}),
                    }
                    for column in table.get("columns", [])
                ],
                "foreign_keys": table.get("foreign_keys", []),
            }
        )
    return {
        "db_id": schema["db_id"],
        "tables": tables,
        "foreign_keys": schema.get("foreign_keys", []),
    }


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def call_openai_json(model: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it later with: export OPENAI_API_KEY='your-key'"
        )

    client = OpenAI()
    response = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=user_prompt,
    )
    return extract_json_object(response.output_text)


def build_sql_planning_prompt(schema: dict[str, Any], question: str) -> tuple[str, str]:
    system_prompt = """You generate SQLite analysis plans as strict JSON.
Use only the provided schema. Do not invent tables or columns.
Generate read-only SQLite SELECT or WITH queries only.
Prefer a small set of high-value analytical queries over many shallow queries."""
    user_prompt = f"""Natural language analytical request:
{question}

Relational database schema JSON:
{json.dumps(compact_schema(schema), indent=2, ensure_ascii=False)}

Return exactly this JSON shape:
{{
  "analysis_title": "short report title",
  "assumptions": ["assumption if needed"],
  "queries": [
    {{
      "name": "snake_case_query_name",
      "purpose": "what this query proves or measures",
      "sql": "SELECT ..."
    }}
  ],
  "report_sections": ["Executive Summary", "Methodology", "Key Findings", "SQL Evidence", "Limitations"]
}}
"""
    return system_prompt, user_prompt


def build_repair_prompt(
    schema: dict[str, Any],
    question: str,
    query: dict[str, Any],
    error: str,
) -> tuple[str, str]:
    system_prompt = """You repair invalid SQLite SELECT queries as strict JSON.
Use only the provided schema. Return one corrected read-only SQLite query."""
    user_prompt = f"""Natural language analytical request:
{question}

Schema JSON:
{json.dumps(compact_schema(schema), indent=2, ensure_ascii=False)}

Failed query:
{json.dumps(query, indent=2, ensure_ascii=False)}

Execution or validation error:
{error}

Return exactly:
{{
  "name": "{query.get("name", "repaired_query")}",
  "purpose": "{query.get("purpose", "repair failed query")}",
  "sql": "SELECT ..."
}}
"""
    return system_prompt, user_prompt


def build_report_prompt(
    schema: dict[str, Any],
    question: str,
    plan: dict[str, Any],
    traces: list[dict[str, Any]],
) -> tuple[str, str]:
    system_prompt = """You write analytical reports grounded only in SQL execution traces.
Return strict JSON. Do not claim findings that are not supported by query results."""
    user_prompt = f"""Original analytical request:
{question}

Database:
{schema["db_id"]}

Analysis plan:
{json.dumps(plan, indent=2, ensure_ascii=False)}

SQL execution traces and results:
{json.dumps(traces, indent=2, ensure_ascii=False)}

Return exactly this JSON shape:
{{
  "title": "report title",
  "sections": [
    {{
      "heading": "Executive Summary",
      "body": "concise paragraph"
    }},
    {{
      "heading": "Methodology",
      "body": "explain which SQL evidence was used"
    }},
    {{
      "heading": "Key Findings",
      "body": "findings grounded in the result rows"
    }},
    {{
      "heading": "SQL Evidence",
      "body": "mention query names and important numeric/table results"
    }},
    {{
      "heading": "Limitations",
      "body": "data, schema, or truncation caveats"
    }}
  ]
}}
"""
    return system_prompt, user_prompt


def validate_sql(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    statements = [statement for statement in sqlparse.parse(stripped) if str(statement).strip()]

    if len(statements) != 1:
        raise ValueError("SQL must contain exactly one statement.")

    first_token = statements[0].token_first(skip_cm=True)
    first_keyword = first_token.value.upper() if first_token else ""
    if first_keyword not in {"SELECT", "WITH"}:
        raise ValueError("Only SELECT or WITH statements are allowed.")

    if FORBIDDEN_SQL_PATTERN.search(stripped):
        raise ValueError("SQL contains a forbidden write/admin keyword.")

    return stripped


def execute_select(
    sqlite_path: Path,
    sql: str,
    query_name: str,
    purpose: str,
    attempt: int,
    max_rows: int,
    timeout_steps: int,
) -> QueryAttempt:
    start = time.perf_counter()
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    steps = 0

    def progress_handler() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps > timeout_steps else 0

    try:
        safe_sql = validate_sql(sql)
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(progress_handler, 1000)
        cursor = connection.execute(safe_sql)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [description[0] for description in cursor.description or []]
        truncated = len(rows) > max_rows
        visible_rows = rows[:max_rows]
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        return QueryAttempt(
            query_name=query_name,
            purpose=purpose,
            sql=safe_sql,
            attempt=attempt,
            status="success",
            runtime_ms=elapsed_ms,
            row_count_returned=len(visible_rows),
            truncated=truncated,
            columns=columns,
            rows=[dict(row) for row in visible_rows],
        )
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        return QueryAttempt(
            query_name=query_name,
            purpose=purpose,
            sql=sql,
            attempt=attempt,
            status="error",
            runtime_ms=elapsed_ms,
            error=str(exc),
        )
    finally:
        connection.close()


def execute_with_repairs(
    schema: dict[str, Any],
    sqlite_path: Path,
    question: str,
    query: dict[str, Any],
    model: str,
    max_rows: int,
    max_repairs: int,
    timeout_steps: int,
) -> list[QueryAttempt]:
    traces = []
    current_query = dict(query)

    for attempt in range(1, max_repairs + 2):
        trace = execute_select(
            sqlite_path=sqlite_path,
            sql=current_query.get("sql", ""),
            query_name=current_query.get("name", f"query_{attempt}"),
            purpose=current_query.get("purpose", ""),
            attempt=attempt,
            max_rows=max_rows,
            timeout_steps=timeout_steps,
        )
        traces.append(trace)
        if trace.status == "success" or attempt > max_repairs:
            return traces

        system_prompt, user_prompt = build_repair_prompt(
            schema=schema,
            question=question,
            query=current_query,
            error=trace.error or "Unknown SQL error",
        )
        current_query = call_openai_json(model, system_prompt, user_prompt)

    return traces


def render_markdown_report(result: dict[str, Any]) -> str:
    report = result["report"]
    lines = [f"# {report['title']}", ""]
    for section in report.get("sections", []):
        lines.append(f"## {section['heading']}")
        lines.append(section["body"])
        lines.append("")

    lines.append("## SQL Execution Traces")
    for trace in result["execution_traces"]:
        lines.append(f"### {trace['query_name']} ({trace['status']})")
        lines.append(trace.get("purpose") or "")
        lines.append("")
        lines.append("```sql")
        lines.append(trace["sql"])
        lines.append("```")
        if trace.get("error"):
            lines.append(f"Error: {trace['error']}")
        else:
            lines.append(
                f"Runtime: {trace['runtime_ms']} ms; rows returned: "
                f"{trace['row_count_returned']}; truncated: {trace['truncated']}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    schema_path = resolve_schema_path(args.db_id, args.schema_dir)
    schema = load_json(schema_path)
    sqlite_path = resolve_sqlite_path(schema, args.train_databases)

    # Step 1: Ask the LLM for a structured SQL analysis plan.
    planning_system, planning_user = build_sql_planning_prompt(schema, args.question)
    if args.dry_run:
        return {
            "request": args.question,
            "db_id": args.db_id,
            "dry_run": True,
            "schema_path": str(schema_path),
            "sqlite_path": str(sqlite_path),
            "planning_prompt": {
                "system": planning_system,
                "user": planning_user,
            },
        }

    plan = call_openai_json(args.model, planning_system, planning_user)

    # Step 2: Validate, execute, and repair SQL when needed.
    all_attempts: list[QueryAttempt] = []
    for query in plan.get("queries", []):
        all_attempts.extend(
            execute_with_repairs(
                schema=schema,
                sqlite_path=sqlite_path,
                question=args.question,
                query=query,
                model=args.model,
                max_rows=args.max_result_rows,
                max_repairs=args.max_repairs,
                timeout_steps=args.timeout_steps,
            )
        )
    execution_traces = [attempt.to_dict() for attempt in all_attempts]

    # Step 3: Ask the LLM for a final multi-section report grounded in traces.
    report_system, report_user = build_report_prompt(schema, args.question, plan, execution_traces)
    report = call_openai_json(args.model, report_system, report_user)

    # Step 4: Persist the report, SQL traces, and result previews.
    return {
        "request": args.question,
        "db_id": args.db_id,
        "model": args.model,
        "schema_path": str(schema_path),
        "sqlite_path": str(sqlite_path),
        "analysis_plan": plan,
        "execution_traces": execution_traces,
        "report": report,
    }


def parse_args() -> argparse.Namespace:
    load_env_file(Path(".env"))
    parser = argparse.ArgumentParser(
        description="Run an OpenAI-powered NL-to-SQL analytical reporting pipeline."
    )
    parser.add_argument("--db-id", required=True, help="BIRD database id, e.g. european_football_1.")
    parser.add_argument("--question", required=True, help="Natural language analytical request.")
    parser.add_argument("--schema-dir", type=Path, default=DEFAULT_SCHEMA_DIR)
    parser.add_argument("--train-databases", type=Path, default=DEFAULT_TRAIN_DATABASES)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--output", type=Path, default=Path("analysis_outputs/result.json"))
    parser.add_argument("--markdown-output", type=Path, help="Optional Markdown report output path.")
    parser.add_argument("--max-result-rows", type=int, default=50)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument(
        "--timeout-steps",
        type=int,
        default=100000,
        help="SQLite progress-handler limit. Lower values stop expensive queries sooner.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the first LLM prompt without calling OpenAI or executing SQL.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_pipeline(args)
    write_json(result, args.output)

    if args.markdown_output and not args.dry_run:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown_report(result), encoding="utf-8")

    print(f"Wrote JSON output: {args.output}")
    if args.markdown_output and not args.dry_run:
        print(f"Wrote Markdown report: {args.markdown_output}")
    if args.dry_run:
        print("Dry run complete. Set OPENAI_API_KEY later to run the full pipeline.")


if __name__ == "__main__":
    main()
