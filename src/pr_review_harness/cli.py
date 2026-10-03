"""Local-first commands for reviewing commits and curating repository feedback."""

import argparse
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from pr_review_harness.automation import parse_event
from pr_review_harness.benchmark import run_benchmark
from pr_review_harness.budget import DEFAULT_POLICY, BudgetPolicy
from pr_review_harness.cloud_cache import (
    cache_identity,
    export_syntax_cache,
    restore_latest_syntax_cache,
)
from pr_review_harness.cloud_state import (
    CloudContext,
    download_checkpoint,
    export_checkpoint,
    restore_checkpoint,
    resume_selection,
    state_profile,
    write_pointer,
)
from pr_review_harness.context import build_context
from pr_review_harness.demo import create_demo
from pr_review_harness.evaluation import evaluate_report, load_gold
from pr_review_harness.execution import ExecutionPolicy
from pr_review_harness.github import PRRef, configured_client, fetch_snapshot, publish_report
from pr_review_harness.incremental import IncrementalStore
from pr_review_harness.memory import MemoryStore
from pr_review_harness.memory_bundle import DEFAULT_MEMORY_PATH, load_github_memory, read_bundle
from pr_review_harness.persistence import RunStore, atomic_json, identity
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
    demo.add_argument("--runs-dir", type=Path, default=Path(".pr-harness/runs"))
    demo.add_argument("--run-id")
    _execution_arguments(demo, default="local")
    _budget_arguments(demo)
    _model_arguments(demo)
    actual = commands.add_parser("review", help="Review immutable local Git revisions with an LLM")
    _review_arguments(actual)
    _budget_arguments(actual)
    _model_arguments(actual)
    github = commands.add_parser(
        "github-review", help="Fetch and review a GitHub PR; no publishing"
    )
    _github_arguments(github)
    _review_arguments(github, remote=True)
    _budget_arguments(github)
    _model_arguments(github)
    ci = commands.add_parser("github-ci", help="Review a validated Actions event; default preview")
    ci.add_argument("--event-file", type=Path, required=True)
    ci.add_argument("--repository", required=True, help="Trusted GITHUB_REPOSITORY")
    ci.add_argument("--event-name", default=os.getenv("GITHUB_EVENT_NAME"))
    ci.add_argument("--cache-dir", type=Path, default=Path(".pr-harness/github"))
    ci.add_argument("--send", action="store_true", help="Opt in to automatic COMMENT publication")
    ci.add_argument("--publications-dir", type=Path, default=Path(".pr-harness/publications"))
    ci.add_argument(
        "--cloud-checkpoint", action="store_true", help="Prepare a resumable Actions state artifact"
    )
    ci.add_argument(
        "--cloud-syntax-cache",
        action="store_true",
        help="Reuse pure compilation results from this trusted workflow's recent completed jobs",
    )
    ci.add_argument("--syntax-cache-dir", type=Path, default=Path(".pr-harness/syntax-cache"))
    ci.add_argument(
        "--resume-run-id", help="Recover state from a trusted Actions run in this repository"
    )
    ci.add_argument("--resume-attempt", default="1", help="Source Actions attempt (default: 1)")
    ci.add_argument(
        "--pause-after-review",
        action="store_true",
        help="Save the primary review before running verification; fresh runs only",
    )
    _review_arguments(ci, remote=True)
    _budget_arguments(ci)
    _model_arguments(ci)
    cloud_export = commands.add_parser(
        "cloud-export", help="Snapshot a stopped CI run, including committed SQLite WAL pages"
    )
    cloud_export.add_argument("--pointer", type=Path, default=Path("outputs/ci/cloud-run.json"))
    cloud_export.add_argument("--out", type=Path, default=Path("outputs/state/checkpoint.zip"))
    for command in (github, ci):
        command.add_argument(
            "--github-memory",
            action="store_true",
            help="Require and import feedback pinned from the business repository default branch",
        )
        command.add_argument("--github-memory-path", default=DEFAULT_MEMORY_PATH)
    fetch = commands.add_parser(
        "github-fetch", help="Fetch pinned GitHub PR context without a model"
    )
    _github_arguments(fetch)
    fetch.add_argument("--out", type=Path, default=Path("outputs/github-input"))
    fetch.add_argument("--max-chars", type=int, default=DEFAULT_POLICY.context_chars)
    publish = commands.add_parser("github-publish", help="Preview a saved review; --send posts it")
    publish.add_argument("--report", type=Path, required=True)
    publish.add_argument("--out", type=Path, default=Path("outputs/github-publication"))
    publish.add_argument("--publications-dir", type=Path, default=Path(".pr-harness/publications"))
    publish.add_argument("--send", action="store_true", help="Publish COMMENT review to GitHub")
    publish.add_argument("--retry-unknown", action="store_true")
    resume = commands.add_parser(
        "resume", help="Resume the exact saved run from SQLite checkpoints"
    )
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--runs-dir", type=Path, default=Path(".pr-harness/runs"))
    resume.add_argument("--out", type=Path, default=Path("outputs/resumed-review"))
    resume.add_argument("--head", help="Optional expected head SHA/ref; mismatch refuses recovery")
    resume.add_argument(
        "--retry-unknown",
        action="store_true",
        help="Explicitly rerun tools whose durable results are missing",
    )
    _model_arguments(resume)
    verify = commands.add_parser("verify", help="Run or resume the saved review's second stage")
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--out", type=Path, default=Path("outputs/verified-review"))
    verify.add_argument("--model-calls", type=int, help="Default: saved verifier stage budget")
    verify.add_argument("--tool-calls", type=int, help="Default: saved verifier stage budget")
    verify.add_argument("--max-findings", type=int, default=5)
    verify.add_argument("--retry-unknown", action="store_true")
    _model_arguments(verify)
    benchmark = commands.add_parser(
        "benchmark", help="Run fixed PR cases with controlled ablations"
    )
    benchmark.add_argument("--cases", type=Path, required=True)
    benchmark.add_argument("--out", type=Path, default=Path("outputs/benchmark"))
    benchmark.add_argument("--model-calls", type=int, default=DEFAULT_POLICY.model_calls)
    benchmark.add_argument("--tool-calls", type=int, default=DEFAULT_POLICY.tool_calls)
    benchmark.add_argument(
        "--variants", nargs="+", choices=["baseline", "working", "memory", "both"]
    )
    benchmark.add_argument(
        "--scripted", action="store_true", help="Offline plumbing only, no quality claim"
    )
    _model_arguments(benchmark)
    _budget_arguments(benchmark)
    evaluation = commands.add_parser("evaluate", help="Score a saved report against human labels")
    evaluation.add_argument("--report", type=Path, required=True)
    evaluation.add_argument("--gold", type=Path, required=True)
    evaluation.add_argument("--out", type=Path, help="Directory for evaluation.json")
    context = commands.add_parser("context", help="Inspect selected context without calling an LLM")
    _snapshot_arguments(context)
    context.add_argument("--max-chars", type=int, default=DEFAULT_POLICY.context_chars)
    context.add_argument("--strategy", choices=["ast", "imports"], default="ast")
    memory = commands.add_parser("memory", help="Add or inspect explicit maintainer feedback")
    memories = memory.add_subparsers(dest="memory_command", required=True)
    add = memories.add_parser("add")
    _memory_repository_arguments(add)
    add.add_argument("--text", required=True)
    add.add_argument("--source", required=True, help="PR/comment URL or local feedback reference")
    add.add_argument("--scope", default="*")
    add.add_argument("--rule-key", help="Explicit topic key used for potential-conflict groups")
    add.add_argument("--ttl-days", type=int, default=90)
    add.add_argument("--disposition", choices=["accepted", "dismissed"], default="accepted")
    feedback = memories.add_parser(
        "feedback", help="Record human feedback linked to a saved review"
    )
    _memory_repository_arguments(feedback)
    feedback.add_argument("--report", type=Path, required=True)
    feedback.add_argument("--finding-id", help="Stable finding ID from review.json")
    feedback.add_argument("--text", required=True)
    feedback.add_argument("--source", required=True)
    feedback.add_argument("--scope")
    feedback.add_argument("--rule-key")
    feedback.add_argument("--ttl-days", type=int, default=90)
    feedback.add_argument("--disposition", choices=["accepted", "dismissed"], default="dismissed")
    listing = memories.add_parser("list")
    _memory_repository_arguments(listing)
    export = memories.add_parser("export", help="Export complete versioned feedback history")
    _memory_repository_arguments(export)
    export.add_argument("--out", type=Path, required=True)
    restore = memories.add_parser(
        "import", help="Import a validated bundle; refuse local edit loss"
    )
    _memory_repository_arguments(restore)
    restore.add_argument("--bundle", type=Path, required=True)
    revise = memories.add_parser("revise", help="Replace feedback and retain its history")
    revise.add_argument("--rule-key", help="Defaults to previous topic key")
    revise.add_argument("--text", required=True)
    revise.add_argument("--source", required=True)
    revise.add_argument("--scope", default=None, help="Defaults to the previous record scope")
    revise.add_argument("--ttl-days", type=int, default=90)
    revise.add_argument("--disposition", choices=["accepted", "dismissed"], default=None)
    revoke = memories.add_parser("revoke", help="Exclude feedback from future reviews")
    for command in (revise, revoke):
        _memory_repository_arguments(command)
        command.add_argument("--id", type=int, required=True)
        command.add_argument("--reason", required=True)
    for command in (
        add,
        feedback,
        listing,
        export,
        restore,
        revise,
        revoke,
        actual,
        demo,
        github,
        ci,
    ):
        command.add_argument("--memory-db", type=Path, default=Path(".pr-harness/memory.sqlite3"))
    return root


