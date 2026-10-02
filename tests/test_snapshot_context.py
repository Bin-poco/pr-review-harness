"""Risk-focused tests for immutable versions, diff locations, and bounded context."""

import subprocess
from pathlib import Path

import pytest

from pr_review_harness.context import build_context
from pr_review_harness.snapshot import Snapshot


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "source repo"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "Harness tests")
    git(root, "config", "user.email", "harness@example.invalid")
    return root


def test_snapshot_uses_commits_and_only_changed_head_lines(repo: Path) -> None:
    path = repo / "numbers.py"
    path.write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    base = commit(repo, "base")
    path.write_text("a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
    head = commit(repo, "head")
    path.write_text("uncommitted and unrelated\n")
    (repo / "untracked.py").write_text("local = True\n")

    snapshot = Snapshot.load(repo, base, head)

    assert snapshot.read_file("numbers.py", "base").splitlines()[1] == "b = 2"
    assert snapshot.read_file("numbers.py").splitlines()[1] == "b = 20"
    assert "untracked.py" not in snapshot.head_files
    assert snapshot.changed_files[0].added_ranges == ((2, 2), (5, 5))
    assert snapshot.validate_location("numbers.py", 2)
    assert snapshot.validate_location("numbers.py", 5)
    assert not snapshot.validate_location("numbers.py", 1)  # Same hunk, unchanged line.
    assert not snapshot.validate_location("numbers.py", 6)
    assert not snapshot.validate_location("numbers.py", True)
    assert path.read_text() == "uncommitted and unrelated\n"


def test_comparison_and_base_reads_use_merge_base(repo: Path) -> None:
    (repo / "common.py").write_text("value = 1\n")
    common = commit(repo, "common")
    git(repo, "checkout", "-qb", "feature")
    (repo / "common.py").write_text("value = 2\n")
    head = commit(repo, "feature")
    git(repo, "checkout", "-q", "main")
    (repo / "base_only.py").write_text("base_only = True\n")
    base = commit(repo, "base advanced")

    snapshot = Snapshot(repo, base, head)

    assert snapshot.base_sha == base
    assert snapshot.merge_base_sha == common
    assert tuple(change.path for change in snapshot.changed_files) == ("common.py",)
    with pytest.raises(FileNotFoundError):
        snapshot.read_file("base_only.py", "base")


def test_spaces_renames_deletions_binary_and_symlinks(repo: Path, tmp_path: Path) -> None:
    (repo / "old name.py").write_text("one = 1\ntwo = 2\nthree = 3\n")
    (repo / "deleted.py").write_text("deleted = True\n")
    base = commit(repo, "base")
    git(repo, "mv", "old name.py", "new name.py")
    (repo / "deleted.py").unlink()
    (repo / "blob.bin").write_bytes(b"\x00\xffbinary")
    (repo / "escape_link.py").symlink_to("/etc/passwd")
    head = commit(repo, "head")

    snapshot = Snapshot(repo, base, head)
    statuses = {change.path: change.status for change in snapshot.changed_files}

    assert statuses["new name.py"].startswith("R")
    assert statuses["deleted.py"] == "D"
    assert snapshot.read_file("new name.py").startswith("one = 1")
    assert not snapshot.validate_location("new name.py", 1)  # Pure rename adds no lines.
    assert not snapshot.validate_location("deleted.py", 1)
    with pytest.raises(ValueError, match="Binary"):
        snapshot.read_file("blob.bin")
    with pytest.raises(FileNotFoundError):
        snapshot.read_file("escape_link.py")
    exported = snapshot.export("head", tmp_path / "export")
    assert (exported / "blob.bin").read_bytes() == b"\x00\xffbinary"
    assert (exported / "new name.py").is_file()
    assert not (exported / "escape_link.py").exists()
    assert not (exported / ".git").exists()
    base_export = snapshot.export("base", tmp_path / "base export")
    assert (base_export / "old name.py").is_file()
    assert (base_export / "deleted.py").is_file()
    assert not (base_export / "new name.py").exists()


def test_git_pathspec_metacharacters_are_literal(repo: Path) -> None:
    (repo / "[ab].py").write_text("brackets = 1\n")
    (repo / "a.py").write_text("plain = 1\n")
    base = commit(repo, "base")
    (repo / "[ab].py").write_text("brackets = 2\n")
    (repo / "a.py").write_text("plain = 2\n")
    head = commit(repo, "head")

    snapshot = Snapshot(repo, base, head)
    bracket_change = next(change for change in snapshot.changed_files if change.path == "[ab].py")

    assert "+brackets = 2" in bracket_change.patch
    assert "+plain = 2" not in bracket_change.patch
    assert snapshot.read_file("[ab].py") == "brackets = 2\n"


@pytest.mark.parametrize(
    "path", ["../outside", "/etc/passwd", "a/../../outside", ".git/config", "a\\b"]
)
def test_read_rejects_path_escape(repo: Path, path: str) -> None:
    (repo / "safe.py").write_text("safe = True\n")
    sha = commit(repo, "base")
    snapshot = Snapshot(repo, sha, sha)
    with pytest.raises(ValueError):
        snapshot.read_file(path)
    assert not snapshot.validate_location(path, 1)


@pytest.mark.parametrize("ref", ["--help", "--output=/tmp/unsafe", "HEAD\x00bad", "HEAD\nbad"])
def test_references_cannot_be_options(repo: Path, ref: str) -> None:
    (repo / "safe.py").write_text("safe = True\n")
    sha = commit(repo, "base")
    with pytest.raises(ValueError):
        Snapshot(repo, ref, sha)


def test_exports_reject_overwrites_and_original_worktree(repo: Path, tmp_path: Path) -> None:
    (repo / "safe.py").write_text("safe = True\n")
    sha = commit(repo, "base")
    snapshot = Snapshot(repo, sha, sha)
    with pytest.raises(ValueError, match="outside"):
        snapshot.export("head", repo / "export")
    populated = tmp_path / "existing"
    populated.mkdir()
    (populated / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        snapshot.export("head", populated)
    assert (populated / "keep.txt").read_text() == "keep"
    with pytest.raises(ValueError, match="version"):
        snapshot.export("HEAD", tmp_path / "invalid")


def test_read_and_export_limits_are_checked_before_copying(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / "large.py").write_text("x" * 100)
    sha = commit(repo, "base")
    snapshot = Snapshot(repo, sha, sha)
    monkeypatch.setattr(Snapshot, "MAX_FILE_BYTES", 40)
    with pytest.raises(ValueError, match="read limit"):
        snapshot.read_file("large.py")
    monkeypatch.setattr(Snapshot, "MAX_EXPORT_BYTES", 40)
    destination = tmp_path / "limited"
    with pytest.raises(ValueError, match="export byte limit"):
        snapshot.export("head", destination)
    assert not destination.exists()


def test_repo_identity_is_shared_by_worktrees(repo: Path, tmp_path: Path) -> None:
    (repo / "safe.py").write_text("safe = True\n")
    sha = commit(repo, "base")
    worktree = tmp_path / "linked worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), sha)
    assert Snapshot(repo, sha, sha).repo_id == Snapshot(worktree, sha, sha).repo_id


def test_context_includes_head_neighbors_tests_callers_and_config(repo: Path) -> None:
    (repo / "calculator.py").write_text("def divide(a, b):\n    return a / b\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_calculator.py").write_text(
        "from calculator import divide\n\ndef test_divide():\n    assert divide(4, 2) == 2\n"
    )
    (repo / "consumer.py").write_text("from calculator import divide\nvalue = divide(1, 2)\n")
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    base = commit(repo, "base")
    (repo / "calculator.py").write_text("def divide(a, b):\n    return a // b\n")
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))
    entries = {(item.path, item.reason) for item in pack.items}

    assert ("calculator.py", "PR diff against merge base") in entries
    assert ("calculator.py", "HEAD lines near changed ranges") in entries
    assert any(
        path == "tests/test_calculator.py" and "related test" in reason for path, reason in entries
    )
    assert any(path == "consumer.py" and "potential caller" in reason for path, reason in entries)
    assert ("pyproject.toml", "project configuration") in entries
    assert "2:     return a // b" in pack.text
    assert len(pack.text) <= pack.max_chars


