"""The runner-level exception setting should apply unless invoke overrides it."""

import click
from click.testing import CliRunner


@click.command()
def raises_error() -> None:
    raise RuntimeError("expected")


runner = CliRunner(catch_exceptions=False)
try:
    runner.invoke(raises_error)
except RuntimeError as exc:
    assert str(exc) == "expected"
else:
    raise AssertionError("The runner should propagate the exception")

result = runner.invoke(raises_error, catch_exceptions=True)
assert result.exit_code == 1
assert isinstance(result.exception, RuntimeError)
