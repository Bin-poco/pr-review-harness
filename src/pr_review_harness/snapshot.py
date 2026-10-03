"""Read immutable Git versions without checking out or changing the source repository."""

import hashlib
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import ClassVar

from .models import ChangedFile


@dataclass(frozen=True)
class _TreeEntry:
    oid: str
    mode: str
    size: int


def _safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ValueError("Expected a nonempty repository-relative POSIX path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Paths must not be absolute or contain traversal components")
    if any(part.casefold() == ".git" for part in parsed.parts):
        raise ValueError("Git metadata paths are not reviewable")
    return path


def _added_ranges(patch: str) -> tuple[tuple[int, int], ...]:
    """Locate added lines, excluding unchanged lines in the same diff hunk."""
    added: list[int] = []
    head_line: int | None = None
    for line in patch.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            head_line = int(hunk.group(1))
        elif line.startswith("diff --git "):
            head_line = None
        elif head_line is not None:
            if line.startswith("+"):
                added.append(head_line)
                head_line += 1
            elif line.startswith(" "):
                head_line += 1
    ranges: list[tuple[int, int]] = []
    for line in added:
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return tuple(ranges)


@dataclass(frozen=True)
class Snapshot:
    """A merge-base/head comparison pinned to commits, independent of local edits."""

    repo: Path
    base_ref: str
    head_ref: str
    repository_identity: str | None = None
    base_sha: str = field(init=False)
    head_sha: str = field(init=False)
    merge_base_sha: str = field(init=False)
    changed_files: tuple[ChangedFile, ...] = field(init=False)
    head_files: tuple[str, ...] = field(init=False)
    repo_id: str = field(init=False)
    _base_entries: Mapping[str, _TreeEntry] = field(init=False, repr=False)
    _head_entries: Mapping[str, _TreeEntry] = field(init=False, repr=False)

    MAX_FILE_BYTES: ClassVar[int] = 1_000_000
    MAX_PATCH_BYTES: ClassVar[int] = 2_000_000
    MAX_TREE_FILES: ClassVar[int] = 100_000
    MAX_CHANGED_FILES: ClassVar[int] = 1_000
    MAX_EXPORT_FILES: ClassVar[int] = 10_000
    MAX_EXPORT_FILE_BYTES: ClassVar[int] = 8_000_000
    MAX_EXPORT_BYTES: ClassVar[int] = 64_000_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", Path(self.repo).expanduser().resolve())
        if not self.repo.is_dir():
            raise ValueError("Repository directory does not exist")
        inside = self._git("rev-parse", "--is-inside-work-tree").strip()
        if inside != b"true":
            raise ValueError("Expected a Git working tree")
        root = Path(self._git("rev-parse", "--show-toplevel").decode().strip()).resolve()
        object.__setattr__(self, "repo", root)
        base_sha = self._resolve_ref(self.base_ref)
        head_sha = self._resolve_ref(self.head_ref)
        merge_base = self._git("merge-base", base_sha, head_sha).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge_base):
            raise ValueError("Could not resolve a unique merge base")
        object.__setattr__(self, "base_sha", base_sha)
        object.__setattr__(self, "head_sha", head_sha)
        object.__setattr__(self, "merge_base_sha", merge_base)
        base_entries = self._tree(merge_base)
        head_entries = self._tree(head_sha)
        object.__setattr__(self, "_base_entries", MappingProxyType(base_entries))
        object.__setattr__(self, "_head_entries", MappingProxyType(head_entries))
        object.__setattr__(self, "head_files", tuple(sorted(head_entries)))
        common = Path(self._git("rev-parse", "--git-common-dir").decode().strip())
        if not common.is_absolute():
            common = self.repo / common
        identity = (self.repository_identity or str(common.resolve())).encode(
            "utf-8", errors="surrogateescape"
        )
        object.__setattr__(self, "repo_id", hashlib.sha256(identity).hexdigest())
        object.__setattr__(self, "changed_files", self._changes())

    @classmethod
    def load(
        cls, repo: Path, base_ref: str, head_ref: str, *, repository_identity=None
    ) -> "Snapshot":
        return cls(repo, base_ref, head_ref, repository_identity)

    def _git(self, *args: str, max_bytes: int = 32_000_000) -> bytes:
        result = subprocess.run(
            ["git", "--no-pager", "-C", str(self.repo), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
            raise ValueError(f"Git operation failed: {detail}")
        if len(result.stdout) > max_bytes:
            raise ValueError("Git result exceeds the supported snapshot size")
        return result.stdout

    def _resolve_ref(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref or ref.startswith("-"):
            raise ValueError("Expected a Git commit reference, not an option")
        if any(ord(char) < 32 for char in ref):
            raise ValueError("Git references must not contain control characters")
        sha = (
            self._git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValueError("Expected a single commit")
        return sha

    def _tree(self, sha: str) -> dict[str, _TreeEntry]:
        entries: dict[str, _TreeEntry] = {}
        data = self._git("ls-tree", "-r", "-l", "-z", "--full-tree", sha)
        for record in data.split(b"\x00"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                continue  # Symlinks and submodules are never followed or exported.
            path = raw_path.decode("utf-8", errors="surrogateescape")
            try:
                _safe_path(path)
            except ValueError:
                continue
            entries[path] = _TreeEntry(oid.decode(), mode.decode(), int(size))
            if len(entries) > self.MAX_TREE_FILES:
                raise ValueError("Repository has too many files for this snapshot")
        return entries

    def _changes(self) -> tuple[ChangedFile, ...]:
        records = self._git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-status",
            "-z",
            "--find-renames",
            self.merge_base_sha,
            self.head_sha,
            "--",
        ).split(b"\x00")
        changes: list[ChangedFile] = []
        cursor = 0
        while cursor < len(records) and records[cursor]:
            status = records[cursor].decode("ascii")
            cursor += 1
            old_path = records[cursor].decode("utf-8", errors="surrogateescape")
            cursor += 1
            path = old_path
            if status.startswith(("R", "C")):
                path = records[cursor].decode("utf-8", errors="surrogateescape")
                cursor += 1
            _safe_path(path)
            _safe_path(old_path)
            pathspecs = [f":(literal){old_path}"]
            if path != old_path:
                pathspecs.append(f":(literal){path}")
            patch = self._git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--unified=3",
                "--find-renames",
                self.merge_base_sha,
                self.head_sha,
                "--",
                *pathspecs,
                max_bytes=self.MAX_PATCH_BYTES,
            ).decode("utf-8", errors="replace")
            changes.append(ChangedFile(path, status, patch, _added_ranges(patch)))
            if len(changes) > self.MAX_CHANGED_FILES:
                raise ValueError("PR has too many changed files for this snapshot")
        return tuple(changes)

    def _entries(self, version: str) -> Mapping[str, _TreeEntry]:
        if version == "head":
            return self._head_entries
        if version == "base":
            return self._base_entries
        raise ValueError("version must be 'base' (merge base) or 'head'")

    def file_size(self, path: str, version: str = "head") -> int:
        """Return blob size without loading it, for bounded context selection."""
        entry = self._entries(version).get(_safe_path(path))
        if entry is None:
            raise FileNotFoundError(f"No regular file at {path!r} in {version}")
        return entry.size

    def file_paths(self, directory: str = "", version: str = "head") -> tuple[str, ...]:
        """List regular tracked files under a literal directory at the pinned version."""
        prefix = _safe_path(directory) + "/" if directory else ""
        return tuple(sorted(path for path in self._entries(version) if path.startswith(prefix)))

    def read_file(self, path: str, version: str = "head") -> str:
        entry = self._entries(version).get(_safe_path(path))
        if entry is None:
            raise FileNotFoundError(f"No regular file at {path!r} in {version}")
        if entry.size > self.MAX_FILE_BYTES:
            raise ValueError(f"File exceeds {self.MAX_FILE_BYTES} byte read limit: {path}")
        data = self._git("cat-file", "blob", entry.oid, max_bytes=self.MAX_FILE_BYTES)
        if b"\x00" in data:
            raise ValueError(f"Binary file cannot be read as text: {path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"File is not UTF-8 text: {path}") from exc

    def validate_location(self, path: str, line: int) -> bool:
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            return False
        try:
            content = self.read_file(path)
        except (ValueError, FileNotFoundError):
            return False
        if line > len(content.splitlines()):
            return False
        return any(
            changed.path == path
            and any(start <= line <= end for start, end in changed.added_ranges)
            for changed in self.changed_files
        )

    def export(self, version: str, destination: Path) -> Path:
        """Export bounded regular blobs into a new/empty directory outside the repository."""
        entries = self._entries(version)
        target = Path(destination).expanduser().resolve()
        if target == self.repo or target.is_relative_to(self.repo):
            raise ValueError("Export destination must be outside the source repository")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("Export destination must be a new or empty directory")
        if len(entries) > self.MAX_EXPORT_FILES:
            raise ValueError("Too many files to export")
        if any(entry.size > self.MAX_EXPORT_FILE_BYTES for entry in entries.values()):
            raise ValueError("A file exceeds the export size limit")
        if sum(entry.size for entry in entries.values()) > self.MAX_EXPORT_BYTES:
            raise ValueError("Repository exceeds the export byte limit")
        target.mkdir(parents=True, exist_ok=True)
        for path, entry in entries.items():
            output = target.joinpath(*PurePosixPath(_safe_path(path)).parts)
            if not output.resolve().is_relative_to(target):
                raise ValueError("Export path escapes the destination")
            output.parent.mkdir(parents=True, exist_ok=True)
            data = self._git("cat-file", "blob", entry.oid, max_bytes=self.MAX_EXPORT_FILE_BYTES)
            with output.open("xb") as stream:
                stream.write(data)
            output.chmod(0o755 if entry.mode == "100755" else 0o644)
        return target
