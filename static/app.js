document.addEventListener("submit", (event) => {
  const button = event.submitter || event.target.querySelector("[data-csrf]");
  if (button && button.dataset.csrf) {
    event.preventDefault();
    fetch(event.target.action, {method: "POST", body: new FormData(event.target), headers: {"X-CSRF-Token": button.dataset.csrf}})
      .then((response) => response.redirected ? window.location.assign(response.url) : response.text().then((html) => { document.open(); document.write(html); document.close(); }));
  }
});

