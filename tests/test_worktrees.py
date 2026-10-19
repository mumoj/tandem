"""Implementer worktrees against a real git repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from tandem import worktrees
from tandem.worktrees import GitError, git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.test")
    git(root, "config", "user.name", "dev")
    (root / "greetings.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_a_worktree_lives_outside_the_repository_and_retrying_reuses_it(
    repo: Path, state_root: Path
) -> None:
    first = worktrees.create_worktree(repo, state_root, "farewell", None)
    again = worktrees.create_worktree(repo, state_root, "farewell", None)

    assert again == first
    assert first.branch == "tandem/farewell"
    assert first.path.is_relative_to(state_root)
    assert (first.path / "greetings.py").is_file()
    assert git(repo, "status", "--porcelain") == ""
    assert worktrees.list_worktrees(repo) == [first]
    assert worktrees.repo_root(first.path) == repo


@pytest.mark.parametrize("name", ["", "Has-Caps", "-dash", "a/b", "x" * 41, "../up"])
def test_names_are_safe_branch_and_directory_names(repo: Path, state_root: Path, name: str) -> None:
    with pytest.raises(GitError, match="bad implementer name"):
        worktrees.create_worktree(repo, state_root, name, None)


def test_a_leftover_branch_is_not_silently_reused(repo: Path, state_root: Path) -> None:
    git(repo, "branch", "tandem/old")
    with pytest.raises(GitError, match="exists without a worktree"):
        worktrees.create_worktree(repo, state_root, "old", None)


def test_review_shows_new_and_changed_files_against_the_fork_point(
    repo: Path, state_root: Path
) -> None:
    tree = worktrees.create_worktree(repo, state_root, "farewell", None)
    (tree.path / "greetings.py").write_text("def greet(name):\n    return name\n")
    (tree.path / "farewell.py").write_text("def farewell(name):\n    return name\n")

    out = worktrees.review(repo, tree, max_chars=10_000)

    assert "greetings.py" in str(out["stat"])
    assert "farewell.py" in str(out["stat"])
    assert "+def farewell(name):" in str(out["diff"])
    assert out["repo_head_moved"] is False
    assert out["truncated"] is False


def test_merge_brings_uncommitted_work_into_the_current_branch(
    repo: Path, state_root: Path
) -> None:
    tree = worktrees.create_worktree(repo, state_root, "farewell", None)
    (tree.path / "farewell.py").write_text("def farewell(name):\n    return name\n")

    assert worktrees.commit_all(tree, "Add farewell")
    assert worktrees.merge(repo, tree, "Merge tandem/farewell") == "main"
    worktrees.remove(repo, tree)

    assert (repo / "farewell.py").is_file()
    assert worktrees.list_worktrees(repo) == []
    assert not tree.path.exists()
    assert git(repo, "branch", "--list", "tandem/farewell") == ""


def test_nothing_to_commit_is_not_an_error(repo: Path, state_root: Path) -> None:
    tree = worktrees.create_worktree(repo, state_root, "idle", None)
    assert worktrees.commit_all(tree, "nothing") is False


def test_a_conflicting_merge_is_aborted_and_leaves_the_repository_clean(
    repo: Path, state_root: Path
) -> None:
    tree = worktrees.create_worktree(repo, state_root, "clash", None)
    (tree.path / "greetings.py").write_text("one\n")
    worktrees.commit_all(tree, "theirs")
    (repo / "greetings.py").write_text("two\n")
    git(repo, "commit", "-qam", "ours")

    with pytest.raises(GitError, match="aborted"):
        worktrees.merge(repo, tree, "Merge tandem/clash")

    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "greetings.py").read_text() == "two\n"
    assert worktrees.list_worktrees(repo) == [tree]


def test_discarding_removes_the_work(repo: Path, state_root: Path) -> None:
    tree = worktrees.create_worktree(repo, state_root, "scrap", None)
    (tree.path / "junk.py").write_text("x = 1\n")
    worktrees.remove(repo, tree)
    assert worktrees.list_worktrees(repo) == []
    assert not (repo / "junk.py").exists()


def test_outside_a_repository_says_so(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="not inside a git repository"):
        worktrees.repo_root(tmp_path)


def test_files_left_by_running_checks_are_never_landed(repo: Path, state_root: Path) -> None:
    tree = worktrees.create_worktree(repo, state_root, "checked", None)
    (tree.path / "farewell.py").write_text("def farewell(name):\n    return name\n")
    for junk in ("__pycache__/farewell.cpython-312.pyc", "pkg/__pycache__/x.pyc", "stray.pyc"):
        (tree.path / junk).parent.mkdir(parents=True, exist_ok=True)
        (tree.path / junk).write_bytes(b"\x00")
    (tree.path / ".pytest_cache").mkdir()
    (tree.path / ".pytest_cache" / "README.md").write_text("cache\n")

    out = worktrees.review(repo, tree, max_chars=10_000)
    assert "farewell.py" in str(out["stat"])
    assert "pyc" not in str(out["stat"])
    assert ".pytest_cache" not in str(out["stat"])

    worktrees.commit_all(tree, "Add farewell")
    landed = git(tree.path, "show", "--name-only", "--format=", "HEAD").split()
    assert landed == ["farewell.py"]
