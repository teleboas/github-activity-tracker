#!/usr/bin/env python3
"""
GitHub Activity Tracker using GitHub CLI (gh)

Extract activity days for a specific GitHub user in an organization for a given month.
Uses the GitHub CLI for simpler and more reliable API access.
"""

import subprocess
import json
import argparse
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
        # API call counter
        self.api_call_count = 0
        # Common variations of the username to search for
        self.username_variations = [
            username.lower(),
            username,
            username.title(),
            f"{username.title()} {username.title()}",  # If username is first name only
        ]

    def get_month_date_range(self) -> tuple:
        """Get start and end dates for the specified month."""
        start_date = datetime(self.year, self.month, 1, tzinfo=timezone.utc)

        if self.month == 12:
            end_date = datetime(self.year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        else:
            end_date = datetime(self.year, self.month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)

        return start_date, end_date

    def _run_gh_command(self, cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
        """Run a gh command and track API calls."""
        self.api_call_count += 1
        return subprocess.run(cmd, **kwargs)
    
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
            if self.verbose:
                print(f"Error fetching repositories: {e}")
            return []
        except json.JSONDecodeError as e:
            if self.verbose:
                print(f"Error parsing repository data: {e}")
            return []

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
            issue_comment_days = self._get_issue_comments_for_repo(repo_name, start_date, end_date)
            activity_days.update(issue_comment_days)

            # Get PR comments
            pr_comment_days = self._get_pr_comments_for_repo(repo_name, start_date, end_date)
            activity_days.update(pr_comment_days)

            # Get commit comments
            commit_comment_days = self._get_commit_comments_for_repo(repo_name, start_date, end_date)
            activity_days.update(commit_comment_days)

            # Get wiki edits
            wiki_days = self._get_wiki_edits_for_repo(repo_name, start_date, end_date)
            activity_days.update(wiki_days)

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
            if self.verbose:
                print(f"  Warning: Error checking wiki edits in {repo_name}: {e}")

        return activity_days

    def _get_commits_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get commit days for the user in a specific repository."""
        activity_days = set()
        
        try:
            # Get recent commits and filter by date and author locally
            cmd = [
                'gh', 'api', 
                f'repos/{self.org}/{repo_name}/commits?per_page=100',
                '--paginate',
                '--jq', '.[] | {date: .commit.author.date, name: .commit.author.name, email: .commit.author.email, sha: .sha, message: .commit.message}'
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

                            # Check if this commit is by our user (any variation)
                            is_user_commit = (
                                author_name in self.username_variations or
                                author_email.split('@')[0].lower() == self.username.lower() or
                                self.username.lower() in author_name.lower()
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
            if self.verbose:
                print(f"  Warning: Error checking commits in {repo_name}: {e}")
        
        return activity_days

    def _get_prs_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get pull request creation and review days for the user in a specific repository."""
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
            if self.verbose:
                print(f"  Warning: Error checking PRs in {repo_name}: {e}")
        
        # Get PR reviews by the user
        try:
            # Get all PRs and check for reviews by our user
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/pulls?state=all',
                '--paginate',
                '--jq', '.[] | {number: .number, updated_at: .updated_at}'
            ]
            
            result = self._run_gh_command(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and line != 'null':
                        try:
                            pr_data = json.loads(line)
                            pr_number = pr_data['number']
                            updated_at = datetime.fromisoformat(pr_data['updated_at'].replace('Z', '+00:00'))
                            
                            # Only check PRs updated in our date range
                            if start_date <= updated_at <= end_date:
                                review_days = self._get_reviews_for_pr(repo_name, pr_number, start_date, end_date)
                                activity_days.update(review_days)
                                
                        except (json.JSONDecodeError, KeyError):
                            continue
            
        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            if self.verbose:
                print(f"  Warning: Error checking PR reviews in {repo_name}: {e}")
        
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
            if self.verbose:
                print(f"  Warning: Error checking issues in {repo_name}: {e}")

        return activity_days

    def _get_issue_comments_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get issue comments by the user in a specific repository."""
        activity_days = set()

        try:
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/issues/comments?per_page=100',
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
            if self.verbose:
                print(f"  Warning: Error checking issue comments in {repo_name}: {e}")

        return activity_days

    def _get_pr_comments_for_repo(self, repo_name: str, start_date: datetime, end_date: datetime) -> Set[str]:
        """Get PR comments (both review comments and issue comments on PRs) by the user."""
        activity_days = set()

        # Get review comments (comments on specific code lines)
        try:
            cmd = [
                'gh', 'api', f'repos/{self.org}/{repo_name}/pulls/comments?per_page=100',
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
            if self.verbose:
                print(f"  Warning: Error checking PR comments in {repo_name}: {e}")

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
            if self.verbose:
                print(f"  Warning: Error checking commit comments in {repo_name}: {e}")

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
                    '--jq', '.items[] | {date: .commit.author.date, sha: .sha, message: .commit.message, repo: .repository.name, author_name: .commit.author.name, author_email: .commit.author.email}'
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
                                self.seen_commits.add(commit_sha)
                                
                                # Verify the author actually matches our user (don't trust search API blindly!)
                                is_user_commit = (
                                    author_name in self.username_variations or
                                    author_email.split('@')[0].lower() == self.username.lower() or
                                    self.username.lower() in author_name.lower()
                                )
                                
                                if not is_user_commit:
                                    continue  # Skip commits that don't actually match our user
                                
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
            if self.verbose:
                print(f"Warning: Commit search failed: {e}")
        
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
            if self.verbose:
                print(f"Warning: PR search failed: {e}")

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
            if self.verbose:
                print(f"Warning: Issue search failed: {e}")

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

                            # Fetch comments for this issue/PR
                            if is_pr:
                                # Check PR review comments
                                pr_comment_days = self._get_pr_comments_for_repo(repo_name, start_date, end_date)
                                activity_days.update(pr_comment_days)
                            else:
                                # Check issue comments
                                issue_comment_days = self._get_issue_comments_for_repo(repo_name, start_date, end_date)
                                activity_days.update(issue_comment_days)

                        except (json.JSONDecodeError, KeyError):
                            continue

                if self.verbose:
                    print(f"Checked comments for issues/PRs where user was involved")

        except Exception as e:
            if self.verbose:
                print(f"Warning: Comment search failed: {e}")

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
    print(f"GitHub API calls made: {tracker.api_call_count}")
    
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
            'generated_at': datetime.now().isoformat()
        }
        
        with open(args.output, 'w') as f:
            json.dump(result, f, indent=2)
        
        print(f"\nResults saved to: {args.output}")


if __name__ == '__main__':
    main()