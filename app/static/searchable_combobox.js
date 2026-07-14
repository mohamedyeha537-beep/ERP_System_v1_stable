(function (global) {
  "use strict";

  function initSearchableCombobox(root, opts) {
    if (!root || root._comboboxInit) return;
    root._comboboxInit = true;

    opts = opts || {};
    var hidden = root.querySelector("[data-combobox-value]");
    var input = root.querySelector("[data-combobox-input]");
    var menu = root.querySelector("[data-combobox-menu]");
    var emptyMsg = root.querySelector("[data-combobox-empty]");
    var toggle = root.querySelector(".bom-combobox-toggle");
    var clearBtn = root.querySelector("[data-combobox-clear]");
    var options = Array.prototype.slice.call(
      root.querySelectorAll(".bom-combobox-option[data-value]")
    );
    var autoSubmitForm =
      opts.autoSubmitForm ||
      (root.dataset.autoSubmit
        ? document.querySelector(root.dataset.autoSubmit)
        : null);
    var submitOnSelect = opts.submitOnSelect !== false;

    if (!hidden || !input || !menu) return;

    var open = false;
    var selectedLabel = "";
    var activeIndex = -1;

    function visibleOptions() {
      return options.filter(function (o) {
        return !o.hidden;
      });
    }

    function syncInputFromHidden() {
      var val = (hidden.value || "").trim();
      if (!val) {
        selectedLabel = "";
        input.value = "";
        input.placeholder = input.getAttribute("data-placeholder") || "";
        if (clearBtn) clearBtn.hidden = true;
        return;
      }
      var match = options.find(function (o) {
        return (o.getAttribute("data-value") || "") === val;
      });
      selectedLabel = match
        ? match.getAttribute("data-label") || match.textContent.trim()
        : val;
      input.value = selectedLabel;
      if (clearBtn) clearBtn.hidden = false;
    }

    var minChars = parseInt(root.getAttribute("data-min-chars") || "0", 10) || 0;
    var defaultEmptyMsg = emptyMsg ? emptyMsg.textContent : "";

    function isBrowsingList() {
      var q = (input.value || "").trim();
      return !q || q === selectedLabel;
    }

    function filterOptions() {
      var q = (input.value || "").trim().toLowerCase();
      var count = 0;
      var browsing = isBrowsingList();
      options.forEach(function (opt) {
        var text = (opt.getAttribute("data-label") || opt.textContent || "").toLowerCase();
        var match;
        if (browsing) {
          match = true;
        } else if (minChars > 0) {
          if (q.length >= minChars) {
            match = text.indexOf(q) !== -1;
          } else {
            match = false;
          }
        } else {
          match = !q || text.indexOf(q) !== -1;
        }
        opt.hidden = !match;
        opt.classList.remove("is-active");
        if (match) count += 1;
      });
      activeIndex = -1;
      if (emptyMsg) {
        if (minChars > 0 && !browsing && q.length > 0 && q.length < minChars) {
          emptyMsg.textContent =
            "اكتب " + minChars + " أحرف على الأقل للبحث";
          emptyMsg.hidden = false;
        } else {
          if (defaultEmptyMsg) emptyMsg.textContent = defaultEmptyMsg;
          emptyMsg.hidden = count > 0;
        }
      }
      var vis = visibleOptions();
      if (vis.length && q) setActive(0);
      return count;
    }

    function setActive(idx) {
      var vis = visibleOptions();
      vis.forEach(function (o) {
        o.classList.remove("is-active");
      });
      activeIndex = idx;
      if (idx >= 0 && idx < vis.length) {
        vis[idx].classList.add("is-active");
        vis[idx].scrollIntoView({ block: "nearest" });
      }
    }

    function openMenu() {
      if (open) return;
      open = true;
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
      filterOptions();
    }

    function closeMenu() {
      open = false;
      menu.hidden = true;
      input.setAttribute("aria-expanded", "false");
      options.forEach(function (o) {
        o.classList.remove("is-active");
        o.hidden = false;
      });
      if (emptyMsg) emptyMsg.hidden = true;
      activeIndex = -1;
      syncInputFromHidden();
    }

    function maybeSubmit() {
      if (autoSubmitForm && submitOnSelect) {
        autoSubmitForm.submit();
      }
    }

    function selectOption(opt) {
      hidden.value = opt.getAttribute("data-value") || "";
      selectedLabel = opt.getAttribute("data-label") || opt.textContent.trim();
      if (hidden.value) {
        input.value = selectedLabel;
      } else {
        input.value = "";
      }
      closeMenu();
      hidden.dispatchEvent(new Event("change", { bubbles: true }));
      maybeSubmit();
    }

    function clearSelection() {
      hidden.value = "";
      selectedLabel = "";
      input.value = "";
      closeMenu();
      hidden.dispatchEvent(new Event("change", { bubbles: true }));
      maybeSubmit();
    }

    input.setAttribute("data-placeholder", input.placeholder || "");

    input.addEventListener("focus", function () {
      openMenu();
      if (selectedLabel) input.select();
    });

    input.addEventListener("input", function () {
      if (!open) openMenu();
      if ((input.value || "").trim() !== selectedLabel) {
        hidden.value = "";
      }
      filterOptions();
    });

    input.addEventListener("keydown", function (e) {
      var vis = visibleOptions();
      if (e.key === "Escape") {
        e.preventDefault();
        closeMenu();
        input.blur();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!open) openMenu();
        setActive(activeIndex < 0 ? 0 : Math.min(activeIndex + 1, vis.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive(activeIndex < 0 ? vis.length - 1 : Math.max(activeIndex - 1, 0));
        return;
      }
      if (e.key === "Enter" && open) {
        e.preventDefault();
        if (activeIndex >= 0 && vis[activeIndex]) {
          selectOption(vis[activeIndex]);
        } else if (vis.length === 1) {
          selectOption(vis[0]);
        }
      }
    });

    if (toggle) {
      toggle.addEventListener("click", function () {
        if (open) {
          closeMenu();
          input.blur();
        } else {
          input.focus();
        }
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        clearSelection();
      });
    }

    options.forEach(function (opt) {
      opt.addEventListener("mousedown", function (e) {
        e.preventDefault();
      });
      opt.addEventListener("click", function () {
        selectOption(opt);
      });
    });

    document.addEventListener("click", function (e) {
      if (!root.contains(e.target)) closeMenu();
    });

    syncInputFromHidden();
  }

  function initAll() {
    document.querySelectorAll("[data-searchable-combobox]").forEach(function (root) {
      initSearchableCombobox(root, {
        submitOnSelect: root.getAttribute("data-submit-on-select") !== "false",
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }

  global.initSearchableCombobox = initSearchableCombobox;
})(window);
