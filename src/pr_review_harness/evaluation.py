"""Score saved reviews against private, version-pinned human labels.

The score is deliberately a location-match proxy. A person must still decide
whether the proposed trigger and root cause actually describe the same defect.
"""

import json
import re
from pathlib import Path


def load_gold(path: str | Path) -> dict:
    """Load a private JSON label file for use after the review has finished."""
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid gold JSON at {source}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("Gold labels must be a JSON object")
    return value


def evaluate_report(report: dict, gold: dict) -> dict:
    """Compare a completed review with labels tied to the same immutable PR.

    One report finding may match one gold label by changed head path and line.
    A second finding for that label is counted as a duplicate. Invalid findings
    are false positives and cannot match a label. Undefined ratios are ``None``.
    """
    if not isinstance(report, dict) or not isinstance(gold, dict):
        raise ValueError("Report and gold labels must be objects")
    if gold.get("schema_version") != 1 or report.get("schema_version") != 1:
        raise ValueError("Only report and gold schema_version 1 are supported")
    for field in ("repo_id", "head_sha", "merge_base_sha"):
        value = gold.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Gold {field} must be a nonempty string")
        if report.get(field) != value:
            raise ValueError(f"Report and gold {field} differ; refusing to score stale labels")

    changed = _changed_lines(gold.get("changed_lines"))
    if "changed_lines" in report and changed != _changed_lines(report["changed_lines"]):
        raise ValueError("Gold changed_lines differ from the immutable review report")
    labels = _labels(gold.get("findings"), changed)
    findings = report.get("findings")
    evidence = report.get("evidence")
    if not isinstance(findings, list) or not isinstance(evidence, list):
        raise ValueError("Report findings and evidence must be lists")
    evidence_issues, diagnostics = _evidence_index(evidence, report)

    matches: list[dict] = []
    false_positives: list[dict] = []
    missed_labels: list[dict] = []
    duplicates: list[dict] = []
    invalid_findings: list[dict] = []
    matched_label: dict[str, int] = {}
    seen_claim: dict[tuple[str, int, str], int] = {}

    for index, finding in enumerate(findings):
        reasons = _finding_issues(finding, changed, evidence_issues)
        if reasons:
            invalid_findings.extend(
                {"report_index": index, "code": code, "message": message}
                for code, message in reasons
            )
            false_positives.append(_false_positive(index, finding, "invalid finding"))
            continue

        path, line = finding["path"], finding["line"]
        title = finding.get("title", "")
        candidates = [
            label
            for label in labels
            if label["path"] == path and label["start_line"] <= line <= label["end_line"]
        ]
        # _labels rejects ambiguous spans on any changed line, so at most one can match.
        label = candidates[0] if candidates else None
        claim_key = (path, line, _normalize_title(title))
        if label is not None and label["id"] in matched_label:
            duplicates.append(
                {
                    "report_index": index,
                    "of_report_index": matched_label[label["id"]],
                    "label_id": label["id"],
                }
            )
        elif label is None and claim_key in seen_claim:
            duplicates.append(
                {"report_index": index, "of_report_index": seen_claim[claim_key], "label_id": None}
            )
        elif label is None:
            false_positives.append(_false_positive(index, finding, "no matching gold location"))
        else:
            matched_label[label["id"]] = index
            matches.append({"report_index": index, "label_id": label["id"]})
        seen_claim.setdefault(claim_key, index)

    for label in labels:
        if label["id"] not in matched_label:
            missed_labels.append(label)
    tp, fp, fn = len(matches), len(false_positives), len(missed_labels)
    mode = report.get("mode")
    return {
        "schema_version": 1,
        "metric_scope": "changed_head_location_proxy",
        "requires_human_review": True,
        "run_mode": mode,
        "is_live_model_run": mode == "live",
        "eligible_for_model_quality": False,
        "repo_id": gold["repo_id"],
        "head_sha": gold["head_sha"],
        "merge_base_sha": gold["merge_base_sha"],
        "counts": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "duplicate": len(duplicates),
            "raw_findings": len(findings),
            "scored_findings": tp + fp,
        },
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "matches": matches,
        "false_positives": false_positives,
        "missed_labels": missed_labels,
        "duplicates": duplicates,
        "invalid_findings": invalid_findings,
        "diagnostics": diagnostics,
    }


