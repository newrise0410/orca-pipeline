# orca-pipeline

**기획 → 기획 검수 → 실행**, 3가지 역할을 전담 Orca 에이전트 세션에 고정하여 파이프라인 전체에서 재사용하는 오케스트레이션 스킬입니다.

> A three-role Orca orchestration skill (plan → review → build) that **pins one agent terminal per role and reuses it across tasks**, instead of spawning a fresh agent session for every task.

---

## 무엇을 해결하는가

Orca 오케스트레이션을 기본 설정대로 실행하면 태스크마다 에이전트 세션이 매번 새로 생성됩니다. 이는 버그가 아니라 공식 도움말에 명시된 기본 동작 방식입니다.

> *"Current and existing worktrees never rerun setup; **a fresh agent terminal is created unless `--terminal` is explicit**."* — `orca orchestration worker-start --help`

이러한 동작이 발생하는 주원인은 다음과 같습니다.

1. `worker-start --worktree current`는 호출할 때마다 **새로운 에이전트 터미널**을 생성합니다. 기존 세션을 재사용하는 방법은 `--terminal <handle>`을 명시하는 것뿐입니다.
2. `worker_done` 완료 후 권장되는 `worker-release`는 **해당 터미널을 즉시 종료**합니다. 이로 인해 다음 태스크는 필연적으로 컨텍스트가 없는 콜드 세션(Cold Session)에서 다시 시작됩니다.

그 결과 기획자는 매번 프로젝트의 맥락을 처음부터 파악해야 하고, 실행자 역시 매 태스크마다 코드베이스를 다시 분석해야 하는 비효율이 발생합니다.

**orca-pipeline**은 첫 태스크 실행 시 역할별 터미널 핸들을 확보(`worker-show` → `worker.agent_terminal_handle`)한 뒤, 이후의 모든 태스크를 `--terminal <handle>`로 전달합니다. 고정된 역할 세션은 파이프라인이 종료될 때까지 해제(release)하지 않으며, 작업 대기 구간에서는 `worker-retain`을 통해 세션 보존 상태를 명시적으로 유지합니다.

---

## 역할 편성

| 역할 | 세션 정책 | 정책 선정 이유 |
|---|---|---|
| **기획** (planner) | **고정** (단일 세션 재사용) | 프로젝트 컨텍스트의 연속적인 누적이 기획 완성도와 직결됨 |
| **기획 검수** (reviewer) | **매번 신규 세션** (독립 세션) | 기획 수립 과정을 모르는 제3자의 시선에서 객관적으로 검증하여 확증 편향 방지 |
| **실행** (builder) | **고정** (단일 세션 재사용) | 코드베이스 구조 및 수정 내역에 대한 이해도 누적 |

검수자 세션을 매번 새로 띄우는 것은 의도된 설계입니다. 계획을 직접 작성한 세션이 스스로 검수할 경우 무비판적으로 통과시키는 편향이 발생하기 때문입니다.

검수 단계에서 반려(`VERDICT: REJECT`)가 발생하더라도 전체 파이프라인이 중단되거나 기획 단계로 되돌아가지 않습니다. 대신 반려 사유가 실행자의 작업 명세서 내 **주의사항** 섹션으로 전달되며, 실행자는 각 지적 사항을 구현에 반영하거나 미반영 사유를 `worker_done` 보고 시 반드시 명시해야 합니다. (지적 사항 묵살 불가)

---

## 시작 시 설정: 모델 하드코딩 대신 동적 탐색(Discovery)

스킬 실행 전(`run-create`), 세 가지 역할에 할당할 **에이전트·모델·추론 강도(effort)**를 설정합니다. 이때 지원 모델 목록을 코드에 하드코딩하지 않습니다. 모델 명칭은 빠르게 갱신되기 때문입니다(`gpt-5.5` → `gpt-5.6-luna`, `claude-opus-4-7` → `claude-opus-5`). 하드코딩된 목록은 새로운 모델이 출시되는 즉시 구버전이 되어버립니다.

따라서 실행 시점마다 각 CLI의 **현재 설정 정보**를 동적으로 탐색합니다.

```bash
python skills/orca-pipeline/scripts/discover_models.py
```

