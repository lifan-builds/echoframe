(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  let album = null;
  let selectedChannel = 'smart_frame';
  let searchResults = null;
  let activeSearchQuery = '';
  let inspectedSlide = null;
  let toastTimer = null;

  function showToast(msg) {
    const node = el('toast');
    node.textContent = msg;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
  }

  async function apiGet(path) {
    const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function apiPost(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function slidesForView() {
    if (!album) return [];
    if (searchResults !== null) return searchResults;
    const all = album.slides || [];
    if (selectedChannel === 'hidden') {
      return all.filter((s) => s.hidden);
    }
    const visible = all.filter((s) => !s.hidden);
    if (selectedChannel === 'smart_frame') {
      return visible.filter((s) => s.in_default_frame !== false);
    }
    if (selectedChannel === 'on_this_day') {
      const now = new Date();
      const seasonal = visible.filter((s) => (s.date_label || '').includes(now.toLocaleString('en-US', { month: 'short' })));
      return seasonal.length ? seasonal : visible.slice(0, 24);
    }
    return visible.filter((s) => (s.channels || []).includes(selectedChannel));
  }

  function renderHeaderSummary() {
    if (!album || !album.stats) return;
    const st = album.stats;
    const state = album.state || {};
    const years = st.years ? Object.keys(st.years) : [];
    const spanTxt = years.length >= 2 ? `${years[0]}–${years[years.length - 1]}` : '';
    el('album-summary').textContent =
      `${st.curated_photos || 0} curated iPhone photos & ${st.diptych_slides || 0} diptychs` +
      (spanTxt ? ` (${spanTxt})` : '') +
      ` · Zero\u00a0cloud\u00a0uploads.`;

    const chObj = (album.channels || []).find((c) => c.id === state.active_channel);
    const chLabel = state.active_channel === 'search' && state.active_query
      ? `Semantic Search: “${state.active_query}”`
      : (chObj ? chObj.name : state.active_channel || 'Ambient Mix');
    el('live-status').innerHTML =
      `Now cast on displays: <strong></strong> · ${state.interval_sec || 10}s interval` +
      (state.paused ? ' (Paused)' : '');
    el('live-status').querySelector('strong').textContent = chLabel;
  }

  function renderChannelTabs() {
    const strip = el('channel-strip');
    strip.innerHTML = '';
    if (!album) return;

    const channels = [...(album.channels || [])];
    const hiddenCount = (album.slides || []).filter((s) => s.hidden).length;
    if (hiddenCount > 0) {
      channels.push({ id: 'hidden', name: 'Hidden', count: hiddenCount });
    }

    for (const ch of channels) {
      if (ch.id === 'search') continue;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'channel-tab' + (searchResults === null && selectedChannel === ch.id ? ' is-active' : '');
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', String(searchResults === null && selectedChannel === ch.id));
      const labelSpan = document.createElement('span');
      labelSpan.textContent = ch.name;
      const countSpan = document.createElement('span');
      countSpan.className = 'count';
      countSpan.textContent = String(ch.count);
      btn.append(labelSpan, countSpan);
      btn.onclick = () => {
        searchResults = null;
        activeSearchQuery = '';
        el('search-input').value = '';
        el('clear-search').hidden = true;
        selectedChannel = ch.id;
        renderAll();
      };
      strip.appendChild(btn);
    }
  }

  function renderWall() {
    const wall = el('photo-wall');
    wall.innerHTML = '';
    const items = slidesForView();

    if (searchResults !== null) {
      el('view-count').textContent = `${items.length} semantic matches for “${activeSearchQuery}”`;
      el('cast-label').textContent = `Cast “${activeSearchQuery}” to Display`;
    } else {
      const chObj = (album && album.channels || []).find((c) => c.id === selectedChannel);
      const cname = selectedChannel === 'hidden' ? 'Hidden' : (chObj ? chObj.name : 'Gallery');
      el('view-count').textContent = `${items.length} slides in ${cname}`;
      el('cast-label').textContent = selectedChannel === 'hidden' ? 'Cast Ambient Mix' : `Cast ${cname} to Display`;
    }

    el('empty-state').hidden = items.length > 0;

    for (const slide of items) {
      const fig = document.createElement('figure');
      fig.className = 'photo-tile' + (slide.kind === 'diptych' ? ' is-diptych' : '');
      fig.tabIndex = 0;

      const frame = document.createElement('div');
      frame.className = 'tile-frame';
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.src = slide.src;
      img.alt = slide.display_caption || slide.label || '';
      frame.appendChild(img);

      const capRow = document.createElement('figcaption');
      capRow.className = 'tile-caption-row';
      const title = document.createElement('p');
      title.className = 'tile-title';
      title.textContent = slide.display_caption || slide.label || '';
      const meta = document.createElement('span');
      meta.className = 'tile-meta';
      meta.textContent = [slide.age_badge, slide.date_label].filter(Boolean).join(' · ');
      capRow.append(title, meta);

      fig.append(frame, capRow);
      fig.onclick = () => openLightbox(slide);
      fig.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openLightbox(slide);
        }
      };
      wall.appendChild(fig);
    }
  }

  function openLightbox(slide) {
    inspectedSlide = slide;
    el('lb-img').src = slide.src;
    el('lb-img').alt = slide.display_caption || '';
    el('lb-title').textContent = slide.display_caption || slide.label || '';
    const details = [
      slide.age_badge,
      slide.date_label,
      slide.kind === 'diptych' ? '16:10 Portrait Diptych' : `${slide.orientation || 'Photo'}`,
      `Score ${slide.frame_score || ''}`,
      slide.resolution_tier === 'full_2048' ? '2048px USB Original' : '640px Normalized',
    ].filter(Boolean);
    el('lb-meta').textContent = details.join('  ·  ');
    el('lb-hide').textContent = slide.hidden ? 'Restore to Frame' : 'Hide from Frame';
    el('lightbox').hidden = false;
    el('lb-close').focus();
  }

  function closeLightbox() {
    el('lightbox').hidden = true;
    inspectedSlide = null;
  }

  async function runSemanticSearch(query) {
    const q = query.trim();
    if (!q) {
      searchResults = null;
      activeSearchQuery = '';
      el('clear-search').hidden = true;
      renderAll();
      return;
    }
    el('view-count').textContent = `Searching OpenAI embeddings for “${q}”…`;
    try {
      const data = await apiGet(`/api/search?q=${encodeURIComponent(q)}&limit=42`);
      activeSearchQuery = q;
      searchResults = data.results || [];
      el('clear-search').hidden = false;
      renderAll();
    } catch (err) {
      showToast('Search unavailable — check server connection.');
    }
  }

  async function castCurrentView() {
    try {
      if (searchResults !== null && activeSearchQuery) {
        const resp = await apiPost('/api/control', { search_query: activeSearchQuery });
        album.state = resp.state;
        renderHeaderSummary();
        showToast(`Cast “${activeSearchQuery}” (${searchResults.length} slides) to Echo Show & displays.`);
      } else {
        const chToCast = selectedChannel === 'hidden' ? 'smart_frame' : selectedChannel;
        const resp = await apiPost('/api/control', { channel: chToCast });
        album.state = resp.state;
        renderHeaderSummary();
        showToast('Updated active channel across all displays.');
      }
    } catch (err) {
      showToast('Could not reach display server.');
    }
  }

  function renderAll() {
    renderHeaderSummary();
    renderChannelTabs();
    renderWall();
  }

  async function reloadAlbum() {
    try {
      album = await apiGet('/api/album');
      renderAll();
    } catch (err) {
      el('live-status').textContent = 'Cannot load album — verify serve_album.py is running.';
    }
  }

  el('search-form').onsubmit = (e) => {
    e.preventDefault();
    runSemanticSearch(el('search-input').value);
  };

  el('clear-search').onclick = () => {
    el('search-input').value = '';
    searchResults = null;
    activeSearchQuery = '';
    el('clear-search').hidden = true;
    renderAll();
  };

  for (const chip of document.querySelectorAll('.prompt-chip')) {
    chip.onclick = () => {
      const q = chip.getAttribute('data-query') || '';
      el('search-input').value = q;
      runSemanticSearch(q);
    };
  }

  el('cast-btn').onclick = castCurrentView;
  el('lb-close').onclick = closeLightbox;
  el('lightbox').onclick = (e) => {
    if (e.target === el('lightbox')) closeLightbox();
  };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !el('lightbox').hidden) closeLightbox();
  });

  el('lb-cast-one').onclick = async () => {
    if (!inspectedSlide) return;
    try {
      await apiPost('/api/control', {
        action: 'goto',
        slide_id: inspectedSlide.id,
        custom_slide_ids: [inspectedSlide.id, ...slidesForView().filter((s) => s.id !== inspectedSlide.id).slice(0, 29).map((s) => s.id)],
        label: inspectedSlide.display_caption || 'Pinned Slide',
      });
      closeLightbox();
      await reloadAlbum();
      showToast('Showing selected photograph on active displays.');
    } catch (err) {
      showToast('Failed to send slide to display.');
    }
  };

  el('lb-hide').onclick = async () => {
    if (!inspectedSlide) return;
    const newHidden = !inspectedSlide.hidden;
    try {
      await apiPost('/api/hide', { id: inspectedSlide.id, hidden: newHidden });
      closeLightbox();
      await reloadAlbum();
      if (searchResults !== null) {
        searchResults = searchResults.filter((s) => s.id !== inspectedSlide.id);
        renderWall();
      }
      showToast(newHidden ? 'Hidden from ambient frame.' : 'Restored to ambient frame.');
    } catch (err) {
      showToast('Could not update hidden list.');
    }
  };

  reloadAlbum();
}());
