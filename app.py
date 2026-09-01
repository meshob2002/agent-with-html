"""
OK Agent × HTML 보고서 브릿지 (Flask 백엔드)

동작 흐름:
  1. 사용자가 웹 UI 에서 쿼리 입력
  2. 백엔드가 Agent API(재사용 AgentClient)로 쿼리를 실행
  3. Agent 응답에서 CSV 다운로드 링크를 추출해 UI 로 반환
  4. 사용자가 링크를 선택(또는 자동)하면 백엔드가 CSV 다운로드
  5. pandas 로 분석 -> 자체 완결형 HTML 보고서 파일 생성(reports/)
  6. UI 에서 보고서 미리보기 / 새 탭 열기 / 다운로드

* Agent 프롬프트 쪽에서 "요청을 처리하고 결과 CSV 의 다운로드 링크를 응답에 포함" 하도록
  등록돼 있다고 가정한다. 앱은 사용자 입력을 그대로 전달하고, 응답에서 링크만 추출한다.

Agent 없이도 테스트할 수 있도록 로컬 CSV 업로드 분석 모드(/api/upload)도 제공한다.

실행:
    python app.py            # 기본 127.0.0.1:8000
    # 또는  flask --app app run
"""

from __future__ import annotations

import json
import os
import re
import uuid

from flask import (Flask, jsonify, render_template, request,
                   send_from_directory)

from agent_client import AgentClient
from analyzer import (analyze_dataframe, build_report_html, download_csv,
                      extract_links, find_echarts, read_csv_bytes)

# ----------------------------------------------------------------------
# 설정 기본값 (환경변수로 지정 가능 — 원본 프로젝트와 동일한 이름 사용)
# ----------------------------------------------------------------------
DEFAULT_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://aip-admin.oksavingsbank.com")
DEFAULT_APP_ID = os.environ.get("AGENT_APP_ID", "")           # (레거시 단일 앱 경로용)
DEFAULT_TOKEN = os.environ.get("AGENT_API_TOKEN", "")
DEFAULT_PROJECT_KEY = os.environ.get("AGENT_PROJECT_KEY", "")

