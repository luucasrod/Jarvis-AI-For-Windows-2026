from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.tools.cleanup_worktrees import (
    DEFAULT_PROTECTED_BRANCHES,
    build_cleanup_plan,
    parse_merged_branches,
    parse_worktree_porcelain,
    run_git,
)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def test_parse_branch_output_strips_git_worktree_markers() -> None:
    output = """
    + claude/issue-12-fix
    * integration/orchestration
      codex/issue-10-agent-context
    """

    assert parse_merged_branches(output) == {
        "claude/issue-12-fix",
        "integration/orchestration",
        "codex/issue-10-agent-context",
    }


def test_cleanup_plan_uses_temp_git_repo_and_preserves_unmerged_work(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "tests@example.com")
    git(repo, "config", "user.name", "Tests")

    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "integration/orchestration")

    merged_worktree = tmp_path / "merged-worktree"
    git(repo, "worktree", "add", "-b", "codex/issue-merged", str(merged_worktree))
    (merged_worktree / "merged.txt").write_text("merged\n", encoding="utf-8")
    git(merged_worktree, "add", "merged.txt")
    git(merged_worktree, "commit", "-m", "merged branch")
    git(repo, "merge", "--no-ff", "codex/issue-merged", "-m", "merge codex issue")

    unmerged_worktree = tmp_path / "unmerged-worktree"
    git(repo, "worktree", "add", "-b", "claude/issue-unmerged", str(unmerged_worktree))
    (unmerged_worktree / "unmerged.txt").write_text("unmerged\n", encoding="utf-8")
    git(unmerged_worktree, "add", "unmerged.txt")
    git(unmerged_worktree, "commit", "-m", "unmerged branch")

    wave_worktree = tmp_path / "wave-worktree"
    git(
        repo,
        "worktree",
        "add",
        "-b",
        "integration/wave-0",
        str(wave_worktree),
        "integration/orchestration",
    )

    harness_worktree = tmp_path / "harness-worktree"
    git(
        repo,
        "worktree",
        "add",
        "-b",
        "harness/g7-cleanup-worktrees",
        str(harness_worktree),
        "integration/orchestration",
    )

    worktrees = parse_worktree_porcelain(
        run_git(repo, ["worktree", "list", "--porcelain"])
    )
    merged_branches = parse_merged_branches(
        run_git(repo, ["branch", "--merged", "integration/orchestration"])
    )
    plan = build_cleanup_plan(
        worktrees,
        merged_branches,
        main_repo=repo,
        protected_paths=[harness_worktree],
        protected_branches=DEFAULT_PROTECTED_BRANCHES,
    )

    removable_branches = {candidate.worktree.branch for candidate in plan.removable}
    skipped_branches = {candidate.worktree.branch for candidate in plan.skipped}

    assert removable_branches == {"codex/issue-merged"}
    assert "claude/issue-unmerged" in skipped_branches
    assert "integration/wave-0" in skipped_branches
    assert "harness/g7-cleanup-worktrees" in skipped_branches
    assert "integration/orchestration" in skipped_branches
