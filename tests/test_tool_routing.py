"""Check repository discovery, wrong-space recovery, and native virtual storage."""

import json
from threading import Lock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_runtime import make_snapshot
from test_snapshot_context import commit, git

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.checks import CheckRunner
from pr_review_harness.models import ReviewSession
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, _review_tools, review
from pr_review_harness.snapshot import Snapshot


def inventory(snapshot, session=None, policy=None):
    return next(
        t
        for t in _review_tools(
            snapshot, session or ReviewSession(), CheckRunner(snapshot), Lock(), policy
        )
        if t.name == "list_code_files"
    )


def test_inventory_uses_pinned_regular_files_and_merge_base(tmp_path):
    original = make_snapshot(tmp_path)
    root = original.repo
    git(root, "checkout", "-qb", "inventory-base", original.merge_base_sha)
    (root / "old.txt").write_text("old\n")
    common = commit(root, "common with text file")
    git(root, "checkout", "-qb", "inventory-head")
    git(root, "mv", "old.txt", "new name.txt")
    (root / "escape.py").symlink_to("/etc/passwd")
    head = commit(root, "rename and symlink")
    git(root, "checkout", "-q", "inventory-base")
    (root / "base-tip-only.py").write_text("value = 1\n")
    base = commit(root, "target branch advanced")
    (root / "untracked.py").write_text("local = True\n")

    snapshot = Snapshot(root, base, head)
    head_page = json.loads(inventory(snapshot).invoke({}))
    base_page = json.loads(inventory(snapshot).invoke({"version": "base"}))
    assert head_page["sha"] == head
    assert base_page["sha"] == common
    assert "new name.txt" in head_page["paths"]
    assert "old.txt" in base_page["paths"]
    assert "old.txt" not in head_page["paths"]
    assert "new name.txt" not in base_page["paths"]
    assert not {"untracked.py", "escape.py", "base-tip-only.py"} & set(head_page["paths"])
    assert "base-tip-only.py" not in base_page["paths"]
    assert (root / "untracked.py").read_text() == "local = True\n"


def test_inventory_paginates_without_skipping_budget_shortened_paths(tmp_path):
    snapshot = make_snapshot(tmp_path)
    one = json.loads(inventory(snapshot).invoke({"limit": 1}))
    assert one["truncated"] and one["next_offset"] == 1
    second = json.loads(inventory(snapshot).invoke({"offset": 1, "limit": 1}))
    assert not second["truncated"] and second["next_offset"] is None
    assert one["paths"] + second["paths"] == list(snapshot.head_files)
    tests = json.loads(inventory(snapshot).invoke({"directory": "tests"}))
    assert tests["paths"] == ["tests/test_pricing.py"]

    # Limit 50 returns one complete path when the read budget cannot fit both.
    initial = json.loads(inventory(snapshot).invoke({"limit": 50}))
    initial["paths"] = initial["paths"][:1]
    initial.update(truncated=True, next_offset=1)
    session = ReviewSession()
    policy = BudgetPolicy(read_chars=len(json.dumps(initial, ensure_ascii=False)))
    tool = inventory(snapshot, session, policy)
    first = json.loads(tool.invoke({}))
    assert first["paths"] == [snapshot.head_files[0]]
    assert first["next_offset"] == 1
    assert session.read_chars == policy.read_chars
    exhausted = json.loads(tool.invoke({"offset": 1}))
    assert exhausted["truncated"] and "Read budget exhausted" in exhausted["error"]
    assert "next_offset" not in exhausted


@pytest.mark.parametrize("directory", ["../tests", "/tests", ".git", "tests/../tests", "a\\b"])
def test_inventory_rejects_unsafe_directories(tmp_path, directory):
    result = json.loads(inventory(make_snapshot(tmp_path)).invoke({"directory": directory}))
    assert "error" in result and "paths" not in result


