// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
/* Offline-capable Service Worker for Ambient Photo Frame (tablets, TVs, browsers & modern Silk). */
'use strict';

var SHELL_CACHE = 'ambient-shell-v1';
var MEDIA_CACHE = 'ambient-media-v1';

var SHELL_ASSETS = [
  '/',
  '/gallery',
  '/remote',
  '/album.css',
  '/app.css',
  '/album.js',
  '/gallery.js',
  '/remote.js',
  '/silence.wav',
  '/fonts/InstrumentSerif-Regular.ttf',
  '/fonts/InstrumentSerif-Italic.ttf',
  '/fonts/Manrope-Variable.ttf'
];

self.addEventListener('install', function (event) {
  self.skipWaiting();
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      /* Pre-cache shell opportunistically; ignore individual failures if unauthenticated */
      return Promise.all(
        SHELL_ASSETS.map(function (url) {
          return fetch(url, { credentials: 'same-origin' })
            .then(function (resp) {
              if (resp && resp.status === 200) {
                return cache.put(url, resp);
              }
            })
            .catch(function () {});
        })
      );
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (k) {
          if (k !== SHELL_CACHE && k !== MEDIA_CACHE) {
            return caches.delete(k);
          }
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

function canonicalApiKey(urlObj) {
  if (urlObj.pathname === '/api/state') {
    return urlObj.origin + '/api/state';
  }
  return urlObj.href;
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') {
    return;
  }
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  /* Never intercept key-bootstrap URLs so auth cookie redirect always executes on server */
  if (url.searchParams.has('key')) {
    return;
  }

  /* 1. Curated slide JPEGs: Cache-first for zero-bandwidth continuous rotation */
  if (url.pathname.indexOf('/photos/') === 0 || url.pathname.indexOf('/diptychs/') === 0) {
    event.respondWith(
      caches.open(MEDIA_CACHE).then(function (cache) {
        return cache.match(url.pathname).then(function (hit) {
          if (hit) {
            return hit;
          }
          return fetch(req).then(function (resp) {
            if (resp && resp.status === 200) {
              cache.put(url.pathname, resp.clone());
            }
            return resp;
          });
        });
      })
    );
    return;
  }

  /* 2. JSON state/album endpoints: Network-first with offline cache fallback */
  if (url.pathname.indexOf('/api/') === 0) {
    if (url.pathname === '/api/ping') {
      return;
    }
    var cacheKey = canonicalApiKey(url);
    event.respondWith(
      fetch(req)
        .then(function (resp) {
          if (resp && resp.status === 200) {
            var copy = resp.clone();
            caches.open(MEDIA_CACHE).then(function (cache) {
              cache.put(cacheKey, copy);
            });
          }
          return resp;
        })
        .catch(function () {
          return caches.open(MEDIA_CACHE).then(function (cache) {
            return cache.match(cacheKey).then(function (hit) {
              if (hit) {
                return hit;
              }
              return new Response(JSON.stringify({ error: 'offline' }), {
                status: 503,
                headers: { 'Content-Type': 'application/json' }
              });
            });
          });
        })
    );
    return;
  }

  /* 3. App shell routes & static assets (HTML, JS, CSS, fonts, silence.wav) */
  var shellLookup = (url.pathname === '/' || url.pathname === '/gallery' || url.pathname === '/remote')
    ? url.pathname
    : req;

  event.respondWith(
    fetch(req)
      .then(function (resp) {
        if (resp && resp.status === 200) {
          var clone = resp.clone();
          caches.open(SHELL_CACHE).then(function (cache) {
            cache.put(shellLookup, clone);
          });
        }
        return resp;
      })
      .catch(function () {
        return caches.open(SHELL_CACHE).then(function (cache) {
          return cache.match(shellLookup);
        });
      })
  );
});

var precacheInFlight = false;

self.addEventListener('message', function (event) {
  var data = event.data || {};
  if (data.type !== 'PRECACHE_SLIDES' || !Array.isArray(data.urls) || precacheInFlight) {
    return;
  }
  precacheInFlight = true;
  var urls = data.urls.slice(0, 260);
  var cachedCount = 0;

  event.waitUntil(
    caches.open(MEDIA_CACHE).then(function (cache) {
      var idx = 0;
      function nextBatch() {
        if (idx >= urls.length) {
          precacheInFlight = false;
          if (event.source && event.source.postMessage) {
            event.source.postMessage({
              type: 'PRECACHE_COMPLETE',
              cached: cachedCount,
              total: urls.length
            });
          }
          return Promise.resolve();
        }
        var batch = urls.slice(idx, idx + 4);
        idx += 4;
        return Promise.all(
          batch.map(function (rawUrl) {
            var u = new URL(rawUrl, self.location.origin);
            return cache.match(u.pathname).then(function (hit) {
              if (hit) {
                cachedCount += 1;
                return;
              }
              return fetch(u.href, { credentials: 'same-origin' })
                .then(function (resp) {
                  if (resp && resp.status === 200) {
                    cachedCount += 1;
                    return cache.put(u.pathname, resp);
                  }
                })
                .catch(function () {});
            });
          })
        ).then(nextBatch);
      }
      return nextBatch();
    }).catch(function () {
      precacheInFlight = false;
    })
  );
});
