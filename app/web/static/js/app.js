// Site-wide behaviour: toasts, character counters, copy buttons, confirmations,
// live updates (Server-Sent Events) and navigation badges. No inline scripts (CSP).
(function () {
  "use strict";

  var body = document.body;
  var csrfToken = body.dataset.csrf || "";

  function toast(message, level) {
    var box = document.getElementById("toasts");
    if (!box || !message) return;
    var item = document.createElement("div");
    item.className = "toast" + (level === "error" ? " toast--error" : "");
    item.setAttribute("role", level === "error" ? "alert" : "status");
    item.textContent = message;
    box.appendChild(item);
    setTimeout(function () { item.remove(); }, 6000);
  }

  // Toast from an "HX-Trigger: {"toast": {...}}" response header (also used by chat.js).
  function toastFromHeader(header) {
    if (!header) return false;
    try {
      var data = JSON.parse(header);
      if (data && data.toast) {
        toast(data.toast.message, data.toast.level);
        return true;
      }
    } catch (e) { /* not JSON: ignore */ }
    return false;
  }

  // --- Character counters: <textarea data-counter="500" [data-counter-from="400"]> ---
  function setupCounter(field) {
    var max = parseInt(field.dataset.counter, 10);
    if (!max || field.dataset.counterReady) return;
    field.dataset.counterReady = "1";
    var from = parseInt(field.dataset.counterFrom || "0", 10);
    var counter = document.createElement("span");
    counter.className = "counter";
    counter.setAttribute("aria-hidden", "true");
    field.insertAdjacentElement("afterend", counter);
    function update() {
      var length = field.value.length;
      counter.textContent = length + " / " + max;
      counter.hidden = length < from;
      counter.classList.toggle("is-over", length > max);
    }
    field.addEventListener("input", update);
    field.form && field.form.addEventListener("reset", function () { setTimeout(update, 0); });
    update();
  }

  // --- Copy buttons: <button data-copy="#input-id"> ---
  function copyText(button) {
    var input = document.querySelector(button.dataset.copy);
    if (!input) return;
    var label = button.querySelector(".copy-label");
    function done() {
      if (label) {
        var original = label.textContent;
        label.textContent = "Скопировано";
        setTimeout(function () { label.textContent = original; }, 2000);
      } else {
        toast("Ссылка скопирована");
      }
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(input.value).then(done, function () { input.select(); });
    } else {
      input.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* user copies manually */ }
    }
  }

  function init(root) {
    root.querySelectorAll("[data-counter]").forEach(setupCounter);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (button) copyText(button);
  });

  // Plain (non-HTMX) forms with data-confirm ask before submitting.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });

  // Filters are open on wide screens, collapsible on phones.
  document.querySelectorAll("details[data-open-wide]").forEach(function (details) {
    if (window.matchMedia("(min-width: 1000px)").matches) details.open = true;
  });

  // --- HTMX integration ---
  document.addEventListener("toast", function (event) {
    if (event.detail) toast(event.detail.message, event.detail.level);
  });
  document.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail.xhr;
    if (!xhr.getResponseHeader("HX-Trigger")) {
      toast("Не удалось выполнить действие. Обновите страницу и попробуйте ещё раз.", "error");
    }
  });
  document.addEventListener("htmx:sendError", function () {
    toast("Нет соединения с сервером. Проверьте интернет и попробуйте ещё раз.", "error");
  });
  document.addEventListener("htmx:afterSwap", function () { init(document); });

  // --- Navigation badges ---
  var badgeTimer = null;
  function refreshBadges() {
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(function () {
      fetch("/nav/badges", { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (counts) {
          if (!counts) return;
          document.querySelectorAll("[data-badge]").forEach(function (badge) {
            var value = counts[badge.dataset.badge] || 0;
            badge.textContent = value > 99 ? "99+" : String(value);
            badge.hidden = value === 0;
          });
        })
        .catch(function () { /* offline: keep the old numbers */ });
    }, 300);
  }

  // --- Live updates: one EventSource per page (body[data-stream]) ---
  // Events are re-dispatched on document as "live:<name>" for page scripts (chat.js).
  var stream = null;
  function connect() {
    var url = body.dataset.stream;
    if (!url || !window.EventSource) return;
    stream = new EventSource(url);
    ["ready", "resync", "badge", "message", "closed", "wali"].forEach(function (name) {
      stream.addEventListener(name, function (event) {
        var detail = {};
        try { detail = JSON.parse(event.data || "{}"); } catch (e) { /* ignore */ }
        document.dispatchEvent(new CustomEvent("live:" + name, { detail: detail }));
        // "ready" follows every (re)connection: events sent while disconnected were missed.
        if (name === "badge" || name === "resync" || name === "ready") refreshBadges();
      });
    });
  }
  window.addEventListener("pagehide", function () { if (stream) stream.close(); });
  window.addEventListener("pageshow", function (event) { if (event.persisted) connect(); });

  window.Istina = { toast: toast, toastFromHeader: toastFromHeader, refreshBadges: refreshBadges, csrfToken: csrfToken, init: init };

  init(document);
  connect();
})();
