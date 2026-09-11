# orca-pipeline

**기획 → 기획검수 → 실행**, 세 역할을 고정된 Orca 에이전트 세션에 붙여 돌리는 스킬.

> A three-role Orca orchestration skill (plan → review → build) that **pins one agent
> terminal per role and reuses it across tasks**, instead of spawning a fresh agent
> session for every task.

---

## 무엇을 해결하는가

Orca 오케스트레이션을 그냥 쓰면 태스크마다 에이전트 세션이 새로 뜹니다. 버그가 아니라
문서화된 기본 동작입니다:

> *"Current and existing worktrees never rerun setup; **a fresh agent terminal is created
> unless `--terminal` is explicit**."* — `orca orchestration worker-start --help`

원인은 두 개입니다.

1. `worker-start --worktree current`는 호출마다 **새 에이전트 터미널**을 만든다.
   재사용 경로는 `--terminal <handle>` 하나뿐이다.
2. `worker_done` 뒤에 권장되는 `worker-release`가 **그 터미널을 닫는다.** 그래서 다음
   태스크는 필연적으로 차가운 세션에서 시작한다.

결과적으로 기획자는 매번 프로젝트를 처음 보고, 실행자는 매번 코드베이스를 처음 읽습니다.

이 스킬은 역할별 터미널 핸들을 첫 실행에서 잡아두고(`worker-show` →
`worker.agent_terminal_handle`), 이후 모든 태스크를 `--terminal <handle>`로 보냅니다.
고정 역할은 파이프라인이 끝날 때까지 release하지 않고, 유휴 구간에는 `worker-retain`으로
보존을 명시적으로 기록합니다.

## 역할 편성

| 역할 | 세션 정책 | 왜 |
|---|---|---|
| **기획** planner | **고정** — 한 번 띄우고 재사용 | 프로젝트 컨텍스트 누적이 계획 품질에 직결 |
| **기획검수** reviewer | **매번 새 세션** | 계획이 쓰여지는 과정을 못 본 상태로 판단해야 편향이 없다 |
| **실행** builder | **고정** — 한 번 띄우고 재사용 | 코드베이스 이해 누적 |

검수자가 차가운 것은 의도입니다. 계획을 쓴 세션이 그 계획을 검수하면 통과 편향이 생깁니다.

반려(`VERDICT: REJECT`)는 파이프라인을 멈추지 않고 기획으로 되돌리지도 않습니다. 반려
사유가 실행자 스펙의 **주의사항** 섹션으로 넘어가고, 실행자는 각 항목을 반영하거나
반영하지 않는 이유를 `worker_done`에 명시해야 합니다. 묵살은 허용되지 않습니다.

## 시작 시 설정 — 모델은 발견하고, 박아두지 않는다

스킬은 `run-create` 전에 세 역할의 **에이전트·모델·에포트**를 물어봅니다. 다만 모델
목록을 코드에 박아두지 않습니다. 모델명은 빠르게 낡습니다 — `gpt-5.5` → `gpt-5.6-luna`,
`claude-opus-4-7` → `claude-opus-5`. 박아둔 목록은 출시 다음 주에 이미 거짓입니다.

그래서 매번 각 CLI의 **현재 설정**을 읽습니다:

```bash
python skills/orca-pipeline/scripts/discover_models.py
```

| 소스 | 읽는 것 |
|---|---|
| `~/.codex/config.toml` | `model`, `model_reasoning_effort`, `[tui.model_availability_nux]`, `[profiles.*]` |
| `~/.claude/settings.json` | `model` |
| `~/.claude.json` | 세션 이력에 등장한 모델 id (참고용) |

읽기 전용이고 예외를 던지지 않습니다. 설정이 없으면 해당 필드가 `null`/`[]`이 되고
`warnings`에 사유가 담깁니다.

선택지는 항상 이 순서로 구성되고, **첫 번째가 권장값**입니다:

1. **`CLI 기본값 그대로`** — `--model`/`--effort`를 아예 생략해 에이전트 CLI가 자기
   현재 기본값을 쓰게 한다. 낡을 수 없는 유일한 선택지.
2. Codex `[profiles.*]`에 정의된 프리셋
3. Claude는 **별칭**(`opus`/`sonnet`/`haiku`) — `claude --help`가 `--model`을 *"an alias
   for the latest model"*로 문서화하므로 별칭 자체가 업데이트를 따라간다
4. Codex는 발견된 구체적 id
5. 필요한 역할에만 effort 상향 변형

