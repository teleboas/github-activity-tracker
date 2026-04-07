# GitHub Activity Tracker

A Python tool to extract activity days for a specific GitHub user within an organization for a given month.

## Features

- **Layered Strategy**: Events API -> Search API -> Targeted per-repo fallback for optimal coverage with minimal API calls
- **Comprehensive Activity Detection**: Tracks commits, pull requests (created/merged/closed), issues (created/closed/reopened), comments, PR reviews, wiki edits, releases, branch/tag creation, and forks
- **Efficient API Usage**: Typically 10-30 API calls vs. 300+ with the legacy approach
- **Coverage Guardrails**: Detects Events API truncation and automatically supplements with search/fallback
- **Flexible Date Ranges**: Specify any year and month
- **JSON Output**: Save results in structured format with detailed daily activity breakdown
- **Deduplication**: Unified dedup registry across all data sources by stable IDs

## Installation

```bash
# No additional dependencies needed beyond Python 3 and GitHub CLI
# Check if gh is installed and authenticated:
gh auth status

# If not authenticated, run:
gh auth login
```

## Usage

```bash
# Basic usage (checks last month by default, uses 'auto' strategy)
python extract_activity_gh.py --org org-name --user username

# Specific date
python extract_activity_gh.py --org org-name --user username --year 2025 --month 12

# Include specific repos (useful for docs repos with active wikis)
python extract_activity_gh.py --org org-name --user username --include-repos docs-repo,wiki-repo

# Events API only (fastest, limited to last 90 days)
python extract_activity_gh.py --org org-name --user username --strategy events-only

# Search only (works for any date range, no 90-day limit)
python extract_activity_gh.py --org org-name --user username --strategy search-only

# Legacy per-repo crawl (backward compatibility)
python extract_activity_gh.py --org org-name --user username --strategy legacy-repos --repo-limit 30

# Save results to JSON
python extract_activity_gh.py --org org-name --user username --output activity_report.json

# Verbose mode shows per-layer progress and API call breakdown
python extract_activity_gh.py --org org-name --user username --verbose
```

### Command Line Arguments

| Argument | Description | Default |
|---|---|---|
| `--org` | GitHub organization name | **required** |
| `--user` | GitHub username | **required** |
| `--year` | Year to analyze | last month's year |
| `--month` | Month to analyze (1-12) | last month |
| `--strategy` | Strategy pipeline (see below) | `auto` |
| `--repo-limit` | Repo limit for `legacy-repos` strategy | 20 |
| `--include-repos` | Comma-separated repos to always include | |
| `--output` | Output file for JSON results | |
| `--verbose` | Show detailed progress and API call stats | |
| `--method` | **(Deprecated)** Maps to `--strategy` | |

## Strategies

### `auto` (default, recommended)

A three-layer pipeline that balances coverage and efficiency:

1. **Layer 1 — Events API** (`/users/{user}/events/orgs/{org}`): Primary source for months within 90 days of today. Parses all event types with accurate timestamps. Detects truncation (API cap of ~300 events or history not reaching month start).

2. **Layer 2 — Search supplements**: Org-wide searches for commits, PRs created, PRs merged, and issues created. Also runs an `involves:` search for repo discovery only (not day attribution).

3. **Layer 3 — Targeted per-repo fallback**: Scans only repos discovered in layers 1-2 (plus `--include-repos`) for: issue comments, PR review comments, commit comments, wiki edits, and PR reviews (with accurate `submitted_at` timestamps). Uses `since` server-side filters where available.

PR review fallback (the most expensive part) is skipped when Events API coverage is complete.

**Typical API calls: 10-30** (vs. 300+ with `legacy-repos`)

### `events-only`

Events API only. Fastest option but limited to the last 90 days and ~300 events. Good for quick checks on recent months for users with moderate activity.

### `search-only`

Search API + targeted per-repo fallback. Works for any date range (no 90-day limit on search queries). Slightly more API calls than `auto` for recent months since it can't skip the PR review fallback.

**Caveat**: Wiki edits and some event types (create/delete/release/fork) are only available through the Events API or the repo events endpoint, which is limited to ~90 days of history. For months older than 90 days, these activity types may be silently missed.

### `legacy-repos`

Backward-compatible per-repo crawl. Scans the N most recently updated repos in the org. Still benefits from correctness fixes (date boundaries) and server-side filters (`since`/`until`/`author` on commits). Use `--repo-limit` to control scope.

## Activity Types Tracked

| Type | Events API | Search API | Per-repo fallback |
|---|---|---|---|
| Commits | PushEvent | search/commits | commits endpoint |
| PR Created | PullRequestEvent (opened) | search/issues type:pr | pr list |
| PR Merged | PullRequestEvent (closed+merged) | search/issues is:merged | - |
| PR Closed/Reopened | PullRequestEvent | - | - |
| PR Review | PullRequestReviewEvent | - | pulls/{n}/reviews |
| Issue Created | IssuesEvent (opened) | search/issues type:issue | issue list |
| Issue Closed/Reopened | IssuesEvent | - | - |
| Issue Comment | IssueCommentEvent | - | issues/comments |
| PR Comment | IssueCommentEvent (on PR) | - | issues/comments |
| PR Review Comment | PullRequestReviewCommentEvent | - | pulls/comments |
| Commit Comment | CommitCommentEvent | - | comments endpoint |
| Wiki Edit | GollumEvent | - | repo events |
| Branch/Tag Created | CreateEvent | - | - |
| Branch/Tag Deleted | DeleteEvent | - | - |
| Release | ReleaseEvent | - | - |
| Fork | ForkEvent | - | - |

## Coverage Notes and Limitations

- **Events API**: Limited to ~300 events and ~90 days of history. For very active users, events may be truncated mid-month. The tool detects this and marks coverage as `partial`, triggering search supplements.
- **Search API**: Cannot find commit comments, wiki edits, or PR reviews. Cannot determine the exact day a PR was reviewed (only the PR's created/updated date). These gaps are filled by the targeted per-repo fallback.
- **PR merges by non-authors**: The `merged:` search qualifier finds PRs authored by the user. Merging someone else's PR is only captured via the Events API (PullRequestEvent where the actor is the merger).
- **Issue triage**: Closing, reopening, and labeling issues is only captured via Events API (IssuesEvent).
- **Wiki edits**: Only available through Events API or repo events endpoint (~90 days retention). For months older than 90 days, wiki edits are silently missed regardless of strategy.
- **GitHub Discussions**: Not currently tracked by any layer.

## Authentication

The tool uses GitHub CLI for authentication. Make sure you have:

1. GitHub CLI installed (`gh` command)
2. Authenticated with GitHub: `gh auth login`
3. Your token has appropriate permissions for the organization

## Output

Example output:
```
=== Activity Summary ===
User: username
Organization: org-name
Period: 2026-03
Strategy: auto
Events coverage: complete
Total active days: 12

API calls: 18 total
  events: 1
  repo-fallback: 12
  search: 5

Detailed daily activity:
  2026-03-01:
    - Commit: abc1234: Fix bug in authentication in repo-name
    - PR Created: #123: Add new feature in repo-name
  2026-03-03:
    - PR Review: on PR #124 (APPROVED) in repo-name
    - Issue Comment: on issue #125 in repo-name
    - Release: published v1.2.0 in repo-name
  ...
```
