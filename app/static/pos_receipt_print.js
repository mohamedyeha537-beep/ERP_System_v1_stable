/**
 * طباعة أمر التجهيز / الفاتورة من نقطة البيع عبر iframe مخفي.
 */
(function (global) {
  "use strict";

  function toast(msg, tone) {
    var el = document.createElement("div");
    el.setAttribute("role", "status");
    var bg = tone === "ok" ? "#ecfdf5" : "#fef2f2";
    var border = tone === "ok" ? "#86efac" : "#fecaca";
    var color = tone === "ok" ? "#166534" : "#991b1b";
    el.style.cssText =
      "position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);z-index:10050;" +
      "max-width:92vw;padding:0.65rem 1rem;border-radius:10px;font-size:0.88rem;" +
      "box-shadow:0 8px 24px rgba(0,0,0,0.15);background:" +
      bg +
      ";border:1px solid " +
      border +
      ";color:" +
      color;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () {
      el.remove();
    }, tone === "ok" ? 5000 : 9000);
  }

  function financialDocQuery(docKind) {
    // invoice = فاتورة نهائية بعد إقفال الفاتورة · receipt = إيصال قبض لأي دفعة
    if (docKind === "invoice" || docKind === "final") return "doc=invoice";
    if (docKind === "receipt" || docKind === "rcp") return "doc=receipt";
    return "";
  }

  function printPath(saleId, autoprint, docKind) {
    var ap = autoprint !== false ? "1" : "0";
    var kind = docKind || "prebill";
    if (kind === "prebill" || kind === "kitchen") {
      return (
        "/pos/prebill/" +
        encodeURIComponent(String(saleId)) +
        "?autoprint=" +
        ap +
        "&embed=1"
      );
    }
    var q = financialDocQuery(kind);
    return (
      "/pos/receipt/" +
      encodeURIComponent(String(saleId)) +
      "?autoprint=" +
      ap +
      "&embed=1" +
      (q ? "&" + q : "")
    );
  }

  function previewPath(saleId, autoprint, docKind) {
    var ap = autoprint !== false ? "1" : "0";
    var kind = docKind || "prebill";
    if (kind === "prebill" || kind === "kitchen") {
      return (
        "/pos/prebill/" +
        encodeURIComponent(String(saleId)) +
        "?autoprint=" +
        ap
      );
    }
    var q = financialDocQuery(kind);
    return (
      "/pos/receipt/" +
      encodeURIComponent(String(saleId)) +
      "?autoprint=" +
      ap +
      (q ? "&" + q : "")
    );
  }

  function printViaIframe(path, saleId) {
    return new Promise(function (resolve) {
      var done = false;
      var iframe = document.createElement("iframe");
      iframe.setAttribute("title", "طباعة");
      iframe.style.cssText =
        "position:fixed;width:320px;height:720px;right:0;bottom:0;" +
        "border:0;opacity:0.01;pointer-events:none;z-index:-1";

      function finish(payload) {
        if (done) return;
        done = true;
        window.removeEventListener("message", onMsg);
        clearTimeout(timer);
        setTimeout(function () {
          if (iframe.parentNode) iframe.parentNode.removeChild(iframe);
        }, 400);
        resolve(payload || { ok: false, error: "تعذّرت الطباعة" });
      }

      function onMsg(ev) {
        if (!ev.data || ev.data.type !== "pos-receipt-print") return;
        if (String(ev.data.saleId) !== String(saleId)) return;
        finish(ev.data);
      }

      var timer = setTimeout(function () {
        finish({ ok: false, error: "انتهت مهلة الطباعة — تحقق من وكيل الطباعة" });
      }, 50000);

      window.addEventListener("message", onMsg);
      iframe.src = path;
      document.body.appendChild(iframe);
    });
  }

  global.posReceiptPrint = async function (saleId, autoprint, docKind) {
    if (!saleId) return false;
    var kind = docKind || "prebill";
    toast("جاري تجهيز الطباعة…", "ok");
    var path = printPath(saleId, autoprint, kind);
    var out = await printViaIframe(path, saleId);
    if (out.ok) {
      if (out.message !== "") {
        toast(out.message || "تم إرسال الطباعة للطابعة", "ok");
      }
      return true;
    }
    if (out.needsBrowserPrint) {
      toast("الطباعة المباشرة غير مفعّلة — سيتم فتح نافذة الطباعة الآن", "ok");
      window.open(
        previewPath(saleId, true, kind),
        "pos_receipt_print_" + saleId,
        "noopener,width=480,height=720"
      );
      return true;
    }
    toast(out.error || "تعذّرت الطباعة", "err");
    return false;
  };

  global.posReceiptPreview = function (saleId, docKind) {
    if (!saleId) return;
    var kind = docKind || "prebill";
    window.open(
      previewPath(saleId, false, kind),
      "pos_receipt_preview_" + saleId,
      "noopener,width=480,height=720"
    );
  };

  global.posReceiptWhatsApp = async function (saleId, phone) {
    if (!saleId) return false;
    phone = (phone || "").trim();
    if (!phone) {
      toast("أدخل رقم واتساب أولاً", "err");
      return false;
    }
    toast("جاري إرسال الفاتورة على واتساب…", "ok");
    var iframe = document.createElement("iframe");
    iframe.setAttribute("title", "فاتورة واتساب");
    iframe.style.cssText =
      "position:fixed;width:360px;height:800px;right:0;bottom:0;" +
      "border:0;opacity:0.01;pointer-events:none;z-index:-1";
    document.body.appendChild(iframe);
    var loaded = new Promise(function (resolve) {
      iframe.onload = function () { resolve(); };
      iframe.src = "/pos/receipt/" + encodeURIComponent(String(saleId)) + "?embed=1";
    });
    try {
      await loaded;
      await new Promise(function (r) { setTimeout(r, 900); });
      var doc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
      var image = "";
      if (doc && typeof html2canvas === "function") {
        var root = doc.getElementById("receipt-capture-root");
        if (root) {
          var canvas = await html2canvas(root, {
            scale: 2,
            useCORS: true,
            backgroundColor: "#ffffff",
            logging: false,
          });
          image = canvas.toDataURL("image/png");
        }
      }
      var res = await fetch("/pos/receipt/" + encodeURIComponent(String(saleId)) + "/send-whatsapp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ image_png_b64: image || "", phone: phone }),
      });
      var data = {};
      try { data = await res.json(); } catch (e) { data = {}; }
      if (!res.ok || !data.ok) {
        toast((data && data.error) || "تعذّر الإرسال على واتساب", "err");
        return false;
      }
      toast("تم الإرسال على واتساب" + (data.phone ? " → " + data.phone : ""), "ok");
      return true;
    } catch (e) {
      toast((e && e.message) || "تعذّر الإرسال على واتساب", "err");
      return false;
    } finally {
      setTimeout(function () {
        if (iframe.parentNode) iframe.parentNode.removeChild(iframe);
      }, 400);
    }
  };
})(window);
