// Chat page: live messages (SSE events from app.js -> fetch the HTML partial after the last id;
// the witness's messages arrive the same way), sending with Enter on a physical keyboard
// (Shift+Enter for a new line; on touch screens Enter is a new line and the button sends), errors
// and the contact-filter notice next to the composer, loading older messages, marking as read,
// announcing new messages of the partner and the witness to screen readers.
(function () {
  "use strict";

  var chat = document.getElementById("chat");
  if (!chat) return;
  var list = document.getElementById("messages");
  var form = document.getElementById("composer");
  var textarea = form ? form.querySelector("textarea") : null;
  var status = document.getElementById("composer-status");
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
        var author = item.dataset.author || chat.dataset.partner;
        heard.push(author + ": " + item.querySelector(".msg__bubble").textContent);
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
  // A notice next to the composer: an error (kept until the next attempt) or the filter notice.
  function showStatus(message, isError) {
    if (!status) return false;
    status.textContent = message || "";
    status.hidden = !message;
    status.classList.toggle("is-error", Boolean(isError));
    status.setAttribute("role", isError ? "alert" : "status");
    if (textarea) {
      if (isError && message) {
        textarea.setAttribute("aria-invalid", "true");
        textarea.setAttribute("aria-describedby", "composer-status");
      } else {
        textarea.removeAttribute("aria-invalid");
        textarea.removeAttribute("aria-describedby");
      }
    }
    return Boolean(message);
  }

  function triggerMessage(header) {
    if (!header) return "";
    try {
      var data = JSON.parse(header);
      return data && data.toast ? data.toast.message : "";
    } catch (e) {
      return "";
    }
  }

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
          var notice = triggerMessage(response.headers.get("HX-Trigger"));
          if (response.status === 201) {
            return response.text().then(function (html) {
              append(html, true);
              textarea.value = "";
              textarea.dispatchEvent(new Event("input"));
              showStatus(notice, false);  // e.g. contacts hidden by the filter
            });
          }
          if (!showStatus(notice || "Сообщение не отправлено. Попробуйте ещё раз.", true)) {
            window.Istina.toast(notice || "Сообщение не отправлено. Попробуйте ещё раз.", "error");
          }
          if (response.status === 403 || response.status === 404) window.location.reload();
        })
        .catch(function () {
          if (!showStatus("Нет соединения. Сообщение не отправлено.", true)) {
            window.Istina.toast("Нет соединения. Сообщение не отправлено.", "error");
          }
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
