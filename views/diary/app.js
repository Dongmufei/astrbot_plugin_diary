/* 日记本 · 插件视图前端
 * 通过 AstrBot 注入的 bridge SDK（window.AstrBotPluginView）与后端通信，
 * 端点写相对路径，插件名由 dashboard 自动转发。
 */
(function () {
  "use strict";

  var bridge = null;
  var ctx = null;
  var state = {
    list: [],
    dates: [],
    total: 0,
    today: "",
    lastDate: "",
    nextDate: "",
    current: null, // 当前编辑中的对象
    dirty: false,
    settings: null,
    searchTimer: null,
    saving: false,
  };

  var el = {};
  var IDS = [
    "brandTitle", "brandSub", "btnSettings", "btnNew", "btnExport", "search", "list",
    "sideFoot", "main", "empty", "editor", "dateInput", "btnPrevDay", "btnNextDay",
    "btnGenerate", "btnDelete", "btnSave", "pillState", "pillLogs", "pillChars",
    "savedTip", "titleInput", "contentInput", "preview", "logPanel", "logBody",
    "settingsMask", "settings", "btnCloseSettings", "settingsBody", "btnSaveSettings",
    "settingsTip", "toasts",
  ];

  // ------------------------------------------------------------ 工具

  function $(id) { return document.getElementById(id); }

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function toast(msg, isErr) {
    var t = h("div", { class: "toast" + (isErr ? " err" : ""), text: String(msg) });
    el.toasts.appendChild(t);
    setTimeout(function () {
      t.style.opacity = "0";
      t.style.transition = "opacity .25s";
      setTimeout(function () { t.remove(); }, 260);
    }, isErr ? 4200 : 2200);
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function toDateStr(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }

  function parseDate(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  function shiftDate(s, delta) {
    var d = parseDate(s) || new Date();
    d.setDate(d.getDate() + delta);
    return toDateStr(d);
  }

  function isFuture(s) {
    var d = parseDate(s);
    if (!d) return false;
    var t = parseDate(state.today);
    return t ? d > t : false;
  }

  function fmtDate(s) {
    var d = parseDate(s);
    if (!d) return s || "";
    var wk = "日一二三四五六"[d.getDay()];
    return d.getFullYear() + " 年 " + (d.getMonth() + 1) + " 月 " + d.getDate() + " 日 · 星期" + wk;
  }

  function excerpt(s, n) {
    s = String(s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  function bytes(s) { return String(s || "").length; }

  // ------------------------------------------------------------ 通信

  function encodePath(p) {
    return String(p).split("/").map(encodeURIComponent).join("/");
  }

  async function apiGet(path, params) {
    if (!bridge) throw new Error("页面还没接上 AstrBot（bridge 未就绪）");
    return await bridge.apiGet(path, params || undefined);
  }

  async function apiSend(method, path, body) {
    if (!bridge) throw new Error("页面还没接上 AstrBot（bridge 未就绪）");
    var fn = bridge[method];
    if (typeof fn !== "function") {
      throw new Error("当前 AstrBot 版本不支持 " + method + " 请求，请升级 AstrBot");
    }
    return await fn.call(bridge, path, body);
  }

  function errText(e) {
    if (!e) return "未知错误";
    if (typeof e === "string") return e;
    return e.message || e.detail || e.error || (e.msg ? e.msg : JSON.stringify(e));
  }

  function pick(payload) {
    if (!payload) return null;
    if (Array.isArray(payload)) return null;
    if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) return payload.data;
    return payload;
  }

  // ------------------------------------------------------------ 列表

  async function loadList(keyword) {
    var params = { size: 200 };
    if (keyword) params.q = keyword;
    try {
      var raw = await apiGet("diaries", params);
      var data = pick(raw) || {};
      state.list = data.items || [];
      state.dates = data.dates || [];
      state.total = data.total || state.list.length;
      state.today = data.today || state.today;
      state.lastDate = data.last_date || "";
      state.nextDate = data.next_date || state.today;
      renderList(keyword);
      renderFoot();
    } catch (e) {
      el.list.innerHTML = "";
      el.list.appendChild(h("li", { class: "list-tip", text: "读取日记列表失败：" + errText(e) }));
      toast("读取列表失败：" + errText(e), true);
    }
  }

  function renderFoot() {
    if (!state.total) {
      el.sideFoot.textContent = "本子还是空的。点「新的一天」写第一篇吧。";
      return;
    }
    el.sideFoot.textContent = "共 " + state.total + " 篇 · 今天 " + (state.dates.indexOf(state.today) >= 0 ? "已写" : "还没写");
  }

  function renderList(keyword) {
    el.list.innerHTML = "";
    if (!state.list.length) {
      el.list.appendChild(
        h("li", { class: "list-tip", text: keyword ? "没有找到包含「" + keyword + "」的日记" : "本子还是空的" })
      );
      return;
    }
    state.list.forEach(function (it) {
      var active = state.current && state.current.date === it.date;
      var li = h("li", { class: active ? "active" : "", onclick: function () { openDate(it.date); } }, [
        it.date === state.today ? h("span", { class: "badge-today", text: "今天" }) : null,
        h("div", { class: "li-date", text: it.date + " · " + it.weekday }),
        h("div", { class: "li-title", text: it.title || "（无题）" }),
        h("div", { class: "li-excerpt", text: excerpt(it.content, 46) }),
      ]);
      el.list.appendChild(li);
    });
  }

  // ------------------------------------------------------------ 编辑

  async function openDate(date, opts) {
    if (!date) return;
    opts = opts || {};
    if (state.dirty && !opts.force) {
      if (!window.confirm("这一天还有没保存的修改，要放弃吗？")) return;
    }
    state.dirty = false;
    el.empty.classList.add("hidden");
    el.editor.classList.remove("hidden");
    el.dateInput.value = date;
    el.titleInput.value = "";
    el.contentInput.value = "";
    el.savedTip.textContent = "";
    el.pillState.textContent = "加载中…";
    el.pillState.className = "pill muted";
    el.pillLogs.textContent = "";
    el.pillChars.textContent = "";
    el.logBody.innerHTML = "";
    setBusy(false);

    try {
      var raw = await apiGet("diaries/" + encodePath(date));
      var data = pick(raw);
      if (!data) throw new Error("返回格式异常");
      state.current = {
        date: date,
        title: (data.diary && data.diary.title) || "",
        content: (data.diary && data.diary.content) || "",
        hasDiary: !!data.has_diary,
      };
      el.titleInput.value = state.current.title;
      el.contentInput.value = state.current.content;
      renderMeta(data);
      renderLogs(data.messages || []);
      renderPreview();
      renderList(el.search.value.trim());
    } catch (e) {
      el.pillState.textContent = "加载失败";
      el.pillState.className = "pill warn";
      toast("打开 " + date + " 失败：" + errText(e), true);
    }
  }

  function renderMeta(data) {
    var has = !!(data && data.has_diary);
    el.pillState.textContent = has ? "已写好" : "这一天还没有日记";
    el.pillState.className = has ? "pill" : "pill muted";
    var lg = data.log || {};
    if (lg.count) {
      el.pillLogs.textContent = "当天聊了 " + lg.count + " 条（可当素材）";
      el.pillLogs.className = "pill muted";
    } else if (lg.exists) {
      el.pillLogs.textContent = "当天只有她自己的话";
      el.pillLogs.className = "pill muted";
    } else {
      el.pillLogs.textContent = "当天没有聊天记录";
      el.pillLogs.className = "pill muted";
    }
    updateChars();
  }

  function updateChars() {
    el.pillChars.textContent = bytes(el.contentInput.value) + " 字";
  }

  function renderLogs(messages) {
    el.logBody.innerHTML = "";
    el.logPanel.open = false;
    if (!messages.length) {
      el.logBody.appendChild(h("div", { class: "log-empty", text: "这天没有你发的消息（她只能凭设定和心情写）。" }));
      el.logPanel.querySelector("summary").textContent = "那天的聊天记录：没有";
      return;
    }
    el.logPanel.querySelector("summary").textContent = "那天的聊天记录（" + messages.length + " 条，写日记时会用到）";
    messages.forEach(function (m) {
      el.logBody.appendChild(
        h("div", { class: "log-line" }, [
          h("span", { class: "t", text: m.t || "" }),
          h("span", { class: "n", text: (m.n || "有人") + "：" }),
          document.createTextNode(m.m || ""),
        ])
      );
    });
  }

  function renderPreview() {
    var on = el.editor.getAttribute("data-preview") === "1";
    if (on) {
      el.preview.textContent = el.contentInput.value;
      el.preview.classList.remove("hidden");
      el.contentInput.classList.add("hidden");
    } else {
      el.preview.classList.add("hidden");
      el.contentInput.classList.remove("hidden");
    }
  }

  function markDirty() {
    state.dirty = true;
    el.savedTip.textContent = "有未保存的修改";
  }

  function setBusy(busy, label) {
    state.saving = busy;
    [el.btnSave, el.btnGenerate, el.btnDelete, el.btnNew].forEach(function (b) {
      if (b) b.disabled = busy;
    });
    if (busy && label) {
      el.savedTip.innerHTML = '<span class="spin">◌</span> ' + label;
    }
  }

  async function save() {
    if (!state.current) return;
    var date = el.dateInput.value;
    var title = el.titleInput.value.trim();
    var content = el.contentInput.value;
    if (!content.trim() && !title.trim()) {
      toast("内容和标题都是空的，没东西可存", true);
      return;
    }
    setBusy(true, "保存中…");
    try {
      await apiSend("apiPut", "diaries/" + encodePath(date), { title: title, content: content });
      state.dirty = false;
      state.current = { date: date, title: title, content: content, hasDiary: true };
      el.savedTip.textContent = "已保存";
      el.pillState.textContent = "已写好";
      el.pillState.className = "pill";
      toast("已保存 " + date);
      await loadList(el.search.value.trim());
    } catch (e) {
      el.savedTip.textContent = "";
      toast("保存失败：" + errText(e), true);
    } finally {
      setBusy(false);
    }
  }

  async function generate() {
    var date = el.dateInput.value;
    if (!date) return;
    var exists = state.current && state.current.hasDiary;
    if (exists && !window.confirm("这一天已经有日记了，要让她重写吗？原来的内容会被覆盖。")) return;
    setBusy(true, "她正在写…");
    try {
      var raw = await apiSend("apiPost", "diaries/" + encodePath(date) + "/generate", { force: true });
      var data = pick(raw) || {};
      var d = data.diary;
      if (!d) throw new Error("没有返回日记内容");
      el.titleInput.value = d.title || "";
      el.contentInput.value = d.content || "";
      state.current = { date: date, title: d.title || "", content: d.content || "", hasDiary: true };
      state.dirty = false;
      el.savedTip.textContent = "她写好了";
      el.pillState.textContent = "已写好";
      el.pillState.className = "pill";
      updateChars();
      renderPreview();
      toast("她写好了 " + date);
      await loadList(el.search.value.trim());
    } catch (e) {
      el.savedTip.textContent = "";
      toast("让她写失败：" + errText(e), true);
    } finally {
      setBusy(false);
    }
  }

  async function removeDay() {
    var date = el.dateInput.value;
    if (!date) return;
    if (!state.current || !state.current.hasDiary) {
      toast("这一天本来就没有日记");
      return;
    }
    if (!window.confirm("删除 " + date + " 的日记？删了就找不回来了。")) return;
    setBusy(true, "删除中…");
    try {
      await apiSend("apiDelete", "diaries/" + encodePath(date));
      el.titleInput.value = "";
      el.contentInput.value = "";
      state.current = { date: date, title: "", content: "", hasDiary: false };
      state.dirty = false;
      el.pillState.textContent = "这一天还没有日记";
      el.pillState.className = "pill muted";
      el.savedTip.textContent = "";
      updateChars();
      renderPreview();
      toast("已删除 " + date);
      await loadList(el.search.value.trim());
    } catch (e) {
      toast("删除失败：" + errText(e), true);
    } finally {
      setBusy(false);
    }
  }

  function newDay() {
    var d = state.nextDate || state.today || toDateStr(new Date());
    openDate(d, { force: true });
  }

  function goDelta(delta) {
    var cur = el.dateInput.value || state.today;
    openDate(shiftDate(cur, delta));
  }

  async function exportAll() {
    if (!bridge || typeof bridge.download !== "function") {
      // 退化成页面内预览
      try {
        var raw = await apiGet("diaries", { size: 200 });
        var data = pick(raw) || {};
        var items = data.items || [];
        var lines = ["# 她的日记本", ""];
        items.forEach(function (d) {
          lines.push("## " + d.date + " · " + (d.title || ""));
          lines.push("");
          lines.push(d.content || "");
          lines.push("");
          lines.push("---");
          lines.push("");
        });
        var blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "她的日记本.md";
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
      } catch (e) {
        toast("导出失败：" + errText(e), true);
      }
      return;
    }
    try {
      await bridge.download("export", {}, "她的日记本.md");
    } catch (e) {
      toast("导出失败：" + errText(e), true);
    }
  }

  // ------------------------------------------------------------ 设置

  async function openSettings() {
    el.settingsMask.classList.remove("hidden");
    el.settings.classList.remove("hidden");
    el.settingsBody.innerHTML = '<div class="log-empty">加载设置中…</div>';
    el.settingsTip.textContent = "";
    try {
      var raw = await apiGet("settings");
      var data = pick(raw) || {};
      state.settings = data;
      renderSettings(data);
    } catch (e) {
      el.settingsBody.innerHTML = "";
      el.settingsBody.appendChild(h("div", { class: "log-empty", text: "读取设置失败：" + errText(e) }));
    }
  }

  function closeSettings() {
    el.settingsMask.classList.add("hidden");
    el.settings.classList.add("hidden");
  }

  function renderSettings(data) {
    var body = el.settingsBody;
    body.innerHTML = "";
    var values = data.values || {};
    var fields = data.fields || [];
    var groups = data.groups || [];
    var providers = data.providers || [];
    var personas = data.personas || [];

    groups.forEach(function (g) {
      var items = fields.filter(function (f) { return f.group === g.key; });
      if (!items.length) return;
      body.appendChild(h("div", { class: "group-title", text: g.name }));
      if (g.desc) body.appendChild(h("div", { class: "group-desc", text: g.desc }));
      items.forEach(function (f) {
        body.appendChild(renderField(f, values[f.key], { providers: providers, personas: personas, lastUmo: data.last_umo }));
      });
    });

    if (!groups.length) {
      fields.forEach(function (f) {
        body.appendChild(renderField(f, values[f.key], { providers: providers, personas: personas }));
      });
    }
  }

  function fieldWrap(f, control, extraHint) {
    var hint = f.hint || f.desc || "";
    return h("div", { class: "field" }, [
      h("div", { class: "field-head" }, [
        h("span", { class: "field-label", text: f.label || f.key }),
        h("span", { class: "field-key", text: f.key }),
      ]),
      control,
      hint ? h("div", { class: "field-hint", text: hint }) : null,
      extraHint ? h("div", { class: "field-hint", text: extraHint }) : null,
    ]);
  }

  function renderField(f, value, extra) {
    var id = "cfg_" + f.key;
    var control;

    if (f.type === "bool") {
      var cb = h("input", { type: "checkbox", id: id });
      cb.checked = !!value;
      control = h("div", { class: "switch-row" }, [
        h("label", { class: "switch" }, [cb, h("span")]),
        h("span", { class: "field-hint", text: cb.checked ? "已开启" : "已关闭", id: id + "_lbl" }),
      ]);
      cb.addEventListener("change", function () {
        var lbl = document.getElementById(id + "_lbl");
        if (lbl) lbl.textContent = cb.checked ? "已开启" : "已关闭";
      });
    } else if (f.type === "text") {
      control = h("textarea", { id: id });
      control.value = value || "";
    } else if (f.type === "int") {
      var num = h("input", { type: "number", id: id });
      num.value = value === undefined || value === null ? f.default : value;
      if (f.slider) {
        num.min = f.slider.min;
        num.max = f.slider.max;
        num.step = f.slider.step;
        var range = h("input", { type: "range", class: "range", style: "width:100%;margin-top:6px" });
        range.min = f.slider.min;
        range.max = f.slider.max;
        range.step = f.slider.step;
        range.value = num.value;
        range.addEventListener("input", function () { num.value = range.value; });
        num.addEventListener("input", function () { range.value = num.value; });
        control = h("div", {}, [num, range]);
      } else {
        control = num;
      }
    } else if (f.type === "list") {
      var isUmo = f.special === "umo";
      var ta = h("textarea", { id: id, spellcheck: "false" });
      ta.value = Array.isArray(value) ? value.join("\n") : value || "";
      ta.placeholder = isUmo ? "一行一个，例如：\naiocqhttp:FriendMessage:123456789" : "一行一个";
      var extraHint = "";
      if (isUmo && extra.lastUmo) {
        extraHint = "最近一次聊天会话：" + extra.lastUmo + "（填这个基本就能用）";
      } else if (isUmo) {
        extraHint = "还没检测到会话。先在 QQ 里让她回一句话再回来看，这里就会提示该填什么。";
      }
      return fieldWrap(f, ta, extraHint);
    } else if (f.special === "select_provider") {
      var sp = h("select", { id: id });
      sp.appendChild(h("option", { value: "", text: "跟随当前会话" }));
      (extra.providers || []).forEach(function (p) {
        var o = h("option", { value: p.id, text: (p.name || p.id) + "（" + p.id + "）" });
        if (p.id === value) o.selected = true;
        sp.appendChild(o);
      });
      if (value && !(extra.providers || []).some(function (p) { return p.id === value; })) {
        var keep = h("option", { value: value, text: value });
        keep.selected = true;
        sp.appendChild(keep);
      }
      control = sp;
    } else if (f.special === "select_persona") {
      var ps = h("select", { id: id });
      ps.appendChild(h("option", { value: "", text: "不使用（用下面的「她的设定」）" }));
      (extra.personas || []).forEach(function (p) {
        var o = h("option", { value: p.id, text: p.name || p.id });
        if (p.id === value) o.selected = true;
        ps.appendChild(o);
      });
      if (value && !(extra.personas || []).some(function (p) { return p.id === value; })) {
        var keep2 = h("option", { value: value, text: value });
        keep2.selected = true;
        ps.appendChild(keep2);
      }
      control = ps;
    } else {
      var txt = h("input", { type: "text", id: id });
      txt.value = value === undefined || value === null ? f.default || "" : value;
      control = txt;
    }
    return fieldWrap(f, control);
  }

  function collectSettings() {
    var out = {};
    var fields = (state.settings && state.settings.fields) || [];
    fields.forEach(function (f) {
      var node = document.getElementById("cfg_" + f.key);
      if (!node) return;
      if (f.type === "bool") out[f.key] = !!node.checked;
      else if (f.type === "int") out[f.key] = node.value === "" ? 0 : Number(node.value);
      else if (f.type === "list") out[f.key] = node.value.split(/\n+/).map(function (s) { return s.trim(); }).filter(Boolean);
      else out[f.key] = node.value;
    });
    return out;
  }

  async function saveSettings() {
    var values = collectSettings();
    el.btnSaveSettings.disabled = true;
    el.settingsTip.textContent = "保存中…";
    try {
      var raw = await apiSend("apiPost", "settings", { values: values });
      var saved = pick(raw) || {};
      if (saved.settings) {
        state.settings = saved.settings;
        renderSettings(saved.settings);
      }
      el.settingsTip.textContent = "已保存，立即生效";
      toast("设置已保存");
      await refreshStats();
      await loadList(el.search.value.trim());
    } catch (e) {
      el.settingsTip.textContent = "";
      toast("保存设置失败：" + errText(e), true);
    } finally {
      el.btnSaveSettings.disabled = false;
    }
  }

  async function refreshStats() {
    try {
      var raw = await apiGet("stats");
      var d = pick(raw) || {};
      state.today = d.today || state.today;
      var name = "她的日记本";
      try {
        var bt = document.getElementById("brandTitle");
        if (bt) name = bt.textContent || name;
      } catch (_) {}
      el.brandSub.textContent =
        (d.count || 0) + " 篇 · " + (d.enabled ? "每天 " + (d.diary_time || "--:--") + " 自动写" : "自动写日记已关闭");
    } catch (e) {
      /* 忽略：stats 只是装饰 */
    }
  }

  // ------------------------------------------------------------ 初始化

  function bind() {
    el.btnNew.addEventListener("click", newDay);
    el.btnExport.addEventListener("click", exportAll);
    el.btnSave.addEventListener("click", save);
    el.btnGenerate.addEventListener("click", generate);
    el.btnDelete.addEventListener("click", removeDay);
    el.btnPrevDay.addEventListener("click", function () { goDelta(-1); });
    el.btnNextDay.addEventListener("click", function () { goDelta(1); });

    el.dateInput.addEventListener("change", function () { openDate(el.dateInput.value); });
    el.titleInput.addEventListener("input", markDirty);
    el.contentInput.addEventListener("input", function () { markDirty(); updateChars(); renderPreview(); });
    el.contentInput.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        save();
      }
    });

    el.search.addEventListener("input", function () {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(function () { loadList(el.search.value.trim()); }, 260);
    });

    el.btnSettings.addEventListener("click", openSettings);
    el.btnCloseSettings.addEventListener("click", closeSettings);
    el.settingsMask.addEventListener("click", closeSettings);
    el.btnSaveSettings.addEventListener("click", saveSettings);

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !el.settings.classList.contains("hidden")) closeSettings();
    });

    window.addEventListener("beforeunload", function (e) {
      if (state.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }

  async function boot() {
    IDS.forEach(function (id) { el[id] = $(id); });
    bind();

    try {
      bridge = window.AstrBotPluginView || null;
      if (bridge && typeof bridge.ready === "function") {
        ctx = await bridge.ready();
      } else if (bridge && typeof bridge.getContext === "function") {
        ctx = bridge.getContext();
      }
    } catch (e) {
      ctx = null;
    }

    var pageTitle = (ctx && ctx.pageTitle) || "日记本";
    var display = (ctx && ctx.displayName) || "";
    document.title = pageTitle + (display ? " · " + display : "");
    el.brandTitle.textContent = pageTitle;
    el.brandSub.textContent = display || "加载中…";

    if (!bridge) {
      toast("没检测到 AstrBot bridge：请从 AstrBot 后台的插件页面打开这个视图", true);
    }

    await refreshStats();
    await loadList();

    var start = state.nextDate || state.today;
    if (state.dates.indexOf(state.today) >= 0) start = state.today;
    if (state.dates.length && state.dates[0] > start) start = state.dates[0];
    if (start) openDate(start, { force: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();