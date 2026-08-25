/**
 * تنبيه صوتي لطلبات محادثة الويب — تظهر في قسم التوصيل/الاستلام.
 */
(function (global) {
  "use strict";

  var knownIds = new Set();
  var initialized = false;
  var STORAGE_KEY = "pos_web_chat_known_ids";
  var pollTimer = null;

  function loadKnownFromStorage() {
    try {
      var raw = sessionStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      raw.split(",").forEach(function (p) {
        var n = parseInt(p, 10);
        if (n > 0) knownIds.add(n);
      });
    } catch (e) { /* ignore */ }
  }

  function saveKnownToStorage() {
    try {
      sessionStorage.setItem(STORAGE_KEY, Array.from(knownIds).join(","));
    } catch (e) { /* ignore */ }
  }

  function readAllSaleIds() {
    var ids = [];
    document.querySelectorAll("[data-web-chat-sale-ids]").forEach(function (el) {
      (el.getAttribute("data-web-chat-sale-ids") || "")
        .split(",")
        .forEach(function (p) {
          var n = parseInt(p, 10);
          if (n > 0) ids.push(n);
        });
    });
    return ids;
  }

  function playNewOrderSound() {
    if (global.KdsAlertSound && global.KdsAlertSound.play) {
      global.KdsAlertSound.play("kitchen", 92).catch(function () { /* ignore */ });
      setTimeout(function () {
        global.KdsAlertSound.play("chime", 85).catch(function () { /* ignore */ });
      }, 420);
    }
  }

  function syncKnownIds() {
    var ids = readAllSaleIds();
    if (!initialized) {
      loadKnownFromStorage();
      if (knownIds.size === 0) {
        ids.forEach(function (id) { knownIds.add(id); });
      } else {
        ids.forEach(function (id) {
          if (!knownIds.has(id)) knownIds.add(id);
        });
      }
      initialized = true;
      saveKnownToStorage();
      return;
    }
    var hasNew = false;
    ids.forEach(function (id) {
      if (!knownIds.has(id)) {
        knownIds.add(id);
        hasNew = true;
      }
    });
    Array.from(knownIds).forEach(function (id) {
      if (ids.indexOf(id) === -1) knownIds.delete(id);
    });
    saveKnownToStorage();
    if (hasNew) playNewOrderSound();
  }

  function unlockAudioOnce() {
    if (global.KdsAlertSound && global.KdsAlertSound.unlock) {
      global.KdsAlertSound.unlock();
    }
  }

  function bindOpenButtons(root) {
    if (global.initPosOpenOrderLinks) global.initPosOpenOrderLinks();
    if (!root) return;
    root.querySelectorAll("[data-open-order]").forEach(function (btn) {
      if (btn._posOpenBound) return;
      btn._posOpenBound = true;
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        var sid = btn.getAttribute("data-open-order");
        if (!sid) return;
        var f = document.createElement("form");
        f.method = "post";
        f.action = "/pos/open-order/" + sid;
        document.body.appendChild(f);
        f.submit();
      });
    });
  }

  function pollChatRails() {
    if (document.hidden) return;
    if (!document.getElementById("pos-chat-delivery-incoming")) return;
    fetch("/pos/web-chat-rails", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) return null;
        return r.text();
      })
      .then(function (html) {
        if (!html) return;
        var wrap = document.createElement("div");
        wrap.innerHTML = html.trim();
        ["pos-chat-delivery-incoming", "pos-chat-pickup-incoming"].forEach(function (id) {
          var incoming = wrap.querySelector("#" + id);
          var target = document.getElementById(id);
          if (incoming && target) {
            target.replaceWith(incoming);
            bindOpenButtons(incoming);
          }
        });
        syncKnownIds();
      })
      .catch(function () { /* ignore */ });
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(pollChatRails, 20000);
  }

  function boot() {
    bindOpenButtons(document);
    syncKnownIds();
    startPoll();
    document.body.addEventListener("click", unlockAudioOnce, { once: true });
    document.body.addEventListener("keydown", unlockAudioOnce, { once: true });
  }

  document.body.addEventListener("htmx:afterSwap", function (ev) {
    var t = ev.detail && ev.detail.target;
    if (!t) return;
    if (
      t.id === "pos-order-hub"
      || t.id === "pos-chat-delivery-incoming"
      || t.id === "pos-chat-pickup-incoming"
    ) {
      bindOpenButtons(t);
      syncKnownIds();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(typeof window !== "undefined" ? window : this);
