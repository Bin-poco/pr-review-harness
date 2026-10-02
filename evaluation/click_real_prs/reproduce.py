"""Run independent reproducers against the immutable base and head trees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from prepare import OUTPUT, SOURCE, prepare

from pr_review_harness.snapshot import Snapshot

STDIN_REPRO = '''import click
from click.testing import CliRunner

@click.command()
@click.argument("file_input", type=click.File("r"))
def cli(file_input):
    for line in file_input.readlines():
        print(line.rstrip())

result = CliRunner().invoke(cli, ["-"], input="test\\n")
assert result.exit_code == 0 and result.output == "test\\n", (
    result.exit_code, result.output, repr(result.exception)
)
'''

PAGER_REPRO = '''from click import command, echo_via_pager
from click.testing import CliRunner

@command()
def cli():
    echo_via_pager("Hello, Click!")

result = CliRunner().invoke(cli)
assert result.exit_code == 0 and result.output == "Hello, Click!\\n"
'''

REPRODUCERS = {
    "click-2934-stdin-eof-regression": STDIN_REPRO,
    "click-1572-pager-closes-stdout": PAGER_REPRO,
    "click-2940-stdin-eof-fix": STDIN_REPRO,
}

PAGER_ADJUDICATIONS = {
    "no-stdout-fallback": {
        "code": '''import click
import click._termui_impl as impl

impl._default_text_stdout = lambda: None
impl.isatty = lambda stream: False
click.echo_via_pager("hello")
''',
        "expected": {"base": "pass", "head": "fail"},
    },
    "custom-color-attribute": {
        "code": '''import io
import click

class Sink(io.StringIO):
    color = False

sink = Sink()
click.echo(click.style("hello", fg="red"), file=sink)
assert "\\x1b[" not in sink.getvalue()
''',
        "expected": {"base": "pass", "head": "fail"},
    },
    "temporary-file-lifecycle": {
        "code": '''import os
import click
import click._termui_impl as impl

impl.WIN = True
impl.isatty = lambda stream: True
os.environ["PAGER"] = "true"
click.echo_via_pager("hello")
''',
        "expected": {"base": "pass", "head": "pass"},
    },
}


def reproduce() -> Path:
    prepare()
    manifest = json.loads(SOURCE.read_text(encoding="utf-8"))
    repo = OUTPUT / "repo"
    results = []
    with tempfile.TemporaryDirectory(prefix="click-repro-", dir=OUTPUT) as temporary:
        scratch = Path(temporary)
        for case in manifest["cases"]:
            snapshot = Snapshot.load(repo, case["base_sha"], case["head_sha"])
            for version in ("base", "head"):
                tree = scratch / case["id"] / version
                snapshot.export(version, tree)
                env = os.environ.copy()
                env["PYTHONPATH"] = str(tree / "src")
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["PAGER"] = ""
                checks = {
                    "known-regression": {
                        "code": REPRODUCERS[case["id"]],
                        "expected": case["expected_reproducer"],
                    }
                }
                if case["id"] == "click-1572-pager-closes-stdout":
                    checks.update(PAGER_ADJUDICATIONS)
                for check_name, spec in checks.items():
                    process = subprocess.run(
                        [sys.executable, "-c", spec["code"]],
                        cwd=tree,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    outcome = "pass" if process.returncode == 0 else "fail"
                    expected = spec["expected"][version]
                    results.append(
                        {
                            "case_id": case["id"],
                            "check": check_name,
                            "version": version,
                            "sha": (
                                snapshot.merge_base_sha if version == "base" else snapshot.head_sha
                            ),
                            "outcome": outcome,
                            "expected": expected,
                            "as_expected": outcome == expected,
                            "error_tail": process.stderr[-500:] if process.returncode else "",
                        }
                    )
    target = OUTPUT / "reproduction.json"
    target.write_text(
        json.dumps({"schema_version": 1, "python": sys.version, "runs": results}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    if not all(row["as_expected"] for row in results):
        raise RuntimeError(f"Reproducer results did not match pinned expectations: {target}")
    return target


if __name__ == "__main__":
    print(reproduce())