def _github_arguments(command):
    command.add_argument("--pr", required=True, help="https://github.com/OWNER/REPO/pull/123")
    command.add_argument("--cache-dir", type=Path, default=Path(".pr-harness/github"))


def _memory_repository_arguments(command):
    repository = command.add_mutually_exclusive_group(required=True)
    repository.add_argument("--repo", type=Path, help="Local repository identity")
    repository.add_argument("--pr", help="GitHub PR URL; share feedback by GitHub repository ID")
    command.add_argument("--cache-dir", type=Path, default=Path(".pr-harness/github"))


def _review_arguments(command, *, remote=False):
    if not remote:
        _snapshot_arguments(command)
    command.add_argument(
        "--run-tests", action="store_true", help="Allow bounded repository unittest execution"
    )
    _execution_arguments(command)
    if remote:
        command.add_argument("--expected-head", help="Refuse a PR event whose head SHA changed")
    command.add_argument("--out", type=Path, default=Path("outputs/review"))
    command.add_argument("--context-chars", type=int, default=DEFAULT_POLICY.context_chars)
    command.add_argument("--context-strategy", choices=["ast", "imports"], default="ast")
    command.add_argument("--model-calls", type=int, default=DEFAULT_POLICY.model_calls)
    command.add_argument("--tool-calls", type=int, default=DEFAULT_POLICY.tool_calls)
    command.add_argument("--verify", action="store_true", help="Run an independent second pass")
    command.add_argument("--verify-max-findings", type=int, default=5)
    command.add_argument(
        "--verify-model-calls", type=int, default=DEFAULT_POLICY.verify_model_calls
    )
    command.add_argument("--verify-tool-calls", type=int, default=DEFAULT_POLICY.verify_tool_calls)
    command.add_argument("--runs-dir", type=Path, default=Path(".pr-harness/runs"))
    command.add_argument("--run-id")
    command.add_argument(
        "--incremental",
        action="store_true",
        help="Prioritize PR updates and reuse pure syntax checks locally",
    )
    command.add_argument(
        "--incremental-dir",
        type=Path,
        default=Path(".pr-harness/incremental"),
        help="Trusted local baseline and syntax cache directory",
    )
    command.add_argument(
        "--review-key",
        help="Stable local review label across head updates; "
        "GitHub reviews use the numeric repository ID and PR number",
    )


