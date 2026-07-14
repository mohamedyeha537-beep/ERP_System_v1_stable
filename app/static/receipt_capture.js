/**
 * التقاط الفاتورة كصورة PNG وإرسالها للطباعة الحرارية عبر الوكيل.
 * العرض يملأ الورقة؛ الهامش الجانبي من padding في CSS فقط.
 * الوكيل (escpos) يوسّط الصورة إن كانت أضيق من عرض الورقة.
 */
(function (global) {
  "use strict";

  function waitForImages(root) {
    var imgs = root.querySelectorAll("img");
    var pending = [];
    imgs.forEach(function (img) {
      if (img.complete && img.naturalWidth) return;
      pending.push(
        new Promise(function (resolve) {
          img.addEventListener("load", resolve, { once: true });
          img.addEventListener("error", resolve, { once: true });
        })
      );
    });
    return Promise.all(pending);
  }

  function captureRoot() {
    return (
      document.getElementById("receipt-capture-root") ||
      document.querySelector(".receipt-capture-root") ||
      document.querySelector(".receipt-page")
    );
  }

  function findReceiptPage(root) {
    if (!root) return null;
    if (
      root.classList &&
      (root.classList.contains("paper-80mm") ||
        root.classList.contains("paper-58mm"))
    ) {
      return root;
    }
    return root.closest ? root.closest(".receipt-page") : null;
  }

  function isThermalPaper(root) {
    var page = findReceiptPage(root);
    if (!page) return false;
    return (
      page.classList.contains("paper-80mm") ||
      page.classList.contains("paper-58mm")
    );
  }

  function thermalFullDots(root) {
    var page = findReceiptPage(root) || root;
    return page.classList.contains("paper-58mm") ? 384 : 576;
  }

  function captureScale() {
    return Math.min(2, Math.max(1.75, global.devicePixelRatio || 1.75));
  }

  /** عرض CSS للالتقاط — يطابق عرض المحتوى الفعلي على الورقة (لا يُصغَّر لاحقاً). */
  function thermalCssWidth(root) {
    var page = findReceiptPage(root);
    if (page && page.classList.contains("paper-58mm")) return 219;
    return 302;
  }

  function measureContentHeight(root) {
    var inner =
      document.getElementById("receipt-capture-root") ||
      root.querySelector(".receipt-capture-root") ||
      root;
    return Math.max(
      inner.scrollHeight,
      inner.offsetHeight,
      Math.ceil(inner.getBoundingClientRect().height),
      root.scrollHeight,
      root.offsetHeight,
      Math.ceil(root.getBoundingClientRect().height)
    );
  }

  function measureCaptureBox(root) {
    var thermal = isThermalPaper(root);
    var h = measureContentHeight(root) + 96;
    if (thermal) {
      return { width: thermalCssWidth(root), height: h };
    }
    var rect = root.getBoundingClientRect();
    var w = Math.max(
      root.scrollWidth,
      root.offsetWidth,
      Math.ceil(rect.width)
    );
    return { width: w, height: h };
  }

  function fixCloneForCapture(clonedDoc, clonedRoot) {
    var el = clonedRoot;
    while (el) {
      el.style.overflow = "visible";
      el.style.maxHeight = "none";
      el.style.height = "auto";
      el = el.parentElement;
    }
    if (clonedDoc.body) {
      clonedDoc.body.style.overflow = "visible";
      clonedDoc.body.style.height = "auto";
      clonedDoc.body.style.margin = "0";
      clonedDoc.body.style.padding = "0";
    }
    if (clonedDoc.documentElement) {
      clonedDoc.documentElement.style.overflow = "visible";
      clonedDoc.documentElement.style.height = "auto";
    }
    var inner = clonedDoc.getElementById("receipt-capture-root");
    if (inner) {
      inner.style.overflow = "visible";
      inner.style.maxHeight = "none";
      inner.style.height = "auto";
      inner.style.paddingBottom = "1.5rem";
    }
  }

  async function captureReceiptPng() {
    if (typeof html2canvas !== "function") {
      throw new Error("html2canvas غير محمّل");
    }
    var root = captureRoot();
    if (!root) {
      throw new Error("لم يُعثر على محتوى الفاتورة");
    }
    root.scrollIntoView({ block: "start", behavior: "instant" });
    await waitForImages(root);
    await new Promise(function (r) {
      requestAnimationFrame(function () {
        requestAnimationFrame(r);
      });
    });
    var box = measureCaptureBox(root);
    var thermal = isThermalPaper(root);
    var scale = thermal
      ? captureScale()
      : Math.min(2, Math.max(1.25, global.devicePixelRatio || 1.5));
    var targetDots = thermal ? thermalFullDots(root) : 0;
    var canvas = await html2canvas(root, {
      scale: scale,
      width: box.width,
      height: box.height,
      windowWidth: box.width + 32,
      windowHeight: box.height + 64,
      scrollX: 0,
      scrollY: 0,
      x: 0,
      y: 0,
      useCORS: true,
      allowTaint: true,
      backgroundColor: "#ffffff",
      logging: false,
      ignoreElements: function (el) {
        return el.classList && el.classList.contains("no-print");
      },
      onclone: function (clonedDoc, clonedRoot) {
        fixCloneForCapture(clonedDoc, clonedRoot);
        if (!thermal || targetDots <= 0) return;
        var page = findReceiptPage(clonedRoot);
        if (page) {
          var cssW = Math.round(targetDots / scale);
          page.style.width = cssW + "px";
          page.style.maxWidth = cssW + "px";
          page.style.minWidth = cssW + "px";
          page.style.boxSizing = "border-box";
          page.style.margin = "0 auto";
          page.style.paddingTop = "2mm";
          page.style.paddingBottom = "4mm";
          page.style.paddingLeft = "5mm";
          page.style.paddingRight = "5mm";
        }
        clonedRoot.style.width = "100%";
        clonedRoot.style.maxWidth = "100%";
        clonedRoot.style.boxSizing = "border-box";
      },
    });
    return canvas.toDataURL("image/png");
  }

  async function postReceiptImage(url, dataUrl) {
    var res = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      body: JSON.stringify({ image_png_b64: dataUrl }),
    });
    var data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }
    return { res: res, data: data };
  }

  global.receiptCapturePrint = async function (url, onStatus) {
    if (onStatus) onStatus("جاري تجهيز صورة الفاتورة…", null);
    var dataUrl = await captureReceiptPng();
    if (onStatus) onStatus("جاري الإرسال للطابعة…", null);
    var out = await postReceiptImage(url, dataUrl);
    return out;
  };
})(window);
