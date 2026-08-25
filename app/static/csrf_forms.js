(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta && meta.getAttribute("content");
  if (!token) return;
  document.querySelectorAll("form[method='post'], form[method='POST']").forEach(function (form) {
    if (form.querySelector('input[name="csrf_token"]')) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "csrf_token";
    input.value = token;
    form.appendChild(input);
  });
})();