| 탐색 소스 | 수집 항목 |
|---|---|
| `<codex_home>/models_cache.json` | 모델 목록 (`slug`, `display_name`, `description`, **모델별 effort**, `priority`). 단, `visibility: "hide"`인 내부 모델 제외 |
| `<codex_home>/config.toml` | 기본 설정 (`model`, `model_reasoning_effort`, `[profiles.*]`) |
| `claude --help` | `--model` 별칭(`fable`/`opus`/`sonnet`) 및 지원 `--effort` 레벨 파싱 |
| `~/.claude/settings.json` | 기본 모델 (`model`) |

이 탐색 스크립트는 읽기 전용으로 안전하게 동작하며 예외를 던져 실행을 중단시키지 않습니다. 설정 소스를 찾을 수 없는 경우 해당 필드는 `null` 또는 빈 배열(`[]`)로 처리되고 `warnings`에 그 사유가 기록됩니다.

특히 유의해야 할 두 가지 핵심 사항이 있습니다.

1. **추론 강도(Effort)는 모델별로 상이합니다.**  
   예를 들어 `gpt-5.6-sol`, `gpt-5.6-terra`는 `ultra`까지 지원하지만 `gpt-5.6-luna`는 `max`, `gpt-5.5`는 `xhigh`까지만 지원합니다. 에이전트 단위로 단일 목록을 강제하면 지원되지 않는 레벨이 선택되어 오류가 발생할 수 있습니다.
2. **`CODEX_HOME`은 계정 단위로 분리됩니다.**  
   Orca가 Codex 계정별로 환경변수를 재지정하므로, 한 머신 내에도 여러 캐시가 존재할 수 있고 **계정마다 모델 목록과 기본값이 서로 다릅니다.** (실측 예시):

| 홈 경로 | 기본값 | 확인 가능한 모델 수 |
|---|---|---|
| `~/.codex` | `gpt-5.6-luna` / `medium` | 6개 (`gpt-5.4`, `gpt-5.4-mini` 포함) |
| Orca 계정 전용 홈 | `gpt-5.6-terra` / `xhigh` | 4개 |

스크립트는 현재 활성화된 계정 홈의 목록만 수집하며, 다른 홈 경로(`other_homes`)는 단순 경로 정보만 제공합니다. 계정별 권한 범위가 다르므로 임의로 병합하지 않습니다. 모델 목록이 올바르지 않다면 `home` 및 `home_from_env` 값을 먼저 확인하세요.

선택지 목록은 다음 우선순위로 구성되며, **첫 번째 항목이 기본 권장값**입니다.

1. **`CLI 기본값 그대로`**: `--model` 및 `--effort`를 지정하지 않고 각 에이전트 CLI의 현재 기본 설정을 사용합니다. 모델 변경에 구애받지 않는 가장 안정적인 옵션입니다.
2. Codex `[profiles.*]` 프리셋
3. Claude **별칭**: `claude --help`에 *"an alias for the latest model"*로 정의되어 있어 최신 모델 업데이트가 자동 반영됩니다. 별칭 목록도 그 도움말에서 파싱하므로 이 문서에 고정해 적지 않습니다 (실측 예: `fable`/`opus`/`sonnet`).
4. Codex 동적 탐색 id
5. 특정 역할 전용 effort 상향 옵션

> 기획과 검수 역할에는 서로 다른 모델 제공사(Provider)를 배정하는 것을 권장합니다. 동일한 계열의 모델은 유사한 맹점을 공유하기 쉽기 때문입니다.

### 실행 시작 시 모델 설정을 확정하는 이유

Orca에서는 **`--model` 및 `--effort` 옵션을 세션 재사용 플래그(`--terminal`)와 함께 사용할 수 없습니다.** 고정 세션의 모델은 첫 생성 시점에 확정되며, 이후 재사용 호출로는 변경할 수 없습니다. 중간에 모델을 변경하려면 해당 역할을 해제(release)한 뒤 새로 실행(relaunch)해야 하며, 이 경우 기존에 누적된 컨텍스트가 모두 소실됩니다.

---

## 설치 방법

### 1. 플러그인 마켓플레이스 (권장)

Claude Code 환경에서 아래 명령어를 실행합니다.

```
/plugin marketplace add newrise0410/orca-pipeline
/plugin install orca-pipeline@newrise0410
```

플러그인은 `<플러그인명>@<마켓플레이스명>` 으로 식별됩니다. 이 저장소의 마켓플레이스명은
`newrise0410` 이므로 `@newrise0410` 을 빼면 플러그인을 찾지 못합니다.

- 업데이트: `/plugin update orca-pipeline@newrise0410`
- 자체 버전 관리 및 업데이트 기능을 온전히 지원하는 권장 설치 경로입니다.

