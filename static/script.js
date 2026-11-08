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

// Lignes du panneau (barre latérale + tiroir mobile) pour un appareil : 'poele', 'clim' ou entity_id
function _deviceRows(key) {
  return document.querySelectorAll(`.device[data-device="${CSS.escape(key)}"]`);
}

// Affiche l'état réel d'un appareil : interrupteur, libellé, teinte
function setDeviceState(key, state) {
  const known = state != null && !['unknown', 'unavailable'].includes(state);
  const isOn = state === 'heat' || state === 'on';
  _deviceRows(key).forEach(row => {
    row.classList.toggle('is-on', isOn);
    row.classList.toggle('is-unknown', !known);
    row.querySelector('.device-switch').setAttribute('aria-checked', String(isOn));
    if (!row.classList.contains('is-busy')) {
      const e = row.classList.contains('device-clim') ? 'e' : '';
      row.querySelector('.device-state').textContent = !known ? 'Indisponible' : (isOn ? 'Allumé' : 'Éteint') + e;
    }
  });
  updateAmbiance();
}

function _setDeviceBusy(key, busy, label) {
  _deviceRows(key).forEach(row => {
    row.classList.toggle('is-busy', busy);
    row.querySelector('.device-switch').disabled = busy;
    if (label) row.querySelector('.device-state').textContent = label;
  });
}

// Clic sur un interrupteur : demande l'état inverse de l'état affiché
function toggleDevice(btn) {
  const row = btn.closest('.device');
  const key = row.dataset.device;
  const action = row.classList.contains('is-on') ? 'off' : 'on';
  if (key === 'poele') controlPoele(action);
  else if (key === 'clim') controlClim(action);
  else controlRadiateur(key, action);
}

// Commande manuelle commune : endpoint HA, toast, puis relecture de l'état réel
async function _controlDevice(key, endpoint, action, label, onSuccess) {
  const previous = _deviceRows(key)[0]?.querySelector('.device-state').textContent;
  _setDeviceBusy(key, true, action === 'on' ? 'Allumage…' : 'Extinction…');
  try {
    const response = await fetch(`${endpoint}?manual=true`, { method: 'POST' });
    const data = await response.json();
    if (response.ok && data.status === 'ok') {
      showToast(`${label} ${action === 'on' ? 'allumé' : 'éteint'}${label === 'Clim' ? 'e' : ''}`, 'success');
      _setDeviceBusy(key, false);
      setDeviceState(key, action);
      onSuccess();
      return;
    }
    showToast(data.error || 'Erreur', 'error');
  } catch (error) {
    console.error(`Erreur contrôle ${label}:`, error);
    showToast('Erreur réseau', 'error');
  }
  _setDeviceBusy(key, false, previous);
}

function controlPoele(action) {
  const endpoint = action === 'on' ? '/api/ha/turn_on' : '/api/ha/turn_off';
  return _controlDevice('poele', endpoint, action, 'Poêle', () => {
    updatePoeleStatus();
    setTimeout(() => location.reload(), 1500);
  });
}

function controlClim(action) {
  const endpoint = action === 'on' ? '/api/ha/clim/turn_on' : '/api/ha/clim/turn_off';
  return _controlDevice('clim', endpoint, action, 'Clim', () => {
    updateClimStatus();
    setTimeout(() => location.reload(), 1500);
  });
}

function controlRadiateur(entityId, action) {
  const endpoint = `/api/radiateurs/turn_${action}/${encodeURIComponent(entityId)}`;
  return _controlDevice(entityId, endpoint, action, 'Radiateur', () => updateRadiateurStatus(entityId));
}

function renderPoeleStatus(state) {
  setDeviceState('poele', state);
}

async function updatePoeleStatus() {
  try {
    const response = await fetch('/api/ha/state');
    renderPoeleStatus((await response.json()).state);
  } catch (e) {
    console.warn('Erreur mise à jour état poêle:', e);
  }
}

function renderClimStatus(state) {
  setDeviceState('clim', state);
}

async function updateClimStatus() {
  try {
    const response = await fetch('/api/ha/clim/state');
    renderClimStatus((await response.json()).state);
  } catch (e) {
    console.warn('Erreur mise à jour état clim:', e);
  }
}

