// Browser part of the cross-channel end-to-end test, run by tests/e2e/test_cross_channel.py.
//
// Two browsers (brother: desktop, sister: phone), an admin and a person migrated from the legacy bot
// use the real site; the Telegram side runs the bot's real handlers and outbox worker through
// `python -m tests.e2e.telegram_side`. People are known by profile number only («Анкета №N»).
// Progress goes to stderr; the result (numbers, ids and the Telegram deliveries) is printed to stdout
// as JSON for the Python runner to check. Any failed expectation exits with code 1.
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
const LEGACY = JSON.parse(process.env.E2E_LEGACY || "{}");
const LIVE_TIMEOUT = 10000; // a live update must arrive without any reload within this time
const CONTACTS_HIDDEN = "Часть текста была скрыта фильтром (телефоны/ссылки/@). Свидетель уведомлён.";
const OPEN_CHAT_LIMIT = "У одного из участников уже есть открытый чат. Завершите его сначала.";

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

async function mainText(page) {
  return page.locator("main").innerText();
}

// A new account fills the wizard (no name: people are known by number) and lands on «Моя анкета».
async function completeWizard(page, person, screenshotName) {
  expect(new URL(page.url()).pathname === "/profile/new", `${person.label}: a new account lands on the wizard`);
  expect(await page.locator('input[name="name"]').count() === 0, `${person.label}: the wizard has no name field`);
  await chip(page, "gender", person.gender);
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
    page.waitForURL(`${BASE}/me`),
    page.click('button[type="submit"]:has-text("Создать анкету")'),
  ]);
  const me = await mainText(page);
  expect(/модерац/i.test(me), `${person.label}: the new profile waits for moderation (PREMODERATION=true)`);
  const match = me.match(/Анкета №(\d+)/);
  expect(match, `${person.label}: «Моя анкета» shows the profile number`);
  // Pending profiles may not browse (legacy rule against fakes collecting profiles).
  await page.goto(`${BASE}/catalog`);
  expect(/ещё не одобрен|модерац/i.test(await mainText(page)), `${person.label}: the catalog waits for approval`);
  return Number(match[1]);
}

async function waitLive(page) {
  await page.waitForFunction(() => window.__e2eReady > 0);
  await page.evaluate(() => { window.__e2eNoReload = true; });
}

async function expectNoReload(page, label) {
  expect(await page.evaluate(() => window.__e2eNoReload === true), `${label}: the update arrived without a reload`);
}

