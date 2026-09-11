---
name: orca-pipeline
description: >-
  Run work through a fixed three-role Orca pipeline — 기획(plan) → 기획검수(review) →
  실행(build) — keeping the planner and builder on persistent agent sessions that are
  reused across tasks instead of respawning per task, and launching a fresh reviewer
  for every unit. Discovers each agent CLI's currently configured models and asks
  the user how to staff each role before starting. Use when the user says
  "orca-pipeline", "파이프라인으로", "기획→검수→실행", "세션 재사용해서", "역할별로 돌려",
  "plan then review then build", or asks to orchestrate repeated units of work across a
  planner/reviewer/builder split. Do NOT use for a single one-off task, a full ownership
  handoff, or plain terminal prompts — use orca-cli for those.
---

# Orca 3-Role Pipeline (기획 → 기획검수 → 실행)

Supervised Orca orchestration with **pinned planner and builder sessions** and a
**fresh reviewer every unit**. The default `worker-start --worktree current` path
creates a *fresh* agent terminal every call — that is documented behavior, not a bug:

> *"Current and existing worktrees never rerun setup; a fresh agent terminal is
> created unless `--terminal` is explicit."* — `worker-start --help`

This skill pins the planner's and builder's terminals once and reuses them for every
later task, so those two roles accumulate project context instead of starting cold each
time. The reviewer is deliberately relaunched cold for every unit (Step 0c).

Resolve the CLI executable once (`$ORCA_CLI_COMMAND` → `orca-dev` in a dev checkout →
`orca-ide` on bare Linux → otherwise `orca`). Below, `ORCA` is that executable.

## Progress checklist (coordinator)

Keep this order; record each step in `.orca-pipeline/<run_id>/coordinator/progress.md`
(Setup) as it happens. Checks and evidence are in
[`references/validation.md`](references/validation.md).

1. Staffing confirmed (Step 0) and echoed back.
2. Run created or bound; run directory and `coordinator/` exist.
3. Per unit: plan accepted → valid review accepted (verdict recorded) → reviewer
   cleaned up with termination evidence → build accepted.
4. Between units: next owner of each pinned session chosen (reuse or retain).
5. End: pinned sessions released and closed with evidence, residual terminals listed,
   every Task settled, final report.

---

## Step 0 — 역할 편성 (필수, `run-create` 보다 먼저)

A pinned role's model is fixed at its first launch and cannot be changed by a reuse
call, so this has to happen before anything is created.

### 0a. 모델을 하드코딩하지 말고 발견한다

Model names and effort levels rot fast — `gpt-5.5` → `gpt-5.6-luna`, `claude-opus-4-7`
→ `claude-opus-5`, Claude gained a `fable` alias, and `gpt-5.6-sol` accepts an `ultra`
effort that older models reject. Never write a model id or an effort list into a
question. Read what the CLIs can actually reach **now**:

```bash
python "<skill_dir>/scripts/discover_models.py"
```

Read-only. For a missing, unreadable, malformed or wrongly-typed source the affected
field is `null`/`[]`/`{}` with a `warnings` entry, valid neighbouring entries are kept,
and one agent's failure does not erase the other's — the script still prints one JSON
document. Read `warnings` before building options.

**codex** — from `<codex_home>/models_cache.json` and `<codex_home>/config.toml`:
- `models[]` — `slug`, `display_name`, `description`, `default_effort`, and
  **`efforts` per model**. Internal models (`visibility: "hide"`) are excluded and
  the list is sorted by the cache's own `priority`.
- `default_model` / `default_effort` — this home's configured default. Either can be
  `null` independently: a config with only `model_reasoning_effort` yields
  `default_model: null` with a `default_effort`.
- `profiles` — named model+effort presets, when any exist
- `cache_fetched_at` / `cache_client_version` — check these; a cache written by an
  older client can predate a model release
- `home`, `home_from_env`, `other_homes`

**claude** — from `claude --help` and `~/.claude/settings.json`:
- `aliases` — parsed out of the `--model` help text, not hardcoded
- `efforts` — parsed out of the `--effort` help text
- `default_model` — the configured model

