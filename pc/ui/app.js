/* Dilmaç desktop GUI (pc/ui). Talks to Python through window.pywebview.api (gui_web.Api); Python sends events
 * with py.events([...]). Opened with ?mock in a normal browser, a fake back end stands in (for layout work). */
'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let api = null;
let S = null;                 // state from Python (gui_web.Api.state)
const ST = {};                // connection statuses by name: {level, txt, data}
let running = false;
let runStarted = 0;
let lastTr = null;
let currentEn = '';

// ------------------------------------------------------------ icons

const ICONS = {
  logo: '<svg viewBox="0 0 32 32"><rect x="4" y="7" width="24" height="18" rx="4" fill="#0b4fa8"/><rect x="8" y="17" width="16" height="3" rx="1.5" fill="#fff"/><rect x="11" y="21.5" width="10" height="2.5" rx="1.25" fill="#a9c8ff"/><text x="16" y="14.5" font-size="7.5" font-weight="800" text-anchor="middle" fill="#fff" font-family="Segoe UI">TR</text></svg>',
  pad: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round"><path d="M7 9h4M9 7v4M15 10h.01M18 9h.01"/><path d="M6.5 5h11a4.5 4.5 0 0 1 4.4 5.5l-1.1 5A3.5 3.5 0 0 1 14.9 17l-1.4-2h-3l-1.4 2a3.5 3.5 0 0 1-5.9-1.5l-1.1-5A4.5 4.5 0 0 1 6.5 5z"/></svg>',
  tv: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/></svg>',
  globe: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 3 2.5 15 0 18M12 3c-2.5 3-2.5 15 0 18"/></svg>',
  box: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/></svg>',
};

function icons(root = document) {
  for (const el of $$('[data-icon]', root)) {
    if (el.dataset.done) continue;
    el.dataset.done = '1';
    el.insertAdjacentHTML('afterbegin', ICONS[el.dataset.icon] || '');
  }
}

// ------------------------------------------------------------ small helpers

function toast(text) {
  const t = $('#toast');
  t.textContent = text;
  t.classList.add('on');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove('on'), 2600);
}

/** A message box; resolves true on OK. cancel=false: only an OK button. */
function ask(title, text, { ok = 'Tamam', cancel = 'Vazgeç' } = {}) {
  return new Promise((resolve) => {
    $('#dialog-title').textContent = title;
    $('#dialog-text').textContent = text;
    $('#dialog-ok').textContent = ok;
    $('#dialog-cancel').textContent = cancel || '';
    $('#dialog-cancel').classList.toggle('hidden', !cancel);
    $('#dialog').classList.remove('hidden');
    const done = (v) => {
      $('#dialog').classList.add('hidden');
      $('#dialog-ok').onclick = $('#dialog-cancel').onclick = null;
      resolve(v);
    };
    $('#dialog-ok').onclick = () => done(true);
    $('#dialog-cancel').onclick = () => done(false);
  });
}

function dotClass(level) {
  return { ok: 'ok', warn: 'warn', bad: 'bad', busy: 'busy' }[level] || '';
}

function tvLabel(kind) {
  return (S && S.tv_labels && S.tv_labels[kind]) || 'TV';
}

/** What the TV is called: its own name, or "LG TV" / "Samsung TV" / "Android TV". */
function tvName(kind, name) {
  if (name) return name;
  const label = tvLabel(kind || 'android');
  return /TV$/.test(label) ? label : label + ' TV';
}

function engineOf(cfg) {
  return cfg.google_only ? 'google' : ['gemini', 'local'].includes(cfg.engine) ? cfg.engine : 'opus';
}

/** The engine choice on the computer step: highlighted button and the Gemini key row. */
function renderSegEngine(p) {
  const eng = engineOf(S.cfg);
  for (const b of $$('#engine-seg button', p)) b.classList.toggle('on', b.dataset.v === eng);
  $('#gemini-row', p).classList.toggle('hidden', eng !== 'gemini');
}

async function refreshState() {
  S = await api.state();
  for (const [name, st] of Object.entries(S.statuses || {})) ST[name] = st;
  return S;
}

async function save(values, restart = true) {
  const r = await api.save_settings(values, restart);
  if (!r.ok) { await ask('Ayarlar', r.error || 'Kaydedilemedi.', { cancel: false }); return false; }
  S = r.state;
  return r;
}

// ------------------------------------------------------------ events from Python

window.py = {
  events(list) {
    for (const e of list) {
      try { handle(e); } catch (err) { console.error(err); }
    }
  },
};

function handle(e) {
  switch (e.event) {
    case 'log': appendLog(e.line); break;
    case 'status': ST[e.name] = { level: e.level, txt: e.txt, data: e.data || {} }; onStatus(e.name); break;
    case 'tv': ST.tv = { level: e.connected ? 'ok' : 'bad', txt: e.connected ? 'Bağlı' : 'TV bağlantısı yok; günlüğe bak', data: {} }; onStatus('tv'); break;
    case 'en': currentEn = e.txt; $('#now-en').textContent = (e.correction ? '✎ ' : '') + e.txt; break;
    case 'tr': addTranslation(e); break;
    case 'skipped': $('#now-meta').textContent = 'Son satır anlamsız bulundu, gösterilmedi'; break;
    case 'clear': $('#now-tr').style.opacity = '0.45'; break;
    case 'ready': setRunState('Çalışıyor · altyazı bekleniyor', 'ok'); break;
    case 'window': onWindow(e); break;
    case 'error': ask('Dilmaç', e.txt, { cancel: false }); break;
    case 'stopped': setRunning(false); break;
    case 'restarted': setRunning(true); setRunState('Yeni ayarlarla yeniden başlatıldı', 'ok'); break;
    case 'install': onInstall(e); break;
    case 'autostart': if (!running) startStop(); break;
  }
}

function onStatus(name) {
  if (page === 'main') renderCards();
  if (page === 'setup') refreshStep();
}

// ------------------------------------------------------------ pages

let page = '';

function show(name) {
  page = name;
  for (const p of ['setup', 'main', 'settings']) $('#' + p).classList.toggle('hidden', p !== name);
  if (name === 'main') renderMain();
  if (name === 'settings') renderSettings();
}

// ------------------------------------------------------------ setup wizard

const STEPS = [
  { id: 'welcome', title: 'Hoş geldin' },
  { id: 'pc', title: 'Bilgisayar' },
  { id: 'ps5', title: 'PS5' },
  { id: 'tv', title: 'TV' },
  { id: 'area', title: 'Oyun ve altyazı alanı' },
  { id: 'try', title: 'Deneme' },
];
let stepIndex = 0;
const W = { tvs: null, scanning: false, selected: null, check: null, checking: false, installLog: [], installing: false,
  login: false, region: null, tried: null, pairing: false, signingIn: false };

function openSetup(stepId) {
  stepIndex = Math.max(0, STEPS.findIndex((s) => s.id === stepId));
  show('setup');
  renderStep();
}

function renderStep() {
  const step = STEPS[stepIndex];
  $('#step-list').innerHTML = STEPS.map((s, i) =>
    `<li class="${i === stepIndex ? 'on' : ''} ${i < stepIndex ? 'done' : ''}" data-i="${i}"><span class="n">${i < stepIndex ? '✓' : i + 1}</span>${esc(s.title)}</li>`).join('');
  for (const li of $$('#step-list li')) li.onclick = () => { stepIndex = Number(li.dataset.i); renderStep(); };
  $('#step-count').textContent = `Adım ${stepIndex + 1} / ${STEPS.length}`;
  $('#step-bar').style.width = `${((stepIndex + 1) / STEPS.length) * 100}%`;
  $('#back').style.visibility = stepIndex === 0 && !(S && S.setup_done) ? 'hidden' : 'visible';
  $('#back').textContent = stepIndex === 0 ? 'Ana ekrana dön' : 'Geri';
  $('#skip').classList.toggle('hidden', step.id === 'welcome');
  $('#next').textContent = stepIndex === STEPS.length - 1 ? 'Bitir' : 'Devam';
  $('#step-panel').innerHTML = '';
  STEP_UI[step.id].render($('#step-panel'));
  $('#step-panel').scrollTop = 0;
  refreshStep();
}

function refreshStep() {
  if (page !== 'setup') return;
  const step = STEPS[stepIndex];
  const ui = STEP_UI[step.id];
  if (ui.refresh) ui.refresh($('#step-panel'));
  $('#next').disabled = ui.ready ? !ui.ready() : false;
}

async function nextStep() {
  const ui = STEP_UI[STEPS[stepIndex].id];
  if (ui.leave && (await ui.leave()) === false) return;
  if (stepIndex === STEPS.length - 1) {
    S = await api.finish_setup();
    show('main');
    toast('Kurulum tamam. Oyunu açınca "Çeviriyi başlat"a bas.');
    return;
  }
  stepIndex++;
  renderStep();
}

