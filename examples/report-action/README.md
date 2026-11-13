# RepoPilot report workflow

Copy `repopilot-report.yml` into `.github/workflows/` in a public GitHub repository.
On push and manual dispatch, it analyzes the triggering repository and SHA. For a
pull request it uses the head repository and head SHA, including public forks.
It installs RepoPilot from the full Git commit pinned in the workflow. The CLI
reads the target as source data. The report job uses read-only permissions.

The artifact named **repopilot-report** contains `repopilot-report.json`. Find it
under **Artifacts** on the workflow run. The JSON includes the analyzed repository
and commit. A failed analysis or upload fails the job and skips the summary comment.
Re-running the workflow replaces the artifact for that run.

## Optional PR comment

Set the repository Actions variable `REPOPILOT_COMMENT` to `true` to enable a
comment on same-repository pull requests. The separate comment job requests
`pull-requests: write`. The repository or organization must allow this permission.
The job updates the bot's existing marked comment and links it to the workflow run.
All other events and fork PRs only produce the artifact. No OpenAI key is required.

## Reusing the workflow

A caller can use the workflow from another repository after that repository has
installed it under `.github/workflows/`. Pin the reusable workflow to its full commit.
The optional Boolean input `comment` defaults to false. For a caller that enables
comments, grant the calling job `contents: read` and `pull-requests: write`. GitHub
applies the caller's permission ceiling to the reusable workflow. Keep the repository
variable unset if the caller should exclusively control the comment setting.