Empty `aliases`/`efforts` mean **not discovered** (help missing, timed out, exited
nonzero, or unparseable — the warning says which). Offer only the Claude CLI default
then; never refill the list from memory.

> **`CODEX_HOME` is account-scoped.** Orca redirects it per Codex account, so one
> machine holds several caches with **different** model lists and different defaults —
> an account may not see every model. The script reports the active home's list only
> and never merges `other_homes`; do not merge them either. If the discovered list
> looks wrong, check `home` and `home_from_env` first.

**Efforts are per model.** Read each model's own `efforts` array. Never offer a level
from another model or from memory.

If the script is unavailable, read the sources directly and say that discovery was
degraded rather than falling back to remembered names.

### 0b. 발견한 값으로 사용자에게 묻는다

**Only the root coordinator runs Step 0.** A dispatched Orca worker (its preamble names
a task id and `orca orchestration ask`) never runs Step 0, never creates a Run, and never
dispatches nested workers; if it needs a staffing decision it asks the coordinator with
the injected `orca orchestration ask`.

If the user already stated a role's agent/model/effort (or the Claude permission mode
below) in this session, use it and do not ask again for that role.

Otherwise ask **once**, using whatever user-question mechanism the current host and mode
actually provide. If a structured question tool is available, put the questions in one
call with headers `기획`, `기획검수`, `실행`; if not, ask the same questions once in
plain text and wait for the answer. Never call a tool the host does not expose, and do
not switch modes just to get one.

> "기획(planner)을 어떤 에이전트·모델·에포트로 돌릴까요?"
> "기획검수(reviewer)를 어떤 에이전트·모델·에포트로? 매 태스크마다 새 세션으로 뜹니다."
> "실행(builder)을 어떤 에이전트·모델·에포트로 돌릴까요?"

Build every option from Step 0a output. Compose the option list in this order:

1. **`<agent> / CLI 기본값 그대로` — always first, always the recommendation.**
   This omits `--model` and `--effort` entirely, so the agent CLI applies its own
   current default. It is the only choice that cannot go stale. Show the discovered
   `default_model` / `default_effort` in the option *description* so the user can see
   what they are accepting, never in the label. If `default_model` is `null`, say the
   CLI's built-in default applies; a `default_effort` without a model is shown as
   information only, never turned into an effort-only launch option.
2. A discovered `profiles` entry (codex), if any exist.
3. For Claude, an option built from a discovered **alias** — `claude --help` documents
   `--model` as taking "an alias for the latest model", so an alias tracks releases on
   its own. Always prefer an alias over a pinned id. No discovered aliases → no alias
   options.
4. For Codex, a `models[]` entry, using its `display_name` + `description` in the
   option description so the choice is legible.
5. A higher-effort variant of the recommended option when the role warrants it
   (planning and review benefit; routine building usually does not). Take the level
   from **that model's own `efforts`** — `max` and `ultra` exist on some models and
   not others.

Suggested defaults to steer toward, without naming models:

| 역할 | 방향 |
|---|---|
| 기획 | 추론이 가장 깊은 편성. effort 한 단계 상향이 정당한 자리 |
| 기획검수 | **기획과 다른 제공자**를 권한다 — 같은 모델끼리는 같은 맹점을 공유한다 |
| 실행 | CLI 기본값으로 충분한 경우가 대부분 |

If a model's `efforts` array is empty (the cache carried no
`supported_reasoning_levels`), omit `--effort` for that model rather than guessing a
level — `--effort` requires `--model`, but not the reverse.

**Claude permission mode — ask only when a role is staffed with Claude.** Add one
question (header `Claude 권한`) to the same round:

- `권한 우회 (--dangerously-skip-permissions)` — description: *Claude의 모든 권한 확인을
  끈다. 워커가 확인 없이 파일을 쓰고 명령을 실행한다. Orca 워커 기동에서 동작이 관측된
  경로다.*
- `권한 우회 없음` — description: *플래그 없이 띄운다. 워커가 권한 확인에서 멈추거나 바로
  종료될 수 있고, 이 경로로 완주한 관측은 없다.*