def test_context_prioritizes_changed_python_over_changelog(repo: Path) -> None:
    (repo / "CHANGES.md").write_text("old note\n")
    (repo / "zlogic.py").write_text("def process():\n    return 1\n")
    base = commit(repo, "base")
    (repo / "CHANGES.md").write_text("new note\n" * 500)
    (repo / "zlogic.py").write_text("def process():\n    return 2\n")
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head), max_chars=1500)
    assert pack.items[0].path == "zlogic.py"
    assert "return 2" in pack.text


@pytest.mark.parametrize("budget", [1, 30, 100, 500, 1000, 24000])
def test_context_strict_character_budget_and_visible_omissions(repo: Path, budget: int) -> None:
    (repo / "module.py").write_text("value = 1\n" + "# base\n" * 500)
    base = commit(repo, "base")
    (repo / "module.py").write_text("value = 2\n" + "# head\n" * 500)
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head), max_chars=budget)

    assert len(pack.text) <= budget
    assert pack.max_chars == budget
    assert pack.omitted
    if budget >= 30:
        assert "OMITTED" in pack.text


def test_related_scan_does_not_read_oversized_files(repo: Path) -> None:
    (repo / "module.py").write_text("value = 1\n")
    (repo / "large_caller.py").write_text("import module\n" + "# padding\n" * 7000)
    base = commit(repo, "base")
    (repo / "module.py").write_text("value = 2\n")
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))

    assert not any(item.path == "large_caller.py" for item in pack.items)
    assert any(
        "large_caller.py" in message and "scan byte limit" in message for message in pack.omitted
    )


