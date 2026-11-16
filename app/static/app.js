"use strict";

const $ = selector => document.querySelector(selector);
const h = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[ch]));
const num = value => Number.isFinite(value) ? value.toFixed(2) : "—";
const periodNames = {day: "日线", "5": "5分钟", "15": "15分钟"};
const fieldNames = ["J", "K", "D", "RSV", "close"];
const opNames = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=", "!=": "≠", cross_above: "向上穿越", cross_below: "向下穿越"};
const tabInfo = {
  overview: ["行情概览", "把关注交给规则，把时间留给自己。"],
  stocks: ["自选股票", "为每一支关注的股票，安排专属的观察周期。"],
  rules: ["提醒规则", "用你的条件，定义值得关注的时刻。"],
  targets: ["Bark 设备", "让每一条信号，抵达合适的设备。"],
  history: ["提醒记录", "每一次触发与发送，都有迹可循。"],
  settings: ["监控设置", "检查频率、指标参数，由你掌握。"]
};
let state = null, csrf = "", activeTab = "overview", editorContext = null, historyPage = 1;
let toastTimer, loadingState = false;

async function api(path, method = "GET", payload) {
  const response = await fetch("/api/" + path, {
    method, credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    ...(payload === undefined ? {} : {body: JSON.stringify(payload)})
  });
  let data;
  try { data = await response.json(); } catch { throw new Error("服务暂时不可用，请检查服务器"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function toast(message) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500);
}

function showLogin() {
  desktop.reset();
  state = null;
  csrf = "";
  $("#login-screen").hidden = false;
  $("#app-screen").hidden = true;
  if ($("#editor").open) $("#editor").close();
}

async function loadState(silent = false) {
  if (loadingState) return;
  loadingState = true;
  try {
    const first = !state;
    state = await api("state");
    csrf = state.csrf;
    $("#login-screen").hidden = true;
    $("#app-screen").hidden = false;
    renderOverview();
    renderStocks();
    renderRules();
    renderTargets();
    if (first) fillSettings();
    await desktop.sync(state);
    if (activeTab === "history") await loadHistory();
    $("#global-notice").hidden = state.targets.some(t => t.enabled) || state.settings.browser_enabled;
    $("#global-notice").textContent = "尚未启用提醒通道。请添加 Bark 设备，或在“监控设置”中启用浏览器桌面通知。";
  } catch (error) {
    if (state && !silent) toast(error.message);
    else if (state) {
      $("#global-notice").hidden = false;
      $("#global-notice").textContent = "页面刷新失败：" + error.message + "。请检查网络连接。";
    }
  } finally { loadingState = false; }
}

function empty(title, description) { return `<div class="empty"><strong>${h(title)}</strong>${h(description)}</div>`; }
function enabledPill(enabled) { return `<span class="pill ${enabled ? "" : "warn"}">${enabled ? "已启用" : "已暂停"}</span>`; }
function periodText(periods) { return periods.map(p => periodNames[p]).join(" / "); }
function actionButton(table, id, action = "edit", label = "编辑", cls = "secondary") {
  return `<button type="button" class="${cls}" data-action="${action}" data-table="${table}" data-id="${id}">${label}</button>`;
}
function selectedNames(ids, entries, allLabel) {
  return ids.length ? ids.map(id => entries.find(x => x.id === id)?.name || "已删除 #" + id).join("、") : allLabel;
}

function renderChart(points) {
  if (!points?.length) return "";
  const values = points.flatMap(p => [p.K, p.D, p.J]);
  const low = Math.min(0, ...values) - 8, high = Math.max(100, ...values) + 8;
  const y = value => (84 - (value - low) / (high - low) * 76).toFixed(2);
  const lines = ["K", "D", "J"].map(field => `<polyline class="line-${field.toLowerCase()}" points="${points.map((p, i) => `${(i / Math.max(1, points.length - 1) * 300).toFixed(2)},${y(p[field])}`).join(" ")}"/>`).join("");
  return `<svg class="mini-chart" viewBox="0 0 300 92" role="img" aria-label="最近 ${points.length} 根 K 线的 KDJ 曲线"><title>最近 ${points.length} 根 K 线；金色 K，蓝色 D，紫色 J</title><line class="chart-grid" x1="0" x2="300" y1="${y(100)}" y2="${y(100)}"/><line class="chart-grid" x1="0" x2="300" y1="${y(0)}" y2="${y(0)}"/>${lines}</svg>`;
}

function renderOverview() {
  $("#stat-stocks").innerHTML = `${state.stocks.filter(s => s.enabled).length}<small>支</small>`;
  $("#stat-rules").innerHTML = `${state.rules.filter(r => r.enabled).length}<small>条</small>`;
  $("#stat-targets").innerHTML = `${state.targets.filter(t => t.enabled).length}<small>台</small>`;
  $("#stat-status").textContent = state.status.running ? "检查中" : state.settings.market_only && state.status.message.startsWith("非交易") ? "等待开市" : state.status.last_run ? "监控就绪" : "等待检查";
  $("#engine-message").textContent = state.status.message;
  $("#check-time").textContent = state.status.last_run ? "上次检查 " + formatDate(state.status.last_run) : "每 " + state.settings.poll_seconds + " 秒检查";
  const cards = [];
  for (const stock of state.stocks.filter(s => s.enabled)) {
    for (const period of stock.periods) {
      const snapshot = state.snapshots.find(s => s.stock_id === stock.id && s.period === period);
      const data = snapshot?.data;
      const badge = snapshot?.error ? "采集失败" : !data ? "待采集" : data.stale ? "历史数据" : state.settings.completed_only ? "已收盘" : "含盘中线";
      cards.push(`<article class="watch-card">
        <div class="watch-head"><div><h3>${h(stock.name)}</h3><span class="symbol">${h(stock.symbol.toUpperCase())}</span></div><span class="pill ${snapshot?.error ? "error" : data?.stale ? "warn" : ""}">${h(periodNames[period])} · ${h(badge)}</span></div>
        ${data ? `<div class="price-line"><strong>${num(data.close)}</strong><span>CNY / 最新收盘价</span></div>
          <div class="kdj-grid"><div><span><i class="dot k"></i>K</span><strong>${num(data.K)}</strong></div><div><span><i class="dot d"></i>D</span><strong>${num(data.D)}</strong></div><div><span><i class="dot j"></i>J</span><strong class="${data.J > 98 ? "high" : data.J < 2 ? "low" : ""}">${num(data.J)}</strong></div></div>
          ${renderChart(data.history)}<div class="signal-label">${data.stale ? "历史快照 · 不推送" : data.signals?.length ? "● " + h(data.signals.join(" / ")) : "○ 暂无满足的规则"}</div>
          <div class="watch-bottom"><span>${h(data.time)}</span><span>${h(data.source === "tencent" ? "腾讯" : "东方财富")} · ${data.kdj_n}/${data.kdj_m1}/${data.kdj_m2}</span></div>` : `<div class="no-data">等待获取 K 线与 KDJ 数据<br><p class="help">可点击“立即检查”获取行情</p></div>`}
        ${snapshot?.error ? `<div class="inline-error">${h(snapshot.error)}${data ? " · 上方为上次成功快照" : ""}</div>` : ""}
      </article>`);
    }
  }
  $("#watch-grid").innerHTML = cards.join("") || empty("还没有监控中的股票", "前往自选股票，添加代码并启用监控。");
}

function renderStocks() {
  $("#stocks-list").innerHTML = state.stocks.map(stock => `<article class="list-card ${stock.enabled ? "" : "disabled"}"><div><h3>${h(stock.name)} <span class="badge">${h(stock.symbol.toUpperCase())}</span></h3><p>${h(periodText(stock.periods))} · KDJ ${stock.kdj_n || state.settings.kdj_n} / ${stock.kdj_m1 || state.settings.kdj_m1} / ${stock.kdj_m2 || state.settings.kdj_m2} ${stock.kdj_n || stock.kdj_m1 || stock.kdj_m2 ? "（独立参数）" : "（跟随默认）"}</p></div><div class="actions">${enabledPill(stock.enabled)}${actionButton("stocks", stock.id)}${actionButton("stocks", stock.id, "delete", "删除", "danger")}</div></article>`).join("") || empty("添加第一支股票", "输入六位 A 股代码，例如 688605，即可保存并开始观察。");
}

function renderRules() {
  $("#rules-list").innerHTML = state.rules.map(rule => `<article class="rule-card ${rule.enabled ? "" : "disabled"}"><div class="rule-card-head"><h3>${h(rule.name)}</h3>${enabledPill(rule.enabled)}</div>
    <div class="rule-expression">${rule.condition.items.map((item, i) => `${i ? `<span class="logic-label">${rule.condition.logic === "all" ? "且" : "或"}</span>` : ""}<span class="condition-chip">${h(item.left)} ${h(opNames[item.op])} ${h(item.right)}</span>`).join("")}</div>
    <div class="rule-meta"><span>股票：${h(selectedNames(rule.stock_ids, state.stocks, "全部启用股票"))}</span><span>周期：${h(periodText(rule.periods))}</span><span>触发：${rule.mode === "on_enter" ? "进入条件时" : "每根满足条件的 K 线"} · 冷却 ${rule.cooldown_minutes ?? state.settings.cooldown_minutes} 分钟</span><span>设备：${h(selectedNames(rule.target_ids, state.targets, "全部启用设备"))}</span></div>
    <div class="rule-footer">${actionButton("rules", rule.id)}${actionButton("rules", rule.id, "delete", "删除", "danger")}</div></article>`).join("") || empty("你的第一条提醒规则", "设定指标、阈值和组合条件，让信号自动提醒你。");
}

function renderTargets() {
  $("#targets-list").innerHTML = state.targets.map(target => `<article class="list-card ${target.enabled ? "" : "disabled"}"><div><h3>${h(target.name)}</h3><span class="target-url muted">${h(target.server)}</span><p>设备 Key 已保存 · 分组 ${h(target.group_name || "无")} · ${h({active:"正常通知", passive:"静默通知", timeSensitive:"时效性通知"}[target.level])}</p></div><div class="actions">${enabledPill(target.enabled)}${actionButton("targets", target.id, "test", "测试推送", "ghost")}${actionButton("targets", target.id)}${actionButton("targets", target.id, "delete", "删除", "danger")}</div></article>`).join("") || empty("连接你的 iPhone", "添加 Bark 设备 Key，支持官方服务与自建服务。");
}

function formatDate(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false});
}

