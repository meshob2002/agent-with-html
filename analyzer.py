"""
CSV 다운로드 · 분석 · HTML 보고서 생성 모듈.

역할:
  1) Agent 응답 텍스트에서 CSV 다운로드 링크를 추출          -> extract_links()
  2) 링크(또는 업로드된 바이트)에서 CSV 를 읽어 DataFrame 화 -> download_csv() / read_csv_bytes()
  3) DataFrame 을 분석해 JSON 직렬화 가능한 dict 로 요약      -> analyze_dataframe()
  4) 분석 결과로 대시보드형 HTML 보고서 생성                  -> build_report_html()

차트 렌더링:
  - `echarts.min.js`(또는 `echart.min.js`)가 있으면 ECharts 인터랙티브 대시보드로 렌더
    (내부망 파일을 인라인 삽입 -> 저장한 보고서도 오프라인 단독 실행 가능)
  - 없으면 외부 의존 없이 인라인 CSS 막대로 폴백 (앱은 그대로 동작)
"""

from __future__ import annotations

import datetime
import html
import io
import json
import math
import os
import re
from urllib.parse import urljoin

import requests
import urllib3

try:
    import pandas as pd
except ImportError as e:  # pragma: no cover
    raise ImportError("pandas 가 필요합니다. `pip install -r requirements.txt`") from e

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ======================================================================
# 0) ECharts 로컬 파일 탐지
# ======================================================================
# 시작 파일(app.py)과 같은 위치를 최우선으로, static/ 도 함께 탐색한다.
ECHARTS_FILENAMES = ("echarts.min.js", "echart.min.js", "echarts.js")


def find_echarts(*search_dirs: str) -> str | None:
    """echarts 라이브러리 파일 경로를 찾는다. 없으면 None."""
    for d in search_dirs:
        if not d:
            continue
        for name in ECHARTS_FILENAMES:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


# ======================================================================
# 1) 링크 추출
# ======================================================================
# 마크다운 링크: [라벨](URL)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((\S+?)\)")
# 맨 URL: http(s)://...
_RAW_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")


def _looks_like_csv(url: str) -> bool:
    low = url.lower()
    return (
        low.endswith(".csv")
        or ".csv?" in low
        or "download" in low
        or "export" in low
        or "file" in low
    )


def extract_links(text: str, base_url: str = "") -> list[dict]:
    """
    Agent 응답 텍스트에서 다운로드 링크 후보를 추출한다.

    Returns:
        [{"url": str, "label": str, "is_csv": bool}, ...]
        - CSV 로 보이는 링크(is_csv=True)를 앞쪽으로 정렬
        - 상대경로 링크는 base_url 기준으로 절대경로화
        - 중복 URL 제거(등장 순서 유지)
    """
    found: list[dict] = []
    seen: set[str] = set()

    def add(url: str, label: str):
        url = (url or "").strip().rstrip(".,);]")
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            if base_url:
                url = urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))
            else:
                return  # 절대경로화 불가하면 스킵
        if url in seen:
            return
        seen.add(url)
        found.append({"url": url, "label": (label or url).strip(), "is_csv": _looks_like_csv(url)})

    for label, url in _MD_LINK_RE.findall(text or ""):
        add(url, label)

    # 마크다운으로 잡히지 않은 맨 URL 도 추가
    md_urls = {f["url"] for f in found}
    for url in _RAW_URL_RE.findall(text or ""):
        cleaned = url.strip().rstrip(".,);]")
        if cleaned not in md_urls:
            add(cleaned, cleaned)

    # CSV 로 보이는 링크 우선 정렬(안정 정렬로 원래 순서 보존)
    found.sort(key=lambda f: (not f["is_csv"],))
    return found


# ======================================================================
# 2) CSV 읽기
# ======================================================================
_ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr", "latin-1")


def read_csv_bytes(raw: bytes, filename: str = "") -> tuple["pd.DataFrame", str]:
    """
    바이트 -> DataFrame. 한글 은행 데이터를 고려해 여러 인코딩을 순차 시도한다.

    Returns: (df, 사용된 인코딩)
    """
    last_err: Exception | None = None
    for enc in _ENCODINGS:
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding=enc)
            return df, enc
        except UnicodeDecodeError as e:
            last_err = e
            continue
        except Exception as e:
            # 파싱 자체 에러(구분자 등)는 인코딩 바꿔도 동일하므로 즉시 중단
            last_err = e
            break
    raise ValueError(f"CSV 파싱 실패({filename or 'bytes'}): {last_err}")


