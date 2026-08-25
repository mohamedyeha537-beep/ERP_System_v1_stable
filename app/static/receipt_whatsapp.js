/** إرسال الفاتورة على واتساب (نص كامل من الخادم + رقم اختياري). */
(function () {
  function resolvePhone(btn) {
    if (!btn) return "";
    var phone = (btn.getAttribute("data-phone") || "").trim();
    if (phone) return phone;
    var block = btn.closest(".receipt-wa-block, .pos-print-row, .pos-success-actions, .card");
    if (!block) return "";
    var inp = block.querySelector(".receipt-wa-phone-input");
    return inp ? (inp.value || "").trim() : "";
  }

  window.receiptWhatsAppResolvePhone = resolvePhone;

  async function sendTextOnly(url, onStatus, phone) {
    if (!phone) {
      var msg = "أدخل رقم واتساب أولاً";
      if (onStatus) onStatus(msg, false);
      throw new Error(msg);
    }
    if (onStatus) onStatus("جاري الإرسال على واتساب…", null);
    var res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ phone: phone }),
    });
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || !data.ok) {
      var errMsg = (data && data.error) || ("خطأ (" + res.status + ")");
      if (onStatus) onStatus(errMsg, false);
      throw new Error(errMsg);
    }
    var okMsg = "تم الإرسال على واتساب" + (data.phone ? " → " + data.phone : "");
    if (onStatus) onStatus(okMsg, true);
    return data;
  }

  window.receiptWhatsAppSend = async function (url, onStatus, phone) {
    if (!url) throw new Error("رابط الإرسال غير متاح");
    return sendTextOnly(url, onStatus, phone);
  };

  window.hotelReceiptWhatsApp = async function (bookingId, onStatus, phone) {
    var url = "/admin/hotel/bookings/" + encodeURIComponent(String(bookingId)) + "/receipt/send-whatsapp";
    return sendTextOnly(url, onStatus, phone);
  };

  function bindWhatsAppButtons(root) {
    (root || document).querySelectorAll(".pos-receipt-whatsapp-btn").forEach(function (btn) {
      if (btn._waBound) return;
      btn._waBound = true;
      btn.addEventListener("click", async function () {
        var url = btn.getAttribute("data-send-url");
        var bid = btn.getAttribute("data-booking-id");
        var block = btn.closest(".receipt-wa-block");
        var statusEl = block ? block.querySelector(".receipt-wa-status") : null;
        function setStatus(msg, ok) {
          if (!statusEl) return;
          statusEl.textContent = msg || "";
          statusEl.style.color = ok ? "#15803d" : (ok === false ? "#b91c1c" : "");
        }
        var phone = resolvePhone(btn);
        btn.disabled = true;
        try {
          if (bid) {
            await window.hotelReceiptWhatsApp(bid, setStatus, phone);
          } else if (url) {
            await window.receiptWhatsAppSend(url, setStatus, phone);
          }
        } catch (e) { /* setStatus */ }
        finally { btn.disabled = false; }
      });
    });
  }

  window.initReceiptWhatsAppButtons = bindWhatsAppButtons;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { bindWhatsAppButtons(document); });
  } else {
    bindWhatsAppButtons(document);
  }
})();
