// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
/**
 * Private Always-On Ambient Photo Frame Worker (Cloudflare Workers + Private R2).
 *
 * Security & Privacy Invariants:
 * 1. Fails closed (`503`) if `env.FRAME_ACCESS_KEY` is unset or < 24 chars.
 * 2. Every route (except `/robots.txt` -> `Disallow: /`) requires a valid HMAC-derived
 *    `__Host-frame_auth` session cookie, `?key=<FRAME_ACCESS_KEY>` bootstrap query, or
 *    `Authorization: Bearer <FRAME_ACCESS_KEY>` / `X-Frame-Key` header verified in constant time.
 * 3. Zero public indexing (`X-Robots-Tag: noindex, nofollow, noarchive, nosnippet, noimageindex`,
 *    `Referrer-Policy: no-referrer`, `Cache-Control: private, ...`).
 * 4. Implements the full `serve_album.py` API (`/api/state`, `/api/photos`, `/api/album`,
 *    `/api/control`, `/api/hide`, `/api/search`, `/api/status`, `/api/ping`, `/photos/*`,
 *    `/diptychs/*`) backed by Private R2 so tablets, TVs, phones, and Echo Silk work 24/7
 *    with the MacBook completely powered off.
 */

const ALLOWED_INTERVALS = new Set([10, 15, 30, 60, 300]);
const IMAGE_MIMES = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
};

const STATIC_ROUTE_MAP = {
  '/': '/index.html',
  '/gallery': '/gallery.html',
  '/remote': '/remote.html',
};

const PRIVACY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'X-Robots-Tag': 'noindex, nofollow, noarchive, nosnippet, noimageindex',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
};

// Isolate-level read-through caches to minimize R2 Class B operations while polling
let cachedManifest = null;
let cachedManifestAt = 0;
let cachedEmbedMeta = null;
let cachedEmbedF32 = null;
let cachedState = null;
let cachedStateAt = 0;
let cachedHidden = null;
let cachedHiddenAt = 0;
const recentClients = new Map();
const queryEmbedCache = new Map();

function withPrivacyHeaders(headersInit = {}, cacheControl = 'private, no-store') {
  const h = new Headers(headersInit);
  for (const [k, v] of Object.entries(PRIVACY_HEADERS)) {
    h.set(k, v);
  }
  if (cacheControl && !h.has('Cache-Control')) {
    h.set('Cache-Control', cacheControl);
  }
  return h;
}

function jsonResponse(payload, status = 200, cacheControl = 'private, no-store') {
  return new Response(JSON.stringify(payload), {
    status,
    headers: withPrivacyHeaders({ 'Content-Type': 'application/json; charset=utf-8' }, cacheControl),
  });
}

function bytesToHex(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, '0');
  }
  return out;
}

function timingSafeEqualStr(a, b) {
  const enc = new TextEncoder();
  const ba = enc.encode(String(a || ''));
  const bb = enc.encode(String(b || ''));
  const maxLen = Math.max(ba.length, bb.length, 1);
  let diff = ba.length ^ bb.length;
  for (let i = 0; i < maxLen; i++) {
    diff |= (ba[i] || 0) ^ (bb[i] || 0);
  }
  return diff === 0 && ba.length > 0;
}

async function computeSessionToken(accessKey) {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    enc.encode(accessKey),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', cryptoKey, enc.encode('ambient-frame-session-v1'));
  return bytesToHex(new Uint8Array(sig));
}

function parseCookies(cookieHeader) {
  const out = {};
  if (!cookieHeader) return out;
  for (const part of cookieHeader.split(';')) {
    const eq = part.indexOf('=');
    if (eq > 0) {
      const k = part.slice(0, eq).trim();
      const v = part.slice(eq + 1).trim();
      out[k] = v;
    }
  }
  return out;
}

function cookieNameForUrl(urlObj) {
  return urlObj.protocol === 'https:' ? '__Host-frame_auth' : 'frame_auth';
}

function buildAuthCookieHeader(urlObj, sessionToken) {
  const name = cookieNameForUrl(urlObj);
  const parts = [`${name}=${sessionToken}`, 'HttpOnly', 'SameSite=Lax', 'Path=/', 'Max-Age=31536000'];
  if (urlObj.protocol === 'https:') {
    parts.push('Secure');
  }
  return parts.join('; ');
}