Neither is pre-selected; the skill never adds the flag on its own. The answer applies
to every Claude role in this pipeline and is written into the launch command
(Launching a role).

Then echo the final staffing table (including the Claude permission mode) back to the
user before launching anything.

### 0b-2. 명시적 모델은 띄우기 전에 검증한다 (필수)

Discovery lists what the CLI advertises. It does **not** prove the account may use a
given model, and the user can always answer "Other" with a model discovery never saw.
A pinned role that fails on its first request costs a full relaunch, so validate first.

Skip this only when the answer was "CLI 기본값 그대로" (nothing to validate).

For a codex model, one cheap non-interactive probe:

```bash
codex exec --model <slug> -c model_reasoning_effort="<effort>" "Reply with exactly: OK"
```

Read the result carefully — `codex exec` **exits 0 even when the request failed**, so the
exit code proves nothing. Look for these in the output instead:

- `warning: Model metadata for '<slug>' not found. Defaulting to fallback metadata` —
  the CLI does not know this slug. Treat it as a **wrong-slug signal**, because the
  request usually 400s right after.
- `ERROR: {... "status":400 ... "message":"The '<slug>' model is not supported when using
  Codex with a ChatGPT account."}` — **do not read this as an entitlement problem.**
  Observed reality: the human-facing name (`astra`) produced exactly this message, while
  the discovered slug (`gpt-6-astra`) worked on the same account and auth. Check the slug
  against Step 0a's `models[]` before concluding anything about permissions.
- A plain model reply with no warning and no `ERROR:` line — validated.

If the slug is absent from `models[]`, suspect a stale cache before suspecting the
account: compare `cache_client_version` against the installed CLI version. `gpt-6-astra`
was missing while the cache was written by an older client and appeared once a current
client refreshed it. Running codex once refreshes it.

For a Claude model, prefer a discovered **alias** — an alias cannot be stale by
construction, which is most of why validation is needed at all.

If validation fails, **stop and ask the user** with the exact error. Do not pick a
replacement yourself: the model was their explicit choice, and the nearest reachable
substitute is a judgement call about cost and depth, not a detail.

> A note on scope, because it is easy to overweight. `--model`/`--effort` are a
> convenience for what the session *launches* with. They are not the mechanism of this
> skill — pinned sessions via `--terminal` are. If a role needs a model the CLI cannot
> launch with (an MCP-provided model, a picker-only model, an entitlement this auth
> lacks), launch the agent on its own default and let the session select the model
> internally. Do not block the pipeline on a launch flag.

### 0c. 세션 정책

| 역할 | 세션 정책 (기본) | 모델·effort 적용 시점 | 정리 |
|---|---|---|---|
| **기획** planner | **고정** — launch once, reuse | 첫 launch 에만 | 파이프라인 종료 시 |
| **기획검수** reviewer | **매번 새 세션** — cold read | 매 launch 마다 | 매 검수 완료 후, 종료 증거까지 |
| **실행** builder | **고정** — launch once, reuse | 첫 launch 에만 | 파이프라인 종료 시 |

| 에이전트 | 기동 경로 (Launching a role) | 모델·effort 전달 위치 |
|---|---|---|
| `claude` | `terminal create --command "claude …"` → `worker-start --terminal` | create 의 커맨드라인 (`--model`, `--effort`) |
| `codex`, `cursor` | `worker-start --agent` | `worker-start --model/--effort` |
| 그 밖 | `worker-start --agent` | 전달하지 않음 |

The reviewer is fresh by design: it must judge the plan without having watched it being
written. The planner and builder are pinned by design: their value grows with
accumulated context. State this policy and proceed unless the user overrides it.

**`--model` / `--effort` are forwarded only for Claude, Codex, and Cursor.** If the user
picks another agent, drop both flags and say so — do not silently pass them.

`--agent` takes "a known TUI agent" id; the CLI does not enumerate them and neither does
`agent-context`, so do not recite a list as authoritative. Orca's own guides mention
`claude`, `codex`, `cursor`, `opencode`, `omp`, `pi`, and `grok`, and group addresses
additionally reference `gemini` and `droid` — but a group address is not proof that the
same string works as an `--agent` id. Offer `claude` / `codex` / `cursor` by default
(they also accept `--model`/`--effort`); for anything else, say it is unverified and let
`worker-start` be the check — an unknown id fails at launch with a clear error, which is
cheap. Never claim an id is valid because this file lists it.

