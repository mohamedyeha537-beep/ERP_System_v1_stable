(function () {
  "use strict";

  var SELECTOR =
    ".help, .help-card, .pos-guide-panel, .pos-guide-intro";

  function guideTitle(el) {
    var custom = el.getAttribute("data-guide-title");
    if (custom) return custom;
    var h = el.querySelector(":scope > h1, :scope > h2, :scope > h3, :scope > strong");
    if (h) return "\u2139\uFE0F " + h.textContent.trim().replace(/\s+/g, " ");
    return "\u2139\uFE0F \u0625\u0631\u0634\u0627\u062f\u0627\u062A \u2014 \u0627\u0636\u063A\u0637 \u0644\u0644\u0639\u0631\u0636";
  }

  function hideDuplicateTitle(el) {
    var h = el.querySelector(":scope > h1, :scope > h2, :scope > h3, :scope > strong");
    if (h) h.classList.add("pos-guide__title-hidden");
  }

  function wrapAsGuide(el) {
    if (!el || el.dataset.posGuideDone || el.closest(".pos-guide")) return;
    if (el.classList.contains("pos-guide-skip") || el.closest(".pos-guide-skip")) return;

    var details = document.createElement("details");
    details.className = "pos-guide";

    var summary = document.createElement("summary");
    summary.className = "pos-guide__summary";
    summary.textContent = guideTitle(el);

    var body = document.createElement("div");
    body.className = "pos-guide__body";

    el.parentNode.insertBefore(details, el);
    body.appendChild(el);
    details.appendChild(summary);
    details.appendChild(body);

    hideDuplicateTitle(el);
    el.dataset.posGuideDone = "1";
  }

  function initPosGuides(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll(SELECTOR).forEach(wrapAsGuide);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initPosGuides(document);
    });
  } else {
    initPosGuides(document);
  }

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (target) initPosGuides(target);
  });
})();
