"""Version, clipping, visibility and replay boundaries of the source excerpt index."""

import hashlib
import json
from threading import Lock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_runtime import make_snapshot
from test_snapshot_context import commit, git

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.checks import CheckRunner
from pr_review_harness.context import _Builder, _numbered, build_context
from pr_review_harness.context_fragments import FragmentIndex
from pr_review_harness.models import ContextItem, ContextPack, ReviewSession
from pr_review_harness.persistence import RunStore
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, _review_tools, review
from pr_review_harness.snapshot import Snapshot, git_lines


def empty_pack():
    return ContextPack("", (), (), 1)


def source_repo(tmp_path, files):
    root = tmp_path / "source"
    root.mkdir(parents=True)
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "Harness tests")
    git(root, "config", "user.email", "harness@example.invalid")
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root, commit(root, "base")


def toolset(snapshot, session, policy=None):
    return {
        tool.name: tool
        for tool in _review_tools(snapshot, session, CheckRunner(snapshot), Lock(), policy)
    }


def test_initial_diff_versions_rename_and_qualified_symbols(tmp_path):
    code = "class Engine:\n    def compute(self, values):\n        return sum(values)\n"
    root, base = source_repo(
        tmp_path,
        {
            "old name.py": code,
            "gone.py": "removed = 1\n",
            "pyproject.toml": '[project]\nname="x"\n',
        },
    )
    git(root, "mv", "old name.py", "new name.py")
    (root / "new name.py").write_text(code.replace("sum(values)", "sum(values) / len(values)"))
    (root / "gone.py").unlink()
    (root / "added.py").write_text("added = 1\n")
    snapshot = Snapshot(root, base, commit(root, "head"))
    pack = build_context(snapshot)
    index = FragmentIndex(snapshot, ReviewSession(), pack)
    records = index.manifest()["records"]
    for record in records:
        lines = snapshot.read_file(record["path"], record["version"]).splitlines()
        start, end = record["line_range"]
        assert record["complete"]
        assert (
            record["text_sha256"]
            == hashlib.sha256("\n".join(lines[start - 1 : end]).encode()).hexdigest()
        )
        assert record["sha"] == (
            snapshot.head_sha if record["version"] == "head" else snapshot.merge_base_sha
        )
    assert index.lookup("old name.py", "base", 3, 3, symbol="Engine.compute")
    assert index.lookup("new name.py", "head", 3, 3, symbol="Engine.compute")
    assert not index.lookup("new name.py", "base", 3, 3)
    assert index.lookup("gone.py", "base", 1, 1)
    assert not index.lookup("gone.py", "head", 1, 1)
    assert index.lookup("added.py", "head", 1, 1)
    assert not index.lookup("added.py", "base", 1, 1)
    assert index.lookup("pyproject.toml", "head", 1, 2)
    for item in pack.items:
        assert (
            pack.text[item.content_start : item.content_start + len(item.content)] == item.content
        )


def test_disjoint_neighborhoods_do_not_cover_the_gap(tmp_path):
    lines = [f"value_{n} = {n}" for n in range(1, 301)]
    root, base = source_repo(tmp_path, {"large.py": "\n".join(lines) + "\n"})
    lines[99], lines[249] = "value_100 = -1", "value_250 = -1"
    (root / "large.py").write_text("\n".join(lines) + "\n")
    snapshot = Snapshot(root, base, commit(root, "head"))
    index = FragmentIndex(snapshot, ReviewSession(), build_context(snapshot, max_chars=40000))
    assert index.lookup("large.py", "head", 100, 100)
    assert index.lookup("large.py", "head", 250, 250)
    assert not index.lookup("large.py", "head", 180, 180)


@pytest.mark.parametrize("kind", ["raw", "numbered"])
def test_builder_clipping_excludes_partial_lines_and_ignores_marker_text(tmp_path, kind):
    text = "# [content truncated]\n" + "x" * 300 + "\n"
    root, sha = source_repo(tmp_path, {"data.txt": text})
    snapshot = Snapshot(root, sha, sha)
    builder = _Builder(400, "")
    builder.add("data.txt", "test", _numbered(text) if kind == "numbered" else text, 100, kind=kind)
    pack = builder.finish()
    index = FragmentIndex(snapshot, ReviewSession(), pack)
    assert index.lookup("data.txt", "head", 1, 1)[0]["complete"]
    assert not index.lookup("data.txt", "head", 2, 2)[0]["complete"]
    visible = index.visible([HumanMessage(pack.text)], pack.text)
    assert [span for v in visible for span in v["complete_ranges"]] == [[1, 1]]
    assert [span for v in visible for span in v["partial_ranges"]] == [[2, 2]]


