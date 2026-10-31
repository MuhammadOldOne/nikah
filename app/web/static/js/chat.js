// Chat page: live messages (SSE events from app.js -> fetch the HTML partial after the last id),
// sending with Enter on a physical keyboard (Shift+Enter for a new line; on touch screens Enter is
// a new line and the button sends), loading older messages, marking as read, announcing the
// partner's new messages to screen readers.
(function () {
  "use strict";

  var chat = document.getElementById("chat");
  if (!chat) return;
  var list = document.getElementById("messages");
  var form = document.getElementById("composer");
  var textarea = form ? form.querySelector("textarea") : null;
  var messagesUrl = chat.dataset.messagesUrl;
  var lastId = parseInt(chat.dataset.lastId || "0", 10);
  var announcer = document.getElementById("chat-announcer");
  var physicalKeyboard = window.matchMedia("(pointer: fine)");
  var fetching = false;
  var again = false;
  var PARTIAL = { "HX-Request": "true" };

  function nearBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight < 120;
  }
  function scrollToBottom() {
    list.scrollTop = list.scrollHeight;
  }

  // Append server-rendered <li class="msg"> items, skipping ones already shown.
  function append(html, forceScroll) {
    var stick = forceScroll || nearBottom();
    var template = document.createElement("template");
    template.innerHTML = html;
    var added = false;
    var heard = [];
    template.content.querySelectorAll("li.msg").forEach(function (item) {
      var id = parseInt(item.dataset.id, 10);
      if (document.getElementById("msg-" + id)) return;
      if (!item.classList.contains("msg--mine")) {
        heard.push(chat.dataset.partner + ": " + item.querySelector(".msg__bubble").textContent);
      }
      list.appendChild(item);
      lastId = Math.max(lastId, id);
      added = true;
    });
    if (heard.length && announcer) announcer.textContent = "Новое сообщение. " + heard.join(" ");
    if (added) {
      var empty = document.getElementById("messages-empty");
      if (empty) empty.remove();
      if (form) form.elements.after_id.value = String(lastId);
      if (stick) scrollToBottom();
    }
    return added;
  }

  function fetchNew() {
    if (fetching) { again = true; return; }
    fetching = true;
    var visible = document.visibilityState === "visible";
    fetch(messagesUrl + "?after_id=" + lastId + (visible ? "&read=1" : ""), {
      headers: PARTIAL, credentials: "same-origin", cache: "no-store"
    })
      .then(function (response) {
        var redirect = response.headers.get("HX-Redirect");
        if (redirect) { window.location.assign(redirect); return ""; }
        return response.ok ? response.text() : "";
      })
      .then(function (html) {
        if (html) append(html, false);
        if (visible) window.Istina.refreshBadges();
      })
      .catch(function () { /* the next event or reconnect retries */ })
      .then(function () {
        fetching = false;
        if (again) { again = false; fetchNew(); }
      });
  }

  document.addEventListener("live:ready", fetchNew);
  document.addEventListener("live:resync", fetchNew);
  document.addEventListener("live:message", function (event) {
    var id = event.detail && event.detail.id;
    if (!id || !document.getElementById("msg-" + id)) fetchNew();
  });
  document.addEventListener("live:closed", function () { window.location.reload(); });
  // A participant linked a wali: from now on the conversation is copied to them.
  document.addEventListener("live:wali", function () {
    var notice = document.getElementById("wali-notice");
    if (notice) notice.hidden = false;
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") fetchNew();
  });

  // --- Older messages ---
  var older = document.getElementById("load-older");
  if (older) {
    older.addEventListener("click", function () {
      older.disabled = true;
      fetch(messagesUrl + "?before_id=" + older.dataset.beforeId, { headers: PARTIAL, credentials: "same-origin" })
        .then(function (response) {
          var more = response.headers.get("X-More") === "1";
          return response.text().then(function (html) { return { html: html, more: more }; });
        })
        .then(function (result) {
          var template = document.createElement("template");
          template.innerHTML = result.html;
          var items = template.content.querySelectorAll("li.msg");
          var anchor = older.parentNode.nextSibling;
          var before = list.scrollHeight;
          items.forEach(function (item) {
            if (!document.getElementById(item.id)) list.insertBefore(item, anchor);
          });
          list.scrollTop += list.scrollHeight - before;
          if (items.length && result.more) {
            older.dataset.beforeId = items[0].dataset.id;
            older.disabled = false;
          } else {
            older.parentNode.remove();
          }
        })
        .catch(function () { older.disabled = false; });
    });
  }

  // --- Sending ---
  function autosize() {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight + 2, 160) + "px";
  }

  if (form && textarea) {
    var sending = false;
    textarea.addEventListener("input", autosize);
    textarea.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && physicalKeyboard.matches) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (sending || !textarea.value.trim()) return;
      sending = true;
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "HX-Request": "true", "X-CSRF-Token": window.Istina.csrfToken },
        credentials: "same-origin"
      })
        .then(function (response) {
          var redirect = response.headers.get("HX-Redirect");
          if (redirect) { window.location.assign(redirect); return; }
          if (response.status === 201) {
            return response.text().then(function (html) {
              append(html, true);
              textarea.value = "";
              textarea.dispatchEvent(new Event("input"));
            });
          }
          if (!window.Istina.toastFromHeader(response.headers.get("HX-Trigger"))) {
            window.Istina.toast("Сообщение не отправлено. Попробуйте ещё раз.", "error");
          }
          if (response.status === 403 || response.status === 404) window.location.reload();
        })
        .catch(function () {
          window.Istina.toast("Нет соединения. Сообщение не отправлено.", "error");
        })
        .then(function () {
          sending = false;
          button.disabled = false;
          textarea.focus();
        });
    });
  }

  scrollToBottom();
})();