async function loadHistory() {
  const data = await api("history?page=" + historyPage);
  $("#history-list").innerHTML = data.items.map(item => `<article class="list-card history-card"><div class="history-top"><h3>${h(item.title)}</h3><span class="pill">${h(formatDate(item.created_at))}</span></div><span class="symbol">${h(item.symbol.toUpperCase())} / ${h(periodNames[item.period])} / K线 ${h(item.bar_time)}</span><pre>${h(item.body)}</pre>${item.browser_title !== null ? `<div class="delivery-row">◫ 已生成桌面通知内容（不代表系统已展示）</div><pre>${h(item.browser_title)}\n${h(item.browser_body)}</pre>` : ""}${item.deliveries.map(d => `<div class="delivery-row"><span class="${h(d.status)}">${{sent:"● 已发送", pending:"◷ 等待重试", failed:"× 发送失败"}[d.status]}</span> · ${h(d.target_name)} · 尝试 ${d.attempts}/3${d.error ? " · " + h(d.error) : ""}</div>`).join("")}</article>`).join("") || empty("还没有触发记录", "当规则满足时，触发内容与各设备的发送结果会显示在这里。");
  const pages = Math.max(1, Math.ceil(data.total / 25));
  $("#history-pagination").innerHTML = `<button class="secondary" type="button" data-page="${historyPage - 1}" ${historyPage <= 1 ? "disabled" : ""}>上一页</button><span>${historyPage} / ${pages} · ${data.total} 条</span><button class="secondary" type="button" data-page="${historyPage + 1}" ${historyPage >= pages ? "disabled" : ""}>下一页</button>`;
}