def _execution_arguments(command, *, default="docker"):
    command.add_argument("--test-backend", choices=["local", "docker"], default=default)
    command.add_argument("--test-image", default="python:3.12-slim")
    command.add_argument("--test-timeout", type=int, default=20)
    command.add_argument("--test-memory-mb", type=int, default=256)
    command.add_argument("--test-cpus", type=float, default=1.0)
    command.add_argument("--test-pids", type=int, default=64)


def _execution_policy(arguments):
    return ExecutionPolicy(
        backend=arguments.test_backend,
        image=arguments.test_image,
        timeout=arguments.test_timeout,
        memory_mb=arguments.test_memory_mb,
        cpus=arguments.test_cpus,
        pids=arguments.test_pids,
    )


def _budget_arguments(command):
    command.add_argument(
        "--submission-repairs",
        type=int,
        default=DEFAULT_POLICY.submission_repairs,
        help="Maximum final-output corrections within existing call budgets (0–5)",
    )
    command.add_argument("--read-chars", type=int, default=DEFAULT_POLICY.read_chars)
    command.add_argument("--window-tokens", type=int, help="Explicit verified model context window")
    command.add_argument("--output-tokens", type=int, default=DEFAULT_POLICY.output_tokens)
    command.add_argument(
        "--request-chars",
        type=int,
        default=DEFAULT_POLICY.request_chars,
        help="Complete request cap when the model window is unknown",
    )
    command.add_argument(
        "--total-model-calls",
        type=int,
        default=DEFAULT_POLICY.total_model_calls,
        help="Global review/summary/verifier attempt limit, preserved on resume",
    )
    command.add_argument("--total-tool-calls", type=int, default=DEFAULT_POLICY.total_tool_calls)
    command.add_argument(
        "--no-working-state", action="store_true", help="Ablation: omit factual ledger"
    )
    command.add_argument("--no-memory", action="store_true", help="Ablation: omit human feedback")


