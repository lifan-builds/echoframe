// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  let state = null;
  let activeDisplaySlideId = null;
  let toastTimer = null;

  function showToast(msg) {
    const node = el('toast');
    node.textContent = msg;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 2800);
  }

  async function apiGet(path) {
    const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function renderConnectionError(detail) {
    const card = el('now-playing');
    if (card) card.classList.add('is-offline');
    el('now-title').textContent = 'Frame status unavailable';
    el('now-meta').textContent = detail || 'Connection lost — retrying…';

    const list = el('heartbeat-list');
    list.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'row-item';
    row.textContent = 'Unable to reach frame server — retrying…';
    list.appendChild(row);
  }

  async function sendControl(payload, toastMessage) {
    try {
      const resp = await fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (data.state) {
        state = data.state;
        renderState();
      }
      if (toastMessage) showToast(toastMessage);
    } catch (err) {
      renderConnectionError('Check Wi-Fi — retrying…');
      showToast('Connection lost — check Wi-Fi.');
    }
  }

  function formatAgo(iso) {
    if (!iso) return 'just now';
    const sec = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
    if (sec < 5) return 'live';
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    return `${min}m ago`;
  }

  function renderState() {
    if (!state) return;
    const card = el('now-playing');
    if (card) card.classList.remove('is-offline');
    const slides = state.slides || [];
    const matched = slides.find((s) => s.id === activeDisplaySlideId) || slides[0];
    if (matched) {
      el('now-thumb').src = matched.src;
      el('now-title').textContent = matched.display_caption || matched.label || 'Active Slide';
    } else {
      el('now-title').textContent = 'Demo Mode';
    }

    const chObj = (state.channels || []).find((c) => c.id === state.active_channel);
    const chTitle = state.active_channel === 'search' && state.active_query
      ? `“${state.active_query}”`
      : (chObj ? chObj.name : state.active_channel);
    el('now-meta').textContent = `${chTitle} · ${slides.length} slides · every ${state.interval_sec}s`;

    el('btn-pause').textContent = state.paused ? 'Resume' : 'Pause';
    el('btn-captions').textContent = state.show_captions ? 'Shown' : 'Hidden';
    el('btn-clock').textContent = state.show_clock ? 'Shown' : 'Hidden';

    for (const b of document.querySelectorAll('.seg-btn')) {
      const sec = Number(b.getAttribute('data-sec'));
      b.classList.toggle('is-active', sec === state.interval_sec);
    }

    const chWrap = el('remote-channels');
    chWrap.innerHTML = '';
    for (const ch of state.channels || []) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'channel-row' + (ch.id === state.active_channel ? ' is-active' : '');
      const left = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = ch.name;
      const desc = document.createElement('span');
      desc.textContent = ch.description || '';
      left.append(strong, desc);
      const count = document.createElement('span');
      count.className = 'badge-count';
      count.textContent = String(ch.count);
      btn.append(left, count);
      btn.onclick = () => sendControl({ channel: ch.id }, `Switched to ${ch.name}`);
      chWrap.appendChild(btn);
    }
  }

  async function refreshStatus() {
    try {
      const [nextState, statusData] = await Promise.all([
        apiGet('/api/state'),
        apiGet('/api/status'),
      ]);
      state = nextState;

      const clients = statusData.clients || [];
      const displayClient = clients.find((c) => c.role === 'display' && c.current_slide) || clients[0];
      if (displayClient && displayClient.current_slide) {
        activeDisplaySlideId = displayClient.current_slide;
      }
      renderState();

      const list = el('heartbeat-list');
      list.innerHTML = '';
      if (!clients.length) {
        const empty = document.createElement('div');
        empty.className = 'row-item';
        empty.textContent = 'No displays connected yet — open /?autostart=1 on Echo Show.';
        list.appendChild(empty);
        return;
      }

      for (const c of clients) {
        const row = document.createElement('div');
        row.className = 'row-item';
        const left = document.createElement('div');
        const nameLine = document.createElement('div');
        const dot = document.createElement('span');
        dot.className = 'heartbeat-dot';
        const title = document.createElement('strong');
        title.textContent = `${c.device_name || 'Display'} (${c.ip})`;
        nameLine.append(dot, title);

        const sub = document.createElement('div');
        sub.style.fontSize = '12.5px';
        sub.style.color = 'var(--ink-secondary)';
        sub.style.maxWidth = '250px';
        sub.style.overflow = 'hidden';
        sub.style.textOverflow = 'ellipsis';
        sub.style.whiteSpace = 'nowrap';
        sub.textContent = c.current_caption ? `Showing: ${c.current_caption}` : 'Connected on local Wi-Fi';
        left.append(nameLine, sub);

        const right = document.createElement('span');
        right.className = 'tabular';
        right.style.fontSize = '12.5px';
        right.style.color = 'var(--ink-secondary)';
        right.textContent = formatAgo(c.last_seen);
        row.append(left, right);
        list.appendChild(row);
      }
    } catch (err) {
      renderConnectionError('Check Wi-Fi or server — retrying…');
    }
  }

  el('btn-prev').onclick = () => sendControl({ action: 'prev' }, 'Stepped back');
  el('btn-next').onclick = () => sendControl({ action: 'next' }, 'Skipped to next slide');
  el('btn-pause').onclick = () => {
    if (!state) return;
    sendControl({ paused: !state.paused }, !state.paused ? 'Paused slideshow' : 'Resumed slideshow');
  };

  el('remote-search-form').onsubmit = (e) => {
    e.preventDefault();
    const q = el('remote-query').value.trim();
    if (!q) return;
    sendControl({ search_query: q }, `Cast “${q}” to displays`);
  };

  for (const b of document.querySelectorAll('.seg-btn')) {
    b.onclick = () => {
      const sec = Number(b.getAttribute('data-sec'));
      sendControl({ interval_sec: sec }, `Slide interval set to ${b.textContent}`);
    };
  }

  el('btn-captions').onclick = () => {
    if (!state) return;
    sendControl({ show_captions: !state.show_captions });
  };

  el('btn-clock').onclick = () => {
    if (!state) return;
    sendControl({ show_clock: !state.show_clock });
  };

  el('btn-hide-current').onclick = async () => {
    if (!state || !state.slides || !state.slides.length) return;
    const target = state.slides.find((s) => s.id === activeDisplaySlideId) || state.slides[0];
    if (!target) return;
    try {
      await fetch('/api/hide', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: target.id, hidden: true }),
      });
      activeDisplaySlideId = null;
      await refreshStatus();
      showToast('Hidden slide from ambient frame.');
    } catch (err) {
      showToast('Failed to hide slide.');
    }
  };

  refreshStatus();
  setInterval(refreshStatus, 5000);
}());
