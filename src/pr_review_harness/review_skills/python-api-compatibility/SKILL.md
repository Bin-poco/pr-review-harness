---
name: python-api-compatibility
description: Check Python pull request changes to callable signatures, return shapes, exceptions, or serialized fields for broken repository callers. Read when a changed public interface may affect callers.
---

# API compatibility

1. Compare the callable or data contract at the merge base and head. Note required arguments, defaults, return types, raised errors, and serialized field names.
2. Search actual repository callers and tests. Trace one realistic call through the changed behavior, including its handling of missing values and exceptions.
3. Confirm that the behavior changed in this PR and that the caller is reachable. Use an existing check on both versions when useful; a check result alone does not prove the proposed impact.
4. Report a concrete failure at a changed head line with the triggering call and impact. If callers already adapt or evidence is insufficient, submit no finding.
