# NL2 SQL BIRD Analysis Pipeline

This project extracts BIRD benchmark SQLite schemas and runs a natural-language-to-SQL analytical reporting pipeline.

## Goal

Input:

- Natural language analytical request
- Relational database schema

Output:

- Multi-section analytical report
- SQL execution traces and result previews

## Project Files

- `requirements.txt` - Python dependencies.
- `extract_bird_schema.py` - Extracts tables, columns, primary keys, foreign keys, row counts, and BIRD column descriptions from SQLite databases.
- `run_analysis.py` - Runs the LLM-powered NL -> SQL -> execution trace -> report pipeline.
- `.env.example` - Placeholder for provider credentials and model settings.
- `schema_json/` - Generated schema JSON files.
- `prompts/` - Generated LLM prompt templates.
- `analysis_outputs/` - Generated analysis reports and traces.

## 1. Install Requirements

From the project folder:

```bash
cd /path/to/NL2-SQL
python3 -m pip install -r requirements.txt
```

## 2. Dataset Location

Set the path to your local BIRD train databases folder:

```bash
export BIRD_TRAIN_DATABASES_DIR="/path/to/train/train_databases"
```

Each database folder contains a SQLite database, for example:

```text
${BIRD_TRAIN_DATABASES_DIR}/european_football_1/european_football_1.sqlite
```

## 3. Generate Schema JSON Files

Run this when the databases are new or changed:

```bash
python3 extract_bird_schema.py \
  --database-path "$BIRD_TRAIN_DATABASES_DIR" \
  --output schema_json \
  --prompt-output prompts
```

This creates:

```text
schema_json/{db_id}_schema.json
prompts/{db_id}_prompt.txt
```

Example:

```text
schema_json/european_football_1_schema.json
prompts/european_football_1_prompt.txt
```

The extractor reads:

- SQLite tables and views
- Columns and SQLite types
- Primary keys
- Foreign keys
- Row counts
- BIRD `database_description/*.csv` metadata when available

## 4. Choose An LLM Provider

The pipeline supports:

- `openai` - hosted OpenAI API.
- `ollama` - local models through Ollama.

### OpenAI

Create a local `.env` file:

```bash
cp .env.example .env
```

Edit `.env`:

```text
OPENAI_API_KEY=your-real-openai-api-key
OPENAI_MODEL=gpt-5.1
```

You can also use shell environment variables instead:

```bash
export OPENAI_API_KEY="your-real-openai-api-key"
export OPENAI_MODEL="gpt-5.1"
```

### Ollama

Install Ollama and pull a local model:

```bash
ollama pull llama3.1:8b
```

Run the pipeline with Ollama:

```bash
python3 run_analysis.py \
  --provider ollama \
  --model llama3.1:8b \
  --db-id european_football_1 \
  --question "How many matches are in each division?" \
  --output analysis_outputs/ollama_report.json \
  --markdown-output analysis_outputs/ollama_report.md
```

## 5. Dry Run The Pipeline

Use dry run before adding credentials or making API calls:

```bash
python3 run_analysis.py \
  --db-id european_football_1 \
  --question "Which divisions have the most matches by season?" \
  --dry-run \
  --output analysis_outputs/dry_run.json
```

Dry run verifies that:

- The schema JSON exists
- The SQLite database path can be resolved
- The LLM planning prompt can be built

It does not call the LLM provider and does not execute SQL.

## 6. Run Full Analysis

With OpenAI:

```bash
python3 run_analysis.py \
  --provider openai \
  --db-id european_football_1 \
  --question "Which divisions have the most matches by season?" \
  --output analysis_outputs/european_football_report.json \
  --markdown-output analysis_outputs/european_football_report.md
```

With Ollama:

```bash
python3 run_analysis.py \
  --provider ollama \
  --model llama3.1:8b \
  --db-id european_football_1 \
  --question "Which divisions have the most matches by season?" \
  --output analysis_outputs/european_football_report.json \
  --markdown-output analysis_outputs/european_football_report.md
```

This produces:

```text
analysis_outputs/european_football_report.json
analysis_outputs/european_football_report.md
```

## Pipeline Behavior

`run_analysis.py` uses already-created schema JSON files. It does not regenerate schemas on each run.

Flow:

```text
Natural language question
  -> load schema_json/{db_id}_schema.json
  -> resolve SQLite DB path
  -> ask the selected LLM provider for a SQL analysis plan
  -> validate SQL
  -> execute SQL against SQLite
  -> repair failed SQL with the selected LLM provider if needed
  -> generate final multi-section report
  -> save JSON traces and optional Markdown report
```

## SQL Safety

The execution layer only allows read-only SQL:

- `SELECT`
- `WITH`

It blocks write/admin keywords such as:

- `DROP`
- `DELETE`
- `UPDATE`
- `INSERT`
- `ALTER`
- `CREATE`
- `VACUUM`
- `PRAGMA`

SQLite is opened in read-only mode and `PRAGMA query_only = ON` is set before query execution.

## Output JSON Structure

The final JSON includes:

```json
{
  "request": "original natural language question",
  "db_id": "database id",
  "provider": "openai or ollama",
  "model": "LLM model",
  "schema_path": "schema JSON path",
  "sqlite_path": "SQLite DB path",
  "analysis_plan": {},
  "execution_traces": [],
  "report": {}
}
```

Each execution trace includes:

```json
{
  "query_name": "query name",
  "purpose": "why this query was run",
  "sql": "executed SQL",
  "attempt": 1,
  "status": "success",
  "runtime_ms": 88,
  "row_count_returned": 3,
  "truncated": false,
  "columns": ["column_a", "column_b"],
  "rows": [],
  "error": null
}
```

## Useful Commands

Regenerate all schemas:

```bash
python3 extract_bird_schema.py \
  --database-path "$BIRD_TRAIN_DATABASES_DIR" \
  --output schema_json \
  --prompt-output prompts
```

Generate schema for one DB:

```bash
python3 extract_bird_schema.py \
  --database-path "$BIRD_TRAIN_DATABASES_DIR/european_football_1/european_football_1.sqlite" \
  --output schema_json/european_football_1_schema.json \
  --prompt-output prompts/european_football_1_prompt.txt
```

Run one report:

```bash
python3 run_analysis.py \
  --provider ollama \
  --model llama3.1:8b \
  --db-id european_football_1 \
  --question "How many matches are in each division?" \
  --output analysis_outputs/report.json \
  --markdown-output analysis_outputs/report.md
```

Change OpenAI model for one run:

```bash
python3 run_analysis.py \
  --provider openai \
  --db-id european_football_1 \
  --question "How many matches are in each division?" \
  --model gpt-5.1 \
  --output analysis_outputs/report.json
```

Limit result preview rows:

```bash
python3 run_analysis.py \
  --db-id european_football_1 \
  --question "Show match counts by division and season." \
  --max-result-rows 25 \
  --output analysis_outputs/report.json
```
