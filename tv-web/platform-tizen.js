/* Dilmaç TV page: the Samsung Tizen part (HDMI picture, launch parameters, remote keys, screen saver).
 * In the Samsung package this file is platform.js (samsung_tv.build_wgt).
 *
 * Samsung does not let an app draw over the TV's own HDMI input either. Tizen has an official way to show an input
 * inside an app: the TV window (tizen.tvwindow) set to the HDMI source and shown BEHIND the page, so the page's
 * transparent parts show the picture and the subtitles are drawn above it.
 *
 * The PC starts the app over the developer connection (sdb), which passes no parameters: the PC's address is written
 * into the package when it is installed (window.DILMAC_PC) and app.js searches the TV's network if it changed.
 *
 * Plain ES5 on purpose: Tizen 3.0 (2017) has Chromium 47.
 */
(function () {
  'use strict';

  var hasTizen = typeof window.tizen !== 'undefined';
  var port = 1;
  var shown = false;
  var hook = function () {};

  function tvwindow() {
    return hasTizen && window.tizen.tvwindow ? window.tizen.tvwindow : null;
  }

  function errorText(e) {
    return e ? (e.name || '') + (e.message ? ': ' + e.message : '') : '?';
  }

  /** The HDMI sources the TV knows: cb(connected[], disconnected[]) with {type:'HDMI', number:N}. */
  function hdmiSources(cb) {
    try {
      window.tizen.systeminfo.getPropertyValue('VIDEOSOURCE', function (info) {
        var isHdmi = function (s) { return s && s.type === 'HDMI'; };
        cb((info.connected || []).filter(isHdmi), (info.disconnected || []).filter(isHdmi));
      }, function (e) {
        hook('HDMI kaynakları okunamadı: ' + errorText(e));
        cb([], []);
      });
    } catch (e) {
      cb([], []);
    }
  }

  function fullRect() {
    return ['0px', '0px', (window.innerWidth || 1920) + 'px', (window.innerHeight || 1080) + 'px'];
  }

  function showWindow() {
    var w = tvwindow();
    if (!w) return;
    try {
      w.show(function () {
        shown = true;
        hook('HDMI ' + port + ' gösteriliyor');
      }, function (e) {
        hook('HDMI penceresi açılamadı: ' + errorText(e));
      }, fullRect(), 'MAIN', 'BEHIND');
    } catch (e) {
      hook('HDMI penceresi açılamadı: ' + errorText(e));
    }
  }

  function hideWindow() {
    var w = tvwindow();
    if (!w || !shown) return;
    shown = false;
    try { w.hide(function () {}, function () {}, 'MAIN'); } catch (e) { /* ignore */ }
  }

  function build() {
    var w = tvwindow();
    if (!w) return;
    hdmiSources(function (connected, disconnected) {
      var all = connected.concat(disconnected);
      var source = null;
      for (var i = 0; i < all.length; i++) if (Number(all[i].number) === port) source = all[i];
      if (!source) {
        hook('TV\'de HDMI ' + port + ' yok (' + all.length + ' HDMI girişi)');
        return;
      }
      try {
        w.setSource(source, function () { showWindow(); }, function (e) {
          hook('HDMI ' + port + ' seçilemedi: ' + errorText(e));
        });
      } catch (e) {
        hook('HDMI ' + port + ' seçilemedi: ' + errorText(e));
      }
    });
  }

  function detectInput(cb) {
    if (!hasTizen) { cb(0); return; }
    hdmiSources(function (connected) {
      var p = connected.length ? Number(connected[0].number) || 0 : 0;
      hook('bağlı HDMI girişleri: ' + (connected.map(function (s) { return s.number; }).join(', ') || 'yok'));
      cb(p);
    });
  }

  /** Parameters the app was started with (an app control's PAYLOAD), if any. */
  function launchParams() {
    try {
      var control = window.tizen.application.getCurrentApplication().getRequestedAppControl();
      var data = (control && control.appControl && control.appControl.data) || [];
      for (var i = 0; i < data.length; i++) {
        if (data[i].key === 'PAYLOAD' && data[i].value && data[i].value.length) {
          var p = JSON.parse(data[i].value[0]);
          return (p && p.values !== undefined ? p.values : p) || {};
        }
      }
    } catch (e) { /* no parameters */ }
    return {};
  }

  /** The TV's own network address: cb(ip or null). */
  function localAddress(cb) {
    if (!hasTizen) { cb(null); return; }
    var names = ['ETHERNET_NETWORK', 'WIFI_NETWORK'];
    var i = 0;
    function next() {
      if (i >= names.length) { cb(null); return; }
      var name = names[i++];
      try {
        window.tizen.systeminfo.getPropertyValue(name, function (info) {
          if (info && info.ipAddress && info.ipAddress !== '0.0.0.0') cb(info.ipAddress);
          else next();
        }, next);
      } catch (e) {
        next();
      }
    }
    next();
  }

  window.Platform = {
    name: hasTizen ? 'tizen' : 'browser',
    KEY_BACK: [10009, 27, 8],
    KEY_OK: [13],
    KEY_LEFT: [37],
    KEY_RIGHT: [39],

    start: function (log) {
      hook = log || hook;
      // The TV window is drawn behind the page: nothing above it may paint a background.
      document.getElementById('picture').style.background = 'transparent';
      document.body.style.background = 'transparent';
      document.documentElement.style.background = 'transparent';
      // The game is played without touching the TV remote; the TV must not take it for idle (Samsung AppCommon API).
      try {
        window.webapis.appcommon.setScreenSaver(window.webapis.appcommon.AppCommonScreenSaverState.SCREEN_SAVER_OFF);
      } catch (e) { /* older TVs without the API */ }
      window.addEventListener('appcontrol', function () {
        if (window.Platform.onRelaunch) window.Platform.onRelaunch(launchParams());
      });
      document.addEventListener('visibilitychange', function () {
        if (document.hidden) { hook('uygulama arka planda'); hideWindow(); }
        else { hook('uygulama önde'); build(); }
      });
    },

    launchParams: launchParams,
    onRelaunch: null,
    detectInput: detectInput,
    localAddress: localAddress,

    hdmiPort: function () { return port; },

    showHdmi: function (p) {
      p = Math.max(1, Math.min(4, Number(p) || 1));
      if (p === port && shown) return;
      port = p;
      build();
    },

    exit: function () {
      hideWindow();
      try { window.tizen.application.getCurrentApplication().exit(); return; } catch (e) { /* ignore */ }
      window.close();
    }
  };
})();
