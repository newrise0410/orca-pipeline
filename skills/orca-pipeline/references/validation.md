# Validation procedures for orca-pipeline

This file is a **procedure**, not evidence. It says what to check and what counts as
proof. The results of an actual Run go to
`.orca-pipeline/<run_id>/coordinator/<NN>-validation.md`, written by the coordinator,
never by a worker (a worker cannot observe its own settlement).

Mark every line of a validation record as one of: **verified** (with the receipt,
file, or command output that proves it), **requested only** (the command asked for
it but nothing observed the effect), or **unverified**. Do not upgrade one to another.

## 1. One unit completed

| Claim | Evidence required |
|---|---|
| Plan done | Accepted `worker_done` whose `taskId`/`dispatchId` match the current planner Dispatch in `progress.md`, `outcome: succeeded`, and `<NN>-plan.md` exists |
| Review done | Same match for the reviewer Dispatch, `<NN>-review.md` exists, exactly one `VERDICT: APPROVE` or `VERDICT: REJECT` line, numbered concerns each marked `blocking`/`advisory` |
| Review concerns carried | The concern numbers in `<NN>-review.md`, in the builder spec, and in the `<NN>-build.md` table are the same set |
| Build done | Same match for the builder Dispatch, `<NN>-build.md` exists, 3-sentence body, `--report-path` set |
| Fresh reviewer | The reviewer's terminal handle came from a `terminal create` (or `worker-start --agent`) receipt made for this unit, and differs from the planner, the builder, and every earlier reviewer |
| Reviewer cleaned up | **Positive termination evidence**: the reviewer handle is absent from `ORCA terminal list --worktree current --json` after cleanup, or the close receipt reports the process killed. A `worker-release` receipt alone is not enough — for a `terminal create` terminal it returns `ok: true`, `state: retained`, `reason: external_terminal` and exit 0 while the terminal stays live |

A `failed` outcome, a missing file, or a `worker_done` from an older Dispatch is not
evidence for any row. A stale or mismatched `worker_done` is recorded, not accepted.

## 2. Pinned sessions were reused

Evidence: two different Dispatches of the same role whose
`worker-show` → `worker.agent_terminal_handle` is identical, where the second was
started with `worker-start --terminal <that handle>` and its `worker_done` was
accepted. A changed handle is a relaunch, not a reuse — record it as such.

If the pipeline has no second unit, give the planner and the builder one read-only
follow-up Task each **before teardown**, in the same Run: read the existing plan or
build report, report the role, the current Task/Dispatch ids and what was checked, and
change nothing. These are not new units, not nested pipelines, and launch no new
role. A worker never creates these follow-ups itself.

One fresh reviewer observed in one unit does not prove "a fresh reviewer every unit";
say so if only one unit ran.

## 3. Final settlement

- `ORCA orchestration task-list --json`: every Task this Run created is settled.
- `ORCA orchestration worker-list --run <run_id> --json`, every page
  (`page.nextCursor` until `page.hasMore` is false): no `active`, `reclaimable`,
  `release_pending` or `release_unknown` rows left for this Run.
- Every `retained` row is one of: a handle proven closed (section 1 evidence), a
  pinned role deliberately retained because the pipeline continues (say so), or a
  user-owned terminal (`ownershipState: user_owned`) listed in the final report for
  the user to decide. `reclaimable: 0` alone proves none of these.

## 4. Review → build handoff branches

Synthetic cases. Check each against the SKILL sections named; record the SKILL line
numbers you relied on so someone else can re-judge.

| Review input | Expected handoff | Decided by |
|---|---|---|
| APPROVE, 0 concerns | 주의사항 `없음`, build starts | SKILL "3. 실행" builder spec |
| APPROVE, 2 advisory | Both concerns carried with original numbers and text; build table has 2 rows | SKILL "3. 실행" builder spec |
| REJECT, 1 blocking + 1 advisory | Both carried; build starts; planner is not re-run | SKILL "3. 실행" first paragraph and builder spec |
| Review Task failed, or file missing, or not exactly one valid VERDICT line | Not a handoff. Coordinator records it and does not start the build. Relaunch one fresh reviewer; if the second attempt is also invalid (or the Task circuit-breaks), ask the user and stop | SKILL "2. 기획검수" → "No valid review" |

## 5. Claude launch path

Record for every Claude role launch:

1. The exact `terminal create --command` line (flags present or absent).
2. `terminal wait --terminal <handle> --for tui-idle` receipt with `satisfied: true`.
3. `terminal read --terminal <handle> --screen` showing Claude's input prompt — not a
   trust, bypass-confirmation or settings screen. If one was shown, what the user
   chose and the second screen read.
4. The `worker-start --terminal <handle>` receipt (`effects` terminal `reused`,
   `dispatch_input: accepted`). This is **not** proof the agent is alive.
5. Liveness: a heartbeat, question or `worker_done` from that Dispatch, or non-empty
   `worker-read`. The known failure fingerprint is an `escalation` "Agent exited
   unexpectedly" right after `input_accepted`, with an empty `worker-read`.
6. Model and effort: this path leaves `launch.requested`/`launch.effective` model and
   effort `null` in the receipt. Record the model/effort visible on the Claude screen
   (step 3) as verified; if the screen does not show them, write **requested only**.
7. Permission mode: `bypass permissions on` visible on screen when the flag was used.
8. Work done: the role's file was written and its `worker_done` accepted.
9. Cleanup: section 1 "Reviewer cleaned up" evidence (or, for a pinned role, at
   teardown).

A flag present in the command does not prove any later step. If a step fails, keep
the screen text and error; do not attribute the failure to the flag without evidence.

## 6. Entry-path branches

| Situation | Single expected path | Decided by |
|---|---|---|
| Root session, a structured question tool is available | Ask all roles once with that tool, then echo the staffing table | SKILL Step 0b |
| Root session, no structured question tool | Ask the same questions once in plain text and wait; do not call a missing tool or switch modes | SKILL Step 0b |
| Dispatched Orca worker | Do not run Step 0 or create a Run; ask the coordinator with the injected `orca orchestration ask` | SKILL Step 0b and Hard constraints |
| Staffing already stated by the user in this session | Use it; do not ask again for those roles | SKILL Step 0b |

These are document checks, not observations of a real host UI. Record them as such.

## 7. Local repository checks

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -c "import json,sys; [json.load(open(p, encoding='utf-8')) for p in sys.argv[1:]]" \
  .claude-plugin/marketplace.json .claude-plugin/plugin.json .codex-plugin/plugin.json \
  .agents/plugins/marketplace.json
git add -N <each new file>      # untracked files are invisible to diff --check otherwise
git diff --check
```

The unit tests prove discovery parsing and failure handling only — not that an account
may use a model, and not that the real CLI still prints the same help text.
