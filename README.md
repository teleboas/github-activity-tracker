# GitHub Activity Tracker

A Python tool to extract activity days for a specific GitHub user within an organization for a given month.

## Features

- **Dual Strategy**: Combines repository-by-repository checking with the GitHub search API
- **Comprehensive Activity Detection**: Tracks commits, pull requests, issues, comments, PR reviews, and wiki edits
- **Repository Discovery**: Finds the repositories a user touched, rather than relying on a fixed cutoff
- **Flexible Date Ranges**: Specify any year and month
- **JSON Output**: Save results in structured format with detailed daily activity breakdown
- **Loud Failures**: A failed call is reported, not swallowed, so an incomplete report says so
- **Deduplication**: Automatically deduplicates activity found by both methods

## Installation

```bash
# No additional dependencies needed beyond Python 3 and GitHub CLI
# Check if gh is installed and authenticated:
gh auth status

# If not authenticated, run:
gh auth login
```

## Usage

The script uses GitHub CLI for authentication and API access:

```bash
# Basic usage (checks last month by default)
python extract_activity_gh.py --org org-name --user username

# Use only repository-by-repository method
python extract_activity_gh.py --org org-name --user username --method repos

# Use only GitHub search (faster, may miss some activity)
python extract_activity_gh.py --org org-name --user username --method search

# Specific user/org/date
python extract_activity_gh.py --org org-name --user username --year 2024 --month 12

# Force a repo that only ever sees wiki edits or commit comments
python extract_activity_gh.py --org org-name --user username --include-repos docs-repo-name

# Save results to JSON
python extract_activity_gh.py --org org-name --user username --output activity_report.json
```

### Command Line Arguments
- `--org`: GitHub organization name (**required**)
- `--user`: GitHub username (**required**)
- `--year`: Year to analyze (default: last month's year)
- `--month`: Month to analyze (default: last month)
- `--method`: Method to use - 'repos', 'search', or 'both' (default: both)
- `--repo-limit`: Number of most recently pushed repos to check (default: 20). Repos found by search are checked on top of these, so this is a floor rather than a cap.
- `--include-repos`: Comma-separated list of repos to always include. See [When to use `--include-repos`](#when-to-use---include-repos) — most repos no longer need it.
- `--output`: Output file for JSON results
- `--verbose`: Enable verbose output. Failures are reported with or without it.

## Authentication

The tool uses GitHub CLI for authentication. Make sure you have:

1. GitHub CLI installed (`gh` command)
2. Authenticated with GitHub: `gh auth login`
3. Your token has appropriate permissions for the organization

## Output

The tool outputs:
- List of active days in YYYY-MM-DD format
- Total count of active days
- Detailed breakdown of activity by day
- Summary information

Example output:
```
=== Final Activity Summary ===
User: username
Organization: org-name
Period: 2025-08
Total active days: 12
GitHub API requests made: 252
gh commands run: 216

Detailed daily activity:
  2025-08-01:
    - Commit: abc1234: Fix bug in authentication in repo-name
    - PR Created: #123: Add new feature in repo-name
  2025-08-03:
    - PR Review: on PR #124 in repo-name
    - Issue Comment: on issue #125 in repo-name
    - Wiki Edit: edited 'API Documentation' in docs-repo-name
  2025-08-07:
    - PR Comment: on PR #126 in repo-name
    - Commit: def5678: Update dependencies in repo-name
  ...
```

`GitHub API requests made` counts HTTP requests, not subprocesses: a single
`gh` command sends one request per page when it paginates, so the two numbers
differ by roughly an order of magnitude on a full run.

Activity types tracked:
- **Commits**: Code commits to repositories
- **PR Created**: Pull requests opened
- **PR Review**: Formal pull request reviews
- **PR Comment**: Review comments on pull request code
- **Issue Created**: New issues opened
- **Issue Comment**: Comments on issues, including the conversation on a pull request
- **Commit Comment**: Comments on specific commits
- **Wiki Edit**: Edits to repository wikis

## Incomplete reports

Any failed call is printed to stderr as it happens and listed at the end of the
summary:

```
INCOMPLETE: 2 call(s) failed, so activity may be missing from this report:
  - repos/org-name/repo-name/commits: gh: Not Found (HTTP 404)
  - issue list (org-name/repo-name): GraphQL: Could not resolve to a Repository
Re-run to see whether the failures were transient.
```

The exit status is 1 when this happens, and the JSON output carries a
`complete` flag and a `failures` list. A run that prints no such block lost
nothing to a failed call.

## How It Works

The tool uses two complementary methods:

1. **Repository Method** (`--method repos`):
   - Checks each selected repository for commits, issues, comments and wiki edits
   - More thorough but makes more API calls

2. **Search Method** (`--method search`):
   - Uses GitHub's search API to find commits, PRs and issues
   - Faster with fewer API calls
   - Does not see wiki edits or commit comments

3. **Both Methods** (`--method both`, default):
   - Runs both and combines the results, deduplicated
   - Recommended for accurate, comprehensive tracking

### Which repositories get checked

Repository selection is the union of two sources:

- the `--repo-limit` most recently **pushed** repositories in the organization
- every repository the search API says the user touched that month

The second matters because push order measures code pushes, not one person's
activity. A repository dormant for months still collects reviews, comments and
issues, and no cutoff on the ranked list catches those: in one real month the
user's active repositories ranked as low as 40th while only 16 were active at
all, so raising `--repo-limit` would have scanned the wrong repositories more
expensively.

PR reviews are looked up by asking search which pull requests the user reviewed,
rather than by inspecting every pull request in every repository.

### When to use `--include-repos`

Search discovery covers commits, pull requests, issues and their comments, so
those repositories are found whatever their rank. Two activity types are
invisible to it:

- **wiki edits**, which come from the parent repository's events feed and are
  indexed by no search qualifier
- **commit comments**, which `involves:` does not cover

So `--include-repos` is worth setting only for a repository where the user
edits the wiki or comments on commits but does nothing else. If they also open
an issue or a pull request there, discovery finds it on its own.

Pass the **parent repository name**, never the `.wiki` suffix. A wiki is not a
repository in the REST API, so `docs-repo-name.wiki` can only produce 404s,
while `docs-repo-name` reads both the code and the wiki activity.

## Which day an activity counts towards

Commit days come from the search API, which preserves the author's UTC offset,
so work done at 00:50 +02:00 counts as that day rather than the previous one in
UTC. Commits whose author date carries no offset, which is common for commits
made on a server, fall back to UTC.

Everything else — issues, pull requests, reviews and comments — is only exposed
by GitHub in UTC, and is counted on the UTC day.

## Limitations

- **Wiki edits expire.** They are read from GitHub's repository events feed,
  which only returns recent activity. The documented window is 90 days, and in
  practice it can be considerably shorter: one docs repository reached back
  about three weeks. Wiki edits for an older month cannot be recovered by
  re-running, so keep the report generated at the time.
- **Search caps at 1000 results**, so a month with more matching commits than
  that will fall back to what the repository scan finds.
- **Merge commits** carry the offset of whoever's environment created them,
  which is usually but not always the person credited as author.

## Notes

- The tool automatically handles API pagination
- Commits and comments are filtered by date server-side, so a repository's full
  history is not downloaded to find one month
- Activity is automatically deduplicated when using both methods
- Use `--verbose` to see per-repository progress