function renderRadiateurStatus(entityId, state) {
  setDeviceState(entityId, state);
}

async function updateRadiateurStatus(entityId) {
  try {
    const response = await fetch('/api/radiateurs/status');
    const entity = (await response.json()).entities.find(e => e.entity_id === entityId);
    if (entity) renderRadiateurStatus(entityId, entity.state);
  } catch (e) {
    console.warn('Erreur mise à jour état radiateur:', e);
  }
}

// Interrupteur « Auto le week-end » (radiateur des combles) : sidebar et tiroir mobile restent synchronisés
async function setWeekendAuto(input) {
  const allowed = input.checked;
  const inputs = document.querySelectorAll('.weekend-toggle-input');
  inputs.forEach(i => { i.disabled = true; });
  try {
    const response = await fetch('/api/radiateurs/weekend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allowed }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    inputs.forEach(i => { i.checked = data.allowed; });
    showToast(data.allowed ? 'Gestion automatique autorisée le week-end' : 'Pas de rallumage automatique le week-end', 'success');
  } catch (e) {
    inputs.forEach(i => { i.checked = !allowed; });
    showToast(`Réglage non enregistré : ${e.message}`, 'error');
  } finally {
    inputs.forEach(i => { i.disabled = false; });
  }
}

// Card « Remplissage du poêle » : heures de chauffe depuis le dernier plein + débit réel mesuré
const _fmtH = h => h.toLocaleString('fr-FR', { maximumFractionDigits: 1 });

function _refillMeasureHtml(p) {
  if (!p.measuring) {
    return '<span>Mesure du débit réel : démarre au prochain clic « Trémie remplie ».</span>';
  }
  const since = `${_fmtH(p.refill_hours)} h de chauffe depuis le dernier plein`;
  const e = p.estimate;
  if (!e) {
    return `<span>Mesure du débit réel : <strong>${p.refills_count}/${p.min_cycles}</strong> remplissages · ${since}</span>`;
  }
  const differs = Math.abs(e.hours_per_bag - p.cycle_hours) >= 0.1;
  return `<span>Réel mesuré : <strong>${_fmtH(e.hours_per_bag)} h/sac</strong> (${e.kg_per_hour.toLocaleString('fr-FR')} kg/h)
      sur ${e.cycles} remplissages · ${_fmtH(e.min_hours_per_bag)}–${_fmtH(e.max_hours_per_bag)} h</span>`
    + (differs ? `<button type="button" class="btn-thresholds btn-thresholds-save" onclick="applyPelletEstimate(${e.hours_per_bag})">Appliquer ${_fmtH(e.hours_per_bag)} h</button>` : '');
}

function renderPelletRefill(p) {
  const card = document.getElementById('refill-card');
  if (!card || !p) return;
  const level = p.minutes >= p.alert_hours * 60 ? 'due' : p.percent >= 75 ? 'soon' : 'ok';
  card.classList.remove('refill-ok', 'refill-soon', 'refill-due');
  card.classList.add(`refill-${level}`);
  const hoursEl = card.querySelector('[data-value="refill-hours"]');
  if (hoursEl) hoursEl.textContent = `${Math.floor(p.minutes / 60)} h ${String(p.minutes % 60).padStart(2, '0')}`;
  const cycleEl = hoursEl?.nextElementSibling;
  if (cycleEl) cycleEl.textContent = `/ ${Math.round(p.cycle_hours)} h de chauffe`;
  const fill = card.querySelector('.refill-fill');
  if (fill) fill.style.width = `${p.percent}%`;
  const mark = card.querySelector('.refill-mark');
  if (mark && p.cycle_hours) mark.style.left = `${p.alert_hours / p.cycle_hours * 100}%`;
  const bar = card.querySelector('.refill-bar');
  if (bar) { bar.setAttribute('aria-valuenow', p.hours); bar.setAttribute('aria-valuemax', p.cycle_hours); }
  const note = card.querySelector('[data-value="refill-note"]');
  if (note) note.textContent = `Alerte à ${Math.round(p.alert_hours)} h · jauge bloquée à 100 % jusqu'au remplissage`;
  const measure = card.querySelector('[data-value="refill-measure"]');
  if (measure) measure.innerHTML = _refillMeasureHtml(p);
}

async function _postPellet(url, body, okMessage, errPrefix) {
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    renderPelletRefill(data);
    showToast(okMessage, 'success');
  } catch (e) {
    showToast(`${errPrefix} : ${e.message}`, 'error');
  }
}

