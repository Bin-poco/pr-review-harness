"""Local-first commands for reviewing commits and curating repository feedback."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from pr_review_harness.context import build_context
from pr_review_harness.demo import create_demo
from pr_review_harness.evaluation import evaluate_report, load_gold
from pr_review_harness.memory import MemoryStore
from pr_review_harness.report import write_report
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, review
from pr_review_harness.snapshot import Snapshot
from pr_review_harness.verification import DemoVerifierModel, verify_report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Python PR review harness (Deep Agents)")
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run a scripted, network-free tool-loop example")
    demo.add_argument("--out", type=Path)
    demo.add_argument(
        "--live", action="store_true", help="Use a configured live model on the example"
    )
    demo.add_argument("--verify", action="store_true", help="Run an independent second pass")
    _model_arguments(demo)
    actual = commands.add_parser("review", help="Review immutable local Git revisions with an LLM")
    _snapshot_arguments(actual)
    actual.add_argument("--out", type=Path, default=Path("outputs/review"))
    actual.add_argument(
        "--run-tests", action="store_true", help="Execute repository unittest code locally"
    )
    actual.add_argument("--context-chars", type=int, default=24000)
    actual.add_argument("--context-strategy", choices=["ast", "imports"], default="ast")
    actual.add_argument("--model-calls", type=int, default=12)
    actual.add_argument("--tool-calls", type=int, default=24)
    actual.add_argument("--verify", action="store_true", help="Run an independent second pass")
    actual.add_argument("--verify-max-findings", type=int, default=5)
    actual.add_argument("--verify-model-calls", type=int, default=24)
    actual.add_argument("--verify-tool-calls", type=int, default=32)
    _model_arguments(actual)
    evaluation = commands.add_parser("evaluate", help="Score a saved report against human labels")
    evaluation.add_argument("--report", type=Path, required=True)
    evaluation.add_argument("--gold", type=Path, required=True)
    evaluation.add_argument("--out", type=Path, help="Directory for evaluation.json")
    context = commands.add_parser("context", help="Inspect selected context without calling an LLM")
    _snapshot_arguments(context)
    context.add_argument("--max-chars", type=int, default=24000)
    context.add_argument("--strategy", choices=["ast", "imports"], default="ast")
    memory = commands.add_parser("memory", help="Add or inspect explicit maintainer feedback")
    memories = memory.add_subparsers(dest="memory_command", required=True)
    add = memories.add_parser("add")
    add.add_argument("--repo", type=Path, required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--source", required=True, help="PR/comment URL or local feedback reference")
    add.add_argument("--scope", default="*")
    add.add_argument("--ttl-days", type=int, default=90)
    add.add_argument("--disposition", choices=["accepted", "dismissed"], default="accepted")
    listing = memories.add_parser("list")
    listing.add_argument("--repo", type=Path, required=True)
    for command in (add, listing, actual, demo):
        command.add_argument("--memory-db", type=Path, default=Path(".pr-harness/memory.sqlite3"))
    return root


def _snapshot_arguments(command) -> None:
    command.add_argument("--repo", type=Path, required=True)
    command.add_argument("--base", required=True, help="Target branch/ref")
    command.add_argument("--head", default="HEAD")


def _model_arguments(command) -> None:
    command.add_argument("--model", default=os.getenv("HARNESS_MODEL"))
    command.add_argument("--base-url", default=os.getenv("HARNESS_BASE_URL"))
    command.add_argument("--api-key-env", default="HARNESS_API_KEY")


def _live_model(arguments):
    from langchain_openai import ChatOpenAI

    if not arguments.model:
        raise ValueError("Set HARNESS_MODEL or --model to a tool-calling model available to you.")
    key = os.getenv(arguments.api_key_env) or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError(
            f"Set {arguments.api_key_env} (or OPENAI_API_KEY); do not pass keys on CLI."
        )
    return ChatOpenAI(
        model=arguments.model,
        api_key=key,
        base_url=arguments.base_url,
        temperature=0,
        max_tokens=4096,
        timeout=60,
        max_retries=1,
        use_responses_api=False,
    )


def main() -> int:
    arguments = parser().parse_args()
    try:
        return _dispatch(arguments)
    except ReviewFailure as exc:
        output = getattr(arguments, "out", None) or Path("outputs/failed-review")
        output.mkdir(parents=True, exist_ok=True)
        failure_path = output / "failed.json"
        failure_path.write_text(json.dumps(exc.partial, ensure_ascii=False, indent=2))
        print(f"Review failed: {exc}. Partial evidence: {failure_path.resolve()}", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(arguments) -> int:
    if arguments.command == "demo":
        output = arguments.out or Path("outputs") / datetime.now().strftime("demo-%Y%m%d-%H%M%S")
        base, head = create_demo(output / "repo")
        snapshot = Snapshot.load(output / "repo", base, head)
        model = _live_model(arguments) if arguments.live else DemoChatModel()
        mode = "live" if arguments.live else "scripted-demo"
        memory = MemoryStore(arguments.memory_db).recall(
            snapshot.repo_id, [item.path for item in snapshot.changed_files]
        )
        report = review(snapshot, model, memory=memory, run_tests=True, mode=mode)
        paths = write_report(report, output)
        if arguments.verify:
            verifier = model if arguments.live else DemoVerifierModel()
            report = verify_report(snapshot, report, verifier)
            paths = write_report(report, output)
        print(f"Report: {paths[1].resolve()}")
        print(f"Evidence and trace: {paths[0].resolve()}")
        return 0
    if arguments.command == "evaluate":
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
        result = evaluate_report(report, load_gold(arguments.gold))
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if arguments.out:
            arguments.out.mkdir(parents=True, exist_ok=True)
            target = arguments.out / "evaluation.json"
            target.write_text(rendered + "\n", encoding="utf-8")
            print(f"Evaluation: {target.resolve()}")
        else:
            print(rendered)
        return 0
    if arguments.command == "memory":
        snapshot = Snapshot.load(arguments.repo, "HEAD", "HEAD")
        store = MemoryStore(arguments.memory_db)
        if arguments.memory_command == "list":
            print(json.dumps(store.list_records(snapshot.repo_id), ensure_ascii=False, indent=2))
        else:
            record_id = store.add(
                snapshot.repo_id,
                arguments.text,
                arguments.source,
                path_glob=arguments.scope,
                ttl_days=arguments.ttl_days,
                disposition=arguments.disposition,
            )
            print(f"Saved feedback: {record_id}")
        return 0
    snapshot = Snapshot.load(arguments.repo, arguments.base, arguments.head)
    if arguments.command == "context":
        print(
            build_context(snapshot, max_chars=arguments.max_chars, strategy=arguments.strategy).text
        )
        return 0
    if not 1 <= arguments.model_calls <= 50 or not 1 <= arguments.tool_calls <= 100:
        raise ValueError("Use 1–50 model calls and 1–100 tool calls.")
    if not 1 <= arguments.verify_max_findings <= 10:
        raise ValueError("Use 1–10 verifier findings.")
    if not 1 <= arguments.verify_model_calls <= 50 or not 1 <= arguments.verify_tool_calls <= 100:
        raise ValueError("Use 1–50 verifier model calls and 1–100 verifier tool calls.")
    memory = MemoryStore(arguments.memory_db).recall(
        snapshot.repo_id, [item.path for item in snapshot.changed_files]
    )
    model = _live_model(arguments)
    report = review(
        snapshot,
        model,
        memory=memory,
        run_tests=arguments.run_tests,
        context_chars=arguments.context_chars,
        context_strategy=arguments.context_strategy,
        model_calls=arguments.model_calls,
        tool_calls=arguments.tool_calls,
    )
    paths = write_report(report, arguments.out)
    if arguments.verify:
        report = verify_report(
            snapshot,
            report,
            model,
            model_calls=arguments.verify_model_calls,
            tool_calls=arguments.verify_tool_calls,
            max_findings=arguments.verify_max_findings,
        )
        paths = write_report(report, arguments.out)
    print(f"Report: {paths[1].resolve()}")
    print(f"Evidence and trace: {paths[0].resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
