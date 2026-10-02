"""Resolve a relative symlink against its containing directory."""

from pathlib import Path
from tempfile import TemporaryDirectory

from click import Path as ClickPath

with TemporaryDirectory() as directory:
    root = Path(directory)
    target = root / "target.txt"
    target.write_text("marker", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to("target.txt")
    converted = ClickPath(exists=True, resolve_path=True).convert(str(link), None, None)
    assert Path(converted).read_text(encoding="utf-8") == "marker"