검수 역할에는 **기획과 다른 제공자**를 권합니다. 같은 모델끼리는 같은 맹점을 공유합니다.

시작 시점에 묻는 이유가 있습니다 — **`--model`/`--effort`는 `--terminal`과 함께 쓸 수
없습니다.** 고정 세션의 모델은 첫 실행에서 확정되고, 재사용 호출로는 바꿀 수 없습니다.
중간에 바꾸려면 그 역할을 release → relaunch해야 하고 누적 컨텍스트가 사라집니다.

---

## 설치

### 1. 플러그인 마켓플레이스 (권장)

Claude Code 안에서:

```
/plugin marketplace add newrise0410/orca-pipeline
/plugin install orca-pipeline
```

업데이트는 `/plugin update orca-pipeline`. 버전 관리와 갱신이 내장된 유일한 경로입니다.

> 이미 `~/.claude/skills/orca-pipeline/`에 직접 넣어 쓰고 있었다면, 플러그인 설치 후
> 그 디렉터리를 지우세요. 같은 이름의 스킬이 두 벌 잡힙니다.

### 2. git clone + 심볼릭 링크

Claude Code 플러그인 시스템을 쓰지 않는 호스트(Codex, 직접 구성한 하네스 등)용.

```bash
git clone https://github.com/newrise0410/orca-pipeline.git ~/src/orca-pipeline

# Linux / macOS
ln -s ~/src/orca-pipeline/skills/orca-pipeline ~/.claude/skills/orca-pipeline

# Windows (PowerShell, 관리자 권한 또는 개발자 모드)
New-Item -ItemType SymbolicLink `
  -Path "$env:USERPROFILE\.claude\skills\orca-pipeline" `
  -Target "$HOME\src\orca-pipeline\skills\orca-pipeline"
```

심볼릭 링크라 `git pull`이 곧 업데이트입니다.

### 3. 복사

링크가 부담스러우면 그냥 복사해도 됩니다. 대신 업데이트를 직접 챙겨야 합니다.

```bash
cp -r skills/orca-pipeline ~/.claude/skills/
```

### npm 전역설치를 쓰지 않는 이유

스킬은 마크다운 파일 하나입니다. npm 전역설치는 이 배포에 맞지 않습니다.

- **런타임 의존성이 생긴다.** JS가 한 줄도 없는데 Node를 요구하게 된다.
- **파일을 원하는 곳에 놓을 수 없다.** npm 패키지가 `~/.claude/skills/`에 쓰려면
  `postinstall`로 자기 패키지 디렉터리 밖에 쓰는 수밖에 없다. `--ignore-scripts`면
  조용히 아무 일도 일어나지 않고, 요즘 npm 생태계는 postinstall 부작용을 줄이는
  방향으로 가고 있다.
- **갱신이 수동이다.** 사용자가 `npm update -g`를 기억해야 한다. 플러그인
  마켓플레이스는 `/plugin update`가 내장이다.
- **발견 경로가 어긋난다.** Claude Code 사용자는 스킬을 npm에서 찾지 않는다.

한 줄 설치가 목적이라면 npm보다 `/plugin marketplace add`가 이미 더 짧습니다.

---

## 사용

```
/orca-pipeline
```

또는 자연어로 — "파이프라인으로 돌려줘", "기획→검수→실행으로", "아스트라로 기획하고
클로드로 검수해줘", "세션 재사용해서 진행" 등에서 자동으로 걸립니다.

일감 하나(unit)마다 기획 → 검수 → 실행이 한 바퀴 돌고, 다음 unit은 같은 기획·실행
세션을 재사용합니다.

## 요구사항

- **Orca** 1.4.x 이상, 런타임 실행 중 (`orca status --json` → `state: ready`)
- Settings → Experimental 에서 **orchestration** 활성화
- 코디네이터(루트) 터미널에서 실행. nested worker depth 기본값이 1이라 워커 안에서
  호출하면 `nested_worker_depth_exceeded`로 실패합니다 — 이건 우회하지 말고 보고하고
  멈추는 것이 맞습니다.
- 각 역할에 쓸 에이전트 CLI가 PATH에 있어야 합니다. `--model`/`--effort`는 **Claude,
  Codex, Cursor**에만 전달됩니다. 다른 에이전트를 고르면 스킬이 두 플래그를 떼고
  그 사실을 알립니다.

## 라이선스

MIT — [LICENSE](./LICENSE)