---

## Hard constraints (verified against this CLI)

- **`--model` / `--effort` cannot combine with `--terminal`**, and `--effort` requires
  `--model`. On the `worker-start --agent` path pass them on a pinned role's *first*
  `worker-start` only; on the Claude path they go into the `terminal create` command.
- **`worker-start --agent` passes no extra agent argv** — only `--model`/`--effort`.
  A Claude worker launched as `worker-start --agent claude` was observed to exit right
  after `input_accepted`, with an empty `worker-read`. Launch Claude with the
  Launching-a-role path.
- **A `ready` start receipt is not a live worker.** `dispatch_input: accepted` and a
  matching `launch.effective` only prove the input was delivered.
- **`worker-release` closes only a terminal that `worker-start --agent` created.** For
  a terminal made by `terminal create` and attached with `worker-start --terminal`, the
  Dispatch lifecycle is supervised but the terminal resource is `external`: release
  returns `ok: true`, `state: "retained"`, `reason: "external_terminal"`,
  `processAction: "none"`, exit 0 — and the terminal stays live. Never read exit 0 or
  `ok: true` as "closed"; see Cleanup.
- **Never release a pinned role mid-pipeline.**
- **`--retry-of` is valid only while the Task is `failed` or `blocked`.** An
  `escalation` for a dead worker returns the Task to `ready`; then `--retry-of` is
  refused with `task_not_startable` — start without it.
- **`worker-abandon` closes nothing.** It fences the Dispatch and leaves its terminal in
  `residualResources`.
- **Nested worker depth defaults to 1.** Run this skill from a coordinator (root)
  terminal. Inside a dispatched worker it fails with `nested_worker_depth_exceeded` —
  do not route around it; report and stop.
- **`check --wait --json` prints keepalives to stderr.** Pipe stdout only. Never
  `check --wait --json 2>&1 | <parser>` — it fails with `Extra data: line 2`.

## Setup (once per pipeline)

```bash
ORCA status --json                       # runtime must be ready
ORCA orchestration run-create --objective "<전체 목표>" --json
```

Read `run.id` out of the `run-create` receipt — every artifact path below is keyed on it.

Artifacts go in a run directory so roles hand off by file, not by pasted text. **The
coordinator creates it before the first dispatch**; do not leave it to a worker, whose
first write would otherwise fail on a missing parent:

```bash
mkdir -p ".orca-pipeline/<run_id>/coordinator"
```

```
.orca-pipeline/<run_id>/<NN>-plan.md      # planner writes
.orca-pipeline/<run_id>/<NN>-review.md    # reviewer writes
.orca-pipeline/<run_id>/<NN>-build.md     # builder writes
.orca-pipeline/<run_id>/coordinator/      # coordinator only; no worker reads it
    progress.md
    <NN>-validation.md
```

**`<NN>` is the unit number**, two digits (`01`, `02`, …), identical for every artifact
of that unit. It is not a step counter: unit 1's review is `01-review.md`. A retried
role in the same unit writes the same path; `progress.md` records which Dispatch
produced the file that was accepted.

`progress.md` holds, per step: unit, role, Task id, the **currently valid** Dispatch id,
terminal handle and how it was created (`worker-start --agent` or `terminal create`),
artifact path, outcome, and the retain/reuse/release/close decision with its receipt.
Orca receipts are the source of truth; this file is the resume summary. Update the
Dispatch id after every relaunch or reuse so cleanup never targets an old Dispatch.

Add `.orca-pipeline/` to `.gitignore` if it is not there.

---

## Launching a role

**Claude** (any role) — custom argv, then attach:

