"""A missing context default should be exposed as None to callers."""

import click

context = click.Context(click.Command("cmd"))
assert context.lookup_default("missing") is None
