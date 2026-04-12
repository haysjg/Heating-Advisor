// Actualisation manuelle via le bouton — conserve le rechargement complet
function refresh() {
  const btn = document.querySelector('.btn-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ En cours…'; }

  fetch('/api/refresh')
    .then(r => r.json())
    .then(() => window.location.reload())
    .catch(() => window.location.reload());
}

// Module de rafraîchissement AJAX du dashboard
const DashboardRefresh = {
  intervalSeconds: 15,
  timer: null,
  errorCount: 0,
  maxErrors: 3,
  lastUpdate: null,
  isPageVisible: true,
  backoffDelay: 5000, // Initial backoff 5s

  init() {
    this.lastUpdate = new Date();
    this.startPolling();
    this.setupVisibilityChange();
    this.updateTimeAgo();
    setInterval(() => this.updateTimeAgo(), 1000); // Mise à jour du "il y a Xs" chaque seconde
  },

  startPolling() {
    if (this.timer) return; // Déjà en cours

    this.timer = setInterval(() => {
      if (this.isPageVisible) {
        this.fetchAndUpdate();
      }
    }, this.intervalSeconds * 1000);

    // Premier appel immédiat
    setTimeout(() => this.fetchAndUpdate(), this.intervalSeconds * 1000);
  },

  stopPolling() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  },

  setupVisibilityChange() {
    document.addEventListener('visibilitychange', () => {
      this.isPageVisible = !document.hidden;

      if (this.isPageVisible) {
        console.log('[Dashboard] Page visible — reprise du polling');
        // Rafraîchir immédiatement au retour
        this.fetchAndUpdate();
      } else {
        console.log('[Dashboard] Page cachée — pause du polling');
      }
    });
  },

  async fetchAndUpdate() {
    try {
      const response = await fetch('/api/dashboard/refresh');

      if (response.status === 401) {
        // Session expirée — redirection vers login
        window.location.href = '/login';
        return;
      }

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const data = await response.json();

      // Mise à jour réussie — reset erreurs
      this.errorCount = 0;
      this.backoffDelay = 5000;
      this.hideErrorBanner();
      this.lastUpdate = new Date();

      // Mise à jour du DOM
      this.updateIndoorTemp(data.indoor);
      this.updateOutdoorTemp(data.outdoor);
      this.updateRecommendation(data.recommendation, data.tempo);
      this.updateThermostat(data.thermostat);
      this.updateRadiateurs(data.radiateurs);
      this.updateTempo(data.tempo);
      this.updateTomorrow(data.tomorrow);
      this.updateTimestamp(data.timestamp);

      console.log('[Dashboard] Rafraîchissement réussi');

    } catch (error) {
      console.error('[Dashboard] Erreur de rafraîchissement:', error);
      this.errorCount++;

      if (this.errorCount >= this.maxErrors) {
        // Afficher bannière d'erreur et proposer rechargement complet
        this.showErrorBanner();
        this.stopPolling();

        // Fallback : rechargement complet après 60s
        setTimeout(() => {
          console.log('[Dashboard] Fallback — rechargement complet');
          window.location.reload();
        }, 60000);
      } else {
        // Exponential backoff
        this.backoffDelay = Math.min(this.backoffDelay * 2, 20000);
        console.log(`[Dashboard] Retry dans ${this.backoffDelay / 1000}s (erreur ${this.errorCount}/${this.maxErrors})`);
      }
    }
  },

  updateIndoorTemp(indoor) {
    const card = document.querySelector('[data-refresh="indoor"]');
    if (!card || !indoor) return;

    const temp = indoor.temperature;
    const humidity = indoor.humidity;
    const felt = indoor.felt_temperature;

    if (temp !== null && temp !== undefined) {
      const tempEl = card.querySelector('[data-value="temperature"]');
      if (tempEl) tempEl.textContent = `${temp.toFixed(1)} °C`;
    }

    if (humidity !== null && humidity !== undefined) {
      const humidityEl = card.querySelector('[data-value="humidity"]');
      if (humidityEl) humidityEl.innerHTML = `Humidité : <strong>${Math.round(humidity)} %</strong>`;
    }

    if (felt !== null && felt !== undefined) {
      const feltEl = card.querySelector('[data-value="felt"]');
      if (feltEl) feltEl.innerHTML = `Ressenti : <strong>${felt.toFixed(1)} °C</strong>`;
    }

    this.flashElement(card);
  },

  updateOutdoorTemp(outdoor) {
    const card = document.querySelector('[data-refresh="outdoor"]');
    if (!card || !outdoor) return;

    if (outdoor.temperature !== null && outdoor.temperature !== undefined) {
      const tempEl = card.querySelector('[data-value="temperature"]');
      if (tempEl) tempEl.textContent = `${outdoor.temperature.toFixed(1)} °C`;
    }

    if (outdoor.source) {
      const sourceEl = card.querySelector('[data-value="source"]');
      if (sourceEl) {
        const isReal = outdoor.source === 'météociel.fr';
        sourceEl.className = isReal ? 'source-badge source-real' : 'source-badge source-model';
        sourceEl.textContent = isReal
          ? `📡 ${outdoor.source} — mesure réelle`
          : `🌐 ${outdoor.source} — modèle (fallback)`;
      }
    }

    this.flashElement(card);
  },

  updateRecommendation(rec, tempo) {
    const card = document.querySelector('[data-refresh="recommendation"]');
    if (!card || !rec) return;

    // Mise à jour de l'icône
    const iconEl = card.querySelector('[data-value="icon"]');
    if (iconEl) {
      const icons = { clim: '❄️', poele: '🔥', none: '🌙' };
      iconEl.textContent = icons[rec.system] || '⚠️';
    }

    // Mise à jour du titre
    const titleEl = card.querySelector('[data-value="title"]');
    if (titleEl) titleEl.textContent = rec.title || '';

    // Mise à jour de l'explication
    const explanationEl = card.querySelector('[data-value="explanation"]');
    if (explanationEl) explanationEl.textContent = rec.explanation || '';

    // Mise à jour des économies
    const savingsEl = card.querySelector('[data-value="savings"]');
    if (savingsEl && rec.savings_per_hour > 0) {
      savingsEl.textContent = `Économie : ${rec.savings_per_hour.toFixed(3)} €/h`;
      savingsEl.style.display = '';
    } else if (savingsEl) {
      savingsEl.style.display = 'none';
    }

    // Mise à jour des classes CSS de la carte
    card.className = `card card-${rec.level}${rec.system === 'none' ? ' card-night' : ''} card-labeled`;

    this.flashElement(card);
  },

  updateThermostat(thermostat) {
    const card = document.querySelector('[data-refresh="thermostat"]');
    if (!card || !thermostat) return;

    // Mise à jour du badge état avec système actif
    const badgeEl = card.querySelector('[data-value="state-badge"]');
    if (badgeEl) {
      const isOn = thermostat.state === 'on';
      badgeEl.className = isOn ? 'thermostat-badge-on' : 'thermostat-badge-off';
      if (isOn) {
        if (thermostat.active_system === 'clim') {
          badgeEl.textContent = '❄️ Clim allumée';
        } else {
          badgeEl.textContent = '🔥 Poêle allumé';
        }
      } else {
        badgeEl.textContent = '⏸ Éteint';
      }
    }

    this.flashElement(card);
  },

  updateRadiateurs(radiateurs) {
    // Pour l'instant, pas de section dédiée dans le HTML avec data-refresh="radiateurs"
    // Cette fonction est prête pour une future utilisation
    console.log('[Dashboard] Radiateurs mis à jour:', radiateurs);
  },

  updateTempo(tempo) {
    // Le Tempo est affiché dans la section de droite, pas besoin de mise à jour AJAX
    // (données déjà cachées 30min)
  },

  updateTomorrow(tomorrow) {
    const card = document.querySelector('[data-refresh="tomorrow"]');
    if (!card || !tomorrow) return;

    if (tomorrow.tempo_unknown) {
      // Afficher le message "Tempo inconnu"
      card.innerHTML = `
        <div class="tomorrow-label">DEMAIN</div>
        <div class="rec-icon rec-icon-sm">❓</div>
        <div class="rec-content">
          <p class="tempo-unknown-warning">Couleur Tempo de demain non encore publiée par RTE.</p>
        </div>
      `;
      card.className = 'card card-tomorrow';
    } else {
      const rec = tomorrow.recommendation;
      if (!rec) return;

      // Mise à jour de l'icône
      const iconEl = card.querySelector('[data-value="icon"]');
      if (iconEl) {
        const icons = { clim: '❄️', poele: '🔥', none: '☀️' };
        iconEl.textContent = icons[rec.system] || '⚠️';
      }

      // Mise à jour du titre
      const titleEl = card.querySelector('[data-value="title"]');
      if (titleEl) titleEl.textContent = rec.title || '';

      // Mise à jour de l'explication
      const explanationEl = card.querySelector('[data-value="explanation"]');
      if (explanationEl) explanationEl.textContent = rec.explanation || '';

      // Mise à jour des classes CSS
      card.className = `card card-${rec.level}${rec.system === 'none' ? ' card-night' : ''} card-tomorrow`;
    }

    this.flashElement(card);
  },

  updateTimestamp(timestamp) {
    const timeEl = document.getElementById('last-update-time');
    if (timeEl && timestamp) {
      // Format : "2026-04-10 21:30:15"
      const formatted = timestamp.substring(0, 19).replace('T', ' ');
      timeEl.textContent = formatted;
    }
  },

  updateTimeAgo() {
    const timeAgoEl = document.getElementById('time-ago');
    if (!timeAgoEl || !this.lastUpdate) return;

    const now = new Date();
    const diffSeconds = Math.floor((now - this.lastUpdate) / 1000);

    if (diffSeconds < 60) {
      timeAgoEl.textContent = `(il y a ${diffSeconds}s)`;
    } else {
      const diffMinutes = Math.floor(diffSeconds / 60);
      timeAgoEl.textContent = `(il y a ${diffMinutes}min)`;
    }
  },

  flashElement(element) {
    if (!element) return;
    element.classList.remove('refresh-flash');
    // Force reflow pour redémarrer l'animation
    void element.offsetWidth;
    element.classList.add('refresh-flash');

    setTimeout(() => {
      element.classList.remove('refresh-flash');
    }, 600);
  },

  showErrorBanner() {
    const banner = document.getElementById('ajax-error-banner');
    if (banner) banner.classList.add('show');
  },

  hideErrorBanner() {
    const banner = document.getElementById('ajax-error-banner');
    if (banner) banner.classList.remove('show');
  }
};

// Initialisation conditionnelle selon la configuration
(function initDashboard() {
  // La variable ajax_interval est passée depuis le template (définie dans base.html ou index.html)
  // Elle sera accessible via une variable globale ou via un attribut data-*

  // Fallback : détection via un élément du DOM
  const ajaxInterval = window.AJAX_REFRESH_INTERVAL || 0;

  if (ajaxInterval > 0) {
    console.log(`[Dashboard] AJAX polling activé (${ajaxInterval}s)`);
    DashboardRefresh.intervalSeconds = ajaxInterval;
    DashboardRefresh.init();
  } else {
    console.log('[Dashboard] AJAX désactivé — rechargement complet toutes les 30min');
    // Fallback au rechargement complet (comportement original)
    setTimeout(() => window.location.reload(), 30 * 60 * 1000);
  }
})();
