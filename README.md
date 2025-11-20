# GitHub Activity Tracker

A Python tool to extract activity days for a specific GitHub user within an organization for a given month.

## Features

- **Dual Search Strategy**: Combines repository-by-repository checking with GitHub search API for comprehensive coverage
- **Comprehensive Activity Detection**: Tracks commits, pull requests, issues, comments, PR reviews, and wiki edits
- **Flexible Date Ranges**: Specify any year and month
- **JSON Output**: Save results in structured format with detailed daily activity breakdown
- **Rate Limit Friendly**: Handles GitHub API pagination and limits
- **Wiki Support**: Tracks wiki edits via GitHub Events API
- **Deduplication**: Automatically deduplicates activity found by both search methods

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

# Include specific repos (useful for docs repos with active wikis)
python extract_activity_gh.py --org org-name --user username --include-repos docs-repo-name,wiki-repo-name

# Limit repo checks but include specific repos
python extract_activity_gh.py --org org-name --user username --repo-limit 10 --include-repos docs-repo-name

# Save results to JSON
python extract_activity_gh.py --org org-name --user username --output activity_report.json
```

### Command Line Arguments
- `--org`: GitHub organization name (**required**)
- `--user`: GitHub username (**required**)
- `--year`: Year to analyze (default: last month's year)
- `--month`: Month to analyze (default: last month)
- `--method`: Method to use - 'repos', 'search', or 'both' (default: both)
- `--repo-limit`: Number of most recent repos to check (default: 20)
- `--include-repos`: Comma-separated list of repos to always include (e.g., "docs-repo-name,wiki-repo-name")
- `--output`: Output file for JSON results
- `--verbose`: Enable verbose output with API call tracking

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
GitHub API calls made: 45

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

Activity types tracked:
- **Commits**: Code commits to repositories
- **PR Created**: Pull requests opened
- **PR Review**: Formal pull request reviews
- **PR Comment**: Comments on pull requests
- **Issue Created**: New issues opened
- **Issue Comment**: Comments on issues
- **Commit Comment**: Comments on specific commits
- **Wiki Edit**: Edits to repository wikis

## How It Works

The tool uses two complementary methods to ensure comprehensive activity tracking:

1. **Repository Method** (`--method repos`):
   - Fetches the most recently updated repositories in the organization
   - Checks each repository individually for all activity types
   - More thorough but makes more API calls
   - Use `--repo-limit` to control how many repos to check (default: 20)
   - Use `--include-repos` to ensure specific repos are always checked (useful for docs repos with wikis)

2. **Search Method** (`--method search`):
   - Uses GitHub's search API to find commits, PRs, and issues
   - Faster with fewer API calls
   - May miss some activity types (like wiki edits or some comments)
   - Good for quick checks across many repositories

3. **Both Methods** (`--method both`, default):
   - Runs both methods and combines results
   - Automatically deduplicates activity
   - Recommended for accurate, comprehensive tracking

## Notes

- The tool automatically handles API pagination
- Tracks comprehensive activity: commits, PRs, issues, comments, reviews, and wiki edits
- Activity is automatically deduplicated when using both methods
- Use `--verbose` flag to see detailed progress and API call statistics
- The `--include-repos` parameter ensures important repositories (like documentation repos with active wikis) are always checked, regardless of `--repo-limit`