def test_actual_request_prefix_and_reference_only_summary_have_distinct_visibility(tmp_path):
    snapshot = make_snapshot(tmp_path)
    builder = _Builder(1000, "")
    builder.add("pricing.py", "test", "1: first\n2: second", 500)
    pack = builder.finish()
    index = FragmentIndex(snapshot, ReviewSession(), pack)
    prefix = pack.text[: pack.text.index("second") + 2]
    displayed = index.visible([HumanMessage(prefix)], prefix)
    assert displayed[0]["complete_ranges"] == [[1, 1]]
    assert displayed[0]["partial_ranges"] == [[2, 2]]
    assert (
        index.visible([AIMessage(pack.text), HumanMessage("summary with fragment IDs")], pack.text)
        == []
    )
    assert index.references()


def test_real_read_receipts_deduplicate_and_keep_versions_separate(tmp_path):
    snapshot = make_snapshot(tmp_path)
    session = ReviewSession()
    tools = toolset(snapshot, session)
    outputs = []
    for version in ("head", "base", "head"):
        outputs.append(
            tools["read_code"].invoke(
                {"path": "pricing.py", "version": version, "start_line": 3, "end_line": 5}
            )
        )
    tools["read_code"].invoke({"path": "missing.py"})
    tools["list_code_files"].invoke({})
    index = FragmentIndex(snapshot, session, empty_pack())
    manifest = index.manifest()
    assert len(manifest["records"]) == 2
    head = index.lookup("pricing.py", "head", 3, 5)[0]
    assert len(head["origins"]) == 2
    assert head["id"] != index.lookup("pricing.py", "base", 3, 5)[0]["id"]
    messages = [ToolMessage(outputs[0], name="read_code", tool_call_id="read")]
    assert index.visible(messages, "")[0]["complete_ranges"] == [[3, 5]]
    assert (
        index.visible(
            [ToolMessage("Output saved to /large.txt", name="read_code", tool_call_id="read")], ""
        )
        == []
    )
    assert index.manifest() == manifest


def test_partial_read_and_long_search_hit_never_claim_full_lines(tmp_path):
    snapshot = make_snapshot(tmp_path)
    session = ReviewSession()
    output = toolset(snapshot, session, BudgetPolicy(read_chars=12))["read_code"].invoke(
        {"path": "pricing.py"}
    )
    index = FragmentIndex(snapshot, session, empty_pack())
    assert all(not r["complete"] for r in index.manifest()["records"])
    displayed = index.visible([ToolMessage(output, name="read_code", tool_call_id="read")], "")
    assert displayed and all(not item["complete_ranges"] for item in displayed)

    root, sha = source_repo(
        tmp_path / "search", {"long.py": "# MATCH " + "z" * 400 + "\n# MATCH short\n"}
    )
    search_snapshot = Snapshot(root, sha, sha)
    search_session = ReviewSession()
    result = toolset(search_snapshot, search_session)["search_code"].invoke({"query": "MATCH"})
    assert json.loads(result)["matches"][0]["text_truncated"]
    search_index = FragmentIndex(search_snapshot, search_session, empty_pack())
    assert not search_index.lookup("long.py", "head", 1, 1)[0]["complete"]
    assert search_index.lookup("long.py", "head", 2, 2)[0]["complete"]


@pytest.mark.parametrize("limit", ["records", "bytes", "origins"])
def test_index_limits_are_explicit_and_do_not_truncate_records(tmp_path, monkeypatch, limit):
    snapshot = make_snapshot(tmp_path)
    session = ReviewSession()
    tools = toolset(snapshot, session)
    for end in (3, 5, 5, 5):
        tools["read_code"].invoke({"path": "pricing.py", "start_line": 3, "end_line": end})
    if limit == "records":
        monkeypatch.setattr(FragmentIndex, "MAX_FRAGMENTS", 1)
    elif limit == "bytes":
        monkeypatch.setattr(FragmentIndex, "MAX_BYTES", 100)
    else:
        monkeypatch.setattr(FragmentIndex, "MAX_ORIGINS", 1)
    value = FragmentIndex(snapshot, session, empty_pack()).manifest()
    assert len(value["records"]) <= value["max_records"]
    assert value["record_bytes"] <= value["max_record_bytes"]
    assert value["omitted_fragments"] + value["omitted_origins"] > 0
    assert value["record_bytes"] == sum(
        len(json.dumps(record, ensure_ascii=True).encode()) for record in value["records"]
    )


