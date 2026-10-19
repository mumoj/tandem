"""One git worktree per implementer, so several can work on one repository at once.

Each implementer gets a branch ``tandem/<name>`` checked out in its own directory
under the state root, outside the repository, so the planner's working tree and
``git status`` stay clean. Git's own worktree list is the registry: nothing here
keeps state that could disagree with it.

The implementer edits files but does not commit. Landing commits whatever it
left, then merges that branch into the repository's current branch, or throws
it away.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "tandem/"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# What an implementer leaves behind by running its own checks. Never its work,
# so never committed, whether or not the repository ignores it.
GENERATED = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "*.pyc")
_LANDED = (
    ".",
    *(f":(exclude,glob)**/{pattern}" for pattern in GENERATED),
    *(f":(exclude,glob)**/{pattern}/**" for pattern in GENERATED if "*" not in pattern),
)
"""A pathspec for the implementer's work: everything but ``GENERATED``."""


class GitError(Exception):
    """A git command failed; the message carries git's own explanation."""


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=120
    )
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise GitError(f"git {args[0]}: {' '.join(detail[-3:]) or f'exit {done.returncode}'}")
    return done.stdout


def repo_root(path: Path) -> Path:
    """The main working tree of the repository ``path`` belongs to."""
    try:
        common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    except GitError as exc:
        raise GitError(f"{path} is not inside a git repository") from exc
    root = Path(common).parent
    if not (root / ".git").exists():
        raise GitError(f"{path} belongs to a bare repository; implementers need a working tree")
    return root


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise GitError(
            f"bad implementer name {name!r}: lower-case letters, digits and dashes, "
            "starting with a letter or digit, at most 40 characters"
        )
    return name


@dataclass(frozen=True, slots=True)
class Worktree:
    name: str
    path: Path
    branch: str


def worktree_home(state_root: Path, repo: Path) -> Path:
    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:6]
    return state_root.expanduser() / "worktrees" / f"{repo.name}-{digest}"


def list_worktrees(repo: Path) -> list[Worktree]:
    """The implementer worktrees of ``repo``, from git's own list."""
    found: list[Worktree] = []
    path: Path | None = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/" + BRANCH_PREFIX) and path is not None:
            branch = line.removeprefix("branch refs/heads/")
            found.append(Worktree(branch.removeprefix(BRANCH_PREFIX), path, branch))
    return found


def find_worktree(repo: Path, name: str) -> Worktree:
    for worktree in list_worktrees(repo):
        if worktree.name == name:
            return worktree
    known = ", ".join(w.name for w in list_worktrees(repo)) or "none"
    raise GitError(f"no implementer named {name!r} in {repo} (running: {known})")


def create_worktree(repo: Path, state_root: Path, name: str, base: str | None) -> Worktree:
    """A new branch and worktree for ``name``, or the existing one: retrying is safe."""
    check_name(name)
    for worktree in list_worktrees(repo):
        if worktree.name == name:
            return worktree
    branch = BRANCH_PREFIX + name
    if git(repo, "branch", "--list", branch).strip():
        raise GitError(
            f"branch {branch} exists without a worktree; land or delete it, or pick another name"
        )
    path = worktree_home(state_root, repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base or "HEAD")
    return Worktree(name, path, branch)


def commit_all(worktree: Worktree, message: str) -> bool:
    """Commit everything the implementer left in its worktree. False if there was nothing."""
    git(worktree.path, "add", "-A", "--", *_LANDED)
    if not git(worktree.path, "diff", "--cached", "--name-only").strip():
        return False
    git(
        worktree.path,
        "-c",
        "user.name=tandem",
        "-c",
        "user.email=tandem@localhost",
        "commit",
        "--no-verify",
        "-m",
        message,
    )
    return True


def review(repo: Path, worktree: Worktree, max_chars: int) -> dict[str, object]:
    """What landing would merge: committed and uncommitted changes against the fork point."""
    head = git(repo, "rev-parse", "HEAD").strip()
    base = git(repo, "merge-base", head, worktree.branch).strip()
    # Untracked files only show in a diff once git knows about them; intent-to-add
    # records that without staging their content.
    git(worktree.path, "add", "--intent-to-add", "--all", "--", *_LANDED)
    stat = git(worktree.path, "diff", "--stat", base, "--", *_LANDED).strip()
    diff = git(worktree.path, "diff", base, "--", *_LANDED)
    truncated = len(diff) > max_chars
    return {
        "branch": worktree.branch,
        "worktree": str(worktree.path),
        "base": base,
        "repo_head_moved": base != head,
        "stat": stat or "(no changes)",
        "diff": diff[:max_chars] + ("\n... (truncated)" if truncated else ""),
        "truncated": truncated,
    }


def merge(repo: Path, worktree: Worktree, message: str) -> str:
    """Merge the implementer's branch into ``repo``'s current branch, or leave no trace."""
    current = git(repo, "branch", "--show-current").strip()
    if not current:
        raise GitError(f"{repo} is on a detached HEAD; check out the branch to merge into")
    done = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "-m", message, worktree.branch],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if done.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        detail = " ".join((done.stdout + done.stderr).strip().splitlines()[-4:])
        raise GitError(f"merge into {current} failed and was aborted: {detail}")
    return current


def remove(repo: Path, worktree: Worktree) -> None:
    """Delete the worktree and its branch. Only after merging, or to discard the work."""
    git(repo, "worktree", "remove", "--force", str(worktree.path))
    git(repo, "branch", "-D", worktree.branch)
