// Actualisation manuelle via le bouton — conserve le rechargement complet
function refresh() {
  const btn = document.querySelector('.btn-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ En cours…'; }

  fetch('/api/refresh')
    .then(r => r.json())
    .then(() => window.location.reload())
    .catch(() => window.location.reload());
}

// ── Manual Control Functions ────────────────────────────

async function controlPoele(action) {
  const endpoint = action === 'on' ? '/api/ha/turn_on' : '/api/ha/turn_off';
  const btn = document.getElementById(`btn-poele-${action}`);
  const otherBtn = document.getElementById(`btn-poele-${action === 'on' ? 'off' : 'on'}`);

  if (!btn) return;

  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = action === 'on' ? '⏳ Allumage…' : '⏳ Extinction…';

  try {
    const response = await fetch(`${endpoint}?manual=true`, { method: 'POST' });
    const data = await response.json();

    if (response.ok && data.status === 'ok') {
      showToast(`Poêle ${action === 'on' ? 'allumé' : 'éteint'}`, 'success');
      updatePoeleStatus();
      setTimeout(() => location.reload(), 1500);
    } else {
      showToast(data.error || 'Erreur', 'error');
      btn.textContent = originalText;
      btn.disabled = false;
    }
  } catch (error) {
    console.error('Erreur contrôle poêle:', error);
    showToast('Erreur réseau', 'error');
    btn.textContent = originalText;
    btn.disabled = false;
  }
}

async function controlClim(action) {
  const endpoint = action === 'on' ? '/api/ha/clim/turn_on' : '/api/ha/clim/turn_off';
  const btn = document.getElementById(`btn-clim-${action}`);

  if (!btn) return;

  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = action === 'on' ? '⏳ Allumage…' : '⏳ Extinction…';

  try {
    const response = await fetch(`${endpoint}?manual=true`, { method: 'POST' });
    const data = await response.json();

    if (response.ok && data.status === 'ok') {
      showToast(`Clim ${action === 'on' ? 'allumée' : 'éteinte'}`, 'success');
      updateClimStatus();
      setTimeout(() => location.reload(), 1500);
    } else {
      showToast(data.error || 'Erreur', 'error');
      btn.textContent = originalText;
      btn.disabled = false;
    }
  } catch (error) {
    console.error('Erreur contrôle clim:', error);
    showToast('Erreur réseau', 'error');
    btn.textContent = originalText;
    btn.disabled = false;
  }
}

async function controlRadiateur(entityId, action) {
  const endpoint = `/api/radiateurs/turn_${action}/${encodeURIComponent(entityId)}`;
  const statusDiv = document.querySelector(`[data-entity-id="${entityId}"]`);
  const compact = statusDiv ? statusDiv.closest('.control-compact') : null;
  const buttons = compact ? compact.querySelectorAll('.btn-compact') : [];

  buttons.forEach(btn => btn.disabled = true);

  try {
    const response = await fetch(`${endpoint}?manual=true`, { method: 'POST' });
    const data = await response.json();

    if (response.ok && data.status === 'ok') {
      showToast(`Radiateur ${action === 'on' ? 'allumé' : 'éteint'}`, 'success');
      updateRadiateurStatus(entityId);
    } else {
      showToast(data.error || 'Erreur', 'error');
    }
  } catch (error) {
    console.error('Erreur contrôle radiateur:', error);
    showToast('Erreur réseau', 'error');
  } finally {
    buttons.forEach(btn => btn.disabled = false);
  }
}

async function updatePoeleStatus() {
  try {
    const response = await fetch('/api/ha/state');
    const data = await response.json();

    const icon = document.getElementById('poele-status-icon');
    const btnOn = document.getElementById('btn-poele-on');
    const btnOff = document.getElementById('btn-poele-off');

    if (!icon) return;

    const isOn = data.state === 'heat' || data.state === 'on';

    icon.textContent = isOn ? '🟢' : '⚫';

    if (btnOn) {
      if (isOn) {
        btnOn.classList.add('active');
      } else {
        btnOn.classList.remove('active');
      }
    }

    if (btnOff) {
      if (isOn) {
        btnOff.classList.remove('active');
      } else {
        btnOff.classList.add('active');
      }
    }
  } catch (e) {
    console.warn('Erreur mise à jour état poêle:', e);
  }
}

async function updateClimStatus() {
  try {
    const response = await fetch('/api/ha/clim/state');
    const data = await response.json();

    const icon = document.getElementById('clim-status-icon');
    const btnOn = document.getElementById('btn-clim-on');
    const btnOff = document.getElementById('btn-clim-off');

    if (!icon) return;

    const isOn = data.state === 'heat' || data.state === 'on';

    icon.textContent = isOn ? '🟢' : '⚫';

    if (btnOn) {
      if (isOn) {
        btnOn.classList.add('active');
      } else {
        btnOn.classList.remove('active');
      }
    }

    if (btnOff) {
      if (isOn) {
        btnOff.classList.remove('active');
      } else {
        btnOff.classList.add('active');
      }
    }
  } catch (e) {
    console.warn('Erreur mise à jour état clim:', e);
  }
}