async function send(page, text) {
  await page.fill("#composer-text", text);
  await page.press("#composer-text", "Enter");
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
    const brotherNumber = await completeWizard(brother, {
      label: "brother", gender: "male", birthYear: 1994, city: "Казань", employment: "employed",
      wishes: "Ищу благочестивую супругу, которая ценит семью, намаз и спокойный дом.",
    }, "01-wizard-brother");
    await devLogin(sister, ACCOUNTS.sister, "Мариям", "e2e_sister");
    const sisterNumber = await completeWizard(sister, {
      label: "sister", gender: "female", birthYear: 1998, city: "Казань", employment: "student",
      wishes: "Ищу верующего и ответственного супруга, готового создать крепкую семью.",
    }, "02-wizard-sister");
    result.brother = { number: brotherNumber };
    result.sister = { number: sisterNumber };

    await drain("submitted");
    await devLogin(admin, ACCOUNTS.admin, "Модератор", "e2e_admin");
    await admin.goto(`${BASE}/admin/moderation`);
    const queued = (number) => admin.locator("article.adm-card", { has: admin.locator("h2", { hasText: `Анкета №${number},` }) });
    for (const number of [brotherNumber, sisterNumber]) await queued(number).waitFor();
    await queued(sisterNumber).scrollIntoViewIfNeeded();
    await shot(admin, "03-admin-moderation");
    for (const [key, number] of [["brother", brotherNumber], ["sister", sisterNumber]]) {
      const card = queued(number);
      result[key].userId = Number((await card.getAttribute("id")).slice(1)); // internal id: admin pages only
      await Promise.all([
        admin.waitForURL(/\/admin\/moderation/),
        card.locator('form[action$="/approve"] button[type="submit"]').click(),
      ]);
      await admin.locator(`text=Анкета №${number} опубликована.`).waitFor();
    }
    await drain("moderated");

    // b) Brother finds sister in the catalog by number and sends an interest; her badge updates live.
    log("b) interest -> accept");
    await sister.goto(`${BASE}/catalog`);
    await waitLive(sister);
    await brother.goto(`${BASE}/catalog`);
    const card = brother.locator(`a[href="/p/${sisterNumber}"]`).first();
    await card.waitFor();
    expect((await card.innerText()).includes(`Анкета №${sisterNumber} — Сестра`), "catalog card: «Анкета №N — Сестра»");
    await shot(brother, "04-catalog-brother");
    await card.click();
    await brother.waitForURL(`${BASE}/p/${sisterNumber}`);
    const interestText = "Ассаляму алейкум! Живу в Казани, работаю инженером. Буду рад познакомиться.";
    await brother.fill("#interest-message", interestText);
    await Promise.all([
      brother.waitForURL(`${BASE}/p/${sisterNumber}`),
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
    expect(
      (await sister.locator(".chat__witness").innerText()).includes("Переписку видит свидетель (махрам/администратор)"),
      "sister: the chat says the witness reads it",
    );

    // c) A message written on the site appears in the partner's open chat page via SSE.
    log("c) site -> site live message");
    await brother.goto(`${BASE}/chats/${result.conversationId}`);
    await waitLive(brother);
    const siteText = "Ассаляму алейкум! Спасибо, что приняли отклик.";
    const sentAt = Date.now();
    await send(brother, siteText);
    await brother.locator("#messages li.msg--mine", { hasText: siteText }).waitFor();
    await sister.locator("#messages li.msg", { hasText: siteText }).waitFor({ timeout: LIVE_TIMEOUT });
    result.siteToSiteMs = Date.now() - sentAt;
    await expectNoReload(sister, "sister chat");
    await shot(sister, "08-live-site-message");

    // d) Sister answers from Telegram: with one open chat any text goes there -> live on the site.
    log("d) telegram -> site live message");
    const telegramText = "Ва алейкум ассалам! Отвечаю из Telegram-бота.";
    const relayed = await telegram("relay", ACCOUNTS.sister, telegramText);
    result.relay = relayed;
    expect(relayed.message.origin === "telegram", "the bot stored the reply with origin=telegram");
    expect(relayed.message.conversation_id === result.conversationId, "the text went to the only open chat");
    expect(relayed.replies_after_text.length === 0, "the bot sends no confirmation for a relayed message");
    const fromTelegram = brother.locator("#messages li.msg", { hasText: telegramText });
    await fromTelegram.waitFor({ timeout: LIVE_TIMEOUT });
    expect(!(await fromTelegram.evaluate((el) => el.classList.contains("msg--mine"))), "brother: shown as partner's");
    expect((await fromTelegram.locator(".msg__origin").innerText()).includes("из Telegram"), "brother: «из Telegram» mark");
    await expectNoReload(brother, "brother chat");
    await sister.locator("#messages li.msg--mine", { hasText: telegramText }).waitFor({ timeout: LIVE_TIMEOUT });
    await shot(brother, "09-telegram-reply-on-site");
    await shot(sister, "10-own-telegram-reply-on-site");
    await drain("chat"); // includes the witness copies of both messages

    // The chat list on a phone: a long last message is cut with an ellipsis, the time stays in the card.
    const list = await sister.context().newPage();
    await list.goto(`${BASE}/chats`);
    const layout = await list.evaluate(() => {
      const card = document.querySelector(".chat-list").getBoundingClientRect();
      const time = document.querySelector(".chat-item .chat-item__time").getBoundingClientRect();
      return { cardRight: card.right, timeRight: time.right, pageWidth: document.documentElement.scrollWidth };
    });
    expect(layout.timeRight <= layout.cardRight + 1, `chat list: the time stays inside the card (${JSON.stringify(layout)})`);
    expect(layout.pageWidth <= (await list.evaluate(() => window.innerWidth)), "chat list: no horizontal scroll");
    await shot(list, "10b-chat-list-phone");
    await list.close();

    // e) The witness presses «✍️ Написать в чат» in the witness group: both open pages show it live.
    log("e) witness writes into the chat");
    const witnessText = "Ассаляму алейкум. Напоминаю: обсуждайте никах через вали, без личных встреч.";
    result.witnessSay = await telegram("witness-say", ACCOUNTS.admin, result.conversationId, witnessText);
    for (const [label, page] of [["brother", brother], ["sister", sister]]) {
      const item = page.locator("#messages li.msg--witness", { hasText: witnessText });
      await item.waitFor({ timeout: LIVE_TIMEOUT });
      // textContent: the label is styled uppercase, innerText would read «СВИДЕТЕЛЬ».
      expect((await item.locator(".msg__label").textContent()).includes("Свидетель"), `${label}: signed «Свидетель»`);
      expect((await item.locator(".msg__report").count()) === 0, `${label}: a witness message cannot be reported`);
      await expectNoReload(page, `${label} witness message`);
    }
    await shot(brother, "11-witness-message-brother");
    await shot(sister, "12-witness-message-sister");
    await drain("witness");

    // f) Sister links a wali: invite on the site, acceptance in the bot.
    log("f) wali + outbox delivery");
    await sister.goto(`${BASE}/settings`);
    await Promise.all([
      sister.waitForURL(/\/settings/),
      sister.click('form[action="/settings/wali/invite"] button[type="submit"]'),
    ]);
    const inviteLink = await sister.locator("#wali-link").inputValue();
    const start = new URL(inviteLink).searchParams.get("start") || "";
    expect(start.startsWith("wali_"), "the invite is a t.me deep link with start=wali_<token>");
    await sister.locator("#wali-link").scrollIntoViewIfNeeded();
    await shot(sister, "13-wali-invite");
    const wali = await telegram("wali-accept", ACCOUNTS.wali, "Юсуф", start.slice("wali_".length));
    result.wali = wali;
    expect(
      wali.prompt.includes("Приглашение стать вали") && wali.prompt.includes(`Анкета №${sisterNumber}`),
      "wali sees whose wali he becomes",
    );
    expect(wali.confirmation.some((text) => text.includes("Вы стали вали")), "wali confirmation in the bot");
    await sister.reload();
    await sister.locator("#wali .alert--success", { hasText: "Вали подключён" }).waitFor();
    await shot(sister, "14-wali-linked");

    // The brother's open chat page shows the wali notice at once, without a reload.
    await brother.locator("#wali-notice", { hasText: "Вали подключён" }).waitFor({ timeout: LIVE_TIMEOUT });
    await expectNoReload(brother, "brother wali notice");
    await sister.goto(`${BASE}/chats/${result.conversationId}`);
    await waitLive(sister);
    await drain("wali");
    const watchedText = "Хорошо, что вали подключён. Расскажите, пожалуйста, о себе.";
    await send(brother, watchedText);
    await brother.locator("#messages li.msg--mine", { hasText: watchedText }).waitFor();
    await sister.locator("#messages li.msg", { hasText: watchedText }).waitFor({ timeout: LIVE_TIMEOUT });
    await shot(brother, "15-wali-banner-brother");
    await drain("watched"); // only the message written under the wali's eye

    // g) A phone number in a site message: the sender sees the filter notice, everyone else the placeholder.
    log("g) contact filter");
    const phone = "+7 912 345-67-89";
    const phoneVisible = "Запишите мой номер:";
    const filteredText = `${phoneVisible} ${phone}`;
    await send(brother, filteredText);
    const status = brother.locator("#composer-status");
    await status.waitFor({ state: "visible" });
    expect((await status.innerText()).includes(CONTACTS_HIDDEN), "brother: the filter notice next to the composer");
    const own = brother.locator("#messages li.msg--mine", { hasText: phoneVisible });
    await own.waitFor();
    const ownText = await own.locator(".msg__bubble").innerText();
    expect(ownText.includes("[скрыто свидетелем]") && !ownText.includes("912"), "brother: the stored text is filtered");
    expect((await own.locator(".msg__filtered").count()) === 1, "brother: «часть текста скрыта фильтром» mark");
    const theirs = sister.locator("#messages li.msg", { hasText: phoneVisible });
    await theirs.waitFor({ timeout: LIVE_TIMEOUT });
    expect(!(await theirs.innerText()).includes("912"), "sister: the number never reaches the partner");
    await shot(brother, "16-contact-filter-notice");
    await drain("filtered");
    result.texts = {
      interestText, siteText, telegramText, witnessText, watchedText, phone, phoneVisible,
    };

    // h) A person migrated from the legacy bot: consent gate, old number, completion banner, old chat.
    log("h) migrated legacy user");
    const migrated = await newPage(browser, "migrated", {
      viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true,
    });
    const legacyNumber = LEGACY.numbers.brother;
    await devLogin(migrated, LEGACY.telegramIds.brother, "Старожил", "e2e_legacy");
    const landing = new URL(migrated.url());
    const welcomeText = await mainText(migrated);
    result.legacy = { landing: landing.pathname, welcomeText };
    expect(landing.pathname === "/welcome", `migrated: the consent gate first (got ${landing.pathname})`);
    expect(welcomeText.includes(`Анкета №${legacyNumber}`), "migrated: the welcome page names the old number");
    await shot(migrated, "17-legacy-welcome");
    for (const name of ["consent_pd", "accept_rules"]) await migrated.check(`input[name="${name}"]`);
    await Promise.all([
      migrated.waitForURL((url) => url.pathname !== "/welcome"),
      migrated.click('button[type="submit"]:has-text("Продолжить")'),
    ]);
    await migrated.goto(`${BASE}/me`);
    const me = await mainText(migrated);
    expect(me.includes(`Анкета №${legacyNumber}`), "migrated: «Моя анкета» keeps the old number");
    expect(/Опубликована/.test(me), "migrated: the approved legacy profile stays published");
    const banner = migrated.locator('a[href="/me/complete"]', { hasText: "Дополнить анкету (1 минута)" });
    expect((await banner.count()) === 1, "migrated: the completion banner");
    expect(me.includes("не указано"), "migrated: empty new fields show «не указано»");
    await shot(migrated, "18-legacy-me-complete-banner");
    await migrated.goto(`${BASE}/chats/${LEGACY.chatId}`);
    const history = migrated.locator("#messages li.msg");
    await history.first().waitFor();
    expect((await history.count()) === LEGACY.messages.length, "migrated: the whole old chat history");
    for (const text of LEGACY.messages) {
      const item = migrated.locator("#messages li.msg", { hasText: text });
      expect((await item.locator(".msg__origin").innerText()).includes("из Telegram"), `migrated: «из Telegram» on «${text}»`);
    }
    expect(
      (await migrated.locator(".chat__who").innerText()).includes(`Анкета №${LEGACY.numbers.sister}`),
      "migrated: the partner is shown by her old number",
    );
    await shot(migrated, "19-legacy-chat-history");

    // i) The legacy brother (open chat with his old partner) sends sister an interest; she cannot
    //    accept it while either of them has an open chat (MAX_OPEN_CONVERSATIONS=1).
    log("i) one open chat per person");
    await migrated.goto(`${BASE}/p/${sisterNumber}`);
    const secondInterest = "Ассаляму алейкум! Перешёл из Telegram-бота, хотел бы познакомиться.";
    await migrated.fill("#interest-message", secondInterest);
    await Promise.all([
      migrated.waitForURL(`${BASE}/p/${sisterNumber}`),
      migrated.click('button[type="submit"]:has-text("Откликнуться")'),
    ]);
    await migrated.locator("#action-title", { hasText: "Отклик отправлен" }).waitFor();
    await sister.goto(`${BASE}/interests`);
    const second = sister.locator("li.icard", { hasText: secondInterest });
    await second.waitFor();
    await second.locator('button:has-text("Принять и начать чат")').click();
    const refused = sister.locator(`li.icard:has-text("${secondInterest}") .icard__error`);
    await refused.waitFor({ timeout: LIVE_TIMEOUT });
    result.legacy.openChatError = (await refused.innerText()).trim();
    expect(result.legacy.openChatError === OPEN_CHAT_LIMIT, "sister: the open-chat limit is explained on the card");
    expect(new URL(sister.url()).pathname === "/interests", "sister: no new chat was opened");
    await refused.scrollIntoViewIfNeeded();
    await shot(sister, "20-open-chat-limit");
    await drain("final");
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
