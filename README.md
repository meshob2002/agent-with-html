# OK Agent × HTML 데이터 분석 보고서

내부망 PC에서 **배포된 OK저축은행 Agent API**(프롬프트만 존재, LLM 다이렉트 콜 불가)에
쿼리를 실행시켜 **결과 CSV 다운로드 링크**를 받고, 그 CSV를 내려받아 **pandas로 분석**한 뒤
**자체 완결형(offline) HTML 보고서**를 생성하는 앱.

기존 [agent-jupyter-bridge](https://github.com/meshob2002/agent-jupyter-bridge)의
`agent_client.py`(Agent API 호출부)를 **그대로 재사용**하고, UI는 Streamlit 대신
**HTML + JavaScript + Python(Flask)** 로 새로 구성했다.

## 동작 흐름

```
사용자 요청 (웹 UI)
   │
   ▼  POST /api/query
Flask ──────(사용자 입력 그대로)──────▶ Agent API (AgentClient.run)
   ▲                                      │
   │                         응답 텍스트 + CSV 다운로드 링크
   │                                      ▼
   │                          extract_links() 로 링크 추출
   ▼  POST /api/analyze
CSV 다운로드(verify=False) → pandas 분석 → standalone HTML 보고서(reports/)
   │
   ▼
UI 우측 iframe 미리보기 + 새 탭 / 다운로드
```

- Agent 대화는 `conversationId` 로 상태 유지 (이어서 지시 가능)
- CSV 링크가 CSV로 보이면(`.csv`/`download`/`export` 등) **자동 분석** 옵션 지원
- Agent 없이도 테스트 가능: **로컬 CSV 업로드 분석**(`/api/upload`)
- 보고서는 외부 CDN 없이 인라인 CSS/JS로만 그려져 **오프라인·내부망에서도** 파일 하나로 열림

> **전제**: 코드 실행 루프 대신, "요청을 처리하고 **결과 CSV의 다운로드 링크를 응답에 포함**"
> 하는 규칙이 외부 Agent 프롬프트에 등록돼 있다고 가정한다. 앱은 사용자 입력을 그대로 전달하고
> 응답에서 링크만 추출한다.

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
| BASE_URL | `AGENT_BASE_URL` | 예: `https://aip-admin.oksavingsbank.com` |
| APP_ID | `AGENT_APP_ID` | LLM 앱 Global ID (예: `TExNQXBwOjZh...`) |
| API Key | `AGENT_API_TOKEN` | `Authorization: Bearer <key>` 로 전송 |
| Project Key | `AGENT_PROJECT_KEY` | `PROJ-KEY: <key>` 헤더로 전송 |

- Bearer 방식이 아니면 **고급: 요청 헤더 직접 지정(JSON)** 에 헤더 전체를 넣으면 그대로 사용된다.
- UI에서 입력한 설정은 **브라우저 localStorage** 에만 저장되고, 서버에는 매 요청 시에만 전달된다.

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 웹 UI |
| POST | `/api/query` | Agent 호출 → `{message, conversation_id, links[]}` |
| POST | `/api/analyze` | `{url}` CSV 다운로드·분석 → 보고서 생성 |
| POST | `/api/upload` | 로컬 CSV 업로드 분석 (Agent 없이) |
| GET | `/reports/<id>.html` | 생성된 보고서 파일 |

## 파일 구성

- `app.py` — Flask 백엔드 (라우팅 + Agent 호출 + 다운로드/분석 오케스트레이션)
- `agent_client.py` — **원본 재사용**. Agent API 호출, BOT 응답 파싱
- `analyzer.py` — 링크 추출 · CSV 다운로드/파싱(한글 인코딩 자동) · pandas 분석 · HTML 보고서 생성
- `templates/index.html` — 웹 UI
- `static/style.css`, `static/app.js` — 프론트엔드 스타일 / 로직
- `reports/` — 생성된 보고서 저장 폴더 (기본 git 제외)

## 보고서에 담기는 내용

- KPI 카드: 행/열 수, 숫자형·범주형 열 수, 메모리
- 컬럼 개요: 타입 · 결측치(수/%) · 고유값 수
- 숫자형 통계: `describe()` + 히스토그램(분포)
- 범주형 상위 값 빈도 (막대)
- 결측치 있는 컬럼 요약
- 데이터 샘플(상위 행)

## 주의

- 사내 API가 self-signed 인증서를 쓰므로 `verify=False` 로 호출한다(경고 억제).
- CSV 다운로드 링크가 인증을 요구하면 Agent 호출과 **동일한 헤더**로 내려받는다.
- 한글 데이터(cp949/euc-kr) 인코딩을 자동 시도한다: `utf-8-sig → utf-8 → cp949 → euc-kr → latin-1`.
- GitHub 저장소는 웹에서 생성하고, push는 대화형 터미널에서 수행한다(gh CLI 없음).
