// Fond animé : thèmes choisis dans /config (section Apparence), ordinateur uniquement.
// base.html décide si le fond est actif (classe bg-live sur <html>) ; ce fichier construit le calque.
// Signaux communs à tous les thèmes : braises quand le poêle tourne, halo rouge qui respire en
// Tempo rouge, étoile filante la nuit. Jour Tempo rouge (data-tempo-red) : le thème « Jour rouge »
// passe devant le thème choisi ; gyrophare pendant les heures pleines, les plus chères.
(function () {
  const root = document.documentElement;
  // Mêmes conditions que base.html : ordinateur, animations autorisées
  const ELIGIBLE = window.matchMedia('(hover: hover) and (pointer: fine) and (min-width: 1024px)').matches
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let chosen = root.dataset.bgTheme || 'aurora';
  let started = false;
  const LAT = parseFloat(root.dataset.lat);
  const LON = parseFloat(root.dataset.lon);
  const MIN = 60000;

  let scene = null;      // conteneur du thème courant
  let current = null;    // thème affiché (résolu si « auto »)
  let sky = null;        // { sky, is_day } d'Open-Meteo

  const repeat = (n, html) => Array.from({ length: n }, (_, i) => html(i)).join('');
  const rnd = (a, b) => a + Math.random() * (b - a);

  // ── Lever / coucher du soleil (formules de SunCalc) ─────────────
  function sunTimes(date, lat, lon) {
    const rad = Math.PI / 180, dayMs = 86400000, J1970 = 2440588, J2000 = 2451545, J0 = 0.0009;
    const d = date.valueOf() / dayMs - 0.5 + J1970 - J2000;
    const lw = rad * -lon, phi = rad * lat, e = rad * 23.4397;
    const n = Math.round(d - J0 - lw / (2 * Math.PI));
    const ds = J0 + lw / (2 * Math.PI) + n;
    const M = rad * (357.5291 + 0.98560028 * ds);
    const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    const L = M + C + rad * 102.9372 + Math.PI;
    const dec = Math.asin(Math.sin(L) * Math.sin(e));
    const transit = j => J2000 + j + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * L);
    const w = Math.acos((Math.sin(-0.833 * rad) - Math.sin(phi) * Math.sin(dec)) / (Math.cos(phi) * Math.cos(dec)));
    const noon = transit(ds);
    const set = transit(J0 + (w + lw) / (2 * Math.PI) + n);
    const fromJ = j => new Date((j + 0.5 - J1970) * dayMs);
    return { rise: fromJ(noon - (set - noon)), set: fromJ(set) };
  }

  function sunState(now = new Date()) {
    if (isNaN(LAT) || isNaN(LON)) {
      const h = now.getHours();
      return { phase: h >= 22 || h < 6 ? 'night' : h < 8 ? 'dawn' : h >= 18 ? 'dusk' : 'day', p: (h - 6) / 16 };
    }
    const { rise, set } = sunTimes(now, LAT, LON);
    const t = now.getTime();
    let phase = 'day';
    if (t < rise - 50 * MIN || t > set + 50 * MIN) phase = 'night';
    else if (t < rise + 40 * MIN) phase = 'dawn';
    else if (t > set - 40 * MIN) phase = 'dusk';
    if (t >= rise && t <= set) return { phase, p: (t - rise) / (set - rise), body: 'sun' };
    // Nuit : progression du coucher au lever suivant (ou depuis le coucher de la veille)
    const prevSet = t < rise ? sunTimes(new Date(t - 86400000), LAT, LON).set : set;
    const nextRise = t < rise ? rise : sunTimes(new Date(t + 86400000), LAT, LON).rise;
    return { phase, p: (t - prevSet) / (nextRise - prevSet), body: 'moon' };
  }

  // ── Thèmes ──────────────────────────────────────────────────────
  const THEMES = {
    aurora: () => '<div class="bg-aurora"><i class="aur aur-1"></i><i class="aur aur-2"></i><i class="aur aur-3"></i></div>',

    sky: () => '<div class="sky-tint"></div><i class="sky-body"></i>',

    weather: () => {
      const kind = sky ? sky.sky : 'cloudy';
      const night = sky ? !sky.is_day : root.hasAttribute('data-night');
      const clouds = n => repeat(n, i => `<i class="wx-bg-cloud" style="top:${4 + i * 9}%;--d:${70 + i * 18}s;--t:-${i * 23}s;--s:${0.7 + (i % 3) * 0.25}"></i>`);
      const fall = (n, cls) => repeat(n, () =>
        `<i class="${cls}" style="left:${rnd(0, 100).toFixed(1)}%;--d:${rnd(0.7, 1.3).toFixed(2)};--t:-${rnd(0, 10).toFixed(2)}s;--x:${rnd(-40, 40).toFixed(0)}px"></i>`);
      let inner = '';
      if (kind === 'rain' || kind === 'storm') inner = clouds(5) + fall(60, 'wx-bg-drop') + (kind === 'storm' ? '<div class="wx-bg-flash"></div>' : '');
      else if (kind === 'snow') inner = clouds(5) + fall(70, 'wx-bg-flake');
      else if (kind === 'fog') inner = '<i class="wx-bg-fog f1"></i><i class="wx-bg-fog f2"></i><i class="wx-bg-fog f3"></i>';
      else if (kind === 'cloudy') inner = clouds(5);
      else if (kind === 'partly') inner = (night ? '<i class="wx-bg-moon"></i>' : '<div class="wx-bg-rays"></div>') + clouds(3);
      else inner = night ? '<i class="wx-bg-moon"></i>' : '<div class="wx-bg-rays"></div>';
      return `<div class="wx-bg wx-bg-${kind}${night ? ' is-night' : ''}">${inner}</div>`;
    },

    thermal: () => '<div class="th-glow"></div><div class="th-field">' + repeat(34, i =>
      `<i style="left:${(i * 2.95 + rnd(-1, 1)).toFixed(1)}%;--d:${rnd(9, 16).toFixed(1)}s;--t:-${rnd(0, 16).toFixed(1)}s;--x:${rnd(-60, 60).toFixed(0)}px;--z:${rnd(0.6, 1.4).toFixed(2)}"></i>`) + '</div>',

    'tempo-red': () => '<div class="red-glow"></div><div class="red-veils"><i class="red-veil v1"></i><i class="red-veil v2"></i></div>' +
      '<div class="red-beacon"></div><div class="red-sparks">' + repeat(22, i =>
      `<i style="left:${(i * 4.5 + rnd(0, 3)).toFixed(1)}%;--d:${rnd(8, 14).toFixed(1)}s;--t:-${rnd(0, 14).toFixed(1)}s;--x:${rnd(-50, 50).toFixed(0)}px"></i>`) + '</div>',

    fireside: () => '<div class="fire-glow"></div><div class="fire-glow fire-glow-2"></div><div class="fire-vignette"></div>',
  };

  // Signaux communs : braises (poêle ou coin du feu), chaleur, alerte Tempo rouge, étoile filante
  const COMMON = '<div class="bg-heat"></div><div class="bg-embers">' + '<i></i>'.repeat(14) +
    '</div><div class="bg-alert"></div><i class="bg-shoot"></i>';

  // ── Thermique : écart entre ressenti et plage de confort ────────
  function applyThermal(t) {
    if (!t || t.felt == null || isNaN(t.felt)) { root.removeAttribute('data-thermal'); return; }
    const state = t.felt < t.on ? 'cold' : t.felt > t.off ? 'hot' : 'ok';
    root.dataset.thermal = state;
    // Plus on s'éloigne de la plage de confort, plus le champ est dense et rapide
    const gap = state === 'cold' ? t.on - t.felt : state === 'hot' ? t.felt - t.off : 0;
    root.style.setProperty('--th-gap', Math.min(gap / 2, 1).toFixed(2));
  }
  window.bgThermal = function (felt, on, off) {
    const t = { felt, on, off };
    try { localStorage.setItem('thermal', JSON.stringify(t)); } catch (e) { /* stockage indisponible */ }
    applyThermal(t);
  };

  // ── Ciel du jour : teinte et astre selon l'heure réelle ─────────
  function updateSky() {
    if (current !== 'sky' || !scene) return;
    const s = sunState();
    root.dataset.skyPhase = s.phase;
    const body = scene.querySelector('.sky-body');
    if (!body) return;
    const p = Math.min(Math.max(s.p, 0), 1);
    body.className = `sky-body is-${s.body || (s.phase === 'night' ? 'moon' : 'sun')}`;
    body.style.left = `${6 + p * 88}%`;
    body.style.top = `${62 - Math.sin(Math.PI * p) * 52}%`;
  }

  // Heures pleines (gyrophare du jour rouge) : plage HP de /config, ex. « 6-22 »
  function updateHp() {
    const [start, end] = (root.dataset.hp || '6-22').split('-').map(Number);
    const h = new Date().getHours();
    const hp = start <= end ? h >= start && h < end : h >= start || h < end;
    if (hp) root.dataset.tempoHp = ''; else delete root.dataset.tempoHp;
  }

  // ── Automatique : coin du feu le soir poêle allumé, météo s'il tombe quelque chose, sinon ciel ──
  function resolve() {
    if (root.hasAttribute('data-tempo-red')) return 'tempo-red';
    if (chosen !== 'auto') return chosen;
    const h = new Date().getHours();
    if (root.dataset.stove === 'on' && (h >= 17 || h < 6)) return 'fireside';
    if (sky && ['rain', 'storm', 'snow', 'fog'].includes(sky.sky)) return 'weather';
    return 'sky';
  }

  function render(force) {
    const theme = resolve();
    if (theme === current && !force) return;
    current = theme;
    root.dataset.bgScene = theme;
    if (theme !== 'sky') delete root.dataset.skyPhase;
    scene.className = `bg-scene bg-scene-${theme}`;
    scene.innerHTML = THEMES[theme] ? THEMES[theme]() : '';
    if (theme === 'sky') updateSky();
  }

  async function loadSky() {
    if (!started || !['weather', 'auto'].includes(chosen)) return;
    try {
      const r = await fetch('/api/weather/conditions');
      if (!r.ok) return;
      const next = await r.json();
      const changed = !sky || next.sky !== sky.sky || next.is_day !== sky.is_day;
      sky = next;
      if (changed) render(current === 'weather');
    } catch (e) { /* hors ligne : on garde l'existant */ }
  }

  function start() {
    started = true;
    const layer = document.createElement('div');
    layer.className = 'bg-live-layer';
    layer.setAttribute('aria-hidden', 'true');
    layer.innerHTML = '<div class="bg-scene"></div>' + COMMON;
    scene = layer.firstChild;
    document.body.prepend(layer);

    try { applyThermal(JSON.parse(localStorage.getItem('thermal'))); } catch (e) { /* rien de mémorisé */ }
    updateHp();
    render(true);
    loadSky();
    setInterval(loadSky, 15 * MIN);
    setInterval(() => { updateHp(); updateSky(); render(); }, MIN);
    // Poêle allumé / éteint, jour rouge (mis à jour par l'accueil) : le thème affiché peut changer
    new MutationObserver(() => render()).observe(root, { attributes: true, attributeFilter: ['data-stove', 'data-tempo-red'] });
    // Onglet en arrière-plan : animations figées
    document.addEventListener('visibilitychange', () => root.classList.toggle('bg-paused', document.hidden));
  }

  // Changement de thème à chaud (/config) : démarre, change ou coupe le fond sans recharger
  window.bgSetTheme = function (theme) {
    chosen = theme;
    root.dataset.bgTheme = theme;
    if (theme === 'none' || !ELIGIBLE) { root.classList.remove('bg-live'); return; }
    root.classList.add('bg-live');
    if (!started) { start(); return; }
    render(true);
    if (!sky) loadSky();
  };

  document.addEventListener('DOMContentLoaded', () => {
    if (root.classList.contains('bg-live')) start();
  });
})();
