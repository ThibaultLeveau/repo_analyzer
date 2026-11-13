# Explore a repository

## Follow a question to its source

1. Import a public repository and open **Browse source**. Expand a directory
   and select a file. Click a symbol to open its definition.
2. Press **Ctrl+K** (or **⌘K**) to search files, symbols, source text, cached
   issues and known commits. To add semantic matches, select **Build semantic
   index** on the search page. The repository selector in the header switches
   between imported snapshots and shows the branch when known.
3. In the explorer, enter a **Question about this commit** and select **Ask**.
   A supported answer includes an excerpt for each claim. Select **Open cited
   lines** to inspect the original file and line range at the same commit.
   An insufficient-evidence result means the available excerpts could not support
   an answer.

Ask can combine repository observations, dated GitHub records and indexed
publisher guidance. For example, ask “How does our code load YAML, and what
do the PyYAML docs recommend?” after indexing PyYAML in **Dependencies**.
Follow **Open cited lines** for the implementation or **Open official
documentation section** for the publisher's guidance. When a requested
source is unavailable, the answer identifies that gap. Historical questions
use the records loaded from the **Issues** view.

Set `REPOPILOT_RERANK=1` before starting RepoPilot to enable optional model
reranking with your configured OpenAI key. Ask sends at most 12 candidate
IDs with up to 350 characters from each excerpt. A ranking request has an
eight-second timeout and falls back to the existing order on failure. The
**Observability** view records this call separately as `model.rerank`.

From a development checkout, run
`python tests/evaluate_retrieval.py --check --compare-rerank` to compare
recall, source diversity, elapsed time and ranking calls on the pinned
questions using a deterministic fixture reranker.

In the [Catalog test fixture](https://github.com/ThibaultLeveau/repo_analyzer/blob/main/tests/journey_fixture.py),
searching for `load_catalog` leads to its definition in `src/catalog.py`,
lines 4–5. Line 5 calls `yaml.safe_load(source)`. Its test appears in
`tests/test_catalog.py`. These files belong to the example fixture.

## Check architecture, dependencies and setup

The **Architecture** view groups modules and links relationships to imports.
To trace a Python function or method, select its definition in **Code Explorer**
and choose **Trace outgoing calls**. The diagram and ordered call-site list
both link to the invoking line in the selected commit. RepoPilot follows
up to four call levels and displays up to 48 sites. It marks recursion,
unresolved calls and traversal limits explicitly.

Open **Dependencies** to trace a declared package to the modules that import
it. The Catalog example declares `PyYAML==6.0.3` and imports `yaml` in
`src/catalog.py`.

Select a package, then **Official documentation** and **Index documentation**.
Open a section to inspect its excerpt and publisher link. The view shows the
manifest constraint, documentation edition and fetch time. Unversioned and
latest editions can differ from the installed package. Cached pages become
stale after seven days. Select **Refresh documentation** to fetch them again.

To configure a publisher, add a distribution entry to `official-docs.toml`
inside the local RepoPilot data directory. For example:

```toml
[sources.requests]
url = "https://requests.readthedocs.io/en/latest/user/quickstart/"
edition = "Latest"
```

Only the configured page and its same-origin redirects are eligible. A
`{series}` placeholder selects the major and minor version from a single
exact pin or minor-series wildcard, falling back to `stable`.

**Getting Started** separates documented setup steps from commands inferred
from project files. Follow the evidence link before using a command. A command
marked unverified has not been executed by RepoPilot. **Download Markdown**
saves the guide for this commit.

Select a **Guide language** and choose **Show guide** to translate the prose.
Commands and source links stay intact. Expand **Original English guide** to
compare versions, or choose **Download Markdown** to save the selected version.

Drop a Markdown file into **Project notes** or choose it with the file input.
Open the note to inspect its headings, replace it, remove it or download the
original file. In the explorer, ask “What do my notes say about dispatch?” to
search current revisions. Each saved answer cites the revision used at the
time of the question.

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