function fillSettings() {
  if (!state) return;
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(state.settings)) {
    const field = form.elements.namedItem(key);
    if (!field) continue;
    if (field.type === "checkbox") field.checked = value;
    else field.value = value;
  }
  desktop.updateTemplateFields();
}

function switchTab(tab) {
  if (!tabInfo[tab]) return;
  activeTab = tab;
  document.querySelectorAll(".tab-panel").forEach(el => { el.hidden = el.id !== "tab-" + tab; });
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.tab === tab));
  $("#page-title").innerHTML = h(tabInfo[tab][0]) + '<span class="title-dot">.</span>';
  $("#page-description").textContent = tabInfo[tab][1];
  $("#breadcrumb").textContent = "工作台 / " + tabInfo[tab][0];
  const table = {stocks:"股票", rules:"规则", targets:"设备"}[tab];
  $("#page-actions").innerHTML = tab === "overview" ? '<button class="primary" data-action="check" type="button">↻ 立即检查</button>' : table ? `<button class="primary" data-action="add" data-table="${tab}" type="button">＋ 添加${table}</button>` : "";
  if (tab === "settings") fillSettings();
  if (tab === "history") loadHistory().catch(error => toast(error.message));
}

function checkboxes(name, entries, selected) {
  return `<div class="checkbox-group">${entries.map(([value, label]) => `<label><input type="checkbox" name="${name}" value="${h(value)}" ${selected.includes(value) ? "checked" : ""}>${h(label)}</label>`).join("")}</div>`;
}
function periodPicker(selected = ["day", "5", "15"]) { return `<div><div class="field-label">观察周期</div>${checkboxes("periods", ["day", "5", "15"].map(p => [p, periodNames[p]]), selected)}</div>`; }
function referencePicker(name, entries, selected, label) {
  const options = entries.map(entry => [entry.id, entry.name + (entry.enabled ? "" : "（已暂停）")]);
  for (const id of selected) if (!entries.some(e => e.id === id)) options.push([id, "已删除 #" + id]);
  return `<div><div class="field-label">${h(label)} <span class="muted">（不选则使用全部启用项）</span></div><div class="scrollable">${options.length ? checkboxes(name, options, selected) : '<p class="help">尚无可选项，添加后自动适用。</p>'}</div></div>`;
}
function inputField(label, name, value, extra = "", type = "text") { return `<label>${label}<input type="${type}" name="${name}" value="${h(value)}" ${extra}></label>`; }
function selectField(label, name, options, value) { return `<label>${label}<select name="${name}">${options.map(([v, t]) => `<option value="${h(v)}" ${v === value ? "selected" : ""}>${h(t)}</option>`).join("")}</select></label>`; }