```bash
ORCA terminal create --worktree current --title "<role>-<NN>" \
  --command "claude [--dangerously-skip-permissions] [--model <m>] [--effort <e>]" --json
# → handle; record it in progress.md as created by terminal create
ORCA terminal wait --terminal <handle> --for tui-idle --timeout-ms 120000 --json
# → require satisfied: true
ORCA terminal read --terminal <handle> --screen --json
# → require Claude's input prompt; note the model/effort and permission mode shown
ORCA orchestration worker-start --task <task_id> --worktree current \
  --terminal <handle> --json
```

- Include `--dangerously-skip-permissions` only if Step 0b chose it, and
  `--model`/`--effort` only if Step 0b chose an explicit model. Never put them on the
  `worker-start --terminal` call.
- **Startup screen is not the input prompt** (trust, bypass-permissions confirmation,
  settings, login): do not `worker-start` and do not inject a prompt. Show the screen
  text to the user and ask them to answer it in that terminal. Then run the `wait` and
  `read --screen` once more. If it is still not the input prompt, the user declines, or
  the process exited, give up on this launch: `ORCA terminal close --terminal <handle>
  --json` (no Dispatch owns it yet), record the screen text, and report to the user. Do
  not loop.
- Model/effort proof: this path leaves `launch.requested`/`launch.effective` model and
  effort `null`. Record what the screen shows; if it shows nothing, record the values
  as "requested only".

**Codex / Cursor / other agents:**

```bash
ORCA orchestration worker-start --task <task_id> --worktree current \
  --agent <agent> [--model <m> --effort <e>] --json
ORCA orchestration worker-show --dispatch <dispatch_id> --json
# → worker.agent_terminal_handle; compare launch.requested with launch.effective
```

**Either path — started is not alive.** Count the role as started only after a
heartbeat, question or `worker_done` from that Dispatch, or a non-empty `worker-read`.
An `escalation` "Agent exited unexpectedly" right after `input_accepted` with an empty
`worker-read` is the dead-on-launch fingerprint.

## Cleanup of a settled role

Run only after an accepted `worker_done` (or a proven failed/exited worker):

```bash
ORCA orchestration worker-release --dispatch <dispatch_id> --json
```

Read the receipt, not the exit code:

- `released` / `already_released` → confirm the handle is gone (below).
- `retained` with `reason: external_terminal`, **and** `progress.md` shows the
  coordinator created that handle with `terminal create` for this Dispatch → close it:
  `ORCA terminal close --terminal <handle> --json`. Release has already run and been
  recorded, so this is not a substitute for release; it covers the one terminal Orca
  declines to own because the coordinator created it outside `worker-start`.
- `release_pending` / `release_unknown` → follow the receipt's recovery action. Never
  close by hand.
- `retained` for any other reason (user takeover, pre-existing, setup) → do not close.
  List it in the final report and let the user decide.

**Positive termination evidence is required**: the handle is absent from
`ORCA terminal list --worktree current --json`, or the close receipt reports the
process killed. Record it in `progress.md`. Without it the role is not cleaned up.

---

## Per unit of work

### 1. 기획 — pinned planner

First unit only — create the Task and launch the planner (Launching a role):

```bash
ORCA orchestration task-create --spec "<기획 지시>" --json
# then Launching a role → PLANNER_HANDLE
```

Record that handle as `PLANNER_HANDLE`. Every later unit reuses it — **no `--model`,
no `--effort`**, on either path:

```bash
ORCA orchestration worker-start --task <next_task_id> --worktree current \
  --terminal $PLANNER_HANDLE --json
```

The task spec must tell the planner to write its plan to
`.orca-pipeline/<run_id>/<NN>-plan.md` and report it with
`--report-path ".orca-pipeline/<run_id>/<NN>-plan.md"`.

### 2. 기획검수 — fresh reviewer, then cleanup

Every unit relaunches, so the chosen model/effort go on **every** launch:

```bash
ORCA orchestration task-create --spec "<검수 지시 + 계획 파일 경로>" --json
# then Launching a role, always with a new terminal
```

The spec must instruct the reviewer to:
- read `<NN>-plan.md` and **only** that plan (it has no prior context by design),
- **not read** `coordinator/` (progress, validation, coordinator notes) or any other
  unit's `-review.md` / `-build.md` in the run directory; the repository itself may be
  read to check the plan's claims,
