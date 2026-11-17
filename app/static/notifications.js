"use strict";

class DesktopNotifications {
  constructor({api, toast, openHistory}) {
    this.api = api;
    this.toast = toast;
    this.openHistory = openHistory;
    this.config = null;
    this.scope = null;
    this.cursor = null;
    this.busy = false;
    this.generation = 0;
    this.active = new Set();
    this.modalQueue = [];
    this.modalOpen = false;
    this.audio = null;
    this.error = "";
    document.querySelector("#browser-enable").addEventListener("click", () => this.enable());
    document.querySelector("#browser-disable").addEventListener("click", () => this.disable());
    document.querySelector("#browser-test").addEventListener("click", () => this.test());
    document.querySelector("#browser-demo").addEventListener("click", () => this.demo());
    document.querySelector("#browser-alert-close").addEventListener("click", () => this.closeModal());
    document.querySelector("#browser-alert-modal").addEventListener("click", event => {
      if (event.target.id === "browser-alert-modal") this.closeModal();
    });
    document.querySelector("#browser-preset").addEventListener("click", () => {
      const form = document.querySelector("#settings-form");
      form.elements.browser_template_mode.value = "custom";
      form.elements.browser_title.value = "{name} · {rule}";
      form.elements.browser_body.value = "{symbol} · {period}\nJ={J} K={K} D={D}\n价格：{close}\nK线：{bar_time}";
      this.updateTemplateFields();
    });
    document.querySelector('[name="browser_template_mode"]').addEventListener("change", () => this.updateTemplateFields());
    window.addEventListener("storage", event => {
      if (event.key === this.key("enabled")) { this.cursor = null; this.generation++; this.poll(); this.render(); }
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { this.render(); this.poll(); } });
    window.addEventListener("focus", () => { this.render(); this.poll(); });
    // Deliberately poll even when hidden: OS notifications are useful in the background.
    setInterval(() => this.poll(), 5000);
  }

  key(part) { return "stockbark:desktop:" + this.scope + ":" + part; }
  read(part) { try { return localStorage.getItem(this.key(part)); } catch { return null; } }
  write(part, value) {
    try { localStorage.setItem(this.key(part), value); }
    catch { throw new Error("浏览器禁止保存通知偏好，请允许此网站使用本地存储"); }
  }
  supported() { return window.isSecureContext && "Notification" in window; }
  optedIn() { return Boolean(this.scope) && this.read("enabled") === "true"; }
  // Page alerts work without OS permission; permission only adds a Windows notification.
  ready() { return this.config?.browser_enabled && this.optedIn(); }

  sync(state) {
    if (this.scope !== state.notification_scope || this.config?.browser_enabled !== state.settings.browser_enabled) {
      this.cursor = null;
      this.generation++;
    }
    this.scope = state.notification_scope;
    this.config = state.settings;
    this.render();
    return this.poll();
  }

  reset() {
    this.generation++;
    this.config = null;
    this.cursor = null;
    this.error = "";
    for (const notification of this.active) notification.close();
    this.active.clear();
    this.modalQueue = [];
    this.closeModal();
  }

  updateTemplateFields() {
    const form = document.querySelector("#settings-form");
    document.querySelector("#browser-custom-fields").hidden = form.elements.browser_template_mode.value !== "custom";
  }

  render() {
    const status = document.querySelector("#browser-status");
    const enable = document.querySelector("#browser-enable");
    const test = document.querySelector("#browser-test");
    enable.disabled = test.disabled = false;
    document.querySelector("#browser-disable").disabled = !this.optedIn();
    if (!window.isSecureContext) status.textContent = "当前地址不支持 Windows 系统通知；页面弹窗和提示音仍可用。需要系统通知请改用 HTTPS，或通过 SSH 隧道访问 http://localhost:8089。";
    else if (!("Notification" in window)) status.textContent = "当前浏览器不支持桌面通知，请使用桌面版 Edge / Chrome / Firefox。";
    else if (Notification.permission === "denied") status.textContent = "Edge 系统通知权限已被拒绝；页面弹窗和提示音仍可用。请在浏览器地址栏的网站权限中改为允许，再重新测试系统通知。";
    else if (this.error) status.textContent = this.error;
    else if (!this.optedIn()) status.textContent = "当前浏览器未启用。点击下方按钮授权通知；此偏好仅保存到当前浏览器。";
    else if (Notification.permission !== "granted") status.textContent = "当前浏览器已启用页面弹窗；Edge 系统通知尚未授权，授权后会同时显示 Windows 通知。";
    else if (!this.config?.browser_enabled) status.textContent = "当前浏览器已授权；请勾选“启用浏览器提醒通道”并保存设置。";
    else status.textContent = "当前浏览器已启用：页面保持打开并登录即可接收新信号，后台标签页也会检查。";
  }

  async permission() {
    if (!this.supported()) throw new Error("系统通知需要支持此功能的浏览器，并通过 HTTPS 或 localhost 访问");
    // Called directly from a click, before any network request, for user activation.
    const result = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (result !== "granted") throw new Error(result === "denied" ? "通知权限已被拒绝，请在网站权限中改为允许" : "尚未授权通知，可以稍后再次启用");
  }