def test_symbol_analysis_is_bounded_without_losing_source_locations(tmp_path):
    root, sha = source_repo(
        tmp_path,
        {"large.py": "def large():\n    pass\n" + "# padding\n" * 7000, "bad.py": "def bad(\n"},
    )
    snapshot = Snapshot(root, sha, sha)
    session = ReviewSession()
    tools = toolset(snapshot, session)
    for path in ("large.py", "bad.py"):
        tools["read_code"].invoke({"path": path, "start_line": 1, "end_line": 1})
    index = FragmentIndex(snapshot, session, empty_pack())
    assert index.lookup("large.py", "head", 1, 1)[0]["symbol_status"] == "analysis_limit"
    assert index.lookup("bad.py", "head", 1, 1)[0]["symbol_status"] == "unavailable"
    assert not index.lookup("large.py", "head", 1, 1, symbol="large")


def test_symlink_diff_metadata_is_not_regular_source_coverage(tmp_path):
    root, base = source_repo(tmp_path, {"safe.py": "safe = 1\n"})
    (root / "link.py").symlink_to("/etc/passwd")
    snapshot = Snapshot(root, base, commit(root, "symlink"))
    index = FragmentIndex(snapshot, ReviewSession(), build_context(snapshot))
    assert not index.lookup("link.py", "head", 1, 1)
    assert index.manifest()["unsupported_source_fragments"] == 1


def test_legacy_materials_are_explicitly_unindexed(tmp_path):
    pack = ContextPack("old text", (ContextItem("pricing.py", "old", "old text"),), (), 20)
    value = FragmentIndex(make_snapshot(tmp_path), ReviewSession(), pack).manifest()
    assert value["unindexed_legacy_materials"] == 1
    assert not value["records"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_git_locations_survive_unicode_separators_and_crlf(tmp_path, newline):
    text = newline.join(["label = 'A\u2028@@ -100 +100 @@ B'", "value = 1", ""])
    root, base = source_repo(tmp_path, {"unicode.py": text})
    (root / "unicode.py").write_text(text.replace("value = 1", "value = 2"))
    snapshot = Snapshot(root, base, commit(root, "head"))
    assert snapshot.changed_files[0].added_ranges == ((2, 2),)
    assert snapshot.validate_location("unicode.py", 2)
    assert not snapshot.validate_location("unicode.py", 3)
    pack = build_context(snapshot)
    session = ReviewSession()
    tools = toolset(snapshot, session)
    output = json.loads(tools["read_code"].invoke({"path": "unicode.py"}))
    assert output["returned_range"] == [1, 2]
    search = json.loads(tools["search_code"].invoke({"query": "value"}))
    assert search["matches"][0]["line"] == 2
    index = FragmentIndex(snapshot, session, pack)
    for record in index.manifest()["records"]:
        start, end = record["line_range"]
        assert end <= 2
        source = git_lines(snapshot.read_file(record["path"], record["version"]))
        assert (
            record["text_sha256"]
            == hashlib.sha256("\n".join(source[start - 1 : end]).encode()).hexdigest()
        )


def test_completed_review_rebuilds_identical_index_without_more_calls(tmp_path):
    snapshot = make_snapshot(tmp_path)
    options = {"runs_dir": tmp_path / "runs", "run_id": "fragment", "run_tests": True}
    first = review(snapshot, DemoChatModel(), **options)
    second = review(snapshot, DemoChatModel(), **options, resume=True)
    assert (
        first["context"]["assembly"]["fragment_index"]
        == second["context"]["assembly"]["fragment_index"]
    )
    assert first["context"]["assembly"]["requests"] == second["context"]["assembly"]["requests"]
    assert first["budget_usage"] == second["budget_usage"]
    assert first["context"]["assembly"]["requests"][0]["source_fragments"]


def test_receipt_saved_before_graph_failure_rebuilds_index_on_resume(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    options = {"runs_dir": tmp_path / "runs", "run_id": "receipt", "run_tests": True}
    finish = RunStore.finish
    interrupted = False

    def interrupt(self, key, payload):
        nonlocal interrupted
        finish(self, key, payload)
        if not interrupted and payload.get("session", {}).get("trace"):
            interrupted = True
            raise OSError("receipt committed; graph not yet updated")

    monkeypatch.setattr(RunStore, "finish", interrupt)
    with pytest.raises(ReviewFailure) as failure:
        review(snapshot, DemoChatModel(), **options)
    records = failure.value.partial["context_assembly"]["fragment_index"]["records"]
    assert any(o["source"] == "read_code" for r in records for o in r["origins"])
    monkeypatch.setattr(RunStore, "finish", finish)
    restored = review(snapshot, DemoChatModel(), **options, resume=True)
    assert sum(event["tool"] == "read_code" for event in restored["trace"]) == 1
    records = restored["context"]["assembly"]["fragment_index"]["records"]
    assert any(o["source"] == "read_code" for r in records for o in r["origins"])
