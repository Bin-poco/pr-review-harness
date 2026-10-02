# First frozen public-PR pilot (2026-10-02)

The exact case labels, reproductions, implementation, and run settings are recorded in
[`FREEZE.json`](FREEZE.json). The unchanged freeze was verified again after the model
run. This is a public, development-stage pilot. It is not an independent held-out
evaluation, and the location matcher is not a substitute for root-cause review.

## Setup

- 18 pinned PR cases: 8 known-regression commits, their 8 later fixes, and 2
  independent feature-addition controls with no known target defect.
- All 36 base/head reproduction checks behaved as specified before the model run.
- DeepSeek official API, `deepseek-flash`, temperature 0, thinking disabled.
- One `baseline` and one `working` run per case, with 12 model calls, 24 tool calls,
  and a 40,000-character read limit per run. No repository test execution by the
  agent. There was no feedback memory in this comparison.
- The full output is in
  [`outputs/independent-real-prs/156607525e554ebe957009a0fb145a5a/benchmark.json`](../../outputs/independent-real-prs/156607525e554ebe957009a0fb145a5a/benchmark.json).

## Observed result

| Measure | Baseline | Working context |
| --- | ---: | ---: |
| Completed / attempted | 17 / 18 | 16 / 18 |
| Failed to submit a valid review | 1 | 2 |
| Findings in completed runs | 13 | 10 |
| Location matches to known regression labels | 2 | 2 |
| Known regression labels missed in completed runs | 6 | 4 |
| Findings outside known label locations | 11 | 8 |
| Findings on the two feature controls | 0 | 0 |

The aggregate rows contain different completed cases. On the **15 cases completed
by both variants**, each variant matched 2 of the 6 known regression locations
and missed 4. Baseline made 11 findings, 9 outside known label locations; working
context made 9 findings, 7 outside known label locations. Both made no findings
on either control. This does not establish an improvement from working context.

Three runs ended without `submit_review`: working context on the Werkzeug
multipart-CRLF and empty-host regression cases, and baseline on the Click
relative-symlink fix. Their checkpoint and partial trace were preserved. They
are failures, not empty reviews, and are excluded from location counts.
Checkpoint inspection gives a more specific cause: both Werkzeug runs ended
with long plain-text model answers and provider `finish_reason=length`, with
no tool call. The Click run returned a `submit_review` tool call whose arguments
failed JSON parsing, so the harness received no valid tool call. These are
model-output/structured-submission failures; they were not API transport errors.

The 23 model findings are queued in
[`human-review.json`](../../outputs/independent-real-prs/156607525e554ebe957009a0fb145a5a/human-review.json)
with blank root-cause and false-positive judgments. A finding outside a known
label location can be a real unrelated issue, an incorrect claim, or a duplicate
explanation at another location. The location matcher alone cannot decide.

The completed runs reported 5,004,113 input tokens and 79,294 output tokens
across 350 model attempts and 547 tool attempts. These are provider-reported
usage totals for completed runs only; no currency estimate is recorded.

## What to do next

1. Make the harness report `finish_reason`, invalid tool-call parsing, and the
   absence of `submit_review` directly. Add a bounded, recorded repair attempt
   for malformed final output; count a run as failed if repair also fails.
   Preserve the three original failures in this batch.
2. Adjudicate all 23 findings against the pinned code and issue history, recording
   root-cause matches and false positives explicitly. Keep assistant triage
   separate from independent human judgment.
3. Use those observations to change one harness component at a time, then create
   a new frozen revision and rerun on a separately chosen set of PRs. Do not
   tune against this public pilot and present the same cases as held-out evidence.

The project tests passed (114 tests) and Ruff passed before this model run.