function checkRow(id, title) {
  return `<div class="check" id="chk-${id}"><span class="dot"></span><div class="grow"><div class="t">${esc(title)}</div><div class="d"></div></div><div class="row act"></div></div>`;
}

function setCheck(panel, id, level, text, actions = []) {
  const row = $('#chk-' + id, panel);
  if (!row) return;
  $('.dot', row).className = 'dot ' + dotClass(level);
  $('.d', row).textContent = text || '';
  const act = $('.act', row);
  const key = actions.map((a) => a[0]).join('|');
  if (act.dataset.key === key) return;
  act.dataset.key = key;
  act.innerHTML = '';
  for (const [label, fn, primary] of actions) {
    const b = document.createElement('button');
    b.className = 'small' + (primary ? ' primary' : '');
    b.textContent = label;
    b.onclick = fn;
    act.appendChild(b);
  }
}

async function adminFix(action, title, text) {
  if (await ask(title, text)) {
    const r = await api.fix(action);
    if (r && r.error) ask(title, r.error, { cancel: false });
  }
}

// The OCR row offers "Kur" only when installing would help: not while it runs, not when only a restart is left.
function ocrNeedsInstall(ocr) {
  const d = (ocr && ocr.data) || {};
  return ocr && ocr.level !== 'ok' && ocr.level !== 'unknown' && !d.busy && !d.restart;
}

function engineActions(engine) {
  const d = engine ? engine.data || {} : {};
  if (d.kind === 'opus' && d.model === false && !d.busy) return [['Modeli indir', () => adminFix('opus_download', 'Çeviri modeli', 'Bilgisayarda çalışan ücretsiz çeviri modeli (OPUS-MT, yaklaşık 475 MB) bir kez indirilecek. Devam edilsin mi?'), true]];
  if (d.kind === 'local' && d.running === false) return [['Ollama\'yı başlat', async () => { const r = await api.fix('ollama_start'); if (r.error) ask('Ollama', r.error, { cancel: false }); }, true]];
  if (d.kind === 'local' && d.model === false && !d.busy) return [['Modeli indir', () => adminFix('ollama_download', 'Modeli indir', `'${S.cfg.local_model}' modeli indirilecek (birkaç GB olabilir). Devam edilsin mi?`), true]];
  return [];
}

