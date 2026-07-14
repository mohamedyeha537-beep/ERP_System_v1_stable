/** تنسيق كميات/أوزان: بدون أصفار عشرية زائدة (100 لا 100.000). */
(function (global) {
  function formatQtyPlain(value) {
    if (value == null || value === "") return "0";
    var n = Number(String(value).replace(",", "."));
    if (!isFinite(n)) return "0";
    var text = String(n);
    if (text.indexOf("e") >= 0 || text.indexOf("E") >= 0) {
      text = n.toFixed(10);
    }
    if (text.indexOf(".") >= 0) {
      text = text.replace(/\.?0+$/, "");
    }
    return text === "" || text === "-0" ? "0" : text;
  }

  global.formatQtyPlain = formatQtyPlain;
})(typeof window !== "undefined" ? window : globalThis);