function renderUnlockHtml(returnTo, invalidAttempt = false) {
  const safeReturn = String(returnTo || '/').replace(/["<>]/g, '');
  const errHtml = invalidAttempt
    ? '<p style="color:#f28b82;font-size:14px;margin:0 0 14px;">Invalid access key.</p>'
    : '';
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <title>Private Frame</title>
  <style>
    body { margin:0; min-height:100vh; display:grid; place-items:center; background:#0b0e13; color:#eef2f7; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    form { width:min(360px, 88vw); padding:28px; border-radius:16px; background:#141922; border:1px solid rgba(255,255,255,0.1); box-shadow:0 18px 48px rgba(0,0,0,0.45); }
    h1 { margin:0 0 8px; font-size:20px; font-weight:600; }
    p { margin:0 0 18px; font-size:14px; color:#9aa5b5; line-height:1.45; }
    input[type="password"] { width:100%; box-sizing:border-box; padding:11px 13px; border-radius:10px; border:1px solid rgba(255,255,255,0.18); background:#0b0e13; color:#fff; font-size:15px; margin-bottom:14px; }
    button { width:100%; padding:11px 14px; border-radius:10px; border:0; background:#e6edf7; color:#0b0e13; font-weight:600; font-size:15px; cursor:pointer; }
  </style>
</head>
<body>
  <form method="POST" action="/__auth">
    <h1>Private Photo Frame</h1>
    <p>Enter your private frame access key once to unlock this display.</p>
    ${errHtml}
    <input type="hidden" name="next" value="${safeReturn}">
    <input type="password" name="key" placeholder="Access key" autocomplete="current-password" required autofocus>
    <button type="submit">Unlock Display</button>
  </form>
</body>
</html>`;
}

function classifyDeviceLabel(ua, role = '') {
  const low = String(ua || '').toLowerCase();
  if (low.includes('silk') && (low.includes('a1nl4bvlq4l3n3') || low.includes('echo') || low.includes('aft'))) {
    return 'Echo Show (Silk)';
  }
  if (low.includes('silk')) return 'Amazon Silk Display';
  if (low.includes('ipad')) return 'iPad';
  if (low.includes('iphone')) return 'iPhone';
  if (low.includes('macintosh') || low.includes('mac os x')) return 'Mac';
  if (low.includes('smart-tv') || low.includes('tizen') || low.includes('web0s') || low.includes('bravia')) return 'Smart TV';
  if (low.includes('android')) return 'Android Display';
  return role === 'display' ? 'Display Frame' : 'Cloud Viewer';
}

function dayOfYearDistance(mmDd, nowDate) {
  if (!mmDd || mmDd.length !== 5 || mmDd[2] !== '-') return 999;
  const m = parseInt(mmDd.slice(0, 2), 10);
  const d = parseInt(mmDd.slice(3, 5), 10);
  if (!m || !d) return 999;
  const cand = Date.UTC(2024, m - 1, m === 2 ? Math.min(d, 28) : d);
  const ref = Date.UTC(2024, nowDate.getUTCMonth(), nowDate.getUTCMonth() === 1 ? Math.min(nowDate.getUTCDate(), 28) : nowDate.getUTCDate());
  const diff = Math.round(Math.abs(cand - ref) / 86400000);
  return Math.min(diff, 366 - diff);
}

function lexicalScore(query, slide) {
  const tokens = String(query || '').toLowerCase().split(/[^a-z0-9]+/).filter((t) => t.length >= 2);
  if (!tokens.length) return 0;
  const haystack = [
    slide.display_caption || '',
    slide.search_detail || '',
    String(slide.visual_theme || '').replace(/_/g, ' '),
    String(slide.color_palette || '').replace(/_/g, ' '),
    slide.date_label || '',
    slide.age_badge || '',
    slide.year || '',
    ...(slide.channels || []),
    ...(slide.tags || []),
  ].join(' ').toLowerCase();
  let hits = 0;
  for (const tok of tokens) {
    if (haystack.includes(tok)) hits += 1;
  }
  return hits / tokens.length;
}

function float16ToFloat32Array(u16) {
  const out = new Float32Array(u16.length);
  for (let i = 0; i < u16.length; i++) {
    const h = u16[i];
    const s = (h & 0x8000) >> 15;
    const e = (h & 0x7c00) >> 10;
    const f = h & 0x03ff;
    let val;
    if (e === 0) {
      val = (s ? -1 : 1) * Math.pow(2, -14) * (f / 1024);
    } else if (e === 0x1f) {
      val = f ? NaN : (s ? -Infinity : Infinity);
    } else {
      val = (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + f / 1024);
    }
    out[i] = val;
  }
  return out;
}

function defaultStateObj() {
  return {
    revision: 1,
    command_seq: 0,
    last_command: { seq: 0, action: 'none', slide_id: null },
    active_channel: 'smart_frame',
    active_query: '',
    search_slide_ids: [],
    interval_sec: 10,
    paused: false,
    show_captions: true,
    show_clock: true,
  };
}

async function loadManifest(bucket) {
  const now = Date.now();
  if (cachedManifest && now - cachedManifestAt < 30000) {
    return cachedManifest;
  }
  if (!bucket) return null;
  const obj = await bucket.get('manifest.json');
  if (!obj) return null;
  cachedManifest = await obj.json();
  cachedManifestAt = now;
  return cachedManifest;
}

async function loadHiddenSet(bucket) {
  const now = Date.now();
  if (cachedHidden && now - cachedHiddenAt < 10000) {
    return cachedHidden;
  }
  if (!bucket) return new Set();
  const obj = await bucket.get('state/hidden.json');
  if (!obj) {
    cachedHidden = new Set();
    cachedHiddenAt = now;
    return cachedHidden;
  }
  try {
    const arr = await obj.json();
    cachedHidden = new Set(Array.isArray(arr) ? arr.map(String) : []);
  } catch {
    cachedHidden = new Set();
  }
  cachedHiddenAt = now;
  return cachedHidden;
}

async function saveHiddenSet(bucket, hiddenSet) {
  cachedHidden = new Set(hiddenSet);
  cachedHiddenAt = Date.now();
  if (bucket) {
    await bucket.put('state/hidden.json', JSON.stringify([...hiddenSet].sort()), {
      httpMetadata: { contentType: 'application/json' },
    });
  }
}

async function loadFrameState(bucket) {
  const now = Date.now();
  if (cachedState && now - cachedStateAt < 5000) {
    return cachedState;
  }
  if (!bucket) {
    cachedState = cachedState || defaultStateObj();
    return cachedState;
  }
  const obj = await bucket.get('state/frame_state.json');
  if (!obj) {
    cachedState = defaultStateObj();
    cachedStateAt = now;
    return cachedState;
  }
  try {
    cachedState = { ...defaultStateObj(), ...(await obj.json()) };
  } catch {
    cachedState = defaultStateObj();
  }
  cachedStateAt = now;
  return cachedState;
}

async function saveFrameState(bucket, nextState) {
  cachedState = nextState;
  cachedStateAt = Date.now();
  if (bucket) {
    await bucket.put('state/frame_state.json', JSON.stringify(nextState), {
      httpMetadata: { contentType: 'application/json' },
    });
  }
}

function isSlideHidden(slide, hiddenSet) {
  if (hiddenSet.has(slide.id)) return true;
  if (slide.photo_id && hiddenSet.has(slide.photo_id)) return true;
  for (const mid of slide.member_ids || []) {
    if (hiddenSet.has(mid)) return true;
  }
  return false;
}

function formatFrameSlide(s) {
  return {
    id: s.id,
    src: s.src,
    label: s.display_caption || s.label || s.id,
    display_caption: s.display_caption || s.label || '',
    date_label: s.date_label || '',
    age_badge: s.age_badge || '',
    accent_rgb: s.accent_rgb || [24, 32, 44],
    kind: s.kind || 'single',
    orientation: s.orientation || 'landscape',
    aspect_ratio: s.aspect_ratio || 1.5,
    frame_score: s.frame_score || 8.0,
    year: s.year || '',
    channels: s.channels || [],
    resolution_tier: s.resolution_tier || 'full_2048',
  };
}

function slidesForChannel(manifest, hiddenSet, frameState, channelId) {
  if (!manifest || !Array.isArray(manifest.slides)) return [];
  const visible = manifest.slides.filter((s) => !isSlideHidden(s, hiddenSet));
  if (channelId === 'all') return visible;
  if (channelId === 'smart_frame') {
    const res = visible.filter((s) => s.in_default_frame !== false);
    return res.length ? res : visible;
  }
  if (channelId === 'on_this_day') {
    const now = new Date();
    const near = visible.filter((s) => dayOfYearDistance(s.month_day || '', now) <= 24);
    if (near.length >= 4) return near;
    const curMonth = `-${String(now.getUTCMonth() + 1).padStart(2, '0')}-`;
    const seasonal = visible.filter((s) => `-${s.month_day || ''}-`.includes(curMonth));
    return seasonal.length ? seasonal : visible.slice(0, 24);
  }
  if (channelId === 'search' && Array.isArray(frameState.search_slide_ids) && frameState.search_slide_ids.length) {
    const byId = new Map(visible.map((s) => [s.id, s]));
    const matched = frameState.search_slide_ids.map((id) => byId.get(id)).filter(Boolean);
    return matched.length ? matched : visible.slice(0, 20);
  }
  const filtered = visible.filter((s) => (s.channels || []).includes(channelId));
  return filtered.length ? filtered : visible;
}

function buildStatePayload(manifest, hiddenSet, frameState) {
  const activeCh = frameState.active_channel || 'smart_frame';
  const activeQ = frameState.active_query || '';
  const slides = slidesForChannel(manifest, hiddenSet, frameState, activeCh).map(formatFrameSlide);

  let channelsOut = [];
  if (manifest && Array.isArray(manifest.channels)) {
    channelsOut = manifest.channels.map((ch) => ({
      id: ch.id,
      name: ch.name,
      description: ch.description || '',
      count: slidesForChannel(manifest, hiddenSet, frameState, ch.id).length,
    }));
    if (activeCh === 'search' && activeQ) {
      channelsOut.unshift({
        id: 'search',
        name: `Search: "${activeQ}"`,
        description: 'Semantic search playlist',
        count: slides.length,
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
    interval_sec: frameState.interval_sec || 10,
    paused: Boolean(frameState.paused),
    show_captions: frameState.show_captions !== false,
    show_clock: frameState.show_clock !== false,
    hidden_count: hiddenSet.size,
    channels: channelsOut,
    slides,
  };
}

async function loadEmbeddingsIfAvailable(bucket) {
  if (cachedEmbedMeta && cachedEmbedF32) {
    return { meta: cachedEmbedMeta, matrix: cachedEmbedF32 };
  }
  if (!bucket) return null;
  const [metaObj, binObj] = await Promise.all([
    bucket.get('embeddings_meta.json'),
    bucket.get('embeddings.f16.bin'),
  ]);
  if (!metaObj || !binObj) return null;
  const meta = await metaObj.json();
  const buf = await binObj.arrayBuffer();
  const u16 = new Uint16Array(buf);
  cachedEmbedMeta = meta;
  cachedEmbedF32 = float16ToFloat32Array(u16);
  return { meta: cachedEmbedMeta, matrix: cachedEmbedF32 };
}

async function embedQueryOpenAI(query, env) {
  const apiKey = env.OPENAI_API_KEY;
  if (!apiKey) return null;
  const normKey = query.trim().toLowerCase();
  if (queryEmbedCache.has(normKey)) {
    return queryEmbedCache.get(normKey);
  }
  const baseUrl = (env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/+$/, '');
  try {
    const resp = await fetch(`${baseUrl}/embeddings`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ model: 'text-embedding-3-large', input: [query.trim()] }),
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    const vec = data.data[0].embedding;
    let sumSq = 0;
    for (let i = 0; i < vec.length; i++) sumSq += vec[i] * vec[i];
    const norm = Math.sqrt(sumSq) || 1;
    const f32 = new Float32Array(vec.length);
    for (let i = 0; i < vec.length; i++) f32[i] = vec[i] / norm;
    if (queryEmbedCache.size > 128) queryEmbedCache.clear();
    queryEmbedCache.set(normKey, f32);
    return f32;
  } catch {
    return null;
  }
}

async function runSearch(query, topK, manifest, hiddenSet, frameState, env) {
  const q = String(query || '').trim();
  if (!q) {
    const slides = slidesForChannel(manifest, hiddenSet, frameState, 'smart_frame').slice(0, topK).map(formatFrameSlide);
    return { query: '', mode: 'default', results: slides };
  }
  if (!manifest || !Array.isArray(manifest.slides)) {
    return { query: q, mode: 'empty', results: [] };
  }
  const candidates = manifest.slides.filter((s) => !isSlideHidden(s, hiddenSet));
  if (!candidates.length) {
    return { query: q, mode: 'empty', results: [] };
  }

  let mode = 'lexical_fallback';
  let scored = [];

  const [qVec, embPack] = await Promise.all([
    embedQueryOpenAI(q, env),
    env.OPENAI_API_KEY ? loadEmbeddingsIfAvailable(env.ALBUM_BUCKET) : Promise.resolve(null),
  ]);

  if (qVec && embPack && Array.isArray(embPack.meta.ids) && embPack.meta.dim === qVec.length) {
    mode = 'openai_embeddings';
    const dim = embPack.meta.dim;
    const ids = embPack.meta.ids;
    const mat = embPack.matrix;
    const cosById = new Map();
    for (let row = 0; row < ids.length; row++) {
      const offset = row * dim;
      let dot = 0;
      for (let d = 0; d < dim; d++) {
        dot += mat[offset + d] * qVec[d];
      }
      cosById.set(ids[row], dot);
    }
    for (const s of candidates) {
      const cos = cosById.get(s.id) || 0;
      const lex = lexicalScore(q, s);
      scored.push([cos * 0.85 + lex * 0.15, s]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    const topScore = scored[0] ? scored[0][0] : 0;
    const cutoff = Math.max(0.20, topScore - 0.14);
    let filtered = scored.filter(([sc]) => sc >= cutoff).slice(0, topK);
    if (filtered.length < 4) {
      filtered = scored.slice(0, Math.min(12, scored.length));
    }
    scored = filtered;
  } else {
    for (const s of candidates) {
      const sc = lexicalScore(q, s);
      if (sc > 0) scored.push([sc, s]);
    }
    scored.sort((a, b) => (b[0] - a[0]) || ((b[1].frame_score || 0) - (a[1].frame_score || 0)));
    scored = scored.slice(0, topK);
  }

  const results = scored.map(([sc, s]) => ({
    ...formatFrameSlide(s),
    score: Math.round(sc * 10000) / 10000,
  }));
  return { query: q, mode, results };
}

function recordHeartbeat(request, urlObj) {
  const ua = (request.headers.get('User-Agent') || '').slice(0, 512);
  const role = (urlObj.searchParams.get('role') || '').slice(0, 24);
  const slideId = (urlObj.searchParams.get('slide') || '').slice(0, 64);
  const caption = (urlObj.searchParams.get('caption') || '').slice(0, 120);
  const ip = request.headers.get('CF-Connecting-IP') || 'edge-client';
  const key = `${ip}::${classifyDeviceLabel(ua, role)}`;
  const prev = recentClients.get(key) || {};
  recentClients.set(key, {
    ip,
    user_agent: ua,
    device_name: classifyDeviceLabel(ua, role || prev.role || ''),
    role: role || prev.role || 'viewer',
    current_slide: slideId || prev.current_slide || '',
    current_caption: caption || prev.current_caption || '',
    last_seen: new Date().toISOString(),
  });
  if (recentClients.size > 32) {
    const oldestKey = recentClients.keys().next().value;
    recentClients.delete(oldestKey);
  }
}

function validateSafeMediaFilename(rawName) {
  if (!rawName || rawName.includes('/') || rawName.includes('\\') || rawName.includes('..') || rawName.startsWith('.')) {
    return null;
  }
  const dot = rawName.lastIndexOf('.');
  if (dot <= 0) return null;
  const ext = rawName.slice(dot).toLowerCase();
  if (!IMAGE_MIMES[ext]) return null;
  return { name: rawName, mime: IMAGE_MIMES[ext] };
}

function loadBundleState(bucket) {
  return Promise.all([
    loadManifest(bucket),
    loadHiddenSet(bucket),
    loadFrameState(bucket),
  ]);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const pathname = url.pathname;

    // 1. Always disallow search engine crawlers without leaking anything
    if (pathname === '/robots.txt') {
      return new Response('User-agent: *\nDisallow: /\n', {
        status: 200,
        headers: withPrivacyHeaders({ 'Content-Type': 'text/plain; charset=utf-8' }, 'public, max-age=3600'),
      });
    }

    // 2. Fail closed if FRAME_ACCESS_KEY secret has not been configured
    const accessKey = String(env.FRAME_ACCESS_KEY || '').trim();
    if (accessKey.length < 24) {
      return jsonResponse(
        { error: 'Frame is locked: configure FRAME_ACCESS_KEY secret (min 24 chars).' },
        503
      );
    }

    const expectedSession = await computeSessionToken(accessKey);

    // 3. Handle POST /__auth form login
    if (pathname === '/__auth' && request.method === 'POST') {
      let submittedKey = '';
      let nextPath = '/';
      try {
        const form = await request.formData();
        submittedKey = String(form.get('key') || '').trim();
        const candidateNext = String(form.get('next') || '/').trim();
        if (candidateNext.startsWith('/') && !candidateNext.startsWith('//')) {
          nextPath = candidateNext;
        }
      } catch {}

      if (timingSafeEqualStr(submittedKey, accessKey)) {
        return new Response(null, {
          status: 303,
          headers: withPrivacyHeaders({
            'Location': nextPath,
            'Set-Cookie': buildAuthCookieHeader(url, expectedSession),
          }),
        });
      }
      return new Response(renderUnlockHtml(nextPath, true), {
        status: 401,
        headers: withPrivacyHeaders({ 'Content-Type': 'text/html; charset=utf-8' }),
      });
    }

    // 4. Handle one-time `?key=<FRAME_ACCESS_KEY>` bootstrap -> set cookie & clean redirect
    const queryKey = url.searchParams.get('key');
    if (queryKey !== null) {
      if (timingSafeEqualStr(queryKey, accessKey)) {
        const cleanUrl = new URL(url.href);
        cleanUrl.searchParams.delete('key');
        return new Response(null, {
          status: 302,
          headers: withPrivacyHeaders({
            'Location': cleanUrl.pathname + cleanUrl.search,
            'Set-Cookie': buildAuthCookieHeader(url, expectedSession),
          }),
        });
      }
      return new Response(renderUnlockHtml(pathname, true), {
        status: 401,
        headers: withPrivacyHeaders({ 'Content-Type': 'text/html; charset=utf-8' }),
      });
    }

    // 5. Verify session cookie or Bearer / X-Frame-Key header
    const cookies = parseCookies(request.headers.get('Cookie') || '');
    const cookieVal = cookies[cookieNameForUrl(url)] || cookies.frame_auth || '';
    const authHeader = request.headers.get('Authorization') || '';
    const bearerKey = authHeader.startsWith('Bearer ') ? authHeader.slice(7).trim() : '';
    const headerKey = (request.headers.get('X-Frame-Key') || '').trim();

    const isAuthed =
      timingSafeEqualStr(cookieVal, expectedSession) ||
      (bearerKey && timingSafeEqualStr(bearerKey, accessKey)) ||
      (headerKey && timingSafeEqualStr(headerKey, accessKey));

    if (!isAuthed) {
      const wantsHtml =
        request.method === 'GET' &&
        (pathname === '/' || pathname === '/gallery' || pathname === '/remote');
      if (wantsHtml) {
        return new Response(renderUnlockHtml(pathname + url.search, false), {
          status: 401,
          headers: withPrivacyHeaders({ 'Content-Type': 'text/html; charset=utf-8' }),
        });
      }
      return jsonResponse({ error: 'Unauthorized' }, 401);
    }

    // 6. Authenticated GET routes
    if (request.method === 'GET') {
      if (pathname === '/api/ping') {
        recordHeartbeat(request, url);
        return jsonResponse({ ok: true });
      }

      if (pathname === '/api/state') {
        if (url.searchParams.get('role') === 'display') {
          recordHeartbeat(request, url);
        }
        const [manifest, hiddenSet, frameState] = await loadBundleState(env.ALBUM_BUCKET);
        return jsonResponse(buildStatePayload(manifest, hiddenSet, frameState));
      }

      if (pathname === '/api/photos') {
        const [manifest, hiddenSet, frameState] = await loadBundleState(env.ALBUM_BUCKET);
        const st = buildStatePayload(manifest, hiddenSet, frameState);
        return jsonResponse({ demo: st.demo, photos: st.slides });
      }

      if (pathname === '/api/album') {
        const [manifest, hiddenSet, frameState] = await loadBundleState(env.ALBUM_BUCKET);
        const state = buildStatePayload(manifest, hiddenSet, frameState);
        if (!manifest) {
          return jsonResponse({
            curated: false,
            stats: { curated_photos: 0, diptych_slides: 0 },
            hidden_ids: [...hiddenSet].sort(),
            channels: state.channels,
            slides: [],
            photos: [],
            state,
          });
        }
        const allSlides = (manifest.slides || []).map((s) => ({
          ...formatFrameSlide(s),
          hidden: isSlideHidden(s, hiddenSet),
          in_default_frame: s.in_default_frame !== false,
          member_ids: s.member_ids || (s.photo_id ? [s.photo_id] : []),
          visual_theme: s.visual_theme || '',
          color_palette: s.color_palette || '',
          tags: s.tags || [],
        }));
        return jsonResponse({
          curated: true,
          stats: manifest.stats || {},
          hidden_ids: [...hiddenSet].sort(),
          channels: state.channels,
          slides: allSlides,
          photos: manifest.photos || [],
          state,
        });
      }

      if (pathname === '/api/search') {
        const q = (url.searchParams.get('q') || '').trim().slice(0, 200);
        const rawLimit = parseInt(url.searchParams.get('limit') || '36', 10);
        const limit = Number.isFinite(rawLimit) ? Math.min(Math.max(rawLimit, 1), 120) : 36;
        const [manifest, hiddenSet, frameState] = await loadBundleState(env.ALBUM_BUCKET);
        const res = await runSearch(q, limit, manifest, hiddenSet, frameState, env);
        return jsonResponse(res);
      }

      if (pathname === '/api/status') {
        const [manifest, hiddenSet, frameState] = await loadBundleState(env.ALBUM_BUCKET);
        const st = buildStatePayload(manifest, hiddenSet, frameState);
        return jsonResponse({
          clients: [...recentClients.values()],
          active_channel: st.active_channel,
          active_query: st.active_query,
          interval_sec: st.interval_sec,
          paused: st.paused,
          slide_count: st.slides.length,
        });
      }

      if (pathname.startsWith('/photos/') || pathname.startsWith('/diptychs/')) {
        const prefix = pathname.startsWith('/photos/') ? 'photos' : 'diptychs';
        const rawName = decodeURIComponent(pathname.slice(prefix.length + 2));
        const checked = validateSafeMediaFilename(rawName);
        if (!checked || !env.ALBUM_BUCKET) {
          return jsonResponse({ error: 'Not found' }, 404);
        }
        const key = `${prefix}/${checked.name}`;
        const obj = await env.ALBUM_BUCKET.get(key);
        if (!obj) {
          return jsonResponse({ error: 'Not found' }, 404);
        }
        const etag = obj.httpEtag || `"${obj.etag || key}"`;
        if (request.headers.get('If-None-Match') === etag) {
          return new Response(null, {
            status: 304,
            headers: withPrivacyHeaders({ 'ETag': etag }, 'private, max-age=86400'),
          });
        }
        return new Response(obj.body, {
          status: 200,
          headers: withPrivacyHeaders(
            {
              'Content-Type': checked.mime,
              'ETag': etag,
            },
            'private, max-age=86400'
          ),
        });
      }

      // Static web UI assets via Workers Static Assets binding (`env.ASSETS`)
      if (env.ASSETS) {
        const mappedPath = STATIC_ROUTE_MAP[pathname] || pathname;
        const assetUrl = new URL(mappedPath, url.origin);
        const assetResp = await env.ASSETS.fetch(new Request(assetUrl.toString(), request));
        if (assetResp && assetResp.status !== 404) {
          const cacheHdr = mappedPath.startsWith('/fonts/')
            ? 'private, max-age=86400'
            : 'private, no-cache';
          const outHeaders = withPrivacyHeaders(assetResp.headers, cacheHdr);
          outHeaders.set('Cache-Control', cacheHdr);
          if (pathname === '/sw.js') {
            outHeaders.set('Service-Worker-Allowed', '/');
          }
          return new Response(assetResp.body, {
            status: assetResp.status,
            headers: outHeaders,
          });
        }
      }

      return jsonResponse({ error: 'Not found' }, 404);
    }

    // 7. Authenticated POST control routes (`/api/control`, `/api/hide`)
    if (request.method === 'POST') {
      let body = {};
      try {
        body = await request.json();
        if (!body || typeof body !== 'object' || Array.isArray(body)) {
          return jsonResponse({ error: 'Invalid JSON body' }, 400);
        }
      } catch {
        return jsonResponse({ error: 'Invalid JSON body' }, 400);
      }

      const [manifest, hiddenSet, prevFrameState] = await loadBundleState(env.ALBUM_BUCKET);
      const nextState = { ...prevFrameState };

      if (pathname === '/api/control') {
        let changed = false;

        if (typeof body.search_query === 'string' && body.search_query.trim()) {
          const sq = body.search_query.trim().slice(0, 120);
          const res = await runSearch(sq, 40, manifest, hiddenSet, nextState, env);
          nextState.active_query = sq;
          nextState.search_slide_ids = res.results.map((r) => r.id);
          nextState.active_channel = 'search';
          changed = true;
        }

        if (typeof body.channel === 'string' && body.channel.trim()) {
          nextState.active_channel = body.channel.trim().slice(0, 48);
          if (nextState.active_channel !== 'search') {
            nextState.active_query = '';
          }
          changed = true;
        }

        if (Array.isArray(body.custom_slide_ids) && body.custom_slide_ids.length) {
          nextState.search_slide_ids = body.custom_slide_ids.slice(0, 80).map(String);
          nextState.active_query = String(body.label || 'Pinned selection').slice(0, 80);
          nextState.active_channel = 'search';
          changed = true;
        }

        if (body.interval_sec !== undefined) {
          const iv = Number(body.interval_sec);
          if (ALLOWED_INTERVALS.has(iv)) {
            nextState.interval_sec = iv;
            changed = true;
          }
        }

        if (typeof body.paused === 'boolean') {
          nextState.paused = body.paused;
          changed = true;
        }
        if (typeof body.show_captions === 'boolean') {
          nextState.show_captions = body.show_captions;
          changed = true;
        }
        if (typeof body.show_clock === 'boolean') {
          nextState.show_clock = body.show_clock;
          changed = true;
        }

        if (body.action === 'next' || body.action === 'prev' || body.action === 'goto') {
          nextState.command_seq = (nextState.command_seq || 0) + 1;
          nextState.last_command = {
            seq: nextState.command_seq,
            action: body.action,
            slide_id: body.slide_id ? String(body.slide_id).slice(0, 64) : null,
          };
          changed = true;
        }

        if (changed) {
          nextState.revision = (nextState.revision || 1) + 1;
          await saveFrameState(env.ALBUM_BUCKET, nextState);
        }
        return jsonResponse({ ok: true, state: buildStatePayload(manifest, hiddenSet, nextState) });
      }

      if (pathname === '/api/hide') {
        const targetId = String(body.id || '').trim().slice(0, 80);
        if (!targetId) {
          return jsonResponse({ error: 'Missing slide id' }, 400);
        }
        const hideFlag = body.hidden !== false;
        const updatedHidden = new Set(hiddenSet);
        if (hideFlag) {
          updatedHidden.add(targetId);
        } else {
          updatedHidden.delete(targetId);
        }
        nextState.revision = (nextState.revision || 1) + 1;
        await Promise.all([
          saveHiddenSet(env.ALBUM_BUCKET, updatedHidden),
          saveFrameState(env.ALBUM_BUCKET, nextState),
        ]);
        return jsonResponse({
          ok: true,
          id: targetId,
          hidden: hideFlag,
          hidden_ids: [...updatedHidden].sort(),
          state: buildStatePayload(manifest, updatedHidden, nextState),
        });
      }

      return jsonResponse({ error: 'Not found' }, 404);
    }

    return jsonResponse({ error: 'Method not allowed' }, 405);
  },
};