def download_csv(url: str, headers: dict | None = None, verify: bool = False,
                 timeout: int = 300) -> tuple["pd.DataFrame", str, int]:
    """
    다운로드 링크에서 CSV 를 받아 DataFrame 으로 반환한다.
    사내 API 가 self-signed 인증서를 쓰므로 기본 verify=False.

    Returns: (df, 사용된 인코딩, 원본 바이트 수)
    """
    resp = requests.get(url, headers=headers or {}, verify=verify, timeout=timeout)
    resp.raise_for_status()
    raw = resp.content
    df, enc = read_csv_bytes(raw, filename=url)
    return df, enc, len(raw)


# ======================================================================
# 3) 분석
# ======================================================================
def _safe_num(x):
    """NaN/Inf 를 JSON 안전값(None)으로 변환."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _cell(x):
    """샘플 셀을 JSON 안전한 문자열/숫자로."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(x, (int,)):
        return x
    if isinstance(x, float):
        return _safe_num(x)
    return str(x)


def analyze_dataframe(df: "pd.DataFrame", max_categories: int = 12,
                      hist_bins: int = 10, sample_rows: int = 20) -> dict:
    """DataFrame -> JSON 직렬화 가능한 분석 요약 dict."""
    n_rows, n_cols = df.shape
    numeric_cols = df.select_dtypes(include="number").columns.tolist()

    columns = []
    for c in df.columns:
        s = df[c]
        nulls = int(s.isna().sum())
        columns.append({
            "name": str(c),
            "dtype": str(s.dtype),
            "non_null": int(s.notna().sum()),
            "nulls": nulls,
            "null_pct": round(nulls / n_rows * 100, 1) if n_rows else 0.0,
            "unique": int(s.nunique(dropna=True)),
            "is_numeric": c in numeric_cols,
        })

    # 숫자형 통계 + 히스토그램
    numeric_describe, numeric_hist = {}, {}
    for c in numeric_cols:
        s = df[c].dropna()
        if s.empty:
            continue
        desc = s.describe()
        numeric_describe[str(c)] = {
            "count": int(desc.get("count", 0)),
            "mean": _safe_num(desc.get("mean")),
            "std": _safe_num(desc.get("std")),
            "min": _safe_num(desc.get("min")),
            "p25": _safe_num(desc.get("25%")),
            "p50": _safe_num(desc.get("50%")),
            "p75": _safe_num(desc.get("75%")),
            "max": _safe_num(desc.get("max")),
        }
        try:
            if s.nunique() > 1:
                binned = pd.cut(s, bins=hist_bins)
                vc = binned.value_counts(sort=False)
                numeric_hist[str(c)] = {
                    "labels": [f"{iv.left:.4g}~{iv.right:.4g}" for iv in vc.index],
                    "counts": [int(v) for v in vc.values],
                }
        except Exception:
            pass

    # 숫자형 상관계수 행렬 (2개 이상일 때)
    correlation = None
    if len(numeric_cols) >= 2:
        try:
            corr = df[numeric_cols].corr(numeric_only=True)
            correlation = {
                "cols": [str(c) for c in corr.columns],
                "matrix": [[_safe_num(v) for v in row] for row in corr.values],
            }
        except Exception:
            correlation = None

    # 범주형 상위 값 빈도
    categorical = {}
    cat_cols = [c for c in df.columns if c not in numeric_cols]
    for c in cat_cols:
        vc = df[c].astype("object").value_counts(dropna=True)
        if vc.empty:
            continue
        top = vc.head(max_categories)
        other = int(vc.iloc[max_categories:].sum()) if len(vc) > max_categories else 0
        categorical[str(c)] = {
            "labels": [str(i) for i in top.index],
            "counts": [int(v) for v in top.values],
            "other": other,
        }

    # 샘플 (앞부분 행)
    sample_df = df.head(sample_rows)
    sample = {
        "columns": [str(c) for c in df.columns],
        "rows": [[_cell(v) for v in row] for row in sample_df.itertuples(index=False, name=None)],
    }

    try:
        mem_kb = round(df.memory_usage(deep=True).sum() / 1024, 1)
    except Exception:
        mem_kb = None

    return {
        "meta": {
            "rows": int(n_rows),
            "cols": int(n_cols),
            "memory_kb": mem_kb,
            "n_numeric": len(numeric_cols),
            "n_categorical": len(cat_cols),
        },
        "columns": columns,
        "numeric_describe": numeric_describe,
        "numeric_hist": numeric_hist,
        "correlation": correlation,
        "categorical": categorical,
        "missing": {str(c["name"]): c["nulls"] for c in columns if c["nulls"] > 0},
        "sample": sample,
    }


