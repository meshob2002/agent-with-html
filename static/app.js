/* OK Agent × HTML 보고서 — 프론트엔드 로직
 * 흐름: 쿼리 -> /api/query (Agent 호출 + 링크 추출) -> /api/analyze (CSV 다운로드+분석+보고서)
 * 직접 분석: 파일 선택 -> /api/upload
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var LS_KEY = "okagent.config.v1";

  var els = {
    cfgToggle: $("cfgToggle"), cfgPanel: $("cfgPanel"),
    base_url: $("base_url"), app_id: $("app_id"), token: $("token"),
    project_key: $("project_key"), headers_json: $("headers_json"),
    query: $("query"), runBtn: $("runBtn"), autoAnalyze: $("autoAnalyze"),
    fileInput: $("fileInput"), log: $("log"),
    frame: $("reportFrame"), empty: $("reportEmpty"),
    actions: $("reportActions"), openReport: $("openReport"), dlReport: $("dlReport"),
  };

  var conversationId = null;

  // ---- 설정 저장/복원 (localStorage, 브라우저 전용) ----
  function loadCfg() {
    try {
      var c = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
      ["base_url", "app_id", "token", "project_key", "headers_json"].forEach(function (k) {
        if (c[k] != null && els[k] && !els[k].value) els[k].value = c[k];
      });
    } catch (e) { /* 저장 접근 불가 시 무시 */ }
  }
  function saveCfg() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        base_url: els.base_url.value, app_id: els.app_id.value,
        token: els.token.value, project_key: els.project_key.value,
        headers_json: els.headers_json.value,
      }));
    } catch (e) { /* 무시 */ }
  }
  function config() {
    return {
      base_url: els.base_url.value.trim(),
      app_id: els.app_id.value.trim(),
      token: els.token.value,
      project_key: els.project_key.value,
      headers_json: els.headers_json.value,
    };
  }

  // ---- 로그(대화) 렌더 ----
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

  // ---- 보고서 표시 ----
  function showReport(res) {
    els.frame.src = res.report_url;
    els.frame.classList.remove("hidden");
    els.empty.classList.add("hidden");
    els.actions.classList.remove("hidden");
    els.openReport.href = res.report_url;
    els.dlReport.href = res.report_url;
    els.dlReport.setAttribute("download", "report-" + res.report_id.slice(0, 8) + ".html");
  }

  // ---- API 호출 ----
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  // 분석 실행 -> 보고서 표시
  function analyze(url, query) {
    var st = statusMsg("CSV 다운로드 및 분석 중… (" + url + ")");
    return postJSON("/api/analyze", { url: url, query: query, config: config() })
      .then(function (res) {
        st.remove();
        var m = res.meta;
        addMsg("agent", "분석 완료",
          "✅ <b>" + m.rows.toLocaleString() + "행 × " + m.cols + "열</b> · 인코딩 <code>" +
          esc(res.encoding) + "</code> · " + (res.bytes / 1024).toFixed(1) + " KB" +
          '<div class="hint">숫자형 ' + m.n_numeric + "개 · 범주형 " + m.n_categorical +
          "개 → 보고서를 오른쪽에 표시했습니다.</div>");
        showReport(res);
      })
      .catch(function (e) {
        st.remove();
        addMsg("error", "분석 실패", esc(e.message));
      });
  }

  // 링크 목록 렌더 (수동 분석 버튼)
  function renderLinks(links, query) {
    if (!links.length) {
      addMsg("status", "안내", "응답에서 다운로드 링크를 찾지 못했습니다. Agent 프롬프트가 CSV 링크를 포함하는지 확인하세요.");
      return;
    }
    var box = document.createElement("div");
    box.className = "links";
    links.forEach(function (lk) {
      var row = document.createElement("div");
      row.className = "linkrow";
      row.innerHTML = '<span class="badge ' + (lk.is_csv ? "csv" : "") + '">' +
        (lk.is_csv ? "CSV" : "LINK") + '</span>' +
        '<a class="u" href="' + esc(lk.url) + '" target="_blank" rel="noopener" title="' +
        esc(lk.url) + '">' + esc(lk.label) + "</a>";
      var btn = document.createElement("button");
      btn.textContent = "이 링크 분석";
      btn.onclick = function () { analyze(lk.url, query); };
      row.appendChild(btn);
      box.appendChild(row);
    });
    var wrap = addMsg("agent", "다운로드 링크", "");
    wrap.appendChild(box);
    return links;
  }

  // 메인 실행: 쿼리 -> Agent -> 링크 -> (자동)분석
  function run() {
    var q = els.query.value.trim();
    if (!q) { els.query.focus(); return; }
    if (!config().app_id) {
      addMsg("error", "설정 필요", "설정(⚙️)에서 APP_ID 를 먼저 입력하세요.");
      els.cfgPanel.classList.remove("hidden");
      return;
    }
    saveCfg();
    addMsg("user", "요청", esc(q));
    els.runBtn.disabled = true;
    var st = statusMsg("Agent 응답 대기 중…");

    postJSON("/api/query", { query: q, conversation_id: conversationId, config: config() })
      .then(function (res) {
        st.remove();
        conversationId = res.conversation_id || conversationId;
        if (res.message) addMsg("agent", "Agent", esc(res.message));
        renderLinks(res.links, q);

        if (els.autoAnalyze.checked) {
          var csv = res.links.filter(function (l) { return l.is_csv; });
          if (csv.length) return analyze(csv[0].url, q);
        }
      })
      .catch(function (e) {
        st.remove();
        addMsg("error", "Agent 호출 실패", esc(e.message));
      })
      .finally(function () { els.runBtn.disabled = false; });
  }

  // 직접 CSV 업로드 분석
  function uploadFile(file) {
    var st = statusMsg("업로드 파일 분석 중… (" + file.name + ")");
    var fd = new FormData();
    fd.append("file", file);
    fetch("/api/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }); })
      .then(function (res) {
        st.remove();
        var m = res.meta;
        addMsg("agent", "분석 완료 (업로드)",
          "✅ <b>" + m.rows.toLocaleString() + "행 × " + m.cols + "열</b> · 인코딩 <code>" +
          esc(res.encoding) + "</code>");
        showReport(res);
      })
      .catch(function (e) { st.remove(); addMsg("error", "업로드 분석 실패", esc(e.message)); });
  }

  // ---- 이벤트 배선 ----
  els.cfgToggle.onclick = function () { els.cfgPanel.classList.toggle("hidden"); };
  els.runBtn.onclick = run;
  els.query.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") run();
  });
  els.fileInput.addEventListener("change", function () {
    if (this.files && this.files[0]) { addMsg("user", "업로드", esc(this.files[0].name)); uploadFile(this.files[0]); }
    this.value = "";
  });
  ["base_url", "app_id", "token", "project_key", "headers_json"].forEach(function (k) {
    els[k].addEventListener("change", saveCfg);
  });

  loadCfg();
})();
