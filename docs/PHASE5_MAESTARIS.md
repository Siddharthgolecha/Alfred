# Phase 5 — Maestaris issue queue and branch synchronization

Phase 5 epic: [#11](https://github.com/Siddharthgolecha/Alfred/issues/11). Detailed GitHub worker issues: [#12–#18](https://github.com/Siddharthgolecha/Alfred/issues/12). Independent safety CI task: [#19](https://github.com/Siddharthgolecha/Alfred/issues/19).

## Actual Maestaris protocol
This is based on [Maestaris](https://github.com/Siddharthgolecha/Maestaris), not a new orchestration platform. A Maestaris task is a GitHub Issue labeled `maestaris:task` with `priority:P0/P1/P2`. Worker A claims an unblocked issue with:

```text
[MAESTARIS CLAIM]
worker: A
claimed_at: 2026-10-14T16:30:00Z
```

Claims expire after four hours unless refreshed. The coding worker makes `maestaris/<issue>-<slug>`, a focused PR and tests, then posts:

```text
[MAESTARIS RESULT]
worker: A
status: READY_FOR_REVIEW
commit: <full SHA>
pr: <number>
tests: <commands and concrete results>
summary: <changes made>
```

The independent reviewer/orchestrator verifies **actual code + the current PR head's CI**, dependencies, policy requirements and issue acceptance criteria. Only the reviewer merges acceptable PRs into main. Incomplete work gets concrete revision feedback, not a merge. Claim/result comments, PRs and GitHub checks are the durable state; no custom dispatcher, daemon or duplicate state machine is needed.

Scheduled tasks invoke each role hourly (the platform's supported maximum is one invocation per task per hour). Scheduled runs are opportunistic: if GitHub write tools are unavailable, the role reports its limitations and does not claim to have pushed or merged changes. Interactive worker/reviewer chat remains a valid fallback.

## Dependency chain

| Issue | Goal | Depends on |
|---|---|---|
| #12 | Task/session/event contracts and storage | — |
| #13 | Pinned optional DeepSeek Harness bridge | #12 |
| #14 | Bounded contextual Needle tool loop | #12, #13 |
| #15 | Needle→Qwen same-task handoff | #12, #13, #14 |
| #16 | Session memory and retention | #12, #14 |
| #17 | Unified skills/meeting adapter | #15, #16 |
| #18 | Performance/security evaluation and rollout | #13–#17 |
| #19 | CI main→intrinsic_jetson safe branch sync | Independent |

The next ready coding task after CI #19 is #12. Never start work on blocked issues simply because they have a high priority.

## Safe sync from main to intrinsic_jetson

[Workflow](../.github/workflows/sync-main-to-intrinsic-jetson.yml) triggers on **every push to main**, including merged PRs, supports manual `workflow_dispatch`, and has an hourly idempotent scheduled catch-up (minute 17) in case an API-created merge does not dispatch a push event.

1. Checkout `intrinsic_jetson` at its exact tip with complete history.
2. Fetch both branches. If main is already an ancestor, report no-op.
3. Otherwise use an ordinary `git merge --no-ff origin/main` (not rebase, cherry-pick, or destructive reset). Conflicts are aborted with remote untouched.
4. Run syntax + complete portable Python unit tests on the **actual merged worktree** using Python 3.10 and 3.12, installing PDF/QR test dependencies.
5. Push the merged commit only after **both** test environments pass. A remote branch advance, branch-protection refusal, or rejected push fails; there is never a force push.
6. On failure, create a new `[sync]` blocking GitHub Issue containing source SHA and GitHub Actions run URL; retain the target branch intact. Fix via a reviewed PR, then rerun manually.

The merge script is `scripts/sync_main_into_jetson.sh`. Tests in `tests/test_branch_sync.py` cover clean divergent branches, idempotent no-ops, conflicts, wrong checkout, and concurrent remote target advancement.

### Important limitations
- This workflow requires repository GitHub Actions permissions to **read/write contents and create issues**. Branch rulesets may reject pushes by `GITHUB_TOKEN`; if so, grant an appropriate permitted actor or switch to a review-and-merge PR strategy rather than force-bypass protection.
- GitHub generally suppresses additional workflow-triggering events created by the default `GITHUB_TOKEN`. That is why **the sync workflow tests before pushing**, rather than relying on a push to the Jetson branch to start CI.
- When conflict or tests fail, intrinsic_jetson is **not** falsely considered synchronized; the issue contains the run URL and keeps the blocker visible.
- Unit tests are not proof that Cactus Needle/Whistle, CUDA, real USB hub/Bluetooth microphones or swap behave correctly on the physical 8GB Jetson. Relevant hardware changes still require on-device acceptance.

## Local commands

```bash
python -m unittest discover -s tests -v
git switch intrinsic_jetson
bash scripts/sync_main_into_jetson.sh
```

The script performs only a local merge. It **does not push by itself**. Review the merge and tests before manually pushing if you invoke it outside CI.

This protocol intentionally retains the original Maestaris design principle: spend engineering effort on Alfred, not on a complex orchestration subsystem.
