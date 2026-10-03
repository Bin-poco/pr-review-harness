"""Keep immutable repository reads distinct from native virtual agent storage."""

import json
from posixpath import normpath

from langchain_core.messages import ToolMessage

FILESYSTEM_PROMPT = """File tools access virtual agent storage only.
Use read_file for /skills/review/.../SKILL.md, /memories/repository.md, scratch
notes and SDK offloaded results. ls/glob/grep also see only this virtual storage.
They cannot list, search or read the Git repository, even when a path looks similar.
For repository files use list_code_files; for repository text use read_code or
search_code. Empty virtual results say nothing about repository file existence.
write_file/edit_file/delete change ephemeral scratch state only. Repository memory
is read-only. Preserve skill and offloaded-result paths exactly as provided.
"""

FILESYSTEM_DESCRIPTIONS = {
    "ls": "List virtual agent storage only (skills, memory, scratch, offloaded results). "
    "To list tracked Git repository files use list_code_files instead. path is a virtual path.",
    "read_file": "Read a virtual skill, memory, scratch or offloaded-result file. "
    "For Git repository code use read_code instead. file_path is a virtual path; "
    "offset and limit select lines. Preserve SDK result paths exactly.",
    "glob": "Match filenames in virtual agent storage only, never the Git repository. "
    "Use list_code_files(directory=...) to discover repository paths. "
    "pattern supports glob syntax; path scopes the virtual search (default '/').",
    "grep": "Search virtual skills, memory, scratch or offloaded results only. "
    "Use search_code(query=...) for literal text in tracked Python repository files, "
    "or list_code_files then read_code for other files. pattern is literal text, not regex; "
    "path and glob scope virtual files. Empty results do not establish repository absence.",
    "write_file": "Create an ephemeral virtual scratch file. This does not edit Git repository "
    "files. /memories is read-only. file_path is a virtual path; content is the full text.",
    "edit_file": "Edit an existing ephemeral virtual scratch file by replacing old_string "
    "with new_string. This does not edit the Git repository. /memories is read-only.",
    "delete": "Delete an ephemeral virtual scratch file. This does not delete Git repository "
    "files. /memories is read-only.",
}

VIRTUAL_READ_TOOLS = {"ls", "read_file", "glob", "grep"}
REPOSITORY_READ_TOOLS = {"list_code_files", "read_code", "search_code"}
VIRTUAL_NOTICE = (
    "Scope: virtual agent storage only. This result does not describe Git repository files. "
    "Use list_code_files for repository paths, read_code for exact code, or search_code "
    "for literal text in Python files.\n\n"
)


def route_virtual_read(request, handler, session):
    """Explain virtual scope and reject missing virtual paths without guessing intent.

    Called inside ReviewFacts so redirects consume tool budget and have replayable
    receipts. Existing scratch/skill/memory/offload paths always keep native behavior.
    """
    name = request.tool_call["name"]
    args = request.tool_call.get("args", {})
    path = args.get("file_path") if name == "read_file" else (args.get("path") or "/")
    paths = {normpath("/" + p.lstrip("/")) for p in request.state.get("files", {})}
    missing = False
    if isinstance(path, str) and name != "glob":
        normalized = normpath("/" + path.lstrip("/"))
        exists = normalized in paths
        if name != "read_file":
            exists |= normalized == "/" or any(
                p.startswith(normalized.rstrip("/") + "/") for p in paths
            )
        missing = not exists
    if missing:
        output = {
            "routing": "redirected",
            "scope": "virtual_agent_storage",
            "error": "Path is absent from virtual agent storage; repository was not queried.",
            "use_tools": ["list_code_files", "read_code", "search_code"],
        }
        result = ToolMessage(
            content=json.dumps(output),
            name=name,
            tool_call_id=request.tool_call["id"],
            status="error",
        )
    else:
        result = handler(request)
        output = {"routing": "virtual", "scope": "virtual_agent_storage"}
        # Native read tools return ToolMessage. File writes and SDK model offloads
        # are outside this hook; their Commands and receipt transactions are intact.
        if name in {"ls", "glob", "grep"} and isinstance(result, ToolMessage):
            content = result.content
            content = (
                VIRTUAL_NOTICE + content
                if isinstance(content, str)
                else [{"type": "text", "text": VIRTUAL_NOTICE}, *content]
            )
            result = result.model_copy(update={"content": content})
    session.trace.append({"tool": name, "arguments": args, "output": output})
    session.tool_calls += 1
    return result


def routing_manifest(trace):
    virtual = [event for event in trace if event["tool"] in VIRTUAL_READ_TOOLS]
    return {
        "schema_version": 1,
        "repository_read_calls": sum(e["tool"] in REPOSITORY_READ_TOOLS for e in trace),
        "virtual_read_calls": len(virtual),
        "redirected_virtual_calls": sum(
            e["output"].get("routing") == "redirected" for e in virtual
        ),
        "repository_tools": sorted(REPOSITORY_READ_TOOLS),
        "virtual_tools": sorted(VIRTUAL_READ_TOOLS),
    }