- write findings to `<NN>-review.md` as numbered concerns, each marked `blocking` or
  `advisory`, and end the file with exactly one line `VERDICT: APPROVE` or
  `VERDICT: REJECT`,
- report `--outcome succeeded` for a completed review — a REJECT verdict is a
  *finding*, not a worker failure. Reserve `--outcome failed` for a review it could
  not perform.

After its `worker_done` is accepted, run **Cleanup of a settled role** so the next
review starts cold and no reviewer terminal is left behind.

**No valid review.** A failed review Task, a missing `<NN>-review.md`, or a file
without exactly one valid `VERDICT:` line is not a handoff. Record it in `progress.md`
and do not start the build. Clean up that attempt, then launch one fresh reviewer: on
the same Task while it is startable — with `--retry-of <dispatch_id>` only if the Task
is `failed`/`blocked` (Hard constraints), recording the receipt's `retryOfDispatchId`
rather than assuming it — or on a new review Task for the same unit if the old one
already settled `succeeded` with an invalid file. If that second attempt is also
invalid, or Orca circuit-breaks the Task, **stop and ask the user** (retry again, build
without a review, or stop). Do not decide alone.

### 3. 실행 — pinned builder, every review concern carried forward

**A REJECT does not stop the pipeline and does not loop back to the planner.** Every
review concern — whatever the verdict — goes into the builder's spec.

First unit only — create the Task and launch the builder (Launching a role) →
`BUILDER_HANDLE`. Later units:

```bash
ORCA orchestration worker-start --task <next_task_id> --worktree current \
  --terminal $BUILDER_HANDLE --json
```

The builder's spec must contain:

```
## 계획
.orca-pipeline/<run_id>/<NN>-plan.md 를 읽고 구현한다. 계획·검수 파일은 수정하지 않는다.

## 검수 판정: <APPROVE | REJECT>
검수 파일: .orca-pipeline/<run_id>/<NN>-review.md

## 주의사항 (검수 의견) — blocking <b>건, advisory <a>건
<검수 파일의 모든 의견을 원래 번호·등급·원문 그대로 옮긴다. 판정과 무관하다>

각 항목은 반영하거나, 반영하지 않는 이유를 빌드 보고서에 명시한다.
묵살은 허용하지 않는다.

## 산출물
.orca-pipeline/<run_id>/<NN>-build.md
- 변경 요약
- 실제 실행한 검증 명령과 결과
- 검수 의견 처리표: 번호 · 반영/미반영 · 이유 · 증거 (모든 번호)
- 남은 제한

## 완료 보고
worker_done 본문은 정확히 3문장이고 --report-path 로 빌드 보고서를 연결한다.
미반영 의견이 있으면 그 개수와 중요한 번호를 본문에 밝힌다.
요구사항을 달성하지 못했으면 --outcome succeeded 로 숨기지 말고 failed 로 보고한다.
```

Write `없음` under 주의사항 **only when the review has zero concerns** — keep the
section so the builder always knows a review happened. After the build, check that the
concern numbers in the review, the spec, and the build table are the same set.

---

## Waiting

One rolling wait covers all three roles; process the whole Delivery before acking.

```bash
ORCA orchestration check --wait --types worker_done,escalation,question \
  --timeout-ms 1800000 --json
# reply to every question, handle every worker_done AND every escalation, then:
ORCA orchestration check --ack <delivery_id> --wait \
  --types worker_done,escalation,question --timeout-ms 1800000 --json
```

- **Ack every Delivery you processed, escalations included.** A Delivery replays until
  acked: an escalation you already diagnosed and acted on comes back on the next wait
  as if new. Handling it and acking it are separate steps.
- A timeout or `{count:0}` is a **checkpoint, not a failure**. Nothing is acked (there
  is no `deliveryId`); run the same wait again. Pick the window per wait, not per
  pipeline: 15 minutes (`900000`) suits default-effort roles, while a `max`-effort
  reviewer was observed taking about 20 minutes, so use 30 minutes (`1800000`) when a
  high-effort role is running. Coding tasks routinely run 15–60 minutes. Keep waiting
  unless you get `worker_done`/`escalation`, the terminal exits, or the user says stop.
