# Maestaris workflow

Alfred is developed with [Maestaris](https://github.com/Siddharthgolecha/Maestaris). GitHub
Issues are the durable queue, coding chats do the engineering, and a separate reviewer merges.
No custom dispatcher or state machine exists. The rules live in
[`AGENTS.md`](https://github.com/Siddharthgolecha/Alfred/blob/main/AGENTS.md) and [`coordination/maestaris.yaml`](../../coordination/maestaris.yaml).
The scheduled worker and reviewer prompts are
[`prompts/maestaris-worker.md`](https://github.com/Siddharthgolecha/Alfred/blob/main/prompts/maestaris-worker.md) and
[`prompts/maestaris-reviewer.md`](https://github.com/Siddharthgolecha/Alfred/blob/main/prompts/maestaris-reviewer.md).

## Roles

| Role | Does | Must not |
|---|---|---|
| **Worker A** (implementation) | Claims one unblocked issue, implements it on a branch, opens a PR, posts a result. | Merge its own PR. Work on more than one claimed issue at a time. |
| **Reviewer / orchestrator** | Reviews the actual diff and CI, requests changes, merges acceptable PRs into `main`. | Merge because of a label or a worker's assertion. |

Only the reviewer merges Phase 5 PRs. `intrinsic_jetson` is never merged into `main`, and
generic Phase 5 changes never touch `intrinsic_jetson` directly.

## Worker loop

```{mermaid}
flowchart TD
    pick["Pick an open issue labelled<br/>maestaris:task<br/>priority P0, then P1, then P2"] --> dep{"Dependencies<br/>merged into main?"}
    dep -- no --> pick
    dep -- yes --> claim["Comment on the ISSUE:<br/>[MAESTARIS CLAIM]"]
    claim --> branch["Branch maestaris/ISSUE-slug<br/>from latest main"]
    branch --> work["Implement, test, document"]
    work --> pr["Open PR targeting main<br/>with 'Resolves #N'"]
    pr --> result["Comment on the issue:<br/>[MAESTARIS RESULT]"]
    result --> review["Reviewer checks the PR head SHA"]
    review -- "changes needed" --> work
    review -- "all criteria and CI pass" --> merge["Reviewer merges and closes the issue"]
```

1. **Pick.** Choose among open `maestaris:task` issues, preferring P0 over P1 over P2, only when
   every dependency is already merged into `main`. Resume your existing claim first.
2. **Respect live claims.** A claim is valid for **four hours** unless genuinely abandoned. Do
   not preempt one.
3. **Claim** with a comment on the issue, not only the PR:

   ```text
   [MAESTARIS CLAIM]
   worker: A
   claimed_at: 2026-10-14T16:30:00Z
   ```

4. **Branch** `maestaris/<issue>-<slug>` from the latest `main`. Do one issue, with focused tests
   and documentation. Do not merge unrelated branches.
5. **Open a PR** to `main` that says `Resolves #N` and includes implementation, safety, test and
   resource or performance evidence.
6. **Report** on the issue:

   ```text
   [MAESTARIS RESULT]
   worker: A
   status: READY_FOR_REVIEW
   commit: <full sha>
   pr: <number>
   tests: <command and actual result>
   summary: <specific changes>
   ```

   Use `status: BLOCKED` with a concrete dependency or tool restriction for a genuine blocker.
7. **Never invent success.** If a GitHub action is unavailable, report the failure. Do not claim
   a push or PR that did not happen. The next run resumes.

## Reviewer checklist

For each issue marked `READY_FOR_REVIEW` and its open PR, verify:

- the branch and the issue link,
- the actual code diff,
- that dependencies are merged,
- check runs **for the current PR head SHA**, the policy tests, Python 3.10 and 3.12, and
  regressions from baseline,
- the issue's acceptance criteria.

For sensitive changes (Harness, model plugins, shell or network access, approvals, persistence),
check every negative case and offline-only behaviour. No automation may gain permission
unchecked. `coordination/maestaris.yaml` lists the required review evidence: actual PR diff,
current head SHA and CI, dependency merges, acceptance criteria, no permission escalation, no
unsafe replay, offline operation, and test and hardware evidence.

If evidence is incomplete or the change is unsafe, comment with actionable requests and leave
the PR open. If everything passes and the PR is mergeable, merge the reviewed head into `main`
with an unchanged head SHA, close the issue and report.

## Sync from `main` to `intrinsic_jetson`

Every new `main` commit is merged, without force, into `intrinsic_jetson`. The workflow is
[`sync-main-to-intrinsic-jetson.yml`](../../.github/workflows/sync-main-to-intrinsic-jetson.yml).

**Triggers.** A push to `main`, a manual `workflow_dispatch`, and an hourly catch-up at minute 17
in case an API-created merge does not dispatch a push event. All runs are idempotent. A
concurrency group serializes them.

```{mermaid}
flowchart TD
    t["Trigger"] --> guard{"ref is main?"}
    guard -- no --> stop["Fail closed"]
    guard -- yes --> helper["Copy the sync script from main<br/>to the runner temp dir"]
    helper --> co["Check out intrinsic_jetson<br/>with full history"]
    co --> merge["Run sync_main_into_jetson.sh"]
    merge -- "already included" --> verify
    merge -- "conflict" --> issue
    merge -- "merged" --> t10["Test on Python 3.10"]
    t10 --> t12["Test on Python 3.12"]
    t12 -- "both pass" --> push["git push (never force)"]
    t10 -- fail --> issue
    t12 -- fail --> issue
    push -- ok --> verify["Verify main is an ancestor<br/>of remote intrinsic_jetson"]
    push -- rejected --> issue
    verify --> close["Close resolved sync-blocker issues"]
    issue["Create or update the single<br/>'[sync] main to intrinsic_jetson blocked' issue"] --> fail["Job fails"]
```

The script [`scripts/sync_main_into_jetson.sh`](../../scripts/sync_main_into_jetson.sh):

- accepts only `main` into `intrinsic_jetson`,
- must run on a checked-out `intrinsic_jetson`,
- fetches both branches and refuses if the target moved since checkout,
- does nothing if `main` is already an ancestor,
- otherwise runs an ordinary `git merge --no-ff` and aborts on conflict. It never uses rebase,
  cherry-pick, `-X ours` or `-X theirs`.

| Exit code | Meaning |
|---|---|
| 0 | Merged, or already included (no-op). |
| 2 | Unsupported direction, or the wrong branch is checked out. |
| 3 | The target branch advanced since checkout. |
| 4 | Merge conflict. The merge is aborted and the remote is untouched. |

The workflow tests the **merged** tree with `compileall` and the unit suite on Python 3.10 and 3.12
before it pushes. It does this itself because GitHub generally does not start new workflow runs
from events created with the default `GITHUB_TOKEN`, so a push to the Jetson branch would not
trigger CI.

On any failure the workflow creates one blocker issue titled
`[sync] main to intrinsic_jetson blocked` with the main SHA and run URL, labelled `priority:P0`.
If one already exists it is updated and duplicates are closed. After a verified sync, resolved
blockers are closed. The Jetson branch is **not** treated as synchronized after a failure.

Fix a blocker through a reviewed PR, then rerun the workflow manually. Do not force-push, and do
not read an unresolved sync failure as permission to rewrite branch history.

### Requirements and limits

- Repository Actions permissions must allow `contents: write` and `issues: write`.
- A branch ruleset may reject pushes by `GITHUB_TOKEN`. If so, grant an appropriate actor or
  switch to a review-and-merge PR strategy. Do not bypass protection with force.
- Unit tests do not prove Cactus, CUDA, USB or Bluetooth microphones, or swap behaviour on the
  8 GB board.

### Run it locally

```bash
python -m unittest tests.test_branch_sync -v    # tests the script against temporary repos
git switch intrinsic_jetson
bash scripts/sync_main_into_jetson.sh           # local merge only; does not push
```

The script never pushes. Review the merge and run the tests before pushing manually.

## Scheduled runs

Scheduled worker and reviewer chats are an optional convenience, not part of Maestaris
correctness. They may lack repository write tools. In that case they report the real restriction
and do not build a relay or external executor. GitHub retains the durable state.
