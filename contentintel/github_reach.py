"""Agent-Reach's GitHub channel, run through GitHub's public REST API: repository search, and reading a repository (with its README), an issue or a pull request.

No login and no browser, so it runs on hosted runners. Without a token GitHub allows 60 requests an hour per address; GITHUB_TOKEN, when it is set, is sent only to api.github.com and raises that.
A rate limit is reported as RATE_LIMITED and never retried. `fetch(url, headers, timeout)` can be replaced in tests; the real one makes one request.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import contract as K

API = "https://api.github.com"
README_MAX_CHARS = 20000


def _fetch(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:                    # noqa: S310 - the host is fixed to api.github.com by the callers below
        return response.read()


def parse_target(target):
    """(owner, repo, number or None) from owner/repo, a github.com link, or a link to an issue or pull request."""
    text = str(target).strip()
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)(?:/(?:issues|pull)/(\d+))?", text) or re.fullmatch(r"([\w.-]+)/([\w.-]+?)(?:#(\d+))?", text)
    if not match:
        raise K.Failure(K.BAD_INPUT, "GitHub target must be owner/repo or a github.com link to a repository, issue or pull request")
    owner, repo, number = match.group(1), match.group(2).removesuffix(".git"), match.group(3)
    return owner, repo, int(number) if number else None


class GitHubPublic:
    needs_browser = False

    def __init__(self, *, fetch=None, token="", timeout=30, environ=None):
        self.fetch, self.token, self.timeout, self.environ = fetch or _fetch, token or "", timeout, environ

    def available(self):
        return True

    def _get(self, path, accept="application/vnd.github+json"):
        headers = {"Accept": accept, "User-Agent": "contentintel-agent-reach", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            return self.fetch(API + path, headers, self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise K.Failure(K.NOT_FOUND, "GitHub has no such repository, issue or pull request (or it is private)") from None
            if exc.code in (403, 429):
                raise K.Failure(K.RATE_LIMITED, "GitHub's rate limit is reached" + ("" if self.token else " (60 an hour without a token; set GITHUB_TOKEN to raise it)")) from None
            raise K.Failure(K.UPSTREAM_BROKEN, f"GitHub answered HTTP {exc.code}") from None
        except TimeoutError:
            raise K.Failure(K.TIMEOUT, f"GitHub did not answer within {self.timeout} seconds") from None
        except (urllib.error.URLError, OSError) as exc:
            raise K.Failure(K.UPSTREAM_BROKEN, f"GitHub could not be reached ({exc.__class__.__name__})") from None

    @staticmethod
    def _repo_row(info, readme=None):
        return {"title": info.get("full_name") or info.get("name") or "", "url": info.get("html_url") or "", "description": info.get("description") or "",
                "content": (readme or "")[:README_MAX_CHARS], "author": (info.get("owner") or {}).get("login", ""), "likes": info.get("stargazers_count"),
                "language": info.get("language") or "", "forks": info.get("forks_count"), "published": info.get("pushed_at") or info.get("updated_at") or "",
                "topics": ", ".join(info.get("topics") or [])}

    def call(self, args):
        args = list(args)
        action, target = args[0], args[1] if len(args) > 1 else ""
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 10
        if action == "search":
            data = json.loads(self._get("/search/repositories?" + urllib.parse.urlencode({"q": target, "per_page": max(1, min(limit, 30))})) or b"{}")
            return [self._repo_row(r) for r in data.get("items", [])[:limit]]
        if action != "read":
            raise K.Failure(K.BAD_INPUT, f"GitHub has no action {action!r} here (search, read)")
        owner, repo, number = parse_target(target)
        if number is None:
            info = json.loads(self._get(f"/repos/{owner}/{repo}"))
            try:
                readme = self._get(f"/repos/{owner}/{repo}/readme", "application/vnd.github.raw").decode("utf-8", errors="replace")
            except K.Failure as exc:
                if exc.code != K.NOT_FOUND:
                    raise
                readme = ""
            return [self._repo_row(info, readme)]
        issue = json.loads(self._get(f"/repos/{owner}/{repo}/issues/{number}"))
        comments = json.loads(self._get(f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=50"))
        text = (issue.get("body") or "") + "".join(f"\n@{(c.get('user') or {}).get('login', '')}: {c.get('body') or ''}" for c in comments)
        return [{"title": issue.get("title") or "", "url": issue.get("html_url") or "", "description": "", "content": text[:README_MAX_CHARS],
                 "author": (issue.get("user") or {}).get("login", ""), "published": issue.get("created_at") or "", "comments": issue.get("comments")}]
