---
name: orca-pipeline
description: >-
  Run work through a fixed three-role Orca pipeline — 기획(plan) → 기획검수(review) →
  실행(build) — on persistent agent sessions that are reused across tasks instead of
  respawning per task. Discovers each agent CLI's currently configured models and asks
  the user how to staff each role before starting. Use when the user says
  "orca-pipeline", "파이프라인으로", "기획→검수→실행", "세션 재사용해서", "역할별로 돌려",
  "plan then review then build", or asks to orchestrate repeated units of work across a
  planner/reviewer/builder split. Do NOT use for a single one-off task, a full ownership
  handoff, or plain terminal prompts — use orca-cli for those.
---

# Orca 3-Role Pipeline (기획 → 기획검수 → 실행)

Supervised Orca orchestration with **pinned role sessions**. The default
`worker-start --worktree current` path creates a *fresh* agent terminal every call —
that is documented behavior, not a bug:

> *"Current and existing worktrees never rerun setup; a fresh agent terminal is
> created unless `--terminal` is explicit."* — `worker-start --help`

This skill pins each role's terminal once and reuses it for every later task, so the
planner and builder accumulate project context instead of starting cold each time.

Resolve the CLI executable once (`$ORCA_CLI_COMMAND` → `orca-dev` in a dev checkout →
`orca-ide` on bare Linux → otherwise `orca`). Below, `ORCA` is that executable.

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

Read-only, never raises; a missing source yields `null`/`[]` plus a `warnings` entry.

**codex** — from `<codex_home>/models_cache.json` and `<codex_home>/config.toml`:
- `models[]` — `slug`, `display_name`, `description`, `default_effort`, and
  **`efforts` per model**. Internal models (`visibility: "hide"`) are excluded and
  the list is sorted by the cache's own `priority`.
- `default_model` / `default_effort` — this home's configured default
- `profiles` — named model+effort presets, when any exist
- `cache_fetched_at` / `cache_client_version` — check these; a cache written by an
  older client can predate a model release
- `home`, `home_from_env`, `other_homes`

**claude** — from `claude --help` and `~/.claude/settings.json`:
- `aliases` — parsed out of the `--model` help text, not hardcoded
- `efforts` — parsed out of the `--effort` help text
- `default_model` — the configured model

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

Call `AskUserQuestion` **once** with three questions — headers `기획`, `기획검수`, `실행`:

> "기획(planner)을 어떤 에이전트·모델·에포트로 돌릴까요?"
> "기획검수(reviewer)를 어떤 에이전트·모델·에포트로? 매 태스크마다 새 세션으로 뜹니다."
> "실행(builder)을 어떤 에이전트·모델·에포트로 돌릴까요?"

Build every option from Step 0a output. Compose the option list in this order:

1. **`<agent> / CLI 기본값 그대로` — always first, always the recommendation.**
   This omits `--model` and `--effort` entirely, so the agent CLI applies its own
   current default. It is the only choice that cannot go stale. Show the discovered
   `default_model` / `default_effort` in the option *description* so the user can see
   what they are accepting, never in the label.
2. A discovered `profiles` entry (codex), if any exist.
3. For Claude, an option built from a discovered **alias** — `claude --help` documents
   `--model` as taking "an alias for the latest model", so an alias tracks releases on
   its own. Always prefer an alias over a pinned id.
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

Then echo the final staffing table back to the user before launching anything.

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

| 역할 | 세션 정책 (기본) | `--model`/`--effort` 전달 시점 |
|---|---|---|
| **기획** planner | **고정** — launch once, reuse | 첫 launch 에만 |
| **기획검수** reviewer | **매번 새 세션** — cold read, then release | 매 launch 마다 |
| **실행** builder | **고정** — launch once, reuse | 첫 launch 에만 |

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
  `--model`. Pass them on a pinned role's *first* `worker-start` only.
- **`worker-release` closes the terminal.** Never release a pinned role mid-pipeline.
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
mkdir -p ".orca-pipeline/<run_id>"
```

```
.orca-pipeline/<run_id>/<NN>-plan.md      # planner writes
.orca-pipeline/<run_id>/<NN>-review.md    # reviewer writes
```

Add `.orca-pipeline/` to `.gitignore` if it is not there.

---

## Per unit of work

### 1. 기획 — pinned planner

First unit only. Include `--model`/`--effort` **only if** Step 0b chose an explicit
model; omit both when the answer was "CLI 기본값 그대로":

```bash
ORCA orchestration task-create --spec "<기획 지시>" --json
ORCA orchestration worker-start --task <task_id> --worktree current \
  --agent <planner_agent> [--model <m> --effort <e>] --json
ORCA orchestration worker-show --dispatch <dispatch_id> --json   # → worker.agent_terminal_handle
```

Record that handle as `PLANNER_HANDLE`. Every later unit reuses it — **no `--model`,
no `--effort`**:

```bash
ORCA orchestration worker-start --task <next_task_id> --worktree current \
  --terminal $PLANNER_HANDLE --json