function conditionRow(item = {left:"J", op:">", right:98}) {
  return `<div class="condition-row"><select data-part="left" aria-label="左侧指标">${fieldNames.map(f => `<option ${f === item.left ? "selected" : ""}>${f}</option>`).join("")}</select><select data-part="op" aria-label="比较方式">${Object.entries(opNames).map(([value, label]) => `<option value="${h(value)}" ${value === item.op ? "selected" : ""}>${h(label)}</option>`).join("")}</select><input data-part="right" list="operand-values" value="${h(item.right)}" placeholder="数字或 D" aria-label="比较值" required><button type="button" class="icon-button" data-action="remove-condition" aria-label="删除条件">×</button></div>`;
}

function openEditor(table, id) {
  const obj = id ? state[table].find(x => x.id === id) : {};
  if (!obj) return;
  editorContext = {table, id};
  $("#editor-title").textContent = (id ? "编辑" : "添加") + {stocks:"股票", rules:"提醒规则", targets:"Bark 设备"}[table];
  $("#editor-error").textContent = "";
  let html = inputField("名称", "name", obj.name || "", 'required maxlength="80" placeholder="起一个容易辨认的名字"');
  if (table === "stocks") {
    html += inputField("股票代码", "symbol", obj.symbol || "", 'required placeholder="例如 688605 或 sh688605" maxlength="10"');
    html += periodPicker(obj.periods);
    html += '<div><h3 class="subheading">独立 KDJ 参数</h3><p class="help">留空跟随监控设置中的默认值</p></div><div class="form-grid four">';
    for (const [key, label, min, max] of [["kdj_n","N",2,120],["kdj_m1","M1",1,60],["kdj_m2","M2",1,60]]) html += inputField(label, key, obj[key] ?? "", `min="${min}" max="${max}" placeholder="${state.settings[key]}"`, "number");
    html += "</div>";
  } else if (table === "targets") {
    html += inputField("Bark 服务地址", "server", obj.server || "https://api.day.app", 'required placeholder="https://api.day.app"', "url");
    html += inputField("设备 Key / Token", "token", "", `autocomplete="new-password" ${id ? 'placeholder="已保存，留空则保持原 Key"' : 'required placeholder="粘贴 Bark 设备 Key"'}`, "password");
    html += '<p class="help">如果 Bark 显示 https://api.day.app/AbCdEf/推送内容，设备 Key 就是 AbCdEf。保存后不会在页面显示原文。</p><div class="form-grid">';
    html += inputField("通知分组", "group_name", obj.group_name ?? "股票提醒", 'maxlength="80"');
    html += inputField("铃声（可选）", "sound", obj.sound || "", 'placeholder="例如 bell，留空使用默认" maxlength="80"');
    html += "</div>";
    html += selectField("推送等级", "level", [["active","正常通知"],["passive","静默通知"],["timeSensitive","时效性通知（需 iOS 授权）"]], obj.level || "active");
  } else {
    html += referencePicker("stock_ids", state.stocks, obj.stock_ids || [], "适用股票");
    html += periodPicker(obj.periods);
    html += `<div><div class="condition-top"><span>以下条件</span><select name="logic"><option value="all">全部满足（AND）</option><option value="any" ${obj.condition?.logic === "any" ? "selected" : ""}>任一满足（OR）</option></select></div><div id="conditions" class="conditions">${(obj.condition?.items || [{left:"J",op:">",right:98},{left:"K",op:">",right:"D"}]).map(conditionRow).join("")}</div><datalist id="operand-values">${fieldNames.map(f => `<option value="${f}"></option>`).join("")}</datalist><button type="button" class="ghost add-condition" data-action="add-condition">＋ 增加条件</button><p class="help">右侧可输入数值或 J、K、D、RSV、close。穿越比较当前与前一根 K 线，支持最多 12 个条件。</p></div>`;
    html += '<div class="form-grid">' + selectField("触发方式", "mode", [["on_enter","进入条件时提醒"],["per_bar","每根满足条件的 K 线"]], obj.mode || "on_enter");
    html += inputField("冷却时间（分钟）", "cooldown_minutes", obj.cooldown_minutes ?? "", `min="0" max="10080" placeholder="默认 ${state.settings.cooldown_minutes} 分钟"`, "number") + "</div>";
    html += referencePicker("target_ids", state.targets, obj.target_ids || [], "推送设备");
    html += inputField("推送标题", "title", obj.title || "{name} · {rule}", 'required maxlength="200"');
    html += `<label>推送正文<textarea name="body" required maxlength="2000">${h(obj.body || "{name} ({symbol}) · {period}\nJ={J} K={K} D={D}\n价格：{close}\nK线时间：{bar_time}\n条件：{rule}")}</textarea></label>`;
    html += '<p class="help">可用变量：' + state.template_fields.map(f => `<code>{${h(f)}}</code>`).join(" ") + '<br>close 为收盘价，bar_time 为 K 线时间，time 为触发时间。数字自动保留两位小数。</p>';
  }
  html += `<label class="check-line"><input type="checkbox" name="enabled" ${obj.enabled !== false ? "checked" : ""}>启用${{stocks:"监控",rules:"规则",targets:"推送"}[table]}</label>`;
  $("#editor-fields").innerHTML = html;
  $("#editor").showModal();
}

