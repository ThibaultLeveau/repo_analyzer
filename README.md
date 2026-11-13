# RepoPilot

RepoPilot is a local web application for understanding source repositories.

Read the [user manual](docs/index.md) for a guided walkthrough, issue triage and
CLI report examples. To browse the themed manual locally, install
`python -m pip install -e ".[docs]"` and run `python -m mkdocs serve --dev-addr 127.0.0.1:8001`.

## Quickstart

Install Python 3.12 or later, download or clone this repository, then run

```sh
python run.py
```

Open `http://127.0.0.1:8000/` and enter a public GitHub repository URL. The
first start installs dependencies in a private environment. Later starts reuse
it. From another directory, pass the full quoted path to `run.py`. Use
`--port 8010` if port 8000 is occupied.

Press Ctrl+C in the terminal to stop. Run the same command to restart. By
default, `~/.repopilot` holds `state.db` (imports and answers), `issues.db`,
`sources/` (pinned snapshots), `chroma/` (embeddings) and `runtime/` (the
environment for this checkout and Python version). Choose another location
with `--data-dir "path with spaces"` or `REPOPILOT_DATA_DIR`.

## Development

Use Python 3.12 or later. From the project directory, create a virtual environment.

```sh
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in PowerShell or
`source .venv/bin/activate` in a POSIX shell. Install the development dependencies
and run the tests.

```sh
python -m pip install -e ".[dev]"
python -m pytest --ignore=tests/browser
```

Run the development server and visit `http://127.0.0.1:8000/health`.

```sh
python -m uvicorn repopilot.app:create_app --factory
```

## AI configuration

Set `OPENAI_API_KEY` in the process environment to enable model-backed analysis.
Set `REPOPILOT_CHAT_MODEL` to choose an OpenAI model that supports strict
structured outputs. The default is `gpt-4o-mini`. Repository imports, browsing
and static analysis work without credentials.

The semantic index uses `text-embedding-3-small` with 512 dimensions when you
choose **Build semantic index**. The launcher initializes local storage on
startup. Building an index sends the commit’s selected text chunks to OpenAI.

The chat adapter sends the question and selected evidence excerpts to OpenAI.
RepoPilot builds each citation link from the stored repository, commit, path
and line range. [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

## Command-line reports

From the installed Python environment, analyze the default branch of a public repository:

```sh
repopilot analyze https://github.com/ThibaultLeveau/repo_analyzer --format json --output "analysis report.json"
```

Pass `--sha` with a full commit to analyze that revision. Static analysis runs locally.
Add `--generate-guide` to request cited onboarding notes with the configured OpenAI key.
To export stored analysis offline, copy the full SHA from the JSON report:

```sh
repopilot export ThibaultLeveau/repo_analyzer --sha FULL_COMMIT_SHA --format html --output "saved report.html"
```

Both commands support `json`, `md`, and `html`, and share the web application's
`REPOPILOT_DATA_DIR` setting. Omit `--output` to write UTF-8 to stdout. Diagnostics
go to stderr. Exit codes are 0 for success, 1 for an operational error, 2 for invalid
arguments and 3 for a missing cached snapshot.

## Quality checks

GitHub Actions tests Python 3.12 on Windows and Linux for pull requests,
pushes to `main` and `v*` tags. Run the same checks in the development environment.

```sh
python -m pytest -v --ignore=tests/browser
python -m ruff check .
python -m mypy
python -m bandit -r src
```

For browser tests, install Chromium once and run the real application journey
at desktop and mobile widths. On Linux CI, add `--with-deps` to the install command.

```sh
python -m playwright install chromium
python -m pytest -v tests/browser --tracing retain-on-failure --screenshot only-on-failure
```

Failed runs save screenshots and traces under `test-results/`.

### Retrieval evaluation

Run `python tests/evaluate_retrieval.py --check` from the repository root to compare
lexical, vector and hybrid retrieval on five labeled queries against the Catalog fixture.
The command uses deterministic local embeddings and prints JSON with each ranked source
range, metric and elapsed time. The fixture SHA and per-query recall baselines are
in `tests/fixtures/retrieval-v1.json`.

Recall at two is the fraction of distinct labeled ranges overlapped by either of the
first two results in the same file. Source diversity is the number of distinct files
divided by the number of returned results at that cutoff, or zero for an empty result.
The summary averages each metric across queries. Query latency in milliseconds excludes
index construction. CI compares per-query recall and reports timing for inspection.