# 멀티 에이전트: 4개 Agent API 의 app_id (각각 별도 배포)
DEFAULT_APP_IDS = {
    "router": os.environ.get("AGENT_ROUTER_APP_ID", ""),
    "analysis": os.environ.get("AGENT_ANALYSIS_APP_ID", ""),
    "sql": os.environ.get("AGENT_SQL_APP_ID", ""),
    "html": os.environ.get("AGENT_HTML_APP_ID", ""),
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ECharts 로컬 파일 탐지: 시작 파일(app.py)과 같은 위치를 최우선, static/ 도 확인.
# 파일이 있으면 대시보드를 ECharts 로, 없으면 CSS 막대로 폴백한다.
ECHARTS_PATH = find_echarts(BASE_DIR, os.path.join(BASE_DIR, "static"))

# 상태 유지 Jupyter 커널(요청 간 재사용). 분석 에이전트가 사용.
_KERNEL = None


def get_kernel():
    """상태 유지 커널을 지연 생성(요청 간 변수 유지). jupyter 미설치 시 명확한 에러."""
    global _KERNEL
    if _KERNEL is None:
        from kernel_manager import JupyterKernelSession  # 지연 임포트
        _KERNEL = JupyterKernelSession(kernel_name="python3")
    return _KERNEL


app = Flask(__name__)


# ----------------------------------------------------------------------
# 공통: 요청 본문의 config 로 헤더/클라이언트 구성
#   재사용 계약:
#     headers = {"Authorization": "Bearer <KEY>",
#                "Content-Type": "application/json",
#                "PROJ-KEY": <PROJECT_KEY>}
#     client  = AgentClient(base_url, app_id, headers, verify=False)
# ----------------------------------------------------------------------
def build_headers(cfg: dict) -> dict:
    # 고급: 헤더 전체를 JSON 으로 직접 지정한 경우 그대로 사용
    raw = (cfg.get("headers_json") or "").strip()
    if raw:
        return json.loads(raw)

    headers = {"Content-Type": "application/json"}
    token = (cfg.get("token") or DEFAULT_TOKEN).strip()
    project_key = (cfg.get("project_key") or DEFAULT_PROJECT_KEY).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if project_key:
        headers["PROJ-KEY"] = project_key
    return headers


def cfg_from_request(data: dict) -> dict:
    cfg = data.get("config") or {}
    app_ids = cfg.get("app_ids") or {}
    return {
        "base_url": (cfg.get("base_url") or DEFAULT_BASE_URL).strip(),
        "app_id": (cfg.get("app_id") or DEFAULT_APP_ID).strip(),
        "app_ids": {
            role: (app_ids.get(role) or DEFAULT_APP_IDS[role]).strip()
            for role in ("router", "analysis", "sql", "html")
        },
        "token": cfg.get("token", ""),
        "project_key": cfg.get("project_key", ""),
        "headers_json": cfg.get("headers_json", ""),
    }


def save_report(html_text: str) -> str:
    """보고서 HTML 을 파일로 저장하고 report_id(파일명 없는 uuid) 반환."""
    report_id = uuid.uuid4().hex
    path = os.path.join(REPORTS_DIR, f"{report_id}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return report_id


# ======================================================================
# 라우트
# ======================================================================
@app.route("/")
def index():
    return render_template(
        "index.html",
        default_base_url=DEFAULT_BASE_URL,
        default_app_id=DEFAULT_APP_ID,
        default_app_ids=DEFAULT_APP_IDS,
        has_token=bool(DEFAULT_TOKEN),
        has_project_key=bool(DEFAULT_PROJECT_KEY),
        echarts_name=(os.path.basename(ECHARTS_PATH) if ECHARTS_PATH else ""),
    )


@app.post("/api/query")
def api_query():
    """Agent 에 쿼리를 보내고, 응답 텍스트 + 추출된 다운로드 링크를 반환."""
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    conversation_id = data.get("conversation_id") or None
    if not query:
        return jsonify({"error": "쿼리가 비어 있습니다."}), 400

    cfg = cfg_from_request(data)
    if not cfg["app_id"]:
        return jsonify({"error": "APP_ID 를 먼저 설정하세요."}), 400

    try:
        headers = build_headers(cfg)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Headers JSON 파싱 실패: {e}"}), 400

    client = AgentClient(base_url=cfg["base_url"], app_id=cfg["app_id"],
                         headers=headers, verify=False)
    try:
        reply = client.run(query, conversation_id)
    except Exception as e:
        return jsonify({"error": f"Agent 호출 실패: {e}"}), 502

    links = extract_links(reply["message"], base_url=cfg["base_url"])
    return jsonify({
        "message": reply["message"],
        "conversation_id": reply["conversation_id"],
        "links": links,
    })


@app.post("/api/analyze")
def api_analyze():
    """다운로드 링크에서 CSV 를 받아 분석 -> HTML 보고서 생성."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    query = (data.get("query") or "").strip()
    if not url:
        return jsonify({"error": "다운로드 URL 이 없습니다."}), 400

    cfg = cfg_from_request(data)
    try:
        headers = build_headers(cfg)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Headers JSON 파싱 실패: {e}"}), 400
    # CSV 다운로드에는 Content-Type(application/json)이 방해될 수 있어 제거
    headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

    try:
        df, encoding, nbytes = download_csv(url, headers=headers, verify=False)
    except Exception as e:
        return jsonify({"error": f"CSV 다운로드/파싱 실패: {e}"}), 502

    return _build_and_respond(df, source=url, query=query,
                              encoding=encoding, nbytes=nbytes)


@app.post("/api/upload")
def api_upload():
    """로컬 CSV 업로드 분석 (Agent 없이 테스트/직접 분석용)."""
    if "file" not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    f = request.files["file"]
    raw = f.read()
    try:
        df, encoding = read_csv_bytes(raw, filename=f.filename)
    except Exception as e:
        return jsonify({"error": f"CSV 파싱 실패: {e}"}), 400
    return _build_and_respond(df, source=f.filename or "(업로드)",
                              query="직접 업로드 분석", encoding=encoding, nbytes=len(raw))


def _build_and_respond(df, source, query, encoding, nbytes):
    analysis = analyze_dataframe(df)
    title = "OK Agent 데이터 분석 보고서"
    # echarts 파일이 있으면 보고서에 인라인 삽입 -> 저장/공유해도 오프라인 단독 실행
    html_text = build_report_html(analysis, title=title, source=source, query=query,
                                  echarts_path=ECHARTS_PATH)
    report_id = save_report(html_text)
    return jsonify({
        "report_id": report_id,
        "report_url": f"/reports/{report_id}.html",
        "encoding": encoding,
        "bytes": nbytes,
        "renderer": "echarts" if ECHARTS_PATH else "css",
        "meta": analysis["meta"],
        "columns": analysis["columns"],
    })


_ECHARTS_SRC_TAG_RE = re.compile(
    r'<script[^>]*\ssrc=["\'][^"\']*echarts[^"\']*\.js["\'][^>]*>\s*</script>',
    re.IGNORECASE)


def inline_echarts_if_referenced(html_text: str) -> str:
    """HTML Agent 가 만든 문서가 echarts 를 외부 참조하면, 로컬 파일 내용으로 인라인 치환.
    (내부망/오프라인에서 보고서가 단독 실행되도록)"""
    if not ECHARTS_PATH or not _ECHARTS_SRC_TAG_RE.search(html_text):
        return html_text
    try:
        with open(ECHARTS_PATH, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception:
        return html_text
    return _ECHARTS_SRC_TAG_RE.sub("<script>\n" + src + "\n</script>", html_text, count=1)


@app.post("/api/orchestrate")
def api_orchestrate():
    """
    멀티 에이전트 파이프라인 실행.
    multipart/form-data:
      - request   : 사용자 요청 텍스트 (선택)
      - file      : CSV 업로드 (선택)
      - need_sql  : "true"/"false" — SQL 실행 경로 필요 여부
      - mock      : "true"/"false" — 사내망 없이 목 에이전트로 데모
      - config    : JSON 문자열 (base_url, app_ids{router,analysis,sql,html}, token, project_key, headers_json)
    """
    from agents import build_agents, build_mock_agents
    from orchestrator import Orchestrator

    req_text = (request.form.get("request") or "").strip()
    need_sql = (request.form.get("need_sql") or "false").lower() == "true"
    use_mock = (request.form.get("mock") or "false").lower() == "true"
    try:
        cfg = cfg_from_request({"config": json.loads(request.form.get("config") or "{}")})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"config JSON 파싱 실패: {e}"}), 400

    # 업로드 CSV (선택) → 임시 파일로 저장해 커널에 로드
    csv_path, encoding = None, "utf-8-sig"
    f = request.files.get("file")
    if f and f.filename:
        raw = f.read()
        try:
            df, encoding = read_csv_bytes(raw, filename=f.filename)
        except Exception as e:
            return jsonify({"error": f"CSV 파싱 실패: {e}"}), 400
        import tempfile
        fd, csv_path = tempfile.mkstemp(suffix=".csv", dir=REPORTS_DIR)
        os.close(fd)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        encoding = "utf-8-sig"

    if not req_text and not csv_path:
        return jsonify({"error": "요청 텍스트나 CSV 파일 중 하나는 필요합니다."}), 400

    # 에이전트 구성 (목 or 실제)
    if use_mock:
        agents = build_mock_agents()
    else:
        try:
            headers = build_headers(cfg)
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Headers JSON 파싱 실패: {e}"}), 400
        need = ["router", "analysis", "html"] + (["sql"] if need_sql else [])
        missing = [r for r in need if not cfg["app_ids"][r]]
        if missing:
            return jsonify({"error": f"다음 Agent app_id 가 없습니다: {', '.join(missing)} "
                                     f"(설정에서 입력하거나 목 모드를 켜세요)"}), 400
        agents = build_agents(cfg["base_url"], headers, cfg["app_ids"], verify=False)

    dl_headers = {}
    orch_base_url = cfg["base_url"]
    if use_mock:
        # 목 SQL 이 반환하는 상대 링크(mock/sql.csv)를 앱 자신에게서 내려받도록 origin 사용
        orch_base_url = request.url_root
    else:
        dl_headers = {k: v for k, v in build_headers(cfg).items() if k.lower() != "content-type"}

    orch = Orchestrator(agents, get_kernel(), base_url=orch_base_url,
                        download_headers=dl_headers, use_router_llm=True)
    try:
        result = orch.run(user_request=req_text, csv_path=csv_path,
                          csv_encoding=encoding, need_sql=need_sql)
    except Exception as e:
        return jsonify({"error": f"오케스트레이션 실패: {e}", "steps": orch.steps}), 500

    report_url = None
    if result["html"]:
        html_text = inline_echarts_if_referenced(result["html"])
        report_id = save_report(html_text)
        report_url = f"/reports/{report_id}.html"

    return jsonify({"steps": result["steps"], "report_url": report_url, "mock": use_mock})


@app.route("/mock/sql.csv")
def mock_sql_csv():
    """목 모드용 SQL 결과 CSV. (SQL Agent 가 반환하는 다운로드 링크의 목 대상)"""
    import io
    import random
    random.seed(7)
    branches = ["강남", "분당", "서초", "일산", "부산"]
    products = ["신용대출", "주택담보", "예금", "적금"]
    rows = ["지점,상품,건수,평균금액,연체율"]
    for b in branches:
        for p in products:
            rows.append(f"{b},{p},{random.randint(20, 200)},"
                        f"{random.randint(300, 900) * 10000},{round(random.uniform(0.01, 0.2), 3)}")
    csv = "\n".join(rows)
    return app.response_class(csv, mimetype="text/csv")


@app.route("/reports/<path:name>")
def get_report(name):
    return send_from_directory(REPORTS_DIR, name)


@app.route("/vendor/echarts.min.js")
def vendor_echarts():
    """탐지된 로컬 echarts 파일을 서빙 (보고서 미리보기용 참조 경로)."""
    if not ECHARTS_PATH:
        return ("echarts.min.js 를 찾을 수 없습니다. app.py 와 같은 위치에 두세요.", 404)
    directory, name = os.path.split(ECHARTS_PATH)
    return send_from_directory(directory, name, mimetype="application/javascript")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if ECHARTS_PATH:
        print(f"[echarts] 로컬 파일 사용: {ECHARTS_PATH}  -> ECharts 대시보드 모드")
    else:
        print("[echarts] 파일 없음 (app.py 옆에 echarts.min.js 를 두면 대시보드 모드). "
              "지금은 CSS 막대 폴백으로 동작합니다.")
    app.run(host=host, port=port, debug=bool(os.environ.get("DEBUG")))
