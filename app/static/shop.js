(function () {

  "use strict";



  var STORAGE_KEY = "pos_shop_token";

  var REF_KEY = "pos_shop_ref";

  var token = localStorage.getItem(STORAGE_KEY) || "";

  var state = null;

  var sections = [];

  var categories = [];

  var allCategories = [];

  var products = [];

  var allProducts = [];

  var categoryFilterMap = {};

  var activeSection = null;

  var activeCategory = null;

  var checkoutStep = "cart";

  var referralEnabled = false;

  var rateProductId = null;

  var rateStars = 0;

  var shareProductId = null;

  var checkoutDraft = { name: "", phone: "", referral: "" };
  var autofilledGuestName = "";
  var lookupPhoneTimer = null;
  var lastLookupPhone = "";



  var pageCfg = window.__SHOP_PAGE__ || {};



  var productsEl = document.getElementById("shop-products");

  var sectionsEl = document.getElementById("shop-sections");

  var categoriesEl = document.getElementById("shop-categories");

  var cartBtn = document.getElementById("shop-cart-btn");

  var cartBadge = document.getElementById("shop-cart-badge");

  var drawer = document.getElementById("shop-drawer");

  var drawerBody = document.getElementById("shop-drawer-body");

  var drawerFoot = document.getElementById("shop-drawer-foot");

  var drawerClose = document.getElementById("shop-drawer-close");

  var drawerBackdrop = document.getElementById("shop-drawer-backdrop");

  var toastEl = document.getElementById("shop-toast");

  var rateModal = document.getElementById("shop-rate-modal");

  var shareModal = document.getElementById("shop-share-modal");



  if (!productsEl) return;



  function esc(text) {

    var d = document.createElement("div");

    d.textContent = text || "";

    return d.innerHTML;

  }



  function captureCheckoutDraft() {

    var nameEl = document.getElementById("shop-name");

    var phoneEl = document.getElementById("shop-phone");

    var refEl = document.getElementById("shop-referral");

    if (nameEl && nameEl.value.trim()) checkoutDraft.name = nameEl.value.trim();

    if (phoneEl && phoneEl.value.trim()) checkoutDraft.phone = phoneEl.value.trim();

    if (refEl && refEl.value.trim()) checkoutDraft.referral = refEl.value.trim();

  }



  function clearCheckoutDraft() {

    checkoutDraft = { name: "", phone: "", referral: "" };
    autofilledGuestName = "";
    lastLookupPhone = "";
    if (lookupPhoneTimer) {
      clearTimeout(lookupPhoneTimer);
      lookupPhoneTimer = null;
    }

  }



  function normalizePhoneInput(raw) {

    var digits = String(raw || "").replace(/\D/g, "");

    if (digits.indexOf("00218") === 0) digits = digits.slice(5);

    else if (digits.indexOf("218") === 0) digits = digits.slice(3);

    if (digits.length === 9 && digits.charAt(0) === "9") digits = "0" + digits;

    return digits;

  }



  function validateCheckoutPhone(raw) {

    var p = normalizePhoneInput(raw);

    if (p.length !== 10) return "أدخل رقم الهاتف 10 أرقام (مثل 0917122552)";

    if (p.indexOf("09") !== 0) return "رقم الجوال الليبي يبدأ بـ 09";

    return "";

  }

  function errorMessage(err, fallback) {

    fallback = fallback || "حدث خطأ — حاول مرة أخرى";

    if (!err) return fallback;

    if (typeof err === "string") return err;

    var msg = err.message || "";

    if (msg && msg !== "[object Object]") return msg;

    return fallback;

  }



  function formatApiError(data, fallback) {

    fallback = fallback || "خطأ في الطلب";

    if (!data) return fallback;

    var d = data.detail !== undefined ? data.detail : data.message;

    if (!d) return fallback;

    if (typeof d === "string") return d;

    if (Array.isArray(d)) {

      var parts = d

        .map(function (item) {

          if (typeof item === "string") return item;

          if (!item) return "";

          if (item.loc && item.loc.indexOf("phone") !== -1) {

            return "أدخل رقم الهاتف بصيغة 09xxxxxxxx";

          }

          if (item.msg) return String(item.msg);

          return "";

        })

        .filter(Boolean);

      return parts.length ? parts.join(" — ") : fallback;

    }

    if (typeof d === "object" && d.msg) return String(d.msg);

    return fallback;

  }



  function showToast(msg) {

    if (!toastEl) return;

    toastEl.textContent = errorMessage(msg, "حدث خطأ");

    toastEl.hidden = false;

    setTimeout(function () {

      toastEl.hidden = true;

    }, 2800);

  }



  function api(method, url, body) {

    var opts = { method: method, headers: { "Content-Type": "application/json" } };

    if (body !== undefined) opts.body = JSON.stringify(body);

    return fetch(url, opts).then(function (r) {

      return r

        .json()

        .catch(function () {

          return {};

        })

        .then(function (data) {

          if (!r.ok) throw new Error(formatApiError(data, "خطأ في الطلب"));

          return data;

        });

    });

  }



  function storeReferralFromUrl() {

    var ref = pageCfg.referralFromUrl || "";

    if (ref) localStorage.setItem(REF_KEY, ref);

  }



  function applyStoredReferral() {

    var ref = localStorage.getItem(REF_KEY) || pageCfg.referralFromUrl || "";

    if (!ref || !token) return Promise.resolve();

    return api("POST", "/api/shop/checkout/referral", { token: token, referral_code: ref }).then(

      function (res) {

        state = res.state;

      }

    ).catch(function () {});

  }



  function ensureSession() {

    if (token) {

      return api("GET", "/api/shop/session?token=" + encodeURIComponent(token))

        .then(function (res) {

          state = res.state;

          referralEnabled = !!state.referral_enabled;

          return token;

        })

        .catch(function () {

          token = "";

          localStorage.removeItem(STORAGE_KEY);

          return createSession();

        });

    }

    return createSession();

  }



  function createSession() {

    return api("POST", "/api/shop/session").then(function (res) {

      token = res.token;

      localStorage.setItem(STORAGE_KEY, token);

      return loadState().then(applyStoredReferral);

    });

  }



  function loadState() {

    return api("GET", "/api/shop/session?token=" + encodeURIComponent(token)).then(function (res) {

      state = res.state;

      referralEnabled = !!state.referral_enabled;

      updateBadge();

      if (state.order_phase === "submitted" || state.order_phase === "await_receipt") {

        checkoutStep = "done";
        // بعد الإرسال لا نظهر عداد سلة قديم
        updateBadge();

      }

      return state;

    });

  }



  function syncCategoriesForSection() {
    categories = pickCategories(allCategories, sections);
  }

  function applyCatalogFilter() {
    var filterId = activeCategory || activeSection;
    if (!filterId) {
      products = allProducts.slice();
    } else {
      var allowed = categoryFilterMap[String(filterId)];
      if (allowed && allowed.length) {
        var set = {};
        allowed.forEach(function (id) {
          set[String(id)] = true;
        });
        products = allProducts.filter(function (p) {
          return p.category_id != null && set[String(p.category_id)];
        });
      } else {
        products = allProducts.filter(function (p) {
          return intEq(p.category_id, filterId);
        });
      }
    }
    renderSections();
    renderCategories();
    renderProducts();
  }

  function loadCatalog(catId, sectionId) {
    if (sectionId !== undefined && sectionId !== null) {
      activeSection = sectionId;
    }
    if (catId !== undefined) {
      activeCategory = catId;
    }
    if (allProducts.length) {
      applyCatalogFilter();
      return Promise.resolve();
    }
    return api("GET", "/api/shop/catalog").then(function (res) {
      sections = res.sections || [];
      categoryFilterMap = res.filter_map || {};
      allProducts = res.products || [];
      allCategories = res.categories || [];
      referralEnabled = !!res.referral_enabled;
      if (activeSection === null && sections.length >= 1) {
        activeSection = sections[0].id;
      }
      syncCategoriesForSection();
      applyCatalogFilter();
    });
  }

  function reloadFilteredProducts() {
    applyCatalogFilter();
    return Promise.resolve();
  }



  function pickCategories(allCats, secs) {

    if (!activeSection || !secs.length) return allCats;

    var sec = secs.find(function (s) {

      return s.id === activeSection;

    });

    if (!sec) return [];

    if (sec.subcategories && sec.subcategories.length) {

      return sec.subcategories;

    }

    return [

      {

        id: sec.id,

        name_ar: sec.name_ar,

        product_count: sec.product_count || 0,

      },

    ];

  }



  function filterProductsBySection(list) {

    if (!activeSection || !sections.length) return list;

    var sec = sections.find(function (s) {

      return s.id === activeSection;

    });

    if (!sec) return list;

    if (activeCategory) {

      return list.filter(function (p) {

        return intEq(p.category_id, activeCategory) || isInSectionCategory(p, activeCategory);

      });

    }

    return list;

  }



  function isInSectionCategory(product, catId) {

    return intEq(product.category_id, catId);

  }



  function intEq(a, b) {

    return String(a) === String(b);

  }



  function cartQtyTotal() {
    var cart = (state && state.cart) || [];
    var n = 0;
    for (var i = 0; i < cart.length; i++) {
      var q = parseFloat(cart[i] && cart[i].qty);
      if (!isNaN(q) && q > 0) n += q;
    }
    return Math.round(n);
  }

  function updateBadge() {
    if (!cartBadge) return;
    var phase = state && state.order_phase;
    var cart = (state && state.cart) || [];
    var n = 0;
    // بعد إرسال الطلب لا نعرض عداداً حتى يختار العميل «طلب جديد»
    if (phase === "submitted" || phase === "await_receipt") {
      n = 0;
    } else if (cart.length) {
      n = cartQtyTotal() || parseInt(state.cart_count, 10) || cart.length;
    } else {
      n = 0;
    }
    if (n > 0) {
      cartBadge.textContent = String(n);
      cartBadge.hidden = false;
      cartBadge.removeAttribute("hidden");
    } else {
      cartBadge.textContent = "0";
      cartBadge.hidden = true;
      cartBadge.setAttribute("hidden", "");
    }
  }



  function renderSections() {

    if (!sectionsEl) return;

    if (sections.length < 2) {

      sectionsEl.hidden = true;

      sectionsEl.innerHTML = "";

      return;

    }

    sectionsEl.hidden = false;

    sectionsEl.innerHTML = sections

      .map(function (s) {

        return (

          '<button type="button" class="shop-section-btn' +

          (activeSection === s.id ? " active" : "") +

          '" data-section="' +

          s.id +

          '">' +

          esc(s.name_ar) +

          "</button>"

        );

      })

      .join("");

  }



  function renderCategories() {

    if (!categoriesEl) return;

    var html =

      '<button type="button" class="shop-cat-btn' +

      (activeCategory ? "" : " active") +

      '" data-cat="">الكل</button>';

    categories.forEach(function (c) {

      html +=

        '<button type="button" class="shop-cat-btn' +

        (activeCategory === c.id ? " active" : "") +

        '" data-cat="' +

        c.id +

        '">' +

        esc(c.name_ar) +

        "</button>";

    });

    categoriesEl.innerHTML = html;

  }



  function renderProducts() {

    if (!productsEl) return;

    if (!products.length) {

      productsEl.innerHTML =

        '<p class="muted" style="grid-column:1/-1;text-align:center">لا توجد أصناف متاحة.</p>';

      return;

    }

    var highlight = pageCfg.highlightProduct || "";

    productsEl.innerHTML = products

      .map(function (p) {

        var img = p.image_url

          ? '<img src="' + esc(p.image_url) + '" alt="" loading="lazy" />'

          : "🍽️";

        var desc = p.description

          ? '<p class="shop-card-desc">' + esc(p.description) + "</p>"

          : "";

        var rating =

          p.avg_rating && Number(p.rating_count) > 0

            ? '<span class="shop-card-rating">★ ' +

              esc(p.avg_rating) +

              " (" +

              esc(String(p.rating_count)) +

              ")</span>"

            : "";

        var hl =

          highlight && String(p.id) === String(highlight) ? " shop-card-highlight" : "";

        return (

          '<article class="shop-card' +

          hl +

          '" data-product-id="' +

          p.id +

          '">' +

          '<div class="shop-card-img">' +

          img +

          "</div>" +

          '<div class="shop-card-body">' +

          '<h3 class="shop-card-title">' +

          esc(p.name_ar) +

          "</h3>" +

          desc +

          '<div class="shop-card-meta">' +

          '<p class="shop-card-price">' +

          esc(p.sell_price) +

          " د.ل</p>" +

          rating +

          "</div>" +

          '<div class="shop-card-actions">' +

          '<button type="button" class="shop-card-secondary" data-rate="' +

          p.id +

          '">تقييم</button>' +

          '<button type="button" class="shop-card-secondary" data-share="' +

          p.id +

          '">↗ مشاركة</button>' +

          "</div>" +

          '<button type="button" class="shop-card-add" data-add="' +

          p.id +

          '">أضف إلى السلة</button>' +

          "</div></article>"

        );

      })

      .join("");

    if (highlight) {

      var el = productsEl.querySelector('[data-product-id="' + highlight + '"]');

      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });

    }

  }



  function openModal(modal) {

    if (!modal) return;

    modal.hidden = false;

    modal.setAttribute("aria-hidden", "false");

  }



  function closeModal(modal) {

    if (!modal) return;

    modal.hidden = true;

    modal.setAttribute("aria-hidden", "true");

  }



  function openRateModal(productId, productName) {

    rateProductId = productId;

    rateStars = 0;

    var nameEl = document.getElementById("shop-rate-product-name");

    if (nameEl) nameEl.textContent = productName || "";

    updateStarUi(0);

    openModal(rateModal);

  }



  function updateStarUi(n) {

    rateStars = n;

    var starsEl = document.getElementById("shop-rate-stars");

    if (!starsEl) return;

    starsEl.querySelectorAll("[data-star]").forEach(function (btn) {

      var v = parseInt(btn.getAttribute("data-star"), 10);

      btn.classList.toggle("selected", v <= n);

    });

  }



  function submitRate() {

    if (!rateProductId || rateStars < 1) {

      showToast("اختر عدد النجوم");

      return;

    }

    api("POST", "/api/shop/rate", {

      product_id: rateProductId,

      stars: rateStars,

    })

      .then(function () {

        closeModal(rateModal);

        showToast("شكراً على تقييمك!");

        reloadFilteredProducts();

      })

      .catch(function (e) {

        showToast(errorMessage(e, "تعذّر إتمام الطلب — تحقق من البيانات وحاول مرة أخرى"));

      });

  }



  function openShareModal(productId, productName) {

    shareProductId = productId;

    var nameEl = document.getElementById("shop-share-product-name");

    var phoneEl = document.getElementById("shop-share-phone");

    var textEl = document.getElementById("shop-share-text");

    if (nameEl) nameEl.textContent = productName || "";

    if (phoneEl) phoneEl.value = (state && state.guest_phone) || "";

    if (textEl) textEl.value = "جاري التحميل…";

    toggleSharePicker(false);

    openModal(shareModal);

    loadShareText(productId, phoneEl ? phoneEl.value : "");

  }



  function loadShareText(productId, phone, notify) {

    var textEl = document.getElementById("shop-share-text");

    return api("POST", "/api/shop/share", {

      token: token,

      product_id: productId,

      phone: phone || null,

      notify: !!notify,

    })

      .then(function (res) {

        if (textEl) textEl.value = res.message || res.url || "";

        return res;

      })

      .catch(function (e) {

        if (textEl) textEl.value = "";

        showToast(errorMessage(e, "تعذّر تحميل رابط المشاركة"));

      });

  }



  function notifyShareReferral() {
    if (!shareProductId) return Promise.resolve();
    var phoneEl = document.getElementById("shop-share-phone");
    var phone = phoneEl ? String(phoneEl.value || "").trim() : "";
    if (!phone) {
      showToast("أدخل رقم هاتفك لإرسال كود الإحالة");
      return Promise.resolve();
    }
    return loadShareText(shareProductId, phone, true).catch(function () {});
  }

  function copyShareText() {

    var textEl = document.getElementById("shop-share-text");

    var text = textEl ? textEl.value : "";

    if (!text) return;

    notifyShareReferral();

    if (navigator.clipboard && navigator.clipboard.writeText) {

      navigator.clipboard.writeText(text).then(function () {

        showToast("تم النسخ");

      });

      return;

    }

    textEl.select();

    document.execCommand("copy");

    showToast("تم النسخ");

  }



  function getShareText() {
    var textEl = document.getElementById("shop-share-text");
    var text = textEl ? String(textEl.value || "").trim() : "";
    if (!text || text === "جاري التحميل…") return null;
    return text;
  }

  function toggleSharePicker(show) {
    var picker = document.getElementById("shop-share-picker");
    var sysBtn = document.getElementById("shop-share-via-system");
    if (!picker) return;
    if (sysBtn) sysBtn.hidden = !navigator.share;
    picker.hidden = !show;
  }

  function shareViaWhatsApp(text) {
    var wa = "https://wa.me/?text=" + encodeURIComponent(text);
    var opened = window.open(wa, "_blank", "noopener,noreferrer");
    if (!opened) {
      copyShareText();
      showToast("تم النسخ — الصق في واتساب أو فعّل النوافذ المنبثقة");
    }
  }

  function shareViaTelegram(text) {
    var urlMatch = text.match(/https?:\/\/\S+/);
    var url = urlMatch ? urlMatch[0] : "";
    var tg =
      "https://t.me/share/url?url=" +
      encodeURIComponent(url) +
      "&text=" +
      encodeURIComponent(text);
    var opened = window.open(tg, "_blank", "noopener,noreferrer");
    if (!opened) {
      copyShareText();
      showToast("تم النسخ — الصق في تيليجرام أو فعّل النوافذ المنبثقة");
    }
  }

  function systemShare() {
    var text = getShareText();
    if (!text) {
      showToast("انتظر حتى يُحمّل نص المشاركة");
      return;
    }
    if (!navigator.share) {
      showToast("مشاركة الجهاز غير متاحة في هذا المتصفح");
      return;
    }
    var urlMatch = text.match(/https?:\/\/\S+/);
    var sharePayload = {
      title: pageCfg.storeName || "المتجر",
      text: text,
    };
    if (urlMatch) sharePayload.url = urlMatch[0];
    var can =
      typeof navigator.canShare === "function"
        ? navigator.canShare(sharePayload)
        : true;
    if (!can) {
      showToast("لا يمكن مشاركة هذا المحتوى من المتصفح");
      return;
    }
    navigator
      .share(sharePayload)
      .then(function () {
        toggleSharePicker(false);
        showToast("تمت المشاركة");
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        showToast("تعذّرت مشاركة الجهاز — جرّب واتساب أو نسخ النص");
      });
  }

  function handleShareVia(via) {
    var text = getShareText();
    if (!text) {
      showToast("انتظر حتى يُحمّل نص المشاركة");
      return;
    }
    toggleSharePicker(false);
    notifyShareReferral();
    if (via === "system") {
      systemShare();
      return;
    }
    if (via === "whatsapp") {
      shareViaWhatsApp(text);
      return;
    }
    if (via === "telegram") {
      shareViaTelegram(text);
      return;
    }
    if (via === "copy") {
      copyShareText();
    }
  }

  function nativeShare() {
    var text = getShareText();
    if (!text) {
      showToast("انتظر حتى يُحمّل نص المشاركة");
      return;
    }
    var picker = document.getElementById("shop-share-picker");
    if (picker && !picker.hidden) {
      toggleSharePicker(false);
      return;
    }
    toggleSharePicker(true);
  }



  function openDrawer() {

    if (!drawer) return;

    drawer.hidden = false;

    drawer.setAttribute("aria-hidden", "false");

    renderDrawer();

  }



  function closeDrawer() {

    if (!drawer) return;

    drawer.hidden = true;

    drawer.setAttribute("aria-hidden", "true");

  }



  function money(v) {

    return esc(String(v || "0")) + " د.ل";

  }



  function renderDrawer() {

    if (!state || !drawerBody || !drawerFoot) return;

    if (checkoutStep === "done") {

      var confParts = Array.isArray(state.confirmation_parts)
        ? state.confirmation_parts.filter(function (p) {
            return String(p || "").trim();
          })
        : [];
      if (!confParts.length && state.confirmation_message) {
        confParts = [state.confirmation_message];
      }
      if (!confParts.length) {
        confParts = ["سيظهر الطلب في نقطة البيع قريباً."];
      }
      drawerBody.innerHTML =
        '<div class="shop-success">' +
        "<p>✅ تم إرسال طلبك</p>" +
        confParts
          .map(function (p) {
            return (
              '<p class="shop-confirm-part" style="white-space:pre-wrap;text-align:right">' +
              esc(String(p)) +
              "</p>"
            );
          })
          .join("") +
        "</div>";

      drawerFoot.innerHTML =

        '<button type="button" class="shop-btn-primary" id="shop-new-order">طلب جديد</button>';

      return;

    }



    if (checkoutStep === "cart") {

      renderCartStep();

    } else if (checkoutStep === "checkout") {

      renderCheckoutStep();

    }

  }



  function renderCartStep() {

    var cart = (state && state.cart) || [];

    if (!cart.length) {

      drawerBody.innerHTML = "<p>السلة فارغة.</p>";

      drawerFoot.innerHTML = "";

      if (state) state.cart_count = 0;

      updateBadge();

      return;

    }

    drawerBody.innerHTML = cart

      .map(function (ln) {

        return (

          '<div class="shop-line" data-pid="' +

          ln.product_id +

          '">' +

          '<div class="shop-line-info"><p class="shop-line-name">' +

          esc(ln.name_ar) +

          '</p><p class="shop-line-price">' +

          money(ln.line_total) +

          "</p></div>" +

          '<div class="shop-qty">' +

          '<button type="button" data-qty-minus="' +

          ln.product_id +

          '">−</button>' +

          "<span>" +

          esc(ln.qty) +

          "</span>" +

          '<button type="button" data-qty-plus="' +

          ln.product_id +

          '">+</button>' +

          "</div></div>"

        );

      })

      .join("");

    drawerFoot.innerHTML =

      '<div class="shop-summary">المجموع: <strong>' +

      money(state.cart_total) +

      "</strong></div>" +

      '<button type="button" class="shop-btn-primary" id="shop-go-checkout">إتمام الطلب</button>' +

      '<button type="button" class="shop-btn-secondary" id="shop-clear-cart">تفريغ السلة</button>';

  }



  function renderCheckoutStep() {

    captureCheckoutDraft();

    var zones = (state && state.delivery_zones) || [];

    var fulfillment = (state && state.fulfillment) || "PICKUP";

    var guestName = checkoutDraft.name || (state && state.guest_name) || "";

    var guestPhone = checkoutDraft.phone || (state && state.guest_phone) || "";

    var refVal =

      checkoutDraft.referral ||

      (state && state.referral_code) ||

      localStorage.getItem(REF_KEY) ||

      "";

    var refField =

      referralEnabled || state.referral_enabled

        ? '<div class="shop-field"><label>كود الإحالة (اختياري)</label><input id="shop-referral" type="text" dir="ltr" value="' +

          esc(refVal) +

          '" placeholder="123456" maxlength="32" /></div>'

        : "";

    drawerBody.innerHTML =

      '<div class="shop-field"><label>الهاتف *</label><input id="shop-phone" type="tel" dir="ltr" value="' +

      esc(guestPhone) +

      '" placeholder="09xxxxxxxx" required autocomplete="tel" /></div>' +

      '<div class="shop-field"><label>الاسم (اختياري)</label><input id="shop-name" value="' +

      esc(guestName) +

      '" maxlength="120" autocomplete="name" /></div>' +

      refField +

      '<div class="shop-field"><label>طريقة الاستلام</label><div class="shop-choice-row">' +

      '<button type="button" class="shop-choice' +

      (fulfillment === "PICKUP" ? " selected" : "") +

      '" data-fulfillment="PICKUP">🥡 استلام</button>' +

      '<button type="button" class="shop-choice' +

      (fulfillment === "DELIVERY" ? " selected" : "") +

      '" data-fulfillment="DELIVERY">🚚 توصيل</button></div></div>' +

      (fulfillment === "DELIVERY"

        ? '<div class="shop-field"><label>منطقة التوصيل</label><select id="shop-zone"><option value="">—</option>' +

          zones

            .map(function (z) {

              return (

                '<option value="' +

                z.id +

                '"' +

                (String(state.delivery_zone_id) === String(z.id) ? " selected" : "") +

                ">" +

                esc(z.name_ar) +

                " — " +

                esc(z.fee) +

                " د.ل</option>"

              );

            })

            .join("") +

          "</select></div>"

        : "") +

      '<div class="shop-summary">الأصناف: ' +

      money(state.payment_amount) +

      (Number(state.delivery_fee) > 0 ? "<br>التوصيل: " + money(state.delivery_fee) : "") +

      "<br><strong>الإجمالي: " +

      money(state.customer_total) +

      "</strong></div>";

    drawerFoot.innerHTML =

      '<button type="button" class="shop-btn-primary" id="shop-submit-order">تأكيد الطلب</button>' +

      '<button type="button" class="shop-btn-secondary" id="shop-back-cart">رجوع للسلة</button>';

    wireGuestPhoneLookup();

  }

  function applyLookupGuestName(name) {
    var nameEl = document.getElementById("shop-name");
    if (!nameEl) return;
    var current = (nameEl.value || "").trim();
    var found = (name || "").trim();
    if (!found) return;
    // لا نستبدل اسماً كتبه الزبون يدوياً — فقط إن كان فارغاً أو من تعبئة سابقة
    if (!current || current === autofilledGuestName) {
      nameEl.value = found;
      autofilledGuestName = found;
      checkoutDraft.name = found;
    }
  }

  function lookupGuestByPhoneNow() {
    var phoneEl = document.getElementById("shop-phone");
    if (!phoneEl || !token) return;
    var phoneErr = validateCheckoutPhone(phoneEl.value || "");
    if (phoneErr) return;
    var phone = normalizePhoneInput(phoneEl.value || "");
    if (!phone || phone === lastLookupPhone) return;
    lastLookupPhone = phone;
    api("POST", "/api/shop/checkout/lookup-guest", {
      token: token,
      phone: phone,
      name: null,
    })
      .then(function (res) {
        if (res && res.found && res.name) {
          applyLookupGuestName(res.name);
        }
      })
      .catch(function () {
        /* تجاهل أخطاء البحث — لا تعطل الإتمام */
      });
  }

  function wireGuestPhoneLookup() {
    var phoneEl = document.getElementById("shop-phone");
    if (!phoneEl || phoneEl.dataset.lookupBound === "1") return;
    phoneEl.dataset.lookupBound = "1";
    function schedule() {
      if (lookupPhoneTimer) clearTimeout(lookupPhoneTimer);
      lookupPhoneTimer = setTimeout(lookupGuestByPhoneNow, 350);
    }
    phoneEl.addEventListener("input", schedule);
    phoneEl.addEventListener("blur", lookupGuestByPhoneNow);
    phoneEl.addEventListener("change", lookupGuestByPhoneNow);
    if ((phoneEl.value || "").trim()) {
      lookupGuestByPhoneNow();
    }
  }



  function addProduct(pid) {

    api("POST", "/api/shop/cart/add", { token: token, product_id: pid, qty: "1" })

      .then(function (res) {

        state = res.state;

        updateBadge();

        showToast("أُضيف للسلة");

        if (window.WebAnalytics) {
          var pn = findProductName(pid);
          WebAnalytics.trackAddToCart({
            product_id: pid,
            content_name: pn,
            value: (function () {
              var p = products.find(function (x) { return intEq(x.id, pid); });
              return p ? p.price : 0;
            })(),
            currency: "LYD",
          });
        }

      })

      .catch(function (e) {

        showToast(errorMessage(e, "تعذّر إتمام الطلب — تحقق من البيانات وحاول مرة أخرى"));

      });

  }



  function setQty(pid, qty) {

    api("POST", "/api/shop/cart/update", { token: token, product_id: pid, qty: String(qty) })

      .then(function (res) {

        state = res.state;

        updateBadge();

        renderDrawer();

      })

      .catch(function (e) {

        showToast(errorMessage(e, "تعذّر إتمام الطلب — تحقق من البيانات وحاول مرة أخرى"));

      });

  }



  function runCheckout() {

    var name = (document.getElementById("shop-name") || {}).value || "";

    var phone = (document.getElementById("shop-phone") || {}).value || "";

    captureCheckoutDraft();

    checkoutDraft.name = name.trim();

    var phoneErr = validateCheckoutPhone(phone);

    if (phoneErr) {

      showToast(phoneErr);

      return;

    }

    phone = normalizePhoneInput(phone);

    checkoutDraft.phone = phone;

    var refEl = document.getElementById("shop-referral");

    var refCode =

      (refEl && refEl.value ? refEl.value.trim() : "") ||

      (state && state.referral_code ? String(state.referral_code).trim() : "") ||

      localStorage.getItem(REF_KEY) ||

      "";

    var fulfillment =

      (document.querySelector(".shop-choice.selected") || {}).getAttribute("data-fulfillment") ||

      "PICKUP";

    var zoneEl = document.getElementById("shop-zone");

    if (fulfillment === "DELIVERY") {

      if (!zoneEl || !zoneEl.value) {

        showToast("اختر منطقة التوصيل");

        return;

      }

    }

    var guestName = name.trim();

    var chain = api("POST", "/api/shop/checkout/guest", {
      token: token,
      name: guestName || null,
      phone: phone,
    })

      .then(function (res) {

        state = res.state;

        if (state.guest_name) checkoutDraft.name = state.guest_name;

        if (state.guest_phone) checkoutDraft.phone = state.guest_phone;

        if (refCode) {

          return api("POST", "/api/shop/checkout/referral", {

            token: token,

            referral_code: refCode,

          });

        }

        return res;

      })

      .then(function (res) {

        state = res.state;

        return api("POST", "/api/shop/checkout/fulfillment", { token: token, fulfillment: fulfillment });

      })

      .then(function (res) {

        state = res.state;

        if (fulfillment === "DELIVERY" && zoneEl && zoneEl.value) {

          return api("POST", "/api/shop/checkout/delivery-zone", {

            token: token,

            zone_id: parseInt(zoneEl.value, 10),

          });

        }

        return res;

      })

      .then(function (res) {

        state = res.state;

        return api("POST", "/api/shop/checkout/submit", { token: token });

      })

      .then(function (res) {

        state = res.state;
        if (state) {
          state.cart = state.cart || [];
          state.cart_count = state.cart_count || 0;
        }
        checkoutStep = "done";
        updateBadge();
        renderDrawer();

        if (window.WebAnalytics && state) {
          WebAnalytics.trackPurchase({
            sale_id: state.sale_id,
            value: state.customer_total,
            currency: "LYD",
            transaction_id: state.sale_id,
          });
        }

      })

      .catch(function (e) {

        showToast(errorMessage(e, "تعذّر إتمام الطلب — تحقق من البيانات وحاول مرة أخرى"));

      });

  }



  function findProductName(pid) {

    var p = products.find(function (x) {

      return intEq(x.id, pid);

    });

    return p ? p.name_ar : "";

  }



  if (sectionsEl) {

    sectionsEl.addEventListener("click", function (e) {

      var btn = e.target.closest("[data-section]");

      if (!btn) return;

      activeSection = parseInt(btn.getAttribute("data-section"), 10);

      activeCategory = null;

      syncCategoriesForSection();

      reloadFilteredProducts();

    });

  }



  if (categoriesEl) {

    categoriesEl.addEventListener("click", function (e) {

      var btn = e.target.closest("[data-cat]");

      if (!btn) return;

      var raw = btn.getAttribute("data-cat");

      activeCategory = raw ? parseInt(raw, 10) : null;

      reloadFilteredProducts();

    });

  }



  productsEl.addEventListener("click", function (e) {

    var add = e.target.closest("[data-add]");

    if (add) {

      addProduct(parseInt(add.getAttribute("data-add"), 10));

      return;

    }

    var rate = e.target.closest("[data-rate]");

    if (rate) {

      var rid = parseInt(rate.getAttribute("data-rate"), 10);

      openRateModal(rid, findProductName(rid));

      return;

    }

    var share = e.target.closest("[data-share]");

    if (share) {

      var sid = parseInt(share.getAttribute("data-share"), 10);

      openShareModal(sid, findProductName(sid));

    }

  });



  if (cartBtn) cartBtn.addEventListener("click", openDrawer);

  if (drawerClose) drawerClose.addEventListener("click", closeDrawer);

  if (drawerBackdrop) drawerBackdrop.addEventListener("click", closeDrawer);



  document.querySelectorAll("[data-close-modal]").forEach(function (el) {

    el.addEventListener("click", function () {

      closeModal(rateModal);

      closeModal(shareModal);

    });

  });



  var starsEl = document.getElementById("shop-rate-stars");

  if (starsEl) {

    starsEl.addEventListener("click", function (e) {

      var btn = e.target.closest("[data-star]");

      if (!btn) return;

      updateStarUi(parseInt(btn.getAttribute("data-star"), 10));

    });

  }



  var rateSubmit = document.getElementById("shop-rate-submit");

  if (rateSubmit) rateSubmit.addEventListener("click", submitRate);



  var sharePhone = document.getElementById("shop-share-phone");

  if (sharePhone) {

    sharePhone.addEventListener("change", function () {

      if (shareProductId) loadShareText(shareProductId, sharePhone.value);

    });

  }



  var shareCopy = document.getElementById("shop-share-copy");

  if (shareCopy) shareCopy.addEventListener("click", copyShareText);



  var shareNative = document.getElementById("shop-share-native");

  if (shareNative) shareNative.addEventListener("click", nativeShare);

  var sharePicker = document.getElementById("shop-share-picker");

  if (sharePicker) {
    sharePicker.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-share-via]");
      if (!btn) return;
      handleShareVia(btn.getAttribute("data-share-via"));
    });
  }



  drawer.addEventListener("click", function (e) {

    if (e.target.id === "shop-go-checkout") {

      checkoutStep = "checkout";

      if (window.WebAnalytics && state) {
        WebAnalytics.trackBeginCheckout({
          value: state.customer_total,
          currency: "LYD",
        });
      }

      renderDrawer();

      return;

    }

    if (e.target.id === "shop-back-cart") {

      checkoutStep = "cart";

      renderDrawer();

      return;

    }

    if (e.target.id === "shop-clear-cart") {

      api("POST", "/api/shop/cart/clear", { token: token })

        .then(function (res) {

          state = res.state;

          updateBadge();

          renderDrawer();

        })

        .catch(function (err) {

          showToast(errorMessage(err, "تعذّر تفريغ السلة"));

        });

      return;

    }

    if (e.target.id === "shop-submit-order") {

      runCheckout();

      return;

    }

    if (e.target.id === "shop-new-order") {

      localStorage.removeItem(STORAGE_KEY);

      token = "";

      checkoutStep = "cart";

      clearCheckoutDraft();

      createSession().then(function () {

        loadCatalog(null);

        closeDrawer();

      });

      return;

    }

    var minus = e.target.closest("[data-qty-minus]");

    if (minus) {

      var pidM = parseInt(minus.getAttribute("data-qty-minus"), 10);

      var lnM = (state.cart || []).find(function (x) {

        return parseInt(x.product_id, 10) === pidM;

      });

      var qM = lnM ? parseFloat(lnM.qty) - 1 : 0;

      setQty(pidM, qM);

      return;

    }

    var plus = e.target.closest("[data-qty-plus]");

    if (plus) {

      var pidP = parseInt(plus.getAttribute("data-qty-plus"), 10);

      var lnP = (state.cart || []).find(function (x) {

        return parseInt(x.product_id, 10) === pidP;

      });

      var qP = lnP ? parseFloat(lnP.qty) + 1 : 1;

      setQty(pidP, qP);

      return;

    }

    var ful = e.target.closest("[data-fulfillment]");

    if (ful) {

      captureCheckoutDraft();

      state.fulfillment = ful.getAttribute("data-fulfillment");

      renderCheckoutStep();

      return;

    }

    var pm = e.target.closest("[data-pm]");

    if (pm) {

      captureCheckoutDraft();

      state.payment_method_id = parseInt(pm.getAttribute("data-pm"), 10);

      renderCheckoutStep();

    }

  });



  storeReferralFromUrl();



  function initHeroCarousel() {

    var hero = document.getElementById("shop-hero");

    if (!hero) return;

    var slides = hero.querySelectorAll(".shop-hero-slide");

    if (slides.length < 2) return;

    var dots = hero.querySelectorAll(".shop-hero-dot");

    var prevBtn = document.getElementById("shop-hero-prev");

    var nextBtn = document.getElementById("shop-hero-next");

    var intervalMs = Math.max(3000, (parseInt(hero.getAttribute("data-interval"), 10) || 10) * 1000);

    var index = 0;

    var timer = null;



    function show(i) {

      index = (i + slides.length) % slides.length;

      slides.forEach(function (slide, n) {

        slide.classList.toggle("is-active", n === index);

      });

      dots.forEach(function (dot, n) {

        dot.classList.toggle("is-active", n === index);

      });

    }



    function next() {

      show(index + 1);

    }



    function prev() {

      show(index - 1);

    }



    function restartTimer() {

      if (timer) clearInterval(timer);

      timer = setInterval(next, intervalMs);

    }



    if (nextBtn) nextBtn.addEventListener("click", function () { next(); restartTimer(); });

    if (prevBtn) prevBtn.addEventListener("click", function () { prev(); restartTimer(); });

    dots.forEach(function (dot) {

      dot.addEventListener("click", function () {

        var i = parseInt(dot.getAttribute("data-index"), 10);

        if (!isNaN(i)) {

          show(i);

          restartTimer();

        }

      });

    });

    hero.addEventListener("mouseenter", function () {

      if (timer) clearInterval(timer);

    });

    hero.addEventListener("mouseleave", restartTimer);

    restartTimer();

  }



  initHeroCarousel();



  ensureSession()

    .then(function () {

      return applyStoredReferral().then(function () {

        return loadCatalog(null);

      });

    })

    .catch(function (e) {

      showToast(errorMessage(e, "تعذّر تحميل المتجر"));

    });

})();