async function updateRadiateurStatus(entityId) {
  try {
    const response = await fetch('/api/radiateurs/status');
    const data = await response.json();

    const statusDiv = document.querySelector(`[data-entity-id="${entityId}"]`);
    if (!statusDiv) return;

    const entity = data.entities.find(e => e.entity_id === entityId);
    if (!entity) return;

    const radiateurs = Array.from(document.querySelectorAll('.control-compact-status[data-entity-id]'));
    const index = radiateurs.findIndex(r => r.getAttribute('data-entity-id') === entityId);

    if (index >= 0) {
      const icon = document.getElementById(`radiateur-${index}-icon`);
      const compact = statusDiv.closest('.control-compact');
      const btnOn = compact ? compact.querySelector('.btn-compact-on') : null;
      const btnOff = compact ? compact.querySelector('.btn-compact-off') : null;

      const isOn = entity.state === 'on' || entity.state === 'heat';

      if (icon) icon.textContent = isOn ? '🟢' : '⚫';

      if (btnOn) {
        if (isOn) {
          btnOn.classList.add('active');
        } else {
          btnOn.classList.remove('active');
        }
      }

      if (btnOff) {
        if (isOn) {
          btnOff.classList.remove('active');
        } else {
          btnOff.classList.add('active');
        }
      }
    }
  } catch (e) {
    console.warn('Erreur mise à jour état radiateur:', e);
  }
}

function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.textContent = message;

  const bgColor = type === 'success' ? '#22c55e' : type === 'error' ? '#ef4444' : '#3b82f6';
  const duration = type === 'error' ? 4000 : 3000;

  toast.style.cssText = `
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    background: ${bgColor};
    color: white;
    padding: 1rem 1.5rem;
    border-radius: 8px;
    box-shadow: 0 4px 24px rgba(0,0,0,0.4);
    z-index: 9999;
    font-size: 0.95rem;
    font-weight: 500;
    animation: slideInUp 0.3s ease-out;
  `;

  document.body.appendChild(toast);

  const removeToast = () => {
    toast.style.animation = 'slideOutDown 0.3s ease-out';
    setTimeout(() => toast.remove(), 300);
  };

  setTimeout(removeToast, duration);
}

// Initialize control panel
(function initControlPanel() {
  if (!document.querySelector('.sidebar-controls')) return;

  // Initial status update
  updatePoeleStatus();
  if (document.getElementById('clim-status')) {
    updateClimStatus();
  }

  document.querySelectorAll('.control-compact-status[data-entity-id]').forEach(r => {
    updateRadiateurStatus(r.getAttribute('data-entity-id'));
  });

  // Poll every 15s
  setInterval(() => {
    updatePoeleStatus();
    if (document.getElementById('clim-status')) {
      updateClimStatus();
    }
    document.querySelectorAll('.control-compact-status[data-entity-id]').forEach(r => {
      updateRadiateurStatus(r.getAttribute('data-entity-id'));
    });
  }, 15000);
})();

// Add CSS animation keyframes if not already present
if (!document.getElementById('control-panel-animations')) {
  const style = document.createElement('style');
  style.id = 'control-panel-animations';
  style.textContent = `
    @keyframes slideInUp {
      from {
        opacity: 0;
        transform: translateY(20px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @keyframes slideOutDown {
      from {
        opacity: 1;
        transform: translateY(0);
      }
      to {
        opacity: 0;
        transform: translateY(20px);
      }
    }
  `;
  document.head.appendChild(style);
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

    // Mise à jour badge Poêle
    const poeleBadgeEl = card.querySelector('[data-value="poele-badge"]');
    if (poeleBadgeEl) {
      const poeleOn = thermostat.state === 'on' && thermostat.active_system === 'poele';
      poeleBadgeEl.className = poeleOn ? 'thermostat-badge-on' : 'thermostat-badge-off';
      poeleBadgeEl.textContent = poeleOn ? '🔥 Allumé' : '⏹ Éteint';
    }

    // Mise à jour badge Clim
    const climBadgeEl = card.querySelector('[data-value="clim-badge"]');
    if (climBadgeEl) {
      const climOn = thermostat.state === 'on' && thermostat.active_system === 'clim';
      climBadgeEl.className = climOn ? 'thermostat-badge-on' : 'thermostat-badge-off';
      climBadgeEl.textContent = climOn ? '❄️ Allumée' : '⏹ Éteinte';
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
