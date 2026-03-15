// Actualisation manuelle via le bouton
function refresh() {
  const btn = document.querySelector('.btn-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ En cours…'; }

  fetch('/api/refresh')
    .then(r => r.json())
    .then(() => window.location.reload())
    .catch(() => window.location.reload());
}

// Actualisation automatique selon l'intervalle défini en config
(function autoRefresh() {
  const INTERVAL_MIN = 30; // doit correspondre à config.REFRESH_INTERVAL_MINUTES
  setTimeout(() => window.location.reload(), INTERVAL_MIN * 60 * 1000);
})();
