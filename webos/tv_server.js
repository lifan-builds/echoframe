'use strict';

var http = require('http');
var fs = require('fs');
var path = require('path');
var urlMod = require('url');

var BASE_DIR = process.env.ECHOFRAME_DIR || '/media/developer/echoframe';
var PORT = parseInt(process.env.ECHOFRAME_PORT || '18663', 10) || 18663;
var ALLOWED_INTERVALS = { 10: true, 15: true, 30: true, 60: true, 300: true };

var MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.ttf': 'font/ttf',
  '.wav': 'audio/wav'
};

var STATIC_ROUTES = {
  '/': 'index.html',
  '/index.html': 'index.html',
  '/gallery': 'gallery.html',
  '/gallery/': 'gallery.html',
  '/remote': 'remote.html',
  '/remote/': 'remote.html'
};

function createEchoFrameApp(baseDir) {
  var rootDir = baseDir || BASE_DIR;
  var manifestPath = path.join(rootDir, 'manifest.json');
  var statePath = path.join(rootDir, 'state.json');
  var slidesDir = path.join(rootDir, 'slides');
  var webDir = path.join(rootDir, 'web');
  var fontsDir = path.join(rootDir, 'fonts');

  var manifestCache = null;
  var manifestMtime = 0;
  var recentClients = {};

  function defaultState() {
    return {
      revision: 1,
      command_seq: 0,
      last_command: { seq: 0, action: 'none', slide_id: null },
      active_channel: 'smart_frame',
      active_query: '',
      search_slide_ids: [],
      interval_sec: 15,
      paused: false,
      show_captions: true,
      show_clock: true,
      hidden_ids: []
    };
  }

  function loadState() {
    var st = defaultState();
    try {
      if (fs.existsSync(statePath)) {
        var parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
        if (parsed && typeof parsed === 'object') {
          Object.assign(st, parsed);
          if (!Array.isArray(st.hidden_ids)) st.hidden_ids = [];
          if (!Array.isArray(st.search_slide_ids)) st.search_slide_ids = [];
        }
      }
    } catch (e) {}
    return st;
  }

  var frameState = loadState();

  function saveState() {
    try {
      var tmp = statePath + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(frameState, null, 2), 'utf8');
      fs.renameSync(tmp, statePath);
    } catch (e) {
      try { fs.writeFileSync(statePath, JSON.stringify(frameState), 'utf8'); } catch (e2) {}
    }
  }

  function loadManifest() {
    try {
      var stat = fs.statSync(manifestPath);
      var mtime = stat.mtimeMs || stat.mtime.getTime();
      if (manifestCache && mtime === manifestMtime) {
        return manifestCache;
      }
      manifestCache = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      manifestMtime = mtime;
      return manifestCache;
    } catch (e) {
      return manifestCache;
    }
  }

  function buildHiddenMap() {
    var map = {};
    var arr = frameState.hidden_ids || [];
    for (var i = 0; i < arr.length; i++) {
      map[String(arr[i])] = true;
    }
    return map;
  }

  function isSlideHidden(slide, hiddenMap) {
    if (!slide) return true;
    if (hiddenMap[String(slide.id)]) return true;
    if (slide.photo_id && hiddenMap[String(slide.photo_id)]) return true;
    var mids = slide.member_ids || [];
    for (var i = 0; i < mids.length; i++) {
      if (hiddenMap[String(mids[i])]) return true;
    }
    return false;
  }

  function dayOfYearDistance(mmDd, nowDate) {
    if (!mmDd || mmDd.length !== 5 || mmDd.charAt(2) !== '-') return 999;
    var m = parseInt(mmDd.slice(0, 2), 10);
    var d = parseInt(mmDd.slice(3, 5), 10);
    if (!m || !d) return 999;
    var cand = Date.UTC(2024, m - 1, m === 2 ? Math.min(d, 28) : d);
    var refM = nowDate.getMonth();
    var refD = nowDate.getDate();
    var ref = Date.UTC(2024, refM, refM === 1 ? Math.min(refD, 28) : refD);
    var diff = Math.round(Math.abs(cand - ref) / 86400000);
    return Math.min(diff, 366 - diff);
  }

  function lexicalScore(query, slide) {
    var tokens = String(query || '').toLowerCase().split(/[^a-z0-9]+/).filter(function (t) { return t.length >= 2; });
    if (!tokens.length) return 0;
    var parts = [
      slide.display_caption || '',
      slide.search_detail || '',
      String(slide.visual_theme || '').replace(/_/g, ' '),
      String(slide.color_palette || '').replace(/_/g, ' '),
      slide.date_label || '',
      slide.age_badge || '',
      slide.year || ''
    ];
    if (Array.isArray(slide.channels)) parts = parts.concat(slide.channels);
    if (Array.isArray(slide.tags)) parts = parts.concat(slide.tags);
    var haystack = parts.join(' ').toLowerCase();
    var hits = 0;
    for (var i = 0; i < tokens.length; i++) {
      if (haystack.indexOf(tokens[i]) !== -1) hits++;
    }
    return hits / tokens.length;
  }

  function formatSlide(s) {
    var filename = s.filename || (s.id + '.jpg');
    return {
      id: s.id,
      filename: filename,
      src: '/slides/' + encodeURIComponent(filename),
      file_url: 'file://' + path.join(slidesDir, filename),
      label: s.display_caption || s.label || s.id,
      display_caption: s.display_caption || s.label || '',
      date_label: s.date_label || '',
      age_badge: s.age_badge || '',
      accent_rgb: s.accent_rgb || [24, 32, 44],
      kind: s.kind || 'single',
      orientation: s.orientation || 'landscape',
      aspect_ratio: s.aspect_ratio || 1.7778,
      frame_score: s.frame_score || 8.0,
      year: s.year || '',
      channels: s.channels || [],
      resolution_tier: 'tv_1080p'
    };
  }

  function slidesForChannel(manifest, hiddenMap, channelId) {
    if (!manifest || !Array.isArray(manifest.slides)) return [];
    var visible = [];
    for (var i = 0; i < manifest.slides.length; i++) {
      var s = manifest.slides[i];
      if (!isSlideHidden(s, hiddenMap)) visible.push(s);
    }
    if (channelId === 'all') return visible;
    if (channelId === 'smart_frame') {
      var def = visible.filter(function (x) { return x.in_default_frame !== false; });
      return def.length ? def : visible;
    }
    if (channelId === 'on_this_day') {
      var now = new Date();
      var near = visible.filter(function (x) { return dayOfYearDistance(x.month_day || '', now) <= 24; });
      if (near.length >= 4) return near;
      var curMonth = '-' + String(now.getMonth() + 1).padStart(2, '0') + '-';
      var seasonal = visible.filter(function (x) { return ('-' + (x.month_day || '') + '-').indexOf(curMonth) !== -1; });
      return seasonal.length ? seasonal : visible.slice(0, 24);
    }
    if (channelId === 'search' && frameState.search_slide_ids.length) {
      var byId = {};
      for (var k = 0; k < visible.length; k++) byId[visible[k].id] = visible[k];
      var matched = [];
      for (var j = 0; j < frameState.search_slide_ids.length; j++) {
        var found = byId[frameState.search_slide_ids[j]];
        if (found) matched.push(found);
      }
      return matched.length ? matched : visible.slice(0, 20);
    }
    var filtered = visible.filter(function (x) {
      return Array.isArray(x.channels) && x.channels.indexOf(channelId) !== -1;
    });
    return filtered.length ? filtered : visible;
  }

  function buildStatePayload() {
    var manifest = loadManifest();
    var hiddenMap = buildHiddenMap();
    var activeCh = frameState.active_channel || 'smart_frame';
    var activeQ = frameState.active_query || '';
    var rawSlides = slidesForChannel(manifest, hiddenMap, activeCh);
    var slides = rawSlides.map(formatSlide);

    var channelsOut = [];
    if (manifest && Array.isArray(manifest.channels)) {
      for (var i = 0; i < manifest.channels.length; i++) {
        var ch = manifest.channels[i];
        channelsOut.push({
          id: ch.id,
          name: ch.name,
          description: ch.description || '',
          count: slidesForChannel(manifest, hiddenMap, ch.id).length
        });
      }
      if (activeCh === 'search' && activeQ) {
        channelsOut.unshift({
          id: 'search',
          name: 'Search: "' + activeQ + '"',
          description: 'Search playlist',
          count: slides.length
        });
      }
    }

    return {
      demo: slides.length === 0,
      curated: Boolean(manifest),
      revision: frameState.revision || 1,
      command: frameState.last_command || { seq: 0, action: 'none', slide_id: null },
      active_channel: activeCh,
      active_query: activeQ,
      interval_sec: frameState.interval_sec || 15,
      paused: Boolean(frameState.paused),
      show_captions: frameState.show_captions !== false,
      show_clock: frameState.show_clock !== false,
      hidden_count: (frameState.hidden_ids || []).length,
      channels: channelsOut,
      slides: slides
    };
  }

  function runSearch(query, topK) {
    var manifest = loadManifest();
    var hiddenMap = buildHiddenMap();
    var q = String(query || '').trim();
    if (!q) {
      return {
        query: '',
        mode: 'default',
        results: slidesForChannel(manifest, hiddenMap, 'smart_frame').slice(0, topK).map(formatSlide)
      };
    }
    if (!manifest || !Array.isArray(manifest.slides)) {
      return { query: q, mode: 'empty', results: [] };
    }
    var scored = [];
    for (var i = 0; i < manifest.slides.length; i++) {
      var s = manifest.slides[i];
      if (isSlideHidden(s, hiddenMap)) continue;
      var sc = lexicalScore(q, s);
      if (sc > 0) scored.push([sc, s]);
    }
    scored.sort(function (a, b) {
      return (b[0] - a[0]) || ((b[1].frame_score || 0) - (a[1].frame_score || 0));
    });
    var sliced = scored.slice(0, topK);
    return {
      query: q,
      mode: 'lexical_tv',
      results: sliced.map(function (pair) {
        var obj = formatSlide(pair[1]);
        obj.score = Math.round(pair[0] * 10000) / 10000;
        return obj;
      })
    };
  }

  function classifyDevice(ua, role) {
    if (role === 'screensaver') return 'LG TV Idle Screensaver';
    if (role === 'tv_app') return 'LG TV EchoFrame App';
    var low = String(ua || '').toLowerCase();
    if (low.indexOf('web0s') !== -1 || low.indexOf('smart-tv') !== -1) return 'LG TV';
    if (low.indexOf('iphone') !== -1) return 'iPhone';
    if (low.indexOf('ipad') !== -1) return 'iPad';
    if (low.indexOf('macintosh') !== -1 || low.indexOf('mac os x') !== -1) return 'Mac';
    return role === 'display' ? 'TV Display' : 'LAN Viewer';
  }

  function recordHeartbeat(req, query) {
    var ua = String((req.headers && req.headers['user-agent']) || '').slice(0, 256);
    var role = String(query.role || '').slice(0, 24);
    var slide = String(query.slide || '').slice(0, 64);
    var caption = String(query.caption || '').slice(0, 120);
    var ip = (req.socket && req.socket.remoteAddress) || '127.0.0.1';
    ip = ip.replace(/^::ffff:/, '');
    var label = classifyDevice(ua, role);
    var key = ip + '::' + label;
    var prev = recentClients[key] || {};
    recentClients[key] = {
      ip: ip,
      user_agent: ua,
      device_name: label,
      role: role || prev.role || 'viewer',
      current_slide: slide || prev.current_slide || '',
      current_caption: caption || prev.current_caption || '',
      last_seen: new Date().toISOString()
    };
  }

  function safeFilename(rawName) {
    if (!rawName) return null;
    if (rawName.indexOf('/') !== -1 || rawName.indexOf('\\') !== -1 || rawName.indexOf('..') !== -1 || rawName.charAt(0) === '.') {
      return null;
    }
    return rawName;
  }

  function sendJson(res, status, payload) {
    res.writeHead(status, {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'Access-Control-Allow-Origin': '*'
    });
    res.end(JSON.stringify(payload));
  }

  function sendFile(res, req, fullPath, cacheControl) {
    fs.stat(fullPath, function (err, stat) {
      if (err || !stat || !stat.isFile()) {
        sendJson(res, 404, { error: 'Not found' });
        return;
      }
      var ext = path.extname(fullPath).toLowerCase();
      var mime = MIME_TYPES[ext] || 'application/octet-stream';
      var etag = '"' + stat.size + '-' + Math.floor(stat.mtimeMs || stat.mtime.getTime()) + '"';
      if (req.headers && req.headers['if-none-match'] === etag) {
        res.writeHead(304, {
          'ETag': etag,
          'Cache-Control': cacheControl || 'public, max-age=86400',
          'Access-Control-Allow-Origin': '*'
        });
        res.end();
        return;
      }
      res.writeHead(200, {
        'Content-Type': mime,
        'Content-Length': stat.size,
        'ETag': etag,
        'Cache-Control': cacheControl || 'public, max-age=86400',
        'Access-Control-Allow-Origin': '*'
      });
      if (req.method === 'HEAD') {
        res.end();
        return;
      }
      var stream = fs.createReadStream(fullPath);
      stream.on('error', function () {
        try { res.end(); } catch (e) {}
      });
      stream.pipe(res);
    });
  }

  function readJsonBody(req, callback) {
    var chunks = [];
    var total = 0;
    req.on('data', function (chunk) {
      total += chunk.length;
      if (total <= 65536) chunks.push(chunk);
    });
    req.on('end', function () {
      try {
        var raw = Buffer.concat(chunks).toString('utf8');
        callback(null, raw ? JSON.parse(raw) : {});
      } catch (err) {
        callback(err, null);
      }
    });
    req.on('error', function (err) { callback(err, null); });
  }

  function handleRequest(req, res) {
    req.on('error', function () {});
    res.on('error', function () {});

    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, HEAD, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
      });
      res.end();
      return;
    }

    var parsed = urlMod.parse(req.url || '/', true);
    var pathname = parsed.pathname || '/';
    var query = parsed.query || {};

    if (req.method === 'GET' || req.method === 'HEAD') {
      if (pathname === '/api/ping') {
        recordHeartbeat(req, query);
        return sendJson(res, 200, { ok: true });
      }

      if (pathname === '/api/state') {
        if (query.role) recordHeartbeat(req, query);
        return sendJson(res, 200, buildStatePayload());
      }

      if (pathname === '/api/photos') {
        var st = buildStatePayload();
        return sendJson(res, 200, { demo: st.demo, photos: st.slides });
      }

      if (pathname === '/api/album') {
        var manifest = loadManifest();
        var hiddenMap = buildHiddenMap();
        var state = buildStatePayload();
        if (!manifest) {
          return sendJson(res, 200, {
            curated: false,
            stats: {},
            hidden_ids: frameState.hidden_ids.slice().sort(),
            channels: state.channels,
            slides: [],
            photos: [],
            state: state
          });
        }
        var allSlides = (manifest.slides || []).map(function (s) {
          var item = formatSlide(s);
          item.hidden = isSlideHidden(s, hiddenMap);
          item.in_default_frame = s.in_default_frame !== false;
          item.member_ids = s.member_ids || (s.photo_id ? [s.photo_id] : []);
          item.visual_theme = s.visual_theme || '';
          item.color_palette = s.color_palette || '';
          item.tags = s.tags || [];
          return item;
        });
        return sendJson(res, 200, {
          curated: true,
          stats: manifest.stats || {},
          hidden_ids: frameState.hidden_ids.slice().sort(),
          channels: state.channels,
          slides: allSlides,
          photos: [],
          state: state
        });
      }

      if (pathname === '/api/search') {
        var q = String(query.q || '').trim().slice(0, 200);
        var limit = parseInt(query.limit || '36', 10);
        if (!isFinite(limit) || limit < 1) limit = 36;
        if (limit > 120) limit = 120;
        return sendJson(res, 200, runSearch(q, limit));
      }

      if (pathname === '/api/status') {
        var cur = buildStatePayload();
        var clients = Object.keys(recentClients).map(function (k) { return recentClients[k]; });
        return sendJson(res, 200, {
          clients: clients,
          active_channel: cur.active_channel,
          active_query: cur.active_query,
          interval_sec: cur.interval_sec,
          paused: cur.paused,
          slide_count: cur.slides.length
        });
      }

      if (pathname.indexOf('/slides/') === 0 || pathname.indexOf('/photos/') === 0 || pathname.indexOf('/diptychs/') === 0) {
        var raw = decodeURIComponent(pathname.slice(pathname.indexOf('/', 1) + 1));
        var safe = safeFilename(raw);
        if (!safe) return sendJson(res, 404, { error: 'Not found' });
        return sendFile(res, req, path.join(slidesDir, safe), 'public, max-age=86400');
      }

      if (pathname.indexOf('/fonts/') === 0) {
        var fontName = safeFilename(decodeURIComponent(pathname.slice(7)));
        if (!fontName) return sendJson(res, 404, { error: 'Not found' });
        return sendFile(res, req, path.join(fontsDir, fontName), 'public, max-age=86400');
      }

      var mapped = STATIC_ROUTES[pathname];
      if (mapped) {
        return sendFile(res, req, path.join(webDir, mapped), 'no-cache');
      }

      var assetName = safeFilename(decodeURIComponent(pathname.replace(/^\/+/, '')));
      if (assetName) {
        var assetPath = path.join(webDir, assetName);
        if (fs.existsSync(assetPath)) {
          return sendFile(res, req, assetPath, 'no-cache');
        }
      }

      return sendJson(res, 404, { error: 'Not found' });
    }

    if (req.method === 'POST') {
      if (pathname === '/api/control') {
        return readJsonBody(req, function (err, body) {
          if (err || !body) return sendJson(res, 400, { error: 'Invalid JSON' });
          var changed = false;

          if (typeof body.search_query === 'string') {
            var sq = body.search_query.trim();
            if (sq) {
              var sres = runSearch(sq, 40);
              frameState.active_channel = 'search';
              frameState.active_query = sq;
              frameState.search_slide_ids = sres.results.map(function (r) { return r.id; });
            } else {
              frameState.active_channel = 'smart_frame';
              frameState.active_query = '';
              frameState.search_slide_ids = [];
            }
            changed = true;
          }

          if (typeof body.channel === 'string' && body.channel.trim()) {
            frameState.active_channel = body.channel.trim();
            if (frameState.active_channel !== 'search') {
              frameState.active_query = '';
              frameState.search_slide_ids = [];
            }
            changed = true;
          }

          if (body.interval_sec !== undefined) {
            var iv = parseInt(body.interval_sec, 10);
            if (ALLOWED_INTERVALS[iv]) {
              frameState.interval_sec = iv;
              changed = true;
            }
          }

          if (typeof body.paused === 'boolean') {
            frameState.paused = body.paused;
            changed = true;
          }

          if (typeof body.show_captions === 'boolean') {
            frameState.show_captions = body.show_captions;
            changed = true;
          }

          if (typeof body.show_clock === 'boolean') {
            frameState.show_clock = body.show_clock;
            changed = true;
          }

          var action = typeof body.action === 'string' ? body.action.trim() : '';
          var slideId = typeof body.slide_id === 'string' ? body.slide_id.trim() : null;
          if (action === 'next' || action === 'prev' || action === 'goto') {
            frameState.command_seq = (frameState.command_seq || 0) + 1;
            frameState.last_command = { seq: frameState.command_seq, action: action, slide_id: slideId };
            changed = true;
          }

          if (changed) {
            frameState.revision = (frameState.revision || 1) + 1;
            saveState();
          }
          return sendJson(res, 200, { ok: true, state: buildStatePayload() });
        });
      }

      if (pathname === '/api/hide') {
        return readJsonBody(req, function (err, body) {
          if (err || !body) return sendJson(res, 400, { error: 'Invalid JSON' });
          var targetId = String(body.id || '').trim();
          var hide = body.hidden !== false;
          if (!targetId) return sendJson(res, 400, { error: 'Missing id' });

          var setMap = buildHiddenMap();
          if (hide) setMap[targetId] = true;
          else delete setMap[targetId];
          frameState.hidden_ids = Object.keys(setMap).sort();
          frameState.revision = (frameState.revision || 1) + 1;
          saveState();
          return sendJson(res, 200, {
            ok: true,
            hidden_count: frameState.hidden_ids.length,
            state: buildStatePayload()
          });
        });
      }

      return sendJson(res, 404, { error: 'Not found' });
    }

    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  return {
    handleRequest: handleRequest,
    buildStatePayload: buildStatePayload
  };
}

function startServer(opts) {
  opts = opts || {};
  var port = opts.port !== undefined ? opts.port : PORT;
  var host = opts.host || '0.0.0.0';
  var app = createEchoFrameApp(opts.baseDir || BASE_DIR);
  var server = http.createServer(app.handleRequest);
  server.on('clientError', function (err, socket) {
    try { socket.destroy(); } catch (e) {}
  });
  server.on('error', function (err) {
    if (err && err.code === 'EADDRINUSE') {
      console.log('[echoframe] Port ' + port + ' already in use');
      return;
    }
    console.error('[echoframe] Server error:', err && err.message);
  });
  server.listen(port, host, function () {
    console.log('[echoframe] Listening on http://' + host + ':' + port);
  });
  return server;
}

exports.createEchoFrameApp = createEchoFrameApp;
exports.startServer = startServer;

if (!process.env.ECHOFRAME_NO_AUTOSTART) {
  startServer();
}