const STEP_UI = {
  welcome: {
    render(p) {
      p.innerHTML = `
        <h1>Dilmaç'a hoş geldin</h1>
        <p class="lead">Oyunun İngilizce altyazısını okuyup Türkçeye çevirir ve TV'de, İngilizcenin tam üstünde gösterir.
        Kurulum birkaç dakika sürer; her adımda ne yapacağın burada yazar.</p>
        <div class="cards2">
          <div class="card"><h3>Gerekenler</h3><ul class="steps-text dim">
            <li>PS5 ve TV açık, bu bilgisayarla aynı ev ağında</li>
            <li>Android TV / Google TV, LG (2018 ve sonrası) ya da Samsung (2017 ve sonrası) TV</li>
            <li>Oyunu oynarken bu bilgisayar açık kalır</li></ul></div>
          <div class="card"><h3>Nasıl çalışır</h3><ol class="steps-text dim">
            <li>Bilgisayar PS5'in görüntüsünü alır, altyazıyı okur</li>
            <li>Çevirir (bilgisayardaki model, Gemini ya da Google)</li>
            <li>TV'deki Dilmaç uygulaması Türkçesini İngilizcenin üstüne yazar</li></ol></div>
        </div>`;
    },
  },

  pc: {
    render(p) {
      p.innerHTML = `
        <h1>Bilgisayarı hazırlayalım</h1>
        <p class="lead">Yazı tanıma ve çeviri bu bilgisayarda çalışır. Kırmızı olanların yanındaki düğmeye bas; gerisini uygulama yapar.</p>
        ${checkRow('ocr', 'İngilizce yazı tanıma (Windows)')}
        ${checkRow('network', 'Ağ profili')}
        <div class="check" id="chk-engine"><span class="dot"></span><div class="grow"><div class="t">Çeviri motoru</div>
          <div class="seg mt" id="engine-seg"><button data-v="opus">Bilgisayarda (ücretsiz)</button><button data-v="local">Yapay zekâ (Ollama)</button><button data-v="gemini">Gemini</button><button data-v="google">Google Çeviri</button></div>
          <div class="d mt"></div>
          <div class="row mt hidden" id="gemini-row"><input type="password" id="gemini-key" class="grow" placeholder="Gemini API anahtarı"><button class="small" id="gemini-save">Kaydet</button><a id="gemini-link">Anahtar al</a></div>
        </div><div class="row act"></div></div>`;
      for (const b of $$('#engine-seg button', p)) b.onclick = async () => { await save({ engine: b.dataset.v }, false); api.check_now(); renderSegEngine(p); };
      $('#gemini-key', p).value = S.cfg.gemini_api_key || '';
      $('#gemini-save', p).onclick = async () => { await save({ gemini_api_key: $('#gemini-key', p).value.trim() }, false); toast('Anahtar kaydedildi'); };
      $('#gemini-link', p).onclick = () => api.open_url('https://aistudio.google.com/apikey');
      renderSegEngine(p);
    },
    refresh(p) {
      const ocr = ST.ocr || { level: 'unknown', txt: 'Denetleniyor…' };
      setCheck(p, 'ocr', ocr.level, ocr.txt, !ocrNeedsInstall(ocr) ? [] : [['Kur', () => adminFix('ocr_install', 'Yazı tanıma paketi', 'Windows\'un İngilizce yazı tanıma paketi kurulacak. Windows yönetici izni isteyecek; kurulum bitince buraya dön.'), true]]);
      const net = ST.network || { level: 'unknown', txt: 'Denetleniyor…', data: {} };
      setCheck(p, 'network', net.level, net.txt, (net.data && net.data.common && net.data.common.length) ? [['Özel yap', () => adminFix('network_private', 'Ağ profili', 'Ağ "Ortak" olarak işaretli; Windows bu durumda PS5 ve TV ile konuşmayı engelleyebilir. Ağ "Özel" yapılacak (ev ağı için doğru seçim). Yönetici izni istenecek.'), true]] : []);
      const eng = ST.engine || { level: 'unknown', txt: 'Denetleniyor…' };
      setCheck(p, 'engine', eng.level, eng.txt, engineActions(eng));
    },
    ready: () => !ST.ocr || ST.ocr.level === 'ok',
  },

  ps5: {
    render(p) {
      p.innerHTML = `
        <h1>PS5'i bağla</h1>
        <p class="lead">PS5'i aç. Bilgisayar PS5'e doğrudan bağlanır ve görüntüyü kendisi alır; istersen PS Remote Play uygulamasının penceresini de kullanabilirsin.</p>
        <div class="check"><span class="dot" id="ps5-dot"></span><div class="grow"><div class="t" id="ps5-title">PS5 aranıyor…</div><div class="d" id="ps5-sub"></div></div><button class="small" id="ps5-search">Yeniden ara</button></div>
        <div class="seg mt" id="conn-seg"><button data-v="direct">Doğrudan PS5 (önerilen)</button><button data-v="window">Remote Play penceresi</button></div>
        <div id="direct-box" class="mt2">
          <h3>1. PSN hesabı</h3>
          <p class="dim">Bilgisayar PS5'e bir PSN hesabıyla bağlanır. İkinci, ücretsiz bir hesap önerilir: oyunu kendi hesabınla oynamaya devam edersin. Şifren yalnız Sony'nin sayfasına gider.</p>
          <div class="row"><button id="psn-login" class="primary">PSN ile giriş yap</button><span id="psn-acc" class="dim"></span></div>
          <h3 class="mt2">2. PS5'teki kod</h3>
          <p class="dim">PS5'te bu hesapla <b>Ayarlar › Sistem › Uzaktan Oynatma › Cihazı Bağla</b>'yı aç ve ekrandaki 8 haneli kodu yaz. Resmi Remote Play uygulaması açıksa önce kapat.</p>
          <div class="pin" id="pin">${'<input type="text" maxlength="1" inputmode="numeric">'.repeat(8)}</div>
          <div class="row mt"><button id="pair" class="primary">Eşle</button><span id="pair-msg" class="dim"></span></div>
        </div>
        <div id="window-box" class="mt2">
          <p class="dim">PS Remote Play uygulamasıyla PS5'e bağlan; oynarken pencere açık ve görünür kalmalı (küçültme).</p>
          <div class="check"><span class="dot" id="rp-dot"></span><div class="grow"><div class="t">PS Remote Play</div><div class="d" id="rp-sub"></div></div><button class="small" id="rp-open">Aç</button><button class="small" id="rp-dl">İndir</button></div>
        </div>`;
      $('#ps5-search', p).onclick = async () => { $('#ps5-title', p).textContent = 'PS5 aranıyor…'; ST.ps5 = await api.ps5_find(); refreshStep(); };
      for (const b of $$('#conn-seg button', p)) b.onclick = async () => { await save({ connection: b.dataset.v }, false); api.check_now(); refreshStep(); };
      const boxes = $$('#pin input', p);
      boxes.forEach((inp, i) => {
        inp.oninput = () => { inp.value = inp.value.replace(/\D/g, '').slice(-1); if (inp.value && boxes[i + 1]) boxes[i + 1].focus(); };
        inp.onkeydown = (ev) => {
          if (ev.key === 'Backspace' && !inp.value && boxes[i - 1]) boxes[i - 1].focus();
          if (ev.key === 'Enter') $('#pair', p).click();
        };
        inp.onpaste = (ev) => {
          const digits = (ev.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 8);
          if (digits) { ev.preventDefault(); boxes.forEach((b, k) => { b.value = digits[k] || ''; }); }
        };
      });
      $('#psn-login', p).onclick = async () => {
        W.signingIn = true; refreshStep();
        $('#psn-acc', p).textContent = 'Sony giriş penceresi açıldı; giriş bitince kendiliğinden kapanır…';
        const r = await api.psn_login();
        W.signingIn = false;
        if (r.ok) { await refreshState(); toast(`Hesap eklendi: ${r.name}`); } else ask('PSN girişi', r.error, { cancel: false });
        refreshStep();
      };
      $('#pair', p).onclick = async () => {
        const pin = boxes.map((b) => b.value).join('');
        if (pin.length !== 8) { $('#pair-msg', p).textContent = '8 haneli kodu yaz.'; return; }
        W.pairing = true; refreshStep();
        $('#pair-msg', p).textContent = 'PS5\'le eşleniyor…';
        const r = await api.pair_ps5(pin);
        W.pairing = false;
        if (r.ok) { await refreshState(); $('#pair-msg', p).textContent = ''; toast(`PS5 eşlendi: ${r.name}`); } else $('#pair-msg', p).textContent = r.error;
        refreshStep();
      };
      $('#rp-open', p).onclick = async () => { const r = await api.fix('remote_play_open'); if (r.error) ask('Remote Play', r.error, { cancel: false }); };
      $('#rp-dl', p).onclick = () => api.fix('remote_play_download');
    },
    refresh(p) {
      const st = ST.ps5;
      const data = (st && st.data) || {};
      $('#ps5-dot', p).className = 'dot ' + (st ? dotClass(st.level) : 'busy');
      $('#ps5-title', p).textContent = !st ? 'PS5 aranıyor…' : data.ip ? `PS5 bulundu · ${data.name || 'PS5'}` : 'PS5 bulunamadı';
      $('#ps5-sub', p).textContent = !st ? '' : data.ip ? `${data.ip}${st.level === 'warn' ? ' · bekleme modunda (bağlanınca uyanır)' : ''}` : 'PS5 açık ve bu bilgisayarla aynı ağda mı?';
      const direct = (S.cfg.connection || 'window') === 'direct';
      for (const b of $$('#conn-seg button', p)) b.classList.toggle('on', (b.dataset.v === 'direct') === direct);
      $('#direct-box', p).classList.toggle('hidden', !direct);
      $('#window-box', p).classList.toggle('hidden', direct);
      const accounts = S.accounts || [];
      $('#psn-login', p).disabled = W.signingIn;
      $('#psn-login', p).textContent = accounts.length ? 'Başka hesapla giriş yap' : 'PSN ile giriş yap';
      $('#psn-login', p).className = accounts.length ? '' : 'primary';
      if (!W.signingIn) $('#psn-acc', p).innerHTML = accounts.length ? `<span style="color:var(--ok)">✓</span> Hesap: ${esc(accounts.join(', '))}` : '';
      $('#pair', p).disabled = W.pairing || !accounts.length;
      if (S.paired && !W.pairing) $('#pair-msg', p).innerHTML = `<span style="color:var(--ok)">✓ Eşli: ${esc(S.paired)}</span>`;
      const rp = ST.rp || { level: 'unknown', txt: 'Denetleniyor…', data: {} };
      $('#rp-dot', p).className = 'dot ' + dotClass(rp.level);
      $('#rp-sub', p).textContent = rp.txt;
      $('#rp-open', p).classList.toggle('hidden', !(rp.data && rp.data.installed));
      $('#rp-dl', p).classList.toggle('hidden', !(rp.data && rp.data.installed === false));
    },
    ready: () => ((S.cfg.connection || 'window') === 'direct' ? !!S.paired : (ST.rp && ST.rp.level === 'ok')),
  },

  tv: {
    render(p) {
      p.innerHTML = `
        <h1>TV'ni bağla</h1>
        <p class="lead">Aynı ağdaki TV'ler aranıyor. TV'ni seç; markasına göre yapılacakları aşağıda adım adım gösteririm.</p>
        <div class="row"><b>Ağdaki TV'ler</b><span class="dim" id="scan-state"></span><div class="grow"></div><button class="small" id="scan">Yeniden ara</button></div>
        <div class="tv-list mt" id="tv-list"></div>
        <div class="row mt dim"><span>Listede yok mu? IP adresini yaz:</span><input type="text" id="manual-ip" placeholder="192.168.1.20" style="width:150px">
          <div class="seg" id="manual-kind"><button data-v="android" class="on">Android TV</button><button data-v="lg">LG</button><button data-v="samsung">Samsung</button></div>
          <button class="small" id="manual-add">Seç</button></div>
        <div id="tv-panel" class="mt2"></div>`;
      $('#scan', p).onclick = () => scanTvs();
      let manualKind = 'android';
      for (const b of $$('#manual-kind button', p)) b.onclick = () => { manualKind = b.dataset.v; for (const o of $$('#manual-kind button', p)) o.classList.toggle('on', o === b); };
      $('#manual-add', p).onclick = () => {
        const ip = $('#manual-ip', p).value.trim();
        if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) { ask('TV', '192.168.1.20 gibi bir IP adresi yaz (http:// ve port olmadan).', { cancel: false }); return; }
        chooseTv({ ip, kind: manualKind, label: tvLabel(manualKind), name: '' });
      };
      if (S.cfg.tv_ip && !W.selected) W.selected = { ip: S.cfg.tv_ip, kind: S.cfg.tv_type || 'android', name: S.cfg.tv_name || '', label: tvLabel(S.cfg.tv_type) };
      renderTvList();
      renderTvPanel();
      if (!W.tvs && !W.scanning) scanTvs();
    },
    refresh() { renderTvPanelDynamic(); },
    ready: () => !!(W.check && W.check.status.level === 'ok') || (ST.tv && ST.tv.level === 'ok' && S.cfg.tv_ip),
  },

  area: {
    render(p) {
      p.innerHTML = `
        <h1>Oyun ve altyazı alanı</h1>
        <p class="lead">Oyunu seç: sözlüğü ve hazır çevirileri olan oyunlarda adlar korunur, çeviri daha hızlı olur. Sonra altyazının ekranda nerede çıktığını fareyle çiz (GTA VI için hazır alan yeterli).</p>
        <div class="field"><span>Oyun</span><input type="text" id="game" list="games" placeholder="ör. GTA VI"></div>
        <datalist id="games">${(S.games || []).map((g) => `<option value="${esc(g)}">`).join('')}</datalist>
        <div id="area-editor" class="mt"></div>`;
      $('#game', p).value = S.cfg.game || '';
      W.region = regionEditor($('#area-editor', p), S.cfg.region || S.default_region, { save: false });
    },
    async leave() {
      await save({ game: $('#game').value.trim() }, false);
      await api.save_region(W.region.get());
      await refreshState();
    },
  },

  try: {
    render(p) {
      p.innerHTML = `
        <h1>Deneyelim</h1>
        <p class="lead">TV'ye bir deneme altyazısı gönderelim. TV'de PS5'in görüntüsü açıkken bak: yazı, oyunun altyazısının çıktığı yerde, koyu bir kutunun içinde görünmeli.</p>
        <div class="row"><button id="send-test" class="primary">TV'ye deneme yazısı gönder</button><span id="test-msg" class="dim"></span></div>
        <div class="field mt2"><span>Yazı boyutu</span><div class="row"><input type="range" id="try-px" min="16" max="80" step="2"><b id="try-px-v"></b><span class="dim">${S.base_font_px} = oyunun altyazısı kadar</span></div></div>
        <div id="seen" class="mt2 hidden"><h3>TV'de yazıyı gördün mü?</h3>
          <div class="row"><button id="seen-yes" class="primary">Evet, gördüm</button><button id="seen-no">Hayır</button></div></div>
        <div id="help" class="card mt2 hidden"></div>`;
      const px = $('#try-px', p);
      px.value = S.font_px;
      $('#try-px-v', p).textContent = px.value;
      px.oninput = () => { $('#try-px-v', p).textContent = px.value; };
      px.onchange = () => save({ font_px: Number(px.value) }, false);
      $('#send-test', p).onclick = async () => {
        $('#send-test', p).disabled = true;
        $('#test-msg', p).textContent = S.cfg.tv_type === 'android' ? 'Gönderiliyor…' : 'Gönderiliyor (uygulama kapalıysa önce TV\'de açılır)…';
        const r = await api.test_tv('', { font_px: Number(px.value) });
        $('#send-test', p).disabled = false;
        $('#test-msg', p).textContent = r.ok ? 'Gönderildi; TV ekranına bak.' : 'Gönderilemedi; TV bağlı değil görünüyor.';
        W.tried = r.ok;
        $('#seen', p).classList.remove('hidden');
        if (!r.ok) showHelp(p);
        refreshStep();
      };
      $('#seen-yes', p).onclick = () => { W.tried = true; nextStep(); };
      $('#seen-no', p).onclick = () => showHelp(p);
    },
  },
};

