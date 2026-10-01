'use strict';
(() => {
  const panels = [...document.querySelectorAll('[data-page-panel]')];
  const links = [...document.querySelectorAll('[data-page-link]')];
  const labels = new Map(panels.map(panel => [panel.id, panel.dataset.pageTitle]));
  function showPage(focus = false) {
    const requested = location.hash.slice(1);
    const page = labels.has(requested) ? requested : 'tasks';
    if (requested && requested !== page) history.replaceState(null, '', '#tasks');
    for (const panel of panels) panel.hidden = panel.id !== page;
    for (const link of links) {
      if (link.dataset.pageLink === page) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    document.title = `${labels.get(page)} · ARIADNE`;
    if (focus) document.getElementById(page).querySelector('h2').focus();
  }
  // Keep the same DOM and loaded data so navigation never resets draft inputs.
  document.querySelector('.skip-link').addEventListener('click', event => {
    event.preventDefault();
    document.getElementById('main-content').focus();
  });
  window.addEventListener('hashchange', () => showPage(true));
  showPage();
})();