function readCheckboxes(form, name, numeric = false) { return Array.from(form.querySelectorAll(`input[name="${name}"]:checked`), el => numeric ? Number(el.value) : el.value); }
function optionalNumber(value) { return value === "" ? null : Number(value); }

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    const result = await api("login", "POST", {password: $("#password").value});
    csrf = result.csrf;
    $("#password").value = "";
    await loadState();
  } catch (error) { $("#login-error").textContent = error.message; }
  finally { button.disabled = false; }
});

async function logout() {
  try { await api("logout", "POST", {}); showLogin(); } catch (error) { toast(error.message); }
}
$("#logout").addEventListener("click", logout);
$("#mobile-logout").addEventListener("click", logout);

$("#editor-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const {table, id} = editorContext;
  const data = Object.fromEntries(new FormData(form));
  data.enabled = form.elements.enabled.checked;
  if (table !== "targets") data.periods = readCheckboxes(form, "periods");
  if (table === "stocks") for (const key of ["kdj_n", "kdj_m1", "kdj_m2"]) data[key] = optionalNumber(data[key]);
  if (table === "rules") {
    data.stock_ids = readCheckboxes(form, "stock_ids", true);
    data.target_ids = readCheckboxes(form, "target_ids", true);
    data.cooldown_minutes = optionalNumber(data.cooldown_minutes);
    data.condition = {logic: data.logic, items: Array.from(form.querySelectorAll(".condition-row"), row => {
      const right = row.querySelector('[data-part="right"]').value.trim();
      return {left: row.querySelector('[data-part="left"]').value, op: row.querySelector('[data-part="op"]').value, right: fieldNames.includes(right) ? right : right === "" ? null : Number.isFinite(Number(right)) ? Number(right) : right};
    })};
  }
  $("#save-editor").disabled = true;
  $("#editor-error").textContent = "";
  try {
    await api(table + (id ? "/" + id : ""), id ? "PUT" : "POST", data);
    $("#editor").close();
    toast("配置已保存");
    await loadState();
  } catch (error) { $("#editor-error").textContent = error.message; }
  finally { $("#save-editor").disabled = false; }
});

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  for (const key of ["poll_seconds", "kdj_n", "kdj_m1", "kdj_m2", "cooldown_minutes"]) data[key] = Number(data[key]);
  for (const key of ["market_only", "completed_only", "browser_enabled"]) data[key] = form.elements.namedItem(key).checked;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try { await api("settings", "PUT", data); toast("监控设置已保存，下次检查生效"); await loadState(); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

$("#close-editor").addEventListener("click", () => $("#editor").close());
$("#cancel-editor").addEventListener("click", () => $("#editor").close());

document.addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  if (button.dataset.tab) { switchTab(button.dataset.tab); return; }
  if (button.dataset.page) { historyPage = Number(button.dataset.page); loadHistory().catch(error => toast(error.message)); return; }
  const {action, table} = button.dataset;
  const id = Number(button.dataset.id);
  if (action === "add") { openEditor(table); return; }
  if (action === "edit") { openEditor(table, id); return; }
  if (action === "add-condition") {
    if ($("#conditions").children.length >= 12) { toast("最多支持 12 个条件"); return; }
    $("#conditions").insertAdjacentHTML("beforeend", conditionRow()); return;
  }
  if (action === "remove-condition") {
    if ($("#conditions").children.length <= 1) { toast("请至少保留一个条件"); return; }
    button.closest(".condition-row").remove(); return;
  }
  if (!["delete", "test", "check"].includes(action)) return;
  if (action === "delete" && !confirm("确定删除这项配置？已有提醒记录会保留。")) return;
  button.disabled = true;
  try {
    if (action === "delete") { await api(`${table}/${id}`, "DELETE"); toast("已删除配置"); await loadState(); }
    if (action === "test") { await api(`targets/${id}/test`, "POST", {}); toast("Bark 已接收测试推送，请查看手机"); }
    if (action === "check") { await api("check", "POST", {}); toast("已安排检查，满足规则时将发送提醒"); setTimeout(() => loadState(), 2000); }
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false}); }, 1000);
setInterval(() => { if (state && !document.hidden) loadState(true); }, 10000);
const desktop = new DesktopNotifications({api, toast, openHistory: () => switchTab("history")});
switchTab("overview");
loadState(true);
