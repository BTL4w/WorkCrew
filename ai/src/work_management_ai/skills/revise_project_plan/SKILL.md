---
name: revise-project-plan
description: Use when an authorized Manager or Admin requests changes to one exact immutable Planning proposal version.
---

# Revise Project Plan

## Procedure

1. Require workflow, proposal, exact base version and Manager instruction for a pending proposal revision. A Phase 4 risk revision instead requires the hub-bound assessment ID, exact fingerprint, observation IDs and affected weeks; the backend resolves the current project plan under the active Manager.
2. Generate against only that immutable base and permission-filtered context.
3. Preserve unique stable Task refs. For an existing-project weekly revision, preserve actual assignments and completed work; propose only scheduling/effort changes and unassigned new tasks in existing open weeks. Do not create another project or rewrite the original baseline.
4. Clear assignees on newly introduced Tasks and run deterministic verification.
5. Return the draft through `planning.manage_run@1` for backend freshness checks and append.
6. Stop at Manager review; never persist directly, approve, or apply business rows.

Duplicate or missing refs, stale versions, invalid structure, or verifier failures use the manual-editable fallback.