@pytest.mark.parametrize("args", [{"offset": -1}, {"limit": 0}, {"limit": 101}])
def test_inventory_rejects_unbounded_pages(tmp_path, args):
    result = json.loads(inventory(make_snapshot(tmp_path)).invoke(args))
    assert "error" in result and "paths" not in result


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"file_path": "/pricing.py"}),
        ("grep", {"path": "/tests", "pattern": "pricing"}),
    ],
)
def test_missing_virtual_paths_do_not_query_repository(name, args):
    from types import SimpleNamespace

    from pr_review_harness.tool_routing import route_virtual_read

    session = ReviewSession()
    called = []
    request = SimpleNamespace(
        tool_call={"name": name, "args": args, "id": "missing"}, state={"files": {}}
    )
    result = route_virtual_read(request, lambda r: called.append(r), session)
    assert result.status == "error" and called == []
    assert json.loads(result.content)["scope"] == "virtual_agent_storage"
    assert session.tool_calls == 1


class RoutingModel(DemoChatModel):
    """Deliberately take a wrong path, then follow correction through the real graph."""

    fail: bool = False
    calls: int = 0
    descriptions: dict[str, str] = Field(default_factory=dict)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.descriptions.update({t.name: t.description for t in tools if hasattr(t, "name")})
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.fail and self.calls == 3:
            raise RuntimeError("interrupt after routing correction")
        done = [m for m in messages if isinstance(m, ToolMessage)]
        actions = [
            ("ls", {"path": "/tests"}),
            ("glob", {"pattern": "**/*.py"}),
            ("grep", {"pattern": "pricing"}),
            ("list_code_files", {"directory": "tests"}),
            ("read_code", {"path": "tests/test_pricing.py"}),
            ("read_file", {"file_path": "/skills/review/python-boundary-regressions/SKILL.md"}),
            ("read_file", {"file_path": "/memories/repository.md"}),
            ("write_file", {"file_path": "/notes.txt", "content": "saved scratch"}),
            ("read_file", {"file_path": "/notes.txt"}),
            ("submit_review", {"findings": []}),
        ]
        index = len(done)
        name, args = actions[index]
        if index == 1:
            result = json.loads(done[-1].content)
            assert result["routing"] == "redirected"
            assert "repository was not queried" in result["error"]
        if index in {2, 3}:
            assert "virtual agent storage only" in done[-1].content
        if index == 4:
            assert json.loads(done[-1].content)["paths"] == ["tests/test_pricing.py"]
        if index == 9:
            assert "saved scratch" in done[-1].content
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[{"name": name, "args": args, "id": f"route-{index}"}],
                    )
                )
            ]
        )


def test_wrong_space_recovers_and_native_files_survive_resume(tmp_path):
    snapshot = make_snapshot(tmp_path)
    model = RoutingModel(fail=True)
    options = {
        "runs_dir": tmp_path / "runs",
        "run_id": "routing",
        "memory": "Confirmed repository feedback",
        "model_calls": 16,
    }
    with pytest.raises(ReviewFailure) as caught:
        review(snapshot, model, **options)
    partial = caught.value.partial
    assert partial["tool_routing"]["redirected_virtual_calls"] == 1
    assert partial["budget_usage"]["tool_attempts"] == 2
    recovered = RoutingModel()
    report = review(snapshot, recovered, resume=True, **options)
    routing = report["tool_routing"]
    assert routing["repository_read_calls"] == 2
    assert routing["virtual_read_calls"] == 6
    assert routing["redirected_virtual_calls"] == 1
    assert report["budget_usage"]["tool_attempts"] == 10
    assert report["budget_usage"]["model_attempts"] == 11
    assert len({e["tool"] for e in report["trace"]}) == 7
    assert "list_code_files" in recovered.descriptions["ls"]
    assert "virtual" in recovered.descriptions["read_file"]
    assert "No regex" in recovered.descriptions["search_code"]
    assert not {"task", "execute"} & recovered.descriptions.keys()
