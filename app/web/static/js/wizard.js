// Profile wizard: one form shown as three steps with per-step validation.
// Without JavaScript the same form is shown in full and validated by the server.
(function () {
  "use strict";

  var form = document.getElementById("profile-wizard");
  if (!form) return;
  var steps = Array.prototype.slice.call(form.querySelectorAll(".wizard-step"));
  var indicators = document.querySelectorAll("[data-step-indicator]");
  var current = Math.min(Math.max(parseInt(form.dataset.initialStep || "1", 10), 1), steps.length);

  form.noValidate = true; // we validate step by step and focus the first problem ourselves

  function show(number, focus) {
    current = number;
    steps.forEach(function (step, index) {
      step.classList.toggle("is-current", index + 1 === number);
    });
    indicators.forEach(function (item) {
      var n = parseInt(item.dataset.stepIndicator, 10);
      item.classList.toggle("is-current", n === number);
      item.classList.toggle("is-done", n < number);
      if (n === number) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    if (focus) {
      var heading = steps[number - 1].querySelector(".wizard-step__title");
      form.scrollIntoView({ behavior: "smooth", block: "start" });
      if (heading) heading.focus({ preventScroll: true });
    }
  }

  function firstInvalid(step) {
    var fields = step.querySelectorAll("input, select, textarea");
    for (var i = 0; i < fields.length; i++) {
      if (!fields[i].checkValidity()) return fields[i];
    }
    return null;
  }

  function validate(step) {
    var invalid = firstInvalid(step);
    if (!invalid) return true;
    var details = invalid.closest("details");
    if (details) details.open = true;
    invalid.reportValidity();
    return false;
  }

  form.addEventListener("click", function (event) {
    if (event.target.closest("[data-next]")) {
      if (validate(steps[current - 1])) show(current + 1, true);
    } else if (event.target.closest("[data-prev]")) {
      show(current - 1, true);
    }
  });

  form.addEventListener("submit", function (event) {
    for (var i = 0; i < steps.length; i++) {
      if (firstInvalid(steps[i])) {
        event.preventDefault();
        show(i + 1, false);
        validate(steps[i]);
        return;
      }
    }
  });

  // Gender-dependent wording («не был женат» / «не была замужем», «готов» / «готова»…).
  function applyGender(gender) {
    form.querySelectorAll("[data-male]").forEach(function (el) {
      el.textContent = el.dataset[gender] || el.dataset.neutral || el.textContent;
    });
  }
  form.addEventListener("change", function (event) {
    if (event.target.matches("[data-gender-switch]")) applyGender(event.target.value);
  });
  var checked = form.querySelector("[data-gender-switch]:checked");
  if (checked) applyGender(checked.value);

  // Enter in a text field moves to the next step instead of submitting the whole form.
  form.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.target.tagName !== "INPUT" || current === steps.length) return;
    event.preventDefault();
    var next = steps[current - 1].querySelector("[data-next]");
    if (next) next.click();
  });

  show(current, false);
  if (form.dataset.initialStep && form.querySelector("[aria-invalid='true']")) {
    var invalid = steps[current - 1].querySelector("[aria-invalid='true']");
    if (invalid) {
      var details = invalid.closest("details");
      if (details) details.open = true;
      invalid.focus();
    }
  }
})();
