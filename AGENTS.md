# Alfred — Maestaris work protocol

This project follows [Maestaris](https://github.com/Siddharthgolecha/Maestaris):
**GitHub Issues are the durable queue, coding chats do actual engineering work, and the orchestrator reviews and merges.**

## Source of truth
1. Current `main` source and tests, then current target branch.
2. Each issue, its dependency references, claim/result comments.
3. PR diff, CI and commit history.
4. `coordination/maestaris.yaml`, these instructions.
5. Historical conversations.

## Worker A (implementation; at most one claimed issue at a time)
1. Inspect open issues labeled `maestaris:task`; prefer P0 to P1 to P2, only when dependencies are actually merged into `main`.
2. Resume an existing claim for A first. Check last claim from any worker: claim valid for **four hours** unless genuinely abandoned. Do not preempt a live claim.
3. Before doing work, comment on the ISSUE (not only the PR):
   ```
   [MAESTARIS CLAIM]
   worker: A
   claimed_at: YYYY-MM-DDTHH:MM:SSZ
   ```
4. Create/reuse `maestaris/<issue>-<slug>` **from latest main**. Work on that one issue, with focused tests and documentation. No unrelated branch merges; do not touch `intrinsic_jetson` directly for generic Phase 5 changes.
5. Open/update a normal PR targeting `main` with `Resolves #N` and implementation, safety, tests, resource/performance evidence. Do not merge your own PR; the reviewer is separate.
6. Comment on issue:
   ```
   [MAESTARIS RESULT]
   worker: A
   status: READY_FOR_REVIEW
   commit: <full sha>
   pr: <number>
   tests: <command and actual result>
   summary: <specific changes>
   ```
   For a genuine blocker use status: BLOCKED with a concrete dependency or tool restriction.
7. If unable to use a required GitHub connector/action, report the failure; **never invent a successful push or PR**. Next run resumes work.

## Reviewer / orchestrator (separate scheduled task)
- Search issues with READY_FOR_REVIEW and associated open PRs. Verify branch and issue linkage, actual code diff, dependency merge status, check runs **for the current PR head SHA**, policy tests, Python 3.10/3.12, and regressions from baseline.
- For sensitive changes (DeepSeek Harness, model plugins, shell/network access, approvals, persistence), check all negative cases and offline-only behavior. No unchecked automation permission escalation.
- If evidence is incomplete or changes are unsafe, comment actionable requests and leave PR open; never merge due solely to labels or worker assertions.
- If all acceptance criteria and current CI pass and PR mergeability is confirmed, merge the reviewed head into `main` via GitHub with unchanged head SHA, close resolved issue and report results.
- Only the reviewer/orchestrator may merge completed Phase 5 PRs. Do not merge `intrinsic_jetson` itself into `main`.
- Avoid duplicate worker/merge action in the same issue. Do not change or disable unrelated tasks.

## CI & synchronization
New main commits trigger a dedicated safe, tested merge into `intrinsic_jetson` (never force push). Conflicts or failed tests leave the Jetson branch unchanged and create a blocking issue. The worker should not take an unresolved sync failure as permission to rewrite branch history.

## Scheduled chats
Scheduled invocations are optional **convenience**, not part of Maestaris correctness. They may lack repository write tools; in that case report the actual restriction instead of creating a relay/external executor. GitHub retains durable state across invocations.
