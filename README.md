# OK Agent 멀티에이전트 × HTML 보고서

내부망 PC에서 **배포된 OK저축은행 Agent API 4개**(라우팅/분석/SQL/HTML)를 오케스트레이션해
데이터 분석과 HTML 보고서 생성을 자동화하는 앱. 분석 에이전트는 **내부망 Jupyter 커널**과
코드블록을 주고받으며 결과를 만든다.

기존 [agent-jupyter-bridge](https://github.com/meshob2002/agent-jupyter-bridge)의
`agent_client.py`(Agent API 호출부)와 `kernel_manager.py`(Jupyter 커널)를 **그대로 재사용**하고,
UI는 Streamlit 대신 **HTML + JavaScript + Python(Flask)** 로 새로 구성했다.

## 4개 에이전트

같은 BASE_URL·헤더에 **서로 다른 `app_id`** 로 호출한다. 라우터는 슈퍼바이저로,
매 단계 현재 상태를 보고 다음에 실행할 에이전트를 정한다.

| 에이전트 | 역할 |
|---|---|
| ① 라우팅 Agent | 사용자 요청과 상태를 보고 다음 행동(analysis/sql/html/finish)을 JSON 으로 결정 |
| ② 분석 Agent | 내부망 Jupyter 커널과 ` ```python ` 코드블록을 주고받으며 분석 (상태 유지 커널) |
| ③ SQL 실행 Agent | SQL 쿼리를 **직접 실행**하고 결과를 **CSV 다운로드 링크**로 반환 (앱이 내려받아 커널에 로드) |
| ④ HTML 생성 Agent | 분석 결과를 받아 **완성된 standalone HTML 보고서**를 생성 |

## 동작 흐름

```
                       ┌───────────── 라우팅 Agent (슈퍼바이저) ─────────────┐
사용자 요청/CSV ──▶ Flask 오케스트레이터 ──(상태 요약)──▶ "다음 행동?" ──┐        │
                       ▲                                                    ▼        │
                       │   ┌──────────────┬──────────────┬─────────────────┐        │
                       │   ▼ analysis      ▼ sql           ▼ html            │        │
                       │ 분석 Agent      SQL Agent       HTML Agent          │        │
                       │ ↕ Jupyter 커널   (직접 실행)      (완성 HTML)        │        │
                       │   │  결과          │ 결과→분석      │ 보고서            │        │
                       └───┴───────────────┴───────────────┴──────────(finish)┘
                                                                         │
                                                    reports/<id>.html ◀──┘  (UI iframe)
```

- **CSV 업로드** → 커널에 `df` 로 로드 → 라우터가 `analysis` → 분석 → `html` → 보고서
- **쿼리 요청**(SQL 실행 경로 ON) → 라우터가 `sql` → SQL Agent 직접 실행 →
  (CSV 링크면 다운로드해 커널에 로드) → 분석 Agent → `html` → 보고서
- 각 에이전트는 자체 `conversationId` 로 상태 유지
- 라우터 응답이 없거나 파싱 실패하면 **규칙 기반 기본 계획**으로 폴백 (안전망)
- HTML Agent 결과가 echarts 를 외부 참조하면 로컬 echarts 를 **인라인 치환**(오프라인 단독 실행)

## 멀티턴 · 대화 세션 (SQLite)

`conversation_id` 를 키로 대화 세션을 **SQLite**(`data/sessions.db`)에 저장한다.

- **멀티턴**: 같은 `conversation_id` 로 이어서 요청하면, 4개 에이전트의 `conversationId` 를
  이어받아 문맥을 유지하고, 그 대화 전용 **Jupyter 커널**의 변수(`df` 등)도 유지된다.
- **기록/복원**: 각 턴의 사용자 요청·에이전트 스텝·생성 보고서를 저장한다. UI 의 **💬 대화목록**
  에서 과거 대화를 클릭하면 전체 대화가 복원되고, 이어서 입력하면 같은 세션으로 계속된다.
- **새 대화**: **🆕 새 대화** 버튼으로 세션을 리셋한다(과거 대화는 목록에 남는다).
- 저장 위치 `data/` 는 git 에서 제외된다.

### 스트리밍 응답 (`/api/orchestrate`, NDJSON)

에이전트 결과가 나오는 즉시 한 줄씩 전송된다(`application/x-ndjson`). 줄별 JSON 예:

```json
{"type": "conversation", "conversation_id": "c-ab12…", "is_new": true}
{"type": "step", "step": {"agent": "router", "kind": "decision", "text": "다음 행동: analysis", "data": {"action": "analysis", "reason": "…"}}}
{"type": "step", "step": {"agent": "analysis", "kind": "message", "text": "```python\n…\n```"}}
{"type": "step", "step": {"agent": "kernel", "kind": "exec", "text": "[코드 실행 결과] status: ok …"}}
{"type": "done", "report_url": "/reports/<id>.html", "conversation_id": "c-ab12…", "mock": false}
```

## 목 모드 (사내망 없이 개발/데모)

4개 API·커널에 접속할 수 없어도 전체 흐름을 시연할 수 있다. UI 의 **목 모드** 체크(기본 ON) 또는
`mock=true` 로 요청하면, 역할별 목 에이전트가 그럴듯한 응답을 돌려주고 **실제 Jupyter 커널이
코드를 실행**한다. 사내망에서는 목 모드를 끄고 4개 `app_id` 를 설정한다.

## 차트 라이브러리 (ECharts, 내부망 파일)

외부 CDN을 쓰지 않으므로 **ECharts 파일을 직접 배치**한다.

- `echarts.min.js` 를 **시작 파일(`app.py`)과 같은 위치**(프로젝트 루트)에 두면 서버가 자동으로 감지해
  **인터랙티브 대시보드(ECharts)** 모드로 동작한다. (`static/` 안에 두어도 인식)
- 파일이 없으면 외부 의존 없이 **CSS 막대 그래프로 폴백** — 앱은 그대로 동작한다.
- 인식되는 파일명: `echarts.min.js`, `echart.min.js`, `echarts.js`
- 감지 여부는 서버 시작 로그와 화면 우상단 칩(`📈 ECharts 대시보드` / `📊 기본 차트`)으로 확인.
- 감지된 echarts 파일 내용은 생성되는 보고서 HTML에 **인라인 삽입**되므로, 저장·공유한 보고서도
  외부 파일 없이 단독으로 열린다.
- ECharts 파일은 저장소에 커밋하지 않는다(`.gitignore` 처리). 내부망에서 각자 배치할 것.

```
agent-with-html/
├── app.py
├── echarts.min.js   ← 여기(app.py 옆)에 두면 자동 감지
├── analyzer.py
└── ...
```

> **전제(외부 Agent 프롬프트 규약)**: 각 에이전트의 동작 규칙은 배포된 Agent 프롬프트에
> 등록돼 있다고 가정한다. 앱은 그 규약에 맞춰 입출력을 파싱한다.
> - **라우터**: `{"action":"analysis|sql|html|finish","reason":...}` JSON 으로 응답
> - **분석**: 코드가 필요하면 ` ```python ` 코드블록 하나로 응답, 실행결과를 받아 이어가고,
>   완료 시 코드블록 없이 최종 답변 (커널에 업로드 데이터가 `df` 로 로드돼 있음)
> - **SQL**: 쿼리를 직접 실행하고 결과를 **CSV 다운로드 링크**로 응답 (앱이 링크를 추출·다운로드해 커널의 `df` 로 로드)
> - **HTML**: 분석 결과로 완성된 standalone HTML 문서를 응답(``` ```html ``` 펜스 허용)

## 재사용한 핵심 계약 (agent_client.py)

```python
client = AgentClient(base_url, app_id, headers, verify=False)
reply  = client.run(query, conversation_id)
# reply = {"message": str, "conversation_id": str | None, "raw": ...}
```

요청 헤더 형태 (self-signed 인증서라 `verify=False`):

```python
{"Authorization": "Bearer <KEY>", "Content-Type": "application/json", "PROJ-KEY": <PROJECT_KEY>}
```

## 설치 및 실행

내 PC 기준(윈도우 스토어 스텁 `python` 말고 miniconda 파이썬 사용):

```powershell
C:\Users\OK\miniconda3\python.exe -m pip install -r requirements.txt
C:\Users\OK\miniconda3\python.exe app.py
```

기본적으로 `http://127.0.0.1:8000` 에서 열린다. (`HOST`, `PORT` 환경변수로 변경 가능)

## 설정

우측 상단 **⚙️ 설정** 패널에서 입력하거나, 환경변수로 지정:

| 항목 | 환경변수 | 설명 |
|---|---|---|
| BASE_URL (공통) | `AGENT_BASE_URL` | 예: `https://ok.com` |
| 라우팅 app_id | `AGENT_ROUTER_APP_ID` | ① 라우팅 Agent Global ID |
| 분석 app_id | `AGENT_ANALYSIS_APP_ID` | ② 분석 Agent Global ID |
| SQL app_id | `AGENT_SQL_APP_ID` | ③ SQL 실행 Agent Global ID |
| HTML app_id | `AGENT_HTML_APP_ID` | ④ HTML 생성 Agent Global ID |
| API Key | `AGENT_API_TOKEN` | `Authorization: Bearer <key>` 로 전송(공통) |
| Project Key | `AGENT_PROJECT_KEY` | `PROJ-KEY: <key>` 헤더로 전송(공통) |

- 4개 에이전트는 같은 BASE_URL·헤더에 **서로 다른 app_id** 로 호출된다.
- Bearer 방식이 아니면 **고급: 요청 헤더 직접 지정(JSON)** 에 헤더 전체를 넣으면 그대로 사용된다.
- UI에서 입력한 설정은 **브라우저 localStorage** 에만 저장되고, 서버에는 매 요청 시에만 전달된다.
- (레거시 단일 앱 경로 `/api/query`·`/api/analyze` 는 `AGENT_APP_ID` 를 계속 사용한다.)

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 웹 UI |
| POST | `/api/orchestrate` | **멀티에이전트 파이프라인**(라우터→분석/SQL→HTML). multipart: `request`, `file`(선택), `need_sql`, `mock`, `conversation_id`(선택), `config` → NDJSON 스트림 |
| GET | `/api/conversations` | 대화 목록(최근순) |
| GET | `/api/conversations/<cid>` | 대화 전체 기록(턴+스텝+보고서) 복원용 |
| POST | `/api/conversations/<cid>/delete` | 대화 삭제 + 해당 커널 종료 |
| POST | `/api/upload` | 빠른 분석: 로컬 CSV 업로드 → pandas 분석 보고서 (에이전트 없이) |
| POST | `/api/query` | (레거시) 단일 Agent 호출 → `{message, conversation_id, links[]}` |
| POST | `/api/analyze` | (레거시) `{url}` CSV 다운로드·분석 → 보고서 |
| GET | `/reports/<id>.html` | 생성된 보고서 파일 |
| GET | `/vendor/echarts.min.js` | 탐지된 로컬 echarts 서빙 |

## 파일 구성

- `app.py` — Flask 백엔드 (라우팅 + 엔드포인트 + 커널 수명주기)
- `agent_client.py` — **원본 재사용**. Agent API 호출, BOT 응답/코드블록 파싱
- `kernel_manager.py` — **원본 재사용**. 상태 유지 Jupyter 커널(`JupyterKernelSession`)
- `agents.py` — 4개 에이전트 클라이언트 묶음 · 각 에이전트 실행 로직 · 라우터 파싱 · 목 에이전트
- `orchestrator.py` — 라우터(슈퍼바이저) 중심 파이프라인 제어 + 규칙 기반 폴백
- `analyzer.py` — 링크 추출 · CSV 파싱(한글 인코딩 자동) · pandas 분석 · ECharts/CSS 보고서(빠른 분석용)
- `session_store.py` — 대화 세션 저장소(SQLite): conversation_id 키, 턴·스텝·에이전트 conversationId
- `templates/index.html` · `static/{style.css,app.js}` — 웹 UI
- `prompts/` — 4개 에이전트에 등록할 시스템 프롬프트(라우팅/분석/SQL/HTML)
- `reports/` — 생성된 보고서 저장 폴더 (기본 git 제외)
- `data/` — 대화 세션 SQLite DB (git 제외)

## 보고서에 담기는 내용

- KPI 카드: 행/열 수, 숫자형·범주형 열 수, 메모리
- 컬럼 개요: 타입 · 결측치(수/%) · 고유값 수
- 숫자형 통계: `describe()` + 히스토그램(분포)
- 숫자형 상관계수 히트맵 (숫자형 열 2개 이상일 때)
- 범주형 상위 값 빈도 (막대)
- 결측치 있는 컬럼 요약
- 데이터 샘플(상위 행)

## 주의

- 사내 API가 self-signed 인증서를 쓰므로 `verify=False` 로 호출한다(경고 억제).
- CSV 다운로드 링크가 인증을 요구하면 Agent 호출과 **동일한 헤더**로 내려받는다.
- 한글 데이터(cp949/euc-kr) 인코딩을 자동 시도한다: `utf-8-sig → utf-8 → cp949 → euc-kr → latin-1`.
- GitHub 저장소는 웹에서 생성하고, push는 대화형 터미널에서 수행한다(gh CLI 없음).