async function resetPelletRefill() {
  const btn = document.getElementById('refill-reset');
  const checked = document.querySelector('input[name="refill-bags"]:checked');
  const bags = parseFloat(checked.value);
  const label = bags === 1 ? '1 sac' : `${checked.nextElementSibling.textContent} sac`;
  if (!confirm(`Trémie remplie avec ${label} ? Le compteur d'heures de chauffe repart de 0.`)) return;
  btn.disabled = true;
  await _postPellet('/api/poele/refill-reset', { bags }, 'Remplissage enregistré, compteur remis à 0', 'Remise à 0 impossible');
  btn.disabled = false;
  document.querySelector('input[name="refill-bags"][value="1"]').checked = true;
}

async function applyPelletEstimate(hours) {
  if (!confirm(`Utiliser ${_fmtH(hours)} h par sac ? Le seuil d'alerte, la consommation du poêle et le stock de /stock seront recalculés avec cette valeur.`)) return;
  await _postPellet('/api/poele/refill-apply', {}, `Durée par sac réglée sur ${_fmtH(hours)} h`, 'Réglage non appliqué');
}

document.addEventListener('DOMContentLoaded', () => {
  const card = document.getElementById('refill-card');
  if (card?.dataset.pellet) {
    try { renderPelletRefill(JSON.parse(card.dataset.pellet)); } catch (e) { /* rendu serveur conservé */ }
  }
});

// Met à jour le panneau de contrôle (poêle, clim, radiateurs) depuis la réponse de /api/dashboard/refresh
function updateControlPanel(data) {
  if (data.poele_state != null) renderPoeleStatus(data.poele_state);
  if (data.clim_state != null) renderClimStatus(data.clim_state);
  (data.radiateurs || []).forEach(r => renderRadiateurStatus(r.entity_id, r.state));
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
  if (type === 'success' || type === 'error') toastFx(toast, type === 'success');

  const removeToast = () => {
    toast.style.animation = 'slideOutDown 0.3s ease-out';
    setTimeout(() => toast.remove(), 300);
  };

  setTimeout(removeToast, duration);
}

// Toggle mobile control drawer
function toggleControlDrawer() {
  const drawer = document.getElementById('mobile-control-drawer');
  const overlay = document.getElementById('mobile-drawer-overlay');
  const fab = document.getElementById('mobile-fab');
  if (!drawer) return;

  const isOpen = drawer.classList.contains('open');
  drawer.classList.toggle('open', !isOpen);
  overlay.classList.toggle('open', !isOpen);
  fab.classList.toggle('open', !isOpen);
}

