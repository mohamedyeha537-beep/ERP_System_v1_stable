(function () {
  "use strict";

  var STORAGE_KEY = "pos_web_chat_token";
  var REVISION_KEY = "pos_web_chat_cfg_rev";
  var POLL_MS = 3000;
  var RECEIPT_MARKER = "[[upload_receipt]]";

  var messagesEl = document.getElementById("chat-messages");
  var formEl = document.getElementById("chat-form");
  var inputEl = document.getElementById("chat-input");
  var sendBtn = document.getElementById("chat-send");
  var profileBtn = document.getElementById("btn-profile");
  var profileDialog = document.getElementById("profile-dialog");
  var profileForm = document.getElementById("profile-form");
  var profileName = document.getElementById("profile-name");
  var profilePhone = document.getElementById("profile-phone");
  var profileCancel = document.getElementById("profile-cancel");
  var fileInput = document.getElementById("chat-file");
  var newBtn = document.getElementById("chat-new");
  var ratingWrap = document.getElementById("chat-rating-wrap");
  var starsEl = document.getElementById("chat-stars");
  var receiptWrap = document.getElementById("chat-receipt-wrap");
  var receiptBtn = document.getElementById("chat-receipt-btn");

  if (!messagesEl || !formEl) return;

  var token = localStorage.getItem(STORAGE_KEY) || "";
  var lastId = 0;
  var polling = false;
  var pollTimer = null;
  var ratingSubmitting = false;
  var receiptUploading = false;
  var starsBuilt = false;
  var botName = (typeof window.__CHAT_BOT_NAME__ === "string" && window.__CHAT_BOT_NAME__) || "مساعد";
  var sessionState = { order_phase: "browse", feedback_rating: null };

  function esc(text) {
    var d = document.createElement("div");
    d.textContent = text || "";
    return d.innerHTML;
  }

  function senderLabel(msg) {
    if (msg.direction === "inbound") return "";
    if (msg.sender_type === "agent") return "موظف";
    if (msg.sender_type === "bot") return botName;
    return "";
  }

  function formatBody(text) {
    return esc(text || "").replace(/\n/g, "<br>");
  }

  function parseReceiptBody(body) {
    var raw = body || "";
    if (raw.indexOf(RECEIPT_MARKER) === -1) {
      return { text: raw, showReceipt: false };
    }
    return {
      text: raw.split(RECEIPT_MARKER).join("").trim(),
      showReceipt: true,
    };
  }

  function receiptButtonHtml() {
    return (
      '<button type="button" class="guest-chat-receipt-btn" data-action="upload-receipt">' +
      "📷 رفع إيصال التحويل</button>"
    );
  }

  function renderMessage(msg) {
    if (!msg || !msg.id) return;
    if (document.querySelector('[data-msg-id="' + msg.id + '"]')) return;
    var div = document.createElement("div");
    div.className =
      "guest-chat-bubble " + (msg.direction === "inbound" ? "inbound" : "outbound");
    div.setAttribute("data-msg-id", String(msg.id));
    var label = senderLabel(msg);
    var html = "";
    var parsed = parseReceiptBody(msg.body || "");
    if (parsed.text.trim()) {
      html += '<div class="guest-chat-text">' + formatBody(parsed.text) + "</div>";
    }
    if (parsed.showReceipt && msg.direction !== "inbound") {
      html += receiptButtonHtml();
    }
    if (msg.image_url) {
      html +=
        '<img class="guest-chat-img" src="' +
        esc(msg.image_url) +
        '" alt="" loading="lazy" />';
    }
    if (label) {
      html += '<span class="meta">' + esc(label) + "</span>";
    }
    div.innerHTML = html;
    messagesEl.appendChild(div);
    if (msg.id > lastId) lastId = msg.id;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderAll(list) {
    (list || []).forEach(renderMessage);
  }

  function setReceiptControlsDisabled(disabled) {
    receiptUploading = disabled;
    if (receiptBtn) receiptBtn.disabled = disabled;
    messagesEl.querySelectorAll('[data-action="upload-receipt"]').forEach(function (btn) {
      btn.disabled = disabled;
    });
  }

  function openReceiptPicker() {
    if (receiptUploading) return;
    if (sessionState.order_phase !== "await_receipt") {
      alert("رفع الإيصال متاح فقط بعد اختيار الدفع المصرفي وإتمام التحويل.");
      return;
    }
    if (fileInput) fileInput.click();
  }

  function updateSession(session) {
    if (!session) return;
    if (session.bot_name) botName = session.bot_name;
    sessionState = session;
    updateRatingWidget();
    updateReceiptWidget();
    updateInputPlaceholder();
  }

  function shouldShowRating() {
    return (
      sessionState.order_phase === "feedback" &&
      !sessionState.feedback_rating &&
      !ratingSubmitting
    );
  }

  function shouldShowReceipt() {
    return sessionState.order_phase === "await_receipt" && !receiptUploading;
  }

  function setStarHighlight(until) {
    if (!starsEl) return;
    var buttons = starsEl.querySelectorAll(".guest-chat-star");
    buttons.forEach(function (btn) {
      var rating = parseInt(btn.getAttribute("data-rating"), 10);
      var active = until > 0 && rating <= until;
      btn.classList.toggle("is-active", active);
      btn.textContent = active ? "★" : "☆";
    });
  }

  function buildStars() {
    if (!starsEl || starsBuilt) return;
    starsBuilt = true;
    starsEl.innerHTML = "";
    for (var i = 1; i <= 5; i++) {
      (function (rating) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "guest-chat-star";
        btn.setAttribute("data-rating", String(rating));
        btn.setAttribute("aria-label", "تقييم " + rating + " من 5");
        btn.textContent = "☆";
        btn.addEventListener("mouseenter", function () {
          if (ratingSubmitting) return;
          setStarHighlight(rating);
        });
        btn.addEventListener("focus", function () {
          if (ratingSubmitting) return;
          setStarHighlight(rating);
        });
        btn.addEventListener("click", function () {
          if (ratingSubmitting) return;
          submitRating(rating);
        });
        starsEl.appendChild(btn);
      })(i);
    }
    starsEl.addEventListener("mouseleave", function () {
      if (ratingSubmitting) return;
      setStarHighlight(0);
    });
  }

  function updateRatingWidget() {
    if (!ratingWrap) return;
    buildStars();
    ratingWrap.hidden = !shouldShowRating();
    if (!shouldShowRating()) setStarHighlight(0);
  }

  function updateReceiptWidget() {
    if (!receiptWrap) return;
    receiptWrap.hidden = !shouldShowReceipt();
  }

  function updateInputPlaceholder() {
    if (!inputEl) return;
    if (sessionState.order_phase === "await_receipt") {
      inputEl.placeholder = "أو اكتب «تم التحويل» بعد الدفع…";
      return;
    }
    if (sessionState.feedback_rating) {
      inputEl.placeholder = "تعليق اختياري عن تجربتك…";
      return;
    }
    inputEl.placeholder = "اكتب رسالتك…";
  }

  async function submitRating(rating) {
    ratingSubmitting = true;
    setStarHighlight(rating);
    if (starsEl) {
      starsEl.querySelectorAll(".guest-chat-star").forEach(function (btn) {
        btn.disabled = true;
      });
    }
    try {
      await sendMessage(String(rating), { skipFocus: true });
    } catch (err) {
      ratingSubmitting = false;
      if (starsEl) {
        starsEl.querySelectorAll(".guest-chat-star").forEach(function (btn) {
          btn.disabled = false;
        });
      }
      setStarHighlight(0);
      alert(err.message || "تعذّر إرسال التقييم");
    } finally {
      ratingSubmitting = false;
      updateRatingWidget();
    }
  }

  async function api(path, opts) {
    var res = await fetch(path, opts || {});
    var data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }
    if (!res.ok) {
      var err = (data && data.detail) || "حدث خطأ";
      throw new Error(typeof err === "string" ? err : JSON.stringify(err));
    }
    return data;
  }

  async function ensureSession() {
    if (token) {
      try {
        var check = await api("/api/chat/session?token=" + encodeURIComponent(token));
        if (check.session) {
          var serverRev = String(check.session.config_revision || "");
          var localRev = localStorage.getItem(REVISION_KEY) || "";
          if (serverRev && serverRev !== localRev) {
            token = "";
            localStorage.removeItem(STORAGE_KEY);
            await createFreshSession(false);
            return;
          }
          if (profileName) profileName.value = check.session.guest_name || "";
          if (profilePhone) profilePhone.value = check.session.guest_phone || "";
          updateSession(check.session);
          if (serverRev) localStorage.setItem(REVISION_KEY, serverRev);
          return;
        }
      } catch (e) {
        token = "";
        localStorage.removeItem(STORAGE_KEY);
        localStorage.removeItem(REVISION_KEY);
      }
    }
    await createFreshSession(false);
  }

  async function createFreshSession(copyProfile) {
    var keepName = copyProfile && profileName ? profileName.value.trim() : "";
    var keepPhone = copyProfile && profilePhone ? profilePhone.value.trim() : "";
    var created = await api("/api/chat/session", { method: "POST" });
    token = created.session.token;
    localStorage.setItem(STORAGE_KEY, token);
    if (created.session && created.session.config_revision) {
      localStorage.setItem(REVISION_KEY, String(created.session.config_revision));
    }
    lastId = 0;
    ratingSubmitting = false;
    receiptUploading = false;
    starsBuilt = false;
    if (starsEl) starsEl.innerHTML = "";
    messagesEl.innerHTML = "";
    renderAll(created.messages || []);
    updateSession(created.session);
    if (keepName || keepPhone) {
      var data = await api("/api/chat/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: token,
          name: keepName,
          phone: keepPhone,
        }),
      });
      if (profileName) profileName.value = (data.session && data.session.guest_name) || keepName;
      if (profilePhone) profilePhone.value = (data.session && data.session.guest_phone) || keepPhone;
      renderAll(data.messages || []);
      updateSession(data.session);
    }
  }

  async function startNewConversation() {
    var hadActivity = lastId > 0;
    if (hadActivity) {
      var ok = confirm(
        "بدء محادثة جديدة؟\n\n" +
          "ستبدأ من الصفر لطلب جديد. سجل المحادثة الحالية يبقى محفوظاً لدينا."
      );
      if (!ok) return;
    }
    if (newBtn) newBtn.disabled = true;
    if (sendBtn) sendBtn.disabled = true;
    if (receiptBtn) receiptBtn.disabled = true;
    try {
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem(REVISION_KEY);
      token = "";
      await createFreshSession(true);
      if (inputEl) {
        inputEl.value = "";
        inputEl.style.height = "auto";
        inputEl.focus();
      }
    } catch (err) {
      alert(err.message || "تعذّر بدء محادثة جديدة");
    } finally {
      if (newBtn) newBtn.disabled = false;
      if (sendBtn) sendBtn.disabled = false;
      updateReceiptWidget();
    }
  }

  async function loadHistory() {
    if (!token) return;
    var data = await api(
      "/api/chat/messages?token=" +
        encodeURIComponent(token) +
        "&since_id=0"
    );
    renderAll(data.messages || []);
    updateSession(data.session);
  }

  async function poll() {
    if (!token || polling) return;
    polling = true;
    try {
      var data = await api(
        "/api/chat/messages?token=" +
          encodeURIComponent(token) +
          "&since_id=" +
          lastId
      );
      renderAll(data.messages || []);
      updateSession(data.session);
    } catch (e) {
      /* ignore transient poll errors */
    } finally {
      polling = false;
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, POLL_MS);
  }

  async function uploadReceipt(file, caption) {
    setReceiptControlsDisabled(true);
    if (sendBtn) sendBtn.disabled = true;
    try {
      var fd = new FormData();
      fd.append("token", token);
      fd.append("file", file);
      fd.append("caption", caption || "📎 إيصال تحويل");
      var res = await fetch("/api/chat/upload-receipt", { method: "POST", body: fd });
      var data = null;
      try {
        data = await res.json();
      } catch (e) {
        data = {};
      }
      if (!res.ok) {
        var err = (data && data.detail) || "تعذّر رفع الإيصال";
        throw new Error(typeof err === "string" ? err : JSON.stringify(err));
      }
      renderAll(data.messages || []);
      updateSession(data.session);
    } finally {
      setReceiptControlsDisabled(false);
      if (sendBtn) sendBtn.disabled = false;
      updateReceiptWidget();
      if (inputEl) inputEl.focus();
    }
  }

  async function sendMessage(text, opts) {
    opts = opts || {};
    if (sendBtn) sendBtn.disabled = true;
    try {
      var data = await api("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token, text: text }),
      });
      renderAll(data.messages || []);
      updateSession(data.session);
    } finally {
      if (sendBtn) sendBtn.disabled = false;
      if (!opts.skipFocus && inputEl) inputEl.focus();
    }
  }

  formEl.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = (inputEl.value || "").trim();
    if (!text) return;
    inputEl.value = "";
    inputEl.style.height = "auto";
    sendMessage(text).catch(function (err) {
      alert(err.message || "تعذّر الإرسال");
    });
  });

  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 128) + "px";
  });

  inputEl.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      formEl.requestSubmit();
    }
  });

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (!file) return;
      uploadReceipt(file, "").catch(function (err) {
        alert(err.message || "تعذّر رفع الإيصال");
      });
    });
  }

  if (receiptBtn) {
    receiptBtn.addEventListener("click", openReceiptPicker);
  }

  messagesEl.addEventListener("click", function (ev) {
    var btn = ev.target.closest('[data-action="upload-receipt"]');
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    openReceiptPicker();
  });

  if (newBtn) {
    newBtn.addEventListener("click", function () {
      startNewConversation();
    });
  }

  if (profileBtn && profileDialog) {
    profileBtn.addEventListener("click", function () {
      profileDialog.showModal();
    });
  }
  if (profileCancel) {
    profileCancel.addEventListener("click", function () {
      profileDialog.close();
    });
  }
  if (profileForm) {
    profileForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var phoneVal = profilePhone ? profilePhone.value.trim() : "";
      if (!phoneVal) {
        alert("أدخل رقم الهاتف لإتمام الطلب");
        if (profilePhone) profilePhone.focus();
        return;
      }
      api("/api/chat/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: token,
          name: profileName ? profileName.value.trim() : "",
          phone: phoneVal,
        }),
      })
        .then(function (data) {
          profileDialog.close();
          renderAll(data.messages || []);
          updateSession(data.session);
        })
        .catch(function (err) {
          alert(err.message || "تعذّر الحفظ");
        });
    });
  }

  ensureSession()
    .then(function () {
      return loadHistory();
    })
    .then(startPolling)
    .catch(function (err) {
      messagesEl.innerHTML =
        '<p style="color:#b91c1c;text-align:center">' +
        esc(err.message || "تعذّر بدء المحادثة") +
        "</p>";
    });
})();
