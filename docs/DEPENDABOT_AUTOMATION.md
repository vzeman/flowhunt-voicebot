# Dependabot Automation

Dependabot opens grouped weekly PRs for Python dependencies and GitHub Actions.
The `Dependabot Automerge` workflow only runs for PRs authored by
`dependabot[bot]`.

Auto-merge is enabled with `gh pr merge --auto --squash --delete-branch`, so
GitHub waits for required branch protection checks before merging. The
repository validation workflow is the safety gate for dependency updates:

- `python -m unittest discover -s tests`
- `python -m pytest -q -m "not e2e"`
- compile checks for `voicebot`, `agents`, and `tests`
- whitespace checks
- forbidden-name scan
- committed-secret scan

If a Dependabot PR fails validation, it is not merged automatically. Fix the
compatibility issue with a normal PR or by pushing a change to the Dependabot
branch, then auto-merge can proceed after the checks pass.

Deployed E2E tests remain manual because they require a live API URL and
provider credentials. Add provider-specific E2E cases under `tests/e2e` as
deployment coverage expands.
