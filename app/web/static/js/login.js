// Login through the Telegram bot: open the deep link, then poll /auth/poll every 2 s
// until the bot confirms (or 10 minutes pass), and follow the redirect.
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
  var startedAt = null;
  var timer = null;
  var busy = false;

  function stop(message) {
    clearTimeout(timer);
    timer = null;
    startedAt = null;
    status.hidden = true;
    retry.hidden = false;
    if (message) retry.querySelector("p").textContent = message;
    button.setAttribute("aria-disabled", "true");
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, INTERVAL);
  }

  function poll() {
    if (busy) return;
    if (startedAt && Date.now() - startedAt > TIMEOUT) {
      stop();
      return;
    }
    busy = true;
    fetch(pollUrl, { headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.status === "ok") {
          statusText.textContent = "Вход выполнен, открываем сайт…";
          window.location.assign(data.redirect || "/catalog");
        } else if (data.status === "pending") {
          if (startedAt) schedule();
        } else if (data.status === "failed") {
          stop(data.message);
        } else {
          stop();
        }
      })
      .catch(function () {
        statusText.textContent = "Нет связи с сервером, пробуем снова…";
        if (startedAt) schedule();
      })
      .then(function () { busy = false; });
  }

  function start() {
    if (!startedAt) startedAt = Date.now();
    status.hidden = false;
    retry.hidden = true;
    schedule();
  }

  button.addEventListener("click", function (event) {
    if (button.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      return;
    }
    start();
  });

  // Returning from Telegram: check right away instead of waiting for the next tick.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && startedAt) poll();
  });

  // The page was reloaded while a login was in progress: keep waiting for the same token.
  if (root.dataset.resume === "1") start();
})();