// Initialize control panel
(function initControlPanel() {
  const hasSidebar = document.querySelector('.sidebar-controls');
  const hasDrawer  = document.getElementById('mobile-control-drawer');
  if (!hasSidebar && !hasDrawer) return;

  // Avec le rafraîchissement AJAX, DashboardRefresh met déjà le panneau à jour (un seul appel serveur)
  if ((window.AJAX_REFRESH_INTERVAL || 0) > 0) return;

  async function pollAll() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/dashboard/refresh');
      if (response.ok) updateControlPanel(await response.json());
    } catch (e) {
      console.warn('Erreur mise à jour du panneau de contrôle:', e);
    }
  }

  pollAll();
  setInterval(pollAll, 15000);
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

    // Premier appel immédiat (le panneau de contrôle n'a pas d'état initial rendu côté serveur)
    this.fetchAndUpdate();
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
      updateControlPanel(data);
      renderPelletRefill(data.pellet_refill);
      this.updateTempo(data.tempo);
      this.updateTomorrow(data.tomorrow);
      this.updateTimestamp(data.timestamp);

      // Mise à jour du countdown banner
      if (window.updateCountdownFromDashboard) {
        window.updateCountdownFromDashboard(data);
      }

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
    const gauge = document.getElementById('comfort-gauge');
    if (gauge && indoor) {
      const current = indoor.felt_temperature ?? indoor.temperature;
      gauge.dataset.current = current ?? '';
      renderComfortGauge();
    }
    const dial = document.getElementById('indoor-dial');
    if (dial && indoor) {
      dial.dataset.felt = indoor.felt_temperature ?? indoor.temperature ?? '';
      renderDial();
    }

    const card = document.querySelector('[data-refresh="indoor"]');
    if (!card || !indoor) return;

    const temp = indoor.temperature;
    const humidity = indoor.humidity;
    const felt = indoor.felt_temperature;

    if (temp !== null && temp !== undefined) {
      const tempEl = card.querySelector('[data-value="temperature"]');
      setAnimatedText(tempEl, `${temp.toFixed(1)} °C`);
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
      setAnimatedText(tempEl, `${outdoor.temperature.toFixed(1)} °C`);
      card.classList.toggle('wx-frost', outdoor.temperature < 0);
    }

    if (outdoor.source) {
      const sourceEl = card.querySelector('[data-value="source"]');
      if (sourceEl) {
        const isReal = !!outdoor.measured;
        sourceEl.className = isReal ? 'source-badge source-real' : 'source-badge source-model';
        sourceEl.innerHTML = isReal
          ? `${icon('radio-tower')}${escapeHtml(outdoor.source)} — mesure de ${escapeHtml(outdoor.observed_at)}`
          : `${icon('globe')}${escapeHtml(outdoor.source)} — modèle (fallback)`;
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
      const icons = { clim: 'snowflake', poele: 'flame', none: 'moon' };
      iconEl.innerHTML = icon(icons[rec.system] || 'triangle-alert');
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

    this.markSystem(card, rec.system);
  },

  updateThermostat(thermostat) {
    const card = document.querySelector('[data-refresh="thermostat"]');
    if (!card || !thermostat) return;

    // Mise à jour badge Poêle
    // Carte teintée et « respirante » tant qu'un appareil tourne
    const running = thermostat.state === 'on' && ['poele', 'clim'].includes(thermostat.active_system)
      ? thermostat.active_system : null;
    card.classList.toggle('thermo-running', !!running);
    card.classList.toggle('thermo-poele', running === 'poele');
    card.classList.toggle('thermo-clim', running === 'clim');
    updateAmbiance();

    const poeleBadgeEl = card.querySelector('[data-value="poele-badge"]');
    if (poeleBadgeEl) {
      const poeleOn = thermostat.state === 'on' && thermostat.active_system === 'poele';
      if (poeleOn && poeleBadgeEl.className !== 'thermostat-badge-on') setTimeout(() => playOnce(poeleBadgeEl, 'badge-pop'));
      poeleBadgeEl.className = poeleOn ? 'thermostat-badge-on' : 'thermostat-badge-off';
      poeleBadgeEl.innerHTML = poeleOn ? icon('flame') + 'Allumé' : icon('power') + 'Éteint';
    }

    // Mise à jour badge Clim
    const climBadgeEl = card.querySelector('[data-value="clim-badge"]');
    if (climBadgeEl) {
      const climOn = thermostat.state === 'on' && thermostat.active_system === 'clim';
      if (climOn && climBadgeEl.className !== 'thermostat-badge-on') setTimeout(() => playOnce(climBadgeEl, 'badge-pop'));
      climBadgeEl.className = climOn ? 'thermostat-badge-on' : 'thermostat-badge-off';
      climBadgeEl.innerHTML = climOn ? icon('snowflake') + 'Allumée' : icon('power') + 'Éteinte';
    }

    this.flashElement(card);
  },

  updateRadiateurs(radiateurs) {
    // Pour l'instant, pas de section dédiée dans le HTML avec data-refresh="radiateurs"
    // Cette fonction est prête pour une future utilisation
    console.log('[Dashboard] Radiateurs mis à jour:', radiateurs);
  },

  updateTempo(tempo) {
    // Cartes Tempo non rafraîchies (données cachées 30 min) ; seul le jour rouge du fond suit
    const color = tempo && tempo.today && tempo.today.color;
    if (color) setTempoRed(String(color).toLowerCase() === 'red');
  },

  updateTomorrow(tomorrow) {
    const card = document.querySelector('[data-refresh="tomorrow"]');
    if (!card || !tomorrow) return;

    if (tomorrow.tempo_unknown) {
      // Afficher le message "Tempo inconnu"
      card.innerHTML = `
        <div class="tomorrow-label">DEMAIN</div>
        <div class="rec-icon rec-icon-sm">${icon('circle-help')}</div>
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
        const icons = { clim: 'snowflake', poele: 'flame', none: 'sun' };
        iconEl.innerHTML = icon(icons[rec.system] || 'triangle-alert');
      }

      // Mise à jour du titre
      const titleEl = card.querySelector('[data-value="title"]');
      if (titleEl) titleEl.textContent = rec.title || '';

      // Mise à jour de l'explication
      const explanationEl = card.querySelector('[data-value="explanation"]');
      if (explanationEl) explanationEl.textContent = rec.explanation || '';

      // Mise à jour des classes CSS
      card.className = `card card-${rec.level}${rec.system === 'none' ? ' card-night' : ''} card-tomorrow`;
      this.markSystem(card, rec.system);
      return;
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

  // Mémorise le système recommandé ; s'il change, la carte bascule en animation au lieu du flash
  markSystem(card, system) {
    const previous = card.dataset.system;
    card.dataset.system = system;
    if (!previous || previous === system) { this.flashElement(card); return; }
    card.classList.remove('rec-swap');
    void card.offsetWidth;
    card.classList.add('rec-swap');
    setTimeout(() => card.classList.remove('rec-swap'), 700);
  },

  flashElement(element) {
    if (!element) return;
    // Pas de flash pendant l'entrée des cartes : il couperait leur animation
    if (document.documentElement.classList.contains('motion-enter')) return;
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

// ── Compte à rebours jusqu'à la prochaine évaluation du thermostat ──
(function initNextCheckCountdown() {
  const pill = document.getElementById('next-check');
  if (!pill) return;
  const value = document.getElementById('next-check-value');
  let target = Date.parse(pill.dataset.nextCheck) || null;
  let syncing = false;
  let lastSync = Date.now();

  async function sync() {
    if (syncing) return;
    syncing = true;
    try {
      const response = await fetch('/api/thermostat/state');
      if (response.ok) target = Date.parse((await response.json()).next_check) || null;
    } catch (e) { /* on garde l'ancienne échéance */ }
    lastSync = Date.now();
    syncing = false;
  }

  function render() {
    if (!target) { pill.hidden = true; return; }
    pill.hidden = false;
    const remaining = Math.round((target - Date.now()) / 1000);
    if (remaining <= 0) {
      value.textContent = 'en cours…';
      // Le passage vient de démarrer : la nouvelle échéance est connue quelques secondes après
      if (Date.now() - lastSync > 5000) sync();
      return;
    }
    const m = Math.floor(remaining / 60);
    const sec = String(remaining % 60).padStart(2, '0');
    value.textContent = `${m}:${sec}`;
    // Resynchronisation régulière : relance manuelle, changement d'intervalle dans /config…
    if (Date.now() - lastSync > 5 * 60 * 1000) sync();
  }

  const nowBtn = document.getElementById('next-check-now');
  nowBtn.addEventListener('click', async () => {
    nowBtn.disabled = true;
    nowBtn.classList.add('is-busy');
    try {
      const response = await fetch('/api/thermostat/check-now', { method: 'POST' });
      if (response.ok) {
        // Passage lancé : « en cours… » puis nouvelle échéance relue quelques secondes après
        target = Date.now();
        lastSync = Date.now();
        render();
        showToast('Évaluation du thermostat lancée', 'success');
      } else {
        showToast((await response.json().catch(() => ({}))).error || 'Erreur', 'error');
      }
    } catch (e) {
      showToast('Erreur réseau', 'error');
    } finally {
      setTimeout(() => { nowBtn.disabled = false; nowBtn.classList.remove('is-busy'); }, 5000);
    }
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) sync().then(render); });
  render();
  setInterval(render, 1000);
})();

// ── Seuils du thermostat (widget page principale) ────────────────
const THRESHOLD_LIMITS = { on: [10, 25], off: [10, 30] };

function _thresholdInput(kind) {
  return document.getElementById(`threshold-${kind}`);
}

function _savedThresholds() {
  const card = document.getElementById('thresholds-card');
  return { on: parseFloat(card.dataset.tempOn), off: parseFloat(card.dataset.tempOff) };
}

function _thresholdError(on, off) {
  if (isNaN(on) || isNaN(off)) return 'Valeur invalide';
  if (on < THRESHOLD_LIMITS.on[0] || on > THRESHOLD_LIMITS.on[1]) return `Allumage entre ${THRESHOLD_LIMITS.on[0]} et ${THRESHOLD_LIMITS.on[1]} °C`;
  if (off < THRESHOLD_LIMITS.off[0] || off > THRESHOLD_LIMITS.off[1]) return `Extinction entre ${THRESHOLD_LIMITS.off[0]} et ${THRESHOLD_LIMITS.off[1]} °C`;
  if (on >= off) return 'L\'allumage doit être sous l\'extinction';
  return null;
}

function onThresholdInput() {
  const on = parseFloat(_thresholdInput('on').value);
  const off = parseFloat(_thresholdInput('off').value);
  const saved = _savedThresholds();
  const dirty = on !== saved.on || off !== saved.off;
  const error = _thresholdError(on, off);

  const msg = document.getElementById('thresholds-msg');
  msg.textContent = error || '';
  msg.className = 'thresholds-msg' + (error ? ' thresholds-msg-error' : '');
  document.getElementById('thresholds-save').disabled = !dirty || !!error;
  document.getElementById('thresholds-cancel').hidden = !dirty;
  renderComfortGauge();
}

// Jauge : plage allumage → extinction et température ressentie actuelle
function renderComfortGauge() {
  const gauge = document.getElementById('comfort-gauge');
  if (!gauge) return;
  const on = parseFloat(_thresholdInput('on').value);
  const off = parseFloat(_thresholdInput('off').value);
  if (isNaN(on) || isNaN(off) || on >= off) { gauge.hidden = true; return; }
  gauge.hidden = false;

  const current = parseFloat(gauge.dataset.current);
  const hasCurrent = !isNaN(current);
  const lo = Math.floor(Math.min(on, hasCurrent ? current : on) - 2);
  const hi = Math.ceil(Math.max(off, hasCurrent ? current : off) + 2);
  const pct = v => ((v - lo) / (hi - lo) * 100) + '%';
  const fmt = v => v.toLocaleString('fr-FR', { maximumFractionDigits: 1 }) + '°';

  const band = document.getElementById('cg-band');
  band.style.left = pct(on);
  band.style.right = 'calc(100% - ' + pct(off) + ')';
  band.dataset.on = fmt(on);
  band.dataset.off = fmt(off);

  const marker = document.getElementById('cg-marker');
  marker.hidden = !hasCurrent;
  gauge.classList.toggle('has-marker', hasCurrent);
  if (hasCurrent) {
    // Premier affichage : le curseur part de la gauche et glisse jusqu'au ressenti
    if (!marker.style.left && MOTION_OK) {
      marker.style.left = '0%';
      void marker.offsetWidth;
    }
    marker.style.left = pct(current);
    document.getElementById('cg-marker-label').textContent = 'ressenti ' + fmt(current);
  }

  const scale = document.getElementById('cg-scale');
  scale.innerHTML = '';
  for (let t = lo; t <= hi; t += 2) {
    const tick = document.createElement('span');
    tick.style.left = pct(t);
    tick.textContent = t + '°';
    scale.appendChild(tick);
  }
}
document.addEventListener('DOMContentLoaded', renderComfortGauge);

function stepThreshold(kind, delta) {
  const input = _thresholdInput(kind);
  const [min, max] = THRESHOLD_LIMITS[kind];
  const current = parseFloat(input.value) || _savedThresholds()[kind];
  input.value = Math.min(max, Math.max(min, Math.round((current + delta) * 10) / 10));
  playOnce(input, delta > 0 ? 'nudge-up' : 'nudge-down');
  onThresholdInput();
}

function resetThresholds() {
  const saved = _savedThresholds();
  _thresholdInput('on').value = saved.on;
  _thresholdInput('off').value = saved.off;
  onThresholdInput();
}

async function saveThresholds() {
  const btn = document.getElementById('thresholds-save');
  const on = parseFloat(_thresholdInput('on').value);
  const off = parseFloat(_thresholdInput('off').value);
  btn.disabled = true;
  btn.textContent = 'Enregistrement…';

  try {
    const response = await fetch('/api/thermostat/thresholds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ temp_on: on, temp_off: off }),
    });
    const data = await response.json();

    if (response.ok && data.status === 'ok') {
      const card = document.getElementById('thresholds-card');
      card.dataset.tempOn = data.temp_on;
      card.dataset.tempOff = data.temp_off;
      const dial = document.getElementById('indoor-dial');
      if (dial) { dial.dataset.on = data.temp_on; dial.dataset.off = data.temp_off; renderDial(); }
      _thresholdInput('on').value = data.temp_on;
      _thresholdInput('off').value = data.temp_off;
      document.querySelectorAll('[data-value="temp-on"]').forEach(el => el.textContent = `${data.temp_on}°C`);
      document.querySelectorAll('[data-value="temp-off"]').forEach(el => el.textContent = `${data.temp_off}°C`);
      if (data.clim_setpoint === false) {
        showToast(`Seuils enregistrés, mais consigne clim non transmise (${data.temp_off} °C au prochain allumage)`, 'error');
      } else {
        const climNote = data.clim_setpoint ? ' — consigne clim mise à jour' : '';
        showToast(`Seuils enregistrés : ${data.temp_on} → ${data.temp_off} °C${climNote}`, 'success');
        playOnce(btn, 'btn-pop');
      }
    } else {
      showToast(data.error || 'Erreur', 'error');
    }
  } catch (error) {
    console.error('Erreur sauvegarde seuils:', error);
    showToast('Erreur réseau', 'error');
  } finally {
    btn.textContent = 'Enregistrer';
    onThresholdInput();
  }
}

// ── Habillage de l'accueil : ambiance, cadran, ciel, mini-courbes ─────────

// Ambiance de fond : appareil qui tourne > Tempo rouge > neutre (nuit gérée par base.html).
// Mémorisée pour que les autres pages gardent la même teinte.
function updateAmbiance() {
  if (!document.getElementById('recommendation')) return;  // uniquement sur l'accueil
  const running = key =>
    !!document.querySelector(`.device[data-device="${key}"].is-on`) ||
    !!document.querySelector(`[data-refresh="thermostat"].thermo-running.thermo-${key}`);
  const stove = running('poele');
  const amb = stove ? 'poele' : running('clim') ? 'clim'
    : document.querySelector('.card-tempo-red') ? 'tempo-red' : '';
  const root = document.documentElement;
  if (amb) root.dataset.ambiance = amb; else delete root.dataset.ambiance;
  if (stove) root.dataset.stove = 'on'; else delete root.dataset.stove;
  try { localStorage.setItem('ambiance', amb); } catch (e) { /* stockage indisponible */ }
  if (document.querySelector('[class*="card-tempo-"]')) setTempoRed(!!document.querySelector('.card-tempo-red'));
}

// Jour Tempo rouge : le thème de fond « Jour rouge » passe devant le thème choisi.
// Mémorisé jusqu'au prochain 6 h (changement de jour Tempo) pour les autres pages.
function setTempoRed(red) {
  const root = document.documentElement;
  if (red) root.dataset.tempoRed = ''; else delete root.dataset.tempoRed;
  const until = new Date();
  if (until.getHours() >= 6) until.setDate(until.getDate() + 1);
  until.setHours(6, 0, 0, 0);
  try { localStorage.setItem('tempoRed', JSON.stringify({ red, until: until.getTime() })); } catch (e) { /* stockage indisponible */ }
}

// Cadran intérieur : arc sur 270°, plage de confort (seuils) et repère sur le ressenti
function renderDial() {
  const dial = document.getElementById('indoor-dial');
  if (!dial) return;
  const on = parseFloat(dial.dataset.on);
  const off = parseFloat(dial.dataset.off);
  const felt = parseFloat(dial.dataset.felt);
  const vals = [on, off, felt].filter(v => !isNaN(v));
  const lo = Math.floor(Math.min(14, ...vals.map(v => v - 2)));
  const hi = Math.ceil(Math.max(26, ...vals.map(v => v + 2)));
  const deg = v => Math.min(1, Math.max(0, (v - lo) / (hi - lo))) * 270;
  if (!isNaN(on) && !isNaN(off) && on < off) {
    dial.style.setProperty('--b0', `${deg(on)}deg`);
    dial.style.setProperty('--b1', `${deg(off)}deg`);
  }
  dial.classList.toggle('no-value', isNaN(felt));
  if (window.bgThermal && !isNaN(felt) && !isNaN(on) && !isNaN(off)) window.bgThermal(felt, on, off);
  if (!isNaN(felt)) {
    // Premier affichage : le repère part du début de l'arc
    requestAnimationFrame(() => dial.style.setProperty('--a', `${deg(felt) - 135}deg`));
  }
  dial.querySelector('.dial-min').textContent = `${lo}°`;
  dial.querySelector('.dial-max').textContent = `${hi}°`;
}

// Ciel illustré d'après /api/weather/conditions (code météo Open-Meteo)
async function renderWeatherScene() {
  const scene = document.getElementById('wx-scene');
  if (!scene) return;
  let c;
  try {
    const response = await fetch('/api/weather/conditions');
    if (!response.ok) return;
    c = await response.json();
  } catch (e) {
    return;
  }
  const star = c.is_day ? '<span class="wx-sun"></span>' : '<span class="wx-moon"></span>';
  const cloud = n => n === 2 ? '<span class="wx-cloud c2"></span><span class="wx-cloud c1"></span>' : '<span class="wx-cloud c1"></span>';
  const drops = '<span class="wx-drops">' + '<i></i>'.repeat(6) + '</span>';
  const scenes = {
    clear: star,
    partly: star + cloud(1),
    cloudy: cloud(2),
    fog: '<span class="wx-fog f1"></span><span class="wx-fog f2"></span><span class="wx-fog f3"></span>',
    rain: cloud(2) + drops,
    snow: cloud(2) + drops,
    storm: cloud(2) + drops,
  };
  scene.className = `wx-scene wx-${c.sky}${['rain', 'storm'].includes(c.sky) ? ' wx-dark' : ''}`;
  scene.innerHTML = scenes[c.sky] || '';
}

// Mini-courbes 24 h en filigrane derrière les températures
async function renderSparklines() {
  const svgs = document.querySelectorAll('svg.spark[data-series]');
  if (!svgs.length) return;
  let rows;
  try {
    const response = await fetch('/api/statistics?hours=24');
    if (!response.ok) return;
    rows = await response.json();
  } catch (e) {
    return;
  }
  svgs.forEach((svg, n) => {
    const values = rows.map(r => r[svg.dataset.series]).filter(v => v != null);
    if (values.length < 2) return;
    const min = Math.min(...values), max = Math.max(...values);
    const span = (max - min) || 1;
    // Marge basse franche : au minimum la ligne reste au-dessus du bord arrondi de la carte
    const W = 100, H = 40, PAD_TOP = 4, PAD_BOTTOM = 14;
    const pts = values.map((v, i) => [
      (i / (values.length - 1)) * W,
      H - PAD_BOTTOM - ((v - min) / span) * (H - PAD_TOP - PAD_BOTTOM),
    ].map(x => x.toFixed(2)).join(','));
    const line = 'M' + pts.join(' L');
    const id = `spark-fill-${n}`;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.innerHTML = `
      <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="currentColor" stop-opacity="0.22"/>
        <stop offset="1" stop-color="currentColor" stop-opacity="0"/>
      </linearGradient></defs>
      <path d="${line} L${W},${H} L0,${H} Z" fill="url(#${id})"/>
      <path class="spark-line" d="${line}" stroke="currentColor" stroke-opacity="0.55" pathLength="1"/>`;
    svg.classList.add('is-ready');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  updateAmbiance();
  renderDial();
  renderWeatherScene();
  renderSparklines();
});
