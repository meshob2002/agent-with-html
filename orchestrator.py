"""
오케스트레이터: 라우터(슈퍼바이저)를 중심으로 4개 에이전트를 지휘한다.

흐름:
  - CSV 업로드 → (커널에 df 로드) → 라우터가 analysis 지시 → 분석 Agent(커널) →
    결과를 라우터에 반환 → 라우터가 html 지시 → HTML Agent → 보고서
  - 쿼리 요청 → 라우터가 sql 지시 → SQL Agent(직접 실행) → 결과를 분석 Agent로 전달 →
    분석 → 라우터 → html → 보고서

라우터 LLM 응답이 없거나 파싱 실패하면 규칙 기반 기본 계획(default_next_action)으로 폴백한다.
각 단계는 step 이벤트로 기록되어 UI 로 그대로 전달된다.
"""

from __future__ import annotations

from typing import Optional

from agents import (AgentSet, format_exec_result, parse_router_decision,
                    router_prompt, run_analysis, run_html, run_sql)


class Orchestrator:
    def __init__(self, agents: AgentSet, kernel, base_url: str = "",
                 download_headers: Optional[dict] = None,
                 max_router_steps: int = 6, analysis_max_steps: int = 8,
                 exec_timeout: int = 120, use_router_llm: bool = True,
                 on_step=None):
        self.agents = agents
        self.kernel = kernel
        self.base_url = base_url
        self.download_headers = download_headers or {}
        self.max_router_steps = max_router_steps
        self.analysis_max_steps = analysis_max_steps
        self.exec_timeout = exec_timeout
        self.use_router_llm = use_router_llm
        self.on_step = on_step  # 스텝을 실시간으로 흘려보내는 콜백(스트리밍용)

        self.steps: list[dict] = []
        self.conv = {"router": None, "analysis": None, "sql": None, "html": None}
        self.state = {
            "has_csv": False, "need_sql": False,
            "analysis_done": False, "sql_done": False, "sql_csv_ok": False, "html_done": False,
            "analysis_text": "", "sql_text": "", "html": None,
        }

    # ------------------------------------------------------------------
    def _emit(self, ev: dict):
        self.steps.append(ev)
        if self.on_step:
            self.on_step(ev)

    def _state_summary(self) -> str:
        s = self.state
        return (
            f"has_csv={s['has_csv']} need_sql={s['need_sql']} "
            f"sql_done={s['sql_done']} sql_csv_ok={s['sql_csv_ok']} "
            f"analysis_done={s['analysis_done']} html_done={s['html_done']}\n"
            f"[사용자 요청] {s.get('user_request', '')}\n"
            f"[SQL 결과 요약] {(s['sql_text'] or '')[:400]}\n"
            f"[분석 결과 요약] {(s['analysis_text'] or '')[:400]}"
        )

    def default_next_action(self) -> str:
        """규칙 기반 기본 계획(라우터 폴백 겸 안전망)."""
        s = self.state
        if s["html_done"]:
            return "finish"
        if s["need_sql"] and not s["sql_done"]:
            return "sql"
        if not s["analysis_done"]:
            return "analysis"
        return "html"

    def _decide(self) -> dict:
        """라우터 LLM 에게 다음 행동을 묻는다. 실패 시 규칙 기반 폴백."""
        default = self.default_next_action()
        if not self.use_router_llm:
            return {"action": default, "reason": "규칙 기반(라우터 LLM 미사용)"}
        try:
            reply = self.agents.router.run(router_prompt(self._state_summary()), self.conv["router"])
            self.conv["router"] = reply["conversation_id"]
            decision = parse_router_decision(reply["message"])
        except Exception as e:
            self._emit({"agent": "router", "kind": "error",
                        "text": f"라우터 호출 실패 → 기본 계획 사용: {e}"})
            decision = None
        if not decision:
            decision = {"action": default, "reason": "라우터 응답 파싱 실패 → 기본 계획"}
        return decision

    # ------------------------------------------------------------------
    def load_csv_into_kernel(self, csv_path: str, encoding: str = "utf-8-sig",
                             var: str = "df"):
        """업로드/다운로드한 CSV 를 커널에 DataFrame(df)으로 로드한다."""
        code = (
            "import pandas as pd\n"
            f"{var} = pd.read_csv(r'''{csv_path}''', encoding='{encoding}')\n"
            f"print('{var} loaded:', {var}.shape)\n"
        )
        r = self.kernel.execute(code, timeout=self.exec_timeout)
        self._emit({"agent": "kernel", "kind": "exec",
                    "text": format_exec_result(r), "data": {"status": r["status"]}})
        self.state["has_csv"] = True
        return r

    # ------------------------------------------------------------------
    def run(self, user_request: str = "", csv_path: Optional[str] = None,
            csv_encoding: str = "utf-8-sig", need_sql: bool = False) -> dict:
        """
        파이프라인 실행. UI 는 self.steps 와 반환된 html 을 사용한다.

        Returns: {"steps": [...], "html": str|None, "state": {...}}
        """
        self.state["user_request"] = user_request
        self.state["need_sql"] = need_sql

        if csv_path:
            self.load_csv_into_kernel(csv_path, encoding=csv_encoding)

        for _ in range(self.max_router_steps):
            decision = self._decide()
            action = decision["action"]
            self._emit({"agent": "router", "kind": "decision",
                        "text": f"다음 행동: {action}", "data": decision})

            if action == "finish":
                break

            elif action == "sql":
                res = run_sql(self.agents.sql, user_request, self.conv["sql"],
                              base_url=self.base_url, download_headers=self.download_headers,
                              on_step=self._emit)
                self.conv["sql"] = res["conversation_id"]
                self.state["sql_text"] = res["text"]
                self.state["sql_done"] = True
                # 계약: SQL 결과는 CSV 다운로드 링크 → 내려받은 DataFrame 을 커널에 로드해 분석에 사용
                if res["df"] is not None:
                    import os, tempfile
                    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
                    res["df"].to_csv(p, index=False, encoding="utf-8-sig")
                    self.load_csv_into_kernel(p, encoding="utf-8-sig")
                    self.state["sql_csv_ok"] = True

            elif action == "analysis":
                task = self._analysis_task(user_request)
                res = run_analysis(self.agents.analysis, self.kernel, task,
                                   self.conv["analysis"], max_steps=self.analysis_max_steps,
                                   exec_timeout=self.exec_timeout, on_step=self._emit)
                self.conv["analysis"] = res["conversation_id"]
                self.state["analysis_text"] = res["text"]
                self.state["analysis_images"] = res.get("images", [])
                self.state["analysis_done"] = True

            elif action == "html":
                if not self.state["analysis_done"]:
                    # 분석 없이 html 요구 시 분석 먼저 (안전망)
                    self._emit({"agent": "router", "kind": "decision",
                                "text": "분석 결과가 없어 analysis 먼저 수행", "data": {"action": "analysis"}})
                    continue
                res = run_html(self.agents.html, self.state["analysis_text"],
                               self.conv["html"], extra_context=self.state["sql_text"],
                               on_step=self._emit)
                self.conv["html"] = res["conversation_id"]
                self.state["html"] = res["html"]
                self.state["html_done"] = True
                break  # HTML 생성이 종착점

        return {"steps": self.steps, "html": self.state["html"], "state": self.state}

    def _analysis_task(self, user_request: str) -> str:
        """분석 에이전트에 보낼 작업 지시(업로드/ SQL 결과 컨텍스트 포함)."""
        parts = []
        if self.state["has_csv"]:
            parts.append("업로드된 데이터가 커널에 pandas DataFrame `df` 로 로드되어 있다.")
        if self.state["sql_done"] and self.state["sql_text"]:
            parts.append("아래는 SQL 실행 결과다:\n" + self.state["sql_text"][:1500])
        parts.append("[사용자 요청]\n" + (user_request or "데이터를 분석해줘."))
        return "\n\n".join(parts)