> **주의:** 기존에 `~/.claude/skills/orca-pipeline/` 경로에 수동으로 복사해 사용 중이었다면 플러그인 설치 후 해당 수동 디렉터리를 삭제해 주세요. 동일한 이름의 스킬이 중복 인식될 수 있습니다.

### 2. git clone + 심볼릭 링크

Claude Code 플러그인 시스템을 사용하지 않는 환경(Codex, 자체 구축 하네스 등)에 적합합니다.

```bash
git clone https://github.com/newrise0410/orca-pipeline.git ~/src/orca-pipeline

# Linux / macOS
ln -s ~/src/orca-pipeline/skills/orca-pipeline ~/.claude/skills/orca-pipeline

# Windows (PowerShell, 관리자 권한 또는 개발자 모드 필요)
New-Item -ItemType SymbolicLink `
  -Path "$env:USERPROFILE\.claude\skills\orca-pipeline" `
  -Target "$HOME\src\orca-pipeline\skills\orca-pipeline"
```

심볼릭 링크 방식이므로 저장소에서 `git pull`만 수행하면 즉시 최신 버전으로 업데이트됩니다.

### 3. 디렉터리 직접 복사

심볼릭 링크 사용이 어려운 환경에서는 파일을 직접 복사하여 설치할 수 있습니다. (추후 업데이트는 수동으로 진행해야 합니다.)

```bash
cp -r skills/orca-pipeline ~/.claude/skills/
```

### npm 전역 설치 방식을 사용하지 않는 이유

본 스킬은 단일 마크다운 및 스크립트 기반 구성이므로 npm 전역 패키지 배포 방식은 적합하지 않습니다.

- **불필요한 런타임 의존성:** JavaScript 코드가 없음에도 사용자에게 Node.js 런타임을 요구하게 됩니다.
- **경로 배치 제약:** npm 패키지가 사용자 스킬 디렉터리(`~/.claude/skills/`)에 접근하려면 `postinstall` 스크립트를 통해 패키지 외부 영역에 파일을 써야 합니다. 이는 `--ignore-scripts` 환경에서 정상 동작하지 않으며, 최근 npm 생태계의 보안 권장 사항에도 부합하지 않습니다.
- **수동 갱신 번거로움:** 사용자가 직접 `npm update -g`를 주기적으로 실행해야 합니다. 반면 플러그인 마켓플레이스는 `/plugin update` 명령어를 내장하고 있습니다.
- **스킬 탐색 경로 불일치:** Claude Code 사용자는 필요한 스킬을 npm 저장소가 아닌 플러그인 마켓플레이스에서 탐색합니다.

한 줄 설치 편의성 측면에서도 npm보다 `/plugin marketplace add` 명령어가 더 간결합니다.

---

## 사용법

```
/orca-pipeline
```

또는 아래와 같은 자연어 프롬프트로도 자동 트리거됩니다.

- *"파이프라인으로 돌려줘"*
- *"기획 → 검수 → 실행으로 진행해줘"*
- *"코덱스로 기획하고 클로드로 검수해줘"*
- *"세션 재사용해서 진행"*

각 작업 단위(Unit)마다 **[기획 → 검수 → 실행]** 순서로 1사이클이 수행되며, 후속 작업 단위에서도 앞서 생성된 기획 및 실행 세션을 그대로 재사용하여 작업 연속성을 유지합니다.

---

## 요구사항

- **Orca** 런타임 활성화 상태 (`orca status --json` 기준 `state: ready`). 개발·실측 환경은
  1.4.197~1.4.199 입니다. 하위 호환 최소 버전은 확인하지 않았으므로, `worker-start`가
  `--model`/`--effort`/`--terminal` 조합을 거부한다면 Orca를 먼저 업데이트하세요.
- Orca 설정(Settings → Experimental) 내 **orchestration** 기능 활성화
- **루트(Coordinator) 터미널에서 실행 필수:** Orca의 기본 중첩 워커 깊이(nested worker depth) 제한은 1입니다. 워커 세션 내부에서 중첩 호출할 경우 `nested_worker_depth_exceeded` 에러로 실패합니다. (이는 비정상 우회 대신 즉시 원인을 보고하고 중단하는 것이 올바른 동작입니다.)
- 각 역할에 지정할 에이전트 CLI가 시스템 환경변수(`PATH`)에 등록되어 있어야 합니다.
- `--model` 및 `--effort` 옵션은 **Claude, Codex, Cursor** CLI에만 전달됩니다. 다른 에이전트 CLI를 지정한 경우 해당 플래그를 자동으로 제외한 뒤 안내 메시지를 출력합니다.

