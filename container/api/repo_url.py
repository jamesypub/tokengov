"""Repo-reference normalizer (#1042).

URL-first input: accept a full URL, the SCP-SSH short form, or the
`owner/name` shorthand and reduce them all to a canonical
`{host, path}` identity. Host is first-class so the data model can grow
beyond github.com later (e.g. self-hosted GitLab with nested subgroups)
without a second migration — the worker still gates real sync on
`host == 'github.com'`.

Grammar (reduced in this order), mirrors `admin-ui/web/src/lib/
repoUrl.js` — keep the two in sync:

  1. SCP-SSH  `git@host:group/proj.git`  (git's own rule: recognized
     only when there is NO slash before the first colon).
  2. Absolute URL  `scheme://host/path`  (scheme in http/https/ssh/git).
  3. Shorthand  `owner/name`  (or `group/sub/proj`) → host github.com.

Then, regardless of form: strip a leading `git@…`/scheme, strip the
GitLab `/-/` route separator, strip a trailing `/`, strip `.git`, strip
GitHub suffix routes (`/tree/`, `/blob/`, …) for github.com. Require
2..20 path segments. Canonical identity = `host + "/" + path`.

SECURITY: the stored host/path are display/identity only (#1042) — the
normalizer must never become an SSRF vector. It rejects control chars,
whitespace and non-http(s)/ssh/git schemes, and caps segment count, but
NOTHING here performs a network call against the parsed host.
"""
from __future__ import annotations

from urllib.parse import urlsplit

GITHUB_HOST = "github.com"
_ALLOWED_SCHEMES = ("https", "http", "ssh", "git")
_MAX_SEGMENTS = 20  # GitLab subgroup nesting limit
# GitHub route keywords that follow owner/name in a browser deep link;
# everything from the first one on is route, not identity. Only applied
# for github.com (GitLab uses the `/-/` separator instead).
_GITHUB_ROUTE_WORDS = frozenset({
    "tree", "blob", "pull", "pulls", "commit", "commits", "releases",
    "tags", "branches", "issues", "wiki", "actions", "settings",
    "compare", "graphs", "network", "pulse", "projects",
})
# A single path segment: word chars, dot, dash. No slashes/spaces.
_SEG_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._-"
)


class RepoParseError(ValueError):
    """Raised for an input that can't be reduced to host + >=2 segments."""


def _clean_host(netloc: str) -> str:
    # Drop any userinfo (user@) and port (:443); keep the bare host.
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    return netloc.strip().lower()


def _split_segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def normalize_repo(raw: str) -> dict:
    """Reduce a repo reference to {host, path, canonical}.

    Raises RepoParseError with a SPECIFIC message on anything that
    isn't a host + at least owner/name.
    """
    if raw is None:
        raise RepoParseError("Enter a repository URL or owner/name")
    s = raw.strip()
    if not s:
        raise RepoParseError("Enter a repository URL or owner/name")
    # No control chars or internal whitespace — the identity is a PK.
    if any(ord(c) < 0x20 or c == "\x7f" for c in s) or any(
        c.isspace() for c in s
    ):
        raise RepoParseError("Repository reference can't contain spaces")

    host = GITHUB_HOST
    path = s

    # 1. SCP-SSH: git@host:group/proj.git — only when no slash precedes
    #    the first colon (git's disambiguation from a scheme-less path).
    colon = s.find(":")
    slash = s.find("/")
    if (
        "@" in s
        and colon != -1
        and (slash == -1 or slash > colon)
        and "://" not in s
    ):
        userhost, _, after = s.partition(":")
        host = _clean_host(userhost)
        path = after
    # 2. Absolute URL scheme://host/path.
    elif "://" in s:
        parts = urlsplit(s)
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            raise RepoParseError(
                f"Unsupported URL scheme '{parts.scheme}' "
                "(use https, ssh or git)"
            )
        if not parts.netloc:
            raise RepoParseError("Could not parse that URL")
        host = _clean_host(parts.netloc)
        path = parts.path
    # 3. Shorthand owner/name — host defaults to github.com (path = s).

    if not host:
        raise RepoParseError("Could not parse that URL")

    # GitLab `/-/` separates the project path from the route — keep the
    # left side (e.g. group/proj/-/issues → group/proj).
    if "/-/" in path:
        path = path.split("/-/", 1)[0]

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    segs = _split_segments(path)

    # GitHub suffix routes (tree/blob/pull/...) follow owner/name; clip
    # at the first route word. GitLab uses `/-/` (handled above), so
    # only do this for github.com to avoid clipping a real subgroup.
    if host == GITHUB_HOST and len(segs) > 2:
        for i, seg in enumerate(segs):
            if i >= 2 and seg.lower() in _GITHUB_ROUTE_WORDS:
                segs = segs[:i]
                break

    if len(segs) < 2:
        raise RepoParseError(
            "Need at least owner/name (two path segments)"
        )
    if len(segs) > _MAX_SEGMENTS:
        raise RepoParseError(
            f"Path is too deep (max {_MAX_SEGMENTS} segments)"
        )
    for seg in segs:
        if not seg or any(c not in _SEG_OK for c in seg):
            raise RepoParseError(
                f"Invalid character in path segment '{seg}'"
            )

    # Host charset guard (identity / PK hygiene; never network-dialled).
    if not host or any(
        c not in _SEG_OK for c in host
    ):
        raise RepoParseError("Invalid host in repository reference")

    norm_path = "/".join(segs)
    return {
        "host": host,
        "path": norm_path,
        "canonical": f"{host}/{norm_path}",
        "is_github": host == GITHUB_HOST,
    }