  async enable() {
    let permissionError = "";
    try { await this.permission(); } catch (error) { permissionError = error.message; }
    try {
      this.write("enabled", "true");
      this.error = permissionError;
      this.cursor = null;
      this.generation++;
      await this.poll();
      this.toast(permissionError ? "页面弹窗已启用；" + permissionError : "当前浏览器已启用系统通知和页面弹窗");
    } catch (error) { this.error = error.message; this.toast(error.message); }
    this.render();
  }

  disable() {
    try {
      this.write("enabled", "false");
      this.cursor = null;
      this.generation++;
      this.error = "";
      for (const notification of this.active) notification.close();
      this.active.clear();
      this.toast("已停止当前浏览器的提醒，Bark 和其他浏览器不受影响");
    } catch (error) { this.error = error.message; }
    this.render();
  }

  show(title, body, tag) {
    try {
      if (!("Notification" in window) || Notification.permission !== "granted") throw new Error("system notification permission unavailable");
      const notification = new Notification(title, {body, tag, lang: "zh-CN"});
      this.active.add(notification);
      notification.onclose = () => this.active.delete(notification);
      notification.onerror = () => { this.error = "浏览器未能显示系统通知，请检查网站通知权限和操作系统通知设置"; this.render(); };
      notification.onclick = () => {
        window.focus();
        this.openHistory();
        notification.close();
      };
    } catch (_) {
      this.error = "系统通知未显示，页面弹窗仍会保留；请检查浏览器和 Windows 通知权限";
      this.render();
    }
    this.enqueueModal(title, body);
  }

  ensureAudio() {
    if (!this.audio) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return null;
      this.audio = new AudioContext();
    }
    if (this.audio.state === "suspended") this.audio.resume();
    return this.audio;
  }

  beep() {
    try {
      const context = this.ensureAudio();
      if (!context) return;
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = 720;
      gain.gain.setValueAtTime(0.0001, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.075, context.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.34);
      oscillator.connect(gain); gain.connect(context.destination);
      oscillator.start(); oscillator.stop(context.currentTime + 0.35);
    } catch (_) {}
  }

  enqueueModal(title, body) {
    this.modalQueue.push({title, body});
    if (!this.modalOpen) this.nextModal();
  }

  nextModal() {
    const item = this.modalQueue.shift();
    if (!item) { this.closeModal(); return; }
    this.modalOpen = true;
    document.querySelector("#browser-alert-title").textContent = item.title;
    document.querySelector("#browser-alert-body").textContent = item.body;
    document.querySelector("#browser-alert-modal").hidden = false;
    this.beep();
  }

  closeModal() {
    this.modalOpen = false;
    document.querySelector("#browser-alert-modal").hidden = true;
    if (this.modalQueue.length) this.nextModal();
  }

  async test() {
    const generation = this.generation;
    let permissionError = "";
    try { await this.permission(); } catch (error) { permissionError = error.message; }
    try {
      if (generation !== this.generation || !this.config) return;
      const form = document.querySelector("#settings-form");
      const data = await this.api("browser-preview", "POST", {
        browser_template_mode: form.elements.browser_template_mode.value,
        browser_title: form.elements.browser_title.value,
        browser_body: form.elements.browser_body.value
      });
      // Logging out during a request must not display content from that session.
      if (generation !== this.generation || !this.config) return;
      this.error = permissionError;
      this.show(data.title, data.body, "stockbark-template-test");
      this.toast(permissionError ? "已显示页面测试弹窗；" + permissionError : "已请求显示 Windows 系统通知");
    } catch (error) { this.error = error.message; this.toast(error.message); }
    this.render();
  }

  async demo() {
    try {
      const form = document.querySelector("#settings-form");
      const data = await this.api("browser-preview", "POST", {
        browser_template_mode: form.elements.browser_template_mode.value,
        browser_title: form.elements.browser_title.value,
        browser_body: form.elements.browser_body.value
      });
      this.enqueueModal("[演示] " + data.title, data.body + "\n\n仅为演示，不代表真实行情。");
      this.toast("已显示页面弹窗并播放提示音");
    } catch (error) { this.toast(error.message); }
  }

  async poll() {
    if (this.busy || !this.ready()) return;
    const generation = this.generation;
    this.busy = true;
    try {
      const baseline = this.cursor === null;
      const data = await this.api("browser-events" + (baseline ? "" : "?after=" + this.cursor));
      if (generation !== this.generation || !this.ready()) return;
      if (!data.enabled) { this.cursor = null; this.error = "服务器的浏览器提醒通道已关闭"; this.render(); return; }
      if (!baseline) {
        const deliver = async () => {
          for (const item of data.items) {
            if (generation !== this.generation || !this.ready()) return;
            const age = Date.now() - Date.parse(item.created_at);
            // A background tab may resume long after its response was fetched.
            if (!Number.isFinite(age) || age > 300000) continue;
            const last = Number(this.read("last-shown") || 0);
            if (item.id <= last) continue;
            this.show(item.title, item.body, "stockbark-" + this.scope + "-" + item.id);
            this.write("last-shown", String(item.id));
          }
        };
        // Edge/Chrome/Firefox serialize tabs of the same origin to prevent duplicates.
        if (navigator.locks) await navigator.locks.request(this.key("delivery-lock"), deliver);
        else await deliver();
      }
      if (generation !== this.generation) return;
      this.cursor = data.cursor;
      this.error = "";
      this.render();
    } catch (error) {
      this.error = "桌面提醒检查失败：" + error.message + "；连接恢复后会自动继续";
      this.render();
    } finally { this.busy = false; }
  }
}
