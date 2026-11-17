/* Browser API is simulated to avoid showing test notifications on the host OS.
 * Backend integration, rendering, expiry and persistence are tested in test_app.py.
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.PLAYWRIGHT_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_EXECUTABLE} : {})});
  try {
    const context = await browser.newContext();
    await context.addInitScript(() => {
      window.notificationCalls = [];
      window.permissionRequests = 0;
      window.testPermission = "default";
      class TestNotification {
        static get permission() { return window.testPermission; }
        static async requestPermission() { window.permissionRequests++; return window.testPermission = "granted"; }
        constructor(title, options) {
          window.notificationCalls.push({title, ...options});
          window.latestNotification = this;
        }
        close() { this.onclose?.(); }
      }
      Object.defineProperty(window, "Notification", {value: TestNotification, configurable: true});
    });
    let feed = [];
    await context.route("**/api/browser-events*", route => {
      const after = new URL(route.request().url()).searchParams.get("after");
      const head = feed.at(-1)?.id || 0;
      return route.fulfill({json: {enabled: true, cursor: head, items: after === null ? [] : feed.filter(x => x.id > Number(after))}});
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("http://127.0.0.1:18080");
    await page.locator("#password").fill(process.env.TEST_PASSWORD);
    await page.locator('#login-form button[type="submit"]').click();
    await page.locator("#app-screen").waitFor({state: "visible"});
    await page.locator('[data-tab="settings"]').click();
    await page.locator('[name="browser_template_mode"]').selectOption("custom");
    await page.locator('[name="browser_title"]').fill("我的 {name} 提醒");
    await page.locator('[name="browser_body"]').fill("{period} J={J} K={K} D={D}");
    await page.locator("#browser-test").click();
    await page.waitForFunction(() => notificationCalls.length === 1);
    const preview = await page.evaluate(() => notificationCalls[0]);
    assert.equal(preview.title, "[测试] 我的 先锋精科 提醒");
    assert.ok(preview.body.includes("5分钟 J=105.00"));
    assert.ok(preview.body.includes("不代表真实行情"));
    assert.equal(await page.evaluate(() => permissionRequests), 1);
    assert.equal(await page.evaluate(() => desktop.optedIn()), false, "A test alone must not opt in to live alerts");
    await page.locator("#browser-alert-close").click();
    await page.locator("#browser-enable").click();
    await page.waitForFunction(() => desktop.optedIn());
    await page.locator('[name="browser_enabled"]').check();
    await Promise.all([
      page.waitForResponse(r => r.url().endsWith("/api/state") && r.ok()),
      page.locator('#settings-form button[type="submit"]').click()
    ]);
    await page.waitForFunction(() => desktop.cursor === 0 && desktop.ready());
    assert.equal(await page.evaluate(() => permissionRequests), 1, "Already granted permission must not prompt again");
    fs.mkdirSync("data/screenshots", {recursive: true});
    await page.evaluate(() => { document.querySelector("#toast").hidden = true; });
    await page.screenshot({path: "data/screenshots/notifications-desktop.png", fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: "data/screenshots/notifications-mobile.png", fullPage: true});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.setViewportSize({width: 1280, height: 800});

    const other = await context.newPage();
    other.on("pageerror", error => errors.push(error.message));
    await other.goto("http://127.0.0.1:18080");
    await other.waitForFunction(() => typeof desktop !== "undefined" && desktop.config !== null);
    await other.evaluate(() => { window.testPermission = "granted"; });
    await other.evaluate(() => desktop.poll());
    assert.equal(await other.evaluate(() => desktop.cursor), 0);
    await page.evaluate(() => { notificationCalls.length = 0; });
    // Both tabs race for the same event. The background tab must still poll.
    feed.push({id: 1, title: "真实信号样式", body: "J=99.00 K>D", created_at: new Date().toISOString()});
    await other.bringToFront();
    await page.waitForTimeout(6500);
    const counts = await Promise.all([page.evaluate(() => notificationCalls.length), other.evaluate(() => notificationCalls.length)]);
    assert.equal(counts[0] + counts[1], 1, "Multiple tabs must display a signal only once");
    await Promise.all([page.evaluate(() => desktop.poll()), other.evaluate(() => desktop.poll())]);
    const repeated = await Promise.all([page.evaluate(() => notificationCalls.length), other.evaluate(() => notificationCalls.length)]);
    assert.equal(repeated[0] + repeated[1], 1);
    // Reopening starts at the head, with no replay of preexisting signals.
    await other.reload();
    await other.waitForFunction(() => typeof desktop !== "undefined" && desktop.config !== null);
    await other.evaluate(() => { window.testPermission = "granted"; });
    await other.evaluate(() => desktop.poll());
    assert.equal(await other.evaluate(() => notificationCalls.length), 0);
    assert.equal(await other.evaluate(() => desktop.cursor), 1);
    feed.push({id: 2, title: "过期响应", body: "should be skipped", created_at: new Date(Date.now() - 360000).toISOString()});
    await Promise.all([page.evaluate(() => desktop.poll()), other.evaluate(() => desktop.poll())]);
    const afterExpiry = await Promise.all([page.evaluate(() => notificationCalls.length), other.evaluate(() => notificationCalls.length)]);
    assert.equal(afterExpiry[0] + afterExpiry[1], repeated[0]);
    await other.locator('[data-tab="settings"]').click();
    await other.locator("#browser-disable").click();
    await page.waitForFunction(() => !desktop.optedIn());
    feed.push({id: 3, title: "不应展示", body: "disabled", created_at: new Date().toISOString()});
    await other.evaluate(() => desktop.poll());
    assert.equal(await other.evaluate(() => notificationCalls.length), 0);
    await other.evaluate(() => { window.testPermission = "denied"; desktop.render(); });
    assert.equal(await other.locator("#browser-enable").isDisabled(), false);
    assert.ok((await other.locator("#browser-status").textContent()).includes("拒绝"));
    await other.locator("#browser-enable").click();
    await other.waitForFunction(() => desktop.optedIn());
    feed.push({id: 4, title: "页面兜底提醒", body: "系统权限拒绝时仍弹出页面", created_at: new Date().toISOString()});
    await other.evaluate(() => desktop.poll());
    await other.locator("#browser-alert-modal").waitFor({state: "visible"});
    assert.ok((await other.locator("#browser-alert-body").textContent()).includes("系统权限拒绝"));
    await other.locator("#browser-alert-close").click();
    await other.evaluate(() => { Object.defineProperty(window, "isSecureContext", {value: false, configurable: true}); desktop.render(); });
    assert.ok((await other.locator("#browser-status").textContent()).includes("HTTPS"));
    assert.equal(await other.locator("#browser-test").isDisabled(), false);
    await other.locator("#logout").click();
    await other.locator("#login-screen").waitFor({state: "visible"});
    assert.equal(await other.evaluate(() => desktop.config), null);
    assert.deepEqual(errors, []);
    console.log("Desktop notifications OK: permission, unsaved custom test, saved channel, browser-only opt-in, background polling, multi-tab dedup, no replay, disable, denial, logout.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