def _policy(arguments, model):
    return BudgetPolicy.for_model(
        model,
        context_chars=getattr(arguments, "context_chars", DEFAULT_POLICY.context_chars),
        model_calls=getattr(arguments, "model_calls", DEFAULT_POLICY.model_calls),
        tool_calls=getattr(arguments, "tool_calls", DEFAULT_POLICY.tool_calls),
        read_chars=arguments.read_chars,
        verify_model_calls=getattr(
            arguments, "verify_model_calls", DEFAULT_POLICY.verify_model_calls
        ),
        verify_tool_calls=getattr(arguments, "verify_tool_calls", DEFAULT_POLICY.verify_tool_calls),
        window_tokens=arguments.window_tokens,
        output_tokens=arguments.output_tokens,
        request_chars=arguments.request_chars,
        total_model_calls=arguments.total_model_calls,
        total_tool_calls=arguments.total_tool_calls,
        working_state=not arguments.no_working_state,
        submission_repairs=arguments.submission_repairs,
    )


def _snapshot_arguments(command) -> None:
    command.add_argument("--repo", type=Path, required=True)
    command.add_argument("--base", required=True, help="Target branch/ref")
    command.add_argument("--head", default="HEAD")


def _model_arguments(command) -> None:
    command.add_argument("--model", default=os.getenv("HARNESS_MODEL"))
    command.add_argument("--base-url", default=os.getenv("HARNESS_BASE_URL"))
    command.add_argument("--api-key-env", default="HARNESS_API_KEY")
    command.add_argument("--thinking-mode", choices=["enabled", "disabled"])


