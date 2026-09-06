#!/usr/bin/env python3
"""
GitHub Activity Tracker using GitHub CLI (gh)

Extract activity days for a specific GitHub user in an organization for a given month.
Uses the GitHub CLI for simpler and more reliable API access.
"""

import subprocess
import json
import argparse
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Set, List, Dict
import sys
from urllib.parse import quote_plus
from collections import defaultdict


class GitHubCLIActivityTracker:
    def __init__(self, org: str, username: str, year: int, month: int, verbose: bool = False):
        self.org = org
        self.username = username
        self.year = year
        self.month = month
        self.verbose = verbose
        # Track detailed daily activity
        self.daily_activity = defaultdict(list)
        # Global deduplication tracking
        self.seen_commits = set()  # Track commit SHAs globally
        self.seen_prs = set()  # Track PR (repo, number) tuples globally
        # API usage counters
        self.api_call_count = 0      # HTTP requests actually sent
        self.gh_command_count = 0    # gh subprocesses spawned
        # Per-repo comment scans already done in this run, keyed by (kind, repo)
        self.comment_scans = {}
        # Calls that failed, so an incomplete report can say so
        self.failures = []
        # Common variations of the username to search for
        self.username_variations = [
            username.lower(),
            username,
            username.title(),
            f"{username.title()} {username.title()}",  # If username is first name only
        ]

    @staticmethod
    def _normalise(text: str) -> str:
        """Fold accents and drop separators, so 'Renée Dupont' -> 'reneedupont'."""
        decomposed = unicodedata.normalize('NFKD', text or '')
        stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
        return ''.join(c for c in stripped.lower() if c.isalnum())

    def _is_user_commit(self, login: str, author_name: str, author_email: str) -> bool:
        """Decide whether a commit belongs to the tracked user.

        GitHub resolves commits to an account itself and exposes it as
        `.author.login`. When that is present it is authoritative: trust it and
        stop. Matching on the git author name or email is a deliberate fallback
        for the one case GitHub cannot resolve -- a commit whose author email is
        not attached to any GitHub account -- and it is guesswork, so it is kept
        deliberately narrow.
        """
        login = (login or '').strip()
        if login:
            return login.lower() == self.username.lower()

        target = self._normalise(self.username)
        author_email = (author_email or '').strip()
        local_part = author_email.split('@')[0]
        # GitHub noreply addresses look like "12345+username@users.noreply.github.com"
        if '+' in local_part:
            local_part = local_part.split('+', 1)[1]
        if self._normalise(local_part) == target:
            return True

        return self._normalise(author_name) == target

    def get_month_date_range(self) -> tuple:
        """Get start and end dates for the specified month."""
        start_date = datetime(self.year, self.month, 1, tzinfo=timezone.utc)

        # End of the month is the last *instant* of the last day. Subtracting a
        # whole day here would silently drop everything after 00:00 on the 31st.
        if self.month == 12:
            end_date = datetime(self.year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1)
        else:
            end_date = datetime(self.year, self.month + 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1)

        return start_date, end_date

    @staticmethod
    def _describe_command(cmd: List[str]) -> str:
        """Name a gh command well enough to identify it in a warning."""
        if len(cmd) >= 3 and cmd[1] == 'api':
            target = cmd[2].split('?')[0]
        else:
            target = ' '.join(cmd[1:3])
        if '--repo' in cmd:
            target = f"{target} ({cmd[cmd.index('--repo') + 1]})"
        return target

    def _record_failure(self, context: str, detail: str = '') -> None:
        """Record a failed call, and say so at once.

        A swallowed failure quietly drops activity from the report: the run
        still looks clean, and two runs of the same month disagree with no
        visible reason. Anything that costs the report data has to be
        audible, so warn on stderr now and list it in the summary later.
        """
        lines = [line for line in (detail or '').splitlines() if line.strip()]
        summary = lines[-1].strip()[:200] if lines else 'no error output'
        self.failures.append(f"{context}: {summary}")
        print(f"Warning: {context} failed: {summary}", file=sys.stderr)

    @staticmethod
    def _count_requests(stderr) -> int:
        """Count the HTTP requests recorded in a GH_DEBUG=api stderr stream."""
        if not stderr:
            return 0
        return sum(1 for line in stderr.splitlines()
                   if line.startswith('* Request to '))

    def _run_gh_command(self, cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
        """Run a gh command and count the HTTP requests it makes.

        A single gh subprocess can send many requests: --paginate walks one
        request per page inside it, and `gh pr list` issues its own GraphQL
        calls. Counting subprocesses understates real API usage by an order
        of magnitude, so count the requests instead. GH_DEBUG=api makes gh
        log every request to stderr, which every caller already captures.
        The token is redacted in that output.
        """
        kwargs.setdefault('env', dict(os.environ, GH_DEBUG='api'))
        self.gh_command_count += 1
        try:
            result = subprocess.run(cmd, **kwargs)
        except subprocess.CalledProcessError as e:
            self.api_call_count += self._count_requests(e.stderr)
            self._record_failure(self._describe_command(cmd), e.stderr)
            raise
        self.api_call_count += self._count_requests(result.stderr)
        if result.returncode != 0:
            self._record_failure(self._describe_command(cmd), result.stderr)
        return result

    def _scan_repo_comments(self, kind: str, repo_name: str, start_date: datetime,
                            end_date: datetime) -> Set[str]:
        """Scan a repository's comments, at most once per run.

        These scans walk a repository's entire comment history, and the
        search path asks for the same repository once per matching item. On
        a busy repository that repeats a 50-page walk dozens of times for
        identical data. Caching the result is safe because the side effect,
        add_activity, already discards duplicate entries.
        """
        key = (kind, repo_name)
        if key not in self.comment_scans:
            scan = (self._get_issue_comments_for_repo if kind == 'issue'
                    else self._get_pr_comments_for_repo)
            self.comment_scans[key] = scan(repo_name, start_date, end_date)
        return self.comment_scans[key]
    
    def add_activity(self, date_str: str, activity_type: str, details: str, repo: str = None):
        """Add detailed activity for a specific date."""
        repo_str = f" in {repo}" if repo else ""
        activity_str = f"{activity_type}: {details}{repo_str}"
        # Avoid duplicate activity descriptions
        if activity_str not in self.daily_activity[date_str]:
            self.daily_activity[date_str].append(activity_str)

    def check_gh_cli(self) -> bool:
        """Check if GitHub CLI is available and authenticated."""
        try:
            result = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True)
            return result.returncode == 0
        except FileNotFoundError:
            print("Error: GitHub CLI (gh) not found. Please install it first:")
            print("  https://cli.github.com/")
            return False

    def get_org_repos(self, limit: int = 20) -> List[str]:
        """Get repository names in the organization (most recently updated first)."""
        try:
            cmd = ['gh', 'repo', 'list', self.org, '--json', 'name', '--limit', str(limit)]
            result = self._run_gh_command(cmd, capture_output=True, text=True, check=True)
            
            repos_data = json.loads(result.stdout)
            return [repo['name'] for repo in repos_data]
        except subprocess.CalledProcessError as e:
            self._record_failure("fetching the repository list", str(e))
            return []
        except json.JSONDecodeError as e:
            self._record_failure("parsing the repository list", str(e))
            return []

    def _get_active_repos(self, start_date: datetime, end_date: datetime) -> Set[str]:
        """Find repositories the user touched in the month, by search.

        get_org_repos orders by last push, which measures code pushes rather
        than this user's activity, so a repository dormant for months can
        still carry a review or a comment made during the month. No cutoff on
        that ordering catches those: in one observed month the user's active
        repositories ranked as low as 40th, while only 16 were active at all.
        Ask search which repositories the user actually touched, and scan
        those on top of the ranked list.
        """
        repos = set()
        start = start_date.strftime('%Y-%m-%d')
        end = end_date.strftime('%Y-%m-%d')
        searches = [
            # author-date matches the date this report counts commits by
            (f'org:{self.org} author:{self.username} author-date:{start}..{end}',
             'search/commits', '.items[].repository.name'),
            # involves covers authoring, commenting, assignment and mentions
            (f'org:{self.org} involves:{self.username} updated:>={start}',
             'search/issues', '.items[] | (.repository_url|split("/")|last)'),
        ]

        for query, endpoint, jq in searches:
            try:
                cmd = [
                    'gh', 'api', f'{endpoint}?q={quote_plus(query)}&per_page=100',
                    '--paginate',
                    '--jq', jq
                ]
                result = self._run_gh_command(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        name = line.strip()
                        if name and name != 'null':
                            repos.add(name)
            except Exception as e:
                self._record_failure(f"repository discovery via {endpoint} failed", str(e))

        return repos

    def get_user_activity(self, repo_limit: int = 20, include_repos: List[str] = None) -> Set[str]:
        """Get all activity days for the user in the organization."""
        if not self.check_gh_cli():
            return set()

        activity_days = set()
        start_date, end_date = self.get_month_date_range()

        if self.verbose:
            print(f"Fetching activity using GitHub CLI for {self.username} in {self.org}")
            print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

        # Get repositories in the organization (limited to most recent)
        repos = self.get_org_repos(limit=repo_limit)

        # Add any specifically included repos (avoid duplicates)
        if include_repos:
            for repo in include_repos:
                if repo not in repos:
                    repos.append(repo)

        # Add repositories the ranked list misses, so a dormant repository the
        # user reviewed or commented in this month is still scanned.
        added = [repo for repo in sorted(self._get_active_repos(start_date, end_date))
                 if repo not in repos]
        repos.extend(added)
        if self.verbose and added:
            print(f"Search added {len(added)} repositories the ranking missed: "
                  f"{', '.join(added)}")

        if self.verbose:
            print(f"Checking {len(repos)} repositories in {self.org}")

        for repo_name in repos:
            if self.verbose:
                print(f"Checking repository: {repo_name}")

            # Get commits
            commits_days = self._get_commits_for_repo(repo_name, start_date, end_date)
            activity_days.update(commits_days)

            # Get pull requests
            pr_days = self._get_prs_for_repo(repo_name, start_date, end_date)
            activity_days.update(pr_days)

            # Get issues created
            issue_days = self._get_issues_for_repo(repo_name, start_date, end_date)
            activity_days.update(issue_days)

            # Get issue comments
            issue_comment_days = self._scan_repo_comments('issue', repo_name, start_date, end_date)
            activity_days.update(issue_comment_days)

            # Get PR comments
            pr_comment_days = self._scan_repo_comments('pr', repo_name, start_date, end_date)
            activity_days.update(pr_comment_days)

            # Get commit comments
            commit_comment_days = self._get_commit_comments_for_repo(repo_name, start_date, end_date)
            activity_days.update(commit_comment_days)

            # Get wiki edits
            wiki_days = self._get_wiki_edits_for_repo(repo_name, start_date, end_date)
            activity_days.update(wiki_days)

        # Reviews are looked up once for the whole organisation rather than per
        # repository, so this sits outside the loop.
        activity_days.update(self._get_review_days(start_date, end_date))

        return activity_days

    def _get_wiki_edits_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get wiki edit days for the user in a specific repository."""
        activity_days = set()

        try:
            # Get repository events and filter for GollumEvent (wiki edits)
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/events',
                '--paginate',
                '--jq', '.[] | select(.type == "GollumEvent") | {actor: .actor.login, created_at: .created_at, pages: .payload.pages}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                wiki_edit_count = 0
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            event_data = json.loads(line)
                            actor = event_data.get('actor', '')

                            # Check if this edit is by our user
                            if actor == self.username:
                                event_date = datetime.fromisoformat(event_data['created_at'].replace('Z', '+00:00'))

                                # Filter by date range
                                if start_date <= event_date <= end_date:
                                    day_str = event_date.strftime('%Y-%m-%d')
                                    activity_days.add(day_str)

                                    # Get page names from the event
                                    pages = event_data.get('pages', [])
                                    for page in pages:
                                        page_name = page.get('title', page.get('page_name', 'Unknown'))
                                        action = page.get('action', 'edited')
                                        self.add_activity(day_str, "Wiki Edit", f"{action} '{page_name}'", repo_name)

                                    wiki_edit_count += 1

                        except (json.JSONDecodeError, KeyError) as e:
                            continue

                if wiki_edit_count > 0 and self.verbose:
                    print(f"  → Found {wiki_edit_count} wiki edits by {self.username}")

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking wiki edits in {repo_name}", str(e))

        return activity_days

    def _get_commits_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get commit days for the user in a specific repository."""
        activity_days = set()
        
        try:
            # Get recent commits and filter by date and author locally
            # since= filters on the committer date, which is never earlier
            # than the author date this report counts by, so a commit authored
            # within the month cannot be filtered out here. There is no until=:
            # that reasoning does not hold at the top of the range, and the
            # local date check below bounds it anyway.
            since = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            cmd = [
                'gh', 'api', 
                f'repos/{self.org}/{repo_name}/commits?per_page=100&since={since}',
                '--paginate',
                '--jq', '.[] | {date: .commit.author.date, login: (.author.login // ""), name: .commit.author.name, email: .commit.author.email, sha: .sha, message: .commit.message}'
            ]
            
            result = self._run_gh_command(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                commit_count = 0
                for line in lines:
                    if line and line != 'null':
                        try:
                            commit_data = json.loads(line)
                            author_name = commit_data.get('name', '').strip()
                            author_email = commit_data.get('email', '').strip()
                            commit_sha = commit_data.get('sha', '')  # Full hash for deduplication
                            commit_message = commit_data.get('message', '').split('\n')[0][:60]  # First line, truncated

                            # Check if this commit is by our user
                            is_user_commit = self._is_user_commit(
                                commit_data.get('login', ''), author_name, author_email
                            )

                            if is_user_commit:
                                # Check if we've already seen this commit globally
                                if commit_sha in self.seen_commits:
                                    continue
                                self.seen_commits.add(commit_sha)

                                commit_date = datetime.fromisoformat(commit_data['date'].replace('Z', '+00:00'))
                                # Filter by date range locally
                                if start_date <= commit_date <= end_date:
                                    day_str = commit_date.strftime('%Y-%m-%d')
                                    activity_days.add(day_str)
                                    # Record detailed activity (use short hash for display)
                                    short_sha = commit_sha[:7]
                                    self.add_activity(day_str, "Commit", f"{short_sha}: {commit_message}", repo_name)
                                    commit_count += 1
                                
                        except (json.JSONDecodeError, KeyError) as e:
                            continue
                
                if commit_count > 0 and self.verbose:
                    print(f"  → Found {commit_count} commits by {self.username}")

        except subprocess.CalledProcessError:
            # Repository might not exist or no access
            pass
        except Exception as e:
            self._record_failure(f"Error checking commits in {repo_name}", str(e))
        
        return activity_days

    def _get_prs_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get pull request creation days for the user in a specific repository."""
        activity_days = set()
        
        # Get PRs created by the user
        try:
            cmd = [
                'gh', 'pr', 'list',
                '--repo', f'{self.org}/{repo_name}',
                '--author', self.username,
                '--state', 'all',
                '--json', 'createdAt,title,number',
                '--limit', '1000'
            ]
            
            result = self._run_gh_command(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                prs_data = json.loads(result.stdout) if result.stdout.strip() else []
                
                for pr in prs_data:
                    pr_number = pr.get('number')
                    pr_key = (repo_name, pr_number)
                    
                    # Check if we've already seen this PR globally
                    if pr_key in self.seen_prs:
                        continue
                    self.seen_prs.add(pr_key)
                    
                    pr_date = datetime.fromisoformat(pr['createdAt'].replace('Z', '+00:00'))
                    if pr_date.tzinfo is None:
                        pr_date = pr_date.replace(tzinfo=timezone.utc)
                    if start_date <= pr_date <= end_date:
                        day_str = pr_date.strftime('%Y-%m-%d')
                        activity_days.add(day_str)
                        pr_title = pr.get('title', 'Untitled')[:60]
                        self.add_activity(day_str, "PR Created", f"#{pr_number}: {pr_title}", repo_name)
            
        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking PRs in {repo_name}", str(e))
        
        # Reviews are not collected here. See _get_review_days, which asks
        # which pull requests this user reviewed rather than inspecting every
        # pull request in the repository.
        return activity_days

    def _get_review_days(self, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get review days across the organisation, driven by search.

        Listing every pull request in a repository and fetching each one's
        reviews costs a request per pull request, and the cost grows every
        month, because any pull request touched since the month started has
        to be inspected. The search API answers the question directly:
        reviewed-by returns only the pull requests this user submitted a
        review on, which is a far smaller set and does not grow with
        unrelated activity.
        """
        activity_days = set()
        since = start_date.strftime('%Y-%m-%d')
        query = (f'org:{self.org} type:pr reviewed-by:{self.username} '
                 f'updated:>={since}')

        try:
            cmd = [
                'gh', 'api', f'search/issues?q={quote_plus(query)}&per_page=100',
                '--paginate',
                '--jq', '.items[] | {number: .number, repo: .repository_url}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            item = json.loads(line)
                            repo_url = item.get('repo', '')
                            if not repo_url:
                                continue
                            repo_name = repo_url.split('/')[-1]
                            review_days = self._get_reviews_for_pr(
                                repo_name, item['number'], start_date, end_date)
                            activity_days.update(review_days)
                        except (json.JSONDecodeError, KeyError):
                            continue

            if self.verbose:
                print(f"Found {len(activity_days)} days with PR reviews")

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking PR reviews", str(e))

        return activity_days

    def _get_reviews_for_pr(self, repo_name: str, pr_number: int, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get review days for a specific PR."""
        review_days = set()
        
        try:
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/pulls/{pr_number}/reviews',
                '--jq', '.[] | {user: .user.login, submitted_at: .submitted_at}'
            ]
            
            result = self._run_gh_command(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            review_data = json.loads(line)
                            reviewer = review_data.get('user', '')
                            submitted_at = review_data.get('submitted_at')
                            
                            if submitted_at and reviewer == self.username:
                                review_date = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
                                if start_date <= review_date <= end_date:
                                    day_str = review_date.strftime('%Y-%m-%d')
                                    review_days.add(day_str)
                                    self.add_activity(day_str, "PR Review", f"on PR #{pr_number}", repo_name)
                                    
                        except (json.JSONDecodeError, KeyError):
                            continue
            
        except subprocess.CalledProcessError:
            pass
        except Exception:
            pass
        
        return review_days

    def _get_issues_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get issues created by the user in a specific repository."""
        activity_days = set()

        try:
            cmd = [
                'gh', 'issue', 'list',
                '--repo', f'{self.org}/{repo_name}',
                '--author', self.username,
                '--state', 'all',
                '--json', 'createdAt,title,number',
                '--limit', '1000'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                issues_data = json.loads(result.stdout) if result.stdout.strip() else []

                for issue in issues_data:
                    issue_number = issue.get('number')
                    issue_date = datetime.fromisoformat(issue['createdAt'].replace('Z', '+00:00'))
                    if issue_date.tzinfo is None:
                        issue_date = issue_date.replace(tzinfo=timezone.utc)
                    if start_date <= issue_date <= end_date:
                        day_str = issue_date.strftime('%Y-%m-%d')
                        activity_days.add(day_str)
                        issue_title = issue.get('title', 'Untitled')[:60]
                        self.add_activity(day_str, "Issue Created", f"#{issue_number}: {issue_title}", repo_name)

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking issues in {repo_name}", str(e))

        return activity_days

    def _get_issue_comments_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get issue comments by the user in a specific repository."""
        activity_days = set()

        try:
            # since= filters on updated_at, which is never earlier than
            # created_at, so a comment created within the month cannot be
            # filtered out. The local check below bounds the top of the range.
            since = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/issues/comments?per_page=100&since={since}',
                '--paginate',
                '--jq', '.[] | {created_at: .created_at, user: .user.login, issue_url: .issue_url, id: .id}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            comment_data = json.loads(line)
                            if comment_data.get('user') == self.username:
                                comment_date = datetime.fromisoformat(comment_data['created_at'].replace('Z', '+00:00'))
                                if start_date <= comment_date <= end_date:
                                    day_str = comment_date.strftime('%Y-%m-%d')
                                    activity_days.add(day_str)
                                    # Extract issue number from URL
                                    issue_url = comment_data.get('issue_url', '')
                                    issue_number = issue_url.split('/')[-1] if issue_url else 'unknown'
                                    self.add_activity(day_str, "Issue Comment", f"on issue #{issue_number}", repo_name)
                        except (json.JSONDecodeError, KeyError):
                            continue

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking issue comments in {repo_name}", str(e))

        return activity_days

    def _get_pr_comments_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get PR comments (both review comments and issue comments on PRs) by the user."""
        activity_days = set()

        # Get review comments (comments on specific code lines)
        try:
            # Same reasoning as the issue comments above: updated_at is never
            # earlier than created_at.
            since = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/pulls/comments?per_page=100&since={since}',
                '--paginate',
                '--jq', '.[] | {created_at: .created_at, user: .user.login, pull_request_url: .pull_request_url, id: .id}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            comment_data = json.loads(line)
                            if comment_data.get('user') == self.username:
                                comment_date = datetime.fromisoformat(comment_data['created_at'].replace('Z', '+00:00'))
                                if start_date <= comment_date <= end_date:
                                    day_str = comment_date.strftime('%Y-%m-%d')
                                    activity_days.add(day_str)
                                    # Extract PR number from URL
                                    pr_url = comment_data.get('pull_request_url', '')
                                    pr_number = pr_url.split('/')[-1] if pr_url else 'unknown'
                                    self.add_activity(day_str, "PR Comment", f"on PR #{pr_number}", repo_name)
                        except (json.JSONDecodeError, KeyError):
                            continue

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking PR comments in {repo_name}", str(e))

        return activity_days

    def _get_commit_comments_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get commit comments by the user in a specific repository."""
        activity_days = set()

        try:
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/comments?per_page=100',
                '--paginate',
                '--jq', '.[] | {created_at: .created_at, user: .user.login, commit_id: .commit_id, id: .id}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            comment_data = json.loads(line)
                            if comment_data.get('user') == self.username:
                                comment_date = datetime.fromisoformat(comment_data['created_at'].replace('Z', '+00:00'))
                                if start_date <= comment_date <= end_date:
                                    day_str = comment_date.strftime('%Y-%m-%d')
                                    activity_days.add(day_str)
                                    commit_id = comment_data.get('commit_id', 'unknown')[:7]
                                    self.add_activity(day_str, "Commit Comment", f"on commit {commit_id}", repo_name)
                        except (json.JSONDecodeError, KeyError):
                            continue

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            self._record_failure(f"Error checking commit comments in {repo_name}", str(e))

        return activity_days

    def get_user_search_activity(self) -> Set[str]:
        """Alternative method using GitHub search (may be more comprehensive)."""
        if not self.check_gh_cli():
            return set()

        activity_days = set()
        start_date, end_date = self.get_month_date_range()

        if self.verbose:
            print(f"Using GitHub search for {self.username} activity in {self.org}")
        
        # Search for commits using multiple author variations
        commit_days = set()
        
        try:
            # Try each username variation
            for username_var in self.username_variations:
                search_query = f'org:{self.org} author:"{username_var}" committer-date:{start_date.strftime("%Y-%m-%d")}..{end_date.strftime("%Y-%m-%d")}'
                encoded_query = quote_plus(search_query)
                
                cmd = [
                    'gh', 'api', f'search/commits?q={encoded_query}',
                    '--paginate',
                    '--jq', '.items[] | {date: .commit.author.date, sha: .sha, message: .commit.message, repo: .repository.name, login: (.author.login // ""), author_name: .commit.author.name, author_email: .commit.author.email}'
                ]
                
                result = self._run_gh_command(cmd, capture_output=True, text=True)
                
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        if line and line != 'null':
                            try:
                                commit_info = json.loads(line)
                                commit_sha = commit_info.get('sha', '')
                                author_name = commit_info.get('author_name', '').strip()
                                author_email = commit_info.get('author_email', '').strip()
                                
                                # Skip if we've already seen this commit globally
                                if commit_sha in self.seen_commits:
                                    continue

                                # Verify the author actually matches our user (don't trust search API blindly!)
                                if not self._is_user_commit(
                                    commit_info.get('login', ''), author_name, author_email
                                ):
                                    # Do not mark it seen: this variation rejected it,
                                    # another pass may still legitimately claim it.
                                    continue

                                self.seen_commits.add(commit_sha)
                                
                                commit_date = datetime.fromisoformat(commit_info['date'].replace('Z', '+00:00'))
                                day_str = commit_date.strftime('%Y-%m-%d')
                                
                                # Double-check date range (search seems to have issues)
                                if start_date <= commit_date <= end_date:
                                    commit_days.add(day_str)
                                    # Use short hash for display
                                    short_sha = commit_sha[:7]
                                    message = commit_info.get('message', '').split('\n')[0][:60]
                                    repo_name = commit_info.get('repo', 'unknown')
                                    self.add_activity(day_str, "Commit", f"{short_sha}: {message}", repo_name)
                                    
                            except (json.JSONDecodeError, KeyError):
                                continue
            
            activity_days.update(commit_days)
            if self.verbose:
                print(f"Found {len(commit_days)} days with commits via search")
            
        except Exception as e:
            self._record_failure(f"Commit search failed", str(e))
        
        # Search for pull requests
        try:
            search_query = f"org:{self.org} author:{self.username} created:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')} type:pr"
            encoded_query = quote_plus(search_query)
            
            cmd = [
                'gh', 'api', f'search/issues?q={encoded_query}',
                '--paginate',
                '--jq', '.items[] | {created_at: .created_at, title: .title, number: .number, repo: .repository_url}'
            ]
            
            result = self._run_gh_command(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                pr_days = set()
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            pr_info = json.loads(line)
                            pr_number = pr_info.get('number')
                            repo_url = pr_info.get('repo', '')
                            repo_name = repo_url.split('/')[-1] if repo_url else 'unknown'
                            pr_key = (repo_name, pr_number)
                            
                            # Skip if we've already seen this PR globally
                            if pr_key in self.seen_prs:
                                continue
                            self.seen_prs.add(pr_key)
                            
                            pr_date = datetime.fromisoformat(pr_info['created_at'].replace('Z', '+00:00'))
                            # Double-check date range
                            if start_date <= pr_date <= end_date:
                                day_str = pr_date.strftime('%Y-%m-%d')
                                pr_days.add(day_str)
                                pr_title = pr_info.get('title', 'Untitled')[:60]
                                self.add_activity(day_str, "PR Created", f"#{pr_number}: {pr_title}", repo_name)
                        except (json.JSONDecodeError, KeyError):
                            continue
                
                activity_days.update(pr_days)
                if self.verbose:
                    print(f"Found {len(pr_days)} additional days with PRs via search")
            
        except Exception as e:
            self._record_failure(f"PR search failed", str(e))

        # Search for issues created
        try:
            search_query = f"org:{self.org} author:{self.username} created:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')} type:issue"
            encoded_query = quote_plus(search_query)

            cmd = [
                'gh', 'api', f'search/issues?q={encoded_query}',
                '--paginate',
                '--jq', '.items[] | {created_at: .created_at, title: .title, number: .number, repo: .repository_url}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                issue_days = set()
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            issue_info = json.loads(line)
                            issue_number = issue_info.get('number')
                            repo_url = issue_info.get('repo', '')
                            repo_name = repo_url.split('/')[-1] if repo_url else 'unknown'

                            issue_date = datetime.fromisoformat(issue_info['created_at'].replace('Z', '+00:00'))
                            # Double-check date range
                            if start_date <= issue_date <= end_date:
                                day_str = issue_date.strftime('%Y-%m-%d')
                                issue_days.add(day_str)
                                issue_title = issue_info.get('title', 'Untitled')[:60]
                                self.add_activity(day_str, "Issue Created", f"#{issue_number}: {issue_title}", repo_name)
                        except (json.JSONDecodeError, KeyError):
                            continue

                activity_days.update(issue_days)
                if self.verbose:
                    print(f"Found {len(issue_days)} additional days with issues via search")

        except Exception as e:
            self._record_failure(f"Issue search failed", str(e))

        # Search for issues/PRs where user commented (involves:USERNAME search)
        try:
            search_query = f"org:{self.org} involves:{self.username} updated:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
            encoded_query = quote_plus(search_query)

            cmd = [
                'gh', 'api', f'search/issues?q={encoded_query}',
                '--paginate',
                '--jq', '.items[] | {number: .number, repo: .repository_url, updated_at: .updated_at, is_pr: .pull_request}'
            ]

            result = self._run_gh_command(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                # For each issue/PR found, check for comments by the user
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            item_info = json.loads(line)
                            item_number = item_info.get('number')
                            repo_url = item_info.get('repo', '')
                            repo_name = repo_url.split('/')[-1] if repo_url else 'unknown'
                            is_pr = item_info.get('is_pr') is not None

                            # Fetch comments for this issue/PR. The scans are
                            # per repository, so many search hits collapse onto
                            # one scan each.
                            if is_pr:
                                # Check PR review comments
                                pr_comment_days = self._scan_repo_comments('pr', repo_name, start_date, end_date)
                                activity_days.update(pr_comment_days)
                            else:
                                # Check issue comments
                                issue_comment_days = self._scan_repo_comments('issue', repo_name, start_date, end_date)
                                activity_days.update(issue_comment_days)

                        except (json.JSONDecodeError, KeyError):
                            continue

                if self.verbose:
                    print(f"Checked comments for issues/PRs where user was involved")

        except Exception as e:
            self._record_failure(f"Comment search failed", str(e))

        return activity_days


def main():
    # Calculate default year/month as last month
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    default_year = last_month.year
    default_month = last_month.month

    parser = argparse.ArgumentParser(description='Extract GitHub activity days using GitHub CLI')
    parser.add_argument('--org', required=True, help='GitHub organization name')
    parser.add_argument('--user', required=True, help='GitHub username')
    parser.add_argument('--year', type=int, default=default_year, help=f'Year (default: {default_year}, last month)')
    parser.add_argument('--month', type=int, default=default_month, help=f'Month 1-12 (default: {default_month}, last month)')
    parser.add_argument('--method', choices=['repos', 'search', 'both'], default='both',
                        help='Method: repos (check each repo), search (use GitHub search), or both')
    parser.add_argument('--repo-limit', type=int, default=20,
                        help='Limit number of repositories to check (default: 20, most recent first)')
    parser.add_argument('--include-repos', type=str, default='',
                        help='Comma-separated list of repos to always include (e.g., "docs-repo-name,wiki-repo-name")')
    parser.add_argument('--verbose', action='store_true',
                        help='Show detailed progress messages during execution')
    parser.add_argument('--output', help='Output file to save results (JSON format)')

    args = parser.parse_args()

    tracker = GitHubCLIActivityTracker(args.org, args.user, args.year, args.month, verbose=args.verbose)

    # Parse include_repos parameter
    include_repos = [r.strip() for r in args.include_repos.split(',') if r.strip()] if args.include_repos else []

    all_activity_days = set()

    if args.method in ['repos', 'both']:
        if args.verbose:
            print("=== Method 1: Checking individual repositories ===")
        repo_days = tracker.get_user_activity(repo_limit=args.repo_limit, include_repos=include_repos)
        all_activity_days.update(repo_days)
        if args.verbose:
            print(f"Repository method found {len(repo_days)} activity days\n")

    if args.method in ['search', 'both']:
        if args.verbose:
            print("=== Method 2: Using GitHub search ===")
        search_days = tracker.get_user_search_activity()
        all_activity_days.update(search_days)
        if args.verbose:
            print(f"Search method found {len(search_days)} activity days\n")
    
    # Sort and display results
    sorted_days = sorted(list(all_activity_days))
    
    print(f"=== Final Activity Summary ===")
    print(f"User: {args.user}")
    print(f"Organization: {args.org}")
    print(f"Period: {args.year}-{args.month:02d}")
    print(f"Total active days: {len(sorted_days)}")
    print(f"GitHub API requests made: {tracker.api_call_count}")
    print(f"gh commands run: {tracker.gh_command_count}")

    if tracker.failures:
        print(f"\nINCOMPLETE: {len(tracker.failures)} call(s) failed, so activity "
              f"may be missing from this report:")
        for failure in tracker.failures:
            print(f"  - {failure}")
        print("Re-run to see whether the failures were transient.")
    
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
            'total_active_days': len(sorted_days),
            'active_days': sorted_days,
            'daily_activity_details': dict(tracker.daily_activity),
            'method_used': args.method,
            'complete': not tracker.failures,
            'failures': tracker.failures,
            'generated_at': datetime.now().isoformat()
        }
        
        with open(args.output, 'w') as f:
            json.dump(result, f, indent=2)
        
        print(f"\nResults saved to: {args.output}")

    # Exit non-zero when the report is known to be incomplete, so a scripted
    # run does not treat a short report as a good one.
    if tracker.failures:
        sys.exit(1)


if __name__ == '__main__':
    main()