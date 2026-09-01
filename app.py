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
DEFAULT_APP_ID = os.environ.get("AGENT_APP_ID", "")
DEFAULT_TOKEN = os.environ.get("AGENT_API_TOKEN", "")
DEFAULT_PROJECT_KEY = os.environ.get("AGENT_PROJECT_KEY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ECharts 로컬 파일 탐지: 시작 파일(app.py)과 같은 위치를 최우선, static/ 도 확인.
# 파일이 있으면 대시보드를 ECharts 로, 없으면 CSS 막대로 폴백한다.
ECHARTS_PATH = find_echarts(BASE_DIR, os.path.join(BASE_DIR, "static"))

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
    return {
        "base_url": (cfg.get("base_url") or DEFAULT_BASE_URL).strip(),
        "app_id": (cfg.get("app_id") or DEFAULT_APP_ID).strip(),
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
