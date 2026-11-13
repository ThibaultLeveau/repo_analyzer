# Quickstart

RepoPilot opens a public GitHub repository at a specific commit so you can explore
its code, dependencies and issues with links back to the source.

## Start the local app

Install Python 3.12 or later. Download or clone
[RepoPilot](https://github.com/ThibaultLeveau/repo_analyzer), open a terminal in
its directory and run

```sh
python run.py
```

The first launch installs the Python dependencies in a private environment and
initializes local storage. Open the printed address, normally
<http://127.0.0.1:8000/>. If that port is occupied, run
`python run.py --port 8010`. Stop the server with Ctrl+C. The same command
reopens your saved work.

## Import a repository

Enter a public GitHub URL and select **Import repository**. RepoPilot resolves
the default branch, downloads its source archive and displays the commit SHA.
Select **Browse source** to open the file tree. **Refresh repository** checks
the current branch head and preserves the earlier snapshot.

An import reads source files without running the project's code. It requires
network access to GitHub on the first download. The issue queue is loaded
separately when you open **Issues** and request it.

## Enable model-backed features

Set `OPENAI_API_KEY` in the environment of the terminal that starts RepoPilot.
Restart the app after changing the environment. To select a compatible chat
model, set `REPOPILOT_CHAT_MODEL`. The default is `gpt-4o-mini`.

Static architecture, dependency analysis, code browsing and literal search are
available without a key. Asking a question sends the question and selected
excerpts to OpenAI. **Build semantic index** sends source chunks for embedding
and stores the resulting vectors locally. **Generate cited notes** uses selected
setup evidence to supplement the Getting Started guide.

## Find your data

The default data directory is `~/.repopilot`. Choose another with
`python run.py --data-dir "path with spaces"`, or set `REPOPILOT_DATA_DIR` for
both the app and CLI.

| Location | Contents |
| --- | --- |
| `sources/` | Read-only source snapshots and versioned analysis by commit |
| `state.db`, `issues.db` | Import jobs, saved answers and cached GitHub issues |
| `chroma/` | Local semantic index |
| `events.db` | Recent operations, provider usage and resource samples |
| `runtime/` | Reusable launcher environment |

Follow the [repository walkthrough](explore.md) or [export a report](reports.md).