def _changed_lines(value: object) -> dict[str, tuple[tuple[int, int], ...]]:
    if not isinstance(value, dict):
        raise ValueError("Gold changed_lines must map paths to [start, end] ranges")
    changed = {}
    for path, raw_ranges in value.items():
        if not isinstance(path, str) or not path or not isinstance(raw_ranges, list):
            raise ValueError("Gold changed_lines entries need a path and range list")
        ranges = []
        for pair in raw_ranges:
            if not isinstance(pair, list) or len(pair) != 2 or not _valid_span(*pair):
                raise ValueError(f"Invalid changed line range for {path!r}: {pair!r}")
            ranges.append((pair[0], pair[1]))
        changed[path] = tuple(ranges)
    return changed


def _labels(value: object, changed: dict) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Gold findings must be a list")
    labels = []
    ids = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each gold finding must be an object")
        label_id, path = item.get("id"), item.get("path")
        start, end = item.get("start_line"), item.get("end_line")
        if not isinstance(label_id, str) or not label_id or label_id in ids:
            raise ValueError("Gold finding IDs must be unique nonempty strings")
        if not isinstance(path, str) or not path or not _valid_span(start, end):
            raise ValueError(f"Gold finding {label_id!r} needs a path and valid line span")
        if not any(start <= hi and lo <= end for lo, hi in changed.get(path, ())):
            raise ValueError(f"Gold finding {label_id!r} has no changed head line in its span")
        ids.add(label_id)
        labels.append(item)
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            if left["path"] != right["path"]:
                continue
            for lo, hi in changed[left["path"]]:
                shared_start = max(lo, left["start_line"], right["start_line"])
                shared_end = min(hi, left["end_line"], right["end_line"])
                if shared_start <= shared_end:
                    raise ValueError(
                        f"Gold labels {left['id']!r} and {right['id']!r} overlap on changed lines"
                    )
    return labels


def _valid_span(start: object, end: object) -> bool:
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 1 <= start <= end
    )


def _evidence_index(evidence: list, report: dict) -> tuple[dict[str, str | None], list[dict]]:
    issues: dict[str, str | None] = {}
    diagnostics = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            diagnostics.append({"evidence_index": index, "code": "invalid_evidence_record"})
            continue
        evidence_id = item["id"]
        if evidence_id in issues:
            issues[evidence_id] = "duplicate evidence ID"
            diagnostics.append(
                {"evidence_index": index, "code": "duplicate_evidence_id", "id": evidence_id}
            )
            continue
        base, head = item.get("base"), item.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            issue = "missing base/head check versions"
        elif base.get("sha") != report["merge_base_sha"] or head.get("sha") != report["head_sha"]:
            issue = "check evidence is pinned to a different commit"
        else:
            issue = None
        issues[evidence_id] = issue
        if issue:
            diagnostics.append(
                {"evidence_index": index, "code": "stale_evidence", "id": evidence_id}
            )
    return issues, diagnostics


def _finding_issues(finding: object, changed: dict, evidence_issues: dict) -> list[tuple[str, str]]:
    if not isinstance(finding, dict):
        return [("malformed_finding", "Finding is not an object")]
    path, line = finding.get("path"), finding.get("line")
    issues = []
    if not isinstance(path, str) or not path or not isinstance(line, int) or isinstance(line, bool):
        issues.append(("invalid_location", "Finding path or line is invalid"))
    elif not any(lo <= line <= hi for lo, hi in changed.get(path, ())):
        issues.append(("unchanged_head_line", "Finding is not on a changed head line"))
    refs = finding.get("evidence_ids")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        issues.append(("invalid_evidence_ids", "evidence_ids must be a list of IDs"))
    else:
        if len(refs) != len(set(refs)):
            issues.append(("duplicate_evidence_reference", "Finding repeats an evidence ID"))
        for ref in refs:
            if ref not in evidence_issues:
                issues.append(("unknown_evidence_id", f"Evidence {ref!r} is absent from report"))
            elif evidence_issues[ref]:
                issues.append(("invalid_evidence_id", f"Evidence {ref!r}: {evidence_issues[ref]}"))
    return issues


def _false_positive(index: int, finding: object, reason: str) -> dict:
    if not isinstance(finding, dict):
        return {"report_index": index, "reason": reason, "path": None, "line": None, "title": None}
    return {
        "report_index": index,
        "reason": reason,
        "path": finding.get("path"),
        "line": finding.get("line"),
        "title": finding.get("title"),
    }


def _normalize_title(title: object) -> str:
    return re.sub(r"\W+", "", title.casefold()) if isinstance(title, str) else ""
