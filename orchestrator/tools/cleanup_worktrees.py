"""Clean up merged Git worktrees without touching active branches."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_MAIN_REPO = Path(
    r"C:\Users\syann\Desktop\Jarvis-AI-For-Windows-2026-integration-orchestration"
)
DEFAULT_BASE_BRANCH = "integration/orchestration"
DEFAULT_PROTECTED_BRANCHES = frozenset(
    {
        "main",
        "integration/orchestration",
        "integration/wave-0",
        "harness/g7-cleanup-worktrees",
    }
)


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str | None = None
    branch: str | None = None


@dataclass(frozen=True)
class CleanupCandidate:
    worktree: Worktree
    reason: str


@dataclass(frozen=True)
class CleanupPlan:
    removable: tuple[CleanupCandidate, ...]
    skipped: tuple[CleanupCandidate, ...]


def run_git(repo: Path, args: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def parse_worktree_porcelain(output: str) -> tuple[Worktree, ...]:
    entries: list[Worktree] = []
    current: dict[str, str] = {}

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(_worktree_from_fields(current))
                current = {}
            continue

        key, _, value = line.partition(" ")
        current[key] = value

    if current:
        entries.append(_worktree_from_fields(current))

    return tuple(entries)


def parse_merged_branches(output: str) -> set[str]:
    branches: set[str] = set()
    for raw_line in output.splitlines():
        branch = raw_line.strip()
        if not branch:
            continue
        branch = branch.lstrip("*+").strip()
        if branch:
            branches.add(branch)
    return branches


def build_cleanup_plan(
    worktrees: Iterable[Worktree],
    merged_branches: set[str],
    *,
    main_repo: Path,
    protected_paths: Iterable[Path] = (),
    protected_branches: Iterable[str] = DEFAULT_PROTECTED_BRANCHES,
) -> CleanupPlan:
    worktree_list = tuple(worktrees)
    protected_path_set = {
        _normalize_path(path) for path in (main_repo, *tuple(protected_paths))
    }
    if worktree_list:
        protected_path_set.add(_normalize_path(worktree_list[0].path))

    protected_branch_set = set(protected_branches)
    removable: list[CleanupCandidate] = []
    skipped: list[CleanupCandidate] = []

    for worktree in worktree_list:
        normalized_path = _normalize_path(worktree.path)
        branch = worktree.branch

        if normalized_path in protected_path_set:
            skipped.append(CleanupCandidate(worktree, "protected worktree path"))
        elif branch is None:
            skipped.append(CleanupCandidate(worktree, "no branch checked out"))
        elif branch in protected_branch_set:
            skipped.append(CleanupCandidate(worktree, "protected branch"))
        elif branch not in merged_branches:
            skipped.append(CleanupCandidate(worktree, "branch is not merged"))
        else:
            removable.append(CleanupCandidate(worktree, "branch is merged"))

    return CleanupPlan(tuple(removable), tuple(skipped))


def load_cleanup_plan(
    *,
    main_repo: Path,
    base_branch: str,
    protected_paths: Iterable[Path],
    protected_branches: Iterable[str],
) -> CleanupPlan:
    worktrees = parse_worktree_porcelain(
        run_git(main_repo, ["worktree", "list", "--porcelain"])
    )
    merged_branches = parse_merged_branches(
        run_git(main_repo, ["branch", "--merged", base_branch])
    )
    return build_cleanup_plan(
        worktrees,
        merged_branches,
        main_repo=main_repo,
        protected_paths=protected_paths,
        protected_branches=protected_branches,
    )


def apply_cleanup(repo: Path, plan: CleanupPlan) -> None:
    for candidate in plan.removable:
        worktree = candidate.worktree
        if worktree.branch is None:
            continue
        run_git(repo, ["worktree", "remove", str(worktree.path)])
        run_git(repo, ["branch", "-d", worktree.branch])


def format_plan(plan: CleanupPlan, *, apply: bool) -> str:
    action = "Removing" if apply else "Would remove"
    lines = [
        f"{action} {len(plan.removable)} merged worktree(s).",
    ]
    for candidate in plan.removable:
        worktree = candidate.worktree
        lines.append(f"REMOVE {worktree.branch}: {worktree.path}")

    lines.append(f"Skipped {len(plan.skipped)} worktree(s).")
    for candidate in plan.skipped:
        worktree = candidate.worktree
        branch = worktree.branch or "<no branch>"
        lines.append(f"SKIP {branch}: {worktree.path} ({candidate.reason})")

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List or remove Git worktrees whose branches are merged."
    )
    parser.add_argument(
        "--main-repo",
        type=Path,
        default=DEFAULT_MAIN_REPO,
        help=f"repository to run git commands from (default: {DEFAULT_MAIN_REPO})",
    )
    parser.add_argument(
        "--base-branch",
        default=DEFAULT_BASE_BRANCH,
        help=f"branch used by git branch --merged (default: {DEFAULT_BASE_BRANCH})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="list worktrees that would be removed; this is the default",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="remove merged worktrees and delete their branches",
    )
    parser.add_argument(
        "--protect-worktree",
        action="append",
        default=[],
        type=Path,
        help="additional worktree path to preserve; can be repeated",
    )
    parser.add_argument(
        "--protect-branch",
        action="append",
        default=[],
        help="additional branch to preserve; can be repeated",
    )
    args = parser.parse_args(argv)

    protected_paths = [Path.cwd(), *args.protect_worktree]
    protected_branches = [*DEFAULT_PROTECTED_BRANCHES, *args.protect_branch]

    plan = load_cleanup_plan(
        main_repo=args.main_repo,
        base_branch=args.base_branch,
        protected_paths=protected_paths,
        protected_branches=protected_branches,
    )
    print(format_plan(plan, apply=args.apply))

    if args.apply:
        apply_cleanup(args.main_repo, plan)

    return 0


def _worktree_from_fields(fields: dict[str, str]) -> Worktree:
    branch = fields.get("branch")
    if branch and branch.startswith("refs/heads/"):
        branch = branch.removeprefix("refs/heads/")
    return Worktree(
        path=Path(fields["worktree"]),
        head=fields.get("HEAD"),
        branch=branch,
    )


def _normalize_path(path: Path) -> str:
    return str(path.expanduser().resolve()).casefold()


if __name__ == "__main__":
    sys.exit(main())
