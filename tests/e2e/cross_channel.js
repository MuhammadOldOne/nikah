// Browser part of the cross-channel end-to-end test, run by tests/e2e/test_cross_channel.py.
//
// Two browsers (brother: desktop, sister: phone) and an admin use the real site; the Telegram side
// runs the bot's real handlers and outbox worker through `python -m tests.e2e.telegram_side`.
// Progress goes to stderr; the result (ids and the Telegram deliveries) is printed to stdout as JSON
// for the Python runner to check. Any failed expectation exits with code 1.
"use strict";

const { execFile } = require("node:child_process");
const path = require("node:path");
const { promisify } = require("node:util");
const { chromium } = require("playwright");

const BASE = process.env.E2E_BASE_URL;
const PYTHON = process.env.E2E_PYTHON;
const ROOT = process.env.E2E_ROOT;
const SHOTS = process.env.E2E_SCREENSHOTS;
const ACCOUNTS = JSON.parse(process.env.E2E_ACCOUNTS || "{}");
const LIVE_TIMEOUT = 10000; // a live update must arrive without any reload within this time

const execFileAsync = promisify(execFile);
const problems = [];

function log(...parts) {
  console.error("[e2e]", ...parts);
}

function expect(condition, message) {
  if (!condition) throw new Error("expectation failed: " + message);
}

async function telegram(command, ...args) {
  const { stdout } = await execFileAsync(PYTHON, ["-m", "tests.e2e.telegram_side", command, ...args.map(String)], {
    cwd: ROOT,
    env: process.env,
    timeout: 60000,
  });
  return JSON.parse(stdout);
}

async function shot(page, name) {
  await page.screenshot({ path: path.join(SHOTS, `e2e-${name}.png`), fullPage: false });
  log("screenshot", `e2e-${name}.png`);
}

