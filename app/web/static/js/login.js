// Login through the Telegram bot: poll /auth/poll every 2 s from page load until the bot confirms
// (or the token's 10 minutes pass), and follow the redirect. Polling does not wait for a click on
// the button: the link may be opened on a phone («Telegram только на телефоне?»).
(function () {
  "use strict";

  var root = document.getElementById("login");
  if (!root) return;
  var button = document.getElementById("tg-login");
  var status = document.getElementById("login-status");
  var statusText = status.querySelector("[data-text]");
  var retry = document.getElementById("login-retry");
  var pollUrl = root.dataset.pollUrl;
  var INTERVAL = 2000;
  var TIMEOUT = 10 * 60 * 1000;
  var startedAt = Date.now();
  var stopped = false;
  var timer = null;
  var busy = false;

  function stop(message) {
    clearTimeout(timer);
    timer = null;
    stopped = true;
    status.hidden = true;
    retry.hidden = false;
    if (message) retry.querySelector("p").textContent = message;
    button.setAttribute("aria-disabled", "true");
  }

  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(poll, INTERVAL);
  }

  function poll() {
    if (busy || stopped) return;
    if (Date.now() - startedAt > TIMEOUT) {
      stop();
      return;
    }
    busy = true;
    fetch(pollUrl, { headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.status === "ok") {
          status.hidden = false;
          statusText.textContent = "Вход выполнен, открываем сайт…";
          window.location.assign(data.redirect || "/catalog");
        } else if (data.status === "pending") {
          schedule();
        } else if (data.status === "failed") {
          stop(data.message);
        } else {
          stop();
        }
      })
      .catch(function () {
        if (!status.hidden) statusText.textContent = "Нет связи с сервером, пробуем снова…";
        schedule();
      })
      .then(function () { busy = false; });
  }

  function showWaiting() {
    if (stopped) return;
    status.hidden = false;
    retry.hidden = true;
  }

  button.addEventListener("click", function (event) {
    if (button.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      return;
    }
    showWaiting();
  });

  // Returning from Telegram: check right away instead of waiting for the next tick.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") poll();
  });

  // The page was reloaded while a login was in progress: show that we are still waiting.
  if (root.dataset.resume === "1") showWaiting();
  schedule();
})();