# ======================================================================
# 4) HTML 보고서 생성 (ECharts 대시보드, 미탐지 시 CSS 폴백)
# ======================================================================
def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_report_html(analysis: dict, title: str = "데이터 분석 보고서",
                       source: str = "", query: str = "",
                       echarts_path: str | None = None,
                       echarts_src_url: str | None = None) -> str:
    """
    분석 dict -> 대시보드 HTML 문자열.

    echarts_path     : 로컬 echarts 파일 경로. 주면 내용을 인라인 삽입(오프라인 단독 실행).
    echarts_src_url  : (인라인 대신) echarts 를 참조할 URL. 예: "/vendor/echarts.min.js".
                       echarts_path 가 우선한다. 둘 다 없으면 CSS 막대로 폴백.

    * str.format 을 쓰지 않고 토큰 치환만 사용한다(인라인 JS 의 중괄호 안전).
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if echarts_path:
        try:
            echarts_tag = "<script>\n" + _read_text(echarts_path) + "\n</script>"
        except Exception:
            echarts_tag = ""
    elif echarts_src_url:
        echarts_tag = f'<script src="{html.escape(echarts_src_url)}"></script>'
    else:
        echarts_tag = ""

    payload = json.dumps(analysis, ensure_ascii=False)

    replacements = {
        "__TITLE__": html.escape(title),
        "__GENERATED_AT__": html.escape(now),
        "__SOURCE__": html.escape(source or "(직접 업로드)"),
        "__QUERY__": html.escape(query or "-"),
        "__RENDERER__": "ECharts" if echarts_tag else "기본(CSS)",
    }
    out = _REPORT_TEMPLATE
    for k, v in replacements.items():
        out = out.replace(k, v)
    # 큰 blob 은 한 번씩만, 헤더/데이터 치환 뒤에 삽입
    out = out.replace("__DATA_JSON__", payload)
    out = out.replace("__ECHARTS_SCRIPT__", echarts_tag)
    return out


# 토큰: __TITLE__ __GENERATED_AT__ __SOURCE__ __QUERY__ __RENDERER__
#       __DATA_JSON__ __ECHARTS_SCRIPT__  (.format 미사용 -> 중괄호 그대로)
_REPORT_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --ink:#1b2430; --muted:#6b7688; --line:#e7ebf0;
    --brand:#0b6b3a; --brand2:#128a4c; --bar:#128a4c; --warn:#c0392b;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#12161c; --card:#1a2029; --ink:#e6eaf0; --muted:#96a0b0; --line:#2a323d;
             --brand:#3fce85; --brand2:#3fce85; --bar:#3fce85; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
          font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif; line-height:1.5; }
  .wrap { max-width:1140px; margin:0 auto; padding:28px 20px 80px; }
  header.rep { border-left:5px solid var(--brand); padding:6px 0 6px 16px; margin-bottom:22px; }
  header.rep h1 { margin:0 0 6px; font-size:24px; }
  header.rep .meta { color:var(--muted); font-size:13px; }
  header.rep .meta code { background:var(--line); padding:1px 6px; border-radius:5px; word-break:break-all; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:20px 0; }
  .kpi { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .kpi .v { font-size:26px; font-weight:700; color:var(--brand); }
  .kpi .k { font-size:12px; color:var(--muted); margin-top:2px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:12px;
             padding:18px 20px; margin:16px 0; }
  section h2 { margin:0 0 14px; font-size:17px; display:flex; align-items:center; gap:8px; }
  section h2 .tag { font-size:11px; color:var(--muted); font-weight:400; }
  .scroll { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--card); }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .pill { display:inline-block; font-size:11px; padding:1px 7px; border-radius:20px;
           background:var(--line); color:var(--muted); }
  .pill.num { background:rgba(18,138,76,.14); color:var(--brand2); }
  /* ECharts 차트 컨테이너 */
  .chart { width:100%; height:260px; }
  .chart.tall { height:300px; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:16px; }
  .subcard { border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .subcard h3 { margin:0 0 6px; font-size:14px; }
  /* CSS 폴백 막대 */
  .bar-row { display:grid; grid-template-columns:170px 1fr 70px; gap:10px; align-items:center;
             margin:5px 0; font-size:13px; }
  .bar-row .lbl { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--ink); }
  .bar-track { background:var(--line); border-radius:6px; height:16px; overflow:hidden; }
  .bar-fill { height:100%; background:var(--bar); border-radius:6px; }
  .bar-row .cnt { text-align:right; color:var(--muted); font-variant-numeric:tabular-nums; }
  .warn { color:var(--warn); }
  footer { text-align:center; color:var(--muted); font-size:12px; margin-top:30px; }
  .empty { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="rep">
    <h1>📊 __TITLE__</h1>
    <div class="meta">
      생성: __GENERATED_AT__ &nbsp;·&nbsp; 소스: <code>__SOURCE__</code>
      &nbsp;·&nbsp; 렌더러: <code>__RENDERER__</code><br>
      요청: <code>__QUERY__</code>
    </div>
  </header>
  <div id="app"></div>
  <footer>OK Agent × HTML 보고서 · 내부망/오프라인 대시보드</footer>
</div>

<script id="report-data" type="application/json">__DATA_JSON__</script>
__ECHARTS_SCRIPT__
<script>
(function () {
  var D = JSON.parse(document.getElementById("report-data").textContent);
  var app = document.getElementById("app");
  var EC = window.echarts || null;               // 로컬 echarts 탐지
  var charts = [];                               // resize 대상
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var COL = dark
    ? { brand:"#3fce85", brand2:"#8fd6b0", axis:"#96a0b0", line:"#2a323d", text:"#e6eaf0",
        heat:["#22303a","#3fce85"] }
    : { brand:"#128a4c", brand2:"#7fb79b", axis:"#6b7688", line:"#e7ebf0", text:"#1b2430",
        heat:["#eef5f1","#0b6b3a"] };

  var esc = function (s) { return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); };
  var fmt = function (n) {
    if (n === null || n === undefined) return "-";
    if (typeof n !== "number") return esc(n);
    if (Math.abs(n) >= 1000 || Number.isInteger(n)) return n.toLocaleString("ko-KR");
    return (Math.round(n * 10000) / 10000).toLocaleString("ko-KR");
  };
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }
  function section(title, tag) {
    var s = el("section");
    s.appendChild(el("h2", null, esc(title) + (tag ? ' <span class="tag">' + esc(tag) + "</span>" : "")));
    app.appendChild(s);
    return s;
  }

  // ---- ECharts 헬퍼 ----
  function mkChart(host) {
    var c = EC.init(host, null, { renderer: "canvas" });
    charts.push(c);
    return c;
  }
  var axisCommon = function () {
    return {
      axisLine: { lineStyle: { color: COL.axis } },
      axisLabel: { color: COL.text },
      splitLine: { lineStyle: { color: COL.line } },
    };
  };
  function barChart(host, labels, counts, horizontal) {
    var cat = { type: "category", data: labels };
    var val = { type: "value" };
    Object.assign(cat, axisCommon()); Object.assign(val, axisCommon());
    if (horizontal) cat.inverse = true;
    var opt = {
      grid: { left: 6, right: 18, top: 12, bottom: 6, containLabel: true },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: horizontal ? val : cat,
      yAxis: horizontal ? cat : val,
      series: [{
        type: "bar", data: counts, barMaxWidth: 26,
        itemStyle: { color: COL.brand, borderRadius: horizontal ? [0,4,4,0] : [4,4,0,0] },
      }],
    };
    if (!horizontal) { opt.xAxis.axisLabel.rotate = 40; opt.xAxis.axisLabel.fontSize = 10; }
    mkChart(host).setOption(opt);
  }
  function heatmap(host, cols, matrix) {
    var data = [];
    for (var i = 0; i < cols.length; i++)
      for (var j = 0; j < cols.length; j++)
        data.push([j, i, matrix[i][j] == null ? "-" : Math.round(matrix[i][j] * 100) / 100]);
    mkChart(host).setOption({
      grid: { left: 6, right: 12, top: 6, bottom: 6, containLabel: true },
      tooltip: { position: "top" },
      xAxis: { type: "category", data: cols, splitArea: { show: true },
               axisLabel: { color: COL.text, rotate: 40, fontSize: 10 }, axisLine: { lineStyle: { color: COL.axis } } },
      yAxis: { type: "category", data: cols, inverse: true, splitArea: { show: true },
               axisLabel: { color: COL.text, fontSize: 10 }, axisLine: { lineStyle: { color: COL.axis } } },
      visualMap: { min: -1, max: 1, calculable: true, orient: "horizontal", left: "center", bottom: 0,
                   inRange: { color: ["#c0392b", "#f2f2f2", COL.brand] }, textStyle: { color: COL.text } },
      series: [{ type: "heatmap", data: data,
                 label: { show: true, color: "#111", fontSize: 10 },
                 emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,.3)" } } }],
    });
  }

  // ---- CSS 폴백 막대 ----
  function cssBars(container, labels, counts, extra) {
    var max = Math.max.apply(null, counts.concat([1]));
    labels.forEach(function (lb, i) {
      var row = el("div", "bar-row");
      row.appendChild(el("div", "lbl", esc(lb)));
      var track = el("div", "bar-track");
      var fill = el("div", "bar-fill");
      fill.style.width = (counts[i] / max * 100).toFixed(1) + "%";
      track.appendChild(fill); row.appendChild(track);
      row.appendChild(el("div", "cnt", fmt(counts[i])));
      container.appendChild(row);
    });
    if (extra) container.appendChild(el("div", "empty", extra));
  }
  // 막대: echarts 있으면 차트, 없으면 css
  function drawBars(host, labels, counts, horizontal, extra) {
    if (EC) {
      var div = el("div", "chart");
      host.appendChild(div);
      barChart(div, labels, counts, horizontal);
      if (extra) host.appendChild(el("div", "empty", extra));
    } else {
      cssBars(host, labels, counts, extra);
    }
  }

  // ---------------- 렌더 ----------------
  // KPI
  var m = D.meta;
  var cards = el("div", "cards");
  [["행 수", m.rows], ["열 수", m.cols], ["숫자형 열", m.n_numeric],
   ["범주형 열", m.n_categorical], ["메모리(KB)", m.memory_kb]]
    .forEach(function (p) {
      var c = el("div", "kpi");
      c.appendChild(el("div", "v", fmt(p[1])));
      c.appendChild(el("div", "k", esc(p[0])));
      cards.appendChild(c);
    });
  app.appendChild(cards);

  // 컬럼 개요 (표)
  var s1 = section("컬럼 개요", D.columns.length + "개 열");
  var w1 = el("div", "scroll");
  var t1 = el("table");
  t1.innerHTML = "<thead><tr><th>#</th><th>컬럼</th><th>타입</th>" +
    '<th class="num">결측</th><th class="num">결측%</th><th class="num">고유값</th></tr></thead>';
  var tb1 = el("tbody");
  D.columns.forEach(function (c, i) {
    var pill = c.is_numeric ? '<span class="pill num">num</span>' : '<span class="pill">cat</span>';
    var nullCls = c.null_pct >= 20 ? ' class="num warn"' : ' class="num"';
    tb1.appendChild(el("tr", null,
      "<td>" + (i + 1) + "</td><td><b>" + esc(c.name) + "</b></td>" +
      "<td>" + pill + " " + esc(c.dtype) + "</td>" +
      "<td class='num'>" + fmt(c.nulls) + "</td>" +
      "<td" + nullCls + ">" + c.null_pct + "%</td>" +
      "<td class='num'>" + fmt(c.unique) + "</td>"));
  });
  t1.appendChild(tb1); w1.appendChild(t1); s1.appendChild(w1);

  // 결측치
  var missKeys = Object.keys(D.missing || {});
  if (missKeys.length) {
    var sM = section("결측치 있는 컬럼", missKeys.length + "개");
    missKeys.sort(function (a, b) { return D.missing[b] - D.missing[a]; });
    drawBars(sM, missKeys, missKeys.map(function (k) { return D.missing[k]; }), true);
  }

  // 숫자형 통계 (표)
  var ndKeys = Object.keys(D.numeric_describe || {});
  if (ndKeys.length) {
    var s2 = section("숫자형 컬럼 통계", ndKeys.length + "개");
    var w2 = el("div", "scroll");
    var t2 = el("table");
    t2.innerHTML = "<thead><tr><th>컬럼</th>" +
      ["count","mean","std","min","25%","50%","75%","max"]
        .map(function (h) { return '<th class="num">' + h + "</th>"; }).join("") + "</tr></thead>";
    var tb2 = el("tbody");
    ndKeys.forEach(function (k) {
      var d = D.numeric_describe[k];
      tb2.appendChild(el("tr", null, "<td><b>" + esc(k) + "</b></td>" +
        ["count","mean","std","min","p25","p50","p75","max"]
          .map(function (f) { return '<td class="num">' + fmt(d[f]) + "</td>"; }).join("")));
    });
    t2.appendChild(tb2); w2.appendChild(t2); s2.appendChild(w2);

    // 히스토그램
    var hKeys = Object.keys(D.numeric_hist || {});
    if (hKeys.length) {
      var s3 = section("숫자형 분포 (히스토그램)");
      var g = el("div", "grid2");
      hKeys.forEach(function (k) {
        var sc = el("div", "subcard");
        sc.appendChild(el("h3", null, esc(k)));
        drawBars(sc, D.numeric_hist[k].labels, D.numeric_hist[k].counts, false);
        g.appendChild(sc);
      });
      s3.appendChild(g);
    }

    // 상관계수 히트맵 (ECharts 전용; 폴백 시 표)
    if (D.correlation && D.correlation.cols.length >= 2) {
      var s6 = section("숫자형 상관계수", D.correlation.cols.length + "×" + D.correlation.cols.length);
      if (EC) {
        var hd = el("div", "chart tall"); s6.appendChild(hd);
        heatmap(hd, D.correlation.cols, D.correlation.matrix);
      } else {
        var w6 = el("div", "scroll"); var t6 = el("table");
        t6.innerHTML = "<thead><tr><th></th>" +
          D.correlation.cols.map(function (c) { return '<th class="num">' + esc(c) + "</th>"; }).join("") + "</tr></thead>";
        var tb6 = el("tbody");
        D.correlation.cols.forEach(function (c, i) {
          tb6.appendChild(el("tr", null, "<td><b>" + esc(c) + "</b></td>" +
            D.correlation.matrix[i].map(function (v) {
              return '<td class="num">' + (v == null ? "-" : (Math.round(v * 100) / 100)) + "</td>"; }).join("")));
        });
        t6.appendChild(tb6); w6.appendChild(t6); s6.appendChild(w6);
      }
    }
  }

  // 범주형 상위 값
  var cKeys = Object.keys(D.categorical || {});
  if (cKeys.length) {
    var s4 = section("범주형 컬럼 상위 값", cKeys.length + "개");
    var g2 = el("div", "grid2");
    cKeys.forEach(function (k) {
      var c = D.categorical[k];
      var sc = el("div", "subcard");
      sc.appendChild(el("h3", null, esc(k)));
      var extra = c.other ? "그 외 " + fmt(c.other) + "건" : "";
      drawBars(sc, c.labels, c.counts, true, extra);
      g2.appendChild(sc);
    });
    s4.appendChild(g2);
  }

  // 데이터 샘플 (표)
  var s5 = section("데이터 샘플", "상위 " + D.sample.rows.length + "행");
  var w5 = el("div", "scroll"); var t5 = el("table");
  t5.innerHTML = "<thead><tr>" +
    D.sample.columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr></thead>";
  var tb5 = el("tbody");
  D.sample.rows.forEach(function (row) {
    tb5.appendChild(el("tr", null,
      row.map(function (v) { return "<td>" + (v === null ? '<span class="empty">·</span>' : esc(v)) + "</td>"; }).join("")));
  });
  t5.appendChild(tb5); w5.appendChild(t5); s5.appendChild(w5);

  // 리사이즈 대응
  if (EC && charts.length) {
    window.addEventListener("resize", function () {
      charts.forEach(function (c) { try { c.resize(); } catch (e) {} });
    });
  }
})();
</script>
</body>
</html>"""
