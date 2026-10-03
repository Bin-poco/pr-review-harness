"""Index immutable source excerpts and their exact request placements, without an LLM.

Saved materials, raw tool receipts and actual post-summary requests are distinct.
References in the working ledger never count as source-code exposure.
"""

import ast
import hashlib
import json
import re
from copy import deepcopy


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ranges(numbers):
    result = []
    for number in sorted(set(numbers)):
        if result and number == result[-1][1] + 1:
            result[-1][1] = number
        else:
            result.append([number, number])
    return result


def _rows(text, kind, complete_chars):
    """Return source lines plus character offsets in the supplied rendering.

    Hunk counters use Git's LF lines, not patterns inside repository source text.
    Character clipping is evaluated using builder metadata, never marker strings.
    """
    offset, raw_line = 0, 0
    base = head = base_left = head_left = None
    rows = []
    for chunk in _lf_chunks(text):
        line = chunk.removesuffix("\n").removesuffix("\r")
        end = offset + len(line)
        complete = offset + len(chunk) <= complete_chars
        entries = []
        prefix = 0
        if kind == "numbered":
            match = re.match(r"^(\d+): ", line)
            if match:
                prefix = match.end()
                entries.append(("head", int(match[1])))
        elif kind == "raw":
            raw_line += 1
            entries.append(("head", raw_line))
        elif kind == "diff":
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match and complete:
                base, head = int(match[1]), int(match[3])
                base_left = int(match[2]) if match[2] is not None else 1
                head_left = int(match[4]) if match[4] is not None else 1
            elif base is not None and line[:1] in {" ", "+", "-"}:
                prefix = 1
                if line[0] in {" ", "-"} and base_left:
                    entries.append(("base", base))
                    base += 1
                    base_left -= 1
                if line[0] in {" ", "+"} and head_left:
                    entries.append(("head", head))
                    head += 1
                    head_left -= 1
            elif line.startswith("diff --git "):
                base = head = None
        for version, number in entries:
            if number > 0 and (complete or len(line) > prefix):
                rows.append(
                    {
                        "version": version,
                        "line": number,
                        "start": offset + prefix,
                        "end": end,
                        "text": line[prefix:],
                        "complete": complete,
                    }
                )
        offset += len(chunk)
    return rows


def _lf_chunks(text):
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


