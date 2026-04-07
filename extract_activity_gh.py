#!/usr/bin/env python3
"""
GitHub Activity Tracker using GitHub CLI (gh)

Extract activity days for a specific GitHub user in an organization for a given month.
Uses a layered strategy: Events API -> Search API -> Targeted per-repo fallback.
"""

import subprocess
import json
import argparse
from datetime import datetime, timedelta, timezone
from typing import Set, List, Dict, Tuple, Optional
import sys
from urllib.parse import quote_plus
from collections import defaultdict


class GitHubActivityTracker:
    """Track GitHub activity for a user in an organization."""

    def __init__(self, org: str, username: str, year: int, month: int, verbose: bool = False):
        self.org = org
        self.username = username
        self.year = year
        self.month = month
        self.verbose = verbose

        # Date range: [start_date, end_date) — half-open interval
        self.start_date, self.end_date = self._compute_date_range()

        # Detailed daily activity
        self.daily_activity: Dict[str, List[str]] = defaultdict(list)

        # Unified dedup registry keyed by category
        self.seen: Dict[str, set] = {
            'commits': set(),     # commit SHA
            'prs': set(),         # (repo, pr_number, action) e.g. ('repo', 42, 'created')
            'issues': set(),      # (repo, issue_number, action)
            'comments': set(),    # comment id
            'reviews': set(),     # (repo, pr_number, review_id)
            'events': set(),      # event id
            'wiki': set(),        # (repo, page_title, day_str)
            'releases': set(),    # (repo, tag_name)
        }

        # Per-category gh invocation tracking
        self.api_calls: Dict[str, int] = defaultdict(int)

        # Rate limit snapshots for true HTTP request counting
        self.rate_limit_before: Optional[int] = None
        self.rate_limit_after: Optional[int] = None

        # Title cache: (repo, number) -> title, populated from events/search
        self.title_cache: Dict[tuple, str] = {}

        # Repos discovered as touched by the user (populated during execution)
        self.touched_repos: Set[str] = set()

        # Events coverage assessment
        self.events_coverage: str = 'none'  # none, partial, complete

    def _compute_date_range(self) -> Tuple[datetime, datetime]:
        """Compute [start, end) half-open interval for the target month."""
        start = datetime(self.year, self.month, 1, tzinfo=timezone.utc)
        if self.month == 12:
            end = datetime(self.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(self.year, self.month + 1, 1, tzinfo=timezone.utc)
        return start, end

    def in_range(self, dt: datetime) -> bool:
        """Check if datetime falls within [start_date, end_date)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return self.start_date <= dt < self.end_date

    def _parse_dt(self, date_str: str) -> datetime:
        """Parse an ISO datetime string to a timezone-aware datetime."""
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _run_gh(self, cmd: List[str], category: str = 'general') -> subprocess.CompletedProcess:
        """Run a gh command and track invocations by category.

        Note: counts gh CLI invocations, not HTTP requests. A single
        invocation with --paginate may perform multiple HTTP requests
        internally.
        """
        self.api_calls[category] += 1
        self.api_calls['total'] += 1
        return subprocess.run(cmd, capture_output=True, text=True)

    def _dedup(self, category: str, key) -> bool:
        """Returns True if this is a NEW item (not seen before). Adds to seen set."""
        if key in self.seen[category]:
            return False
        self.seen[category].add(key)
        return True

    def _cache_title(self, repo: str, number: int, title: str):
        """Store an issue/PR title in the cache."""
        if title:
            self.title_cache[(repo, number)] = title[:60]

    def _get_title(self, repo: str, number: int) -> str:
        """Get an issue/PR title from cache, or fetch it."""
        key = (repo, number)
        if key in self.title_cache:
            return self.title_cache[key]
        # Fetch from API (works for both issues and PRs)
        cmd = [
            'gh', 'api', f'repos/{self.org}/{repo}/issues/{number}',
            '--jq', '.title',
        ]
        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode == 0 and result.stdout.strip():
            title = result.stdout.strip()[:60]
            self.title_cache[key] = title
            return title
        return ''

    def _add_activity(self, date_str: str, activity_type: str, details: str, repo: str = None):
        """Record an activity entry for a specific date."""
        repo_str = f" in {repo}" if repo else ""
        entry = f"{activity_type}: {details}{repo_str}"
        if entry not in self.daily_activity[date_str]:
            self.daily_activity[date_str].append(entry)

    def _repo_short(self, full_repo: str) -> str:
        """Extract short repo name from org/repo format."""
        return full_repo.split('/')[-1] if '/' in full_repo else full_repo

    def _search_end_date_str(self) -> str:
        """Last day of the target month as YYYY-MM-DD (for search qualifiers)."""
        return (self.end_date - timedelta(days=1)).strftime('%Y-%m-%d')

    def _start_str(self) -> str:
        return self.start_date.strftime('%Y-%m-%d')

    def _since_iso(self) -> str:
        return self.start_date.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _until_iso(self) -> str:
        return self.end_date.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _parse_lines(self, stdout: str):
        """Yield parsed JSON objects from newline-delimited gh jq output."""
        for line in stdout.strip().split('\n'):
            if line and line != 'null':
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    # ─── Authentication check ───

    def check_gh_cli(self) -> bool:
        """Check if GitHub CLI is available and authenticated."""
        try:
            result = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True)
            return result.returncode == 0
        except FileNotFoundError:
            print("Error: GitHub CLI (gh) not found. Install: https://cli.github.com/")
            return False

    def validate_user(self) -> bool:
        """Verify that the GitHub user exists."""
        result = subprocess.run(
            ['gh', 'api', f'users/{self.username}', '--jq', '.login'],
            capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            print(f"Error: GitHub user '{self.username}' not found.", file=sys.stderr)
            return False
        return True

    def snapshot_rate_limit(self) -> Optional[int]:
        """Query GitHub's rate_limit API and return the 'used' count."""
        try:
            result = subprocess.run(
                ['gh', 'api', 'rate_limit', '--jq', '.rate.used'],
                capture_output=True, text=True)
            if result.returncode == 0:
                return int(result.stdout.strip())
        except (ValueError, FileNotFoundError):
            pass
        return None

    def get_http_request_count(self) -> Optional[int]:
        """Return the number of HTTP requests consumed, or None if unavailable."""
        if self.rate_limit_before is not None and self.rate_limit_after is not None:
            return self.rate_limit_after - self.rate_limit_before
        return None

    # ═══════════════════════════════════════════════════════════════════
    # Layer 1: Events API
    # ═══════════════════════════════════════════════════════════════════

    def _fetch_events(self) -> Set[str]:
        """
        Primary source: GET /users/{user}/events/orgs/{org}.
        Returns activity days found. Populates self.touched_repos.
        Only useful for months within ~90 days of today.
        """
        now = datetime.now(timezone.utc)
        if (now - self.end_date).days > 90:
            if self.verbose:
                print("  Target month is >90 days old, skipping Events API")
            self.events_coverage = 'none'
            return set()

        # Check if start_date is outside the retention window (~90 days).
        # If so, the Events API can only cover the recent portion of the month.
        retention_cutoff = now - timedelta(days=90)
        month_straddles_retention = self.start_date < retention_cutoff

        activity_days = set()
        earliest_event_date = None
        event_count = 0

        cmd = [
            'gh', 'api',
            f'/users/{self.username}/events/orgs/{self.org}?per_page=100',
            '--paginate',
            '--jq', '.[]',
        ]

        result = self._run_gh(cmd, category='events')

        if result.returncode != 0:
            if self.verbose:
                print(f"  Events API failed: {result.stderr[:200]}")
            self.events_coverage = 'none'
            return set()

        for event in self._parse_lines(result.stdout):
            event_id = event.get('id')
            if not event_id or not self._dedup('events', event_id):
                continue

            event_type = event.get('type', '')
            created_at_str = event.get('created_at', '')
            if not created_at_str:
                continue
            created_at = self._parse_dt(created_at_str)
            repo = self._repo_short(event.get('repo', {}).get('name', ''))
            payload = event.get('payload', {})

            event_count += 1
            if earliest_event_date is None or created_at < earliest_event_date:
                earliest_event_date = created_at

            if not self.in_range(created_at):
                continue

            self.touched_repos.add(repo)
            day_str = created_at.strftime('%Y-%m-%d')

            self._dispatch_event(event_type, payload, day_str, repo, activity_days)

        # Assess coverage
        # Three reasons events may be incomplete:
        # 1. API cap (~300 events) hit and earliest event is after month start
        # 2. Month straddles the ~90-day retention window (start is outside it)
        # 3. No events returned at all
        if event_count == 0:
            self.events_coverage = 'none'
        elif month_straddles_retention:
            self.events_coverage = 'partial'
            if self.verbose:
                print(f"  Month start {self._start_str()} is outside ~90-day retention "
                      f"window (cutoff ~{retention_cutoff.strftime('%Y-%m-%d')}), marking partial")
        elif event_count >= 300 and earliest_event_date and earliest_event_date > self.start_date:
            self.events_coverage = 'partial'
            if self.verbose:
                print(f"  Events hit API cap ({event_count} events) and earliest="
                      f"{earliest_event_date.strftime('%Y-%m-%d')} > month start, marking partial")
        else:
            self.events_coverage = 'complete'

        if self.verbose:
            print(f"  Events API: {event_count} events, {len(activity_days)} active days, "
                  f"coverage={self.events_coverage}")
            print(f"  Repos touched: {', '.join(sorted(self.touched_repos)) or 'none'}")

        return activity_days

    def _dispatch_event(self, event_type: str, payload: dict, day_str: str, repo: str,
                        activity_days: Set[str]):
        """Route an event to the appropriate handler."""
        handler = {
            'PushEvent': self._handle_push_event,
            'PullRequestEvent': self._handle_pr_event,
            'PullRequestReviewEvent': self._handle_pr_review_event,
            'IssuesEvent': self._handle_issues_event,
            'IssueCommentEvent': self._handle_issue_comment_event,
            'PullRequestReviewCommentEvent': self._handle_pr_review_comment_event,
            'CommitCommentEvent': self._handle_commit_comment_event,
            'GollumEvent': self._handle_gollum_event,
            'CreateEvent': self._handle_create_event,
            'DeleteEvent': self._handle_delete_event,
            'ReleaseEvent': self._handle_release_event,
            'ForkEvent': self._handle_fork_event,
            'MemberEvent': self._handle_generic_event,
            'PublicEvent': self._handle_generic_event,
        }.get(event_type)

        if handler:
            handler(payload, day_str, repo, activity_days, event_type)

    def _handle_push_event(self, payload, day_str, repo, activity_days, _type=None):
        for c in payload.get('commits', []):
            sha = c.get('sha', '')
            if self._dedup('commits', sha):
                msg = c.get('message', '').split('\n')[0][:60]
                self._add_activity(day_str, "Commit", f"{sha[:7]}: {msg}", repo)
                activity_days.add(day_str)

    def _handle_pr_event(self, payload, day_str, repo, activity_days, _type=None):
        action = payload.get('action', '')
        pr = payload.get('pull_request', {})
        pr_num = pr.get('number')
        pr_title = (pr.get('title') or 'Untitled')[:60]
        if not pr_num:
            return
        self._cache_title(repo, pr_num, pr_title)

        if action == 'opened':
            dedup_action = 'created'
            label = "PR Created"
        elif action == 'closed' and pr.get('merged'):
            dedup_action = 'merged'
            label = "PR Merged"
        elif action == 'closed':
            dedup_action = 'closed'
            label = "PR Closed"
        elif action == 'reopened':
            dedup_action = 'reopened'
            label = "PR Reopened"
        else:
            dedup_action = action
            label = f"PR {action.title()}"

        if self._dedup('prs', (repo, pr_num, dedup_action)):
            self._add_activity(day_str, label, f"#{pr_num}: {pr_title}", repo)
            activity_days.add(day_str)

    def _handle_pr_review_event(self, payload, day_str, repo, activity_days, _type=None):
        review = payload.get('review', {})
        pr = payload.get('pull_request', {})
        pr_num = pr.get('number')
        pr_title = (pr.get('title') or '')[:60]
        review_id = review.get('id')
        if pr_num:
            self._cache_title(repo, pr_num, pr_title)
        if pr_num and review_id and self._dedup('reviews', (repo, pr_num, review_id)):
            state = review.get('state', '')
            title_part = f": {pr_title}" if pr_title else ""
            self._add_activity(day_str, "PR Review", f"#{pr_num}{title_part} ({state})", repo)
            activity_days.add(day_str)

    def _handle_issues_event(self, payload, day_str, repo, activity_days, _type=None):
        action = payload.get('action', '')
        issue = payload.get('issue', {})
        issue_num = issue.get('number')
        issue_title = (issue.get('title') or 'Untitled')[:60]
        if not issue_num:
            return
        self._cache_title(repo, issue_num, issue_title)

        label_map = {
            'opened': "Issue Created",
            'closed': "Issue Closed",
            'reopened': "Issue Reopened",
        }
        label = label_map.get(action, f"Issue {action.title()}")

        if self._dedup('issues', (repo, issue_num, action)):
            self._add_activity(day_str, label, f"#{issue_num}: {issue_title}", repo)
            activity_days.add(day_str)

    def _handle_issue_comment_event(self, payload, day_str, repo, activity_days, _type=None):
        comment = payload.get('comment', {})
        issue = payload.get('issue', {})
        comment_id = comment.get('id')
        issue_num = issue.get('number')
        issue_title = (issue.get('title') or '')[:60]
        is_pr = issue.get('pull_request') is not None
        if issue_num and issue_title:
            self._cache_title(repo, issue_num, issue_title)
        if comment_id and self._dedup('comments', comment_id):
            title_part = f": {issue_title}" if issue_title else ""
            if is_pr:
                self._add_activity(day_str, "PR Comment",
                                   f"on PR #{issue_num}{title_part}", repo)
            else:
                self._add_activity(day_str, "Issue Comment",
                                   f"on issue #{issue_num}{title_part}", repo)
            activity_days.add(day_str)

    def _handle_pr_review_comment_event(self, payload, day_str, repo, activity_days, _type=None):
        comment = payload.get('comment', {})
        pr = payload.get('pull_request', {})
        comment_id = comment.get('id')
        pr_num = pr.get('number')
        pr_title = (pr.get('title') or '')[:60]
        if pr_num:
            self._cache_title(repo, pr_num, pr_title)
        if comment_id and self._dedup('comments', comment_id):
            title_part = f": {pr_title}" if pr_title else ""
            self._add_activity(day_str, "PR Review Comment",
                               f"on PR #{pr_num}{title_part}", repo)
            activity_days.add(day_str)

    def _handle_commit_comment_event(self, payload, day_str, repo, activity_days, _type=None):
        comment = payload.get('comment', {})
        comment_id = comment.get('id')
        commit_id = comment.get('commit_id', '')[:7]
        if comment_id and self._dedup('comments', comment_id):
            self._add_activity(day_str, "Commit Comment", f"on commit {commit_id}", repo)
            activity_days.add(day_str)

    def _handle_gollum_event(self, payload, day_str, repo, activity_days, _type=None):
        for page in payload.get('pages', []):
            title = page.get('title', page.get('page_name', 'Unknown'))
            action = page.get('action', 'edited')
            if self._dedup('wiki', (repo, title, day_str)):
                self._add_activity(day_str, "Wiki Edit", f"{action} '{title}'", repo)
                activity_days.add(day_str)

    def _handle_create_event(self, payload, day_str, repo, activity_days, _type=None):
        ref_type = payload.get('ref_type', '')
        ref = payload.get('ref', '')
        if ref_type in ('branch', 'tag'):
            self._add_activity(day_str, f"{ref_type.title()} Created", ref or '(default)', repo)
            activity_days.add(day_str)
        elif ref_type == 'repository':
            self._add_activity(day_str, "Repo Created", repo)
            activity_days.add(day_str)

    def _handle_delete_event(self, payload, day_str, repo, activity_days, _type=None):
        ref_type = payload.get('ref_type', '')
        ref = payload.get('ref', '')
        if ref_type in ('branch', 'tag'):
            self._add_activity(day_str, f"{ref_type.title()} Deleted", ref, repo)
            activity_days.add(day_str)

    def _handle_release_event(self, payload, day_str, repo, activity_days, _type=None):
        release = payload.get('release', {})
        tag = release.get('tag_name', '')
        name = (release.get('name') or tag)[:60]
        action = payload.get('action', 'published')
        if self._dedup('releases', (repo, tag)):
            self._add_activity(day_str, "Release", f"{action} {name}", repo)
            activity_days.add(day_str)

    def _handle_fork_event(self, payload, day_str, repo, activity_days, _type=None):
        forkee = payload.get('forkee', {})
        fork_name = forkee.get('full_name', '')
        self._add_activity(day_str, "Fork", f"created {fork_name}", repo)
        activity_days.add(day_str)

    def _handle_generic_event(self, payload, day_str, repo, activity_days, event_type=None):
        label = (event_type or '').replace('Event', '')
        action = payload.get('action', '')
        self._add_activity(day_str, label, action, repo)
        activity_days.add(day_str)

    # ═══════════════════════════════════════════════════════════════════
    # Layer 2: Search supplements
    # ═══════════════════════════════════════════════════════════════════

    def _search_commits(self) -> Set[str]:
        """Search for commits by this user in the org.

        Note: GitHub's commit search `author:` qualifier does fuzzy matching
        on the git author name, not just the GitHub login. We post-filter
        results by .author.login to avoid false positives.
        """
        activity_days = set()
        query = (f'org:{self.org} author:{self.username} '
                 f'committer-date:{self._start_str()}..{self._search_end_date_str()}')

        cmd = [
            'gh', 'api', f'search/commits?q={quote_plus(query)}&per_page=100',
            '--paginate',
            '--jq', '.items[] | {sha: .sha, date: .commit.author.date, '
                    'message: .commit.message, repo: .repository.name, '
                    'author_login: .author.login}',
        ]

        result = self._run_gh(cmd, category='search')
        if result.returncode != 0:
            if self.verbose:
                print(f"  Commit search failed: {result.stderr[:200]}")
            return activity_days

        for data in self._parse_lines(result.stdout):
            # Post-filter: verify the GitHub login matches exactly
            author_login = data.get('author_login', '')
            if author_login and author_login.lower() != self.username.lower():
                continue
            sha = data.get('sha', '')
            if not self._dedup('commits', sha):
                continue
            dt = self._parse_dt(data['date'])
            if not self.in_range(dt):
                continue
            day_str = dt.strftime('%Y-%m-%d')
            msg = data.get('message', '').split('\n')[0][:60]
            repo = data.get('repo', 'unknown')
            self.touched_repos.add(repo)
            self._add_activity(day_str, "Commit", f"{sha[:7]}: {msg}", repo)
            activity_days.add(day_str)

        if self.verbose:
            print(f"  Commit search: {len(activity_days)} active days")
        return activity_days

    def _search_prs_created(self) -> Set[str]:
        """Search for PRs created by this user."""
        activity_days = set()
        query = (f'org:{self.org} author:{self.username} type:pr '
                 f'created:{self._start_str()}..{self._search_end_date_str()}')

        cmd = [
            'gh', 'api', f'search/issues?q={quote_plus(query)}&per_page=100',
            '--paginate',
            '--jq', '.items[] | {number, created_at, title, repo: .repository_url}',
        ]

        result = self._run_gh(cmd, category='search')
        if result.returncode != 0:
            return activity_days

        for data in self._parse_lines(result.stdout):
            pr_num = data.get('number')
            repo = self._repo_short(data.get('repo', ''))
            if not self._dedup('prs', (repo, pr_num, 'created')):
                continue
            dt = self._parse_dt(data['created_at'])
            if not self.in_range(dt):
                continue
            day_str = dt.strftime('%Y-%m-%d')
            title = (data.get('title') or 'Untitled')[:60]
            self.touched_repos.add(repo)
            self._cache_title(repo, pr_num, title)
            self._add_activity(day_str, "PR Created", f"#{pr_num}: {title}", repo)
            activity_days.add(day_str)

        if self.verbose:
            print(f"  PR created search: {len(activity_days)} active days")
        return activity_days

    def _search_prs_merged(self) -> Set[str]:
        """Search for PRs merged by this user."""
        activity_days = set()
        query = (f'org:{self.org} author:{self.username} type:pr is:merged '
                 f'merged:{self._start_str()}..{self._search_end_date_str()}')

        cmd = [
            'gh', 'api', f'search/issues?q={quote_plus(query)}&per_page=100',
            '--paginate',
            '--jq', '.items[] | {number, closed_at, title, repo: .repository_url}',
        ]

        result = self._run_gh(cmd, category='search')
        if result.returncode != 0:
            return activity_days

        for data in self._parse_lines(result.stdout):
            pr_num = data.get('number')
            repo = self._repo_short(data.get('repo', ''))
            if not self._dedup('prs', (repo, pr_num, 'merged')):
                continue
            # closed_at ≈ merged_at for merged PRs
            dt = self._parse_dt(data.get('closed_at') or data.get('created_at', ''))
            if not self.in_range(dt):
                continue
            day_str = dt.strftime('%Y-%m-%d')
            title = (data.get('title') or 'Untitled')[:60]
            self.touched_repos.add(repo)
            self._cache_title(repo, pr_num, title)
            self._add_activity(day_str, "PR Merged", f"#{pr_num}: {title}", repo)
            activity_days.add(day_str)

        if self.verbose:
            print(f"  PR merged search: {len(activity_days)} active days")
        return activity_days

    def _search_issues(self) -> Set[str]:
        """Search for issues created by this user."""
        activity_days = set()
        query = (f'org:{self.org} author:{self.username} type:issue '
                 f'created:{self._start_str()}..{self._search_end_date_str()}')

        cmd = [
            'gh', 'api', f'search/issues?q={quote_plus(query)}&per_page=100',
            '--paginate',
            '--jq', '.items[] | {number, created_at, title, repo: .repository_url}',
        ]

        result = self._run_gh(cmd, category='search')
        if result.returncode != 0:
            return activity_days

        for data in self._parse_lines(result.stdout):
            issue_num = data.get('number')
            repo = self._repo_short(data.get('repo', ''))
            if not self._dedup('issues', (repo, issue_num, 'opened')):
                continue
            dt = self._parse_dt(data['created_at'])
            if not self.in_range(dt):
                continue
            day_str = dt.strftime('%Y-%m-%d')
            title = (data.get('title') or 'Untitled')[:60]
            self.touched_repos.add(repo)
            self._cache_title(repo, issue_num, title)
            self._add_activity(day_str, "Issue Created", f"#{issue_num}: {title}", repo)
            activity_days.add(day_str)

        if self.verbose:
            print(f"  Issue search: {len(activity_days)} active days")
        return activity_days

    def _search_involved_repos(self) -> Set[str]:
        """
        Use involves: search to discover repos where user participated.
        For REPO DISCOVERY only — not day attribution.
        """
        repos = set()
        query = (f'org:{self.org} involves:{self.username} '
                 f'updated:{self._start_str()}..{self._search_end_date_str()}')

        cmd = [
            'gh', 'api', f'search/issues?q={quote_plus(query)}&per_page=100',
            '--paginate',
            '--jq', '.items[] | .repository_url',
        ]

        result = self._run_gh(cmd, category='search')
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line and line != 'null':
                    repos.add(line.strip().split('/')[-1])

        if self.verbose:
            new_repos = repos - self.touched_repos
            print(f"  Involved repos search: {len(repos)} repos ({len(new_repos)} new)")

        return repos

    # ═══════════════════════════════════════════════════════════════════
    # Layer 3: Targeted per-repo fallback
    # ═══════════════════════════════════════════════════════════════════

    def _fetch_repo_issue_comments(self, repo: str) -> Set[str]:
        """Fetch issue/PR conversation comments for a repo (uses since filter).

        The /issues/comments endpoint returns comments on both issues and PRs.
        We use html_url to distinguish: PR comments contain '/pull/', issue
        comments contain '/issues/'.
        """
        activity_days = set()

        cmd = [
            'gh', 'api',
            f'repos/{self.org}/{repo}/issues/comments?since={self._since_iso()}&per_page=100',
            '--paginate',
            '--jq', '.[] | {id: .id, created_at: .created_at, user: .user.login, '
                    'issue_url: .issue_url, html_url: .html_url}',
        ]

        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode != 0:
            return activity_days

        # Collect comments first, then batch-resolve titles
        pending = []
        for data in self._parse_lines(result.stdout):
            if data.get('user') != self.username:
                continue
            comment_id = data.get('id')
            if not self._dedup('comments', comment_id):
                continue
            dt = self._parse_dt(data['created_at'])
            if not self.in_range(dt):
                continue
            issue_url = data.get('issue_url', '')
            html_url = data.get('html_url', '')
            ref_num_str = issue_url.split('/')[-1] if issue_url else 'unknown'
            try:
                ref_num = int(ref_num_str)
            except ValueError:
                ref_num = None
            is_pr = '/pull/' in html_url
            pending.append((dt.strftime('%Y-%m-%d'), ref_num, is_pr))

        # Batch-resolve unique issue/PR numbers (cache hit = free, miss = 1 API call)
        unique_nums = {num for _, num, _ in pending if num is not None}
        for num in unique_nums:
            if (repo, num) not in self.title_cache:
                self._get_title(repo, num)

        for day_str, ref_num, is_pr in pending:
            title = self.title_cache.get((repo, ref_num), '') if ref_num else ''
            title_part = f": {title}" if title else ""
            if is_pr:
                self._add_activity(day_str, "PR Comment",
                                   f"on PR #{ref_num}{title_part}", repo)
            else:
                self._add_activity(day_str, "Issue Comment",
                                   f"on issue #{ref_num}{title_part}", repo)
            activity_days.add(day_str)

        return activity_days

    def _fetch_repo_pr_review_comments(self, repo: str) -> Set[str]:
        """Fetch inline PR review comments for a repo (uses since filter)."""
        activity_days = set()

        cmd = [
            'gh', 'api',
            f'repos/{self.org}/{repo}/pulls/comments?since={self._since_iso()}&per_page=100',
            '--paginate',
            '--jq', '.[] | {id: .id, created_at: .created_at, user: .user.login, '
                    'pull_request_url: .pull_request_url}',
        ]

        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode != 0:
            return activity_days

        pending = []
        for data in self._parse_lines(result.stdout):
            if data.get('user') != self.username:
                continue
            comment_id = data.get('id')
            if not self._dedup('comments', comment_id):
                continue
            dt = self._parse_dt(data['created_at'])
            if not self.in_range(dt):
                continue
            pr_url = data.get('pull_request_url', '')
            pr_num_str = pr_url.split('/')[-1] if pr_url else 'unknown'
            try:
                pr_num = int(pr_num_str)
            except ValueError:
                pr_num = None
            pending.append((dt.strftime('%Y-%m-%d'), pr_num))

        unique_nums = {num for _, num in pending if num is not None}
        for num in unique_nums:
            if (repo, num) not in self.title_cache:
                self._get_title(repo, num)

        for day_str, pr_num in pending:
            title = self.title_cache.get((repo, pr_num), '') if pr_num else ''
            title_part = f": {title}" if title else ""
            self._add_activity(day_str, "PR Review Comment",
                               f"on PR #{pr_num}{title_part}", repo)
            activity_days.add(day_str)

        return activity_days

    def _fetch_repo_commit_comments(self, repo: str) -> Set[str]:
        """Fetch commit comments for a repo (no since filter available)."""
        activity_days = set()

        cmd = [
            'gh', 'api',
            f'repos/{self.org}/{repo}/comments?per_page=100',
            '--paginate',
            '--jq', '.[] | {id: .id, created_at: .created_at, user: .user.login, '
                    'commit_id: .commit_id}',
        ]

        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode != 0:
            return activity_days

        for data in self._parse_lines(result.stdout):
            if data.get('user') != self.username:
                continue
            comment_id = data.get('id')
            if not self._dedup('comments', comment_id):
                continue
            dt = self._parse_dt(data['created_at'])
            if not self.in_range(dt):
                continue
            day_str = dt.strftime('%Y-%m-%d')
            commit_id = data.get('commit_id', 'unknown')[:7]
            self._add_activity(day_str, "Commit Comment", f"on commit {commit_id}", repo)
            activity_days.add(day_str)

        return activity_days

    def _discover_wiki_repos(self) -> List[str]:
        """Find org repos that have wikis enabled.

        Wiki edits (GollumEvents) only appear on the repo's own events
        endpoint. Repos with only wiki activity are never discovered by
        the normal pipeline (commits/PRs/issues/search), so we need to
        proactively find repos with wikis and scan them.
        """
        cmd = [
            'gh', 'repo', 'list', self.org,
            '--json', 'name,hasWikiEnabled',
            '--limit', '200',
        ]
        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode != 0:
            return []
        try:
            repos = json.loads(result.stdout)
            return [r['name'] for r in repos if r.get('hasWikiEnabled')]
        except (json.JSONDecodeError, KeyError):
            return []

    def _fetch_repo_wiki_edits(self, repo: str) -> Set[str]:
        """Fetch wiki edits via the repo events endpoint."""
        activity_days = set()

        cmd = [
            'gh', 'api', f'repos/{self.org}/{repo}/events?per_page=100',
            '--paginate',
            '--jq', '.[] | select(.type == "GollumEvent") | '
                    '{id: .id, actor: .actor.login, created_at: .created_at, '
                    'pages: .payload.pages}',
        ]

        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode != 0:
            return activity_days

        for data in self._parse_lines(result.stdout):
            if data.get('actor') != self.username:
                continue
            dt = self._parse_dt(data['created_at'])
            if not self.in_range(dt):
                continue
            day_str = dt.strftime('%Y-%m-%d')
            for page in data.get('pages', []):
                title = page.get('title', page.get('page_name', 'Unknown'))
                action = page.get('action', 'edited')
                if self._dedup('wiki', (repo, title, day_str)):
                    self._add_activity(day_str, "Wiki Edit", f"{action} '{title}'", repo)
                    activity_days.add(day_str)

        return activity_days

    def _fetch_repo_pr_reviews(self, repo: str) -> Set[str]:
        """
        Fetch PR reviews with accurate timestamps.
        Lists recently-updated PRs, then fetches reviews for each candidate.
        Breaks early when PRs are older than our window.
        """
        activity_days = set()

        # Get PRs sorted by most recently updated, paginate to avoid
        # missing repos with >100 PRs updated in the window.
        cmd = [
            'gh', 'api',
            f'repos/{self.org}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100',
            '--paginate',
            '--jq', '.[] | {number: .number, updated_at: .updated_at, title: .title}',
        ]

        result = self._run_gh(cmd, category='repo-fallback')
        if result.returncode != 0:
            return activity_days

        candidate_prs = {}  # pr_number -> title
        for data in self._parse_lines(result.stdout):
            updated = self._parse_dt(data['updated_at'])
            if updated < self.start_date:
                break  # sorted desc — everything after is older
            candidate_prs[data['number']] = (data.get('title') or '')[:60]

        for pr_num, pr_title in candidate_prs.items():
            cmd = [
                'gh', 'api',
                f'repos/{self.org}/{repo}/pulls/{pr_num}/reviews',
                '--jq', '.[] | {id: .id, user: .user.login, submitted_at: .submitted_at, '
                        'state: .state}',
            ]

            result = self._run_gh(cmd, category='repo-fallback')
            if result.returncode != 0:
                continue

            for data in self._parse_lines(result.stdout):
                if data.get('user') != self.username:
                    continue
                review_id = data.get('id')
                if not self._dedup('reviews', (repo, pr_num, review_id)):
                    continue
                submitted = data.get('submitted_at')
                if not submitted:
                    continue
                dt = self._parse_dt(submitted)
                if not self.in_range(dt):
                    continue
                day_str = dt.strftime('%Y-%m-%d')
                state = data.get('state', '')
                title_part = f": {pr_title}" if pr_title else ""
                self._add_activity(day_str, "PR Review",
                                   f"#{pr_num}{title_part} ({state})", repo)
                activity_days.add(day_str)

        return activity_days

    # ═══════════════════════════════════════════════════════════════════
    # Strategy orchestration
    # ═══════════════════════════════════════════════════════════════════

    def run_auto(self, include_repos: List[str] = None) -> Set[str]:
        """
        Default layered strategy:
        Events API -> Search supplements -> Targeted per-repo fallback.
        """
        all_days = set()
        include_repos = include_repos or []

        # Layer 1: Events API
        if self.verbose:
            print("=== Layer 1: Events API ===")
        all_days.update(self._fetch_events())

        # Layer 2: Search supplements
        if self.verbose:
            print("\n=== Layer 2: Search Supplements ===")
        all_days.update(self._search_commits())
        all_days.update(self._search_prs_created())
        all_days.update(self._search_prs_merged())
        all_days.update(self._search_issues())

        # Discover repos via involves: search
        involved_repos = self._search_involved_repos()
        self.touched_repos.update(involved_repos)

        # Add explicitly included repos
        for repo in include_repos:
            self.touched_repos.add(repo)

        # Layer 3: Targeted per-repo fallback
        if self.verbose:
            print(f"\n=== Layer 3: Targeted Fallback ({len(self.touched_repos)} repos) ===")

        for repo in sorted(self.touched_repos):
            if self.verbose:
                print(f"  Scanning {repo}...")

            # Comments (search can miss involvement-only comments)
            all_days.update(self._fetch_repo_issue_comments(repo))
            all_days.update(self._fetch_repo_pr_review_comments(repo))

            # Commit comments — search can't find these
            all_days.update(self._fetch_repo_commit_comments(repo))

            # Wiki edits — search can't find these
            all_days.update(self._fetch_repo_wiki_edits(repo))

            # PR reviews — always run. Events can silently drop
            # PullRequestReviewEvent (cap/retention), and search API
            # can't provide accurate review dates. The per-repo
            # /pulls/{n}/reviews endpoint is the only reliable source.
            all_days.update(self._fetch_repo_pr_reviews(repo))

        # Scan wiki-enabled repos not already covered
        wiki_repos = self._discover_wiki_repos()
        extra_wiki_repos = [r for r in wiki_repos if r not in self.touched_repos]
        if extra_wiki_repos:
            if self.verbose:
                print(f"\n=== Wiki scan: {len(extra_wiki_repos)} additional wiki-enabled repos ===")
            for repo in sorted(extra_wiki_repos):
                days = self._fetch_repo_wiki_edits(repo)
                if days:
                    all_days.update(days)
                    if self.verbose:
                        print(f"  Found wiki edits in {repo}")

        return all_days

    def run_events_only(self) -> Set[str]:
        """Events API only — fast, limited to last 90 days."""
        if self.verbose:
            print("=== Strategy: events-only ===")
        return self._fetch_events()

    def run_search_only(self, include_repos: List[str] = None) -> Set[str]:
        """Search API + targeted fallback (no events). Works for any date range."""
        all_days = set()
        include_repos = include_repos or []

        if self.verbose:
            print("=== Strategy: search-only ===")

        all_days.update(self._search_commits())
        all_days.update(self._search_prs_created())
        all_days.update(self._search_prs_merged())
        all_days.update(self._search_issues())

        involved_repos = self._search_involved_repos()
        self.touched_repos.update(involved_repos)
        for repo in include_repos:
            self.touched_repos.add(repo)

        if self.verbose:
            print(f"\n  Targeted fallback for {len(self.touched_repos)} repos")

        for repo in sorted(self.touched_repos):
            if self.verbose:
                print(f"  Scanning {repo}...")
            all_days.update(self._fetch_repo_issue_comments(repo))
            all_days.update(self._fetch_repo_pr_review_comments(repo))
            all_days.update(self._fetch_repo_commit_comments(repo))
            all_days.update(self._fetch_repo_wiki_edits(repo))
            all_days.update(self._fetch_repo_pr_reviews(repo))

        # Scan wiki-enabled repos not already covered
        wiki_repos = self._discover_wiki_repos()
        extra_wiki_repos = [r for r in wiki_repos if r not in self.touched_repos]
        if extra_wiki_repos:
            if self.verbose:
                print(f"\n  Wiki scan: {len(extra_wiki_repos)} additional wiki-enabled repos")
            for repo in sorted(extra_wiki_repos):
                days = self._fetch_repo_wiki_edits(repo)
                if days:
                    all_days.update(days)
                    if self.verbose:
                        print(f"  Found wiki edits in {repo}")

        return all_days

    def run_legacy_repos(self, repo_limit: int = 20, include_repos: List[str] = None) -> Set[str]:
        """
        Legacy per-repo crawl (backward compat / debug).
        Still uses corrected date handling and server-side filters.
        """
        all_days = set()
        include_repos = include_repos or []

        if self.verbose:
            print("=== Strategy: legacy-repos ===")

        cmd = ['gh', 'repo', 'list', self.org, '--json', 'name', '--limit', str(repo_limit)]
        result = self._run_gh(cmd, category='repo-fallback')

        repos = []
        if result.returncode == 0:
            try:
                repos = [r['name'] for r in json.loads(result.stdout)]
            except (json.JSONDecodeError, KeyError):
                pass

        for repo in include_repos:
            if repo not in repos:
                repos.append(repo)

        if self.verbose:
            print(f"  Scanning {len(repos)} repositories")

        for repo in repos:
            if self.verbose:
                print(f"  Scanning {repo}...")

            # Commits with server-side author + since + until
            cmd = [
                'gh', 'api',
                f'repos/{self.org}/{repo}/commits?author={self.username}'
                f'&since={self._since_iso()}&until={self._until_iso()}&per_page=100',
                '--paginate',
                '--jq', '.[] | {sha: .sha, date: .commit.author.date, '
                        'message: .commit.message}',
            ]
            result = self._run_gh(cmd, category='repo-fallback')
            if result.returncode == 0:
                for data in self._parse_lines(result.stdout):
                    sha = data.get('sha', '')
                    if not self._dedup('commits', sha):
                        continue
                    dt = self._parse_dt(data['date'])
                    if not self.in_range(dt):
                        continue
                    day_str = dt.strftime('%Y-%m-%d')
                    msg = data.get('message', '').split('\n')[0][:60]
                    self._add_activity(day_str, "Commit", f"{sha[:7]}: {msg}", repo)
                    all_days.add(day_str)

            # PRs created
            cmd = [
                'gh', 'pr', 'list',
                '--repo', f'{self.org}/{repo}',
                '--author', self.username,
                '--state', 'all',
                '--json', 'createdAt,title,number',
                '--limit', '1000',
            ]
            result = self._run_gh(cmd, category='repo-fallback')
            if result.returncode == 0:
                try:
                    prs = json.loads(result.stdout) if result.stdout.strip() else []
                    for pr in prs:
                        pr_num = pr.get('number')
                        if not self._dedup('prs', (repo, pr_num, 'created')):
                            continue
                        dt = self._parse_dt(pr['createdAt'])
                        if not self.in_range(dt):
                            continue
                        day_str = dt.strftime('%Y-%m-%d')
                        title = (pr.get('title') or 'Untitled')[:60]
                        self._add_activity(day_str, "PR Created", f"#{pr_num}: {title}", repo)
                        all_days.add(day_str)
                except (json.JSONDecodeError, KeyError):
                    pass

            # Issues created
            cmd = [
                'gh', 'issue', 'list',
                '--repo', f'{self.org}/{repo}',
                '--author', self.username,
                '--state', 'all',
                '--json', 'createdAt,title,number',
                '--limit', '1000',
            ]
            result = self._run_gh(cmd, category='repo-fallback')
            if result.returncode == 0:
                try:
                    issues = json.loads(result.stdout) if result.stdout.strip() else []
                    for issue in issues:
                        issue_num = issue.get('number')
                        if not self._dedup('issues', (repo, issue_num, 'opened')):
                            continue
                        dt = self._parse_dt(issue['createdAt'])
                        if not self.in_range(dt):
                            continue
                        day_str = dt.strftime('%Y-%m-%d')
                        title = (issue.get('title') or 'Untitled')[:60]
                        self._add_activity(day_str, "Issue Created",
                                           f"#{issue_num}: {title}", repo)
                        all_days.add(day_str)
                except (json.JSONDecodeError, KeyError):
                    pass

            # Comments, wiki, reviews — reuse targeted fallback methods
            all_days.update(self._fetch_repo_issue_comments(repo))
            all_days.update(self._fetch_repo_pr_review_comments(repo))
            all_days.update(self._fetch_repo_commit_comments(repo))
            all_days.update(self._fetch_repo_wiki_edits(repo))
            all_days.update(self._fetch_repo_pr_reviews(repo))

        return all_days


def main():
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    default_year = last_month.year
    default_month = last_month.month

    parser = argparse.ArgumentParser(
        description='Extract GitHub activity days using GitHub CLI')
    parser.add_argument('--org', required=True, help='GitHub organization name')
    parser.add_argument('--user', required=True, help='GitHub username')
    parser.add_argument('--year', type=int, default=default_year,
                        help=f'Year (default: {default_year})')
    parser.add_argument('--month', type=int, default=default_month,
                        help=f'Month 1-12 (default: {default_month})')
    parser.add_argument('--strategy',
                        choices=['auto', 'events-only', 'search-only', 'legacy-repos'],
                        default='auto',
                        help='Strategy: auto (default), events-only, search-only, legacy-repos')
    parser.add_argument('--repo-limit', type=int, default=20,
                        help='Repo limit for legacy-repos strategy (default: 20)')
    parser.add_argument('--include-repos', type=str, default='',
                        help='Comma-separated repos to always include')
    parser.add_argument('--verbose', action='store_true', help='Show detailed progress')
    parser.add_argument('--output', help='Output file (JSON format)')

    # Backward compatibility
    parser.add_argument('--method', choices=['repos', 'search', 'both'],
                        help='(Deprecated) Use --strategy instead')

    args = parser.parse_args()

    # Map deprecated --method to --strategy
    if args.method:
        method_map = {'repos': 'legacy-repos', 'search': 'search-only', 'both': 'auto'}
        args.strategy = method_map[args.method]
        print(f"Note: --method is deprecated, use --strategy={args.strategy}", file=sys.stderr)

    tracker = GitHubActivityTracker(args.org, args.user, args.year, args.month,
                                    verbose=args.verbose)

    if not tracker.check_gh_cli():
        sys.exit(1)

    if not tracker.validate_user():
        sys.exit(1)

    include_repos = ([r.strip() for r in args.include_repos.split(',') if r.strip()]
                     if args.include_repos else [])

    # Snapshot rate limit before run to compute true HTTP request count
    tracker.rate_limit_before = tracker.snapshot_rate_limit()

    if args.strategy == 'auto':
        all_days = tracker.run_auto(include_repos=include_repos)
    elif args.strategy == 'events-only':
        all_days = tracker.run_events_only()
    elif args.strategy == 'search-only':
        all_days = tracker.run_search_only(include_repos=include_repos)
    elif args.strategy == 'legacy-repos':
        all_days = tracker.run_legacy_repos(repo_limit=args.repo_limit,
                                            include_repos=include_repos)
    else:
        all_days = set()

    # Snapshot rate limit after run
    tracker.rate_limit_after = tracker.snapshot_rate_limit()

    # Sort and display
    sorted_days = sorted(list(all_days))

    print(f"\n=== Activity Summary ===")
    print(f"User: {args.user}")
    print(f"Organization: {args.org}")
    print(f"Period: {args.year}-{args.month:02d}")
    print(f"Strategy: {args.strategy}")
    if tracker.events_coverage != 'none':
        print(f"Events coverage: {tracker.events_coverage}")
    print(f"Total active days: {len(sorted_days)}")

    # API usage stats
    total_invocations = tracker.api_calls.get('total', 0)
    http_requests = tracker.get_http_request_count()
    if http_requests is not None:
        print(f"\nGitHub API requests: {http_requests} (across {total_invocations} gh invocations)")
    else:
        print(f"\ngh invocations: {total_invocations} total")
    for cat in sorted(k for k in tracker.api_calls if k != 'total'):
        print(f"  {cat}: {tracker.api_calls[cat]}")

    if sorted_days:
        print(f"\nDetailed daily activity:")
        for day in sorted_days:
            activities = tracker.daily_activity.get(day, ["No detailed info"])
            print(f"  {day}:")
            for activity in activities:
                print(f"    - {activity}")
    else:
        print("\nNo activity found for the specified period.")

    # Save to file if requested
    if args.output:
        result = {
            'user': args.user,
            'organization': args.org,
            'year': args.year,
            'month': args.month,
            'strategy': args.strategy,
            'events_coverage': tracker.events_coverage,
            'total_active_days': len(sorted_days),
            'active_days': sorted_days,
            'daily_activity_details': dict(tracker.daily_activity),
            'api_usage': {
                'http_requests': http_requests,
                'gh_invocations': total_invocations,
                'by_category': {k: v for k, v in tracker.api_calls.items() if k != 'total'},
            },
            'generated_at': datetime.now().isoformat(),
        }

        with open(args.output, 'w') as f:
            json.dump(result, f, indent=2)

        print(f"\nResults saved to: {args.output}")


if __name__ == '__main__':
    main()
