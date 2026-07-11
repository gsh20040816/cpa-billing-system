document.addEventListener("submit", (event) => {
  const submitter = event.submitter;
  const message = submitter && submitter.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
    return;
  }
  if (submitter) {
    submitter.disabled = true;
    submitter.setAttribute("aria-busy", "true");
  }
});

document.querySelectorAll(".cycle-select select").forEach((select) => {
  select.addEventListener("change", () => select.form.requestSubmit());
});

document.querySelectorAll("[data-back]").forEach((button) => {
  button.addEventListener("click", () => {
    if (window.history.length > 1) {
      window.history.back();
    } else {
      window.location.assign("/");
    }
  });
});
