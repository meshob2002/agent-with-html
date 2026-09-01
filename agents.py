"""
멀티 에이전트 구성 (4개 Agent API + LLM 라우터).

에이전트 (각각 별도로 배포된 Agent API, 서로 다른 app_id, 동일한 AgentClient 계약):
  - router   : 사용자 요청과 현재 상태를 보고 "다음에 무엇을 할지" 결정 (슈퍼바이저)
  - analysis : 내부망 Jupyter 커널과 코드블록을 주고받으며 데이터를 분석
  - sql      : SQL 쿼리를 직접 실행하고 결과(표/다운로드 링크)를 반환
  - html     : 분석 결과를 받아 완성된 standalone HTML 보고서를 생성

이 모듈은 각 에이전트의 "호출 + 응답 파싱" 을 담당한다. 흐름 제어(누가 언제 호출되는지)는
orchestrator.py 가 맡는다. 사내 API 에 실제로 접속할 수 없는 환경을 위해 목(mock) 구현도 제공한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from agent_client import AgentClient, extract_code_blocks

# ----------------------------------------------------------------------
# 실행 결과 → 에이전트 전송용 텍스트 (원본 프로젝트와 동일 규약)
# ----------------------------------------------------------------------
MAX_FEEDBACK_CHARS = 6000


def _truncate(text: str, limit: int = MAX_FEEDBACK_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = tail = limit // 2
    return text[:head] + f"\n... (중략: 총 {len(text)}자) ...\n" + text[-tail:]


def format_exec_result(result: dict) -> str:
    """커널 실행 결과 dict → 에이전트에 되돌려 보낼 텍스트."""
    parts = []
    if result.get("stdout"):
        parts.append("--- stdout ---\n" + result["stdout"])
    if result.get("stderr"):
        parts.append("--- stderr ---\n" + result["stderr"])
    if result.get("result") is not None:
        parts.append("--- 마지막 표현식 값 ---\n" + str(result["result"]))
    if result.get("images"):
        parts.append(f"--- 이미지 ---\n(그래프 {len(result['images'])}개 생성됨)")
    if result.get("error"):
        parts.append("--- error ---\n" + result["error"])
    if not parts:
        parts.append("(출력 없음 - 정상 실행됨)")
    return f"[코드 실행 결과]\nstatus: {result.get('status', '?')}\n" + _truncate("\n".join(parts))


# ======================================================================
# 에이전트 묶음
# ======================================================================
@dataclass
class AgentSet:
    """4개 에이전트 클라이언트. 각 원소는 .run(query, conversation_id) 계약을 만족."""
    router: object
    analysis: object
    sql: object
    html: object


def build_agents(base_url: str, headers: dict, app_ids: dict, verify: bool = False) -> AgentSet:
    """
    config 로부터 4개 AgentClient 를 만든다.
    app_ids = {"router": ..., "analysis": ..., "sql": ..., "html": ...}
    """
    def mk(role: str):
        return AgentClient(base_url=base_url, app_id=app_ids.get(role, ""),
                           headers=headers, verify=verify)
    return AgentSet(mk("router"), mk("analysis"), mk("sql"), mk("html"))


# ======================================================================
# 라우터: 다음 행동 결정
# ======================================================================
VALID_ACTIONS = ("analysis", "sql", "html", "finish")

_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def parse_router_decision(message: str) -> Optional[dict]:
    """
    라우터 응답에서 {"action": ..., "reason": ...} 를 추출한다.
    JSON 우선, 실패 시 키워드 스캔. 유효 action 이 없으면 None.
    """
    if not message:
        return None
    # 1) JSON 객체 시도 (가장 마지막 것 우선 — 설명 뒤 결론 형태 대응)
    for m in reversed(_JSON_OBJ_RE.findall(message)):
        try:
            obj = json.loads(m)
        except json.JSONDecodeError:
            continue
        action = str(obj.get("action", "")).strip().lower()
        if action in VALID_ACTIONS:
            return {"action": action, "reason": str(obj.get("reason", "")).strip()}
    # 2) 키워드 스캔
    low = message.lower()
    for action in VALID_ACTIONS:
        if action in low:
            return {"action": action, "reason": "(키워드 추출)"}
    return None


def router_prompt(state_summary: str) -> str:
    """라우터에 보낼 상태 요약 프롬프트. (라우터 프롬프트 규약은 외부 Agent 에 등록돼 있다고 가정)"""
    return (
        "다음은 현재 작업 상태다. 다음에 수행할 단일 행동을 결정해라.\n"
        f"{state_summary}\n\n"
        '반드시 JSON 한 줄로만 답하라: {"action": "analysis|sql|html|finish", "reason": "..."}\n'
        "- analysis: 데이터 분석(Jupyter 커널)이 필요\n"
        "- sql: SQL 쿼리를 실행해 데이터를 가져와야 함\n"
        "- html: 분석 결과로 최종 HTML 보고서를 생성\n"
        "- finish: 모든 작업 완료"
    )


# ======================================================================
# 각 에이전트 실행 로직
# ======================================================================
def run_analysis(agent, kernel, task: str, conversation_id: Optional[str],
                 max_steps: int = 8, exec_timeout: int = 120,
                 on_step: Optional[Callable[[dict], None]] = None) -> dict:
    """
    분석 에이전트 ↔ Jupyter 커널 루프.
    에이전트가 ```python 코드블록을 주면 커널에서 실행 → 결과를 되돌려주며 반복.
    코드블록 없이 응답하면 그것을 최종 분석으로 간주한다.

    Returns: {"text": 최종분석텍스트, "images": [b64...], "conversation_id": str, "steps": n}
    """
    def emit(ev):
        if on_step:
            on_step(ev)

    query = task
    images: list[str] = []
    for step in range(1, max_steps + 1):
        reply = agent.run(query, conversation_id)
        conversation_id = reply["conversation_id"]
        message = reply["message"]
        blocks = extract_code_blocks(message)

        if not blocks:
            emit({"agent": "analysis", "kind": "final", "text": message})
            return {"text": message, "images": images,
                    "conversation_id": conversation_id, "steps": step}

        emit({"agent": "analysis", "kind": "message", "text": message, "data": {"step": step}})

        combined = {"status": "ok", "stdout": "", "stderr": "", "result": None,
                    "images": [], "error": None}
        for code in blocks:
            r = kernel.execute(code, timeout=exec_timeout)
            combined["stdout"] += r["stdout"]
            combined["stderr"] += r["stderr"]
            combined["images"] += r["images"]
            if r["result"] is not None:
                combined["result"] = r["result"]
            if r["status"] != "ok":
                combined["status"] = r["status"]
                combined["error"] = r["error"]
                break
        images += combined["images"]
        emit({"agent": "kernel", "kind": "exec", "text": format_exec_result(combined),
              "data": {"status": combined["status"], "n_images": len(combined["images"])}})
        query = format_exec_result(combined)

    # 최대 스텝 도달
    return {"text": f"(분석 최대 반복 {max_steps} 스텝 도달)", "images": images,
            "conversation_id": conversation_id, "steps": max_steps}


def run_sql(agent, request: str, conversation_id: Optional[str],
            base_url: str = "", download_headers: Optional[dict] = None,
            on_step: Optional[Callable[[dict], None]] = None) -> dict:
    """
    SQL 에이전트를 호출한다. SQL Agent 는 쿼리를 '직접 실행' 하고 결과를 반환한다고 가정.
    응답에 CSV 다운로드 링크가 있으면 내려받아 DataFrame 으로 확보한다.

    Returns: {"text": 응답텍스트, "csv_path": str|None, "df": DataFrame|None,
              "links": [...], "conversation_id": str}
    """
    from analyzer import download_csv, extract_links  # 지연 임포트(순환 방지)

    reply = agent.run(request, conversation_id)
    conversation_id = reply["conversation_id"]
    message = reply["message"]
    links = extract_links(message, base_url=base_url)

    df = None
    csv_path = None
    csv_links = [l for l in links if l["is_csv"]]
    if csv_links:
        try:
            df, enc, _ = download_csv(csv_links[0]["url"], headers=download_headers, verify=False)
        except Exception as e:
            if on_step:
                on_step({"agent": "sql", "kind": "error", "text": f"CSV 다운로드 실패: {e}"})

    if on_step:
        on_step({"agent": "sql", "kind": "result", "text": message,
                 "data": {"has_df": df is not None, "links": len(links)}})
    return {"text": message, "df": df, "csv_path": csv_path, "links": links,
            "conversation_id": conversation_id}


_HTML_FENCE_RE = re.compile(r"```(?:html)?\s*\n(.*?)```", re.DOTALL)


def run_html(agent, analysis_text: str, conversation_id: Optional[str],
             extra_context: str = "",
             on_step: Optional[Callable[[dict], None]] = None) -> dict:
    """
    HTML 생성 에이전트를 호출해 완성된 standalone HTML 문서를 받는다.
    응답이 ```html ... ``` 로 감싸져 오면 코드펜스를 벗겨낸다.

    Returns: {"html": str, "conversation_id": str}
    """
    query = ("아래 분석 결과로 완성된 standalone HTML 보고서를 생성해줘.\n\n"
             "[분석 결과]\n" + analysis_text)
    if extra_context:
        query += "\n\n[참고]\n" + extra_context

    reply = agent.run(query, conversation_id)
    conversation_id = reply["conversation_id"]
    message = reply["message"]

    m = _HTML_FENCE_RE.search(message)
    html = m.group(1).strip() if m else message.strip()
    if on_step:
        on_step({"agent": "html", "kind": "final", "text": f"HTML 보고서 생성 완료 ({len(html):,}자)"})
    return {"html": html, "conversation_id": conversation_id}


# ======================================================================
# 목(mock) 에이전트 — 사내망 없이 전체 흐름 개발/데모/테스트용
# ======================================================================
class _MockReply:
    pass


class MockAgent:
    """
    역할별로 그럴듯한 응답을 돌려주는 목 에이전트.
    - analysis: 실제로 커널에서 돌아가는 python 코드블록을 반환(진짜 실행됨) → 최종 텍스트
    - sql     : 인라인 표 형태의 결과 반환
    - html    : 완성 HTML 문서 반환
    - router  : 상태를 보고 규칙적으로 다음 행동을 JSON 으로 반환
    """
    def __init__(self, role: str):
        self.role = role
        self._analysis_turn = 0

    def run(self, query: str, conversation_id=None, timeout: int = 300) -> dict:
        cid = conversation_id or f"mock-{self.role}"
        msg = getattr(self, f"_{self.role}")(query)
        return {"message": msg, "conversation_id": cid, "raw": {"mock": True, "role": self.role}}

    def _router(self, query: str) -> str:
        low = query.lower()
        done_analysis = "analysis_done=true" in low
        done_sql = "sql_done=true" in low
        has_html = "html_done=true" in low
        need_sql = "need_sql=true" in low
        if has_html:
            action = "finish"
        elif done_analysis:
            action = "html"
        elif need_sql and not done_sql:
            action = "sql"
        else:
            action = "analysis"
        return json.dumps({"action": action, "reason": "mock 규칙 라우팅"}, ensure_ascii=False)

    def _analysis(self, query: str) -> str:
        # 첫 턴엔 코드블록(커널 실행), 실행결과를 받은 둘째 턴엔 최종 텍스트
        if "[코드 실행 결과]" in query:
            return ("분석을 완료했습니다. 데이터의 행/열 규모와 기초 통계를 확인했으며, "
                    "수치형 컬럼의 분포와 결측 현황을 요약했습니다.")
        return (
            "먼저 데이터를 살펴보겠습니다.\n\n"
            "```python\n"
            "import pandas as pd\n"
            "try:\n"
            "    df\n"
            "except NameError:\n"
            "    df = pd.DataFrame({'demo':[1,2,3]})  # mock: 업로드/ SQL 데이터가 없을 때\n"
            "print('shape:', df.shape)\n"
            "print(df.dtypes)\n"
            "print(df.describe(include='all').head())\n"
            "```\n"
        )

    def _sql(self, query: str) -> str:
        return (
            "요청하신 쿼리를 실행했습니다. 결과 미리보기:\n\n"
            "| 지점 | 건수 | 평균금액 |\n|---|---|---|\n"
            "| 강남 | 120 | 4,150,000 |\n| 분당 | 98 | 3,880,000 |\n| 부산 | 75 | 4,020,000 |\n"
        )

    def _html(self, query: str) -> str:
        return (
            "```html\n"
            "<!doctype html><html lang=ko><head><meta charset=utf-8>"
            "<title>목 보고서</title></head><body>"
            "<h1>분석 보고서 (mock)</h1><pre>" + query.replace("<", "&lt;")[:1500] + "</pre>"
            "</body></html>\n```"
        )


def build_mock_agents() -> AgentSet:
    return AgentSet(MockAgent("router"), MockAgent("analysis"),
                    MockAgent("sql"), MockAgent("html"))
