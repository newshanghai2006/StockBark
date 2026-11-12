/* Optional UI smoke test. Set PLAYWRIGHT_MODULE to an installed playwright package. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  const deadline = Date.now() + 60000;
  while (true) {
    try { if ((await fetch("http://127.0.0.1:18080/healthz")).ok) break; } catch {}
    if (Date.now() > deadline) throw new Error("UI test server is not ready");
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    // The initial state probe deliberately receives 401 before a user logs in.
    if (message.type() === "error" && !message.text().includes("401 (UNAUTHORIZED)")) errors.push(message.text());
  });
  await page.goto("http://127.0.0.1:18080");
  await page.locator("#password").fill(process.env.TEST_PASSWORD);
  await page.locator('#login-form button[type="submit"]').click();
  await page.locator("#app-screen").waitFor({ state: "visible" });
  await page.locator('.nav-item[data-tab="stocks"]').click();
  await page.getByRole("button", { name: "＋ 添加股票" }).click();
  await page.locator('#editor [name="name"]').fill("平安银行");
  await page.locator('#editor [name="symbol"]').fill("000001");
  await page.locator("#save-editor").click();
  await page.locator("#editor").waitFor({ state: "hidden" });
  await page.locator("#stocks-list").getByRole("heading", { name: /平安银行/ }).waitFor();

  await page.locator('.nav-item[data-tab="targets"]').click();
  await page.getByRole("button", { name: "＋ 添加设备" }).click();
  await page.locator('#editor [name="name"]').fill("测试 iPhone");
  await page.locator('#editor [name="token"]').fill("ui-test-not-a-real-device");
  await page.locator("#save-editor").click();
  await page.locator("#editor").waitFor({ state: "hidden" });
  await page.locator("#targets-list").getByRole("heading", { name: "测试 iPhone" }).waitFor();

  await page.locator('.nav-item[data-tab="rules"]').click();
  await page.getByRole("button", { name: "＋ 添加规则" }).click();
  await page.locator('#editor [name="name"]').fill("自定义穿越提醒");
  await page.locator('.condition-row [data-part="left"]').first().selectOption("K");
  await page.locator('.condition-row [data-part="op"]').first().selectOption("cross_above");
  await page.locator('.condition-row [data-part="right"]').first().fill("D");
  await page.locator('[data-action="add-condition"]').click();
  assert.equal(await page.locator(".condition-row").count(), 3);
  await page.locator('.condition-row [data-action="remove-condition"]').last().click();
  await page.locator('#editor [name="body"]').fill("{name} 测试 K={K} D={D} J={J}");
  await page.locator("#save-editor").click();
  await page.locator("#editor").waitFor({ state: "hidden" });
  await page.locator("#rules-list").getByRole("heading", { name: "自定义穿越提醒" }).waitFor();

  await page.locator('.nav-item[data-tab="settings"]').click();
  await page.locator('#settings-form [name="poll_seconds"]').fill("90");
  await page.locator('#settings-form button[type="submit"]').click();
  await page.waitForResponse(r => r.url().endsWith("/api/state") && r.ok());
  const saved = await page.evaluate(async () => (await fetch("/api/state")).json());
  assert.equal(saved.settings.poll_seconds, 90);
  assert.equal(saved.stocks.length, 2);
  assert.equal(saved.rules.length, 3);
  assert.equal(saved.targets.length, 1);
  assert.ok(!JSON.stringify(saved).includes("ui-test-not-a-real-device"));
  await page.locator('.nav-item[data-tab="history"]').click();
  await page.locator("#history-list .empty").waitFor();
  await page.locator('.nav-item[data-tab="overview"]').click();
  await page.evaluate(() => { document.querySelector("#toast").hidden = true; });
  fs.mkdirSync("data/screenshots", { recursive: true });
  await page.screenshot({ path: "data/screenshots/desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "data/screenshots/mobile.png", fullPage: true });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile page overflows horizontally");
  assert.deepEqual(errors, [], "Browser errors");
  console.log("UI OK: login, stock/target/rule CRUD, conditions, settings, history, responsive layout, no token leak.");
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
