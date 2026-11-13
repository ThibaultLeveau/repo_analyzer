# Reports and CLI

## Download from the workspace

Open a repository snapshot and expand **Download report**. Choose **JSON
report** for scripts, **Markdown report** for a review or **HTML report** for
a standalone page. Each export identifies the full commit and uses the
architecture, dependencies, audit, setup, answers and issue scores already
saved for it. Open a section and run its analysis to populate missing results.

## Install the command

From the RepoPilot checkout, activate a Python 3.12+ virtual environment and run

```sh
python -m pip install -e .
```

## Analyze and export a pinned revision

The commands below use a published RepoPilot commit. Run them from the same
directory so both use the same local data store.

```sh
repopilot analyze https://github.com/ThibaultLeveau/repo_analyzer --sha e560271069ddd854d3f2581062d93ad3b64e7965 --data-dir "report cache" --output "analysis.json"
repopilot export ThibaultLeveau/repo_analyzer --sha e560271069ddd854d3f2581062d93ad3b64e7965 --data-dir "report cache" --format html --output "saved report.html"
```

The first command fetches the requested revision and prepares its analysis.
The second works offline and reads those saved results. Omit `--sha` from
`analyze` to resolve the current default branch. For a later export, copy the
full `sha` from the JSON report.

Both commands accept `--format json`, `md` or `html`. Omit `--output` to
write the report to standard output. Add `--generate-guide` to `analyze` to
request cited notes with the configured OpenAI key. Diagnostics go to
standard error. A missing cached revision on export returns exit code 3.

For CI, the [report workflow example](https://github.com/ThibaultLeveau/repo_analyzer/tree/main/examples/report-action)
uploads the report as an artifact and can post a link to it on a pull request.