class FragmentIndex:
    """A bounded, deterministic projection of frozen context and committed receipts."""

    MAX_FRAGMENTS = 512
    MAX_BYTES = 1_000_000
    MAX_ORIGINS = 32
    MAX_SYMBOL_FILES = 80
    MAX_SYMBOL_BYTES = 2_000_000
    MAX_SYMBOL_FILE_BYTES = 64_000

    def __init__(self, snapshot, session, context):
        self.snapshot, self.session, self.context = snapshot, session, context
        self._reset()

    def _reset(self):
        self.records = {}
        self.placements = []
        self.recent = []
        self.omitted = self.omitted_origins = self.unindexed = self.unsupported = self.bytes = 0
        self.symbol_cache = {}
        self.symbol_bytes = 0
        self.cursor = 0
        changes = {item.path: item for item in self.snapshot.changed_files}
        for index, item in enumerate(self.context.items):
            if item.kind == "legacy" or item.content_start is None:
                self.unindexed += 1
                continue
            rows = _rows(item.content[: item.source_chars], item.kind, item.complete_chars)
            old_path = changes[item.path].old_path if item.path in changes else None
            self._add(
                item.path,
                rows,
                {"source": "initial", "item": index, "reason": item.reason},
                old_path=old_path,
            )

    def sync(self):
        if len(self.session.trace) < self.cursor:
            self._reset()
        for index in range(self.cursor, len(self.session.trace)):
            event = self.session.trace[index]
            output = event["output"]
            if output.get("error"):
                continue
            if event["tool"] == "read_code" and output.get("content"):
                version = output.get("version", "head")
                if version not in {"head", "base"}:
                    continue
                rows = _rows(output["content"], "numbered", len(output["content"]))
                returned = output.get("returned_range")
                for row in rows:
                    row["version"] = version
                    row["complete"] = bool(returned and returned[0] <= row["line"] <= returned[1])
                self._add(output["path"], rows, {"source": "read_code", "event": index})
            elif event["tool"] == "search_code":
                for number, match in enumerate(output.get("matches", [])):
                    row = {
                        "version": "head",
                        "line": match["line"],
                        "start": 0,
                        "end": len(match["text"]),
                        "text": match["text"],
                        "complete": match.get("text_truncated") is False,
                    }
                    self._add(
                        match["path"],
                        [row],
                        {"source": "search_code", "event": index, "match": number},
                    )
        self.cursor = len(self.session.trace)

    def _symbols(self, path, version):
        key = (path, version)
        if key in self.symbol_cache:
            return self.symbol_cache[key]
        status, symbols = "not_python", []
        if path.endswith(".py"):
            status = "analysis_limit"
            try:
                size = self.snapshot.file_size(path, version)
                if (
                    size <= self.MAX_SYMBOL_FILE_BYTES
                    and self.symbol_bytes + size <= self.MAX_SYMBOL_BYTES
                    and len(self.symbol_cache) < self.MAX_SYMBOL_FILES
                ):
                    self.symbol_bytes += size
                    source = self.snapshot.read_file(path, version)
                    if re.search(r"\r(?!\n)", source):
                        raise ValueError("Bare CR gives Python and Git different line locations")
                    tree = ast.parse(source)
                    stack = []

                    class Names(ast.NodeVisitor):
                        def visit_FunctionDef(self, node):
                            stack.append(node.name)
                            symbols.append(
                                {
                                    "name": ".".join(stack),
                                    "line_range": [
                                        min(
                                            [node.lineno, *[d.lineno for d in node.decorator_list]]
                                        ),
                                        node.end_lineno or node.lineno,
                                    ],
                                }
                            )
                            self.generic_visit(node)
                            stack.pop()

                        visit_AsyncFunctionDef = visit_FunctionDef
                        visit_ClassDef = visit_FunctionDef

                    Names().visit(tree)
                    status = "static_python_ast"
            except (ValueError, FileNotFoundError, SyntaxError, RecursionError):
                status, symbols = "unavailable", []
        self.symbol_cache[key] = (status, symbols)
        return status, symbols

    def _add(self, path, rows, origin, *, old_path=None):
        for version in ("base", "head"):
            groups = []
            for row in (row for row in rows if row["version"] == version):
                if (
                    groups
                    and row["complete"]
                    and groups[-1][-1]["complete"]
                    and row["line"] == groups[-1][-1]["line"] + 1
                ):
                    groups[-1].append(row)
                else:
                    groups.append([row])
            location = (old_path or path) if version == "base" else path
            if not groups:
                continue
            try:
                self.snapshot.file_size(location, version)
            except (ValueError, FileNotFoundError):
                # Symlink targets and submodule hashes in diffs are metadata,
                # not regular repository code that read_code could retrieve.
                self.unsupported += len(groups)
                continue
            for group in groups:
                sha = self.snapshot.merge_base_sha if version == "base" else self.snapshot.head_sha
                identity = {
                    "repo_id": self.snapshot.repo_id,
                    "path": location,
                    "version": version,
                    "sha": sha,
                    "line_range": [group[0]["line"], group[-1]["line"]],
                    "complete": all(row["complete"] for row in group),
                    "text_sha256": hashlib.sha256(
                        "\n".join(row["text"] for row in group).encode()
                    ).hexdigest(),
                }
                fid = _digest(identity)
                placement = {
                    **origin,
                    "display_range": [group[0]["start"], group[-1]["end"]],
                }
                previous = self.records.get(fid)
                if previous and placement in previous["origins"]:
                    continue
                if previous and len(previous["origins"]) >= self.MAX_ORIGINS:
                    self.omitted_origins += 1
                    continue
                if previous:
                    record = {**previous, "origins": [*previous["origins"], placement]}
                else:
                    status, symbols = self._symbols(location, version)
                    overlapping = [
                        s
                        for s in symbols
                        if s["line_range"][0] <= identity["line_range"][1]
                        and s["line_range"][1] >= identity["line_range"][0]
                    ]
                    record = {
                        "id": fid,
                        **identity,
                        "symbol_status": status,
                        "symbols": overlapping[:8],
                        "omitted_symbols": max(0, len(overlapping) - 8),
                        "origins": [placement],
                    }
                old_bytes = len(json.dumps(previous, ensure_ascii=True).encode()) if previous else 0
                size = len(json.dumps(record, ensure_ascii=True).encode())
                if (not previous and len(self.records) >= self.MAX_FRAGMENTS) or (
                    self.bytes + size - old_bytes > self.MAX_BYTES
                ):
                    self.omitted += 1
                    continue
                self.bytes += size - old_bytes
                self.records[fid] = record
                self.placements.append({"id": fid, "origin": origin, "rows": group})
                if fid in self.recent:
                    self.recent.remove(fid)
                self.recent.append(fid)

    def visible(self, messages, initial_display):
        """Count literal source only in the actual request after native compaction."""
        self.sync()
        initial_chars = 0
        receipts = set()
        for message in messages:
            if message.type == "human" and message.content == initial_display:
                initial_chars = max(initial_chars, len(initial_display))
            if message.type != "tool" or message.name not in {"read_code", "search_code"}:
                continue
            # Offloaded pointers, summaries and arbitrary copied prose are not receipts.
            content = message.content
            if (
                isinstance(content, list)
                and len(content) == 1
                and isinstance(content[0], dict)
                and content[0].get("type") == "text"
            ):
                content = content[0].get("text")
            if isinstance(content, str):
                try:
                    receipts.add((message.name, _digest(json.loads(content))))
                except (ValueError, TypeError):
                    pass
        visible = {}
        for placement in self.placements:
            origin = placement["origin"]
            if origin["source"] == "initial":
                item = self.context.items[origin["item"]]
                limit = initial_chars - item.content_start
            else:
                event = self.session.trace[origin["event"]]
                if (event["tool"], _digest(event["output"])) not in receipts:
                    continue
                limit = max(row["end"] for row in placement["rows"])
            for row in placement["rows"]:
                full = row["complete"] and row["end"] <= limit
                partial = row["start"] < limit and not full
                if full or partial:
                    value = visible.setdefault(placement["id"], {"full": set(), "partial": set()})
                    value["full" if full else "partial"].add(row["line"])
        return [
            {
                "id": fid,
                "complete_ranges": _ranges(value["full"]),
                "partial_ranges": _ranges(value["partial"] - value["full"]),
            }
            for fid, value in visible.items()
        ]

    def references(self):
        self.sync()
        return [
            {
                key: self.records[fid][key]
                for key in ("id", "path", "version", "line_range", "complete")
            }
            for fid in self.recent[-8:]
        ]

    def lookup(self, path, version, start_line, end_line, *, symbol=None):
        """Locate archived excerpts; availability does not mean current prompt coverage."""
        if version not in {"head", "base"} or not 1 <= start_line <= end_line:
            raise ValueError("Use head/base and a positive ascending line range")
        self.sync()
        matches = [
            record
            for record in self.records.values()
            if record["path"] == path
            and record["version"] == version
            and record["line_range"][0] <= end_line
            and record["line_range"][1] >= start_line
            and (symbol is None or any(s["name"] == symbol for s in record["symbols"]))
        ]
        return deepcopy(matches)

    def manifest(self):
        self.sync()
        value = {
            "schema_version": 1,
            "repo_id": self.snapshot.repo_id,
            "head_sha": self.snapshot.head_sha,
            "merge_base_sha": self.snapshot.merge_base_sha,
            "records": list(self.records.values()),
            "record_bytes": self.bytes,
            "max_records": self.MAX_FRAGMENTS,
            "max_record_bytes": self.MAX_BYTES,
            "max_origins_per_fragment": self.MAX_ORIGINS,
            "omitted_fragments": self.omitted,
            "omitted_origins": self.omitted_origins,
            "unindexed_legacy_materials": self.unindexed,
            "unsupported_source_fragments": self.unsupported,
            "symbol_analysis": {
                "method": "static Python AST association; no whole-program call graph",
                "max_files": self.MAX_SYMBOL_FILES,
                "max_file_bytes": self.MAX_SYMBOL_FILE_BYTES,
                "max_total_bytes": self.MAX_SYMBOL_BYTES,
                "scanned_bytes": self.symbol_bytes,
            },
            "coverage": "archived excerpts only; per-request visibility is recorded separately",
            "line_basis": "Git LF lines; excerpt digests normalize CRLF to LF",
        }
        return deepcopy({**value, "sha256": _digest(value)})
