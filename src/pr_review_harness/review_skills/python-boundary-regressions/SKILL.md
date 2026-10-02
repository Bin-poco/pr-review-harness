---
name: python-boundary-regressions
description: Check Python pull request changes to arithmetic, ranges, parsing, or exception paths for concrete boundary regressions. Read when a changed line affects a boundary condition.
---

# Boundary regressions

1. Read the changed head lines and the corresponding base implementation. Identify the exact input condition that reaches the changed branch.
2. Check zero, negative, empty, and upper-bound inputs only where the API can actually receive them. Follow surrounding validation and callers before treating an input as possible.
3. Search for affected callers or tests. If a relevant unchanged test exists, run the same check on base and head; distinguish a passing base from a failing head.
4. Report only a newly introduced behavior with a concrete trigger and impact. Cite a changed head line and genuine check evidence IDs. If the evidence is incomplete, lower confidence or submit no finding.