def test_context_selects_changed_symbol_calls_beyond_first_160_lines(repo: Path) -> None:
    (repo / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests/test_calculator.py").write_text(
        "import calculator as calc\n"
        + "# filler\n" * 180
        + "\ndef test_divide():\n    assert calc.divide(3, 2) == 1.5\n"
    )
    (repo / "consumer.py").write_text(
        "from calculator import divide as quotient\nvalue = quotient(3, 2)\n"
    )
    (repo / "other.py").write_text("from calculator import multiply\nvalue = multiply(3, 2)\n")
    base = commit(repo, "base")
    (repo / "calculator.py").write_text(
        "def divide(a, b):\n    return a // b\n\ndef multiply(a, b):\n    return a * b\n"
    )
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))
    selected = {
        item.path: item
        for item in pack.items
        if "related test" in item.reason or "caller" in item.reason
    }

    assert "changed symbol call" in selected["tests/test_calculator.py"].reason
    assert (
        "184:     assert calc.divide(3, 2) == 1.5" in selected["tests/test_calculator.py"].content
    )
    assert "1: import calculator as calc" in selected["tests/test_calculator.py"].content
    assert "changed symbol call" in selected["consumer.py"].reason
    assert "import heuristic" in selected["other.py"].reason
    assert len(pack.text) <= pack.max_chars

    baseline = build_context(
        Snapshot(repo, base, head), max_chars=pack.max_chars, strategy="imports"
    )
    baseline_test = next(item for item in baseline.items if item.path == "tests/test_calculator.py")
    assert baseline_test.reason == "related test (name/import heuristic)"
    assert "184:     assert calc.divide(3, 2) == 1.5" not in baseline_test.content
    assert len(baseline.text) <= baseline.max_chars


def test_symbol_call_priority_survives_related_section_cap(repo: Path) -> None:
    (repo / "module.py").write_text("def target():\n    return 1\n")
    (repo / "tests").mkdir()
    for number in range(12):
        (repo / "tests" / f"test_module_{number:02}.py").write_text(
            "import module\n\ndef test_import():\n    assert module is not None\n"
        )
    (repo / "z_consumer.py").write_text("from module import target\nvalue = target()\n")
    base = commit(repo, "base")
    (repo / "module.py").write_text("def target():\n    return 2\n")
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))
    selected = [
        item for item in pack.items if "related test" in item.reason or "caller" in item.reason
    ]

    assert len(selected) == 12
    assert selected[0].path == "z_consumer.py"
    assert "changed symbol call" in selected[0].reason
    assert any("related-file sections" in message for message in pack.omitted)


def test_context_resolves_relative_import_of_changed_symbol(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").write_text("")
    (repo / "pkg/calculator.py").write_text("def divide(a, b):\n    return a / b\n")
    (repo / "pkg/consumer.py").write_text(
        "from .calculator import divide as quotient\nresult = quotient(3, 2)\n"
    )
    base = commit(repo, "base")
    (repo / "pkg/calculator.py").write_text("def divide(a, b):\n    return a // b\n")
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))
    consumer = next(item for item in pack.items if item.path == "pkg/consumer.py")

    assert consumer.reason == "potential caller (changed symbol call)"
    assert "2: result = quotient(3, 2)" in consumer.content


def test_method_edit_links_to_callers_of_its_class(repo: Path) -> None:
    (repo / "service.py").write_text(
        "class Engine:\n    def compute(self, value):\n        return value + 1\n"
    )
    (repo / "consumer.py").write_text("from service import Engine\nresult = Engine().compute(2)\n")
    base = commit(repo, "base")
    (repo / "service.py").write_text(
        "class Engine:\n    def compute(self, value):\n        return value + 2\n"
    )
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))
    consumer = next(item for item in pack.items if item.path == "consumer.py")

    assert consumer.reason == "potential caller (changed symbol call)"
    assert "2: result = Engine().compute(2)" in consumer.content


def test_package_init_change_links_to_imported_function(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").write_text("def launch():\n    return 1\n")
    (repo / "consumer.py").write_text("from pkg import launch as start\nresult = start()\n")
    base = commit(repo, "base")
    (repo / "pkg/__init__.py").write_text("def launch():\n    return 2\n")
    head = commit(repo, "head")

    pack = build_context(Snapshot(repo, base, head))
    consumer = next(item for item in pack.items if item.path == "consumer.py")

    assert consumer.reason == "potential caller (changed symbol call)"
