# Explore a repository

## Follow a question to its source

1. Import a public repository and open **Browse source**. Expand a directory
   and select a file. Click a symbol to open its definition.
2. Use **Search this snapshot** to look up a filename, symbol or snippet. To
   add semantic matches, select **Build semantic index** on the search page.
3. In the explorer, enter a **Question about this commit** and select **Ask**.
   A supported answer includes an excerpt for each claim. Select **Open cited
   lines** to inspect the original file and line range at the same commit.
   An insufficient-evidence result means the available excerpts could not support
   an answer.

In the [Catalog test fixture](https://github.com/ThibaultLeveau/repo_analyzer/blob/main/tests/journey_fixture.py),
searching for `load_catalog` leads to its definition in `src/catalog.py`,
lines 4–5. Line 5 calls `yaml.safe_load(source)`. Its test appears in
`tests/test_catalog.py`. These files belong to the example fixture.

## Check architecture, dependencies and setup

The **Architecture** view groups modules and links relationships to imports.
Open **Dependencies** to trace a declared package to the modules that import
it. The Catalog example declares `PyYAML==6.0.3` and imports `yaml` in
`src/catalog.py`.

**Getting Started** separates documented setup steps from commands inferred
from project files. Follow the evidence link before using a command. A command
marked unverified has not been executed by RepoPilot. **Download Markdown**
saves the guide for this commit.

## Triage open issues

Select **Issues**, then **Load latest 50 issues**. The queue includes open
issues and skips pull requests. **Refresh newest 50** checks recent issues.
**Load next 50** extends the existing queue when another page is available.

The default minimum interval between requests is six hours. Change the hours
and select **Save interval**. A GitHub rate limit can extend the wait. You can
continue reading cached issues while the next request is unavailable.

Open an issue to inspect its description, labels and component. Generate its
assessment to see the difficulty factors and their evidence. **Assessment
history** lets you reopen an earlier score after the issue or snapshot changes.

## Inspect usage

Open **Observability** for recent imports, failed stages, cache hits, request
latency and OpenAI usage. Select a time window and choose **Update metrics**.
An unknown token count means the provider supplied no usage figure. Resource
samples cover the local process and storage across all repositories.
