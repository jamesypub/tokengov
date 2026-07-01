"""Normalizer tests for api.repo_url.normalize_repo (#1042).

Mirrors admin-ui/web/src/lib/repoUrl.test.js — keep the case tables in
sync when either grammar changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# container/ on the path so `import api.repo_url` works standalone.
_CONTAINER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_CONTAINER))

from api.repo_url import normalize_repo, RepoParseError  # noqa: E402


@pytest.mark.parametrize("raw,host,path", [
    # full github URL + variants
    ("https://github.com/NVIDIA/SkillSpector",
     "github.com", "NVIDIA/SkillSpector"),
    ("https://github.com/NVIDIA/SkillSpector.git",
     "github.com", "NVIDIA/SkillSpector"),
    ("https://github.com/NVIDIA/SkillSpector/",
     "github.com", "NVIDIA/SkillSpector"),
    ("https://github.com/NVIDIA/SkillSpector/tree/main",
     "github.com", "NVIDIA/SkillSpector"),
    ("https://github.com/NVIDIA/SkillSpector/blob/main/README.md",
     "github.com", "NVIDIA/SkillSpector"),
    ("https://github.com/NVIDIA/SkillSpector/pull/42",
     "github.com", "NVIDIA/SkillSpector"),
    # SCP-SSH short form
    ("git@github.com:NVIDIA/SkillSpector.git",
     "github.com", "NVIDIA/SkillSpector"),
    ("ssh://git@github.com/NVIDIA/SkillSpector.git",
     "github.com", "NVIDIA/SkillSpector"),
    # bare shorthand → github.com
    ("NVIDIA/SkillSpector", "github.com", "NVIDIA/SkillSpector"),
    # self-hosted GitLab subgroup (path depth > 2)
    ("https://gitlab.example.com/team/sub/proj",
     "gitlab.example.com", "team/sub/proj"),
    # GitLab `/-/` route separator stripped
    ("https://gitlab.example.com/team/sub/proj/-/issues/3",
     "gitlab.example.com", "team/sub/proj"),
    # host with port is cleaned
    ("https://gitlab.example.com:8443/team/proj",
     "gitlab.example.com", "team/proj"),
])
def test_normalize_ok(raw, host, path):
    n = normalize_repo(raw)
    assert n["host"] == host
    assert n["path"] == path
    assert n["canonical"] == f"{host}/{path}"


def test_canonical_identity_matches_across_forms():
    forms = [
        "https://github.com/NVIDIA/SkillSpector",
        "https://github.com/NVIDIA/SkillSpector.git",
        "git@github.com:NVIDIA/SkillSpector.git",
        "https://github.com/NVIDIA/SkillSpector/tree/main",
    ]
    canon = {normalize_repo(f)["canonical"] for f in forms}
    assert canon == {"github.com/NVIDIA/SkillSpector"}


def test_is_github_flag():
    assert normalize_repo("owner/name")["is_github"] is True
    assert normalize_repo(
        "https://gitlab.example.com/g/p")["is_github"] is False


@pytest.mark.parametrize("bad", [
    "", "   ", "noslash", "not a repo", "trailing/",
    "/leading", "a/b/c d/e",
])
def test_normalize_rejects(bad):
    with pytest.raises(RepoParseError):
        normalize_repo(bad)


def test_rejects_unsupported_scheme():
    with pytest.raises(RepoParseError):
        normalize_repo("ftp://github.com/owner/name")


def test_rejects_too_deep():
    deep = "https://gitlab.example.com/" + "/".join(
        f"g{i}" for i in range(25))
    with pytest.raises(RepoParseError):
        normalize_repo(deep)


def test_error_message_is_specific_not_owner_name():
    # The dead-end "must be owner/name" message is gone; a URL that
    # almost parses should explain what's actually wrong.
    with pytest.raises(RepoParseError) as ei:
        normalize_repo("https://github.com/justone")
    assert "owner/name" in str(ei.value)  # 'Need at least owner/name'
