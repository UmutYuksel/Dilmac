/* Dilmaç TV page: the LG webOS part (HDMI picture, launch parameters, remote keys, screen saver).
 *
 * LG does not let an app draw over the TV's own HDMI input, so the app stays in front and shows the HDMI input
 * itself: a <video> whose source is "ext://hdmi:N" (not in LG's documentation; the way webOS homebrew apps do it).
 * The subtitles are drawn above it by app.js.
 *
 * Outside a TV (a desktop browser, for testing) there is no HDMI: a still picture given as ?bg=URL stands in for it,
 * and the PC address comes from ?host=&port=.
 *
 * Plain ES5 on purpose: the same page runs on old TV browsers.
 */
(function () {
  'use strict';

  var palm = window.PalmSystem || null;
  var picture = null;
  var video = null;
  var port = 1;
  var builtAt = 0;
  var rebuildTimer = null;
  var hook = function () {};

  function now() { return new Date().getTime(); }

  function query() {
    var out = {};
    var q = (window.location.search || '').replace(/^\?/, '').split('&');
    for (var i = 0; i < q.length; i++) {
      if (!q[i]) continue;
      var kv = q[i].split('=');
      out[decodeURIComponent(kv[0])] = decodeURIComponent((kv[1] || '').replace(/\+/g, ' '));
    }
    return out;
  }

  function launchParams() {
    if (palm) {
      try {
        var p = palm.launchParams;
        if (typeof p === 'string') p = p ? JSON.parse(p) : {};
        return p || {};
      } catch (e) {
        return {};
      }
    }
    return query();
  }

  // ------------------------------------------------------------ luna service calls

  /** Calls a webOS system service; cb(result object) for every answer. Returns false if the bridge is missing. */
  function luna(uri, params, cb) {
    if (typeof window.PalmServiceBridge === 'undefined') return false;
    try {
      var bridge = new window.PalmServiceBridge();
      bridge.onservicecallback = function (msg) {
        var r = null;
        try { r = JSON.parse(msg); } catch (e) { r = { raw: String(msg) }; }
        if (cb) cb(r);
      };
      bridge.call(uri, JSON.stringify(params || {}));
      // Keep a reference: a collected bridge stops delivering answers of a subscription.
      luna.live.push(bridge);
      return true;
    } catch (e) {
      hook('luna ' + uri + ': ' + e.message);
      return false;
    }
  }
  luna.live = [];

  // ------------------------------------------------------------ HDMI picture

  function removeVideo() {
    if (rebuildTimer) { clearTimeout(rebuildTimer); rebuildTimer = null; }
    if (!video) return;
    try { video.pause(); } catch (e) { /* ignore */ }
    try {
      while (video.firstChild) video.removeChild(video.firstChild);
      video.removeAttribute('src');
      video.load();
    } catch (e) { /* ignore */ }
    if (video.parentNode) video.parentNode.removeChild(video);
    video = null;
  }

  function scheduleRebuild(reason) {
    if (rebuildTimer || document.hidden) return;
    // At most one rebuild every 5 s, so a TV that keeps failing does not flicker.
    var wait = Math.max(1500, 5000 - (now() - builtAt));
    hook('HDMI ' + port + ' yeniden kuruluyor (' + reason + ')');
    rebuildTimer = setTimeout(function () {
      rebuildTimer = null;
      build();
    }, wait);
  }

  function build() {
    removeVideo();
    builtAt = now();
    if (!palm) {
      var bg = query().bg;
      var fake = document.createElement('div');
      fake.className = 'fake';
      if (bg) fake.style.backgroundImage = 'url("' + bg + '")';
      picture.innerHTML = '';
      picture.appendChild(fake);
      return;
    }
    var v = document.createElement('video');
    v.setAttribute('autoplay', '');
    var source = document.createElement('source');
    source.setAttribute('type', 'service/webos-external');
    source.setAttribute('src', 'ext://hdmi:' + port);
    v.appendChild(source);
    var events = ['loadedmetadata', 'playing', 'stalled', 'error', 'ended', 'abort'];
    for (var i = 0; i < events.length; i++) {
      (function (name) {
        v.addEventListener(name, function () {
          if (v !== video) return;
          hook('HDMI ' + port + ': ' + name);
          if (name === 'error' || name === 'ended' || name === 'abort') scheduleRebuild(name);
        });
      })(events[i]);
    }
    video = v;
    picture.appendChild(v);
    try {
      var played = v.play();
      if (played && played['catch']) played['catch'](function (e) { hook('HDMI play: ' + e.message); });
    } catch (e) {
      hook('HDMI play: ' + e.message);
    }
  }

  /** Asks the TV which HDMI inputs have a picture; cb(port or 0). A device that calls itself PS5/GAME wins. */
  function detectInput(cb) {
    var done = false;
    function finish(p) { if (!done) { done = true; cb(p); } }
    var asked = luna('luna://com.webos.service.eim/getAllInputStatus', {}, function (r) {
      var list = (r && (r.devices || r.inputSourceList)) || [];
      var best = 0, bestRank = 9;
      for (var i = 0; i < list.length; i++) {
        var d = list[i] || {};
        var id = String(d.id || '');
        if (!/^HDMI_/.test(id)) continue;
        var p = Number(d.port) || Number(id.replace('HDMI_', '')) || 0;
        if (p < 1) continue;
        var info = JSON.stringify(d).toUpperCase();
        var rank = info.indexOf('PS5') >= 0 || info.indexOf('PLAYSTATION') >= 0 ? 0
          : info.indexOf('GAME') >= 0 ? 1 : d.activate ? 2 : 9;
        if (rank < bestRank) { best = p; bestRank = rank; }
      }
      if (best) hook('HDMI girişleri okundu: HDMI ' + best + ' seçildi');
      else hook('HDMI girişleri: ' + JSON.stringify(r).slice(0, 300));
      finish(bestRank < 9 ? best : 0);
    });
    if (!asked) finish(0);
    setTimeout(function () { finish(0); }, 3000);
  }

  // ------------------------------------------------------------ screen saver

  /** The TV starts its screen saver after a while without remote keys even while the game runs; refuse it. */
  function keepAwake() {
    var client = 'com.dilmac.tv';
    luna('luna://com.webos.service.tvpower/power/registerScreenSaverRequest',
      { subscribe: true, clientName: client }, function (r) {
        if (r && r.state === 'Active' && r.timestamp !== undefined) {
          luna('luna://com.webos.service.tvpower/power/responseScreenSaverRequest',
            { clientName: client, ack: false, timestamp: r.timestamp }, null);
        }
      });
  }

  // ------------------------------------------------------------ public interface for app.js

  window.Platform = {
    name: palm ? 'webos' : 'browser',
    KEY_BACK: [461, 27, 8],
    KEY_OK: [13],
    KEY_LEFT: [37],
    KEY_RIGHT: [39],

    /** Starts the picture. log(text) gets diagnostic lines (they go to the PC's log). */
    start: function (log) {
      hook = log || hook;
      picture = document.getElementById('picture');
      if (palm) {
        try { if (palm.stageReady) palm.stageReady(); } catch (e) { /* ignore */ }
        keepAwake();
        document.addEventListener('webOSRelaunch', function () {
          if (window.Platform.onRelaunch) window.Platform.onRelaunch(launchParams());
          try { if (palm.activate) palm.activate(); } catch (e) { /* ignore */ }
        });
      }
      document.addEventListener('visibilitychange', function () {
        // In the background the TV shows its own input; give the HDMI pipeline back and take it again on return.
        if (document.hidden) { hook('uygulama arka planda'); removeVideo(); }
        else { hook('uygulama önde'); build(); }
      });
    },

    launchParams: launchParams,
    onRelaunch: null,
    detectInput: detectInput,

    hdmiPort: function () { return port; },

    showHdmi: function (p) {
      p = Math.max(1, Math.min(4, Number(p) || 1));
      if (p === port && video) return;
      port = p;
      build();
    },

    /** Leaves the app (the TV goes back to what it showed before). */
    exit: function () {
      removeVideo();
      try {
        if (palm && palm.platformBack) { palm.platformBack(); return; }
      } catch (e) { /* ignore */ }
      window.close();
    }
  };
})();