function showHelp(p) {
  const kind = S.cfg.tv_type || 'android';
  const tips = kind === 'android'
    ? ['TV\'de Dilmaç uygulamasını bir kez aç, sonra PS5\'in HDMI girişine geri dön.',
      'Uygulama güncellendiyse "Üstte gösterme" iznini yeniden vermek gerekebilir: TV adımında "Uygulamayı kur"a yeniden bas.']
    : ['TV\'de Dilmaç uygulaması açık mı? Kumandada OK\'a basınca sol üstte bağlantı bilgisi çıkar.',
      'Görüntü yoksa kumandada ◀ ▶ ile PS5\'in HDMI girişini seç.',
      'Windows güvenlik duvarı izin sorduysa izin ver ya da aşağıdaki düğmeyle kuralı ekle.'];
  tips.push('Bilgisayarın ağ profili "Özel" olmalı (Bilgisayar adımı).');
  const h = $('#help', p);
  h.classList.remove('hidden');
  h.innerHTML = `<h3>Şunlara bak</h3><ul class="steps-text dim">${tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>
    <div class="row">${kind !== 'android' ? '<button class="small" id="help-fw">Güvenlik duvarı izni</button>' : ''}<button class="small" id="help-tv">TV adımına dön</button></div>`;
  if ($('#help-fw', h)) $('#help-fw', h).onclick = () => adminFix('firewall', 'Güvenlik duvarı', 'Windows güvenlik duvarına, TV\'nin bu bilgisayara ev ağından bağlanabilmesi için bir kural eklenecek. Yönetici izni istenecek.');
  $('#help-tv', h).onclick = () => openSetup('tv');
}

// ------------------------------------------------------------ TV step

async function scanTvs() {
  W.scanning = true;
  renderTvList();
  try { W.tvs = await api.scan_tvs(); } catch (e) { W.tvs = []; }
  W.scanning = false;
  renderTvList();
}

function renderTvList() {
  const list = $('#tv-list');
  if (!list) return;
  $('#scan-state').textContent = W.scanning ? ' · aranıyor (birkaç saniye)…' : W.tvs ? ` · ${W.tvs.length} TV bulundu` : '';
  $('#scan').disabled = W.scanning;
  const rows = W.tvs || [];
  if (!rows.length) {
    list.innerHTML = `<div class="dim">${W.scanning ? 'Ağ taranıyor…' : 'TV bulunamadı. TV açık mı? Android TV\'de USB hata ayıklama, LG\'de Developer Mode uygulaması açık olmalı; Samsung kendiliğinden bulunur. IP\'yi elle de yazabilirsin.'}</div>`;
    return;
  }
  list.innerHTML = rows.map((r, i) => `
    <div class="tv ${W.selected && W.selected.ip === r.ip ? 'on' : ''}" data-i="${i}">
      <span class="badge ${r.kind}">${esc(r.label)}</span>
      <div><div class="name">${esc(tvName(r.kind, r.name))}</div><div class="sub">${esc(r.ip)} · ${esc(r.state)}</div></div>
    </div>`).join('');
  for (const el of $$('.tv', list)) el.onclick = () => chooseTv(rows[Number(el.dataset.i)]);
}

async function chooseTv(tv) {
  W.selected = { ip: tv.ip, kind: tv.kind, name: tv.name || '', label: tv.label || tvLabel(tv.kind) };
  W.check = null;
  W.installLog = [];
  renderTvList();
  const r = await api.select_tv(tv.ip, tv.kind, tv.name || '');
  if (r && r.state) S = r.state;
  renderTvPanel();
}

const TV_STEPS = {
  android: () => `<ol class="steps-text">
      <li>TV'de <b>Ayarlar › Sistem › Hakkında</b> (Google TV) ya da <b>Cihaz Tercihleri › Hakkında</b>'da <b>Android TV OS derlemesi</b> satırına 7 kez bas.</li>
      <li><b>Geliştirici seçenekleri › USB hata ayıklama</b>'yı aç (varsa <b>Ağ / Kablosuz hata ayıklama</b>'yı da).</li>
      <li>Aşağıdaki <b>Uygulamayı kur</b>'a bas. TV'de "Hata ayıklamaya izin verilsin mi?" çıkınca <b>Her zaman</b>'ı işaretleyip <b>İzin ver</b>.</li></ol>`,
  lg: () => `<ol class="steps-text">
      <li>Telefondan ya da bilgisayardan <a data-url="https://webostv.developer.lge.com">webostv.developer.lge.com</a> adresinde ücretsiz bir LG hesabı aç.</li>
      <li>TV'de LG Content Store'dan <b>Developer Mode</b> uygulamasını kur, aç ve bu hesapla giriş yap.</li>
      <li><b>Dev Mode Status</b>'u aç; TV yeniden başlar.</li>
      <li>Developer Mode uygulamasını yeniden aç, <b>Key Server</b>'ı aç ve uygulamayı ekranda bırak.</li>
      <li>Sol altta yazan 6 karakterli <b>Passphrase</b>'i aşağıya yaz (büyük/küçük harf önemli) ve <b>Kur</b>'a bas.</li></ol>
      <div class="row"><input type="text" id="passphrase" placeholder="Passphrase" style="width:160px;font-size:18px"></div>`,
  samsung: (pc) => `<ol class="steps-text">
      <li>TV'de <b>Uygulamalar</b> ekranını aç ve kumandada sırayla <b>1 2 3 4 5</b>'e bas.</li>
      <li><b>Developer mode</b>'u aç ve <b>Host PC IP</b>'ye bu bilgisayarın adresini yaz: <span class="big-ip selectable">${esc(pc)}</span></li>
      <li>TV'yi tam yeniden başlat: güç tuşunu Samsung logosu görünene kadar (~5 sn) basılı tut. Yalnız kapatıp açmak yetmez.</li>
      <li><b>Kur</b>'a bas. İlk seferde tarayıcıda Samsung giriş sayfası açılır; giriş yap (hesabın yoksa orada ücretsiz açabilirsin).</li></ol>`,
};

