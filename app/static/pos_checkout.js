(function () {
  function checkoutRoot(root) {
    return root || document.getElementById("pos-checkout-dialog-body") || document;
  }

  window.initPosCheckoutForm = function (root) {
    root = checkoutRoot(root);
    var form = root.querySelector("#pay-form");
    if (!form) return;

    var submitBtn = root.querySelector("#pay-submit-btn");
    var payMethodError = root.querySelector("#pay-method-error");
    var payMethodGrid = root.querySelector("#pay-method-grid");
    var loyaltyCb = root.querySelector("#use-loyalty-redeem");
    var loyaltyBlock = root.querySelector("#loyalty-redeem-block");
    var loyaltyFullHint = root.querySelector("#loyalty-full-cover-hint");
    var financialPreview = root.querySelector("#checkout-financial-preview");
    var splitToggle = root.querySelector("#split-payment-toggle");
    var splitFields = root.querySelector("#split-payment-fields");
    var splitSummary = root.querySelector("#split-payment-summary");
    var employeeFields = root.querySelector("#employee-checkout-fields");
    var employeeSelect = root.querySelector("#employee-meal-employee");
    var employeeSummary = root.querySelector("#employee-meal-summary");

    function refreshRefs() {
      submitBtn = root.querySelector("#pay-submit-btn");
      payMethodError = root.querySelector("#pay-method-error");
      payMethodGrid = root.querySelector("#pay-method-grid");
      loyaltyCb = root.querySelector("#use-loyalty-redeem");
      loyaltyBlock = root.querySelector("#loyalty-redeem-block");
      loyaltyFullHint = root.querySelector("#loyalty-full-cover-hint");
      financialPreview = root.querySelector("#checkout-financial-preview");
      splitToggle = root.querySelector("#split-payment-toggle");
      splitFields = root.querySelector("#split-payment-fields");
      splitSummary = root.querySelector("#split-payment-summary");
      employeeFields = root.querySelector("#employee-checkout-fields");
      employeeSelect = root.querySelector("#employee-meal-employee");
      employeeSummary = root.querySelector("#employee-meal-summary");
    }

    function money(value) {
      var n = Number(value || 0);
      if (!isFinite(n)) n = 0;
      return n.toFixed(3).replace(/\.?0+$/, "");
    }

    function points(value) {
      var n = Number(value || 0);
      if (!isFinite(n)) n = 0;
      return String(Math.round(n));
    }

    function loyaltyCoversFullTotal() {
      if (employeeWalletSelected()) return false;
      if (!loyaltyCb || !loyaltyCb.checked || !loyaltyBlock) return false;
      var maxD = parseFloat(loyaltyBlock.getAttribute("data-max-discount") || "0");
      var total = parseFloat(loyaltyBlock.getAttribute("data-sale-total") || "0");
      return maxD >= total - 0.0005;
    }

    function employeeCheckoutSelected() {
      var selected = form.querySelector('input[name="checkout_party"]:checked');
      return !!(selected && selected.value === "employee");
    }

    function employeeWalletSelected() {
      if (!employeeCheckoutSelected()) return false;
      var selected = form.querySelector('input[name="employee_meal_payment"]:checked');
      return !!(selected && selected.value === "wallet");
    }

    function selectedEmployeeMealBalance() {
      if (!employeeSelect || !employeeSelect.value) return 0;
      var opt = employeeSelect.options[employeeSelect.selectedIndex];
      var n = opt ? parseFloat(opt.getAttribute("data-balance") || "0") : 0;
      return isFinite(n) && n > 0 ? n : 0;
    }

    function amountDueBeforeEmployeeMeal() {
      if (!financialPreview) return 0;
      var useRedeem = !!(loyaltyCb && loyaltyCb.checked && !employeeWalletSelected());
      var total = parseFloat(financialPreview.getAttribute("data-invoice-total") || "0");
      var due = useRedeem ? total - currentRedeemDiscount() : total;
      if (due < 0) due = 0;
      return isFinite(due) ? due : 0;
    }

    function currentEmployeeMealCover() {
      if (!employeeWalletSelected()) return 0;
      var due = amountDueBeforeEmployeeMeal();
      var balance = selectedEmployeeMealBalance();
      var cover = Math.min(due, balance);
      return isFinite(cover) && cover > 0 ? cover : 0;
    }

    function currentAmountDue() {
      var due = amountDueBeforeEmployeeMeal() - currentEmployeeMealCover();
      if (due < 0) due = 0;
      return isFinite(due) ? due : 0;
    }

    function currentRedeemDiscount() {
      if (!loyaltyCb || !loyaltyCb.checked || employeeWalletSelected()) return 0;
      var discount = financialPreview
        ? parseFloat(financialPreview.getAttribute("data-redeem-discount") || "0")
        : 0;
      if ((!isFinite(discount) || discount <= 0) && loyaltyBlock) {
        discount = parseFloat(loyaltyBlock.getAttribute("data-max-discount") || "0");
      }
      return isFinite(discount) && discount > 0 ? discount : 0;
    }

    function currentRedeemPoints() {
      if (!loyaltyCb || !loyaltyCb.checked || employeeWalletSelected()) return 0;
      var redeemPts = financialPreview
        ? parseFloat(financialPreview.getAttribute("data-redeem-points") || "0")
        : 0;
      if ((!isFinite(redeemPts) || redeemPts <= 0) && loyaltyBlock) {
        redeemPts = parseFloat(loyaltyBlock.getAttribute("data-max-points") || "0");
      }
      return isFinite(redeemPts) && redeemPts > 0 ? redeemPts : 0;
    }

    function splitPaymentEnabled() {
      return !!(splitToggle && splitToggle.checked);
    }

    function splitPaymentTotal() {
      var sum = 0;
      form.querySelectorAll(".split-payment-amount").forEach(function (input) {
        var n = parseFloat(input.value || "0");
        if (isFinite(n) && n > 0) sum += n;
      });
      return sum;
    }

    function splitPaymentMatchesDue() {
      if (!splitPaymentEnabled()) return false;
      var due = currentAmountDue();
      if (due <= 0.0005) return true;
      return Math.abs(splitPaymentTotal() - due) <= 0.0005;
    }

    function needsPaymentMethod() {
      if (splitPaymentEnabled()) return false;
      return currentAmountDue() > 0.0005;
    }

    function hasPaymentSelected() {
      if (!needsPaymentMethod()) return true;
      return !!form.querySelector('input[name="payment_method_id"]:checked');
    }

    function syncLoyaltyUi() {
      refreshRefs();
      if (loyaltyCb && employeeWalletSelected()) loyaltyCb.checked = false;
      var full = loyaltyCoversFullTotal();
      if (loyaltyFullHint) loyaltyFullHint.style.display = full ? "block" : "none";
      if (full && payMethodGrid) payMethodGrid.style.opacity = "0.45";
      else if (payMethodGrid) payMethodGrid.style.opacity = "";
      if (full) hidePayMethodError();
      syncFinancialPreview();
      syncSplitPaymentUi();
    }

    function syncFinancialPreview() {
      if (!financialPreview) return;
      var useRedeem = !!(loyaltyCb && loyaltyCb.checked);
      var total = parseFloat(financialPreview.getAttribute("data-invoice-total") || "0");
      var discount = useRedeem ? currentRedeemDiscount() : 0;
      var redeemPts = useRedeem ? currentRedeemPoints() : 0;
      var due = useRedeem ? currentAmountDue() : total;
      if (employeeWalletSelected()) due = currentAmountDue();
      var earnedNoRedeem = parseFloat(financialPreview.getAttribute("data-earned-no-redeem") || "0");
      var earned = useRedeem
        ? parseFloat(financialPreview.getAttribute("data-earned-with-redeem") || "0")
        : earnedNoRedeem;
      if (useRedeem && discount > 0 && total > 0) {
        var computedEarned = due * (earnedNoRedeem / total);
        if (!isFinite(earned) || earned <= 0 || Math.abs(earned - earnedNoRedeem) <= 0.0005) {
          earned = computedEarned;
        }
      }
      if (employeeWalletSelected() && total > 0) {
        earned = due * (earnedNoRedeem / total);
      }
      var setText = function (sel, txt) {
        var el = financialPreview.querySelector(sel);
        if (el) el.textContent = txt;
      };
      setText("[data-fin='discount']", money(discount));
      setText("[data-fin='due']", money(due));
      setText("[data-fin='earned']", points(earned));
      var ptsEl = financialPreview.querySelector("[data-fin-points]");
      if (ptsEl) ptsEl.textContent = redeemPts > 0 ? "(" + points(redeemPts) + " نقطة)" : "";
    }

    function syncSplitPaymentUi() {
      refreshRefs();
      var enabled = splitPaymentEnabled();
      if (splitFields) splitFields.style.display = enabled ? "block" : "none";
      if (payMethodGrid) payMethodGrid.style.opacity = enabled ? "0.45" : "";
      if (enabled) {
        form.querySelectorAll('input[name="payment_method_id"]').forEach(function (input) {
          input.checked = false;
        });
        syncPaySelection();
      }
      if (splitSummary) {
        var total = splitPaymentTotal();
        var due = currentAmountDue();
        var diff = due - total;
        splitSummary.textContent =
          "مجموع الدفع المقسّم: " + money(total) + " د.ل" +
          " — المطلوب: " + money(due) + " د.ل" +
          (Math.abs(diff) > 0.0005 ? " — الفرق: " + money(diff) + " د.ل" : " — مطابق");
        splitSummary.style.color = Math.abs(diff) > 0.0005 ? "#b91c1c" : "#047857";
      }
      if (enabled && splitPaymentMatchesDue()) hidePayMethodError();
    }

    function syncEmployeeMealUi() {
      refreshRefs();
      var employeeMode = employeeCheckoutSelected();
      if (employeeFields) employeeFields.style.display = employeeMode ? "block" : "none";
      if (loyaltyBlock) loyaltyBlock.style.opacity = employeeWalletSelected() ? "0.45" : "";
      if (loyaltyCb) loyaltyCb.disabled = employeeWalletSelected();
      if (employeeSummary) {
        if (!employeeMode) {
          employeeSummary.textContent = "اختر قسم الموظفين إذا كانت الوجبة تخص موظفاً.";
        } else if (!employeeSelect || !employeeSelect.value) {
          employeeSummary.textContent = "اختر موظفاً لعرض رصيد الوجبات.";
        } else if (employeeWalletSelected()) {
          var before = amountDueBeforeEmployeeMeal();
          var balance = selectedEmployeeMealBalance();
          var cover = currentEmployeeMealCover();
          var personal = currentAmountDue();
          employeeSummary.textContent =
            "رصيد الموظف المتاح: " + money(balance) + " د.ل" +
            " — سيتحمل المطعم: " + money(cover) + " د.ل" +
            " — يدفع الموظف: " + money(personal) + " د.ل" +
            (balance < before ? " (الفرق على الموظف)" : "");
        } else {
          employeeSummary.textContent = "الموظف سيدفع كامل قيمة الوجبة وتُحتسب له نقاط على المبلغ المدفوع.";
        }
      }
      syncFinancialPreview();
      syncSplitPaymentUi();
      syncPaySelection();
    }

    function showPayMethodError() {
      if (payMethodError) payMethodError.style.display = "block";
      if (payMethodGrid) {
        payMethodGrid.style.outline = "2px solid #f87171";
        payMethodGrid.style.borderRadius = "12px";
        payMethodGrid.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } else if (payMethodError) {
        payMethodError.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }

    function hidePayMethodError() {
      if (payMethodError) payMethodError.style.display = "none";
      if (payMethodGrid) payMethodGrid.style.outline = "";
    }

    function syncPaySelection() {
      refreshRefs();
      form.querySelectorAll("[data-pay-tile]").forEach(function (tile) {
        var input = tile.querySelector('input[type="radio"]');
        tile.classList.toggle("checked", input && input.checked);
      });
      if (hasPaymentSelected()) hidePayMethodError();
    }

    function blockIfNoPayment(e) {
      refreshRefs();
      if (splitPaymentEnabled()) {
        if (splitPaymentMatchesDue()) return;
        e.preventDefault();
        e.stopPropagation();
        if (payMethodError) {
          payMethodError.textContent = "مجموع مبالغ الدفع المقسّم يجب أن يساوي المبلغ المطلوب دفعه.";
        }
        showPayMethodError();
        return;
      }
      if (hasPaymentSelected()) return;
      e.preventDefault();
      e.stopPropagation();
      if (payMethodError) payMethodError.textContent = "اختر وسيلة الدفع المناسبة";
      showPayMethodError();
    }

    function confirmFinancialPreview(e) {
      refreshRefs();
      if (!financialPreview || form.getAttribute("data-financial-confirmed") === "1") return;
      if (splitPaymentEnabled() && !splitPaymentMatchesDue()) return;
      if (!hasPaymentSelected()) return;
      var getText = function (sel) {
        var el = financialPreview.querySelector(sel);
        return el ? (el.textContent || "").trim() : "0";
      };
      var invoice = getText("[data-fin='invoice']");
      var discount = getText("[data-fin='discount']");
      var due = getText("[data-fin='due']");
      var earned = getText("[data-fin='earned']");
      var paymentLine = splitPaymentEnabled()
        ? "\nدفع مقسّم: " + money(splitPaymentTotal()) + " د.ل"
        : "";
      var msg =
        "تأكيد إقفال الفاتورة؟\n\n" +
        "إجمالي الفاتورة: " + invoice + " د.ل\n" +
        "خصم نقاط الولاء: " + discount + " د.ل\n" +
        "المبلغ المطلوب دفعه: " + due + " د.ل\n" +
        "النقاط المتوقع اكتسابها: " + earned + " نقطة" +
        paymentLine;
      if (!window.confirm(msg)) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      form.setAttribute("data-financial-confirmed", "1");
    }

    if (!form._posCheckoutReady) {
      form._posCheckoutReady = true;
      form.addEventListener("change", function (e) {
        refreshRefs();
        if (e.target && e.target.name === "payment_method_id") syncPaySelection();
        if (e.target && e.target.id === "use-loyalty-redeem") syncLoyaltyUi();
        if (e.target && e.target.id === "split-payment-toggle") syncSplitPaymentUi();
        if (
          e.target &&
          (e.target.name === "checkout_party" ||
            e.target.name === "employee_meal_payment" ||
            e.target.id === "employee-meal-employee")
        ) syncEmployeeMealUi();
      });
      form.addEventListener("input", function (e) {
        refreshRefs();
        if (e.target && e.target.classList && e.target.classList.contains("split-payment-amount")) {
          syncSplitPaymentUi();
        }
      });
      form.querySelectorAll("[data-pay-tile]").forEach(function (tile) {
        tile.addEventListener("click", function () {
          if (splitPaymentEnabled()) return;
          var input = tile.querySelector('input[type="radio"]');
          if (input) {
            input.checked = true;
            syncPaySelection();
          }
        });
      });
      form.addEventListener("submit", blockIfNoPayment);
      form.addEventListener("submit", confirmFinancialPreview);
      if (submitBtn) submitBtn.addEventListener("click", blockIfNoPayment);
    }

    syncPaySelection();
    syncEmployeeMealUi();
    syncLoyaltyUi();
    syncFinancialPreview();
    syncSplitPaymentUi();

    var driverDlg = root.querySelector("#checkout-driver-dialog");
    var driverOpenBtn = root.querySelector("#open-driver-dialog-btn");
    var driverCancelBtn = root.querySelector("#checkout-driver-cancel");
    var driverPick = root.querySelector("#checkout-driver-pick");
    var driverName = root.querySelector("#checkout-driver-name");
    var driverPhone = root.querySelector("#checkout-driver-phone");
    if (driverOpenBtn && driverDlg && !driverOpenBtn._posDrvBound) {
      driverOpenBtn._posDrvBound = true;
      driverOpenBtn.addEventListener("click", function () { driverDlg.showModal(); });
    }
    if (driverCancelBtn && driverDlg && !driverCancelBtn._posDrvBound) {
      driverCancelBtn._posDrvBound = true;
      driverCancelBtn.addEventListener("click", function () { driverDlg.close(); });
    }
    if (driverPick && !driverPick._posDrvBound) {
      driverPick._posDrvBound = true;
      driverPick.addEventListener("change", function () {
        var opt = driverPick.options[driverPick.selectedIndex];
        if (!opt || !opt.dataset.name) return;
        if (driverName) driverName.value = opt.dataset.name || "";
        if (driverPhone) driverPhone.value = opt.dataset.phone || "";
      });
    }

    var cancelBtn = root.querySelector("#pos-checkout-cancel");
    if (cancelBtn && !cancelBtn._posCheckoutCancelBound) {
      cancelBtn._posCheckoutCancelBound = true;
      cancelBtn.addEventListener("click", function () {
        var dlg = document.getElementById("pos-checkout-dialog");
        if (dlg && typeof dlg.close === "function") dlg.close();
      });
    }

    var phoneIn = root.querySelector("#checkout-phone");
    if (phoneIn && !phoneIn._posCheckoutBindInit) {
      phoneIn._posCheckoutBindInit = true;
      var bindTimer = null;

      function normalizedPhone() {
        return (phoneIn.value || "").replace(/\D/g, "");
      }

      function bindCheckoutCustomer() {
        var digits = normalizedPhone();
        if (digits.length !== 10 || !/^09/.test(digits)) return;
        if (form.getAttribute("data-bound-phone") === digits) return;
        if (!window.htmx) return;
        form.setAttribute("data-bound-phone", digits);
        htmx.ajax("POST", "/pos/checkout/bind-customer", {
          target: "#checkout-customer-fields",
          swap: "innerHTML",
          values: {
            checkout_phone: digits,
            checkout_name: (root.querySelector("#checkout-name") || {}).value || "",
          },
        });
      }

      phoneIn.addEventListener("input", function () {
        if (bindTimer) clearTimeout(bindTimer);
        var digits = normalizedPhone();
        if (digits.length === 10 && /^09/.test(digits) && form.getAttribute("data-bound-phone") !== digits) {
          bindTimer = setTimeout(bindCheckoutCustomer, 450);
        }
      });

      if (normalizedPhone().length === 10) {
        bindCheckoutCustomer();
      }
    }
  };

  function reinitCheckoutFromSwap(target) {
    if (!target || !target.id) return;
    if (target.id !== "checkout-customer-fields" && target.id !== "checkout-loyalty-redeem") return;
    var root = document.getElementById("pos-checkout-dialog-body") || document;
    if (window.htmx) htmx.process(target);
    if (window.initPosCheckoutForm) window.initPosCheckoutForm(root);
  }

  document.body.addEventListener("htmx:afterSwap", function (ev) {
    reinitCheckoutFromSwap(ev.detail && ev.detail.target);
  });
})();
