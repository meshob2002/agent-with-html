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
    convToggle: $("convToggle"), newConvBtn: $("newConvBtn"),
    convPanel: $("convPanel"), convList: $("convList"),
    convBar: $("convBar"), convId: $("convId"), convTurns: $("convTurns"),
    base_url: $("base_url"), token: $("token"), project_key: $("project_key"),
    app_router: $("app_router"), app_analysis: $("app_analysis"),
    app_sql: $("app_sql"), app_html: $("app_html"), headers_json: $("headers_json"),
    query: $("query"), runBtn: $("runBtn"), needSql: $("needSql"), mockMode: $("mockMode"),
    fileInput: $("fileInput"), quickBtn: $("quickBtn"), log: $("log"),
    frame: $("reportFrame"), empty: $("reportEmpty"),
    actions: $("reportActions"), openReport: $("openReport"), dlReport: $("dlReport"),
  };

  var conversationId = null;   // 현재 대화 세션 키 (멀티턴)
  var turnCount = 0;

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
    if (conversationId) fd.append("conversation_id", conversationId);
    if (file) fd.append("file", file);

    // NDJSON 스트리밍 수신: 한 줄에 이벤트 하나씩 도착하는 즉시 렌더
    function handleEvent(obj) {
      if (obj.type === "conversation") {
        turnCount += 1;
        setConversation(obj.conversation_id);
      } else if (obj.type === "step") {
        st.remove();
        renderStep(obj.step);
      } else if (obj.type === "done") {
        if (obj.conversation_id) setConversation(obj.conversation_id);
        if (obj.report_url) {
          var id = obj.report_url.split("/").pop().replace(".html", "").slice(0, 8);
          addMsg("step a-html", "📄 완료", "HTML 보고서를 오른쪽에 표시했습니다.");
          showReport(obj.report_url, id);
        } else {
          addMsg("status", "안내", "보고서가 생성되지 않았습니다. 스텝 로그를 확인하세요.");
        }
      } else if (obj.type === "error") {
        addMsg("error", "오류", esc(obj.error));
      }
    }

    fetch("/api/orchestrate", { method: "POST", body: fd })
      .then(function (r) {
        if (!r.ok || !r.body) {
          // 에러 응답이 JSON 이 아닐 수도(500 HTML) → 텍스트로 안전하게 처리
          return r.text().then(function (t) {
            var msg = t;
            try { msg = JSON.parse(t).error || t; } catch (e) {}
            throw new Error("서버 오류(HTTP " + r.status + "): " + String(msg).slice(0, 300));
          });
        }
        var reader = r.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (res) {
            buffer += decoder.decode(res.value || new Uint8Array(), { stream: !res.done });
            var lines = buffer.split("\n");
            buffer = res.done ? "" : lines.pop();  // 마지막 미완성 줄은 남겨둠
            lines.forEach(function (line) {
              line = line.trim();
              if (!line) return;
              try { handleEvent(JSON.parse(line)); }
              catch (e) { /* 부분 수신 라인 무시 */ }
            });
            if (!res.done) return pump();
          });
        }
        return pump();
      })
      .catch(function (e) { st.remove(); addMsg("error", "요청 실패", esc(e.message)); })
      .finally(function () {
        els.runBtn.disabled = false;
        els.fileInput.value = "";  // 팔로우업 턴이 같은 파일을 다시 올리지 않도록 비움
      });
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

  // ---- 대화 세션 관리 ----
  function setConversation(cid) {
    conversationId = cid || null;
    if (conversationId) {
      els.convId.textContent = conversationId;
      els.convTurns.textContent = turnCount ? "· " + turnCount + "턴" : "";
      els.convBar.classList.remove("hidden");
    } else {
      els.convBar.classList.add("hidden");
    }
  }
  function newConversation() {
    conversationId = null; turnCount = 0;
    els.log.innerHTML = "";
    els.frame.src = "about:blank"; els.frame.classList.add("hidden");
    els.empty.classList.remove("hidden"); els.actions.classList.add("hidden");
    setConversation(null);
    addMsg("status", "안내", "새 대화를 시작합니다. (이전 대화는 대화목록에서 다시 열 수 있어요)");
  }

  function refreshConvList() {
    fetch("/api/conversations").then(function (r) { return r.json(); }).then(function (res) {
      els.convList.innerHTML = "";
      var convs = res.conversations || [];
      if (!convs.length) { els.convList.innerHTML = '<div class="dim">저장된 대화가 없습니다.</div>'; return; }
      convs.forEach(function (c) {
        var row = document.createElement("div");
        row.className = "conv-row" + (c.id === conversationId ? " active" : "");
        var when = new Date((c.updated_at || 0) * 1000).toLocaleString("ko-KR");
        row.innerHTML =
          '<div class="conv-main"><b>' + esc(c.title || "(제목 없음)") + "</b>" +
          '<div class="dim">' + c.turns + "턴 · " + esc(when) + "</div></div>" +
          '<button class="mini del" title="삭제">🗑️</button>';
        row.querySelector(".conv-main").onclick = function () { loadConversation(c.id); };
        row.querySelector(".del").onclick = function (e) {
          e.stopPropagation();
          fetch("/api/conversations/" + encodeURIComponent(c.id) + "/delete", { method: "POST" })
            .then(function () { if (c.id === conversationId) newConversation(); refreshConvList(); });
        };
        els.convList.appendChild(row);
      });
    });
  }

  function loadConversation(cid) {
    fetch("/api/conversations/" + encodeURIComponent(cid))
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || "로드 실패"); return j; }); })
      .then(function (conv) {
        els.log.innerHTML = "";
        turnCount = conv.turns.length;
        setConversation(conv.id);
        var lastReport = null;
        conv.turns.forEach(function (t) {
          addMsg("user", "요청 (턴 " + t.seq + ")", esc(t.request || "(CSV 분석)") +
            (t.mock ? ' <span class="badge">MOCK</span>' : "") +
            (t.need_sql ? ' <span class="badge">SQL</span>' : ""));
          (t.events || []).forEach(renderStep);
          if (t.report_url) {
            lastReport = t.report_url;
            var id = t.report_url.split("/").pop().replace(".html", "").slice(0, 8);
            var m = addMsg("step a-html", "📄 보고서", "");
            var a = document.createElement("a");
            a.href = t.report_url; a.target = "_blank"; a.textContent = "보고서 열기 (" + id + ")";
            m.appendChild(a);
          }
        });
        if (lastReport) showReport(lastReport, "load");
        els.convPanel.classList.add("hidden");
        addMsg("status", "안내", "대화를 불러왔습니다. 이어서 입력하면 같은 세션으로 계속됩니다.");
      })
      .catch(function (e) { addMsg("error", "대화 로드 실패", esc(e.message)); });
  }

  // ---- 이벤트 배선 ----
  els.cfgToggle.onclick = function () { els.cfgPanel.classList.toggle("hidden"); };
  els.convToggle.onclick = function () {
    els.convPanel.classList.toggle("hidden");
    if (!els.convPanel.classList.contains("hidden")) refreshConvList();
  };
  els.newConvBtn.onclick = newConversation;
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