function renderTvPanel() {
  const panel = $('#tv-panel');
  if (!panel) return;
  const sel = W.selected;
  if (!sel) { panel.innerHTML = ''; return; }
  const web = sel.kind !== 'android';
  panel.innerHTML = `
    <div class="card">
      <div class="row"><span class="badge tv ${sel.kind}" style="padding:6px 10px;border:none;cursor:default">${esc(sel.label)}</span>
        <b>${esc(tvName(sel.kind, sel.name))}</b><span class="dim">${esc(sel.ip)}</span><div class="grow"></div>
        <span class="dot" id="tv-dot"></span><span id="tv-state" class="dim">Denetleniyor…</span></div>
      <div class="items" id="tv-items"></div>
      ${TV_STEPS[sel.kind](S.pc_ip || '?')}
      <div class="row mt"><button id="tv-install" class="primary">${sel.kind === 'android' ? 'Uygulamayı kur' : 'Kur'}</button>
        <button id="tv-cancel-login" class="hidden">Girişi iptal et</button>
        ${web ? '<div class="grow"></div><span class="dim">HDMI girişi</span><select id="tv-hdmi"><option value="0">Otomatik</option><option>1</option><option>2</option><option>3</option><option>4</option></select><button class="small" id="tv-fw">Güvenlik duvarı izni</button>' : ''}</div>
      <div class="logbox mt ${W.installLog.length ? '' : 'hidden'}" id="tv-log"></div>
      ${web ? `<p class="faint mt">TV, PS5'in düğmesiyle kendi HDMI girişine geçip uygulamayı arkaya atıyorsa ${sel.kind === 'lg' ? 'SIMPLINK (HDMI-CEC)' : 'Anynet+ (HDMI-CEC) ve otomatik kaynak değiştirmeyi'} kapat.</p>` : ''}
    </div>`;
  for (const a of $$('[data-url]', panel)) a.onclick = () => api.open_url(a.dataset.url);
  $('#tv-install').onclick = installTv;
  $('#tv-cancel-login').onclick = () => api.cancel_login();
  if ($('#tv-hdmi')) {
    $('#tv-hdmi').value = String(S.cfg.tv_hdmi || 0);
    $('#tv-hdmi').onchange = () => save({ tv_hdmi: Number($('#tv-hdmi').value) }, false);
    $('#tv-fw').onclick = () => adminFix('firewall', 'Güvenlik duvarı', 'Windows güvenlik duvarına, TV\'nin bu bilgisayara ev ağından bağlanabilmesi için bir kural eklenecek. Yönetici izni istenecek.');
  }
  $('#tv-log').textContent = W.installLog.join('\n');
  renderTvPanelDynamic();
  pollTv();
}

function renderTvPanelDynamic() {
  const c = W.check;
  if (!$('#tv-items')) return;
  if (c) {
    $('#tv-dot').className = 'dot ' + dotClass(c.status.level);
    $('#tv-state').textContent = c.status.txt;
    $('#tv-items').innerHTML = c.items.map((it) => `<div class="item ${it.ok ? 'ok' : ''}"><span class="mark">${it.ok ? '✓' : '•'}</span>${esc(it.txt)}</div>`).join('');
  }
  $('#tv-install').disabled = W.installing;
  $('#tv-install').textContent = W.installing ? 'Kuruluyor…' : (W.selected.kind === 'android' ? 'Uygulamayı kur' : 'Kur');
  $('#tv-cancel-login').classList.toggle('hidden', !W.login);
}

async function pollTv() {
  if (W.checking || !W.selected || page !== 'setup' || STEPS[stepIndex].id !== 'tv') return;
  W.checking = true;
  const sel = W.selected;
  try {
    const c = await api.tv_check(sel.ip, sel.kind);
    if (W.selected === sel) { W.check = c; if (c.pc_ip) S.pc_ip = c.pc_ip; renderTvPanelDynamic(); refreshStep(); }
  } catch (e) { /* keep polling */ }
  W.checking = false;
  setTimeout(pollTv, W.installing ? 2000 : 3500);
}

async function installTv() {
  const sel = W.selected;
  const pass = $('#passphrase') ? $('#passphrase').value.trim() : '';
  if (sel.kind === 'lg' && !pass) { ask('LG TV', 'Önce TV\'deki Developer Mode uygulamasının sol altında yazan Passphrase\'i yaz.', { cancel: false }); return; }
  W.installing = true;
  W.installLog = [];
  $('#tv-log').classList.remove('hidden');
  $('#tv-log').textContent = '';
  renderTvPanelDynamic();
  const r = await api.install_tv(pass);
  W.installing = false;
  W.login = false;
  if (r.error) onInstall({ line: r.error });
  onInstall({ line: r.ok ? '✓ Tamam: TV hazır.' : '✗ Kurulum bitmedi; yukarıdaki satırlara bak.' });
  renderTvPanelDynamic();
  refreshStep();
}

function onInstall(e) {
  if (e.login !== undefined) W.login = !!e.login;
  if (e.line) {
    W.installLog.push(e.line);
    const box = $('#tv-log');
    if (box) { box.classList.remove('hidden'); box.textContent = W.installLog.join('\n'); box.scrollTop = box.scrollHeight; }
  }
  if ($('#tv-cancel-login')) $('#tv-cancel-login').classList.toggle('hidden', !W.login);
}

// ------------------------------------------------------------ subtitle area editor

let lastFrame = null;

function regionEditor(root, initial, { save: withSave = true, onSaved = null } = {}) {
  let region = { ...initial };
  root.innerHTML = `
    <div class="region-wrap" id="rw"><img id="rw-img" class="${lastFrame ? '' : 'hidden'}"><div class="empty ${lastFrame ? 'hidden' : ''}">Görüntü yok. Remote Play açıkken "Remote Play'den kare al"a bas ya da bir ekran görüntüsü aç.<br>Kırmızı çerçeve altyazının okunduğu alan; fareyle sürükleyerek yeniden çiz.</div><div class="region-box" id="rw-box"></div></div>
    <div class="row wrap mt"><button class="small" id="rw-grab">Remote Play'den kare al</button><button class="small" id="rw-open">Resim aç…</button><button class="small" id="rw-read">Bu alanı oku</button><button class="small" id="rw-default">Varsayılan alan</button><div class="grow"></div>${withSave ? '<button class="primary" id="rw-save">Alanı kaydet</button>' : ''}</div>
    <div class="dim mt selectable" id="rw-msg"></div>`;
  const wrap = $('#rw', root), box = $('#rw-box', root), msg = $('#rw-msg', root);
  if (lastFrame) $('#rw-img', root).src = lastFrame;
  const draw = () => {
    Object.assign(box.style, { left: region.left * 100 + '%', top: region.top * 100 + '%', width: region.width * 100 + '%', height: region.height * 100 + '%' });
    msg.textContent = `Alan: soldan %${Math.round(region.left * 100)}, üstten %${Math.round(region.top * 100)}, genişlik %${Math.round(region.width * 100)}, yükseklik %${Math.round(region.height * 100)}`;
  };
  const ratio = (ev) => {
    const r = wrap.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height))];
  };
  let origin = null;
  wrap.onmousedown = (ev) => { origin = ratio(ev); ev.preventDefault(); };
  window.addEventListener('mousemove', (ev) => {
    if (!origin) return;
    const [bx, by] = ratio(ev);
    const [ax, ay] = origin;
    if (Math.abs(bx - ax) < 0.02 || Math.abs(by - ay) < 0.02) return;
    region = { left: +Math.min(ax, bx).toFixed(3), top: +Math.min(ay, by).toFixed(3), width: +Math.abs(bx - ax).toFixed(3), height: +Math.abs(by - ay).toFixed(3) };
    draw();
  });
  window.addEventListener('mouseup', () => { origin = null; });
  const setFrame = (r) => {
    if (!r.ok) { if (r.error) msg.textContent = r.error; return; }
    lastFrame = r.img;
    $('#rw-img', root).src = r.img;
    $('#rw-img', root).classList.remove('hidden');
    $('.empty', root).classList.add('hidden');
    draw();
  };
  $('#rw-grab', root).onclick = async () => { msg.textContent = 'Görüntü alınıyor…'; setFrame(await api.grab_frame()); };
  $('#rw-open', root).onclick = async () => setFrame(await api.open_image());
  $('#rw-read', root).onclick = async () => { msg.textContent = 'Okunuyor…'; const r = await api.read_region(region); msg.textContent = r.ok ? r.txt : r.error; };
  $('#rw-default', root).onclick = () => { region = { ...S.default_region }; draw(); };
  if (withSave) $('#rw-save', root).onclick = async () => {
    const r = await api.save_region(region);
    await refreshState();
    toast(r.restarted ? 'Alan kaydedildi; çeviri yeniden başlatılıyor' : 'Alan kaydedildi');
    if (onSaved) onSaved();
  };
  draw();
  return { get: () => ({ ...region }) };
}

function openRegion() {
  const card = $('#region-card');
  card.innerHTML = '<div class="row"><h3 class="grow" style="margin:0">Altyazı alanı</h3><button class="small" id="region-close">Kapat</button></div><p class="dim">Altyazının çıktığı yeri fareyle çiz. Alan, iki satırlık altyazıyı rahatça kapsamalı.</p><div id="region-editor"></div>';
  regionEditor($('#region-editor', card), S.cfg.region || S.default_region, { onSaved: () => $('#region-overlay').classList.add('hidden') });
  $('#region-close', card).onclick = () => $('#region-overlay').classList.add('hidden');
  $('#region-overlay').classList.remove('hidden');
}

// ------------------------------------------------------------ main screen

function renderMain() {
  $('#version').textContent = S.version;
  setRunning(running);
  renderCards();
}

function card(id, level, value, sub) {
  const el = $('#card-' + id);
  $('.dot', el).className = 'dot ' + dotClass(level);
  $('.v', el).textContent = value;
  $('.s', el).textContent = sub || '';
}

function renderCards() {
  if (!S) return;
  const cfg = S.cfg;
  const direct = (cfg.connection || 'window') === 'direct';
  const ps5 = ST.ps5 || {};
  const rp = ST.rp || {};
  if (direct) card('ps5', S.paired ? ps5.level : 'bad', S.paired ? (ps5.data && ps5.data.ip ? 'Eşli · ' + (ps5.data.name || 'PS5') : 'Eşli · bulunamadı') : 'Eşlenmedi', ps5.txt || 'Denetleniyor…');
  else card('ps5', rp.level, 'Remote Play penceresi', rp.txt || 'Denetleniyor…');
  const tv = ST.tv || {};
  card('tv', cfg.tv_ip ? tv.level : 'bad', cfg.tv_ip ? tvName(cfg.tv_type, cfg.tv_name) : 'TV seçilmedi', cfg.tv_ip ? (tv.txt || 'Denetleniyor…') : 'Kurulumdaki TV adımından bağla');
  const eng = ST.engine || {};
  const names = { opus: 'Bilgisayarda', local: 'Yapay zekâ (Ollama)', gemini: 'Gemini', google: 'Google Çeviri' };
  const engineName = names[engineOf(cfg)] || 'Çeviri';
  card('engine', eng.level, engineName + (lastTr && lastTr.ms ? ` · ${(lastTr.ms / 1000).toFixed(1).replace('.', ',')} sn` : ''), eng.txt || 'Denetleniyor…');
  const r = cfg.region || S.default_region;
  card('game', cfg.game ? 'ok' : 'warn', cfg.game || 'Oyun seçilmedi', `Altyazı alanı: üstten %${Math.round(r.top * 100)}, yükseklik %${Math.round(r.height * 100)} · düzenle`);
  renderProblems();
}

function renderProblems() {
  const cfg = S.cfg;
  const direct = (cfg.connection || 'window') === 'direct';
  const rows = [];
  const add = (level, title, text, actions = []) => rows.push({ level, title, text, actions });
  const ocr = ST.ocr, net = ST.network, eng = ST.engine, tv = ST.tv, ps5 = ST.ps5, rp = ST.rp;
  if (ocr && ocr.level !== 'ok') add(ocr.level, 'Yazı tanıma', ocr.txt, ocrNeedsInstall(ocr) ? [['Kur', () => adminFix('ocr_install', 'Yazı tanıma paketi', 'Windows\'un İngilizce yazı tanıma paketi kurulacak. Yönetici izni istenecek; kurulum birkaç dakika sürer, bitince bu satır yeşile döner.')]] : []);
  if (eng && eng.level === 'bad') add(eng.level, 'Çeviri motoru', eng.txt, engineActions(eng));
  else if (eng && eng.data && eng.data.model === false) add('warn', 'Çeviri motoru', eng.txt, engineActions(eng));
  if (!cfg.tv_ip) add('bad', 'TV', 'TV seçilmedi', [['TV\'yi bağla', () => openSetup('tv')]]);
  else if (tv && tv.level !== 'ok' && !running) add(tv.level, 'TV', tv.txt, [['TV adımı', () => openSetup('tv')]]);
  if (direct && !S.paired) add('bad', 'PS5', 'PS5 eşlenmedi', [['Eşle', () => openSetup('ps5')]]);
  else if (direct && ps5 && ps5.level === 'bad') add('warn', 'PS5', ps5.txt, [['Yeniden ara', async () => { ST.ps5 = await api.ps5_find(); renderCards(); }]]);
  if (!direct && rp && rp.level !== 'ok') {
    const acts = rp.data && rp.data.installed ? [['Remote Play\'i aç', () => api.fix('remote_play_open')]] : [['İndir', () => api.fix('remote_play_download')]];
    acts.push(['Doğrudan bağlan', () => openSetup('ps5')]);
    add(rp.level, 'PS Remote Play', rp.txt, acts);
  }
  if (net && net.data && net.data.common && net.data.common.length) add('warn', 'Ağ profili', net.txt, [['Özel yap', () => adminFix('network_private', 'Ağ profili', 'Ağ "Özel" yapılacak (ev ağı için doğru seçim). Yönetici izni istenecek.')]]);
  const box = $('#problems');
  if (!rows.length) {
    box.innerHTML = `<div class="problem"><span class="dot ok"></span><div class="grow"><b>${running ? 'Her şey yolunda' : 'Her şey hazır'}</b><span>${running ? 'Çeviri çalışıyor; altyazılar TV\'de görünür.' : 'Oyunu aç ve "Çeviriyi başlat"a bas.'}</span></div><button class="small" id="test-tv">TV'de dene</button></div>`;
    $('#test-tv').onclick = async () => { const r = await api.test_tv(); toast(r.ok ? 'Deneme yazısı gönderildi; TV\'ye bak' : 'Gönderilemedi; TV bağlı değil'); };
    return;
  }
  box.innerHTML = '<h3>Yapılacaklar</h3>' + rows.map((r, i) => `<div class="problem"><span class="dot ${dotClass(r.level)}"></span><div class="grow"><b>${esc(r.title)}</b><span>${esc(r.text)}</span></div><div class="row" data-i="${i}"></div></div>`).join('');
  rows.forEach((r, i) => {
    const holder = $(`[data-i="${i}"]`, box);
    for (const [label, fn] of r.actions) {
      const b = document.createElement('button');
      b.className = 'small';
      b.textContent = label;
      b.onclick = fn;
      holder.appendChild(b);
    }
  });
}