- Heartbeats and visible terminal activity mean alive, not done. Never stop or restart a
  role because it has been quiet.
- Answer worker questions with
  `ORCA orchestration reply --id <msg_id> --body "<answer>" --json`.
- Steer a pinned role without creating a task:
  `ORCA orchestration send --to dispatch:<dispatch_id> --subject "..." --body "..." --json`.
- For a dead-worker escalation: `worker-show`/`worker-read` to confirm, `worker-abandon`
  if the Dispatch is not yet settled, clean up the terminal (Cleanup), relaunch per the
  `--retry-of` rule, record the new Dispatch in `progress.md`, then ack.

## Between units — keep pinned sessions alive

After a pinned role's `worker_done`, choose its next owner **before** acking:

- **Immediate next task ready** → `worker-start --task <next> --terminal <handle> --json`.
  The new Dispatch becomes the current one in `progress.md`. This is the preferred path.
- **Idle gap before the next task** → record the retention explicitly:
  `ORCA orchestration worker-retain --dispatch <dispatch_id> --json`.
  Do not silently skip cleanup.

If a handle returns `terminal_handle_stale`, re-resolve and continue with the replacement
**only** — never dual-send to old and new handles:

```bash
ORCA terminal list --worktree current --json
```

## Teardown (end of pipeline)

For the planner and the builder, using the **current** Dispatch from `progress.md`, run
Cleanup of a settled role:

```bash
ORCA orchestration worker-release --dispatch <planner_dispatch_id> --json
ORCA orchestration worker-release --dispatch <builder_dispatch_id> --json
# retained + external_terminal on a coordinator-created handle → terminal close
ORCA terminal list --worktree current --json   # positive evidence: handles gone
ORCA orchestration worker-list --run <run_id> --json   # every page
ORCA orchestration task-list --json            # confirm every task settled
```

`worker-retain` earlier does not block this — passing the same Dispatch to
`worker-release` clears the retention. Settlement checks are in
[`references/validation.md`](references/validation.md) §3. User-owned leftover
terminals (for example a failed attempt the user took over) are listed in the final
report and closed only if the user says so.

## Rules

- Never substitute the Task/Agent tool, a generic subagent API, or chat-only parallel
  workers. Those create no Orca task/dispatch provenance, no injected lifecycle
  preamble, and no `worker_done` authority. If work accidentally ran outside
  orchestration, say so plainly rather than describing it as orchestrated.
- Verify before claiming a role was orchestrated:
  `ORCA orchestration task-list --json` and
  `ORCA orchestration dispatch-show --task <task_id> --json`.
- One Run per pipeline. Create or bind it once; do not create a Run per unit.
- Never name a model from memory. Every model id shown to the user comes from Step 0a
  discovery, or the option omits `--model` altogether.
- Never pin a role on an unvalidated explicit model. Run Step 0b-2 first, and remember
  that `codex exec` exits 0 on a failed request — read its output, not its exit code.
- When a launch flag cannot express what the user wants, launch on the CLI default and
  say so. Do not stall the pipeline over `--model`.
- Do not release a worker on timeout, TUI idle, heartbeat, status, question, escalation,
  or a stale `worker_done`.
- Accept a `worker_done` only when its `taskId`/`dispatchId` match the current Dispatch
  in `progress.md`; then check its outcome and artifact. A `failed` outcome, a missing
  file, or an old Dispatch's message never starts the next role.
- If `worker-start` exits nonzero, inspect `stage`, `effects`, and `residualResources`
  in the JSON. Do not auto-retry. A retry restates `--worktree` and `--agent`/`--terminal`,
  and adds `--retry-of <dispatch_id>` only when the Task is `failed` or `blocked`; if an
  escalation already returned the Task to `ready`, start without it.
- Reuse the Step 0 staffing for the whole pipeline. If the user wants a pinned role's
  model changed mid-run, that requires releasing and relaunching that role — say so
  before doing it, since the accumulated session context is lost.
- Report the truth at the end: which units completed, which roles were reused vs
  relaunched, which terminals were closed with evidence and which remain, and every
  reviewer concern the builder declined to address.
