/* Dilmaç TV page: the part shared by LG and Samsung TVs.
 *
 * The PC runs a small HTTP server (pc/tv_web.py). This page connects to it with EventSource (GET /events) and gets one
 * JSON message per event, the same messages the Android TV app gets over TCP:
 *   {"type":"show","id":"speech","text":"...","ttl":10,"new":true,
 *    "box":[x,y,w,h],"lines":1,"lineh":0.029,"opacity":0.97,"scale":1,"max_width":0.8,"old_secs":3,
 *    "color":"#FF9AD5","fade_ms":180}
 *   {"type":"clear"}  or  {"type":"clear","id":"objective"}
 *   {"type":"hello","pc":"NAME","hdmi":0}       sent by the PC when the page connects
 * box: where the English text is in the game picture (0-1); the current line's pill sits exactly on top of it, wide
 * and tall enough to cover it. Every id is its own panel; with "new" the previous line stays dimmed above for
 * "old_secs" seconds. Diagnostic lines go back to the PC (POST /log) and land in its log.
 *
 * The PC's address comes from the launch parameters (the PC starts the app with {"host","port","hdmi"}) and is
 * remembered for the next start. Plain ES5 on purpose: old TV browsers.
 */
(function () {
  'use strict';

  var VERSION = '0.9.0';
  var OLD_SCALE = 0.8;
  var OLD_ALPHA = 0.75;
  var STORE_PC = 'dilmac.pc';
  var STORE_HDMI = 'dilmac.hdmi';
  var STORE_PC_HDMI = 'dilmac.pcHdmi';
  var STORE_EMBEDDED = 'dilmac.embeddedPc';
  var P = window.Platform;

  var layer = document.getElementById('subs');
  var statusBox = document.getElementById('status');
  var panels = {};
  var pc = null;            // {host, port}
  var source = null;        // EventSource
  var connected = false;
  var lastEventAt = 0;
  var statusTimer = null;
  var retryTimer = null;
  var pending = [];         // log lines waiting to be sent to the PC
  var sending = false;

  function now() { return new Date().getTime(); }

  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* storage may be missing */ }
    return null;
  }

  // ------------------------------------------------------------ log to the PC

  function log(msg) {
    msg = String(msg);
    if (window.console) console.log('[Dilmac] ' + msg);
    pending.push(msg);
    if (pending.length > 60) pending.splice(0, pending.length - 60);
    flushLog();
  }

  function flushLog() {
    if (!connected || sending || !pending.length || !pc) return;
    var lines = pending.splice(0, 20);
    sending = true;
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', 'http://' + pc.host + ':' + pc.port + '/log?k=' + encodeURIComponent(pc.key || ''), true);
      // text/plain keeps it a "simple" request: no CORS preflight.
      xhr.setRequestHeader('Content-Type', 'text/plain;charset=UTF-8');
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        sending = false;
        if (xhr.status !== 200) {
          pending = lines.concat(pending);
          return;
        }
        if (pending.length) setTimeout(flushLog, 200);
      };
      xhr.send(JSON.stringify({ platform: P.name, version: VERSION, lines: lines }));
    } catch (e) {
      sending = false;
    }
  }

  // ------------------------------------------------------------ status panel and toast

  function setText(id, html) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function renderStatus() {
    setText('st-version', VERSION);
    var where = pc ? pc.host + ':' + pc.port : 'adres bilinmiyor';
    if (connected) {
      setText('st-pc', '<span class="ok">●</span> Bilgisayara bağlı (' + where + ')');
    } else if (pc) {
      setText('st-pc', '<span class="warn">●</span> Bilgisayar bekleniyor (' + where + ')');
    } else {
      setText('st-pc', '<span class="warn">●</span> Bilgisayarda "Çeviriyi başlat"a bas');
    }
    setText('st-hdmi', 'Görüntü: HDMI ' + P.hdmiPort());
  }

  /** mode: "full" for seconds (then hidden or small), "small" a corner dot while there is no PC link. */
  function showStatus(seconds) {
    renderStatus();
    statusBox.className = 'status shown';
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(function () {
      statusTimer = null;
      renderStatus();
      statusBox.className = connected ? 'status' : 'status shown small';
    }, seconds * 1000);
  }

  var toastTimer = null;
  function toast(text) {
    var t = document.getElementById('toast');
    t.innerHTML = text;
    t.className = 'toast shown';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = 'toast'; }, 2500);
  }

  // ------------------------------------------------------------ subtitle panels

  function num(m, key, oldKey, def) {
    var v = m[key];
    if ((v === undefined || v === null) && oldKey) v = m[oldKey];
    if (v === undefined || v === null || v === '') return def;
    v = Number(v);
    return isNaN(v) ? def : v;
  }

  function flag(m, key, oldKey) {
    return m[key] !== undefined ? !!m[key] : !!m[oldKey];
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function reflow(el) { return el.offsetWidth; }

  function transition(el, value) {
    el.style.webkitTransition = value;
    el.style.transition = value;
  }

  function Panel() {
    this.root = document.createElement('div');
    this.root.className = 'panel';
    this.old = document.createElement('div');
    this.old.className = 'pill old';
    this.cur = document.createElement('div');
    this.cur.className = 'pill cur';
    this.root.appendChild(this.old);
    this.root.appendChild(this.cur);
    layer.appendChild(this.root);
    this.visible = false;
    this.fadingOut = false;
    this.fadeMs = 180;
    this.text = '';
    this.color = '#ffffff';
    this.hideTimer = null;
    this.oldTimer = null;
    this.removeTimer = null;
  }

  Panel.prototype.fadeOutMs = function () { return Math.round(this.fadeMs * 1.4); };

  Panel.prototype.hideOld = function (instant) {
    var old = this.old;
    if (this.oldTimer) { clearTimeout(this.oldTimer); this.oldTimer = null; }
    if (instant || this.fadeMs <= 0 || old.style.display !== 'flex') {
      old.style.display = 'none';
      return;
    }
    transition(old, 'opacity ' + this.fadeOutMs() + 'ms ease-in');
    old.style.opacity = '0';
    setTimeout(function () {
      if (old.style.opacity === '0') old.style.display = 'none';
    }, this.fadeOutMs() + 30);
  };

  Panel.prototype.fadeOut = function () {
    var self = this;
    this.visible = false;
    if (this.hideTimer) { clearTimeout(this.hideTimer); this.hideTimer = null; }
    if (this.oldTimer) { clearTimeout(this.oldTimer); this.oldTimer = null; }
    if (this.removeTimer) clearTimeout(this.removeTimer);
    if (this.fadeMs <= 0) {
      this.remove();
      return;
    }
    this.fadingOut = true;
    transition(this.root, 'opacity ' + this.fadeOutMs() + 'ms ease-in');
    this.root.style.opacity = '0';
    this.removeTimer = setTimeout(function () {
      self.removeTimer = null;
      if (!self.visible) self.remove();
    }, this.fadeOutMs() + 50);
  };

  Panel.prototype.remove = function () {
    this.fadingOut = false;
    transition(this.root, 'none');
    this.root.style.opacity = '0';
    this.root.style.visibility = 'hidden';
    this.hideOld(true);
    this.cur.textContent = '';
    this.text = '';
  };

  Panel.prototype.apply = function (m) {
    var self = this;
    var text = String(m.text || '');
    var ttl = num(m, 'ttl', null, 10);
    var isNew = flag(m, 'new', 'yeni');
    var opacity = clamp(num(m, 'opacity', 'opak', 1), 0, 1);
    var scale = num(m, 'scale', 'olcek', 1);
    var maxWidth = num(m, 'max_width', 'genis', 0.8);
    var oldSecs = num(m, 'old_secs', 'eski', 3);
    this.fadeMs = clamp(Math.round(num(m, 'fade_ms', 'gecis', 180)), 0, 1000);
    var fade = this.fadeMs;
    var W = window.innerWidth || 1920;
    var H = window.innerHeight || 1080;
    var px, padX, padY, maxW, minW = 0, minH = 0, centerX, bottom;

    var box = m.box;
    if (box && box.length === 4) {
      var bx = box[0] * W, by = box[1] * H, bw = box[2] * W, bh = box[3] * H;
      var lines = Math.max(1, Math.round(num(m, 'lines', null, 1)));
      var lineH = m.lineh !== undefined ? num(m, 'lineh', null, 0.03) * H : bh / lines;
      px = lineH * scale;
      var engRow = bh / lines;  // height of one English row
      var coverX = Math.round(engRow * 0.6), coverY = Math.round(engRow * 0.3);
      padX = Math.round(px * 0.5);
      padY = Math.round(px * 0.24);
      minW = Math.round(bw + 2 * coverX);
      minH = Math.round(bh + 2 * coverY);
      maxW = Math.round(Math.max(minW, maxWidth * W));
      centerX = bx + bw / 2;
      bottom = Math.max(0, Math.round(H - (by + bh) - coverY));
    } else {
      // Old layout (no box): "size" in Android sp, "bottom" in dp; a 1080p Android TV has about 2 px per dp.
      px = num(m, 'size', null, 30) * 2 * H / 1080;
      padX = Math.round(px * 0.5);
      padY = Math.round(px * 0.24);
      maxW = Math.round(maxWidth * W);
      centerX = W / 2;
      bottom = Math.round(num(m, 'bottom', null, 60) * 2 * H / 1080);
    }

    var bg = 'rgba(12,14,18,' + opacity + ')';
    var cur = this.cur, old = this.old, root = this.root;
    cur.style.fontSize = px + 'px';
    cur.style.padding = padY + 'px ' + padX + 'px';
    cur.style.borderRadius = Math.round(px * 0.33) + 'px';
    cur.style.minWidth = minW + 'px';
    cur.style.minHeight = minH + 'px';
    cur.style.background = bg;
    old.style.fontSize = (px * OLD_SCALE) + 'px';
    old.style.padding = Math.round(padY * OLD_SCALE) + 'px ' + Math.round(padX * OLD_SCALE) + 'px';
    old.style.borderRadius = Math.round(px * OLD_SCALE * 0.33) + 'px';
    old.style.marginBottom = Math.round(px * 0.3) + 'px';
    old.style.background = bg;

    var color = '#ffffff';
    var cs = String(m.color !== undefined ? m.color : (m.renk || ''));
    if (/^#[0-9a-fA-F]{6}$/.test(cs)) color = cs;

    var wasVisible = this.visible;
    var changed = this.text !== text;
    if (isNew && oldSecs > 0 && wasVisible && this.text && changed) {
      // A subtitle following another: the previous line stays dimmed above for a while, in its speaker's color.
      if (this.oldTimer) clearTimeout(this.oldTimer);
      old.textContent = this.text;
      old.style.color = this.color;
      transition(old, 'none');
      old.style.opacity = String(OLD_ALPHA);
      old.style.display = 'flex';
      this.oldTimer = setTimeout(function () { self.oldTimer = null; self.hideOld(false); }, oldSecs * 1000);
    } else if (!wasVisible) {
      this.hideOld(true);
    }

    // Color: blended when the same line only changes color (the speaker was recognized by voice).
    transition(cur, wasVisible && !changed && fade > 0 ? 'color ' + Math.max(250, fade * 2) + 'ms' : 'none');
    cur.style.color = color;
    if (wasVisible && changed && fade > 0) {
      // The box is already on screen: only the new text fades in (the box does not blink).
      cur.style.opacity = '0.15';
      reflow(cur);
      transition(cur, 'opacity ' + fade + 'ms ease-out');
      cur.style.opacity = '1';
    } else if (changed) {
      cur.style.opacity = '1';
    }
    cur.textContent = text;
    this.text = text;
    this.color = color;

    var width = Math.min(maxW, W);
    var left = clamp(Math.round(centerX - width / 2), 0, Math.max(0, W - width));
    root.style.width = width + 'px';
    if (this.removeTimer) { clearTimeout(this.removeTimer); this.removeTimer = null; }
    if (!wasVisible && !this.fadingOut) {
      transition(root, 'none');
      root.style.left = left + 'px';
      root.style.bottom = bottom + 'px';
      root.style.opacity = fade > 0 ? '0' : '1';
      root.style.visibility = 'visible';
      reflow(root);
      transition(root, 'opacity ' + fade + 'ms ease-out');
      root.style.opacity = '1';
    } else {
      // On screen (or fading out): slide to the new place and fade back in from where it is.
      var slide = wasVisible && fade > 0 ? fade : 0;
      transition(root, 'opacity ' + fade + 'ms ease-out, left ' + slide + 'ms ease-out, bottom ' + slide + 'ms ease-out');
      root.style.visibility = 'visible';
      root.style.left = left + 'px';
      root.style.bottom = bottom + 'px';
      root.style.opacity = '1';
    }
    this.visible = true;
    this.fadingOut = false;
    if (this.hideTimer) clearTimeout(this.hideTimer);
    this.hideTimer = ttl > 0 ? setTimeout(function () { self.hideTimer = null; self.fadeOut(); }, ttl * 1000) : null;
  };

  function show(m) {
    var id = String(m.id || 'speech');
    if (!panels[id]) panels[id] = new Panel();
    panels[id].apply(m);
  }

  function clear(id) {
    for (var key in panels) {
      if (panels.hasOwnProperty(key) && (id === null || id === key) && panels[key].visible) panels[key].fadeOut();
    }
  }

  // ------------------------------------------------------------ HDMI input choice

  /** The PC's HDMI setting (0 = automatic). A new value from the PC wins over the remote; otherwise the remote's
   * last choice stays. */
  function applyPcHdmi(value) {
    value = Number(value) || 0;
    var lastPc = Number(store(STORE_PC_HDMI)) || 0;
    if (value !== lastPc) {
      store(STORE_PC_HDMI, String(value));
      if (value > 0) store(STORE_HDMI, null);
    }
    var chosen = Number(store(STORE_HDMI)) || 0;
    if (chosen > 0) {
      P.showHdmi(chosen);
    } else if (value > 0) {
      P.showHdmi(value);
    } else {
      P.detectInput(function (found) {
        if (found > 0 && !(Number(store(STORE_HDMI)) > 0)) P.showHdmi(found);
        log('görüntü HDMI ' + P.hdmiPort() + (found > 0 ? ' (TV buldu)' : ' (TV giriş bilgisi vermedi)'));
        renderStatus();
      });
    }
    renderStatus();
  }

  function stepHdmi(dir) {
    var p = P.hdmiPort() + dir;
    if (p < 1) p = 4;
    if (p > 4) p = 1;
    store(STORE_HDMI, String(p));
    P.showHdmi(p);
    toast('HDMI ' + p);
    log('kumandayla HDMI ' + p + ' seçildi');
    renderStatus();
  }

  // ------------------------------------------------------------ connection to the PC

  function setConnected(on) {
    if (on === connected) return;
    connected = on;
    if (on) {
      log('bağlandı: ' + P.name + ' ' + VERSION + ', ekran ' + window.innerWidth + 'x' + window.innerHeight +
        ', HDMI ' + P.hdmiPort());
      showStatus(5);
    } else {
      showStatus(20);
    }
  }

  function handle(data) {
    var m;
    try { m = JSON.parse(data); } catch (e) { return; }
    if (!m || !m.type) return;
    if (m.type === 'show') {
      try { show(m); } catch (e) { log('gösterilemedi: ' + e.message); }
    } else if (m.type === 'clear') {
      clear(m.id !== undefined ? String(m.id) : null);
    } else if (m.type === 'hello') {
      if (m.hdmi !== undefined) applyPcHdmi(m.hdmi);
    }
  }

  function connect() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (source) {
      try { source.close(); } catch (e) { /* ignore */ }
      source = null;
    }
    setConnected(false);
    if (!pc) { renderStatus(); return; }
    if (!window.EventSource) {
      setText('st-pc', '<span class="warn">●</span> Bu TV tarayıcısı EventSource desteklemiyor');
      return;
    }
    // k: the PC's access key (from the launch parameters or the package); without it the PC refuses the page.
    var es = new EventSource('http://' + pc.host + ':' + pc.port + '/events?v=' + VERSION + '&tv=' + P.name +
      '&k=' + encodeURIComponent(pc.key || ''));
    source = es;
    lastEventAt = now();
    es.onopen = function () {
      if (es !== source) return;
      lastEventAt = now();
      setConnected(true);
      flushLog();
    };
    es.onmessage = function (e) {
      if (es !== source) return;
      lastEventAt = now();
      handle(e.data);
    };
    es.addEventListener('ping', function () {
      if (es === source) lastEventAt = now();
    });
    es.onerror = function () {
      if (es !== source) return;
      setConnected(false);
      // EventSource retries by itself; once it gives up (CLOSED) start again ourselves.
      if (es.readyState === 2 && !retryTimer) retryTimer = setTimeout(connect, 3000);
    };
  }

  // The link can hang half-open (connected but nothing arrives); the PC pings every 10 s.
  var disconnectedSince = now();
  var lastScanAt = 0;
  setInterval(function () {
    if (connected && now() - lastEventAt > 35000) {
      log('35 sn bilgisayardan bir şey gelmedi, yeniden bağlanılıyor');
      connect();
    }
    if (connected) disconnectedSince = now();
    // The PC's address changed (DHCP) or was never given: look for it on the TV's network.
    else if (P.localAddress && !scanning && now() - disconnectedSince > 12000 && now() - lastScanAt > 60000) scan();
  }, 5000);

  // ------------------------------------------------------------ finding the PC on the network

  var scanning = false;

  /** Asks every address of the TV's /24 network for the PC's /ping (answer "dilmac"). */
  function scan() {
    scanning = true;
    lastScanAt = now();
    var port = pc ? pc.port : 8788;
    P.localAddress(function (ip) {
      if (!ip || !/^\d+\.\d+\.\d+\.\d+$/.test(ip)) {
        scanning = false;
        return;
      }
      var base = ip.split('.').slice(0, 3).join('.') + '.';
      var queue = [];
      for (var i = 1; i < 255; i++) if (base + i !== ip) queue.push(base + i);
      // The PC's last known address first.
      if (pc && pc.host.indexOf(base) === 0) queue.unshift(pc.host);
      var found = null, running = 0;
      setText('st-pc', '<span class="warn">●</span> Bilgisayar ağda aranıyor…');
      log('bilgisayar ' + base + '0/24 ağında aranıyor (port ' + port + ')');
      function probe(host) {
        running++;
        var xhr = new XMLHttpRequest();
        var done = false;
        function finish() {
          if (done) return;
          done = true;
          running--;
          if (!found && xhr.status === 200 && String(xhr.responseText).indexOf('dilmac') === 0) {
            found = host;
            log('bilgisayar bulundu: ' + host + ':' + port);
            scanning = false;
            usePc({ host: host, port: port });
            return;
          }
          next();
        }
        try {
          xhr.open('GET', 'http://' + host + ':' + port + '/ping', true);
          xhr.timeout = 1500;
          xhr.onreadystatechange = function () { if (xhr.readyState === 4) finish(); };
          xhr.ontimeout = finish;
          xhr.send();
        } catch (e) {
          finish();
        }
      }
      function next() {
        if (found) return;
        if (!queue.length) {
          if (!running) {
            scanning = false;
            log('bilgisayar ağda bulunamadı');
            renderStatus();
          }
          return;
        }
        probe(queue.shift());
      }
      for (var k = 0; k < 24; k++) next();
    });
  }

  function usePc(params) {
    var host = params && params.host ? String(params.host) : '';
    var port = params && params.port ? Number(params.port) : 0;
    if (host && port) {
      // A PC found by searching the network keeps the key we already have.
      var key = params.key ? String(params.key) : (pc && pc.key) || '';
      var changed = !pc || pc.host !== host || pc.port !== port || pc.key !== key;
      pc = { host: host, port: port, key: key };
      store(STORE_PC, JSON.stringify(pc));
      if (changed || !connected) connect();
    }
    if (params && params.hdmi !== undefined) applyPcHdmi(params.hdmi);
  }

  // ------------------------------------------------------------ remote keys

  function has(list, code) {
    for (var i = 0; i < list.length; i++) if (list[i] === code) return true;
    return false;
  }

  document.addEventListener('keydown', function (e) {
    var code = e.keyCode;
    if (has(P.KEY_LEFT, code)) { stepHdmi(-1); e.preventDefault(); }
    else if (has(P.KEY_RIGHT, code)) { stepHdmi(1); e.preventDefault(); }
    else if (has(P.KEY_OK, code)) { showStatus(8); e.preventDefault(); }
    else if (has(P.KEY_BACK, code)) {
      e.preventDefault();
      if (statusBox.className.indexOf('shown') >= 0 && statusBox.className.indexOf('small') < 0) {
        statusBox.className = connected ? 'status' : 'status shown small';
      } else {
        log('kumandadan çıkıldı');
        P.exit();
      }
    }
  });

  // ------------------------------------------------------------ start

  P.start(log);
  P.onRelaunch = function (params) {
    log('yeniden açıldı');
    usePc(params);
  };
  var saved = null;
  try { saved = JSON.parse(store(STORE_PC) || 'null'); } catch (e) { saved = null; }
  if (saved && saved.host && saved.port) pc = saved;
  // The PC's address written into the package at install (Samsung). A newly installed package's address wins over the
  // remembered one; otherwise the remembered one (found later, e.g. after the PC's address changed) wins.
  var embedded = window.DILMAC_PC;
  if (embedded && embedded.host && embedded.port) {
    var mark = embedded.host + ':' + embedded.port + ':' + (embedded.key || '');
    if (store(STORE_EMBEDDED) !== mark || !pc) {
      store(STORE_EMBEDDED, mark);
      pc = { host: String(embedded.host), port: Number(embedded.port), key: String(embedded.key || '') };
      store(STORE_PC, JSON.stringify(pc));
    }
  }
  var params = P.launchParams();
  P.showHdmi(Number(store(STORE_HDMI)) || Number(params.hdmi) || 1);
  if (params.host && params.port) usePc(params);
  else {
    applyPcHdmi(Number(store(STORE_PC_HDMI)) || 0);
    connect();
  }
  showStatus(20);

  // For tests in a desktop browser.
  window.Dilmac = { show: show, clear: clear, version: VERSION };
})();
