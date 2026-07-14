(function () {
  "use strict";

  var cfg = window.__WEB_ANALYTICS__ || null;
  if (!cfg || !cfg.surface) return;

  var SURFACE = cfg.surface;
  var CURRENCY = cfg.currency || "LYD";
  var PAGE_PATH = cfg.page_path || location.pathname || "/";
  var TRACK_EXTERNAL = cfg.track_external !== false;
  var STORAGE_KEY = "wa_sid_" + SURFACE;

  function randomId() {
    var a = new Uint8Array(16);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(a);
    else for (var i = 0; i < a.length; i++) a[i] = (Math.random() * 256) | 0;
    return Array.from(a, function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  }

  function sessionId() {
    try {
      var sid = localStorage.getItem(STORAGE_KEY);
      if (!sid) {
        sid = randomId();
        localStorage.setItem(STORAGE_KEY, sid);
      }
      return sid;
    } catch (e) {
      return randomId();
    }
  }

  function num(val) {
    var n = parseFloat(val);
    return isNaN(n) ? 0 : n;
  }

  function beacon(eventType, meta) {
    var body = {
      surface: SURFACE,
      event_type: eventType,
      page_path: PAGE_PATH,
      session_id: sessionId(),
      meta: meta || {},
    };
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([JSON.stringify(body)], { type: "application/json" });
        navigator.sendBeacon("/api/web-analytics/event", blob);
        return;
      }
    } catch (e) {}
    fetch("/api/web-analytics/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
      credentials: "same-origin",
    }).catch(function () {});
  }

  function pushDataLayer(obj) {
    if (!TRACK_EXTERNAL) return;
    window.dataLayer = window.dataLayer || [];
    window.dataLayer.push(obj);
  }

  function gtagEvent(name, params) {
    if (!TRACK_EXTERNAL || typeof window.gtag !== "function") return;
    window.gtag("event", name, params || {});
  }

  function fbEvent(name, params, isStandard) {
    if (!TRACK_EXTERNAL || typeof window.fbq !== "function") return;
    if (isStandard) window.fbq("track", name, params || {});
    else window.fbq("trackCustom", name, params || {});
  }

  function ttEvent(name, params) {
    if (!TRACK_EXTERNAL || typeof window.ttq !== "object" || !window.ttq.track) return;
    window.ttq.track(name, params || {});
  }

  var GA_MAP = {
    pageview: "page_view",
    search: "search",
    add_to_cart: "add_to_cart",
    begin_checkout: "begin_checkout",
    purchase: "purchase",
    booking: "generate_lead",
    room_order: "purchase",
  };

  var FB_MAP = {
    pageview: "PageView",
    search: "Search",
    add_to_cart: "AddToCart",
    begin_checkout: "InitiateCheckout",
    purchase: "Purchase",
    booking: "Lead",
    room_order: "Purchase",
  };

  var TT_MAP = {
    pageview: "Browse",
    search: "Search",
    add_to_cart: "AddToCart",
    begin_checkout: "InitiateCheckout",
    purchase: "CompletePayment",
    booking: "SubmitForm",
    room_order: "CompletePayment",
  };

  function commerceParams(meta) {
    var m = meta || {};
    var out = {};
    if (m.value != null) out.value = num(m.value);
    if (m.currency) out.currency = m.currency;
    else out.currency = CURRENCY;
    if (m.items) out.items = m.items;
    if (m.transaction_id) out.transaction_id = String(m.transaction_id);
    if (m.search_term) out.search_term = m.search_term;
    if (m.content_name) out.content_name = m.content_name;
    if (m.content_ids) out.content_ids = m.content_ids;
    return out;
  }

  function trackEvent(eventType, meta) {
    var ev = (eventType || "").toLowerCase();
    if (!ev) return;
    var m = meta || {};
    beacon(ev, m);

    var gaName = GA_MAP[ev] || ev;
    var gaParams = commerceParams(m);
    if (m.sale_id) gaParams.transaction_id = String(m.sale_id);
    if (m.booking_id) gaParams.transaction_id = String(m.booking_id);
    gtagEvent(gaName, gaParams);

    pushDataLayer({ event: ev, surface: SURFACE, page_path: PAGE_PATH, meta: m });

    var fbName = FB_MAP[ev];
    if (fbName) fbEvent(fbName, commerceParams(m), true);
    else fbEvent(ev, commerceParams(m), false);

    var ttName = TT_MAP[ev];
    if (ttName) ttEvent(ttName, commerceParams(m));
  }

  function trackPageView() {
    trackEvent("pageview", { page_path: PAGE_PATH });
  }

  window.WebAnalytics = {
    surface: SURFACE,
    trackEvent: trackEvent,
    trackPageView: trackPageView,
    trackPurchase: function (meta) {
      trackEvent("purchase", meta);
    },
    trackBooking: function (meta) {
      trackEvent("booking", meta);
    },
    trackRoomOrder: function (meta) {
      trackEvent("room_order", meta);
    },
    trackAddToCart: function (meta) {
      trackEvent("add_to_cart", meta);
    },
    trackBeginCheckout: function (meta) {
      trackEvent("begin_checkout", meta);
    },
    trackSearch: function (meta) {
      trackEvent("search", meta);
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", trackPageView);
  } else {
    trackPageView();
  }

  if (cfg.flash_event) {
    try {
      trackEvent(cfg.flash_event.type, cfg.flash_event.meta || {});
    } catch (e) {}
  }
})();