```

The task spec must tell the planner to write its plan to
`.orca-pipeline/<run_id>/<NN>-plan.md` and report it with
`--report-path ".orca-pipeline/<run_id>/<NN>-plan.md"`.

### 2. 기획검수 — fresh reviewer, then release

Every unit relaunches, so the chosen model/effort go on **every** call:

```bash
ORCA orchestration task-create --spec "<검수 지시 + 계획 파일 경로>" --json
ORCA orchestration worker-start --task <task_id> --worktree current \
  --agent <reviewer_agent> [--model <m> --effort <e>] --json
```

The spec must instruct the reviewer to:
- read `<NN>-plan.md` and **only** that plan (it has no prior context by design),
- write findings to `<NN>-review.md`,
- end with an explicit `VERDICT: APPROVE` or `VERDICT: REJECT` line plus numbered
  concerns, each marked `blocking` or `advisory`,
- report `--outcome succeeded` for a completed review — a REJECT verdict is a
  *finding*, not a worker failure. Reserve `--outcome failed` for a review it could
  not perform.

After its `worker_done`, release it so the next review starts cold:

```bash
ORCA orchestration worker-release --dispatch <review_dispatch_id> --json
```

### 3. 실행 — pinned builder, rejection carried forward

**A REJECT does not stop the pipeline and does not loop back to the planner.** Carry the
rejection into the builder's spec as mandatory 주의사항.

First unit only:

```bash
ORCA orchestration task-create --spec "<실행 지시>" --json
ORCA orchestration worker-start --task <task_id> --worktree current \
  --agent <builder_agent> [--model <m> --effort <e>] --json
ORCA orchestration worker-show --dispatch <dispatch_id> --json   # → BUILDER_HANDLE
```

Later units:

```bash
ORCA orchestration worker-start --task <next_task_id> --worktree current \
  --terminal $BUILDER_HANDLE --json
```

The builder's spec must contain, verbatim:

```
## 계획
<NN>-plan.md 를 읽고 그대로 구현한다.

## 주의사항 (검수 반려 사유)
<reviewer 의 blocking/advisory 항목을 번호째로 옮겨 적는다>

각 항목은 반영하거나, 반영하지 않는 이유를 worker_done 본문에 명시한다.
묵살은 허용하지 않는다.
```

If the verdict was APPROVE, keep the section with `없음` rather than deleting it — the
builder should always know a review happened.

---

## Waiting

One rolling wait covers all three roles; process the whole Delivery before acking.

```bash
ORCA orchestration check --wait --types worker_done,escalation,question \
  --timeout-ms 900000 --json
# reply to every question, handle every worker_done, then:
ORCA orchestration check --ack <delivery_id> --wait \
  --types worker_done,escalation,question --timeout-ms 900000 --json
```

- A timeout or `{count:0}` is a **checkpoint, not a failure**. Coding tasks routinely run
  15–60 minutes. Keep waiting unless you get `worker_done`/`escalation`, the terminal
  exits, or the user says stop.
- Heartbeats and visible terminal activity mean alive, not done. Never stop or restart a
  role because it has been quiet.
- Answer worker questions with
  `ORCA orchestration reply --id <msg_id> --body "<answer>" --json`.
- Steer a pinned role without creating a task:
  `ORCA orchestration send --to dispatch:<dispatch_id> --subject "..." --body "..." --json`.

## Between units — keep pinned sessions alive

After a pinned role's `worker_done`, choose its next owner **before** acking:

- **Immediate next task ready** → `worker-start --task <next> --terminal <handle> --json`.
  Orca transfers cleanup ownership to the new Dispatch. This is the preferred path.
- **Idle gap before the next task** → record the retention explicitly:
  `ORCA orchestration worker-retain --dispatch <dispatch_id> --json`.
  Do not silently skip cleanup.

If a handle returns `terminal_handle_stale`, re-resolve and continue with the replacement
**only** — never dual-send to old and new handles:

```bash
ORCA terminal list --worktree current --json
```

## Teardown (end of pipeline)

```bash
ORCA orchestration worker-release --dispatch <planner_dispatch_id> --json
ORCA orchestration worker-release --dispatch <builder_dispatch_id> --json
ORCA orchestration task-list --json      # confirm every task settled
```

`worker-retain` earlier does not block this — passing the same Dispatch to
`worker-release` clears the retention and releases the terminal.

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
- If `worker-start` exits nonzero, inspect `stage`, `effects`, and `residualResources`
  in the JSON. Do not auto-retry; a retry needs explicit
  `--retry-of <dispatch_id>` plus a restated `--worktree` and `--agent`/`--terminal`.
- Reuse the Step 0 staffing for the whole pipeline. If the user wants a pinned role's
  model changed mid-run, that requires releasing and relaunching that role — say so
  before doing it, since the accumulated session context is lost.
- Report the truth at the end: which units completed, which roles were reused vs
  relaunched, and every reviewer concern the builder declined to address.