function setRunning(on) {
  running = on;
  const b = $('#startstop');
  b.disabled = false;
  b.className = 'big ' + (on ? 'danger' : 'primary');
  b.textContent = on ? '■  Çeviriyi durdur' : '▶  Çeviriyi başlat';
  $('#live-dot').className = 'dot ' + (on ? 'ok' : '');
  if (on && !runStarted) runStarted = Date.now();
  if (!on) { runStarted = 0; $('#live-time').textContent = ''; setRunState(''); }
  if (page === 'main') renderProblems();
}

function setRunState(text, level) {
  const el = $('#run-state');
  el.textContent = text || '';
  el.style.color = level === 'ok' ? 'var(--ok)' : level === 'warn' ? 'var(--warn)' : 'var(--dim)';
}

function onWindow(e) {
  if (e.direct) {
    if (e.status === 'connected') setRunState('Çalışıyor · PS5\'ten görüntü geliyor', 'ok');
    else if (e.status === 'lost') setRunState('PS5 bağlantısı koptu, yeniden deneniyor' + (e.error ? ': ' + String(e.error).slice(0, 60) : ''), 'warn');
    else setRunState('Çalışıyor · PS5\'e bağlanılıyor', 'warn');
  } else {
    setRunState(e.was_found ? 'Çalışıyor · altyazı bekleniyor' : 'Çalışıyor · Remote Play penceresi bekleniyor', e.was_found ? 'ok' : 'warn');
  }
}

async function startStop() {
  const b = $('#startstop');
  if (running) {
    b.disabled = true;
    b.textContent = 'Durduruluyor…';
    await api.stop();
    return;
  }
  const r = await api.start();
  if (!r.ok) { ask('Başlat', r.error, { cancel: false }); return; }
  setRunning(true);
  setRunState(engineOf(S.cfg) === 'local' ? 'Başlatılıyor… (model ilk açılışta 1-2 dk yüklenebilir)' : 'Başlatılıyor…', 'warn');
}

setInterval(() => {
  if (!runStarted) return;
  const s = Math.floor((Date.now() - runStarted) / 1000);
  $('#live-time').textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}, 1000);

function addTranslation(e) {
  const pack = /paket|pack|hazır/i.test(e.source || '');
  if (e.track !== 'objective') {
    const now = $('#now-tr');
    now.classList.remove('idle');
    now.style.opacity = '1';
    now.textContent = e.txt;
    $('#now-meta').textContent = [e.source, e.ms ? `${e.ms} ms` : ''].filter(Boolean).join(' · ');
    lastTr = e;
    if (page === 'main') renderCards();
  }
  const h = document.createElement('div');
  h.className = 'h';
  h.innerHTML = `<div class="tr">${e.track === 'objective' ? '<span class="faint">Görev · </span>' : ''}${esc(e.txt)}</div><div class="en"><span>${esc(e.track === 'objective' ? '' : currentEn)}</span><span class="src ${pack ? 'pack' : ''}">${esc(e.source || '')}</span></div>`;
  const list = $('#history');
  list.prepend(h);
  while (list.children.length > 60) list.lastChild.remove();
}