---

## 알려진 함정

### 기억한 모델명은 틀립니다 — 슬러그를 탐색해야 합니다

이 저장소가 모델명을 하드코딩하지 않는 이유를 실제 사례로 남깁니다.

사람이 부르는 이름("아스트라")과 CLI가 받는 슬러그는 다릅니다. 기억한 이름을 그대로 넘기면:

```
$ codex exec --model astra -c model_reasoning_effort="xhigh" "Reply with exactly: OK"
warning: Model metadata for `astra` not found. Defaulting to fallback metadata
ERROR: {"status":400,"message":"The 'astra' model is not supported when using Codex with a ChatGPT account."}
```

400 메시지가 "이 인증으로는 못 쓴다"고 말하는 바람에 권한 문제로 오진하기 쉽습니다. 실제 원인은
**슬러그가 틀린 것**이었습니다. 동적 탐색이 알려준 올바른 슬러그로는 정상 동작합니다:

```
$ codex exec --model gpt-6-astra -c model_reasoning_effort="xhigh" "Reply with exactly: OK"
codex
OK
```

탐색 결과에는 사람이 고를 수 있는 정보가 함께 담겨 있습니다:

```json
{ "slug": "gpt-6-astra", "display_name": "GPT-6-Astra",
  "description": "Our most capable model for complex, demanding work.",
  "default_effort": "low", "efforts": ["low","medium","high","xhigh","max","ultra"],
  "priority": 1 }
```

덧붙여, 이 모델은 캐시를 **갱신한 뒤에야** 목록에 나타났습니다. 캐시가 구버전 클라이언트
(0.146.0)로 기록된 동안에는 존재하지 않는 것처럼 보였습니다. 그래서 탐색 스크립트는
`cache_fetched_at` 과 `cache_client_version` 을 함께 내보냅니다 — 목록이 비어 보이면 캐시가
낡았는지 먼저 확인하세요. Codex를 한 번 실행하면 갱신됩니다.

그래서 스킬은 고정 세션을 띄우기 전에 명시적 모델을 반드시 프로브합니다(Step 0b-2). 고정 역할이
첫 요청에서 실패하면 세션 전체를 다시 띄워야 하기 때문입니다.

주의: **`codex exec`는 요청이 실패해도 종료코드 0을 반환합니다.** 종료코드로 판정하면 실패를
성공으로 읽습니다. 출력의 `ERROR:` 줄을 봐야 합니다.

### `--model`은 메커니즘이 아니라 편의 기능입니다

이 스킬의 본질은 `--terminal`로 세션을 고정하는 것입니다. `--model`은 세션이 *무엇으로 뜨는지*를
정하는 편의 플래그일 뿐입니다. MCP로 제공되는 모델, TUI 선택기로만 고를 수 있는 모델, 현재
인증으로 권한이 없는 모델이라면 — CLI 기본값으로 띄우고 세션 안에서 모델을 고르면 됩니다.
실행 플래그 하나 때문에 파이프라인을 멈추지 마십시오.

### `CODEX_HOME` 이 Orca 계정으로 재지정되어 있습니다

Orca 터미널 안에서는 `CODEX_HOME`이 Orca 계정 홈을 가리킵니다. 그 결과 Codex 공식 설치
스크립트와 `codex update`가 **자기 설치를 인식하지 못하고 실패합니다**:

```
Refusing to retarget junction at ...\Programs\OpenAI\Codex\bin because it is not managed by this installer.
Error: Could not detect the Codex installation method.
```

`...\Programs\OpenAI\Codex\bin` 은 PATH 전역 항목이라 특정 계정 홈을 가리켜선 안 되므로 이
거부는 옳은 동작입니다. Codex를 업데이트할 때는 Orca 밖의 일반 셸에서 실행하거나, 그 명령에만
변수를 덮어쓰세요:

```powershell
$env:CODEX_HOME="$env:USERPROFILE\.codex"; irm https://chatgpt.com/codex/install.ps1 | iex
```

같은 창에서 이후 Codex를 실행하면 계정 격리가 빠진 상태로 뜨므로, 업데이트 후 창을 닫으세요.

---

## 라이선스

MIT — [LICENSE](./LICENSE)