async function newPage(browser, label, options) {
  const context = await browser.newContext({ locale: "ru-RU", timezoneId: "Europe/Moscow", ...options });
  // Counts SSE "ready" events: the page is subscribed to live updates once it is > 0.
  await context.addInitScript(() => {
    window.__e2eReady = 0;
    document.addEventListener("live:ready", () => { window.__e2eReady += 1; });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on("pageerror", (error) => problems.push(`${label}: page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`${label}: console error: ${message.text()}`);
  });
  return page;
}

async function devLogin(page, telegramId, firstName, username) {
  const query = new URLSearchParams({ telegram_id: String(telegramId), first_name: firstName, username });
  await page.goto(`${BASE}/auth/dev-login?${query}`);
}

async function chip(page, name, value) {
  await page.locator(`label.chip:has(input[name="${name}"][value="${value}"])`).click();
}

async function completeWizard(page, person, screenshotName) {
  expect(new URL(page.url()).pathname === "/profile/new", `${person.name}: a new account lands on the wizard`);
  await chip(page, "gender", person.gender);
  await page.fill("#f-name", person.name);
  await page.selectOption("#f-birth_year", String(person.birthYear));
  await page.fill("#f-city", person.city);
  await page.click('section[data-step="1"] [data-next]');
  await chip(page, "marital_status", "never_married");
  await page.fill("#f-children", "0");
  await chip(page, "education", "higher");
  await chip(page, "employment", person.employment);
  await page.click('section[data-step="2"] [data-next]');
  await page.fill("#f-wishes", person.wishes);
  for (const name of ["consent_pd", "confirm_age", "accept_rules"]) await page.check(`input[name="${name}"]`);
  await shot(page, screenshotName);
  await Promise.all([
    page.waitForURL(`${BASE}/catalog`),
    page.click('button[type="submit"]:has-text("Создать анкету")'),
  ]);
  const catalog = await page.locator("main").innerText();
  expect(/модерац/i.test(catalog), `${person.name}: the new profile waits for moderation (PREMODERATION=true)`);
  await page.goto(`${BASE}/me`);
  const match = (await page.locator("main").innerText()).match(/#(\d+) ·/);
  expect(match, `${person.name}: «Моя анкета» shows the public #id`);
  return Number(match[1]);
}

async function waitLive(page) {
  await page.waitForFunction(() => window.__e2eReady > 0);
  await page.evaluate(() => { window.__e2eNoReload = true; });
}

async function expectNoReload(page, label) {
  expect(await page.evaluate(() => window.__e2eNoReload === true), `${label}: the update arrived without a reload`);
}

async function main() {
  const browser = await chromium.launch();
  // The bot's outbox worker runs at the points where the real one (woken by NOTIFY) would have
  // delivered: some notifications are skipped once outdated (a profile no longer pending, etc.).
  const result = { accounts: ACCOUNTS, drains: {} };
  const drain = async (name) => { result.drains[name] = await telegram("drain"); };
  try {
    const brother = await newPage(browser, "brother", { viewport: { width: 1280, height: 860 } });
    const sister = await newPage(browser, "sister", {
      viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true,
    });
    const admin = await newPage(browser, "admin", { viewport: { width: 1280, height: 900 } });

    // a) Both create a profile through the wizard; the admin approves them.
    log("a) wizard + moderation");
    await devLogin(brother, ACCOUNTS.brother, "Ахмад", "e2e_brother");
    result.brotherId = await completeWizard(brother, {
      gender: "male", name: "Ахмад", birthYear: 1994, city: "Казань", employment: "employed",
      wishes: "Ищу благочестивую супругу, которая ценит семью, намаз и спокойный дом.",
    }, "01-wizard-brother");
    await devLogin(sister, ACCOUNTS.sister, "Мариям", "e2e_sister");
    result.sisterId = await completeWizard(sister, {
      gender: "female", name: "Мариям", birthYear: 1998, city: "Казань", employment: "student",
      wishes: "Ищу верующего и ответственного супруга, готового создать крепкую семью.",
    }, "02-wizard-sister");

    await drain("submitted");
    await devLogin(admin, ACCOUNTS.admin, "Модератор", "e2e_admin");
    await admin.goto(`${BASE}/admin/moderation`);
    for (const id of [result.brotherId, result.sisterId]) {
      await admin.locator(`#p${id}`).waitFor();
    }
    await admin.locator(`#p${result.sisterId}`).scrollIntoViewIfNeeded();
    await shot(admin, "03-admin-moderation");
    for (const id of [result.brotherId, result.sisterId]) {
      await Promise.all([
        admin.waitForURL(/\/admin\/moderation/),
        admin.click(`#p${id} form[action$="/approve"] button[type="submit"]`),
      ]);
      await admin.locator(`text=Анкета #${id} опубликована.`).waitFor();
    }
    await drain("moderated");

    // b) Brother finds sister in the catalog and sends an interest; her badge updates live.
    log("b) interest -> accept");
    await sister.goto(`${BASE}/catalog`);
    await waitLive(sister);
    await brother.goto(`${BASE}/catalog`);
    const card = brother.locator(`a[href="/u/${result.sisterId}"]`).first();
    await card.waitFor();
    await shot(brother, "04-catalog-brother");
    await card.click();
    await brother.waitForURL(`${BASE}/u/${result.sisterId}`);
    const interestText = "Ассаляму алейкум! Живу в Казани, работаю инженером. Буду рад познакомиться.";
    await brother.fill("#interest-message", interestText);
    await Promise.all([
      brother.waitForURL(`${BASE}/u/${result.sisterId}`),
      brother.click('button[type="submit"]:has-text("Откликнуться")'),
    ]);
    await brother.locator("#action-title", { hasText: "Отклик отправлен" }).waitFor();
    await shot(brother, "05-interest-sent");
    await drain("interest");

    const badge = sister.locator('.tabbar [data-badge="interests"]');
    await badge.waitFor({ state: "visible", timeout: LIVE_TIMEOUT });
    expect((await badge.innerText()).trim() === "1", "sister: the interests badge shows 1");
    await expectNoReload(sister, "sister badge");
    await shot(sister, "06-sister-live-badge");

    await sister.goto(`${BASE}/interests`);
    const incoming = sister.locator("li.icard", { hasText: interestText });
    await incoming.waitFor();
    await shot(sister, "07-interest-incoming");
    await Promise.all([
      sister.waitForURL(/\/chats\/\d+$/),
      incoming.locator('button:has-text("Принять и начать чат")').click(),
    ]);
    result.conversationId = Number(new URL(sister.url()).pathname.split("/").pop());
    await waitLive(sister);

    // c) A message written on the site appears in the partner's open chat page via SSE.
    log("c) site -> site live message");
    await brother.goto(`${BASE}/chats/${result.conversationId}`);
    await waitLive(brother);
    const siteText = "Ассаляму алейкум, Мариям! Спасибо, что приняли отклик.";
    const sentAt = Date.now();
    await brother.fill("#composer-text", siteText);
    await brother.press("#composer-text", "Enter");
    await brother.locator("#messages li.msg--mine", { hasText: siteText }).waitFor();
    await sister.locator("#messages li.msg", { hasText: siteText }).waitFor({ timeout: LIVE_TIMEOUT });
    result.siteToSiteMs = Date.now() - sentAt;
    await expectNoReload(sister, "sister chat");
    await shot(sister, "08-live-site-message");

    // d) Sister answers from Telegram (the bot's real relay) -> brother's page shows it live.
    log("d) telegram -> site live message");
    const telegramText = "Ва алейкум ассалам! Отвечаю из Telegram-бота.";
    const relayed = await telegram("relay", ACCOUNTS.sister, result.conversationId, telegramText);
    result.relay = relayed;
    expect(relayed.message.origin === "telegram", "the bot stored the reply with origin=telegram");
    expect(relayed.replies_after_text.length === 0, "the bot sends no confirmation for a relayed message");
    const fromTelegram = brother.locator("#messages li.msg", { hasText: telegramText });
    await fromTelegram.waitFor({ timeout: LIVE_TIMEOUT });
    expect(!(await fromTelegram.evaluate((el) => el.classList.contains("msg--mine"))), "brother: shown as partner's");
    expect((await fromTelegram.locator(".msg__origin").innerText()).includes("из Telegram"), "brother: «из Telegram» mark");
    await expectNoReload(brother, "brother chat");
    await sister.locator("#messages li.msg--mine", { hasText: telegramText }).waitFor({ timeout: LIVE_TIMEOUT });
    await shot(brother, "09-telegram-reply-on-site");
    await shot(sister, "10-own-telegram-reply-on-site");
    await drain("chat");

    // e) Sister links a wali: invite on the site, acceptance in the bot.
    log("e) wali + outbox delivery");
    await sister.goto(`${BASE}/settings`);
    await Promise.all([
      sister.waitForURL(/\/settings/),
      sister.click('form[action="/settings/wali/invite"] button[type="submit"]'),
    ]);
    const inviteLink = await sister.locator("#wali-link").inputValue();
    const start = new URL(inviteLink).searchParams.get("start") || "";
    expect(start.startsWith("wali_"), "the invite is a t.me deep link with start=wali_<token>");
    await sister.locator("#wali-link").scrollIntoViewIfNeeded();
    await shot(sister, "11-wali-invite");
    const wali = await telegram("wali-accept", ACCOUNTS.wali, "Юсуф", start.slice("wali_".length));
    result.wali = wali;
    expect(wali.prompt.includes("Приглашение стать вали") && wali.prompt.includes("Мариям"), "wali sees whose wali he becomes");
    expect(wali.confirmation.some((text) => text.includes("Вы стали вали")), "wali confirmation in the bot");
    await sister.reload();
    await sister.locator("#wali .alert--success", { hasText: "Вали подключён" }).waitFor();
    await shot(sister, "12-wali-linked");

    await brother.reload();
    await waitLive(brother);
    await brother.locator(".chat__notice", { hasText: "Вали подключён" }).waitFor();
    await sister.goto(`${BASE}/chats/${result.conversationId}`);
    await waitLive(sister);
    await drain("wali");
    const watchedText = "Хорошо, что вали подключён. Расскажите, пожалуйста, о себе.";
    await brother.fill("#composer-text", watchedText);
    await brother.press("#composer-text", "Enter");
    await brother.locator("#messages li.msg--mine", { hasText: watchedText }).waitFor();
    await sister.locator("#messages li.msg", { hasText: watchedText }).waitFor({ timeout: LIVE_TIMEOUT });
    await shot(brother, "13-wali-banner-brother");
    await drain("watched"); // only the message written under the wali's eye
    result.texts = { interestText, siteText, telegramText, watchedText };
  } finally {
    await browser.close();
  }
  expect(problems.length === 0, "no JavaScript errors in the pages:\n" + problems.join("\n"));
  return result;
}

main().then(
  (result) => {
    process.stdout.write(JSON.stringify(result) + "\n");
  },
  (error) => {
    console.error(error && error.stack ? error.stack : error);
    process.exit(1);
  },
);