function appendLog(line) {
  const pre = $('#log');
  const cls = /\[TR|\[geri/.test(line) ? 'tr' : /\[EN/.test(line) ? 'en' : /error|hata|başarısız|ulaşılamadı|bağlanılamadı/i.test(line) ? 'err' : '';
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = line + '\n';
  pre.appendChild(span);
  while (pre.childNodes.length > 1500) pre.firstChild.remove();
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

// ------------------------------------------------------------ settings

function renderSettings() {
  const c = S.cfg;
  const eng = engineOf(c);
  $('#settings-note').textContent = '';
  $('#settings-body').innerHTML = `
    <div class="card"><h3>Oyun ve çeviri</h3>
      <label class="field"><span>Oyun</span><input type="text" id="s-game" list="s-games"></label>
      <datalist id="s-games">${(S.games || []).map((g) => `<option value="${esc(g)}">`).join('')}</datalist>
      <div class="field"><span>Çeviri motoru</span><div class="seg" id="s-engine"><button data-v="opus">Bilgisayarda</button><button data-v="local">Ollama</button><button data-v="gemini">Gemini</button><button data-v="google">Google</button></div></div>
      <label class="field"><span>Ollama modeli</span><input type="text" id="s-local_model"></label>
      <label class="field"><span>Gemini anahtarı</span><div class="row"><input type="password" id="s-gemini_api_key" class="grow"><button class="small" id="s-show-key">Göster</button></div></label>
      <label class="field"><span>Gemini modeli</span><input type="text" id="s-gemini_model"></label>
    </div>
    <div class="card"><h3>TV</h3>
      <div class="field"><span>TV</span><div class="row"><b>${esc(c.tv_ip ? tvName(c.tv_type, c.tv_name) : 'Seçilmedi')}</b><span class="dim">${esc(c.tv_ip || '')}</span><div class="grow"></div><button class="small" id="s-change-tv">TV'yi değiştir</button></div></div>
      <label class="field ${c.tv_type === 'android' ? 'hidden' : ''}"><span>HDMI girişi</span><select id="s-tv_hdmi"><option value="0">Otomatik</option><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
      <div class="field"><span>Yazı boyutu</span><div class="row"><input type="range" id="s-font_px" min="16" max="80" step="2"><b id="s-font_px-v"></b><button class="small" id="s-try">TV'de dene</button></div></div>
      <div class="hint field" style="margin-top:-8px"><span></span><span class="faint">${S.base_font_px} = oyunun altyazısı kadar; yazı sabit kalır</span></div>
      <div class="field"><span>Kutu opaklığı</span><div class="row"><input type="range" id="s-box_opacity" min="50" max="100"><b id="s-box_opacity-v"></b></div></div>
      <label class="field"><span>Geçiş (ms)</span><input type="number" id="s-fade_ms" min="0" max="600" step="20" style="width:110px"></label>
      <label class="field"><span>Eski satır (sn)</span><input type="number" id="s-old_line_seconds" min="0" max="8" step="0.5" style="width:110px"></label>
      <label class="field"><span>Kalma süresi (sn)</span><input type="number" id="s-linger_seconds" min="1" max="10" step="0.5" style="width:110px"></label>
    </div>
    <div class="card"><h3>Altyazılar</h3>
      <label class="toggle"><input type="checkbox" id="s-show_objectives"><span>Görev yazıları<small>"Go to the docks" gibi görevleri ayrı kutuda çevir</small></span></label>
      <label class="toggle"><input type="checkbox" id="s-split_blocks"><span>Üst üste altyazılar<small>NPC bağırışı gibi üst üste binen altyazıları ayrı ayrı, her birini kendi kutusunda çevir</small></span></label>
      <label class="toggle"><input type="checkbox" id="s-speaker_colors"><span>Konuşan rengi<small>Paketteki satırlarda yazı konuşanın renginde: Lucia pembe, Jason mavi, diğerleri sarı</small></span></label>
      <label class="toggle"><input type="checkbox" id="s-voice_recognition"><span>Sesten tanıma<small>Doğrudan PS5'te oyunun sesini de al, konuşanı sesinden tanı. TV'nin sesi kesilirse kapat.</small></span></label>
      <label class="toggle"><input type="checkbox" id="s-share_lines"><span>Ortak havuza katkı<small>Hiçbir pakette olmayan satırların yalnız İngilizce metni paket sunucusuna gider; geceleri çevrilip herkesin paketine eklenir. Görüntü, ses ya da hesap bilgisi gitmez.${S.pack_server ? '' : ' (Paket sunucusu henüz ayarlı değil.)'}</small></span></label>
    </div>
    <div class="card"><h3>Bağlantı ve gelişmiş</h3>
      <div class="field"><span>Görüntü kaynağı</span><div class="seg" id="s-connection"><button data-v="direct">Doğrudan PS5</button><button data-v="window">Remote Play penceresi</button></div></div>
      <label class="field"><span>PS5 IP adresi</span><input type="text" id="s-ps5_ip" style="width:170px"></label>
      <label class="toggle"><input type="checkbox" id="s-save_frames"><span>Kare kaydı<small>Çevrilen satırların görüntüsünü pc\\frames klasörüne kaydet (yazı tanıma ayarı için)</small></span></label>
      <div class="row mt"><button class="small" id="s-region">Altyazı alanını düzenle</button><button class="small" id="s-setup">Kurulumu yeniden aç</button></div>
    </div>`;
  const val = (id, v) => { const el = $('#s-' + id); if (el) el.value = v ?? ''; };
  val('game', c.game); val('local_model', c.local_model); val('gemini_api_key', c.gemini_api_key); val('gemini_model', c.gemini_model);
  val('tv_hdmi', String(c.tv_hdmi || 0)); val('font_px', S.font_px); val('box_opacity', Math.round((c.box_opacity ?? 0.97) * 100));
  val('fade_ms', c.fade_ms ?? 180); val('old_line_seconds', c.old_line_seconds ?? 3); val('linger_seconds', c.linger_seconds ?? 2.5); val('ps5_ip', c.ps5_ip);
  for (const k of ['show_objectives', 'split_blocks', 'speaker_colors', 'voice_recognition', 'share_lines']) $('#s-' + k).checked = c[k] !== false;
  $('#s-save_frames').checked = !!c.save_frames;
  const seg = (id, v) => { for (const b of $$(`#s-${id} button`)) { b.classList.toggle('on', b.dataset.v === v); b.onclick = () => { for (const o of $$(`#s-${id} button`)) o.classList.toggle('on', o === b); }; } };
  seg('engine', eng);
  seg('connection', c.connection || 'window');
  const live = (id, fmt) => { const el = $('#s-' + id); const out = () => { $(`#s-${id}-v`).textContent = fmt(el.value); }; el.oninput = out; out(); };
  live('font_px', (v) => v);
  live('box_opacity', (v) => '%' + v);
  $('#s-show-key').onclick = () => { const k = $('#s-gemini_api_key'); k.type = k.type === 'password' ? 'text' : 'password'; };
  $('#s-change-tv').onclick = () => openSetup('tv');
  $('#s-region').onclick = openRegion;
  $('#s-setup').onclick = () => openSetup('welcome');
  $('#s-try').onclick = async () => {
    const px = Number($('#s-font_px').value);
    const r = await api.test_tv(`Bu ${px} punto. Lu, yavaşla biraz, polisler peşimizde!`, { font_px: px, box_opacity: Number($('#s-box_opacity').value) / 100, fade_ms: Number($('#s-fade_ms').value) });
    toast(r.ok ? `${px} punto örnek yazı TV'ye gönderildi` : 'Gönderilemedi; TV bağlı değil');
  };
}

async function saveSettings() {
  const segVal = (id) => { const on = $(`#s-${id} button.on`); return on ? on.dataset.v : undefined; };
  const num = (id) => Number($('#s-' + id).value);
  const values = {
    game: $('#s-game').value.trim(), engine: segVal('engine'), local_model: $('#s-local_model').value.trim(),
    gemini_api_key: $('#s-gemini_api_key').value.trim(), gemini_model: $('#s-gemini_model').value.trim(),
    tv_hdmi: num('tv_hdmi'), font_px: num('font_px'), box_opacity: num('box_opacity') / 100, fade_ms: num('fade_ms'),
    old_line_seconds: num('old_line_seconds'), linger_seconds: num('linger_seconds'), connection: segVal('connection'),
    ps5_ip: $('#s-ps5_ip').value.trim(),
  };
  for (const k of ['show_objectives', 'split_blocks', 'speaker_colors', 'voice_recognition', 'save_frames', 'share_lines']) values[k] = $('#s-' + k).checked;
  if (values.engine === 'gemini' && !values.gemini_api_key) { ask('Ayarlar', 'Gemini seçili ama anahtar yok. Anahtarı gir ya da başka bir motor seç.', { cancel: false }); return; }
  const r = await save(values, true);
  if (r) $('#settings-note').textContent = `Kaydedildi (${new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' })}).${r.restarted ? ' Çeviri yeniden başlatılıyor…' : ''}`;
}

// ------------------------------------------------------------ start

async function boot() {
  api = window.pywebview && window.pywebview.api ? window.pywebview.api : mockApi();
  icons();
  await refreshState();
  running = S.running;
  $('#back').onclick = () => { if (stepIndex === 0) show('main'); else { stepIndex--; renderStep(); } };
  $('#next').onclick = nextStep;
  $('#skip').onclick = () => { if (stepIndex === STEPS.length - 1) nextStep(); else { stepIndex++; renderStep(); } };
  $('#startstop').onclick = startStop;
  $('#open-setup').onclick = () => openSetup('welcome');
  $('#open-settings').onclick = () => show('settings');
  $('#settings-back').onclick = () => show('main');
  $('#settings-save').onclick = saveSettings;
  $('#card-ps5').onclick = () => openSetup('ps5');
  $('#card-tv').onclick = () => openSetup('tv');
  $('#card-engine').onclick = () => show('settings');
  $('#card-game').onclick = openRegion;
  $('#open-log').onclick = () => api.fix('open_log');
  $('#clear-log').onclick = () => { $('#log').innerHTML = ''; };
  if (S.setup_done) show('main'); else openSetup('welcome');
  if (api.ui_ready) api.ui_ready(page);
}

let booted = false;
function bootOnce() { if (!booted) { booted = true; boot(); } }
window.addEventListener('pywebviewready', bootOnce);
window.addEventListener('DOMContentLoaded', () => {
  if (window.pywebview && window.pywebview.api) bootOnce();
  else if (/mock/.test(location.search)) bootOnce();
});

// ------------------------------------------------------------ fake back end (?mock, for layout work in a browser)

function mockApi() {
  const cfg = { game: 'GTA VI', engine: 'opus', local_model: 'gemma4:e4b-it-qat', gemini_api_key: '', gemini_model: 'gemini-3.5-flash-lite',
    tv_ip: /fresh/.test(location.search) ? '' : '192.168.1.59', tv_type: 'lg', tv_hdmi: 0, tv_name: 'Salon TV', ps5_ip: '192.168.1.61', connection: 'direct',
    box_opacity: 0.97, fade_ms: 180, old_line_seconds: 3, linger_seconds: 2.5, show_objectives: true, split_blocks: false,
    speaker_colors: true, voice_recognition: true, save_frames: false, region: { left: 0.08, top: 0.7, width: 0.84, height: 0.27 } };
  const wait = (ms, v) => new Promise((r) => setTimeout(() => r(v), ms));
  const state = () => ({ version: '0.8', cfg, font_px: 32, setup_done: !/fresh/.test(location.search), running: false, statuses: {},
    paired: 'PS5-002 (ikinci hesap)', accounts: ['ikinci hesap'], games: ['GTA VI'], default_region: { left: 0.08, top: 0.7, width: 0.84, height: 0.27 },
    pc_ip: '192.168.1.55', tv_labels: { android: 'Android TV', lg: 'LG', samsung: 'Samsung' }, base_font_px: 32 });
  setTimeout(() => window.py.events([
    /noocr/.test(location.search)
      ? { event: 'status', name: 'ocr', level: 'bad', txt: 'İngilizce yazı tanıma paketi kurulu değil', data: {} }
      : { event: 'status', name: 'ocr', level: 'ok', txt: 'İngilizce yazı tanıma kurulu', data: {} },
    { event: 'status', name: 'network', level: 'ok', txt: 'Özel ağ', data: { common: [] } },
    { event: 'status', name: 'engine', level: 'warn', txt: 'Çeviri modeli indirilmedi (475 MB, bir kez)', data: { kind: 'opus', model: false } },
    { event: 'status', name: 'ps5', level: 'ok', txt: 'Açık · PS5-002 (192.168.1.61) · Grand Theft Auto VI', data: { ip: '192.168.1.61', name: 'PS5-002' } },
    { event: 'status', name: 'rp', level: 'ok', txt: 'Gerekmiyor: PS5\'e doğrudan bağlanılıyor', data: { window: true } },
    { event: 'status', name: 'tv', level: 'ok', txt: 'LG geliştirici modu açık (192.168.1.59) · uygulama çeviri başlayınca açılır', data: {} },
    { event: 'log', line: '12:00:01 Oyun sözlüğü: GTA VI (52 ad)' },
  ]), 300);
  const lines = [['Don\'t move! Don\'t fucking move!', 'Kıpırdama! Sakın kıpırdama!', 'paket'], ['Hey! Police!', 'Hey! Polis!', 'paket'], ['We need to get out of here, Lucia.', 'Buradan çıkmamız lazım, Lucia.', 'yerel model']];
  let timer = null;
  return {
    state: async () => state(),
    save_settings: async (v) => { Object.assign(cfg, v); return { ok: true, restarted: false, state: state() }; },
    finish_setup: async () => state(),
    start: async () => {
      let i = 0;
      timer = setInterval(() => { const [en, tr, src] = lines[i++ % lines.length]; window.py.events([{ event: 'en', txt: en }, { event: 'tr', txt: tr, ms: 380, source: src, track: 'speech' }, { event: 'log', line: `12:00:0${i} [TR ${src}] ${tr}` }]); }, 2500);
      setTimeout(() => window.py.events([{ event: 'ready' }, { event: 'window', direct: true, status: 'connected' }]), 800);
      return { ok: true };
    },
    stop: async () => { clearInterval(timer); setTimeout(() => window.py.events([{ event: 'stopped' }]), 400); return { ok: true }; },
    check_now: async () => {},
    fix: async (action) => {
      if (action === 'ocr_install') {
        window.py.events([{ event: 'status', name: 'ocr', level: 'busy', txt: 'Kuruluyor… (birkaç dakika sürebilir, açılan pencereyi kapatma)', data: { busy: true } }]);
        setTimeout(() => window.py.events([{ event: 'status', name: 'ocr', level: 'ok', txt: 'İngilizce yazı tanıma kurulu', data: {} }]), 6000);
      }
      return { ok: true };
    },
    open_url: async () => {},
    ps5_find: async () => wait(500, { level: 'ok', txt: 'Açık · PS5-002', data: { ip: '192.168.1.61', name: 'PS5-002' } }),
    psn_login: async () => wait(800, { ok: true, name: 'ikinci hesap' }),
    pair_ps5: async () => wait(800, { ok: true, name: 'PS5-002' }),
    scan_tvs: async () => wait(1200, [
      { ip: '192.168.1.59', kind: 'lg', label: 'LG', name: '', state: 'Geliştirici modu açık' },
      { ip: '192.168.1.70', kind: 'samsung', label: 'Samsung', name: 'Samsung Q80 (55)', state: 'Geliştirici modu kapalı' },
      { ip: '192.168.1.65', kind: 'android', label: 'Android TV', name: '', state: 'Dilmaç uygulaması açık' }]),
    tv_check: async (ip, kind) => wait(300, { pc_ip: '192.168.1.55', status: { level: kind === 'samsung' ? 'bad' : 'ok', txt: kind === 'samsung' ? 'Geliştirici modu kapalı' : 'Hazır', data: {} },
      items: kind === 'samsung' ? [{ ok: true, txt: 'TV ağda bulundu: Samsung Q80 (55)' }, { ok: false, txt: 'Developer mode açık' }, { ok: false, txt: 'Host PC IP: — (bu bilgisayar 192.168.1.55)' }, { ok: false, txt: 'Samsung sertifikası alındı, uygulama kuruldu' }]
        : [{ ok: true, txt: 'TV ağda bulundu' }, { ok: true, txt: 'Developer Mode açık' }, { ok: true, txt: 'Key Server açık' }, { ok: true, txt: 'Uygulama kuruldu' }] }),
    select_tv: async (ip, kind, name) => { Object.assign(cfg, { tv_ip: ip, tv_type: kind, tv_name: name }); return { ok: true, state: state() }; },
    install_tv: async () => { for (const l of ['TV\'nin anahtarı alınıyor…', 'Paket TV\'ye gönderiliyor (14 KB)…', 'TV\'de kuruluyor…']) { await wait(500); window.py.events([{ event: 'install', line: l }]); } return { ok: true }; },
    cancel_login: async () => {},
    test_tv: async () => wait(500, { ok: true }),
    grab_frame: async () => ({ ok: false, error: 'Remote Play penceresi bulunamadı.' }),
    open_image: async () => ({ ok: false }),
    read_region: async () => wait(400, { ok: true, txt: 'Okunan: Don\'t move! Don\'t fucking move!' }),
    save_region: async (r) => { cfg.region = r; return { ok: true }; },
  };
}
