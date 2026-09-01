/* OK Agent 멀티에이전트 프론트엔드
 * 파이프라인: 요청/CSV -> /api/orchestrate (라우터 -> 분석/SQL -> HTML) -> 보고서
 * 빠른 분석: 파일 -> /api/upload (에이전트 없이 pandas 분석)
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var LS_KEY = "okagent.multi.v1";

  var els = {
    cfgToggle: $("cfgToggle"), cfgPanel: $("cfgPanel"),
    base_url: $("base_url"), token: $("token"), project_key: $("project_key"),
    app_router: $("app_router"), app_analysis: $("app_analysis"),
    app_sql: $("app_sql"), app_html: $("app_html"), headers_json: $("headers_json"),
    query: $("query"), runBtn: $("runBtn"), needSql: $("needSql"), mockMode: $("mockMode"),
    fileInput: $("fileInput"), quickBtn: $("quickBtn"), log: $("log"),
    frame: $("reportFrame"), empty: $("reportEmpty"),
    actions: $("reportActions"), openReport: $("openReport"), dlReport: $("dlReport"),
  };

  var CFG_KEYS = ["base_url", "token", "project_key", "app_router", "app_analysis",
                  "app_sql", "app_html", "headers_json"];

  // ---- 설정 저장/복원 ----
  function loadCfg() {
    try {
      var c = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
      CFG_KEYS.forEach(function (k) {
        if (c[k] != null && els[k] && !els[k].value) els[k].value = c[k];
      });
      if (c.needSql != null) els.needSql.checked = c.needSql;
      if (c.mockMode != null) els.mockMode.checked = c.mockMode;
    } catch (e) {}
  }
  function saveCfg() {
    try {
      var o = {}; CFG_KEYS.forEach(function (k) { o[k] = els[k].value; });
      o.needSql = els.needSql.checked; o.mockMode = els.mockMode.checked;
      localStorage.setItem(LS_KEY, JSON.stringify(o));
    } catch (e) {}
  }
  function config() {
    return {
      base_url: els.base_url.value.trim(),
      token: els.token.value,
      project_key: els.project_key.value,
      headers_json: els.headers_json.value,
      app_ids: {
        router: els.app_router.value.trim(),
        analysis: els.app_analysis.value.trim(),
        sql: els.app_sql.value.trim(),
        html: els.app_html.value.trim(),
      },
    };
  }

  // ---- 로그 렌더 ----
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function addMsg(kind, who, htmlBody) {
    var d = document.createElement("div");
    d.className = "msg " + kind;
    d.innerHTML = '<div class="who">' + esc(who) + "</div>" + htmlBody;
    els.log.appendChild(d);
    d.scrollIntoView({ behavior: "smooth", block: "end" });
    return d;
  }
  function statusMsg(text) {
    return addMsg("status", "진행", '<span class="spinner"></span> ' + esc(text));
  }

  // 에이전트 스텝 시각화
  var AGENT_META = {
    router:   { icon: "🧭", name: "라우팅 Agent", cls: "a-router" },
    analysis: { icon: "🔬", name: "분석 Agent",   cls: "a-analysis" },
    sql:      { icon: "🗄️", name: "SQL Agent",    cls: "a-sql" },
    html:     { icon: "📄", name: "HTML Agent",   cls: "a-html" },
    kernel:   { icon: "🖥️", name: "Jupyter 커널", cls: "a-kernel" },
    system:   { icon: "⚙️", name: "system",       cls: "" },
  };
  function renderStep(st) {
    var meta = AGENT_META[st.agent] || AGENT_META.system;
    var who = meta.icon + " " + meta.name + (st.kind ? " · " + st.kind : "");
    var body = "";
    if (st.kind === "decision" && st.data) {
      body = "<b>→ " + esc(st.data.action) + "</b>" +
             (st.data.reason ? ' <span class="dim">(' + esc(st.data.reason) + ")</span>" : "");
    } else if (st.kind === "exec") {
      body = "<pre class='exec'>" + esc(st.text) + "</pre>";
    } else {
      body = "<div>" + esc(st.text) + "</div>";
    }
    addMsg("step " + meta.cls + (st.kind === "error" ? " error" : ""), who, body);
  }

  // ---- 보고서 표시 ----
  function showReport(url, id) {
    els.frame.src = url;
    els.frame.classList.remove("hidden");
    els.empty.classList.add("hidden");
    els.actions.classList.remove("hidden");
    els.openReport.href = url;
    els.dlReport.href = url;
    els.dlReport.setAttribute("download", "report-" + (id || "out") + ".html");
  }

  // ---- 파이프라인 실행 ----
  function run() {
    var q = els.query.value.trim();
    var file = els.fileInput.files[0];
    if (!q && !file) { els.query.focus(); return; }

    var cfg = config();
    var mock = els.mockMode.checked;
    if (!mock) {
      var need = ["router", "analysis", "html"];
      if (els.needSql.checked) need.push("sql");
      var miss = need.filter(function (r) { return !cfg.app_ids[r]; });
      if (miss.length) {
        addMsg("error", "설정 필요", "다음 Agent app_id 를 설정하세요: <b>" + miss.join(", ") +
               "</b> (또는 목 모드 사용)");
        els.cfgPanel.classList.remove("hidden");
        return;
      }
    }
    saveCfg();
    addMsg("user", "요청", esc(q || "(CSV 분석)") +
      (file ? ' <span class="dim">+ ' + esc(file.name) + "</span>" : "") +
      (mock ? ' <span class="badge">MOCK</span>' : "") +
      (els.needSql.checked ? ' <span class="badge">SQL</span>' : ""));

    els.runBtn.disabled = true;
    var st = statusMsg("파이프라인 실행 중… (라우터가 다음 단계를 결정합니다)");

    var fd = new FormData();
    fd.append("request", q);
    fd.append("need_sql", els.needSql.checked ? "true" : "false");
    fd.append("mock", mock ? "true" : "false");
    fd.append("config", JSON.stringify(cfg));
    if (file) fd.append("file", file);

    fetch("/api/orchestrate", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) { j.__status = r.status; } return j; }); })
      .then(function (res) {
        st.remove();
        (res.steps || []).forEach(renderStep);
        if (res.error) { addMsg("error", "오류", esc(res.error)); return; }
        if (res.report_url) {
          var id = res.report_url.split("/").pop().replace(".html", "").slice(0, 8);
          addMsg("step a-html", "📄 완료", "HTML 보고서를 오른쪽에 표시했습니다.");
          showReport(res.report_url, id);
        } else {
          addMsg("status", "안내", "보고서가 생성되지 않았습니다. 스텝 로그를 확인하세요.");
        }
      })
      .catch(function (e) { st.remove(); addMsg("error", "요청 실패", esc(e.message)); })
      .finally(function () { els.runBtn.disabled = false; });
  }

  // ---- 빠른 분석 (에이전트 없이 pandas) ----
  function quickAnalyze() {
    var file = els.fileInput.files[0];
    if (!file) { addMsg("status", "안내", "먼저 CSV 파일을 선택하세요."); return; }
    addMsg("user", "빠른 분석", esc(file.name));
    var st = statusMsg("pandas 로 즉시 분석 중…");
    var fd = new FormData(); fd.append("file", file);
    fetch("/api/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }); })
      .then(function (res) {
        st.remove();
        var m = res.meta;
        addMsg("step a-analysis", "🔬 빠른 분석 완료",
          "✅ <b>" + m.rows.toLocaleString() + "행 × " + m.cols + "열</b> · 인코딩 <code>" +
          esc(res.encoding) + "</code> · 렌더러 <code>" + esc(res.renderer) + "</code>");
        showReport(res.report_url, res.report_id.slice(0, 8));
      })
      .catch(function (e) { st.remove(); addMsg("error", "빠른 분석 실패", esc(e.message)); });
  }

  // ---- 이벤트 배선 ----
  els.cfgToggle.onclick = function () { els.cfgPanel.classList.toggle("hidden"); };
  els.runBtn.onclick = run;
  els.quickBtn.onclick = quickAnalyze;
  els.query.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") run();
  });
  CFG_KEYS.forEach(function (k) { els[k].addEventListener("change", saveCfg); });
  els.needSql.addEventListener("change", saveCfg);
  els.mockMode.addEventListener("change", saveCfg);

  loadCfg();
})();
