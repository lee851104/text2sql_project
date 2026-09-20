(function () {
  "use strict";

  var HISTORY_KEY = "powerquery.history.v1";
  var HISTORY_LIMIT = 12;
  var DATA_UPLOAD_MAX_BYTES = 64 * 1024 * 1024;
  var SENSITIVE_HISTORY_PATTERNS = [
    /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i,
    /\b(?:sk|sess|token)-[A-Za-z0-9_-]{8,}\b/i,
    /(?:^|[^A-Z0-9])[A-Z][12]\d{8}(?![A-Z0-9])/i,
    /(?:\+886[-\s]?9\d{2}|09\d{2})[-\s]?\d{3}[-\s]?\d{3}/,
    /(?:\(0\d{1,2}\)|0\d{1,2})[-\s]?\d{3,4}[-\s]?\d{4}/,
    /(?:^|\D)\d{10,16}(?!\d)/,
    /(?:^|\D)(?:\d[ -]?){13,19}(?!\d)/,
    /(?:姓名|姓氏)\s*[:：]\s*[一-鿿]{2,4}/,
    /(?:請\s*)?(?:替|幫|為)\s*[一-鿿]{2,4}\s*[（(]/
  ];
  var PAGE_META = {
    query: ["查詢中心", "安全地把自然語言轉成可追溯資料答案", "queryHeading"],
    overview: ["資料總覽", "掌握資料涵蓋並從常用分析開始", "overviewHeading"],
    data: ["資料管理", "管理資料檔、人工審核、資料庫版本與完整稽核", "dataHeading"],
    settings: ["API 與模型", "切換離線規則或線上模型服務", "settingsHeading"],
    docs: ["API 文件", "查閱端點、請求格式與回應契約", "docsHeading"]
  };

  var byId = function (id) { return document.getElementById(id); };
  var conversation = byId("conversation");
  var form = byId("queryForm");
  var input = byId("queryInput");
  var sendButton = byId("sendButton");
  var workspace = byId("workspace");
  var appMain = byId("appMain");
  var activeController = null;
  var activeMode = "offline";
  var runtimeInfo = {};
  var runtimeRequestActive = false;
  var composing = false;
  var queryHistory = [];
  var historyCursor = null;
  var corpusItems = [];
  var corpusEvents = [];
  var datasetItems = [];
  var dataChanges = [];
  var databaseVersions = [];
  var auditEvents = [];
  var adminSession = null;
  var adminSessionRequest = null;
  var adminCsrfToken = "";
  var activeManagementTab = "files";
  var managementRequestActive = false;
  var trainingCandidateCounts = null;
  var trainingTimer = null;
  var sidebarTrapRelease = null;
  var sidebarReturnFocus = null;
  var dialogReturnFocus = null;
  var datasetDialogReturnFocus = null;
  var dataChangeDialogReturnFocus = null;

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function payloadError(payload, status) {
    if (payload && typeof payload.detail === "string") return payload.detail;
    if (payload && Array.isArray(payload.detail)) {
      return payload.detail.map(function (item) { return item.msg || String(item); }).join("；");
    }
    if (payload && payload.error) return String(payload.error);
    return "伺服器回應錯誤（HTTP " + status + "）";
  }

  function api(path, options) {
    var requestOptions = Object.assign({ headers: {} }, options || {});
    requestOptions.credentials = "same-origin";
    var requestMethod = String(requestOptions.method || "GET").toUpperCase();
    if (adminCsrfToken && requestMethod !== "GET" && requestMethod !== "HEAD") {
      requestOptions.headers = Object.assign({}, requestOptions.headers, {
        "X-PowerQuery-CSRF": adminCsrfToken
      });
    }
    if (requestOptions.body != null && typeof requestOptions.body !== "string") {
      requestOptions.body = JSON.stringify(requestOptions.body);
      requestOptions.headers = Object.assign({}, requestOptions.headers, { "Content-Type": "application/json" });
    }
    return fetch(path, requestOptions).then(function (response) {
      return response.json().catch(function () {
        var formatError = new Error("伺服器回傳非 JSON 內容");
        formatError.kind = "http";
        formatError.status = response.status;
        throw formatError;
      }).then(function (payload) {
        if (!response.ok) {
          var requestError = new Error(payloadError(payload, response.status));
          requestError.kind = "http";
          requestError.status = response.status;
          requestError.payload = payload;
          throw requestError;
        }
        return payload;
      });
    }).catch(function (error) {
      if (error && (error.kind || error.name === "AbortError")) throw error;
      var networkError = new Error("無法連線到本機分析服務");
      networkError.kind = "network";
      networkError.cause = error;
      throw networkError;
    });
  }

  function announce(message, urgent) {
    var target = byId(urgent ? "srAlert" : "srStatus");
    target.textContent = "";
    window.setTimeout(function () { target.textContent = String(message); }, 20);
  }

  function valueText(value) {
    if (value == null || value === "") return "—";
    if (typeof value === "number") return new Intl.NumberFormat("zh-Hant-TW", { maximumFractionDigits: 3 }).format(value);
    if (typeof value === "boolean") return value ? "是" : "否";
    return String(value);
  }

  function formatDate(value) {
    if (!value) return "—";
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-Hant-TW", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
  }

  function unwrap(payload) {
    return payload && payload.data != null ? payload.data : payload;
  }

  function businessData(payload) {
    if (payload && payload.success === false) throw new Error(payload.error || "服務未完成這次操作");
    return unwrap(payload) || {};
  }

  function sidebarIsDrawer() {
    return window.matchMedia("(max-width: 1024px)").matches;
  }

  function trapFocus(container) {
    function focusables() {
      return Array.prototype.filter.call(
        container.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"),
        function (item) { return !item.hidden && item.getClientRects().length > 0; }
      );
    }
    function handleTab(event) {
      if (event.key !== "Tab") return;
      var items = focusables();
      if (!items.length) { event.preventDefault(); return; }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    container.addEventListener("keydown", handleTab);
    return function () { container.removeEventListener("keydown", handleTab); };
  }

  function setSidebarOpen(open, restoreFocus) {
    var sidebar = byId("sidebar");
    var backdrop = byId("sidebarBackdrop");
    var drawer = sidebarIsDrawer();
    if (!drawer) open = false;
    if (open) {
      sidebarReturnFocus = document.activeElement;
      sidebar.classList.add("open");
      backdrop.hidden = false;
      byId("navToggle").setAttribute("aria-expanded", "true");
      byId("navToggle").setAttribute("aria-label", "關閉導覽");
      document.body.classList.add("nav-open");
      sidebar.inert = false;
      sidebar.removeAttribute("inert");
      sidebar.removeAttribute("aria-hidden");
      sidebar.focus();
      appMain.inert = true;
      appMain.setAttribute("inert", "");
      if (sidebarTrapRelease) sidebarTrapRelease();
      sidebarTrapRelease = trapFocus(sidebar);
      return;
    }
    var wasOpen = sidebar.classList.contains("open");
    sidebar.classList.remove("open");
    backdrop.hidden = true;
    byId("navToggle").setAttribute("aria-expanded", "false");
    byId("navToggle").setAttribute("aria-label", "開啟導覽");
    document.body.classList.remove("nav-open");
    sidebar.inert = drawer;
    if (drawer) {
      sidebar.setAttribute("inert", "");
      sidebar.setAttribute("aria-hidden", "true");
    } else {
      sidebar.removeAttribute("inert");
      sidebar.removeAttribute("aria-hidden");
    }
    appMain.inert = false;
    appMain.removeAttribute("inert");
    if (sidebarTrapRelease) sidebarTrapRelease();
    sidebarTrapRelease = null;
    if (wasOpen && restoreFocus !== false && sidebarReturnFocus && typeof sidebarReturnFocus.focus === "function") sidebarReturnFocus.focus();
    sidebarReturnFocus = null;
  }

  function showView(name, options) {
    var meta = PAGE_META[name];
    if (!meta) return;
    document.querySelectorAll("[data-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== name;
    });
    document.querySelectorAll("[data-view]").forEach(function (button) {
      if (button.getAttribute("data-view") === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    byId("pageTitle").textContent = meta[0];
    byId("pageSubtitle").textContent = meta[1];
    document.title = meta[0] + "｜PowerQuery TW";
    setSidebarOpen(false, false);
    workspace.scrollTop = 0;
    if (!options || options.focus !== false) window.setTimeout(function () { byId(meta[2]).focus(); }, 0);
    if (name === "data") enterDataManagement();
    if (name === "settings") {
      if (adminSession) loadRuntime(false);
      else loadAdminSession().then(function (authenticated) {
        if (authenticated) loadRuntime(false);
        else {
          byId("runtimeSaveStatus").textContent = "需要管理登入";
          byId("runtimeFeedback").className = "inline-feedback error";
          byId("runtimeFeedback").textContent = "請先到「資料管理」登入，再調整 API 與模型設定。";
          byId("runtimeFeedback").hidden = false;
        }
      });
    }
  }

  function containsSensitiveText(value) {
    return SENSITIVE_HISTORY_PATTERNS.some(function (pattern) { return pattern.test(value); });
  }

  function loadHistory() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(HISTORY_KEY) || "[]");
      queryHistory = Array.isArray(stored) ? stored.filter(function (item) {
        return typeof item === "string" && item.trim() && item.length <= 500 && !containsSensitiveText(item);
      }).slice(0, HISTORY_LIMIT) : [];
      if (!Array.isArray(stored) || stored.length !== queryHistory.length) {
        window.localStorage.setItem(HISTORY_KEY, JSON.stringify(queryHistory));
      }
    } catch (_error) {
      queryHistory = [];
    }
    renderHistory();
  }

  function saveQuestion(question) {
    if (containsSensitiveText(question)) {
      announce("問句含敏感格式，本機最近查詢不會保存這次內容", false);
      return;
    }
    queryHistory = [question].concat(queryHistory.filter(function (item) { return item !== question; })).slice(0, HISTORY_LIMIT);
    try { window.localStorage.setItem(HISTORY_KEY, JSON.stringify(queryHistory)); } catch (_error) { /* Storage may be disabled. */ }
    historyCursor = null;
    renderHistory();
  }

  function renderHistory() {
    var holder = byId("queryHistory");
    holder.replaceChildren();
    if (!queryHistory.length) {
      holder.appendChild(element("p", "side-empty", "尚無查詢紀錄"));
      byId("clearHistory").disabled = true;
      return;
    }
    byId("clearHistory").disabled = false;
    queryHistory.forEach(function (question) {
      var button = element("button", "history-item", question);
      button.type = "button";
      button.title = question;
      button.addEventListener("click", function () {
        showView("query", { focus: false });
        input.value = question;
        resizeInput();
        input.focus();
      });
      holder.appendChild(button);
    });
  }

  function clearHistory() {
    queryHistory = [];
    historyCursor = null;
    try { window.localStorage.removeItem(HISTORY_KEY); } catch (_error) { /* Storage may be disabled. */ }
    renderHistory();
    announce("最近查詢已清除", false);
  }

  function resizeInput() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 150) + "px";
  }

  function scrollLatest() {
    window.requestAnimationFrame(function () {
      workspace.scrollTo({ top: workspace.scrollHeight, behavior: "smooth" });
    });
  }

  function setProcessing(on) {
    sendButton.disabled = on;
    sendButton.textContent = on ? "分析中" : "查詢";
    document.querySelectorAll("[data-question]").forEach(function (button) { button.disabled = on; });
    if (!on && !byId("queryView").hidden) input.focus();
  }

  function addUserMessage(question, mode) {
    var card = element("article", "message user");
    card.appendChild(element("div", "", question));
    card.appendChild(element("small", "user-mode", modeLabel(mode)));
    conversation.appendChild(card);
  }

  function progressCard() {
    var card = element("article", "message progress-card");
    card.setAttribute("aria-label", "查詢進度");
    var head = element("div", "result-head");
    head.appendChild(element("strong", "", "正在安全地處理查詢"));
    var elapsed = element("span", "meta", "0.0 秒");
    elapsed.setAttribute("aria-hidden", "true");
    head.appendChild(elapsed);
    card.appendChild(head);
    var list = element("ol");
    var labels = ["解析問題與資料實體", "檢查語意與唯讀規則", "產生並執行查詢", "整理結果與圖表"];
    var items = labels.map(function (label, index) {
      var item = element("li", index === 0 ? "active" : "", label);
      if (index === 0) item.setAttribute("aria-current", "step");
      list.appendChild(item);
      return item;
    });
    card.appendChild(list);
    var cancel = element("button", "suggestion", "取消這次查詢");
    cancel.type = "button";
    cancel.addEventListener("click", function () { if (activeController) activeController.abort(); });
    card.appendChild(cancel);
    conversation.appendChild(card);
    var started = performance.now();
    var elapsedTimer = window.setInterval(function () { elapsed.textContent = ((performance.now() - started) / 1000).toFixed(1) + " 秒"; }, 250);
    var stageTimers = items.slice(1).map(function (item, index) {
      return window.setTimeout(function () {
        items[index].className = "done";
        items[index].removeAttribute("aria-current");
        item.className = "active";
        item.setAttribute("aria-current", "step");
      }, 600 + index * 900);
    });
    function stopTimers() {
      window.clearInterval(elapsedTimer);
      stageTimers.forEach(window.clearTimeout);
    }
    return {
      finish: function () {
        stopTimers();
        items.forEach(function (item) { item.className = "done"; item.removeAttribute("aria-current"); });
      },
      remove: function () { stopTimers(); card.remove(); }
    };
  }

  function titleText(value, fallback) {
    if (value && typeof value === "object" && value.text != null) return String(value.text);
    return value == null || value === "" ? fallback : String(value);
  }

  function cleanChartData(spec) {
    return spec.data.map(function (trace) {
      var clean = {
        type: spec.kind === "bar" ? "bar" : "scatter",
        name: String(trace.name || ""),
        x: Array.isArray(trace.x) ? trace.x.slice() : [],
        y: Array.isArray(trace.y) ? trace.y.slice() : []
      };
      if (spec.kind !== "bar") clean.mode = spec.kind === "line" ? "lines+markers" : "markers";
      if (Array.isArray(trace.text)) clean.text = trace.text.map(String);
      return clean;
    });
  }

  function buildFallbackChart(spec, cleanData) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 720 280");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "查詢結果簡易圖表");
    var allY = [];
    cleanData.forEach(function (trace) { trace.y.forEach(function (value) { if (Number.isFinite(Number(value))) allY.push(Number(value)); }); });
    if (!allY.length) return null;
    var min = Math.min.apply(null, allY);
    var max = Math.max.apply(null, allY);
    var span = max - min || 1;
    var colors = ["#1a5fb4", "#c25e00", "#2e7d32", "#7152ad"];
    var axis = document.createElementNS(ns, "path");
    axis.setAttribute("d", "M52 20 V235 H700");
    axis.setAttribute("fill", "none");
    axis.setAttribute("stroke", "#9dabbc");
    svg.appendChild(axis);
    cleanData.forEach(function (trace, traceIndex) {
      var ys = trace.y.map(Number);
      var color = colors[traceIndex % colors.length];
      if (spec.kind === "bar") {
        var width = Math.max(8, Math.min(38, 600 / Math.max(ys.length, 1) - 5));
        ys.forEach(function (value, index) {
          var baseline = Math.min(0, min);
          var range = Math.max(max, 0) - baseline || 1;
          var height = ((value - baseline) / range) * 185;
          var rect = document.createElementNS(ns, "rect");
          rect.setAttribute("x", String(65 + index * (615 / Math.max(ys.length, 1))));
          rect.setAttribute("y", String(235 - height));
          rect.setAttribute("width", String(width));
          rect.setAttribute("height", String(Math.max(1, height)));
          rect.setAttribute("fill", color);
          svg.appendChild(rect);
        });
        return;
      }
      var numericX = trace.x.map(Number);
      var minX = Math.min.apply(null, numericX);
      var maxX = Math.max.apply(null, numericX);
      var points = ys.map(function (value, index) {
        var x = spec.kind === "scatter" && Number.isFinite(numericX[index])
          ? 65 + ((numericX[index] - minX) / (maxX - minX || 1)) * 620
          : 65 + index * (620 / Math.max(ys.length - 1, 1));
        return [x, 225 - ((value - min) / span) * 185];
      });
      if (spec.kind === "line") {
        var line = document.createElementNS(ns, "polyline");
        line.setAttribute("points", points.map(function (point) { return point.join(","); }).join(" "));
        line.setAttribute("fill", "none");
        line.setAttribute("stroke", color);
        line.setAttribute("stroke-width", "2.5");
        svg.appendChild(line);
      }
      points.forEach(function (point) {
        var circle = document.createElementNS(ns, "circle");
        circle.setAttribute("cx", String(point[0]));
        circle.setAttribute("cy", String(point[1]));
        circle.setAttribute("r", "3.5");
        circle.setAttribute("fill", color);
        svg.appendChild(circle);
      });
    });
    return svg;
  }

  function renderChart(spec) {
    if (!spec || ["line", "bar", "scatter"].indexOf(spec.kind) < 0 || !Array.isArray(spec.data)) return null;
    var cleanData = cleanChartData(spec);
    var wrap = element("figure", "chart");
    var layout = spec.layout || {};
    var label = titleText(layout.title, "查詢結果圖表");
    wrap.setAttribute("aria-label", label);
    if (window.Plotly && typeof window.Plotly.newPlot === "function") {
      var plot = element("div", "plotly-chart");
      plot.tabIndex = 0;
      plot.setAttribute("aria-label", label + "，可使用圖表工具列操作");
      wrap.appendChild(plot);
      window.setTimeout(function () {
        var plotResult;
        try {
          plotResult = window.Plotly.newPlot(plot, cleanData, {
            title: { text: label },
            xaxis: { title: { text: titleText(layout.xaxis && layout.xaxis.title, "") } },
            yaxis: { title: { text: titleText(layout.yaxis && layout.yaxis.title, "") } },
            margin: { t: 52, r: 24, b: 58, l: 68 },
            paper_bgcolor: "rgba(0,0,0,0)",
            plot_bgcolor: "rgba(0,0,0,0)",
            font: { family: "system-ui, sans-serif", color: "#263547" },
            autosize: true
          }, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["sendDataToCloud", "lasso2d", "select2d"] });
        } catch (_error) {
          plotResult = Promise.reject(_error);
        }
        Promise.resolve(plotResult).catch(function () {
          if (!plot.isConnected) return;
          var fallback = buildFallbackChart(spec, cleanData);
          plot.replaceWith(fallback || element("p", "empty-state", "圖表無法顯示，請查看下方資料表。"));
        });
      }, 30);
      return wrap;
    }
    var fallback = buildFallbackChart(spec, cleanData);
    if (!fallback) return null;
    wrap.appendChild(fallback);
    return wrap;
  }

  function renderTable(columns, rows) {
    if (!Array.isArray(columns) || !columns.length || !Array.isArray(rows) || !rows.length) return null;
    var wrap = element("div", "table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("aria-label", "查詢結果，可左右捲動");
    var table = element("table");
    var head = element("thead");
    var headRow = element("tr");
    columns.forEach(function (column) { var th = element("th", "", column); th.scope = "col"; headRow.appendChild(th); });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = element("tbody");
    rows.forEach(function (row) {
      var tr = element("tr");
      var values = Array.isArray(row) ? row : columns.map(function (column) { return row[column]; });
      columns.forEach(function (_column, index) { tr.appendChild(element("td", "", valueText(values[index]))); });
      body.appendChild(tr);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function renderResult(data) {
    var card = element("article", "message result-card");
    var head = element("header", "result-head");
    head.appendChild(element("h3", "", "查詢結果"));
    var resultMeta = String(data.record_count || 0) + " 筆";
    if (data.runtime && data.runtime.mode) resultMeta += " · " + modeLabel(data.runtime.mode);
    head.appendChild(element("span", "meta", resultMeta));
    card.appendChild(head);
    card.appendChild(element("p", "explanation", data.explanation || "查詢完成。"));
    (Array.isArray(data.disclosures) ? data.disclosures : []).forEach(function (disclosure) {
      card.appendChild(element("div", "callout", typeof disclosure === "string" ? disclosure : disclosure.reason || disclosure.message || JSON.stringify(disclosure)));
    });
    if (data.learning && typeof data.learning === "object") {
      var learningState = statusLabel(data.learning.status || (data.learning.accepted ? "已加入學習流程" : "未加入學習流程"));
      var learningNote = element("div", "learning-note");
      learningNote.appendChild(element("strong", "", "語料學習：" + learningState));
      if (data.learning.reason) learningNote.appendChild(element("span", "", "（" + data.learning.reason + "）"));
      card.appendChild(learningNote);
    }
    var kpis = element("div", "kpis");
    var count = element("div", "kpi");
    count.appendChild(element("span", "", "結果筆數"));
    count.appendChild(element("strong", "", data.record_count || 0));
    kpis.appendChild(count);
    Object.entries(data.statistics || {}).filter(function (entry) { return entry[1] && typeof entry[1] === "object"; }).slice(0, 2).forEach(function (entry) {
      var kpi = element("div", "kpi");
      kpi.appendChild(element("span", "", entry[0] + "平均"));
      kpi.appendChild(element("strong", "", valueText(entry[1].average)));
      kpis.appendChild(kpi);
    });
    card.appendChild(kpis);
    var chart = renderChart(data.chart_spec);
    if (chart) card.appendChild(chart);
    var table = renderTable(data.columns || [], data.rows || []);
    if (table) card.appendChild(table);
    else card.appendChild(element("div", "empty-state", "這次查詢沒有可顯示的資料列。"));
    if (data.sql) {
      var details = element("details");
      details.appendChild(element("summary", "", "查看執行 SQL"));
      details.appendChild(element("pre", "", data.sql));
      card.appendChild(details);
    }
    conversation.appendChild(card);
  }

  function renderError(payload) {
    var card = element("article", "message error-card");
    // 後設問句也是 clarify，但缺的不是條件而是「這題不該用查詢回答」。標題掛「需要補充
    // 條件」會讓問「目前有接 API 嗎」的人以為自己問法不完整。
    var meta = payload.error_code === "DATA_SCOPE_QUESTION" || payload.error_code === "SYSTEM_STATUS_QUESTION";
    var severity = payload.severity === "clarify" ? (meta ? "這個問題不用查詢回答" : "需要補充條件") : payload.severity === "refuse" ? "這個問題不能直接計算" : "查詢未完成";
    card.appendChild(element("h3", "", severity));
    card.appendChild(element("p", "", payload.error || "系統目前無法完成查詢。"));
    if (payload.error_code) card.appendChild(element("div", "meta", "錯誤碼：" + payload.error_code));
    if (payload.diagnostic_id) card.appendChild(element("div", "meta", "診斷編號：" + payload.diagnostic_id));
    var actions = element("div", "suggestions");
    (payload.suggestions || []).forEach(function (suggestion) {
      var button = element("button", "suggestion", suggestion);
      button.type = "button";
      button.addEventListener("click", function () { input.value = suggestion; resizeInput(); input.focus(); });
      actions.appendChild(button);
    });
    var errorText = String(payload.error || "") + " " + String(payload.error_code || "");
    if (/API|api.?key|online|線上|金鑰/i.test(errorText)) {
      var settingsButton = element("button", "suggestion", "前往 API 設定");
      settingsButton.type = "button";
      settingsButton.addEventListener("click", function () { showView("settings"); });
      actions.appendChild(settingsButton);
    }
    if (actions.childNodes.length) card.appendChild(actions);
    conversation.appendChild(card);
  }

  function submitQuestion(question) {
    if (!question || activeController) return;
    showView("query", { focus: false });
    var selectedMode = byId("executionMode").value;
    var selectedScope = byId("queryScope").value;
    addUserMessage(question, selectedMode || "default");
    saveQuestion(question);
    input.value = "";
    resizeInput();
    setProcessing(true);
    var progress = progressCard();
    activeController = new AbortController();
    scrollLatest();
    var requestBody = { question: question };
    requestBody.query_scope = selectedScope;
    if (selectedMode) requestBody.execution_mode = selectedMode;
    api("/api/query", {
      method: "POST",
      body: requestBody,
      signal: activeController.signal
    }).then(function (payload) {
      progress.finish();
      window.setTimeout(progress.remove, 100);
      if (payload.success) {
        renderResult(payload.data || {});
        loadCorpus(false);
        loadTraining(false);
        announce("查詢完成，共 " + ((payload.data && payload.data.record_count) || 0) + " 筆結果", false);
      } else {
        renderError(payload);
        announce(payload.error || "查詢未完成", true);
      }
    }).catch(function (error) {
      progress.remove();
      var message = error.name === "AbortError" ? "已取消這次查詢。伺服器端處理可能仍會完成，但結果不會顯示。" : error.message;
      renderError({ error: message, severity: "error" });
      announce(error.name === "AbortError" ? "查詢已取消" : message, true);
    }).finally(function () {
      activeController = null;
      setProcessing(false);
      scrollLatest();
    });
  }

  function loadHealth() {
    return api("/api/health").then(function (health) {
      byId("statusDot").className = "status-dot connected";
      byId("statusText").textContent = health.demo ? "離線分析服務已就緒" : "線上分析服務已就緒";
      byId("demoBanner").hidden = !health.demo;
    }).catch(function (error) {
      byId("statusDot").className = "status-dot failed";
      byId("statusText").textContent = "分析服務尚未就緒";
      announce(error.message, true);
    });
  }


  function renderCoverageList(holder, items, render) {
    holder.textContent = "";
    if (!items || !items.length) {
      holder.appendChild(element("li", "", "—"));
      return;
    }
    items.forEach(function (item) {
      var entry = element("li", "");
      entry.appendChild(element("strong", "", render.title(item)));
      entry.appendChild(element("span", "", render.detail(item)));
      holder.appendChild(entry);
    });
  }

  function loadCoverage() {
    return api("/api/coverage").then(function (payload) {
      var data = businessData(payload);
      var counts = data.counts || {};
      var range = data.date_range || {};
      var plantFact = valueText(counts.plants_with_units) + " 座電廠有機組明細";
      if (counts.plants_in_roster) plantFact += "（電廠主檔共 " + valueText(counts.plants_in_roster) + " 座）";
      var facts = [
        "期間 " + valueText(range.start) + " ～ " + valueText(range.end),
        plantFact,
        valueText(counts.units) + " 台機組",
        "燃料別：" + (data.fuels || []).join("、")
      ];
      byId("coverageFacts").textContent = facts.join("　·　");
      renderCoverageList(byId("coverageAnswerable"), data.answerable, {
        title: function (item) { return item.title; },
        detail: function (item) { return item.answers; }
      });
      renderCoverageList(byId("coverageLimitations"), data.limitations, {
        title: function (item) { return item.topic; },
        detail: function (item) { return item.detail; }
      });
      var pitfalls = data.pitfalls || [];
      var refused = pitfalls.filter(function (item) { return item.severity === "refuse"; });
      var note = byId("coveragePitfalls");
      if (pitfalls.length) {
        note.textContent = "已知資料陷阱 " + pitfalls.reduce(function (total, item) { return total + item.count; }, 0) +
          " 項，其中 " + refused.reduce(function (total, item) { return total + item.count; }, 0) +
          " 項會直接拒答，其餘會在答案旁標示範圍。詳細條件由後端語意守門判斷。";
        note.hidden = false;
      } else {
        note.hidden = true;
      }
    }).catch(function (error) {
      byId("coverageFacts").textContent = "無法讀取涵蓋範圍：" + error.message;
    });
  }

  function loadStats(announceResult) {
    var button = byId("refreshOverview");
    button.disabled = true;
    return api("/api/stats").then(function (payload) {
      var stats = businessData(payload);
      byId("totalRecords").textContent = valueText(stats.total_records) + " 筆";
      byId("totalUnits").textContent = valueText(stats.total_units) + " 台";
      byId("outageRecords").textContent = valueText(stats.outage_records) + " 筆";
      byId("dateRange").textContent = valueText(stats.date_range);
      if (announceResult) announce("資料總覽已更新", false);
      return loadCoverage();
    }).catch(function (error) {
      if (announceResult) announce(error.message, true);
    }).finally(function () { button.disabled = false; });
  }

  function loadExamples() {
    return api("/api/examples").then(function (payload) {
      var questions = businessData(payload);
      if (!Array.isArray(questions)) return;
      var existing = Array.prototype.map.call(document.querySelectorAll("#examples [data-question]"), function (item) { return item.getAttribute("data-question"); });
      questions.filter(function (question) { return existing.indexOf(question) < 0; }).slice(0, 2).forEach(function (question) {
        var button = element("button", "analysis-card");
        button.type = "button";
        button.setAttribute("data-question", question);
        button.appendChild(element("span", "analysis-icon violet", "＋"));
        button.appendChild(element("strong", "", "精選問法"));
        button.appendChild(element("small", "", question));
        byId("examples").appendChild(button);
      });
    }).catch(function () { /* Static examples remain available. */ });
  }

  function modeLabel(mode) {
    if (mode === "online") return "線上 API";
    if (mode === "auto") return "自動判斷";
    if (mode === "default") return "沿用預設";
    return "離線規則";
  }

  function capabilityText(value) {
    if (value === true) return "可處理備轉容量、尖峰出力、機組設備與歲修等已驗證題型。";
    if (value === false) return "目前未啟用離線規則能力。";
    if (Array.isArray(value)) return value.map(String).join("、");
    if (value && typeof value === "object") {
      var enabled = Object.keys(value).filter(function (key) { return value[key]; });
      return enabled.length ? enabled.join("、") : "依本機已驗證規則處理";
    }
    return value ? String(value) : "可處理備轉容量、尖峰出力、機組設備與歲修等已驗證題型。";
  }

  function renderRuntime(info) {
    runtimeInfo = info || {};
    activeMode = ["auto", "offline", "online"].indexOf(runtimeInfo.active_mode) >= 0 ? runtimeInfo.active_mode : "offline";
    var defaultMode = ["auto", "offline", "online"].indexOf(runtimeInfo.default_mode) >= 0 ? runtimeInfo.default_mode : activeMode;
    byId("activeModeLabel").textContent = modeLabel(activeMode);
    byId("modeShortcut").setAttribute("data-mode", activeMode);
    byId("summaryActiveMode").textContent = modeLabel(activeMode);
    byId("summaryDefaultMode").textContent = modeLabel(defaultMode);
    byId("summaryOnlineConfigured").textContent = runtimeInfo.online_configured ? "已設定" : "尚未設定";
    byId("summaryProvider").textContent = valueText(runtimeInfo.provider);
    byId("summaryModel").textContent = valueText(runtimeInfo.model);
    byId("offlineCapability").textContent = capabilityText(runtimeInfo.offline_capability);
    var radio = document.querySelector("input[name='runtimeMode'][value='" + defaultMode + "']");
    if (radio) radio.checked = true;
    if (runtimeInfo.model && runtimeInfo.model !== "—" && document.activeElement !== byId("modelInput")) byId("modelInput").value = runtimeInfo.model;
    byId("runtimeSaveStatus").textContent = defaultMode === "online" && !runtimeInfo.online_configured ? "需要 API key" : "已同步";
    byId("clearRuntimeKey").disabled = runtimeRequestActive || !runtimeInfo.online_configured;
  }

  function loadRuntime(announceResult) {
    if (!adminSession) return Promise.resolve();
    return adminApi("/api/runtime/llm").then(function (payload) {
      var info = businessData(payload);
      renderRuntime(info);
      loadHealth();
      if (announceResult) announce("執行模式已同步", false);
    }).catch(function (error) {
      renderRuntime({ default_mode: "auto", active_mode: "offline", online_configured: false, provider: "—", model: "—" });
      byId("runtimeSaveStatus").textContent = error && (error.status === 401 || error.status === 403) ? "需要管理登入" : "無法讀取";
      if (error && (error.status === 401 || error.status === 403)) {
        byId("runtimeFeedback").className = "inline-feedback error";
        byId("runtimeFeedback").textContent = "管理登入已失效；請先到「資料管理」重新登入。";
        byId("runtimeFeedback").hidden = false;
      }
      if (announceResult) announce(error.message, true);
    });
  }

  function saveRuntime(event) {
    event.preventDefault();
    if (!adminSession) {
      byId("runtimeFeedback").className = "inline-feedback error";
      byId("runtimeFeedback").textContent = "請先到「資料管理」登入，再套用執行設定。";
      byId("runtimeFeedback").hidden = false;
      announce("API 與模型設定需要管理登入", true);
      return;
    }
    if (runtimeRequestActive) return;
    var chosen = document.querySelector("input[name='runtimeMode']:checked");
    var keyInput = byId("apiKeyInput");
    var modelInput = byId("modelInput");
    var feedback = byId("runtimeFeedback");
    var save = byId("saveRuntime");
    var body = { mode: chosen ? chosen.value : "auto" };
    if (modelInput.value.trim()) body.model = modelInput.value.trim();
    if (keyInput.value.trim()) body.api_key = keyInput.value.trim();
    runtimeRequestActive = true;
    save.disabled = true;
    byId("clearRuntimeKey").disabled = true;
    feedback.hidden = true;
    byId("runtimeSaveStatus").textContent = "套用中…";
    api("/api/runtime/llm", { method: "PUT", body: body }).then(function (payload) {
      var info = businessData(payload);
      renderRuntime(info);
      feedback.className = "inline-feedback";
      feedback.textContent = "設定已套用。後續查詢會使用「" + modeLabel(info.active_mode || body.mode) + "」。";
      feedback.hidden = false;
      announce("API 與模型設定已套用", false);
    }).catch(function (error) {
      feedback.className = "inline-feedback error";
      feedback.textContent = "設定未套用：" + error.message;
      feedback.hidden = false;
      byId("runtimeSaveStatus").textContent = "儲存失敗";
      announce(error.message, true);
    }).finally(function () {
      keyInput.value = "";
      keyInput.type = "password";
      byId("toggleApiKey").textContent = "顯示";
      byId("toggleApiKey").setAttribute("aria-label", "顯示 API key");
      runtimeRequestActive = false;
      save.disabled = false;
      byId("clearRuntimeKey").disabled = !runtimeInfo.online_configured;
    });
  }

  function clearRuntimeKey() {
    if (!adminSession) {
      byId("runtimeFeedback").className = "inline-feedback error";
      byId("runtimeFeedback").textContent = "請先到「資料管理」登入，再清除記憶體金鑰。";
      byId("runtimeFeedback").hidden = false;
      announce("清除 API key 需要管理登入", true);
      return;
    }
    if (runtimeRequestActive) return;
    var button = byId("clearRuntimeKey");
    var save = byId("saveRuntime");
    var keyInput = byId("apiKeyInput");
    var feedback = byId("runtimeFeedback");
    runtimeRequestActive = true;
    button.disabled = true;
    save.disabled = true;
    keyInput.value = "";
    keyInput.type = "password";
    byId("toggleApiKey").textContent = "顯示";
    byId("toggleApiKey").setAttribute("aria-label", "顯示 API key");
    feedback.hidden = true;
    byId("runtimeSaveStatus").textContent = "清除中…";
    api("/api/runtime/llm", {
      method: "PUT",
      body: { mode: "offline", api_key: null }
    }).then(function (payload) {
      var info = businessData(payload);
      renderRuntime(info);
      loadHealth();
      feedback.className = "inline-feedback";
      feedback.textContent = info.online_configured
        ? "已切換離線並清除程序內金鑰；啟動環境仍提供 OPENAI_API_KEY。"
        : "程序記憶體內的 API key 已清除，預設模式已切換為離線。";
      feedback.hidden = false;
      announce("記憶體內 API key 已清除", false);
    }).catch(function (error) {
      feedback.className = "inline-feedback error";
      feedback.textContent = "金鑰未清除：" + error.message;
      feedback.hidden = false;
      byId("runtimeSaveStatus").textContent = "清除失敗";
      announce(error.message, true);
    }).finally(function () {
      runtimeRequestActive = false;
      save.disabled = false;
      button.disabled = !runtimeInfo.online_configured;
    });
  }

  function firstArray(value, names) {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    for (var i = 0; i < names.length; i += 1) {
      if (Array.isArray(value[names[i]])) return value[names[i]];
      if (value[names[i]] && typeof value[names[i]] === "object") {
        return Object.keys(value[names[i]]).map(function (key) {
          var item = value[names[i]][key];
          return item && typeof item === "object" ? Object.assign({ dataset: key }, item) : { dataset: key, value: item };
        });
      }
    }
    return [];
  }

  function adminIdentity(data) {
    if (!data || typeof data !== "object") return "";
    if (typeof data.username === "string") return data.username;
    if (typeof data.user === "string") return data.user;
    if (data.user && typeof data.user.username === "string") return data.user.username;
    if (typeof data.actor === "string") return data.actor;
    return "";
  }

  function sessionIsAuthenticated(data) {
    return Boolean(data && (
      data.authenticated === true ||
      data.logged_in === true ||
      data.active === true ||
      adminIdentity(data)
    ));
  }

  function sessionCsrf(data) {
    if (!data || typeof data !== "object") return "";
    return String(data.csrf_token || data.csrf || data.csrfToken || "");
  }

  function lockDataManagement(message, isError) {
    adminSession = null;
    adminCsrfToken = "";
    window.clearTimeout(trainingTimer);
    var gate = byId("dataAuthGate");
    var content = byId("dataManagementContent");
    gate.hidden = false;
    gate.inert = false;
    gate.removeAttribute("inert");
    content.hidden = true;
    content.inert = true;
    content.setAttribute("inert", "");
    byId("refreshDataManagement").hidden = true;
    byId("adminPassword").value = "";
    var feedback = byId("dataLoginFeedback");
    if (message) {
      feedback.className = "inline-feedback" + (isError === false ? "" : " error");
      feedback.textContent = message;
      feedback.hidden = false;
    } else feedback.hidden = true;
  }

  function unlockDataManagement(data) {
    adminSession = data || {};
    adminCsrfToken = sessionCsrf(adminSession);
    var gate = byId("dataAuthGate");
    var content = byId("dataManagementContent");
    gate.hidden = true;
    gate.inert = true;
    gate.setAttribute("inert", "");
    content.hidden = false;
    content.inert = false;
    content.removeAttribute("inert");
    byId("refreshDataManagement").hidden = false;
    byId("dataSessionUser").textContent = adminIdentity(adminSession) || "管理員";
    byId("defaultCredentialWarning").hidden = adminSession.using_default_credentials !== true;
    byId("dataLoginFeedback").hidden = true;
  }

  function adminApi(path, options) {
    return api(path, options).catch(function (error) {
      if (error && (error.status === 401 || error.status === 403)) {
        lockDataManagement(error.status === 401 ? "登入已失效，請重新登入。" : "管理驗證已失效，請重新登入。" );
      }
      throw error;
    });
  }

  function loadAdminSession() {
    if (adminSessionRequest) return adminSessionRequest;
    adminSessionRequest = api("/api/admin/session").then(function (payload) {
      var data = businessData(payload);
      if (!sessionIsAuthenticated(data)) {
        lockDataManagement();
        return false;
      }
      unlockDataManagement(data);
      return true;
    }).catch(function (error) {
      if (error && error.status === 401) {
        lockDataManagement();
        return false;
      }
      lockDataManagement("目前無法確認管理登入狀態：" + error.message);
      return false;
    }).finally(function () {
      adminSessionRequest = null;
    });
    return adminSessionRequest;
  }

  function enterDataManagement() {
    loadAdminSession().then(function (authenticated) {
      if (authenticated) refreshDataManagement(false);
      else window.setTimeout(function () { byId("adminUsername").focus(); }, 0);
    });
  }

  function loginAdmin(event) {
    event.preventDefault();
    if (managementRequestActive) return;
    var formElement = byId("dataLoginForm");
    if (!formElement.reportValidity()) return;
    var username = byId("adminUsername").value.trim();
    var passwordInput = byId("adminPassword");
    var feedback = byId("dataLoginFeedback");
    var button = byId("dataLoginButton");
    managementRequestActive = true;
    button.disabled = true;
    feedback.hidden = true;
    api("/api/admin/session", {
      method: "POST",
      body: { username: username, password: passwordInput.value }
    }).then(function (payload) {
      var data = businessData(payload);
      if (!sessionIsAuthenticated(data)) throw new Error("登入回應未建立管理工作階段");
      unlockDataManagement(data);
      selectManagementTab(activeManagementTab, { focus: false });
      announce("資料管理登入成功", false);
      return refreshDataManagement(false);
    }).catch(function (error) {
      lockDataManagement("登入失敗：" + error.message);
      announce("資料管理登入失敗", true);
      window.setTimeout(function () { byId("adminPassword").focus(); }, 0);
    }).finally(function () {
      passwordInput.value = "";
      passwordInput.type = "password";
      byId("toggleAdminPassword").textContent = "顯示";
      byId("toggleAdminPassword").setAttribute("aria-label", "顯示管理密碼");
      managementRequestActive = false;
      button.disabled = false;
    });
  }

  function logoutAdmin() {
    if (managementRequestActive) return;
    managementRequestActive = true;
    byId("dataLogout").disabled = true;
    adminMutation("/api/admin/session", { method: "DELETE" }).catch(function (error) {
      if (error && error.status !== 401) announce("登出請求未完成，但本頁憑證已清除", true);
    }).finally(function () {
      lockDataManagement("已登出資料管理。", false);
      managementRequestActive = false;
      byId("dataLogout").disabled = false;
      window.setTimeout(function () { byId("adminUsername").focus(); }, 0);
    });
  }

  function selectManagementTab(name, options) {
    var selected = document.querySelector('[data-management-tab="' + name + '"]');
    if (!selected) return;
    activeManagementTab = name;
    document.querySelectorAll("[data-management-tab]").forEach(function (tab) {
      var active = tab === selected;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll("[data-management-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-management-panel") !== name;
    });
    if (!options || options.focus !== false) selected.focus();
  }

  function handleManagementTabKeys(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-management-tab]"));
    var current = tabs.indexOf(event.target);
    if (current < 0) return;
    event.preventDefault();
    var next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
      (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    selectManagementTab(tabs[next].getAttribute("data-management-tab"));
  }

  function itemValue(item, names, fallback) {
    if (!item || typeof item !== "object") return fallback;
    for (var i = 0; i < names.length; i += 1) {
      if (item[names[i]] != null && item[names[i]] !== "") return item[names[i]];
    }
    return fallback;
  }

  function datasetIdentifier(item) {
    return String(itemValue(item, ["dataset", "dataset_id", "slot", "id", "key", "name"], ""));
  }

  function datasetFilename(item) {
    return String(itemValue(item, ["filename", "file_name", "display_name", "source_file", "path"], "未提供檔名"));
  }

  function dataItemState(item) {
    if (item && item.active === true) return "active";
    if (item && item.active === false) return "inactive";
    if (item && item.present === true) return "active";
    if (item && item.present === false) return "inactive";
    return String(itemValue(item, ["status", "state", "lifecycle_state", "result"], "active"));
  }

  function humanFileSize(value) {
    var size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "—";
    if (size < 1024) return size + " B";
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
    return (size / (1024 * 1024)).toFixed(1) + " MB";
  }

  function renderDatasetFiles() {
    var keyword = byId("datasetSearch").value.trim().toLowerCase();
    var filter = byId("datasetStateFilter").value;
    var filtered = datasetItems.filter(function (item) {
      var bucket = stateBucket(dataItemState(item));
      return (!keyword || JSON.stringify(item).toLowerCase().indexOf(keyword) >= 0) &&
        (filter === "all" || bucket === filter);
    });
    var holder = byId("datasetFiles");
    holder.replaceChildren();
    byId("datasetResultCount").textContent = valueText(filtered.length) + " 份";
    if (!filtered.length) {
      holder.appendChild(element("div", "empty-state", datasetItems.length ? "沒有符合條件的資料檔。" : "目前沒有已登錄的資料檔。"));
      return;
    }
    filtered.forEach(function (item) {
      var index = datasetItems.indexOf(item);
      var article = element("article", "management-entry dataset-entry");
      var body = element("div", "management-entry-body");
      body.appendChild(element("h4", "", datasetIdentifier(item) || datasetFilename(item)));
      var meta = element("div", "entry-meta");
      meta.appendChild(element("span", "state-chip " + stateBucket(dataItemState(item)), statusLabel(dataItemState(item))));
      var version = itemValue(item, ["version", "database_version", "data_version"], null);
      if (version) meta.appendChild(element("span", "", String(version)));
      var rows = itemValue(item, ["row_count", "rows", "records"], null);
      if (rows != null && !Array.isArray(rows)) meta.appendChild(element("span", "", valueText(Number(rows)) + " 列"));
      body.appendChild(meta);
      body.appendChild(element("p", "", datasetFilename(item)));
      article.appendChild(body);
      var canRemove = stateBucket(dataItemState(item)) === "active" && item.removable !== false;
      var open = element("button", "entry-open", canRemove ? "查看與停用" : "查看內容");
      open.type = "button";
      open.setAttribute("data-dataset-index", String(index));
      article.appendChild(open);
      holder.appendChild(article);
    });
  }

  function renderDataStatus(data) {
    datasetItems = firstArray(data, ["datasets", "files", "sources", "items"]);
    var supported = data.supported_slots || {};
    var activeVersion = itemValue(data, ["active_database_version", "active_version", "database_version", "version"], null);
    datasetItems = datasetItems.map(function (item) {
      return Object.assign({ version: activeVersion }, supported[datasetIdentifier(item)] || {}, item);
    });
    var counts = data.counts || data.summary || {};
    var active = datasetItems.filter(function (item) { return stateBucket(dataItemState(item)) === "active"; }).length;
    byId("activeDatasetCount").textContent = valueText(itemValue(counts, ["active_datasets", "active_files", "active"], active));
    byId("activeDatabaseVersion").textContent = valueText(activeVersion || "—");
    var datasetSelect = byId("datasetName");
    var selectedDataset = datasetSelect.value;
    datasetSelect.replaceChildren(element("option", "", "選擇要新增或替換的資料槽"));
    datasetSelect.firstChild.value = "";
    Object.keys(supported).forEach(function (identifier) {
      var option = element("option", "", supported[identifier].filename ? identifier + " · " + supported[identifier].filename : identifier);
      option.value = identifier;
      datasetSelect.appendChild(option);
    });
    if (Array.prototype.some.call(datasetSelect.options, function (option) { return option.value === selectedDataset; })) datasetSelect.value = selectedDataset;
    renderDatasetFiles();
  }

  function loadDataStatus() {
    return adminApi("/api/data/status").then(function (payload) {
      renderDataStatus(businessData(payload));
    }).catch(function (error) {
      byId("datasetFiles").replaceChildren(element("div", "empty-state", "資料檔讀取失敗：" + error.message));
      byId("datasetResultCount").textContent = "讀取失敗";
    });
  }

  function changeActionLabel(value) {
    var labels = {
      upload: "上傳資料檔",
      add: "新增資料集",
      replace: "替換資料檔",
      remove: "停用資料檔",
      disable: "停用資料檔",
      rollback: "回退資料庫版本"
    };
    return labels[value] || String(value || "資料異動");
  }

  function renderDataChanges() {
    var keyword = byId("dataChangeSearch").value.trim().toLowerCase();
    var filter = byId("dataChangeStateFilter").value;
    var pending = dataChanges.filter(function (item) { return stateBucket(dataItemState(item)) === "pending"; }).length;
    byId("pendingChangeCount").textContent = valueText(pending);
    byId("dataChangesBadge").textContent = valueText(pending);
    var filtered = dataChanges.filter(function (item) {
      return (!keyword || JSON.stringify(item).toLowerCase().indexOf(keyword) >= 0) &&
        (filter === "all" || stateBucket(dataItemState(item)) === filter);
    });
    var holder = byId("dataChanges");
    holder.replaceChildren();
    byId("dataChangeResultCount").textContent = valueText(filtered.length) + " 筆";
    if (!filtered.length) {
      holder.appendChild(element("div", "empty-state", dataChanges.length ? "沒有符合條件的資料異動。" : "目前沒有資料異動。"));
      return;
    }
    filtered.forEach(function (item) {
      var index = dataChanges.indexOf(item);
      var article = element("article", "management-entry change-entry");
      var body = element("div", "management-entry-body");
      var action = itemValue(item, ["action", "operation", "type", "kind"], "change");
      body.appendChild(element("h4", "", changeActionLabel(action) + " · " + (datasetIdentifier(item) || "未命名資料集")));
      var meta = element("div", "entry-meta");
      meta.appendChild(element("span", "state-chip " + stateBucket(dataItemState(item)), statusLabel(dataItemState(item))));
      var actor = itemValue(item, ["actor", "created_by", "requested_by", "username"], null);
      if (actor) meta.appendChild(element("span", "", String(actor)));
      body.appendChild(meta);
      body.appendChild(element("p", "", String(itemValue(item, ["request_reason", "reason", "note", "message", "original_filename", "filename"], "尚無異動說明"))));
      article.appendChild(body);
      var open = element("button", "entry-open", stateBucket(dataItemState(item)) === "pending" ? "開啟審核" : "查看紀錄");
      open.type = "button";
      open.setAttribute("data-change-index", String(index));
      article.appendChild(open);
      holder.appendChild(article);
    });
  }

  function loadDataChanges() {
    return adminApi("/api/data/changes?limit=500").then(function (payload) {
      var data = businessData(payload);
      dataChanges = firstArray(data, ["changes", "items", "entries"]);
      renderDataChanges();
    }).catch(function (error) {
      dataChanges = [];
      renderDataChanges();
      byId("dataChanges").replaceChildren(element("div", "empty-state", "異動紀錄讀取失敗：" + error.message));
    });
  }

  function renderDatabaseVersions() {
    var holder = byId("databaseVersions");
    holder.replaceChildren();
    byId("databaseVersionCount").textContent = valueText(databaseVersions.length) + " 版";
    if (!databaseVersions.length) {
      holder.appendChild(element("div", "empty-state", "目前沒有可調閱的資料庫版本。"));
      return;
    }
    databaseVersions.forEach(function (item) {
      var article = element("article", "management-entry version-entry");
      var body = element("div", "management-entry-body");
      var version = itemValue(item, ["version", "id", "database_version", "name"], "未命名版本");
      body.appendChild(element("h4", "", String(version)));
      var meta = element("div", "entry-meta");
      var versionState = item.active === true ? "active" : "archived";
      meta.appendChild(element("span", "state-chip " + stateBucket(versionState), statusLabel(versionState)));
      var sources = itemValue(item, ["source_count", "dataset_count", "file_count"], null);
      if (sources != null) meta.appendChild(element("span", "", valueText(Number(sources)) + " 份資料檔"));
      body.appendChild(meta);
      var checksum = itemValue(item, ["sha256", "checksum", "database_sha256", "database_checksum"], null);
      body.appendChild(element("p", "mono-copy", checksum ? "SHA-256 " + checksum : "建立時間 " + formatDate(itemValue(item, ["published_at", "created_at", "at", "timestamp"], null))));
      article.appendChild(body);
      if (item.active !== true) {
        var rollback = element("button", "entry-open", "建立回退異動");
        rollback.type = "button";
        rollback.setAttribute("data-version-index", String(databaseVersions.indexOf(item)));
        article.appendChild(rollback);
      }
      holder.appendChild(article);
    });
  }

  function loadDatabaseVersions() {
    return adminApi("/api/data/versions?limit=200").then(function (payload) {
      var data = businessData(payload);
      databaseVersions = firstArray(data, ["versions", "items", "entries"]);
      renderDatabaseVersions();
    }).catch(function (error) {
      databaseVersions = [];
      renderDatabaseVersions();
      byId("databaseVersions").replaceChildren(element("div", "empty-state", "版本讀取失敗：" + error.message));
    });
  }

  function auditResultBucket(item) {
    var state = String(itemValue(item, ["result", "status", "outcome", "state", "event", "action"], "success")).toLowerCase();
    if (/pending|staged|validating|待|處理/.test(state)) return "pending";
    if (/fail|error|reject|denied|失敗|拒絕/.test(state)) return "failed";
    return "success";
  }

  function renderAuditEvents() {
    var keyword = byId("auditSearch").value.trim().toLowerCase();
    var filter = byId("auditResultFilter").value;
    var filtered = auditEvents.filter(function (item) {
      return (!keyword || JSON.stringify(item).toLowerCase().indexOf(keyword) >= 0) &&
        (filter === "all" || auditResultBucket(item) === filter);
    });
    var holder = byId("auditEvents");
    holder.replaceChildren();
    byId("auditEventCount").textContent = valueText(filtered.length) + " 筆";
    if (!filtered.length) {
      holder.appendChild(element("li", "empty-state", auditEvents.length ? "沒有符合條件的稽核紀錄。" : "目前沒有稽核紀錄。"));
      return;
    }
    filtered.forEach(function (event) {
      var details = event.details && typeof event.details === "object" ? event.details : {};
      var item = element("li", "audit-item");
      var head = element("div", "audit-head");
      head.appendChild(element("strong", "", eventLabel(itemValue(event, ["event", "action", "type", "kind"], "data_change"))));
      var resultState = itemValue(event, ["result", "status", "outcome"], null) || (auditResultBucket(event) === "failed" ? "failed" : auditResultBucket(event) === "pending" ? "pending" : "success");
      head.appendChild(element("span", "state-chip " + stateBucket(resultState), statusLabel(resultState)));
      item.appendChild(head);
      var description = itemValue(event, ["message", "description", "reason", "dataset", "target"], null);
      if (!description) {
        var detailAction = itemValue(details, ["action", "operation"], null);
        var detailTarget = itemValue(details, ["slot", "dataset", "active_version", "candidate_version", "change_id"], null);
        description = [detailAction ? changeActionLabel(detailAction) : null, detailTarget].filter(Boolean).join(" · ") || "資料管理內容已更新";
      }
      item.appendChild(element("p", "", String(description)));
      var meta = element("div", "audit-meta");
      var actor = itemValue(event, ["actor", "reviewer", "username", "created_by"], null);
      if (actor) meta.appendChild(element("span", "", "人員：" + actor));
      meta.appendChild(element("time", "", formatDate(itemValue(event, ["at", "created_at", "timestamp", "updated_at"], null))));
      var correlation = itemValue(event, ["correlation_id", "request_id", "id", "event_hash"], null);
      if (correlation) meta.appendChild(element("span", "mono-copy", "ID " + correlation));
      item.appendChild(meta);
      holder.appendChild(item);
    });
  }

  function loadAuditEvents() {
    return adminApi("/api/data/events?limit=500").then(function (payload) {
      var data = businessData(payload);
      auditEvents = firstArray(data, ["events", "items", "entries"]);
      renderAuditEvents();
    }).catch(function (error) {
      auditEvents = [];
      renderAuditEvents();
      byId("auditEvents").replaceChildren(element("li", "empty-state", "稽核紀錄讀取失敗：" + error.message));
    });
  }

  function refreshDataManagement(announceResult) {
    if (!adminSession) return Promise.resolve();
    var button = byId("refreshDataManagement");
    button.disabled = true;
    return Promise.all([
      loadDataStatus(),
      loadDataChanges(),
      loadDatabaseVersions(),
      loadAuditEvents(),
      loadCorpus(false),
      loadTraining(false),
      loadRuntime(false)
    ]).then(function () {
      if (announceResult && adminSession) announce("資料管理狀態已同步", false);
    }).finally(function () {
      button.disabled = false;
    });
  }

  function adminMutation(path, options) {
    if (!adminCsrfToken) {
      return Promise.reject(new Error("管理工作階段缺少 CSRF 驗證，請重新登入。"));
    }
    return adminApi(path, options);
  }

  function fileToBase64(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.addEventListener("load", function () {
        var result = String(reader.result || "");
        var marker = result.indexOf(",");
        if (marker < 0) reject(new Error("無法將 CSV 轉換為上傳內容"));
        else resolve(result.slice(marker + 1));
      });
      reader.addEventListener("error", function () { reject(new Error("無法讀取選取的 CSV 檔案")); });
      reader.readAsDataURL(file);
    });
  }

  function setFormDisabled(formElement, disabled) {
    formElement.querySelectorAll("input, select, textarea, button").forEach(function (control) {
      control.disabled = disabled;
    });
  }

  function stageDatasetUpload(event) {
    event.preventDefault();
    if (managementRequestActive) return;
    var formElement = byId("datasetUploadForm");
    if (!formElement.reportValidity()) return;
    var fileInput = byId("datasetFileInput");
    var file = fileInput.files && fileInput.files[0];
    var feedback = byId("datasetUploadFeedback");
    if (!file || !/\.csv$/i.test(file.name)) {
      fileInput.setCustomValidity("請選擇 CSV 檔案。" );
      fileInput.reportValidity();
      fileInput.setCustomValidity("");
      return;
    }
    if (file.size > DATA_UPLOAD_MAX_BYTES) {
      fileInput.setCustomValidity("CSV 不可超過 64 MB。" );
      fileInput.reportValidity();
      fileInput.setCustomValidity("");
      return;
    }
    managementRequestActive = true;
    setFormDisabled(formElement, true);
    byId("datasetUploadProgress").hidden = false;
    feedback.className = "inline-feedback";
    feedback.textContent = "正在讀取並上傳 CSV；完成驗證後會進入待審異動。";
    feedback.hidden = false;
    fileToBase64(file).then(function (contentBase64) {
      return adminMutation("/api/data/changes/upload", {
        method: "POST",
        body: {
          dataset: byId("datasetName").value.trim(),
          filename: file.name,
          content_base64: contentBase64,
          reason: byId("datasetChangeReason").value.trim()
        }
      });
    }).then(function (payload) {
      businessData(payload);
      formElement.reset();
      byId("datasetFileName").textContent = "尚未選擇檔案（上限 64 MB）";
      feedback.className = "inline-feedback";
      feedback.textContent = "CSV 已上傳並送入待審異動；尚未影響目前查詢資料。";
      selectManagementTab("changes", { focus: false });
      announce("資料檔已上傳並送審", false);
      return refreshDataManagement(false);
    }).catch(function (error) {
      feedback.className = "inline-feedback error";
      feedback.textContent = "CSV 未送審：" + error.message;
      announce(error.message, true);
    }).finally(function () {
      managementRequestActive = false;
      setFormDisabled(formElement, false);
      byId("datasetUploadProgress").hidden = true;
    });
  }

  function openModal(dialog, closeButtonId) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    window.setTimeout(function () { byId(closeButtonId).focus(); }, 0);
  }

  function closeModal(dialog, returnFocus) {
    if (typeof dialog.close === "function") dialog.close();
    else {
      dialog.removeAttribute("open");
      if (returnFocus && typeof returnFocus.focus === "function") returnFocus.focus();
    }
  }

  function stageDatasetRemove(item, reasonInput, formElement, feedback) {
    var dataset = datasetIdentifier(item);
    if (!dataset || !reasonInput.reportValidity() || managementRequestActive) return;
    managementRequestActive = true;
    setFormDisabled(formElement, true);
    feedback.className = "review-feedback";
    feedback.textContent = "正在建立停用異動…";
    feedback.hidden = false;
    adminMutation("/api/data/changes/remove", {
      method: "POST",
      body: { dataset: dataset, reason: reasonInput.value.trim() }
    }).then(function (payload) {
      businessData(payload);
      closeDatasetDialog();
      selectManagementTab("changes", { focus: false });
      announce("停用要求已送交人工審核", false);
      return refreshDataManagement(false);
    }).catch(function (error) {
      feedback.className = "review-feedback error";
      feedback.textContent = "停用異動未建立：" + error.message;
      announce(error.message, true);
    }).finally(function () {
      managementRequestActive = false;
      if (formElement.isConnected) setFormDisabled(formElement, false);
    });
  }

  function openDatasetDialog(index, trigger) {
    var item = datasetItems[index];
    if (!item) return;
    datasetDialogReturnFocus = trigger || document.activeElement;
    var body = byId("datasetDialogBody");
    body.replaceChildren();
    var grid = element("dl", "detail-grid");
    appendDetail(grid, "資料集", datasetIdentifier(item));
    appendDetail(grid, "檔名", datasetFilename(item));
    appendDetail(grid, "狀態", statusLabel(dataItemState(item)));
    appendDetail(grid, "版本", itemValue(item, ["version", "data_version", "database_version"], null));
    appendDetail(grid, "資料列", itemValue(item, ["row_count", "rows", "records"], null));
    appendDetail(grid, "檔案大小", humanFileSize(itemValue(item, ["size_bytes", "file_size", "bytes"], NaN)));
    appendDetail(grid, "啟用時間", formatDate(itemValue(item, ["activated_at", "updated_at", "created_at"], null)));
    if (grid.childNodes.length) body.appendChild(grid);
    var checksum = itemValue(item, ["sha256", "checksum", "file_checksum"], null);
    if (checksum) {
      var checksumSection = element("section", "dialog-section");
      checksumSection.appendChild(element("h3", "", "SHA-256"));
      checksumSection.appendChild(element("p", "mono-copy", String(checksum)));
      body.appendChild(checksumSection);
    }
    if (item.present !== false && datasetIdentifier(item)) {
      var downloadSection = element("section", "dialog-section");
      var download = element("a", "source-file-download", "下載這個版本的 CSV");
      download.href = "/api/data/files/" + encodeURIComponent(datasetIdentifier(item)) + (item.version ? "?version=" + encodeURIComponent(item.version) : "");
      download.setAttribute("download", "");
      downloadSection.appendChild(download);
      body.appendChild(downloadSection);
    }
    var tables = firstArray(item, ["tables", "views", "targets"]);
    if (tables.length) {
      var tableSection = element("section", "dialog-section");
      tableSection.appendChild(element("h3", "", "匯入資料表／檢視"));
      tableSection.appendChild(element("p", "", tables.map(function (value) {
        return typeof value === "object" ? itemValue(value, ["name", "table", "view"], JSON.stringify(value)) : value;
      }).join("、")));
      body.appendChild(tableSection);
    }
    if (stateBucket(dataItemState(item)) === "active" && item.removable !== false) {
      var section = element("section", "dialog-section destructive-section");
      section.setAttribute("aria-labelledby", "datasetRemoveTitle");
      var title = element("h3", "", "建立停用異動");
      title.id = "datasetRemoveTitle";
      section.appendChild(title);
      section.appendChild(element("p", "", "資料不會立即移除；必須在待審異動中由管理員核准，才會建立並切換新版資料庫。"));
      var removeForm = element("form", "review-form");
      var label = element("label", "review-field");
      label.appendChild(element("span", "", "停用原因"));
      var reason = element("textarea");
      reason.name = "reason";
      reason.rows = 3;
      reason.maxLength = 500;
      reason.required = true;
      reason.placeholder = "說明為何停用這份資料檔";
      label.appendChild(reason);
      removeForm.appendChild(label);
      var feedback = element("div", "review-feedback");
      feedback.hidden = true;
      feedback.setAttribute("role", "status");
      removeForm.appendChild(feedback);
      var actions = element("div", "review-actions");
      var remove = element("button", "review-button reject", "送出停用審核");
      remove.type = "submit";
      actions.appendChild(remove);
      removeForm.appendChild(actions);
      removeForm.addEventListener("submit", function (event) {
        event.preventDefault();
        stageDatasetRemove(item, reason, removeForm, feedback);
      });
      section.appendChild(removeForm);
      body.appendChild(section);
    }
    var raw = element("details", "dialog-section");
    raw.appendChild(element("summary", "", "查看完整資料檔欄位"));
    raw.appendChild(element("pre", "", JSON.stringify(item, null, 2)));
    body.appendChild(raw);
    openModal(byId("datasetDialog"), "closeDatasetDialog");
  }

  function closeDatasetDialog() {
    closeModal(byId("datasetDialog"), datasetDialogReturnFocus);
  }

  function reviewDataChange(item, decision, noteInput, formElement, feedback) {
    var identifier = String(itemValue(item, ["id", "change_id", "request_id"], ""));
    if (!identifier || managementRequestActive || !noteInput.reportValidity()) return;
    managementRequestActive = true;
    setFormDisabled(formElement, true);
    feedback.className = "review-feedback";
    feedback.textContent = decision === "approve" ? "正在驗證、建立並切換新版資料庫…" : "正在拒絕資料異動…";
    feedback.hidden = false;
    adminMutation("/api/data/changes/" + encodeURIComponent(identifier) + "/review", {
      method: "POST",
      body: { decision: decision, note: noteInput.value.trim() }
    }).then(function (payload) {
      businessData(payload);
      closeDataChangeDialog();
      announce(decision === "approve" ? "資料異動已核准並套用" : "資料異動已拒絕", false);
      loadHealth();
      loadStats(false);
      return refreshDataManagement(false);
    }).catch(function (error) {
      feedback.className = "review-feedback error";
      feedback.textContent = "資料異動審核未完成：" + error.message;
      announce(error.message, true);
    }).finally(function () {
      managementRequestActive = false;
      if (formElement.isConnected) setFormDisabled(formElement, false);
    });
  }

  function openDataChangeDialog(index, trigger) {
    var item = dataChanges[index];
    if (!item) return;
    dataChangeDialogReturnFocus = trigger || document.activeElement;
    byId("dataChangeDialogTitle").textContent = "資料異動審查";
    var body = byId("dataChangeDialogBody");
    body.replaceChildren();
    var grid = element("dl", "detail-grid");
    appendDetail(grid, "異動編號", itemValue(item, ["id", "change_id", "request_id"], null));
    appendDetail(grid, "動作", changeActionLabel(itemValue(item, ["action", "operation", "type", "kind"], null)));
    appendDetail(grid, "資料集", datasetIdentifier(item));
    appendDetail(grid, "檔名", itemValue(item, ["original_filename", "filename", "file_name"], null));
    appendDetail(grid, "基礎版本", item.base_version);
    appendDetail(grid, "候選版本", item.candidate_version);
    appendDetail(grid, "候選 DB checksum", item.database_sha256);
    appendDetail(grid, "狀態", statusLabel(dataItemState(item)));
    appendDetail(grid, "提出人", itemValue(item, ["actor", "created_by", "requested_by"], null));
    appendDetail(grid, "建立時間", formatDate(itemValue(item, ["created_at", "at", "timestamp"], null)));
    appendDetail(grid, "資料列", itemValue(item, ["row_count", "rows", "records"], null));
    if (grid.childNodes.length) body.appendChild(grid);
    var reasonSection = element("section", "dialog-section");
    reasonSection.appendChild(element("h3", "", "異動原因"));
    reasonSection.appendChild(element("p", "", String(itemValue(item, ["request_reason", "reason", "note", "description"], "未提供"))));
    body.appendChild(reasonSection);
    var validation = itemValue(item, ["build_report", "validation", "checks", "preview", "diff"], null);
    if (validation) {
      var validationSection = element("details", "dialog-section validation-section");
      validationSection.open = true;
      validationSection.appendChild(element("summary", "", "驗證結果與差異預覽"));
      validationSection.appendChild(element("pre", "", JSON.stringify(validation, null, 2)));
      body.appendChild(validationSection);
    }
    if (stateBucket(dataItemState(item)) === "pending") {
      var reviewSection = element("section", "dialog-section review-section");
      reviewSection.setAttribute("aria-labelledby", "dataChangeReviewTitle");
      var title = element("h3", "", "人工審核");
      title.id = "dataChangeReviewTitle";
      reviewSection.appendChild(title);
      reviewSection.appendChild(element("p", "", "核准後才會建立並切換資料庫版本；審核人員取自目前登入帳號。"));
      var reviewForm = element("form", "review-form");
      var label = element("label", "review-field");
      label.appendChild(element("span", "", "審核說明"));
      var note = element("textarea");
      note.name = "note";
      note.rows = 3;
      note.maxLength = 500;
      note.required = true;
      note.placeholder = "記錄核准或拒絕的判斷依據";
      label.appendChild(note);
      reviewForm.appendChild(label);
      var feedback = element("div", "review-feedback");
      feedback.hidden = true;
      feedback.setAttribute("role", "status");
      reviewForm.appendChild(feedback);
      var actions = element("div", "review-actions");
      var reject = element("button", "review-button reject", "拒絕異動");
      reject.type = "button";
      reject.addEventListener("click", function () { reviewDataChange(item, "reject", note, reviewForm, feedback); });
      var approve = element("button", "review-button", "核准並套用");
      approve.type = "button";
      approve.addEventListener("click", function () { reviewDataChange(item, "approve", note, reviewForm, feedback); });
      actions.appendChild(reject);
      actions.appendChild(approve);
      reviewForm.appendChild(actions);
      reviewForm.addEventListener("submit", function (event) { event.preventDefault(); });
      reviewSection.appendChild(reviewForm);
      body.appendChild(reviewSection);
    }
    var raw = element("details", "dialog-section");
    raw.appendChild(element("summary", "", "查看完整異動欄位"));
    raw.appendChild(element("pre", "", JSON.stringify(item, null, 2)));
    body.appendChild(raw);
    openModal(byId("dataChangeDialog"), "closeDataChangeDialog");
  }

  function closeDataChangeDialog() {
    closeModal(byId("dataChangeDialog"), dataChangeDialogReturnFocus);
  }

  function stageVersionRollback(item, reasonInput, formElement, feedback) {
    var version = String(itemValue(item, ["version", "id", "database_version"], ""));
    if (!version || managementRequestActive || !reasonInput.reportValidity()) return;
    managementRequestActive = true;
    setFormDisabled(formElement, true);
    feedback.className = "review-feedback";
    feedback.textContent = "正在建立回退異動；目前作用中版本不會立即改變…";
    feedback.hidden = false;
    adminMutation("/api/data/versions/" + encodeURIComponent(version) + "/rollback", {
      method: "POST",
      body: { reason: reasonInput.value.trim() }
    }).then(function (payload) {
      businessData(payload);
      closeDataChangeDialog();
      selectManagementTab("changes", { focus: false });
      announce("回退要求已送交人工審核", false);
      return refreshDataManagement(false);
    }).catch(function (error) {
      feedback.className = "review-feedback error";
      feedback.textContent = "回退異動未建立：" + error.message;
      announce(error.message, true);
    }).finally(function () {
      managementRequestActive = false;
      if (formElement.isConnected) setFormDisabled(formElement, false);
    });
  }

  function openVersionRollbackDialog(index, trigger) {
    var item = databaseVersions[index];
    if (!item || item.active === true) return;
    dataChangeDialogReturnFocus = trigger || document.activeElement;
    byId("dataChangeDialogTitle").textContent = "建立回退異動";
    var body = byId("dataChangeDialogBody");
    body.replaceChildren();
    var version = String(itemValue(item, ["version", "id", "database_version"], ""));
    var grid = element("dl", "detail-grid");
    appendDetail(grid, "目標版本", version);
    appendDetail(grid, "建立時間", formatDate(itemValue(item, ["published_at", "created_at", "at"], null)));
    appendDetail(grid, "發布人員", itemValue(item, ["published_by", "actor", "created_by"], null));
    appendDetail(grid, "DB checksum", itemValue(item, ["database_sha256", "sha256", "checksum"], null));
    if (grid.childNodes.length) body.appendChild(grid);
    var section = element("section", "dialog-section review-section");
    section.setAttribute("aria-labelledby", "rollbackReviewTitle");
    var title = element("h3", "", "送出回退申請");
    title.id = "rollbackReviewTitle";
    section.appendChild(title);
    section.appendChild(element("p", "", "回退只會建立待審異動，不會立即切換資料庫；人工核准後才正式套用。"));
    var formElement = element("form", "review-form");
    var label = element("label", "review-field");
    label.appendChild(element("span", "", "回退原因"));
    var reason = element("textarea");
    reason.name = "reason";
    reason.rows = 3;
    reason.maxLength = 500;
    reason.required = true;
    reason.placeholder = "說明需要回到這個版本的原因";
    label.appendChild(reason);
    formElement.appendChild(label);
    var feedback = element("div", "review-feedback");
    feedback.hidden = true;
    feedback.setAttribute("role", "status");
    formElement.appendChild(feedback);
    var actions = element("div", "review-actions");
    var submit = element("button", "review-button", "建立回退異動");
    submit.type = "submit";
    actions.appendChild(submit);
    formElement.appendChild(actions);
    formElement.addEventListener("submit", function (event) {
      event.preventDefault();
      stageVersionRollback(item, reason, formElement, feedback);
    });
    section.appendChild(formElement);
    body.appendChild(section);
    openModal(byId("dataChangeDialog"), "closeDataChangeDialog");
  }

  function stateBucket(value) {
    var state = String(value || "").toLowerCase();
    if (/validating|pending|queue|train|review|draft|待|訓練|審核|排程|新增/.test(state)) return "pending";
    if (/ignored|inactive|disabled|reject|error|fail|停用|拒絕|失敗|忽略/.test(state)) return "inactive";
    if (/promoted|active|enabled|approved|applied|success|ready|trained|publish|啟用|核准|套用|成功|就緒|完成/.test(state)) return "active";
    return "unknown";
  }

  function statusLabel(value) {
    var labels = {
      validating: "驗證中",
      pending_review: "待人工審核",
      pending: "待處理",
      staged: "已送審",
      promoted: "已提升發布",
      approved: "已核准",
      applied: "已套用",
      active: "已啟用",
      archived: "歷史版本",
      success: "成功",
      rejected: "已拒絕",
      failed: "失敗",
      inactive: "已停用",
      removed: "已停用",
      ignored: "已忽略",
      error: "處理失敗"
    };
    return labels[value] || String(value || "未標示");
  }

  function eventLabel(value) {
    var labels = {
      candidate_staged: "已建立學習候選",
      duplicate_candidate: "發現重複候選",
      candidate_pending_review: "候選待人工審核",
      candidate_promoted: "候選已發布至語料",
      candidate_rejected: "候選未通過驗證",
      candidate_ignored: "已忽略重複候選",
      learning_error: "學習流程發生錯誤",
      data_change_staged: "資料異動已送審",
      data_change_approved: "資料異動已核准",
      data_change_rejected: "資料異動已拒絕",
      data_version_activated: "資料庫版本已切換",
      dataset_uploaded: "資料檔已上傳",
      dataset_removed: "資料檔已停用",
      change_staged: "資料異動已送審",
      change_approved: "資料異動已核准",
      change_rejected: "資料異動已拒絕",
      change_build_failed: "候選資料庫建置失敗",
      change_publish_failed: "資料庫版本切換失敗",
      change_conflict: "資料異動版本衝突",
      login: "管理員登入",
      logout: "管理員登出"
    };
    return labels[value] || String(value || "語料異動");
  }

  function entryState(item) {
    return item.state || item.status || item.lifecycle_state || item.training_status || "未標示";
  }

  function entryQuestion(item) {
    return item.question || item.canonical_question || item.utterance || item.text || item.content || item.name || "未命名語料";
  }

  function entryDescription(item) {
    return item.description || item.answer || item.explanation || item.sql || item.notes || "尚無補充說明";
  }


  function corpusSubmitParams() {
    return byId("corpusSubmitParams").value
      .split("\n")
      .map(function (line) { return line.trim(); })
      .filter(function (line) { return line.length > 0; });
  }

  function submitCorpusEntry(event) {
    event.preventDefault();
    if (managementRequestActive) return;
    var formElement = byId("corpusSubmitForm");
    if (!formElement.reportValidity()) return;
    var feedback = byId("corpusSubmitFeedback");
    var button = byId("corpusSubmitButton");
    managementRequestActive = true;
    button.disabled = true;
    feedback.className = "review-feedback";
    feedback.textContent = "正在驗證並送出候選…";
    feedback.hidden = false;
    adminMutation("/api/corpus/entries", {
      method: "POST",
      body: {
        question: byId("corpusSubmitQuestion").value.trim(),
        sql: byId("corpusSubmitSql").value.trim(),
        params: corpusSubmitParams(),
        intent: byId("corpusSubmitIntent").value.trim()
      }
    }).then(function (payload) {
      businessData(payload);
      byId("corpusSubmitQuestion").value = "";
      byId("corpusSubmitSql").value = "";
      byId("corpusSubmitParams").value = "";
      byId("corpusSubmitIntent").value = "";
      feedback.className = "review-feedback";
      feedback.textContent = "候選已送出，等待審核。";
      return refreshDataManagement(false);
    }).then(function () {
      announce("語料候選已送出，等待審核", false);
    }).catch(function (error) {
      feedback.className = "review-feedback error";
      feedback.textContent = "候選未送出：" + error.message;
      feedback.hidden = false;
      announce(error.message, true);
    }).finally(function () {
      managementRequestActive = false;
      button.disabled = false;
    });
  }

  function corpusDataSources(item) {
    var direct = firstArray(item, ["data_sources", "source_files", "datasets", "files"]);
    if (direct.length) return direct;
    return firstArray(item && (item.data_provenance || item.provenance), ["data_sources", "source_files", "datasets", "files"]);
  }

  function renderCorpusList() {
    var keyword = byId("corpusSearch").value.trim().toLowerCase();
    var stateFilter = byId("corpusStateFilter").value;
    var filtered = corpusItems.filter(function (item) {
      var matchesText = !keyword || JSON.stringify(item).toLowerCase().indexOf(keyword) >= 0;
      var bucket = stateBucket(entryState(item));
      return matchesText && (stateFilter === "all" || bucket === stateFilter);
    });
    var holder = byId("corpusEntries");
    holder.replaceChildren();
    byId("corpusResultCount").textContent = valueText(filtered.length) + " 筆";
    if (!filtered.length) {
      holder.appendChild(element("div", "empty-state", corpusItems.length ? "沒有符合篩選條件的語料。" : "目前沒有可調閱的語料。"));
      return;
    }
    filtered.forEach(function (item) {
      var index = corpusItems.indexOf(item);
      var article = element("article", "corpus-entry");
      var body = element("div");
      body.appendChild(element("h4", "", entryQuestion(item)));
      var meta = element("div", "entry-meta");
      var status = element("span", "state-chip " + stateBucket(entryState(item)), statusLabel(entryState(item)));
      status.title = "原始狀態：" + entryState(item);
      meta.appendChild(status);
      if (item.intent || item.category) meta.appendChild(element("span", "", item.intent || item.category));
      if (item.source) meta.appendChild(element("span", "", item.source));
      var sources = corpusDataSources(item);
      if (sources.length) {
        var firstSource = sources[0];
        var sourceName = typeof firstSource === "object" ? datasetFilename(firstSource) : String(firstSource);
        meta.appendChild(element("span", "", "資料：" + sourceName + (sources.length > 1 ? " 等 " + sources.length + " 份" : "")));
      }
      body.appendChild(meta);
      article.appendChild(body);
      var open = element("button", "entry-open", "查看內容");
      open.type = "button";
      open.setAttribute("data-corpus-index", String(index));
      article.appendChild(open);
      article.appendChild(element("p", "", entryDescription(item)));
      holder.appendChild(article);
    });
  }

  function renderCorpusEvents() {
    var holder = byId("corpusEvents");
    holder.replaceChildren();
    if (!corpusEvents.length) {
      holder.appendChild(element("li", "empty-state", "尚無異動資料"));
      return;
    }
    corpusEvents.slice(0, 12).forEach(function (event) {
      var item = element("li", "activity-item");
      item.appendChild(element("strong", "", eventLabel(event.event || event.action || event.type || event.status)));
      item.appendChild(element("p", "", event.message || event.description || event.question || event.candidate_id || event.entry_id || event.reason || "內容已更新"));
      var time = element("time", "", formatDate(event.at || event.created_at || event.timestamp || event.updated_at));
      item.appendChild(time);
      holder.appendChild(item);
    });
  }

  function countValue(counts, names, fallback) {
    for (var i = 0; i < names.length; i += 1) {
      if (counts && counts[names[i]] != null) return Number(counts[names[i]]);
    }
    return fallback;
  }

  function renderCorpus(data) {
    corpusItems = Array.isArray(data) ? data : Array.isArray(data.items) ? data.items : Array.isArray(data.entries) ? data.entries : [];
    corpusEvents = Array.isArray(data.events) ? data.events : Array.isArray(data.activity) ? data.activity : [];
    var counts = trainingCandidateCounts || data.counts || data.summary || {};
    var activeCount = corpusItems.filter(function (item) { return stateBucket(entryState(item)) === "active"; }).length;
    var pendingCount = corpusItems.filter(function (item) { return stateBucket(entryState(item)) === "pending"; }).length;
    byId("corpusTotal").textContent = valueText(countValue(counts, ["total", "all", "entry_count"], corpusItems.length));
    byId("corpusActive").textContent = valueText(countValue(counts, ["promoted", "active", "enabled", "approved", "ready"], activeCount));
    var exactPending = counts.validating != null || counts.pending_review != null ? Number(counts.validating || 0) + Number(counts.pending_review || 0) : null;
    var pendingTotal = exactPending != null ? exactPending : countValue(counts, ["pending", "queued", "training", "review"], pendingCount);
    byId("corpusPending").textContent = valueText(pendingTotal);
    byId("pendingCorpusCount").textContent = valueText(pendingTotal);
    byId("corpusPendingBadge").textContent = valueText(pendingTotal);
    byId("corpusEventCount").textContent = valueText(countValue(counts, ["events", "event_count", "changes"], corpusEvents.length));
    renderCorpusList();
    renderCorpusEvents();
  }

  function loadCorpus(announceResult) {
    var eventsRequest = adminApi("/api/corpus/events?limit=100").catch(function () {
      return { success: true, data: { events: [], returned: 0 } };
    });
    return Promise.all([adminApi("/api/corpus/entries?state=all&limit=500"), eventsRequest]).then(function (payloads) {
      var entryData = businessData(payloads[0]);
      var eventData = businessData(payloads[1]);
      renderCorpus(Object.assign({}, entryData, { events: eventData.events || [] }));
      if (announceResult) announce("語料內容已同步", false);
    }).catch(function (error) {
      corpusItems = [];
      corpusEvents = [];
      var holder = byId("corpusEntries");
      holder.replaceChildren(element("div", "empty-state", "目前無法讀取語料內容：" + error.message));
      byId("corpusResultCount").textContent = "讀取失敗";
      if (announceResult) announce(error.message, true);
    });
  }

  function renderTraining(payload) {
    var envelope = payload || {};
    var data = envelope.data || {};
    var status = envelope.training_status || data.training_status || data.status || (envelope.is_training_complete ? "語料索引已就緒" : "等待處理");
    var isComplete = envelope.is_training_complete === true || /完成|就緒|ready|complete/i.test(status);
    var isFailed = /失敗|錯誤|fail|error/i.test(status);
    var isRunning = !isComplete && !isFailed && /進行|訓練|索引|處理|running|training|index/i.test(status);
    var pulse = byId("trainingPulse");
    pulse.className = "training-pulse" + (isRunning ? " running" : "") + (isFailed ? " failed" : "");
    byId("trainingStatus").textContent = status;
    var hasDetailedStatus = Object.keys(data).length > 0;
    byId("trainingAuto").textContent = hasDetailedStatus ? (data.workspace_ready ? "已就緒" : "尚未就緒") : (isComplete ? "已就緒" : "—");
    byId("trainingIndexed").textContent = valueText(data.published_examples);
    var counts = data.candidate_counts || {};
    trainingCandidateCounts = counts;
    var pending = Number(counts.validating || 0) + Number(counts.pending_review || 0);
    byId("trainingPending").textContent = valueText(pending);
    byId("pendingCorpusCount").textContent = valueText(pending);
    byId("corpusPendingBadge").textContent = valueText(pending);
    byId("trainingUpdated").textContent = valueText(data.corpus_version);
    if (counts.total != null) {
      byId("corpusTotal").textContent = valueText(counts.total);
      byId("corpusActive").textContent = valueText(counts.promoted || 0);
      byId("corpusPending").textContent = valueText(pending);
    }
    var policyParts = ["所有新語料（包含離線規則與 LLM 來源）均需人工核准後才發布"];
    policyParts.push(hasDetailedStatus ? (data.index_synchronized ? "索引已同步" : "索引待同步") : (isComplete ? "索引已同步" : "詳細狀態需更新後端服務"));
    if (data.latest_event && data.latest_event.event) policyParts.push("最近事件：" + eventLabel(data.latest_event.event) + (data.latest_event.at ? "（" + formatDate(data.latest_event.at) + "）" : ""));
    byId("trainingPolicy").textContent = policyParts.join("；") + "。";
    var total = Number(counts.total || 0);
    var processed = Number(counts.promoted || 0) + Number(counts.rejected || 0) + Number(counts.ignored || 0);
    var rawProgress = data.progress_percent != null ? Number(data.progress_percent) : data.progress != null ? Number(data.progress) : total ? processed / total : NaN;
    var progress = rawProgress <= 1 ? rawProgress * 100 : rawProgress;
    if (Number.isFinite(progress)) {
      progress = Math.max(0, Math.min(100, progress));
      byId("trainingProgressWrap").hidden = false;
      byId("trainingProgress").value = progress;
      byId("trainingProgress").textContent = Math.round(progress) + "%";
      byId("trainingProgressText").textContent = Math.round(progress) + "%";
    } else {
      byId("trainingProgressWrap").hidden = true;
    }
    window.clearTimeout(trainingTimer);
    if (isRunning) trainingTimer = window.setTimeout(function () { loadTraining(false); }, 3000);
  }

  function loadTraining(announceResult) {
    window.clearTimeout(trainingTimer);
    return adminApi("/api/training-status").then(function (payload) {
      renderTraining(payload);
      if (announceResult) announce("訓練狀態已更新", false);
    }).catch(function (error) {
      byId("trainingPulse").className = "training-pulse failed";
      byId("trainingStatus").textContent = "狀態讀取失敗";
      if (announceResult) announce(error.message, true);
    });
  }

  function appendDetail(grid, label, value) {
    if (value == null || value === "") return;
    var row = element("div");
    row.appendChild(element("dt", "", label));
    row.appendChild(element("dd", "", valueText(value)));
    grid.appendChild(row);
  }

  function reviewCorpusEntry(item, decision, noteInput, formElement, feedback) {
    var candidateId = item.id || item.entry_id;
    if (!candidateId || ["approve", "reject"].indexOf(decision) < 0) return;
    if (!noteInput.reportValidity() || managementRequestActive) return;
    var buttons = formElement.querySelectorAll("button");
    managementRequestActive = true;
    buttons.forEach(function (button) { button.disabled = true; });
    feedback.className = "review-feedback";
    feedback.textContent = decision === "approve" ? "正在核准並重新驗證候選…" : "正在拒絕候選…";
    feedback.hidden = false;
    adminMutation("/api/corpus/entries/" + encodeURIComponent(candidateId) + "/review", {
      method: "POST",
      body: { decision: decision, note: noteInput.value.trim() }
    }).then(function (payload) {
      businessData(payload);
      noteInput.value = "";
      closeCorpusDialog();
      return refreshDataManagement(false);
    }).then(function () {
      announce(decision === "approve" ? "候選已核准，語料與索引狀態已更新" : "候選已拒絕，語料狀態已更新", false);
    }).catch(function (error) {
      feedback.className = "review-feedback error";
      feedback.textContent = "審核未完成：" + error.message;
      feedback.hidden = false;
      announce(error.message, true);
    }).finally(function () {
      managementRequestActive = false;
      if (formElement.isConnected) buttons.forEach(function (button) { button.disabled = false; });
    });
  }

  function buildReviewSection(item) {
    if (entryState(item) !== "pending_review" || !(item.id || item.entry_id)) return null;
    var section = element("section", "dialog-section review-section");
    section.setAttribute("aria-labelledby", "corpusReviewTitle");
    var title = element("h3", "", "人工審核");
    title.id = "corpusReviewTitle";
    section.appendChild(title);
    section.appendChild(element("p", "", "所有來源的候選都需由登入人員確認。核准時會重新執行安全、語意與回歸檢查，通過後才發布。"));
    var reviewForm = element("form", "review-form");
    var label = element("label", "review-field");
    label.appendChild(element("span", "", "審核說明"));
    var noteInput = element("textarea");
    noteInput.name = "note";
    noteInput.required = true;
    noteInput.maxLength = 500;
    noteInput.rows = 3;
    noteInput.placeholder = "記錄核准或拒絕的判斷依據";
    label.appendChild(noteInput);
    var privacy = element("small", "", "審核人員將記錄為目前登入帳號：" + (adminIdentity(adminSession) || "管理員"));
    label.appendChild(privacy);
    reviewForm.appendChild(label);
    var feedback = element("div", "review-feedback");
    feedback.hidden = true;
    feedback.setAttribute("role", "status");
    reviewForm.appendChild(feedback);
    var actions = element("div", "review-actions");
    var reject = element("button", "review-button reject", "拒絕候選");
    reject.type = "button";
    reject.addEventListener("click", function () { reviewCorpusEntry(item, "reject", noteInput, reviewForm, feedback); });
    var approve = element("button", "review-button", "核准並發布");
    approve.type = "button";
    approve.addEventListener("click", function () { reviewCorpusEntry(item, "approve", noteInput, reviewForm, feedback); });
    actions.appendChild(reject);
    actions.appendChild(approve);
    reviewForm.appendChild(actions);
    reviewForm.addEventListener("submit", function (event) { event.preventDefault(); });
    section.appendChild(reviewForm);
    return section;
  }

  function openCorpusDialog(index, trigger) {
    var item = corpusItems[index];
    if (!item) return;
    dialogReturnFocus = trigger || document.activeElement;
    var body = byId("corpusDialogBody");
    body.replaceChildren();
    var grid = element("dl", "detail-grid");
    appendDetail(grid, "識別碼", item.id || item.entry_id || item.key);
    appendDetail(grid, "狀態", entryState(item));
    appendDetail(grid, "意圖／分類", item.intent || item.category);
    appendDetail(grid, "產生來源", item.source);
    appendDetail(grid, "查詢資料表／檢視", Array.isArray(item.tables) ? item.tables.join("、") : item.tables);
    appendDetail(grid, "資料庫版本", item.database_version || (item.data_provenance && item.data_provenance.database_version) || item.data_manifest_version);
    appendDetail(grid, "結果列數", item.result_row_count || item.row_count);
    appendDetail(grid, "結果 checksum", item.result_checksum);
    appendDetail(grid, "建立時間", formatDate(item.created_at));
    appendDetail(grid, "更新時間", formatDate(item.updated_at || item.trained_at));
    if (grid.childNodes.length) body.appendChild(grid);
    var sources = corpusDataSources(item);
    var sourceSection = element("section", "dialog-section source-section");
    sourceSection.appendChild(element("h3", "", "查詢所使用的資料檔"));
    if (!sources.length) {
      sourceSection.appendChild(element("p", "", "這筆舊語料尚未記錄來源資料檔；可依資料庫版本與 SQL 追溯。"));
    } else {
      var sourceList = element("div", "source-file-list");
      sources.forEach(function (source) {
        var sourceItem = typeof source === "object" ? source : { dataset: source, filename: source };
        var sourceRow = element("div", "source-file-row");
        var button = element("button", "source-file-link");
        button.type = "button";
        button.setAttribute("data-source-dataset", datasetIdentifier(sourceItem) || datasetFilename(sourceItem));
        button.title = "前往目前作用中的同一資料槽；候選使用的歷史版本請由右側連結下載。";
        button.appendChild(element("strong", "", datasetFilename(sourceItem)));
        var detailParts = [datasetIdentifier(sourceItem)];
        var sourceVersion = itemValue(sourceItem, ["version", "data_version", "database_version"], null);
        if (sourceVersion) detailParts.push(String(sourceVersion));
        var sourceViews = firstArray(sourceItem, ["views", "tables"]);
        if (sourceViews.length) detailParts.push(sourceViews.map(String).join("、"));
        var sourceChecksum = itemValue(sourceItem, ["sha256", "checksum"], null);
        if (sourceChecksum) detailParts.push("SHA-256 " + String(sourceChecksum).slice(0, 12) + "…");
        button.appendChild(element("small", "", detailParts.filter(Boolean).join(" · ") || "查看資料檔"));
        sourceRow.appendChild(button);
        var sourceDataset = datasetIdentifier(sourceItem);
        if (sourceDataset && sourceItem.present !== false) {
          var download = element("a", "source-file-download", "下載該版本 CSV");
          var version = itemValue(sourceItem, ["version", "data_version", "database_version"], null);
          download.href = "/api/data/files/" + encodeURIComponent(sourceDataset) + (version ? "?version=" + encodeURIComponent(version) : "");
          download.setAttribute("download", "");
          sourceRow.appendChild(download);
        }
        sourceList.appendChild(sourceRow);
      });
      sourceSection.appendChild(sourceList);
    }
    body.appendChild(sourceSection);
    var questionSection = element("section", "dialog-section");
    questionSection.appendChild(element("h3", "", "問句／內容"));
    questionSection.appendChild(element("p", "", entryQuestion(item)));
    body.appendChild(questionSection);
    if (entryDescription(item) !== "尚無補充說明") {
      var descriptionSection = element("section", "dialog-section");
      descriptionSection.appendChild(element("h3", "", item.sql ? "SQL／說明" : "補充內容"));
      if (item.sql) descriptionSection.appendChild(element("pre", "", item.sql));
      else descriptionSection.appendChild(element("p", "", entryDescription(item)));
      body.appendChild(descriptionSection);
    }
    var reviewSection = buildReviewSection(item);
    if (reviewSection) body.appendChild(reviewSection);
    var rawSection = element("details", "dialog-section");
    rawSection.appendChild(element("summary", "", "查看完整語料欄位"));
    rawSection.appendChild(element("pre", "", JSON.stringify(item, null, 2)));
    body.appendChild(rawSection);
    var dialog = byId("corpusDialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    window.setTimeout(function () { byId("closeCorpusDialog").focus(); }, 0);
  }

  function closeCorpusDialog() {
    var dialog = byId("corpusDialog");
    if (typeof dialog.close === "function") dialog.close();
    else {
      dialog.removeAttribute("open");
      if (dialogReturnFocus) dialogReturnFocus.focus();
      dialogReturnFocus = null;
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!composing) submitQuestion(input.value.trim());
  });
  input.addEventListener("compositionstart", function () { composing = true; });
  input.addEventListener("compositionend", function () { composing = false; });
  input.addEventListener("keydown", function (event) {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitQuestion(input.value.trim());
      return;
    }
    if ((event.key === "ArrowUp" || event.key === "ArrowDown") && (input.value === "" || historyCursor != null) && queryHistory.length) {
      event.preventDefault();
      if (event.key === "ArrowUp") historyCursor = historyCursor == null ? 0 : Math.min(queryHistory.length - 1, historyCursor + 1);
      else historyCursor = historyCursor == null ? null : historyCursor - 1;
      input.value = historyCursor == null || historyCursor < 0 ? "" : queryHistory[historyCursor];
      if (historyCursor != null && historyCursor < 0) historyCursor = null;
      resizeInput();
    }
  });
  input.addEventListener("input", function () { resizeInput(); });

  document.addEventListener("click", function (event) {
    var nav = event.target.closest("[data-view]");
    if (nav) { showView(nav.getAttribute("data-view")); return; }
    var questionButton = event.target.closest("[data-question]");
    if (questionButton && !questionButton.disabled) { submitQuestion(questionButton.getAttribute("data-question")); return; }
    var corpusButton = event.target.closest("[data-corpus-index]");
    if (corpusButton) openCorpusDialog(Number(corpusButton.getAttribute("data-corpus-index")), corpusButton);
    var datasetButton = event.target.closest("[data-dataset-index]");
    if (datasetButton) openDatasetDialog(Number(datasetButton.getAttribute("data-dataset-index")), datasetButton);
    var changeButton = event.target.closest("[data-change-index]");
    if (changeButton) openDataChangeDialog(Number(changeButton.getAttribute("data-change-index")), changeButton);
    var versionButton = event.target.closest("[data-version-index]");
    if (versionButton) openVersionRollbackDialog(Number(versionButton.getAttribute("data-version-index")), versionButton);
    var sourceButton = event.target.closest("[data-source-dataset]");
    if (sourceButton) {
      var dataset = sourceButton.getAttribute("data-source-dataset");
      closeCorpusDialog();
      selectManagementTab("files", { focus: false });
      byId("datasetSearch").value = dataset;
      renderDatasetFiles();
      window.setTimeout(function () { byId("datasetSearch").focus(); }, 0);
    }
  });

  byId("navToggle").addEventListener("click", function () { setSidebarOpen(!byId("sidebar").classList.contains("open")); });
  byId("sidebarClose").addEventListener("click", function () { setSidebarOpen(false); });
  byId("sidebarBackdrop").addEventListener("click", function () { setSidebarOpen(false); });
  byId("modeShortcut").addEventListener("click", function () { showView("settings"); });
  byId("clearHistory").addEventListener("click", clearHistory);
  byId("refreshOverview").addEventListener("click", function () { loadStats(true); });
  loadCoverage();
  byId("refreshDataManagement").addEventListener("click", function () { refreshDataManagement(true); });
  byId("dataLoginForm").addEventListener("submit", loginAdmin);
  byId("dataLogout").addEventListener("click", logoutAdmin);
  byId("toggleAdminPassword").addEventListener("click", function () {
    var password = byId("adminPassword");
    var show = password.type === "password";
    password.type = show ? "text" : "password";
    this.textContent = show ? "隱藏" : "顯示";
    this.setAttribute("aria-label", (show ? "隱藏" : "顯示") + "管理密碼");
  });
  byId("datasetUploadForm").addEventListener("submit", stageDatasetUpload);
  byId("corpusSubmitForm").addEventListener("submit", submitCorpusEntry);
  byId("datasetFileInput").addEventListener("change", function () {
    var file = this.files && this.files[0];
    byId("datasetFileName").textContent = file ? file.name + " · " + humanFileSize(file.size) : "尚未選擇檔案（上限 64 MB）";
  });
  byId("datasetSearch").addEventListener("input", renderDatasetFiles);
  byId("datasetStateFilter").addEventListener("change", renderDatasetFiles);
  byId("dataChangeSearch").addEventListener("input", renderDataChanges);
  byId("dataChangeStateFilter").addEventListener("change", renderDataChanges);
  byId("auditSearch").addEventListener("input", renderAuditEvents);
  byId("auditResultFilter").addEventListener("change", renderAuditEvents);
  document.querySelectorAll("[data-management-tab]").forEach(function (tab) {
    tab.addEventListener("click", function () { selectManagementTab(this.getAttribute("data-management-tab"), { focus: false }); });
    tab.addEventListener("keydown", handleManagementTabKeys);
  });
  byId("corpusSearch").addEventListener("input", renderCorpusList);
  byId("corpusStateFilter").addEventListener("change", renderCorpusList);
  byId("runtimeForm").addEventListener("submit", saveRuntime);
  byId("clearRuntimeKey").addEventListener("click", clearRuntimeKey);
  byId("toggleApiKey").addEventListener("click", function () {
    var key = byId("apiKeyInput");
    var show = key.type === "password";
    key.type = show ? "text" : "password";
    this.textContent = show ? "隱藏" : "顯示";
    this.setAttribute("aria-label", (show ? "隱藏" : "顯示") + " API key");
  });
  byId("closeCorpusDialog").addEventListener("click", closeCorpusDialog);
  byId("closeDatasetDialog").addEventListener("click", closeDatasetDialog);
  byId("closeDataChangeDialog").addEventListener("click", closeDataChangeDialog);
  byId("corpusDialog").addEventListener("close", function () {
    if (dialogReturnFocus && typeof dialogReturnFocus.focus === "function") dialogReturnFocus.focus();
    dialogReturnFocus = null;
  });
  byId("corpusDialog").addEventListener("click", function (event) { if (event.target === this) closeCorpusDialog(); });
  byId("datasetDialog").addEventListener("close", function () {
    if (datasetDialogReturnFocus && typeof datasetDialogReturnFocus.focus === "function") datasetDialogReturnFocus.focus();
    datasetDialogReturnFocus = null;
  });
  byId("datasetDialog").addEventListener("click", function (event) { if (event.target === this) closeDatasetDialog(); });
  byId("dataChangeDialog").addEventListener("close", function () {
    if (dataChangeDialogReturnFocus && typeof dataChangeDialogReturnFocus.focus === "function") dataChangeDialogReturnFocus.focus();
    dataChangeDialogReturnFocus = null;
  });
  byId("dataChangeDialog").addEventListener("click", function (event) { if (event.target === this) closeDataChangeDialog(); });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (byId("dataChangeDialog").open) { closeDataChangeDialog(); return; }
    if (byId("datasetDialog").open) { closeDatasetDialog(); return; }
    if (byId("corpusDialog").open) { closeCorpusDialog(); return; }
    if (byId("sidebar").classList.contains("open")) setSidebarOpen(false);
  });
  window.addEventListener("resize", function () { setSidebarOpen(false, false); });

  loadHistory();
  showView("query", { focus: false });
  loadHealth();
  loadStats(false);
  loadExamples();
  lockDataManagement();
  loadAdminSession().then(function (authenticated) {
    if (authenticated) loadRuntime(false);
  });
}());