def _live_model(arguments):
    from langchain_openai import ChatOpenAI

    if not arguments.model:
        raise ValueError("Set HARNESS_MODEL or --model to a tool-calling model available to you.")
    key = os.getenv(arguments.api_key_env) or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError(
            f"Set {arguments.api_key_env} (or OPENAI_API_KEY); do not pass keys on CLI."
        )
    options = {}
    if arguments.thinking_mode:
        options["extra_body"] = {"thinking": {"type": arguments.thinking_mode}}
    return ChatOpenAI(
        model=arguments.model,
        api_key=key,
        base_url=arguments.base_url,
        temperature=0,
        max_tokens=getattr(arguments, "output_tokens", DEFAULT_POLICY.output_tokens),
        timeout=60,
        max_retries=1,
        use_responses_api=False,
        model_kwargs={"parallel_tool_calls": False},
        **options,
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
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(arguments) -> int:
    event = None
    cloud = None
    selection = None
    cache_context = None
    syntax_cache = None
    syntax_cache_source = None
    if arguments.command == "cloud-export":
        repository = os.getenv("GITHUB_REPOSITORY", "")
        repository_id = int(os.getenv("GITHUB_REPOSITORY_ID", "0"))
        cloud = CloudContext.from_environment(repository, repository_id)
        result = export_checkpoint(arguments.pointer, arguments.out, cloud)
        atomic_json(arguments.pointer.parent / "checkpoint-export.json", result)
        print(f"Cloud checkpoint: {result['status']}")
        return 0
    if arguments.command == "github-ci":
        if arguments.run_tests:
            raise ValueError("Credential-bearing GitHub CI permits syntax checks only")
        if arguments.event_file.stat().st_size > 2_000_000:
            raise ValueError("GitHub event exceeds the supported size")
        event = parse_event(
            json.loads(arguments.event_file.read_text(encoding="utf-8")),
            repository=arguments.repository,
            event_name=arguments.event_name,
        )
        if event.skip_reason:
            atomic_json(
                arguments.out / "automation.json",
                {
                    "status": "skipped",
                    "reason": event.skip_reason,
                },
            )
            print(f"Automation skipped: {event.skip_reason}")
            return 0
        arguments.pr = event.ref.url
        if arguments.cloud_syntax_cache:
            if arguments.incremental:
                raise ValueError("Cloud syntax caching uses full reviews without baseline import")
            cache_context = CloudContext.from_environment(arguments.repository, event.repository_id)
        if (
            arguments.resume_run_id or arguments.pause_after_review
        ) and not arguments.cloud_checkpoint:
            raise ValueError("Cloud recovery or pausing requires --cloud-checkpoint")
        if arguments.cloud_checkpoint:
            if arguments.send or arguments.incremental:
                raise ValueError(
                    "Cloud recovery currently supports preview without incremental scheduling"
                )
            cloud = CloudContext.from_environment(arguments.repository, event.repository_id)
            selection = resume_selection(cloud, arguments.resume_run_id, arguments.resume_attempt)
            if arguments.pause_after_review and not arguments.verify:
                raise ValueError("Pausing after review requires a requested verification stage")
    if arguments.command == "github-fetch":
        snapshot, source = fetch_snapshot(
            PRRef.parse(arguments.pr), configured_client(), arguments.cache_dir
        )
        context = build_context(snapshot, max_chars=arguments.max_chars)
        arguments.out.mkdir(parents=True, exist_ok=True)
        atomic_json(
            arguments.out / "snapshot.json",
            {
                "source": source,
                "repo": str(snapshot.repo),
                "repo_id": snapshot.repo_id,
                "base_sha": snapshot.base_sha,
                "head_sha": snapshot.head_sha,
                "merge_base_sha": snapshot.merge_base_sha,
            },
        )
        (arguments.out / "context.txt").write_text(context.text, encoding="utf-8")
        print(f"PR snapshot: {(arguments.out / 'snapshot.json').resolve()}")
        return 0
    if arguments.command == "github-publish":
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
        result = publish_report(
            report,
            configured_client(),
            arguments.publications_dir,
            send=arguments.send,
            retry_unknown=arguments.retry_unknown,
        )
        atomic_json(arguments.out / "publication.json", result)
        print(f"Publication: {result['status']}; {(arguments.out / 'publication.json').resolve()}")
        return 0
    if arguments.command == "benchmark":
        model = DemoChatModel() if arguments.scripted else _live_model(arguments)
        result = run_benchmark(
            arguments.cases,
            model,
            _policy(arguments, model),
            arguments.out,
            variants=arguments.variants,
            scripted=arguments.scripted,
        )
        print(f"Benchmark: {result}")
        return 0
    if arguments.command == "verify":
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
        policy = BudgetPolicy(**report["run_manifest"]["budget"])
        if report["mode"] == "scripted-demo":
            model = DemoVerifierModel()
        else:
            arguments.model = arguments.model or report["run_manifest"]["model"].get("model_name")
            arguments.base_url = arguments.base_url or report["run_manifest"]["model"].get(
                "openai_api_base"
            )
            arguments.output_tokens = policy.output_tokens
            model = _live_model(arguments)
        snapshot = Snapshot.load(
            Path(report["repo"]),
            report["base_sha"],
            report["head_sha"],
            repository_identity=report["run_manifest"].get("repository_identity"),
        )
        verified = verify_report(
            snapshot,
            report,
            model,
            budget=policy,
            model_calls=arguments.model_calls,
            tool_calls=arguments.tool_calls,
            max_findings=arguments.max_findings,
            retry_unknown=arguments.retry_unknown,
        )
        paths = write_report(verified, arguments.out)
        print(f"Verification: {verified['verification']['status']}; report: {paths[1].resolve()}")
        return 1 if verified["verification"]["status"] == "failed" else 0
    if arguments.command == "resume":
        store = RunStore(arguments.runs_dir, arguments.run_id)
        manifest, _ = store.load()
        policy = BudgetPolicy(**manifest["budget"])
        snapshot = Snapshot.load(
            Path(manifest["repo"]),
            manifest["base_sha"],
            arguments.head or manifest["head_sha"],
            repository_identity=manifest.get("repository_identity"),
        )
        if manifest["mode"] == "scripted-demo":
            model = DemoChatModel()
        else:
            arguments.model = arguments.model or manifest["model"].get("model_name")
            arguments.base_url = arguments.base_url or manifest["model"].get("openai_api_base")
            arguments.output_tokens = policy.output_tokens
            model = _live_model(arguments)
        report = review(
            snapshot,
            model,
            budget=policy,
            context_strategy=manifest["strategy"],
            run_tests=manifest["run_tests"],
            mode=manifest["mode"],
            runs_dir=arguments.runs_dir,
            run_id=arguments.run_id,
            resume=True,
            retry_unknown=arguments.retry_unknown,
            source=manifest.get("source"),
            execution=ExecutionPolicy.from_manifest(manifest.get("execution")),
            incremental=manifest.get("incremental", {}).get("enabled", False),
            incremental_dir=Path(
                manifest.get("incremental", {}).get("directory", ".pr-harness/incremental")
            ),
            review_key=manifest.get("incremental", {}).get("review_key"),
            syntax_cache=(
                IncrementalStore(Path(manifest["syntax_cache"]["directory"]))
                if manifest.get("syntax_cache", {}).get("enabled")
                else None
            ),
        )
        paths = write_report(report, arguments.out)
        print(f"Run: {report['run_id']}")
        print(f"Report: {paths[1].resolve()}")
        return 0
    if arguments.command == "demo":
        output = arguments.out or Path("outputs") / datetime.now().strftime("demo-%Y%m%d-%H%M%S")
        base, head = create_demo(output / "repo")
        snapshot = Snapshot.load(output / "repo", base, head)
        model = _live_model(arguments) if arguments.live else DemoChatModel()
        mode = "live" if arguments.live else "scripted-demo"
        policy = _policy(arguments, model)
        memory = MemoryStore(arguments.memory_db).freeze_snapshot(
            snapshot.repo_id, [item.path for item in snapshot.changed_files], policy.memory_chars
        )
        report = review(
            snapshot,
            model,
            memory="" if arguments.no_memory else memory,
            run_tests=True,
            execution=_execution_policy(arguments),
            mode=mode,
            budget=policy,
            runs_dir=arguments.runs_dir,
            run_id=arguments.run_id,
        )
        paths = write_report(report, output)
        if arguments.verify:
            verifier = model if arguments.live else DemoVerifierModel()
            report = verify_report(snapshot, report, verifier, budget=policy)
            paths = write_report(report, output)
        print(f"Run: {report['run_id']}")
        print(f"Report: {paths[1].resolve()}")
        print(f"Evidence and trace: {paths[0].resolve()}")
        return 1 if report.get("verification", {}).get("status") == "failed" else 0
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
        if arguments.pr:
            snapshot, _ = fetch_snapshot(
                PRRef.parse(arguments.pr), configured_client(), arguments.cache_dir
            )
        else:
            snapshot = Snapshot.load(arguments.repo, "HEAD", "HEAD")
        store = MemoryStore(arguments.memory_db)
        if arguments.memory_command == "list":
            print(json.dumps(store.list_records(snapshot.repo_id), ensure_ascii=False, indent=2))
        elif arguments.memory_command == "export":
            atomic_json(arguments.out, store.export_bundle(snapshot.repo_id))
            print(f"Exported feedback bundle: {arguments.out.resolve()}")
        elif arguments.memory_command == "import":
            store.import_bundle(snapshot.repo_id, read_bundle(arguments.bundle, snapshot.repo_id))
            print(f"Imported feedback bundle: {arguments.memory_db.resolve()}")
        elif arguments.memory_command == "feedback":
            report = json.loads(arguments.report.read_text(encoding="utf-8"))
            record_id = store.add_feedback(
                snapshot.repo_id,
                report,
                arguments.text,
                arguments.source,
                finding_id=arguments.finding_id,
                path_glob=arguments.scope,
                ttl_days=arguments.ttl_days,
                disposition=arguments.disposition,
                rule_key=arguments.rule_key,
            )
            print(f"Saved linked feedback: {record_id}")
        elif arguments.memory_command == "revoke":
            store.revoke(snapshot.repo_id, arguments.id, reason=arguments.reason)
            print(f"Revoked feedback: {arguments.id}")
        elif arguments.memory_command == "revise":
            record_id = store.revise(
                snapshot.repo_id,
                arguments.id,
                arguments.text,
                arguments.source,
                reason=arguments.reason,
                path_glob=arguments.scope,
                ttl_days=arguments.ttl_days,
                disposition=arguments.disposition,
                rule_key=arguments.rule_key,
            )
            print(f"Replaced feedback {arguments.id} with: {record_id}")
        else:
            record_id = store.add(
                snapshot.repo_id,
                arguments.text,
                arguments.source,
                path_glob=arguments.scope,
                ttl_days=arguments.ttl_days,
                disposition=arguments.disposition,
                rule_key=arguments.rule_key,
            )
            print(f"Saved feedback: {record_id}")
        return 0
    source = None
    if arguments.command in {"github-review", "github-ci"}:
        if arguments.run_tests and arguments.test_backend != "docker":
            raise ValueError("GitHub repository tests require --test-backend docker")
        snapshot, source = fetch_snapshot(
            PRRef.parse(arguments.pr), configured_client(), arguments.cache_dir
        )
        if arguments.expected_head and arguments.expected_head != snapshot.head_sha:
            raise ValueError("PR head changed since the triggering event; start a new run")
        if event:
            event.validate_source(source)
    else:
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
    if cache_context:
        syntax_cache = IncrementalStore(arguments.syntax_cache_dir)
        syntax_cache_source = (
            {"status": "resume_uses_frozen_receipts", "entries": 0}
            if selection
            else restore_latest_syntax_cache(
                configured_client(), cache_context, snapshot.repo_id, syntax_cache
            )
        )
        atomic_json(arguments.out / "syntax-cache-import.json", syntax_cache_source)
    if not selection:
        store = MemoryStore(arguments.memory_db)
        if source and arguments.github_memory:
            receipt = load_github_memory(
                snapshot, source, configured_client(), store, path=arguments.github_memory_path
            )
            atomic_json(arguments.out / "memory-source.json", receipt)
    model = _live_model(arguments)
    policy = _policy(arguments, model)
    restored_from = None
    if selection:
        bundle, origin = download_checkpoint(configured_client(), cloud, selection)
        expected = {
            **identity(snapshot, model, policy, arguments.context_strategy, False, "live"),
            "incremental": {"enabled": False},
            **(
                {"syntax_cache": cache_identity(arguments.syntax_cache_dir)} if syntax_cache else {}
            ),
        }
        saved, restored_from = restore_checkpoint(
            bundle,
            arguments.runs_dir,
            cloud,
            origin,
            snapshot,
            expected,
            state_profile(arguments),
            source,
        )
        arguments.run_id = saved["run_id"]
        # Titles may change; retain the original source object and the original frozen feedback.
        source = saved["source"]
        memory = ""
        _, artifacts = RunStore(arguments.runs_dir, arguments.run_id).load()
        origin = artifacts["memory"].get("storage", {}).get("origin")
        if origin:
            atomic_json(
                arguments.out / "memory-source.json", {**origin, "restored_frozen_snapshot": True}
            )
    else:
        memory = store.freeze_snapshot(
            snapshot.repo_id, [item.path for item in snapshot.changed_files], policy.memory_chars
        )
        if cloud or cache_context:
            if arguments.run_id:
                raise ValueError("Cloud runs assign their own stable run identity")
            context = cloud or cache_context
            arguments.run_id = f"ci-{context.run_id}-{context.attempt}"
    if cloud:
        write_pointer(
            arguments.out / "cloud-run.json",
            cloud,
            arguments.runs_dir,
            arguments.run_id,
            state_profile(arguments),
            restored_from,
        )
    try:
        report = review(
            snapshot,
            model,
            memory="" if arguments.no_memory else memory,
            budget=policy,
            runs_dir=arguments.runs_dir,
            run_id=arguments.run_id,
            resume=bool(selection),
            run_tests=arguments.run_tests,
            execution=_execution_policy(arguments),
            context_chars=arguments.context_chars,
            context_strategy=arguments.context_strategy,
            model_calls=arguments.model_calls,
            tool_calls=arguments.tool_calls,
            source=source,
            incremental=arguments.incremental,
            incremental_dir=arguments.incremental_dir,
            review_key=arguments.review_key,
            syntax_cache=syntax_cache,
            syntax_cache_source=syntax_cache_source,
        )
    finally:
        if syntax_cache:
            try:
                result = export_syntax_cache(
                    syntax_cache,
                    cache_context,
                    snapshot.repo_id,
                    arguments.out.parent / "cache/syntax-cache.json",
                )
            except (ValueError, OSError, sqlite3.Error):
                result = {"status": "unavailable", "entries": 0}
            atomic_json(arguments.out / "syntax-cache-export.json", result)
    paths = write_report(report, arguments.out)
    if cloud and arguments.pause_after_review and not selection:
        atomic_json(
            arguments.out / "automation.json",
            {
                "status": "paused",
                "stage": "before_verification",
                "run_id": report["run_id"],
                "head_sha": report["head_sha"],
                "publication": "not_requested",
            },
        )
        print(f"Primary review saved: {report['run_id']}; verification awaits recovery")
        return 0
    if arguments.verify:
        report = verify_report(
            snapshot,
            report,
            model,
            model_calls=arguments.verify_model_calls,
            tool_calls=arguments.verify_tool_calls,
            max_findings=arguments.verify_max_findings,
            budget=policy,
        )
        paths = write_report(report, arguments.out)
    print(f"Run: {report['run_id']}")
    print(f"Report: {paths[1].resolve()}")
    print(f"Evidence and trace: {paths[0].resolve()}")
    if event:
        if report.get("verification", {}).get("status") == "failed":
            raise ValueError("Independent verification failed; automatic publication refused")
        publication = publish_report(
            report,
            configured_client(),
            arguments.publications_dir,
            send=arguments.send,
        )
        atomic_json(arguments.out / "publication.json", publication)
        atomic_json(
            arguments.out / "automation.json",
            {
                "status": "completed",
                "publication": publication["status"],
                "head_sha": report["head_sha"],
                "run_id": report["run_id"],
                **({"restored_from": restored_from} if restored_from else {}),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
