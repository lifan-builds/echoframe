/* Strictly ES5 syntax and XMLHttpRequest for Gen-1 Echo Show & Echo Show 5 Silk browsers. */
(function () {
  'use strict';

  function el(id) { return document.getElementById(id); }

  var slides = [];
  var channels = [];
  var activeChannel = 'smart_frame';
  var activeQuery = '';
  var index = 0;
  var activeSlot = 'a';
  var started = false;
  var paused = false;
  var intervalSec = 10;
  var showCaptions = true;
  var showClock = true;
  var serverRevision = 0;
  var lastCommandSeq = 0;
  var slideTimer = null;
  var demoColors = ['#17343c', '#35293d', '#3a3222'];
  var demoTitles = ['Room for<br>your memories.', 'A quieter<br>home screen.', 'Made for<br>your table.'];
  var reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var OFFLINE_STATE_KEY = 'ambient_frame_state_v1';
  var isCloudOrigin = window.location.protocol === 'https:';

  function saveOfflineState(stateObj) {
    try {
      if (window.localStorage && stateObj && stateObj.slides && stateObj.slides.length) {
        window.localStorage.setItem(OFFLINE_STATE_KEY, JSON.stringify(stateObj));
      }
    } catch (e) {}
  }

  function loadOfflineState() {
    try {
      if (!window.localStorage) { return null; }
      var raw = window.localStorage.getItem(OFFLINE_STATE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function warmServiceWorkerCache() {
    try {
      if (!navigator.serviceWorker || !slides.length) { return; }
      var urls = [];
      var i;
      for (i = 0; i < slides.length; i++) {
        if (slides[i] && slides[i].src) {
          urls.push(slides[i].src);
        }
      }
      if (!urls.length) { return; }
      if (navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage({ type: 'PRECACHE_SLIDES', urls: urls });
      } else if (navigator.serviceWorker.ready && navigator.serviceWorker.ready.then) {
        navigator.serviceWorker.ready.then(function (reg) {
          if (reg && reg.active) {
            reg.active.postMessage({ type: 'PRECACHE_SLIDES', urls: urls });
          }
        });
      }
    } catch (e) {}
  }

  function registerOfflineWorker() {
    try {
      if (navigator.serviceWorker && navigator.serviceWorker.register) {
        navigator.serviceWorker.register('/sw.js').then(function () {
          if (started) { warmServiceWorkerCache(); }
        })['catch'](function () {});
      }
    } catch (e) {}
  }

  function requestJson(method, path, bodyObj, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, path, true);
    xhr.timeout = 7000;
    if (bodyObj) {
      xhr.setRequestHeader('Content-Type', 'application/json');
    }
    xhr.onload = function () {
      if (xhr.status < 200 || xhr.status >= 300) {
        callback(new Error('HTTP ' + xhr.status));
        return;
      }
      try {
        callback(null, JSON.parse(xhr.responseText));
      } catch (err) {
        callback(err);
      }
    };
    xhr.onerror = xhr.ontimeout = function () {
      callback(new Error('Server unavailable'));
    };
    xhr.send(bodyObj ? JSON.stringify(bodyObj) : null);
  }

  function count() {
    return slides.length || demoTitles.length;
  }

  function formatClockNow() {
    var now = new Date();
    var h = now.getHours();
    var m = now.getMinutes();
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12;
    if (h === 0) { h = 12; }
    return h + ':' + (m < 10 ? '0' + m : m) + ' ' + ampm;
  }

  function updateClock() {
    var pill = el('clock-pill');
    if (!started || !showClock || !slides.length) {
      pill.hidden = true;
      return;
    }
    pill.textContent = formatClockNow();
    pill.hidden = false;
  }

  function applyAmbientGlow(rgb) {
    var glow = el('ambient-glow');
    if (!rgb || rgb.length < 3) {
      glow.style.background = 'radial-gradient(ellipse at 50% 50%, rgba(36,46,62,0.40) 0%, rgba(8,10,15,0.98) 76%)';
      return;
    }
    var r = parseInt(rgb[0], 10) || 28;
    var g = parseInt(rgb[1], 10) || 36;
    var b = parseInt(rgb[2], 10) || 48;
    glow.style.background = 'radial-gradient(ellipse at 50% 50%, rgba(' + r + ',' + g + ',' + b + ',0.44) 0%, rgba(8,10,15,0.98) 78%)';
  }

  function updateCaptionOverlay(slide) {
    var plate = el('caption-plate');
    if (!started || !showCaptions || !slide) {
      plate.hidden = true;
      return;
    }
    var titleText = slide.display_caption || slide.label || '';
    var parts = [];
    var badge = slide.age_badge || '';
    if (badge) { parts.push(badge); }
    if (slide.date_label) { parts.push(slide.date_label); }
    if (activeChannel === 'search' && activeQuery) {
      parts.push('“' + activeQuery + '”');
    }
    el('caption-title').textContent = titleText;
    el('caption-meta').textContent = parts.join('  ·  ');
    plate.hidden = !titleText && !parts.length;
  }

  function preloadNextSlide() {
    if (slides.length < 2) { return; }
    var nextIdx = (index + 1) % slides.length;
    var pre = new Image();
    pre.src = slides[nextIdx].src;
  }

  function swapToSlide(slide) {
    var incoming = activeSlot === 'a' ? el('photo-b') : el('photo-a');
    var outgoing = activeSlot === 'a' ? el('photo-a') : el('photo-b');
    var nextSlot = activeSlot === 'a' ? 'b' : 'a';

    incoming.hidden = false;
    if (incoming.getAttribute('data-loaded-src') === slide.src) {
      incoming.alt = slide.label || '';
      incoming.className = 'slide-layer is-visible';
      outgoing.className = 'slide-layer';
      activeSlot = nextSlot;
      applyAmbientGlow(slide.accent_rgb);
      updateCaptionOverlay(slide);
      preloadNextSlide();
      return;
    }

    incoming.onload = function () {
      incoming.onload = null;
      incoming.setAttribute('data-loaded-src', slide.src);
      incoming.alt = slide.label || '';
      incoming.className = 'slide-layer is-visible';
      outgoing.className = 'slide-layer';
      activeSlot = nextSlot;
      applyAmbientGlow(slide.accent_rgb);
      updateCaptionOverlay(slide);
      preloadNextSlide();
    };
    incoming.onerror = function () {
      incoming.onerror = null;
      el('connection').textContent = 'Photo unavailable offline — skipping';
      if (slides.length > 1 && started && !paused) {
        setTimeout(function () { move(1); }, 1200);
      }
    };
    incoming.src = slide.src;
  }

  function renderCurrent() {
    var total = count();
    if (index >= total) { index = 0; }
    if (index < 0) { index = 0; }

    var channelBadge = '';
    if (activeChannel === 'search' && activeQuery) {
      channelBadge = ' · ' + activeQuery;
    }
    el('counter').textContent = (index + 1) + ' / ' + total + (slides.length ? channelBadge : ' · demo');
    el('toggle-captions').textContent = showCaptions ? 'Captions: On' : 'Captions: Off';

    if (slides.length) {
      el('demo').hidden = true;
      swapToSlide(slides[index]);
    } else {
      el('photo-a').hidden = true;
      el('photo-b').hidden = true;
      el('caption-plate').hidden = true;
      el('demo').hidden = false;
      el('demo').style.backgroundColor = demoColors[index % demoColors.length];
      el('demo').getElementsByTagName('h1')[0].innerHTML = demoTitles[index % demoTitles.length];
    }
    updateClock();
  }

  function scheduleAdvance() {
    if (slideTimer) {
      clearInterval(slideTimer);
      slideTimer = null;
    }
    if (started && !paused) {
      slideTimer = setInterval(function () {
        index = (index + 1) % count();
        renderCurrent();
      }, intervalSec * 1000);
    }
    el('pause').textContent = paused ? 'Resume' : 'Pause';
  }

  function move(step) {
    index = (index + step + count()) % count();
    renderCurrent();
    scheduleAdvance();
  }

  function setControlsVisible(visible) {
    el('controls').hidden = !visible;
    el('show-controls').hidden = visible;
    el('show-controls').setAttribute('aria-expanded', String(visible));
  }

  function requestFullScreen() {
    var root = document.documentElement;
    var method = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!method) {
      el('connection').textContent = 'Full screen unavailable; use Silk menu';
      return;
    }
    var res = method.call(root);
    if (res && res.catch) {
      res.catch(function () {
        el('connection').textContent = 'Full screen declined by browser';
      });
    }
  }

  function populateChannelSelect() {
    var sel = el('channel-select');
    if (!channels || !channels.length) {
      sel.hidden = true;
      return;
    }
    sel.hidden = false;
    sel.innerHTML = '';
    var i, opt, ch;
    for (i = 0; i < channels.length; i++) {
      ch = channels[i];
      opt = document.createElement('option');
      opt.value = ch.id;
      opt.textContent = ch.name + ' (' + ch.count + ')';
      if (ch.id === activeChannel) {
        opt.selected = true;
      }
      sel.appendChild(opt);
    }
  }

  function findSlideIndexById(slideId) {
    var i;
    if (!slideId) { return -1; }
    for (i = 0; i < slides.length; i++) {
      if (slides[i].id === slideId) { return i; }
    }
    return -1;
  }

  function applyServerState(state, isInitial) {
    var currentSlideId = (slides[index] && slides[index].id) ? slides[index].id : null;
    var channelChanged = state.active_channel !== activeChannel || state.active_query !== activeQuery;
    var intervalChanged = state.interval_sec && state.interval_sec !== intervalSec;
    var pauseChanged = typeof state.paused === 'boolean' && state.paused !== paused;

    activeChannel = state.active_channel || 'smart_frame';
    activeQuery = state.active_query || '';
    channels = state.channels || [];
    if (state.interval_sec) { intervalSec = state.interval_sec; }
    if (typeof state.show_captions === 'boolean') { showCaptions = state.show_captions; }
    if (typeof state.show_clock === 'boolean') { showClock = state.show_clock; }
    if (!isInitial && pauseChanged) {
      paused = state.paused;
    }

    slides = state.slides || [];
    populateChannelSelect();

    if (channelChanged || isInitial) {
      index = 0;
    } else if (currentSlideId) {
      var preserved = findSlideIndexById(currentSlideId);
      if (preserved >= 0) {
        index = preserved;
      } else if (index >= slides.length) {
        index = 0;
      }
    }

    if (state.command && state.command.seq && state.command.seq !== lastCommandSeq) {
      lastCommandSeq = state.command.seq;
      if (!isInitial) {
        if (state.command.action === 'next') {
          index = (index + 1) % count();
        } else if (state.command.action === 'prev') {
          index = (index - 1 + count()) % count();
        } else if (state.command.action === 'goto' && state.command.slide_id) {
          var targetIdx = findSlideIndexById(state.command.slide_id);
          if (targetIdx >= 0) { index = targetIdx; }
        }
      }
    }

    serverRevision = state.revision || 1;
    saveOfflineState(state);
    renderCurrent();
    if (started && (channelChanged || isInitial)) {
      warmServiceWorkerCache();
    }
    if (intervalChanged || pauseChanged || channelChanged) {
      scheduleAdvance();
    }
  }

  function buildHeartbeatQuery() {
    var cur = slides[index];
    var sid = cur && cur.id ? encodeURIComponent(cur.id) : '';
    var cap = cur && (cur.display_caption || cur.label) ? encodeURIComponent(cur.display_caption || cur.label) : '';
    return '?role=display&slide=' + sid + '&caption=' + cap;
  }

  function pollState() {
    requestJson('GET', '/api/state' + buildHeartbeatQuery(), null, function (err, state) {
      if (err || !state) {
        el('connection').textContent = slides.length
          ? ('Offline cache active · ' + slides.length + ' slides')
          : 'Reconnecting…';
        return;
      }
      el('connection').textContent = isCloudOrigin ? 'Synced · always-on frame' : 'Synced over local Wi-Fi';
      if (state.revision !== serverRevision) {
        applyServerState(state, false);
      }
    });
  }

  function pingServer() {
    requestJson('GET', '/api/ping' + buildHeartbeatQuery(), null, function (err) {
      if (err) {
        el('connection').textContent = slides.length
          ? ('Offline cache active · ' + slides.length + ' slides')
          : 'Server disconnected — check network';
        return;
      }
      el('connection').textContent = isCloudOrigin ? 'Connected · always-on frame' : 'Connected over local Wi-Fi';
    });
  }

  function startAlbum(useAudio) {
    if (useAudio) {
      var audio = el('keepalive');
      var playRes = audio.play();
      if (playRes && playRes.catch) {
        playRes.catch(function () {
          el('connection').textContent = 'Keep-open audio blocked; slideshow running';
        });
      }
    }
    started = true;
    paused = reducedMotion ? true : paused;
    el('setup').hidden = true;
    setControlsVisible(false);
    renderCurrent();
    scheduleAdvance();
    warmServiceWorkerCache();
    pingServer();
  }

  el('start').onclick = function () { startAlbum(el('keep-open').checked); };
  el('previous').onclick = function () { move(-1); };
  el('next').onclick = function () { move(1); };
  el('pause').onclick = function () {
    paused = !paused;
    scheduleAdvance();
    requestJson('POST', '/api/control', { paused: paused }, function () {});
  };
  el('toggle-captions').onclick = function () {
    showCaptions = !showCaptions;
    renderCurrent();
    requestJson('POST', '/api/control', { show_captions: showCaptions }, function () {});
  };
  el('channel-select').onchange = function () {
    var chosen = el('channel-select').value;
    requestJson('POST', '/api/control', { channel: chosen }, function (err, resp) {
      if (!err && resp && resp.state) {
        applyServerState(resp.state, false);
      }
    });
  };
  el('fullscreen').onclick = requestFullScreen;
  el('show-controls').onclick = function () { setControlsVisible(true); el('previous').focus(); };
  el('hide-controls').onclick = function () { setControlsVisible(false); el('show-controls').focus(); };

  el('stage').onclick = function () {
    if (!started) { return; }
    setControlsVisible(el('controls').hidden);
  };

  document.onkeydown = function (event) {
    var tag = event.target && event.target.tagName ? event.target.tagName : '';
    if (!started || tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT') { return; }
    if (event.key === 'ArrowRight') { move(1); }
    if (event.key === 'ArrowLeft') { move(-1); }
    if (event.key === 'Escape') { setControlsVisible(true); }
    if (event.key === ' ') {
      event.preventDefault();
      paused = !paused;
      scheduleAdvance();
    }
  };

  /* Initial load: try rich /api/state first, falling back to /api/photos for plain folders */
  requestJson('GET', '/api/state?role=display', null, function (err, state) {
    if (!err && state) {
      applyServerState(state, true);
      el('intro').textContent = slides.length
        ? (slides.length + ' curated slides ready · changing every ' + intervalSec + 's with ambient color matte.')
        : 'Three-slide connection demo ready. Run extract_iphone_album.py or pass --photos.';
      el('start').disabled = false;
      if (/(?:^|[?&])autostart=1(?:&|$)/.test(window.location.search)) {
        startAlbum(false);
      }
      return;
    }
    requestJson('GET', '/api/photos', null, function (err2, data) {
      if (err2 || !data) {
        var cachedState = loadOfflineState();
        if (cachedState && cachedState.slides && cachedState.slides.length) {
          applyServerState(cachedState, true);
          el('intro').textContent = slides.length + ' cached slides ready offline.';
          el('start').disabled = false;
          if (/(?:^|[?&])autostart=1(?:&|$)/.test(window.location.search)) {
            startAlbum(false);
          }
          return;
        }
        el('intro').textContent = 'Cannot reach album server. Check Wi-Fi and reload.';
        return;
      }
      slides = data.photos || [];
      el('intro').textContent = slides.length
        ? (slides.length + ' photos ready · changing every 10 seconds.')
        : 'Three-slide connection demo ready.';
      el('start').disabled = false;
      renderCurrent();
      if (/(?:^|[?&])autostart=1(?:&|$)/.test(window.location.search)) {
        startAlbum(false);
      }
    });
  });

  registerOfflineWorker();
  pingServer();
  setInterval(pollState, 10000);
  setInterval(pingServer, 20000);
  setInterval(updateClock, 15000);
}());
