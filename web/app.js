/* Cleanarr UI: a poster wall of the library, a queue, and the settings. */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  shows: [], movies: [], calendar: null, home: null, settings: null,
  picked: new Set(),     // queued job ids ticked in the Queue
  lastPicked: null,      // for shift-click ranges
  queueIds: [],          // the queue in display order, for range selection
};

// ---------------------------------------------------------------- helpers
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...options,
  });
  if (res.status === 401) {
    // The session expired, or this instance just had a login turned on. Put
    // the gate up rather than letting every poll fail silently behind it.
    showGate('login');
    throw new Error('signed out');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* not json */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(message, ms = 3200) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const stamp = (seconds) => {
  const s = Math.max(0, Math.floor(seconds || 0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};
const gb = (bytes) => (bytes ? `${(bytes / 1e9).toFixed(1)} GB` : '');
const size = (bytes) => {
  if (!bytes) return '';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  return `${Math.round(bytes / 1e6)} MB`;
};
/* How long the running job has been going, so a slow stage reads as slow
   rather than stuck. */
const elapsed = (startedAt) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - startedAt));
  return s < 60 ? `${s}s so far` : `${Math.floor(s / 60)}m ${s % 60}s so far`;
};

const when = (epoch) => (epoch
  ? new Date(epoch * 1000).toLocaleDateString(undefined,
      { day: 'numeric', month: 'short', year: 'numeric' })
  : '');

/* A question with named buttons. Returns which one was pressed, or null if
   the person backed out - used before redoing work that is already done. */
function ask(title, body, choices) {
  return new Promise((resolve) => {
    const modal = $('#ask');
    $('#ask-title').textContent = title;
    $('#ask-body').textContent = body;
    const actions = $('#ask-actions');
    actions.innerHTML = '';
    const close = (value) => { modal.hidden = true; resolve(value); };
    choices.forEach((choice) => {
      const button = document.createElement('button');
      button.className = choice.primary ? 'small' : 'small ghost';
      button.textContent = choice.label;
      button.onclick = () => close(choice.value);
      actions.appendChild(button);
    });
    const cancel = document.createElement('button');
    cancel.className = 'small ghost';
    cancel.textContent = 'Cancel';
    cancel.onclick = () => close(null);
    actions.appendChild(cancel);
    modal.hidden = false;
  });
}

/* Anything already cleaned gets said out loud before it is cleaned again. */
async function confirmRedo(items) {
  const done = items.filter((i) => ['done', 'skipped'].includes(i.job_status));
  if (!done.length) return items;

  if (items.length === 1) {
    const one = done[0];
    const detail = [when(one.cleaned_at) && `cleaned ${when(one.cleaned_at)}`,
                    one.muted ? `${one.muted} words muted` : 'nothing found to mute']
      .filter(Boolean).join(', ');
    const answer = await ask('This one is already cleaned',
      `${one.label} was done before (${detail}). Cleaning it again replaces its cleaned track.`,
      [{ label: 'Clean it again', value: 'all', primary: true }]);
    return answer === 'all' ? items : [];
  }

  const answer = await ask('Some of these are already cleaned',
    `${done.length} of ${items.length} already have a cleaned track. `
    + 'Cleaning them again replaces it.',
    [{ label: `Only the other ${items.length - done.length}`, value: 'rest', primary: true },
     { label: 'All of them again', value: 'all' }]);
  if (answer === 'all') return items;
  if (answer === 'rest') return items.filter((i) => !['done', 'skipped'].includes(i.job_status));
  return [];
}

// ------------------------------------------------------------------ tabs
$$('.tab').forEach((tab) => tab.addEventListener('click', () => {
  $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${tab.dataset.view}`));
  if (tab.dataset.view === 'home') loadHome();
  if (tab.dataset.view === 'shows' && !state.shows.length) loadShows();
  if (tab.dataset.view === 'upcoming') loadCalendar();
  if (tab.dataset.view === 'movies' && !state.movies.length) loadMovies();
  if (tab.dataset.view === 'queue') loadJobs();
  if (tab.dataset.view === 'cleaned') loadCleaned();
  if (tab.dataset.view === 'settings') plexNow();
  closeDrawer();
}));

// ------------------------------------------------------- the drawer itself
function closeDrawer() {
  $('#drawer').hidden = true;
  $('#scrim').hidden = true;
}
$('#drawer-close').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
// Clicking anywhere that is not the drawer closes it - including the search
// box, which is where a hand goes next after looking at a show.
document.addEventListener('mousedown', (e) => {
  if ($('#drawer').hidden) return;
  if (e.target.closest('#drawer') || e.target.closest('.poster')
      || e.target.closest('#ask') || e.target.closest('[data-job]')) return;
  closeDrawer();
});

function openDrawer() {
  $('#scrim').hidden = false;
  $('#drawer').hidden = false;
}

// ------------------------------------------------------------------- home
/* "Two days ago" reads faster than a date when the whole point of the page is
   what is new. Anything older than a fortnight gets the date instead. */
const ago = (iso) => {
  if (!iso) return '';
  const days = Math.floor((Date.now() - Date.parse(iso)) / 86400000);
  if (Number.isNaN(days)) return '';
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return new Date(iso).toLocaleDateString(undefined,
    { day: 'numeric', month: 'short', year: 'numeric' });
};

async function loadHome() {
  let data;
  // Asking both services takes a few seconds, and this is what opens first:
  // an empty page for that long reads as broken rather than busy.
  if (!state.home) {
    $('#home-episodes').innerHTML = '<p class="muted">Asking Sonarr…</p>';
    $('#home-movies').innerHTML = '<p class="muted">Asking Radarr…</p>';
  }
  try {
    data = await api('/api/home');
  } catch (err) {
    $('#home-summary').innerHTML =
      `<p class="muted">${escapeHtml(err.message)} — check Settings.</p>`;
    return;
  }
  state.home = data;

  const { stats } = data;
  const tile = (value, label) => `<div class="tile"><b>${value}</b><span>${label}</span></div>`;
  $('#home-summary').innerHTML =
    tile(stats.cleaned_files, `file${stats.cleaned_files === 1 ? '' : 's'} cleaned`)
    + tile(stats.words_muted, `word${stats.words_muted === 1 ? '' : 's'} muted`)
    + tile(size(stats.added_bytes) || '0 MB', 'of cleaned audio')
    + tile(data.queued + data.running, 'waiting in the queue')
    + tile(data.monitors, `show${data.monitors === 1 ? '' : 's'} cleaning themselves`)
    + (data.failed ? tile(data.failed, 'failed') : '')
    + (data.holding ? `<div class="tile wide"><b>⏸ waiting for Plex</b>
        <span>${escapeHtml(data.holding)}</span></div>` : '');

  if (data.problems.length) {
    $('#home-summary').insertAdjacentHTML('beforeend',
      `<div class="tile wide warn"><b>Not everything answered</b>
       <span>${data.problems.map(escapeHtml).join(' · ')}</span></div>`);
  }

  $('#home-episodes').innerHTML = data.episodes.map((e, index) => `
    <div class="card">
      <img class="thumb" loading="lazy" alt=""
           src="/api/poster?source=sonarr&id=${e.series_id}"
           onerror="this.classList.add('missing');this.removeAttribute('src')">
      <div class="grow">
        <div class="title">${escapeHtml(e.series)}
          <span class="muted">S${String(e.season).padStart(2, '0')}E${
            String(e.episode).padStart(2, '0')} · ${escapeHtml(e.title)}</span></div>
        <div class="sub">${ago(e.added)} · ${escapeHtml(e.quality || '')} ${gb(e.size)}${
          e.muted ? ` · ${e.muted} muted` : ''}${
          e.monitored ? ' · <span class="chip auto">auto</span>' : ''}</div>
      </div>
      ${statusBadge(e.job_status)}
      <button class="small ghost" data-open-series="${e.series_id}"
              data-title="${escapeHtml(e.series)}">Open show</button>
      <button class="small" data-new-episode="${index}">${
        ['done', 'skipped'].includes(e.job_status) ? 'Clean again' : 'Clean'}</button>
    </div>`).join('')
    || '<p class="muted">Sonarr has not imported anything lately.</p>';

  $('#home-movies').innerHTML = data.movies
    .map((m, index) => posterCard({ ...m, index, home: true }, 'movie')).join('')
    || '<p class="muted">Radarr has not imported anything lately.</p>';
}

// --------------------------------------------------------------- upcoming
/* "Tonight", "Tomorrow", then the weekday - a date alone makes you count. */
const dayHeading = (iso) => {
  const day = new Date(`${iso}T00:00:00`);
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const days = Math.round((day - midnight) / 86400000);
  const name = day.toLocaleDateString(undefined,
    { weekday: 'long', day: 'numeric', month: 'short' });
  if (days < 0) return `Yesterday · ${name}`;
  if (days === 0) return `Tonight · ${name}`;
  if (days === 1) return `Tomorrow · ${name}`;
  return name;
};

const airTime = (iso) => (iso
  ? new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  : '');

const shortDate = (iso) => (iso
  ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
  : '');

/* How long after airing it turned up - "same night", "2 days later". The
   interesting number when a calendar is mostly things you already have. */
const gap = (airedIso, gotIso) => {
  if (!airedIso || !gotIso) return '';
  const hours = (Date.parse(gotIso) - Date.parse(airedIso)) / 3600000;
  if (Number.isNaN(hours)) return '';
  if (hours < 0) return 'before it aired';
  if (hours < 18) return 'same night';
  const days = Math.round(hours / 24);
  return days <= 1 ? 'next day' : `${days} days later`;
};

async function loadCalendar() {
  const days = $('#calendar-days').value;
  if (!state.calendar) $('#calendar-list').innerHTML = '<p class="muted">Asking Sonarr…</p>';
  try {
    const { items } = await api(`/api/calendar?days=${days}`);
    state.calendar = items;
  } catch (err) {
    $('#calendar-list').innerHTML =
      `<p class="muted">Sonarr: ${escapeHtml(err.message)} — check Settings.</p>`;
    return;
  }
  renderCalendar();
}

function renderCalendar() {
  const filter = $('#calendar-filter').value;
  const items = (state.calendar || []).filter((e) => {
    if (filter === 'missing') return !e.has_file;
    if (filter === 'monitored') return e.monitored;
    if (filter === 'unmonitored') return !e.monitored;
    return true;
  });
  $('#calendar-count').textContent =
    `${items.length} episode${items.length === 1 ? '' : 's'}`;

  // Grouped by the day it airs, in the order Sonarr gave them (already sorted
  // by air time), so the page reads like a diary rather than a table.
  const days = [];
  const byDay = new Map();
  items.forEach((e) => {
    const key = (e.airs || '').slice(0, 10);
    if (!byDay.has(key)) { byDay.set(key, []); days.push(key); }
    byDay.get(key).push(e);
  });

  $('#calendar-list').innerHTML = days.map((key) => `
    <h3 class="day-head">${escapeHtml(dayHeading(key))}</h3>
    <div class="list">
      ${byDay.get(key).map((e) => `
        <div class="card">
          <img class="thumb" loading="lazy" alt=""
               src="/api/poster?source=sonarr&id=${e.series_id}"
               onerror="this.classList.add('missing');this.removeAttribute('src')">
          <div class="grow">
            <div class="title">${escapeHtml(e.series)}
              <span class="muted">S${String(e.season).padStart(2, '0')}E${
                String(e.episode).padStart(2, '0')}${
                e.title ? ` · ${escapeHtml(e.title)}` : ''}</span></div>
            <div class="sub">${[
              `aired ${shortDate(e.airs)} at ${airTime(e.airs)}`,
              escapeHtml(e.network || ''),
              e.runtime ? `${e.runtime} min` : '',
            ].filter(Boolean).join(' · ')}</div>
            ${e.has_file ? `<div class="sub got">${
              e.downloaded_at
                ? `downloaded ${shortDate(e.downloaded_at)} at ${airTime(e.downloaded_at)}`
                  + ` <span class="muted">(${gap(e.airs, e.downloaded_at)})</span>`
                : 'already downloaded'}</div>` : ''}
          </div>
          ${e.has_file ? '<span class="chip done">downloaded</span>'
            : (e.wanted ? '<span class="chip pending">wanted</span>'
                        : '<span class="chip empty">not wanted</span>')}
          <label class="switch-label" title="Every episode of this show is cleaned as it downloads">
            <input type="checkbox" data-watch="${e.series_id}"
                   data-show-title="${escapeHtml(e.series)}"
                   ${e.monitored ? 'checked' : ''}> Clean it
          </label>
          <button class="small ghost" data-open-series="${e.series_id}"
                  data-title="${escapeHtml(e.series)}">Open</button>
        </div>`).join('')}
    </div>`).join('')
    || '<p class="muted">Nothing airing in that window.</p>';
}

$('#calendar-days').addEventListener('change', () => {
  state.calendar = null;
  loadCalendar();
});
$('#calendar-filter').addEventListener('change', renderCalendar);

/* The tick box on a calendar row is the same switch as the one in a show's
   drawer, so it is handled the same way and every view is updated at once. */
document.addEventListener('change', async (event) => {
  const box = event.target.closest('[data-watch]');
  if (!box) return;
  const id = box.dataset.watch;
  const title = box.dataset.showTitle;
  try {
    if (box.checked) {
      await api('/api/monitors', { method: 'POST', body: JSON.stringify(
        { source: 'sonarr', source_id: String(id), title, mode: 'new_only' })});
      toast(`${title}: episodes will be cleaned as they download`);
    } else {
      await api(`/api/monitors/sonarr/${id}`, { method: 'DELETE' });
      toast(`${title}: no longer cleaned automatically`);
    }
  } catch (err) {
    box.checked = !box.checked;
    return toast(err.message);
  }
  (state.calendar || []).forEach((e) => {
    if (String(e.series_id) === String(id)) e.monitored = box.checked;
  });
  const show = state.shows.find((s) => String(s.id) === String(id));
  if (show) { show.monitored = box.checked; renderShows(); }
  renderCalendar();
});

// --------------------------------------------------------- the poster wall
function posterCard(item, kind) {
  const chips = [];
  if (kind === 'show') {
    if (item.monitored) chips.push('<span class="chip auto">auto</span>');
    if (item.pending) chips.push(`<span class="chip pending">${item.pending} queued</span>`);
    if (item.failed) chips.push(`<span class="chip failed">${item.failed} failed</span>`);
    if (item.cleaned) {
      chips.push(`<span class="chip done">${item.cleaned}/${item.episodes} clean</span>`);
    }
    // A show with nothing on disk is worth opening anyway - to set it cleaning
    // itself before the first episode ever arrives.
    if (!item.episodes) chips.push('<span class="chip empty">no files</span>');
  } else if (item.job_status) {
    const label = { done: 'cleaned', skipped: 'nothing found', running: 'cleaning',
                    queued: 'queued', failed: 'failed' }[item.job_status] || item.job_status;
    const cls = { done: 'done', skipped: 'done', running: 'pending',
                  queued: 'pending', failed: 'failed' }[item.job_status] || '';
    chips.push(`<span class="chip ${cls}">${label}</span>`);
  }
  // Home holds its own short list of films, so its cards index that rather
  // than the full Movies wall - the two are never the same array.
  const attr = kind === 'show' ? 'data-series'
    : (item.home ? 'data-home-movie' : 'data-movie');
  const value = kind === 'show' ? item.id : item.index;
  const line = kind === 'show'
    ? (item.episodes ? `${item.episodes} ep` : 'nothing downloaded yet')
    : (item.home ? ago(item.added) : escapeHtml(item.quality || ''));
  return `
    <div class="poster" ${attr}="${value}" data-title="${escapeHtml(item.title)}">
      <img class="art" loading="lazy" alt=""
           src="/api/poster?source=${kind === 'show' ? 'sonarr' : 'radarr'}&id=${item.id}"
           onerror="this.classList.add('missing');this.removeAttribute('src');
                    this.dataset.initial='${escapeHtml((item.title || '?')[0])}'">
      <div class="corner">${chips.join('')}</div>
      <div class="meta">
        <div class="name">${escapeHtml(item.title)}</div>
        <div class="year">${[item.year || '', line].filter(Boolean).join(' · ')}</div>
      </div>
    </div>`;
}

async function loadShows() {
  $('#show-grid').innerHTML = '<p class="muted">Asking Sonarr…</p>';
  try {
    const { items } = await api('/api/series');
    state.shows = items;
    renderShows();
  } catch (err) {
    $('#show-grid').innerHTML =
      `<p class="muted">Sonarr: ${escapeHtml(err.message)} — check Settings.</p>`;
  }
}

/* Newest first, with anything undated at the back. `latest` is when a file
   last arrived for this title, not when the title was added to the library. */
const byRecent = (a, b) => String(b.latest || '').localeCompare(String(a.latest || ''));
const byTitle = (a, b) => (a.title || '').toLowerCase()
  .localeCompare((b.title || '').toLowerCase());

function renderShows() {
  const needle = $('#show-search').value.trim().toLowerCase();
  const filter = $('#show-filter').value;
  const items = state.shows.filter((s) => {
    if (needle && !s.title.toLowerCase().includes(needle)) return false;
    if (filter === 'cleaned') return s.cleaned > 0;
    if (filter === 'monitored') return s.monitored;
    if (filter === 'untouched') return !s.cleaned && !s.pending;
    if (filter === 'nofiles') return !s.episodes;
    return true;
  }).sort($('#show-sort').value === 'recent' ? byRecent : byTitle);
  $('#show-count').textContent = `${items.length} show${items.length === 1 ? '' : 's'}`;
  $('#show-grid').innerHTML = items.map((s) => posterCard(s, 'show')).join('')
    || '<p class="muted">Nothing matches.</p>';
}
$('#show-search').addEventListener('input', renderShows);
$('#show-filter').addEventListener('change', renderShows);
$('#show-sort').addEventListener('change', renderShows);

async function loadMovies() {
  $('#movie-grid').innerHTML = '<p class="muted">Asking Radarr…</p>';
  try {
    const { items } = await api('/api/movies');
    state.movies = items.map((m, index) => ({ ...m, index }));
    renderMovies();
  } catch (err) {
    $('#movie-grid').innerHTML =
      `<p class="muted">Radarr: ${escapeHtml(err.message)} — check Settings.</p>`;
  }
}

function renderMovies() {
  const needle = $('#movie-search').value.trim().toLowerCase();
  const filter = $('#movie-filter').value;
  const items = state.movies.filter((m) => {
    if (needle && !m.title.toLowerCase().includes(needle)) return false;
    if (filter === 'cleaned') return ['done', 'skipped'].includes(m.job_status);
    if (filter === 'untouched') return !m.job_status;
    return true;
  }).sort($('#movie-sort').value === 'recent' ? byRecent : byTitle);
  $('#movie-count').textContent = `${items.length} movie${items.length === 1 ? '' : 's'}`;
  $('#movie-grid').innerHTML = items.map((m) => posterCard(m, 'movie')).join('')
    || '<p class="muted">Nothing matches.</p>';
}
$('#movie-search').addEventListener('input', renderMovies);
$('#movie-filter').addEventListener('change', renderMovies);
$('#movie-sort').addEventListener('change', renderMovies);

// --------------------------------------------------------- episode drawer
async function openSeries(seriesId, title) {
  const drawer = $('#drawer');
  // Opened from Home the poster wall may never have loaded, so fall back to
  // asking which shows are watched rather than showing the tick box unticked.
  let show = state.shows.find((s) => String(s.id) === String(seriesId));
  if (!show) {
    const { items } = await api('/api/monitors').catch(() => ({ items: [] }));
    show = { monitored: items.some((m) => m.source === 'sonarr'
      && String(m.source_id) === String(seriesId)) };
  }
  $('#drawer-title').textContent = title;
  $('#drawer-body').innerHTML = '<p class="muted">Loading episodes…</p>';
  $('#monitor-toggle').hidden = false;
  $('#clean-show').hidden = false;
  $('#monitor-check').checked = !!show.monitored;
  drawer._series = { id: seriesId, title };
  // Which seasons were open belongs to the show being looked at, not to the
  // drawer - without this, opening a 37-season show inherits whatever was
  // expanded on the last one.
  drawer._openSeasons = null;
  openDrawer();

  // Wrapped, because the click handler that calls this returns the promise
  // rather than awaiting it - so a rejection here reaches nobody and the
  // drawer sits on "Loading episodes…" for ever. A failure has to say so.
  try {
    const { items } = await api(`/api/series/${seriesId}/episodes`);
    drawer._episodes = items;
    drawer._title = title;
    renderSeasons();
  } catch (err) {
    drawer._episodes = null;
    $('#clean-show').hidden = true;
    $('#remove-show').hidden = true;
    $('#drawer-body').innerHTML = `
      <p class="muted">Could not load the episodes: ${escapeHtml(err.message)}</p>
      <p class="muted">The auto-clean switch above still works. Try again in a
        moment, or check Sonarr under Settings.</p>
      <button class="small" data-open-series="${escapeHtml(String(seriesId))}"
              data-title="${escapeHtml(title)}">Try again</button>`;
  }
}

function renderSeasons() {
  const drawer = $('#drawer');
  const items = drawer._episodes || [];
  // A show Sonarr is waiting on has nothing to list. Say so, and point at the
  // one control that still does something useful here.
  if (!items.length) {
    $('#clean-show').hidden = true;
    $('#remove-show').hidden = true;
    $('#drawer-body').innerHTML = `
      <p class="muted">Nothing on disk for this show yet — Sonarr has not
        imported an episode. There is nothing to clean today.</p>
      <p class="muted">Tick <strong>Clean newly downloaded episodes</strong>
        above and every episode that arrives from now on is cleaned as it
        lands, without you coming back here.</p>`;
    return;
  }
  const open = drawer._openSeasons || new Set();
  const seasons = [...new Set(items.map((e) => e.season))].sort((a, b) => a - b);
  // One season opens itself; a dozen stay shut until asked for.
  if (!drawer._openSeasons && seasons.length === 1) open.add(seasons[0]);
  drawer._openSeasons = open;

  $('#drawer-body').innerHTML = seasons.map((season) => {
    const eps = items.filter((e) => e.season === season);
    const clean = eps.filter((e) => ['done', 'skipped'].includes(e.job_status)).length;
    const busy = eps.filter((e) => ['queued', 'running'].includes(e.job_status)).length;
    const isOpen = open.has(season);
    return `
      <div class="season ${isOpen ? 'open' : ''}" data-toggle="${season}">
        <span class="caret">▶</span>
        <span class="season-name">${season === 0 ? 'Specials' : `Season ${season}`}</span>
        <span class="season-count">${clean} of ${eps.length} cleaned${
          busy ? ` · ${busy} queued` : ''}</span>
        <button class="small ghost" data-season="${season}">Clean the season</button>
        ${clean ? `<button class="small ghost" data-remove-season="${season}"
          title="Take the cleaned tracks back out of this season">Remove cleaned</button>` : ''}
      </div>
      <div class="season-episodes ${isOpen ? 'open' : ''}" data-season-body="${season}">
        ${eps.map((e) => `
          <div class="card">
            <div class="grow">
              <div class="title">${String(e.episode).padStart(2, '0')}. ${escapeHtml(e.title)}</div>
              <div class="sub">${escapeHtml(e.quality || '')} ${gb(e.size)}${
                e.muted ? ` · ${e.muted} muted` : ''}${
                e.added_bytes ? ` · +${size(e.added_bytes)}` : ''}${
                e.cleaned_at ? ` · ${when(e.cleaned_at)}` : ''}</div>
            </div>
            ${statusBadge(e.job_status)}
            ${e.job_id ? `<button class="small ghost" data-job="${e.job_id}">Details</button>` : ''}
            ${['done', 'skipped'].includes(e.job_status)
              ? `<button class="small ghost" data-remove-episode="${e.id}"
                   title="Take the cleaned track back out">Remove</button>` : ''}
            <button class="small ghost" data-from="${e.id}"
                    title="Clean this episode and every one after it">From here</button>
            <button class="small" data-episode="${e.id}">Clean</button>
          </div>`).join('')}
      </div>`;
  }).join('');
}

$('#remove-show').addEventListener('click', async () => {
  const drawer = $('#drawer');
  try {
    await removeTracks(drawer._episodes || [], `Every cleaned episode of ${drawer._title}`);
  } catch (err) { toast(err.message); }
});

$('#clean-show').addEventListener('click', async () => {
  const drawer = $('#drawer');
  const outstanding = (drawer._episodes || [])
    .filter((e) => !['done', 'skipped', 'queued', 'running'].includes(e.job_status));
  if (!outstanding.length) return toast('every episode on disk is already cleaned or queued');
  const hours = outstanding.length * 2.5 / 60;
  const answer = await ask(`Clean ${outstanding.length} episode${outstanding.length === 1 ? '' : 's'}?`,
    `Everything on disk for this show that has not been cleaned yet — roughly `
    + `${hours < 1 ? `${Math.round(outstanding.length * 2.5)} minutes` : `${hours.toFixed(1)} hours`} of work.`,
    [{ label: 'Queue them', value: 'yes', primary: true }]);
  if (answer !== 'yes') return;
  try {
    await queueEpisodes(outstanding, drawer._title);
  } catch (err) { toast(err.message); }
});

const statusBadge = (status) => {
  if (!status) return '';
  const label = { done: 'cleaned', running: 'cleaning', queued: 'queued',
                  skipped: 'nothing found', failed: 'failed' }[status] || status;
  return `<span class="badge ${status}">${label}</span>`;
};

$('#monitor-check').addEventListener('change', async (event) => {
  const show = $('#drawer')._series;
  if (!show) return;
  try {
    if (event.target.checked) {
      // Exactly one meaning: episodes that arrive from now on. Cleaning what
      // is already on disk is a separate, deliberate button.
      await api('/api/monitors', { method: 'POST', body: JSON.stringify(
        { source: 'sonarr', source_id: String(show.id), title: show.title,
          mode: 'new_only' })});
      toast('episodes downloaded from now on will be cleaned automatically');
    } else {
      await api(`/api/monitors/sonarr/${show.id}`, { method: 'DELETE' });
      toast('stopped cleaning new episodes automatically');
    }
    const found = state.shows.find((s) => String(s.id) === String(show.id));
    if (found) { found.monitored = event.target.checked; renderShows(); }
    // The same show may be sitting on the calendar with its own tick box.
    if (state.calendar) {
      state.calendar.forEach((e) => {
        if (String(e.series_id) === String(show.id)) e.monitored = event.target.checked;
      });
      renderCalendar();
    }
  } catch (err) { toast(err.message); }
});

// -------------------------------------------------------------- queueing
async function queue(items, monitor) {
  if (!items.length) return;
  const body = { items };
  if (monitor) body.monitor = monitor;
  const result = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
  const parts = [`queued ${result.queued}`];
  if (result.already_queued) parts.push(`${result.already_queued} already waiting`);
  toast(parts.join(', '));
  loadJobs();
}

/* Take the cleaned track back out of files, freeing what it costs. */
async function removeTracks(episodes, label) {
  const cleaned = episodes.filter((e) => ['done', 'skipped'].includes(e.job_status));
  if (!cleaned.length) return toast('none of those have a cleaned track');
  const freed = cleaned.reduce((sum, e) => sum + (e.added_bytes || 0), 0);
  const answer = await ask(
    `Remove the cleaned track from ${cleaned.length} file${cleaned.length === 1 ? '' : 's'}?`,
    `${label}. The original audio and everything else in the file is untouched — `
    + `only the “Cleaned - English” track is taken out`
    + `${freed ? `, giving back about ${size(freed)}` : ''}.`,
    [{ label: 'Remove them', value: 'yes', primary: true }]);
  if (answer !== 'yes') return;
  const drawer = $('#drawer');
  await queue(cleaned.map((e) => ({
    kind: 'episode', title: drawer._title || label,
    subtitle: `S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')}`
              + ` · ${e.title} (removing)`,
    path: e.path, source: 'sonarr', source_id: String(e.id), action: 'remove',
  })));
}

/* Queue episodes, having first said which of them are already done. */
async function queueEpisodes(episodes, showTitle) {
  const drawer = $('#drawer');
  const labelled = episodes.map((e) => ({
    ...e,
    label: `S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')} · ${e.title}`,
  }));
  const wanted = await confirmRedo(labelled);
  if (!wanted.length) return;
  const monitor = drawer._series
    ? { source: 'sonarr', source_id: String(drawer._series.id), title: drawer._series.title }
    : null;
  await queue(wanted.map((e) => ({
    ...episodeItem(e, showTitle),
    force: ['done', 'skipped'].includes(e.job_status),
  })), monitor);
  $('#monitor-check').checked = true;
  wanted.forEach((w) => {
    const found = (drawer._episodes || []).find((e) => e.id === w.id);
    if (found) found.job_status = 'queued';
  });
  renderSeasons();
}

document.addEventListener('click', async (event) => {
  const el = event.target.closest('[data-series],[data-movie],[data-home-movie],[data-open-series],[data-new-episode],[data-episode],[data-from],[data-season],[data-toggle],[data-job],[data-cancel],[data-retry],[data-test],[data-again],[data-remove-episode],[data-remove-season],[data-remove-job]');
  if (!el) return;

  try {
    if (el.dataset.series) return openSeries(el.dataset.series, el.dataset.title);
    if (el.dataset.openSeries) {
      return openSeries(el.dataset.openSeries, el.dataset.title);
    }

    // Home: one of the episodes that just landed.
    if (el.dataset.newEpisode !== undefined && el.dataset.newEpisode !== '') {
      const e = (state.home?.episodes || [])[Number(el.dataset.newEpisode)];
      if (!e) return;
      const label = `${e.series} S${String(e.season).padStart(2, '0')}E${
        String(e.episode).padStart(2, '0')}`;
      const wanted = await confirmRedo([{ ...e, label }]);
      if (!wanted.length) return;
      await queue([{ kind: 'episode', title: e.series,
                     subtitle: `S${String(e.season).padStart(2, '0')}E${
                       String(e.episode).padStart(2, '0')} · ${e.title}`,
                     path: e.path, source: 'sonarr', source_id: String(e.episode_id),
                     force: ['done', 'skipped'].includes(e.job_status) }]);
      return loadHome();
    }

    const movieIndex = el.dataset.movie ?? el.dataset.homeMovie;
    if (movieIndex !== undefined && movieIndex !== '') {
      const m = (el.dataset.homeMovie !== undefined
        ? (state.home?.movies || []) : state.movies)[Number(movieIndex)];
      if (!m) return;
      const wanted = await confirmRedo([{ ...m, label: `${m.title} (${m.year || ''})` }]);
      if (!wanted.length) return;
      return queue([{ kind: 'movie', title: m.title, subtitle: String(m.year || ''),
                      path: m.path, source: 'radarr', source_id: String(m.id),
                      force: ['done', 'skipped'].includes(m.job_status) }]);
    }

    const drawer = $('#drawer');

    if (el.dataset.toggle !== undefined && !event.target.closest('[data-season]')) {
      const season = Number(el.dataset.toggle);
      const open = drawer._openSeasons;
      if (open.has(season)) open.delete(season); else open.add(season);
      return renderSeasons();
    }

    if (el.dataset.episode) {
      const e = (drawer._episodes || []).find((x) => String(x.id) === el.dataset.episode);
      return queueEpisodes([e], drawer._title);
    }
    // "I am up to here" - this episode and everything after it, across seasons.
    if (el.dataset.from) {
      const all = drawer._episodes || [];
      const start = all.find((x) => String(x.id) === el.dataset.from);
      if (!start) return;
      const after = all.filter((e) => e.season > start.season
        || (e.season === start.season && e.episode >= start.episode));
      return queueEpisodes(after, drawer._title);
    }
    if (el.dataset.season) {
      const season = Number(el.dataset.season);
      return queueEpisodes((drawer._episodes || []).filter((e) => e.season === season),
                           drawer._title);
    }
    if (el.dataset.removeEpisode) {
      const e = (drawer._episodes || []).find(
        (x) => String(x.id) === el.dataset.removeEpisode);
      return removeTracks([e], `${drawer._title} ${e.title}`);
    }
    if (el.dataset.removeSeason) {
      const season = Number(el.dataset.removeSeason);
      return removeTracks((drawer._episodes || []).filter((e) => e.season === season),
                          `${drawer._title}, season ${season}`);
    }
    if (el.dataset.removeJob) {
      const row = JSON.parse(decodeURIComponent(el.dataset.removeJob));
      const answer = await ask('Remove this cleaned track?',
        `${row.title} ${row.subtitle}. The original audio is untouched`
        + `${row.added_bytes ? `, and about ${size(row.added_bytes)} comes back` : ''}.`,
        [{ label: 'Remove it', value: 'yes', primary: true }]);
      if (answer !== 'yes') return;
      await queue([{ kind: row.kind, title: row.title,
                     subtitle: `${row.subtitle} (removing)`, path: row.path,
                     source: row.source, source_id: row.source_id, action: 'remove' }]);
      return loadCleaned();
    }

    if (el.dataset.job) return openJob(el.dataset.job);
    if (el.dataset.again) {
      const job = $('#drawer')._job;
      await queue([{ kind: job.kind, title: job.title, subtitle: job.subtitle,
                     path: job.path, source: job.source, source_id: job.source_id,
                     force: true }]);
      $('#drawer').hidden = true;
      return;
    }
    if (el.dataset.retry) {
      await api('/api/jobs/retry', { method: 'POST',
        body: JSON.stringify({ ids: [Number(el.dataset.retry)] }) });
      toast('back in the queue');
      return loadJobs();
    }
    if (el.dataset.move) {
      await api(`/api/jobs/${el.dataset.move}/move`, { method: 'POST',
        body: JSON.stringify({ where: el.dataset.where }) });
      return loadJobs();
    }
    if (el.dataset.cancel) {
      await api(`/api/jobs/${el.dataset.cancel}`, { method: 'DELETE' });
      toast('cancelled');
      return loadJobs();
    }
    if (el.dataset.test) {
      $('#test-result').textContent = 'testing…';
      const result = await api(`/api/settings/test/${el.dataset.test}`, { method: 'POST' });
      $('#test-result').textContent = result.ok
        ? `${result.app} ${result.version} — connected`
        : `failed: ${result.error}`;
    }
  } catch (err) {
    toast(err.message);
  }
});

const episodeItem = (e, showTitle) => ({
  kind: 'episode',
  title: showTitle,
  subtitle: `S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')} · ${e.title}`,
  path: e.path, source: 'sonarr', source_id: String(e.id),
});

// ------------------------------------------------------------------ jobs
async function loadJobs() {
  let data;
  try { data = await api('/api/jobs'); } catch (err) { return; }
  const { items, stats } = data;
  const open = stats.waiting ?? items.filter(
    (j) => j.status === 'queued' || j.status === 'running').length;
  $('#queue-count').textContent = open || '';
  $('#clear-queue').hidden = !open;
  $('#cleaned-count').textContent = stats.cleaned_files || '';
  $('#stats').textContent =
    `${stats.cleaned_files} file${stats.cleaned_files === 1 ? '' : 's'} cleaned · ` +
    `${stats.words_muted} word${stats.words_muted === 1 ? '' : 's'} muted`;

  $('#queue-hold').textContent = data.holding ? `⏸ ${data.holding}` : '';
  $('#clean-anyway').hidden = !data.holding;
  $('#purge-cancelled').hidden = !data.cancelled;
  $('#purge-cancelled').textContent = `Remove ${data.cancelled} cancelled`;
  $('#retry-failed').hidden = !data.failed;
  $('#retry-failed').textContent = `Retry ${data.failed} failed`;
  $('#purge-failed').hidden = !data.failed;
  $('#purge-failed').textContent = `Remove ${data.failed} failed`;

  // Say plainly that the watching happens by itself, and when it last ran.
  const ago = data.last_check
    ? Math.max(0, Math.round((Date.now() / 1000 - data.last_check) / 60)) : null;
  $('#monitor-status').textContent = data.monitors
    ? `${data.monitors} show${data.monitors === 1 ? '' : 's'} watched for new episodes`
      + `, checked every 10 min${ago === null ? ''
        : ` · last ${ago === 0 ? 'just now' : `${ago} min ago`}`}`
    : 'No shows set to clean new episodes automatically';

  // Keep ticks only for jobs still waiting - one that started or was cancelled
  // should not stay silently selected.
  state.queueIds = items.filter((j) => j.status === 'queued').map((j) => j.id);
  state.picked = new Set([...state.picked].filter((id) => state.queueIds.includes(id)));
  renderSelection();

  $('#job-list').innerHTML = items.map((j) => `
    <div class="card ${state.picked.has(j.id) ? 'picked' : ''}">
      ${j.status === 'queued'
        ? `<input type="checkbox" class="pick" data-pick="${j.id}"
             ${state.picked.has(j.id) ? 'checked' : ''}
             title="Select — shift-click to select a run of episodes">`
        : ''}
      <div class="grow">
        <div class="title">${j.status === 'queued'
          ? `<span class="show-pick" data-pick-show="${escapeHtml(j.title)}"
               title="Select every queued episode of this show">${escapeHtml(j.title)}</span>`
          : escapeHtml(j.title)} ${j.subtitle ? `<span class="muted">${escapeHtml(j.subtitle)}</span>` : ''}</div>
        <div class="sub">${escapeHtml(j.message || j.stage || '')}${
          j.status === 'running' && j.started_at
            ? ` <span class="muted">· ${elapsed(j.started_at)}</span>` : ''}</div>
        ${j.status === 'running' ? `<div class="bar"><div style="width:${Math.round(j.progress * 100)}%"></div></div>` : ''}
      </div>
      ${statusBadge(j.status)}
      ${j.status === 'queued' ? `
        <span class="order">
          <button class="small ghost" data-move="${j.id}" data-where="top" title="Do this one first">⤒</button>
          <button class="small ghost" data-move="${j.id}" data-where="up" title="Move up">▲</button>
          <button class="small ghost" data-move="${j.id}" data-where="down" title="Move down">▼</button>
          <button class="small ghost" data-move="${j.id}" data-where="bottom" title="Do this one last">⤓</button>
        </span>` : ''}
      ${j.muted ? `<button class="small ghost" data-job="${j.id}">${j.muted} muted</button>` : ''}
      ${j.status === 'failed'
        ? `<button class="small ghost" data-retry="${j.id}">Retry</button>` : ''}
      ${(j.status === 'queued' || j.status === 'running')
        ? `<button class="small ghost" data-cancel="${j.id}">Cancel</button>` : ''}
    </div>`).join('') || '<p class="muted">Nothing queued. Pick a show or a movie.</p>';

  $('#status').textContent = data.holding
    ? 'waiting for Plex' : (data.busy ? 'working…' : 'idle');
}

$('#clean-anyway').addEventListener('click', async () => {
  try {
    await api('/api/queue/clean-anyway', { method: 'POST' });
    toast('carrying on despite the transcode');
    loadJobs();
  } catch (err) { toast(err.message); }
});

$('#clear-queue').addEventListener('click', async () => {
  const answer = await ask('Empty the queue?',
    'Everything waiting is cancelled. A file being cleaned right now finishes.',
    [{ label: 'Empty it', value: 'yes', primary: true }]);
  if (answer !== 'yes') return;
  try {
    const { cancelled } = await api('/api/queue', { method: 'DELETE' });
    toast(`cancelled ${cancelled}`);
    loadJobs();
  } catch (err) { toast(err.message); }
});

// ------------------------------------------------------- selecting in bulk
function renderSelection() {
  const count = state.picked.size;
  $('#selection-bar').hidden = count === 0;
  $('#selection-count').textContent =
    `${count} selected${count ? ` of ${state.queueIds.length} waiting` : ''}`;
}

function togglePick(id, viaShift) {
  // Shift-click selects everything between the last tick and this one, which
  // is how you grab "the rest of season two" without thirty taps.
  if (viaShift && state.lastPicked !== null) {
    const from = state.queueIds.indexOf(state.lastPicked);
    const to = state.queueIds.indexOf(id);
    if (from !== -1 && to !== -1) {
      const [lo, hi] = from < to ? [from, to] : [to, from];
      state.queueIds.slice(lo, hi + 1).forEach((x) => state.picked.add(x));
      state.lastPicked = id;
      return;
    }
  }
  if (state.picked.has(id)) state.picked.delete(id);
  else state.picked.add(id);
  state.lastPicked = id;
}

document.addEventListener('click', async (event) => {
  const pick = event.target.closest('[data-pick]');
  if (pick) {
    togglePick(Number(pick.dataset.pick), event.shiftKey);
    return loadJobs();
  }
  const show = event.target.closest('[data-pick-show]');
  if (show) {
    // Every queued episode of this show, or none of them if they are all on.
    const title = show.dataset.pickShow;
    const rows = [...document.querySelectorAll('#job-list .card')]
      .filter((card) => card.querySelector('[data-pick-show]')?.dataset.pickShow === title)
      .map((card) => Number(card.querySelector('[data-pick]')?.dataset.pick))
      .filter(Boolean);
    const allOn = rows.every((id) => state.picked.has(id));
    rows.forEach((id) => (allOn ? state.picked.delete(id) : state.picked.add(id)));
    return loadJobs();
  }

  const bulk = event.target.closest('[data-bulk]');
  if (!bulk) return;
  const ids = [...state.picked];
  try {
    if (bulk.dataset.bulk === 'clear') {
      state.picked.clear();
      state.lastPicked = null;
    } else if (bulk.dataset.bulk === 'cancel') {
      const answer = await ask(`Cancel ${ids.length} job${ids.length === 1 ? '' : 's'}?`,
        'They come out of the queue. Nothing already cleaned is affected.',
        [{ label: 'Cancel them', value: 'yes', primary: true }]);
      if (answer !== 'yes') return;
      const { cancelled } = await api('/api/jobs/cancel',
        { method: 'POST', body: JSON.stringify({ ids }) });
      toast(`cancelled ${cancelled}`);
      state.picked.clear();
    } else {
      await api('/api/jobs/move',
        { method: 'POST', body: JSON.stringify({ ids, where: bulk.dataset.bulk }) });
    }
  } catch (err) {
    toast(err.message);
  }
  loadJobs();
});

$('#retry-failed').addEventListener('click', async () => {
  try {
    const { retrying } = await api('/api/jobs/retry', { method: 'POST', body: '{}' });
    toast(`${retrying} back in the queue`);
    loadJobs();
  } catch (err) { toast(err.message); }
});

$('#purge-failed').addEventListener('click', async () => {
  const answer = await ask('Remove the failed jobs?',
    'They disappear from the queue. The files themselves were never changed — '
    + 'a job only touches a file after it has been checked.',
    [{ label: 'Remove them', value: 'yes', primary: true }]);
  if (answer !== 'yes') return;
  try {
    const { removed } = await api('/api/jobs/failed', { method: 'DELETE' });
    toast(`removed ${removed}`);
    loadJobs();
  } catch (err) { toast(err.message); }
});

$('#purge-cancelled').addEventListener('click', async () => {
  try {
    const { removed } = await api('/api/jobs/cancelled', { method: 'DELETE' });
    toast(`removed ${removed} cancelled job${removed === 1 ? '' : 's'}`);
    loadJobs();
  } catch (err) { toast(err.message); }
});

$('#check-new').addEventListener('click', async () => {
  try {
    const { queued } = await api('/api/monitors/check', { method: 'POST' });
    toast(queued ? `queued ${queued} new episode(s)` : 'nothing new to clean');
    loadJobs();
  } catch (err) { toast(err.message); }
});

async function openJob(jobId) {
  const { job, detections } = await api(`/api/jobs/${jobId}`);
  const left = detections.filter((d) => !d.muted);
  $('#drawer-title').textContent = `${job.title} ${job.subtitle || ''}`.trim();
  $('#monitor-toggle').hidden = true;
  $('#clean-show').hidden = true;
  $('#drawer')._series = null;
  $('#drawer-body').innerHTML = `
    <p class="muted">${escapeHtml(job.message || '')}</p>
    <div class="row">
      <button class="small ghost" data-again="${job.id}">Clean again</button>
      <span class="muted">Re-runs this file and replaces its cleaned track —
        use after changing the word lists in Settings.</span>
    </div>
    ${left.length ? `<p class="muted">${left.length} found but left in, judged
      an ordinary word in context.</p>` : ''}
    <table class="detections">
      <thead><tr><th>At</th><th>Word</th><th></th><th>Note</th></tr></thead>
      <tbody>${detections.map((d) => `
        <tr>
          <td>${stamp(d.start)}</td>
          <td>${escapeHtml(d.text)}</td>
          <td>${d.muted ? '<span class="badge done">muted</span>'
                        : '<span class="badge">left in</span>'}</td>
          <td class="muted">${escapeHtml(d.reason || '')}</td>
        </tr>`).join('')}</tbody>
    </table>`;
  $('#drawer')._job = job;
  openDrawer();
}

// --------------------------------------------------------------- cleaned
async function loadCleaned() {
  const query = $('#cleaned-search').value.trim();
  let data;
  try {
    data = await api(`/api/history?q=${encodeURIComponent(query)}`);
  } catch (err) { return; }
  const { items, stats } = data;
  // Space is the thing you cannot see from the library, so it is said plainly
  // here: what all these extra tracks are costing, in total and per file.
  const shown = items.reduce((sum, j) => sum + (j.added_bytes || 0), 0);
  const unknown = items.filter((j) => !j.added_bytes).length;
  $('#cleaned-stats').innerHTML =
    `${stats.cleaned_files} file${stats.cleaned_files === 1 ? '' : 's'} · `
    + `${stats.words_muted} word${stats.words_muted === 1 ? '' : 's'} muted · `
    + `<strong>${size(stats.added_bytes) || '0 MB'}</strong> of cleaned audio`
    + (unknown ? ` <span class="muted">(${unknown} cleaned before sizes were
        recorded, so the total is an undercount)</span>` : '');

  $('#remove-all').hidden = !stats.cleaned_files;
  $('#cleaned-list').innerHTML = items.map((j) => `
    <div class="card">
      <div class="grow">
        <div class="title">${escapeHtml(j.title)} ${j.subtitle
          ? `<span class="muted">${escapeHtml(j.subtitle)}</span>` : ''}</div>
        <div class="sub">${when(j.finished_at)}${j.muted
          ? ` · ${j.muted} word${j.muted === 1 ? '' : 's'} muted`
          : ' · nothing found to mute'}${
          j.added_bytes ? ` · ${size(j.added_bytes)}` : ''}</div>
      </div>
      ${statusBadge(j.status)}
      <button class="small ghost" data-job="${j.id}">Details</button>
      <button class="small ghost" data-remove-job="${encodeURIComponent(JSON.stringify({
        kind: j.kind, title: j.title, subtitle: j.subtitle, path: j.path,
        source: j.source, source_id: j.source_id, added_bytes: j.added_bytes }))}"
        title="Take the cleaned track back out of this file">Remove</button>
    </div>`).join('')
    || `<p class="muted">${query ? 'Nothing matches.'
        : 'Nothing cleaned yet — pick a show or a movie.'}</p>`;
}
$('#remove-all').addEventListener('click', async () => {
  const { stats } = await api('/api/history?limit=1');
  // Two steps on purpose: this undoes every hour the service has ever spent,
  // and the only way back is cleaning them all again.
  const first = await ask(
    `Remove the cleaned track from all ${stats.cleaned_files} files?`,
    `Every “Cleaned - English” track goes, giving back ${size(stats.added_bytes)}. `
    + `The original audio in every file is untouched. Re-cleaning them later `
    + `would take hours.`,
    [{ label: 'Continue', value: 'go' }]);
  if (first !== 'go') return;
  const second = await ask('Are you sure?',
    `This queues ${stats.cleaned_files} removals and cannot be undone except by `
    + `cleaning everything again.`,
    [{ label: `Yes — remove all ${stats.cleaned_files}`, value: 'yes' }]);
  if (second !== 'yes') return;
  try {
    const { queued } = await api('/api/history/remove-all',
      { method: 'POST', body: JSON.stringify({ confirm: 'REMOVE ALL' }) });
    toast(`queued ${queued} removals`);
    loadJobs(); loadCleaned();
  } catch (err) { toast(err.message); }
});

$('#cleaned-search').addEventListener('input', () => {
  clearTimeout(loadCleaned._t);
  loadCleaned._t = setTimeout(loadCleaned, 250);
});


/* ===========================================================================
   Signing in
   ---------------------------------------------------------------------------
   Off entirely until somebody sets a username, which is right for a box on
   your own LAN. Once set, this is the first thing the page does.
   =========================================================================== */

let gateMode = 'login';

function showGate(mode) {
  gateMode = mode;
  const setup = mode === 'setup';
  $('#gate-blurb').textContent = setup
    ? 'Pick a username and password. You will need these every time you open '
      + 'Cleanarr from now on.'
    : 'Sign in to continue.';
  $('#gate-submit').textContent = setup ? 'Create account' : 'Sign in';
  $('#gate-confirm-row').hidden = !setup;
  $('#gate-pass').autocomplete = setup ? 'new-password' : 'current-password';
  $('#gate-error').hidden = true;
  $('#gate').hidden = false;
  setTimeout(() => $('#gate-user').focus(), 40);
}

function hideGate() {
  $('#gate').hidden = true;
  $('#gate-pass').value = '';
  $('#gate-confirm').value = '';
}

$('#gate-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = $('#gate-user').value.trim();
  const password = $('#gate-pass').value;
  const error = $('#gate-error');
  error.hidden = true;

  if (gateMode === 'setup' && password !== $('#gate-confirm').value) {
    error.textContent = 'Those two passwords are not the same.';
    error.hidden = false;
    return;
  }

  try {
    const res = await fetch(`/api/auth/${gateMode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || 'that did not work');
    }
    hideGate();
    boot();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

/* The page asks this before it draws anything. */
async function checkAuth() {
  let state;
  try {
    state = await (await fetch('/api/auth/state')).json();
  } catch (err) {
    return true;          // cannot ask: let the app try and fail honestly
  }
  state.username = state.username || '';
  window.__auth = state;
  if (!state.configured) return true;   // no login on this instance

  // Configured: find out whether this browser is already signed in.
  const probe = await fetch('/api/settings');
  if (probe.status === 401) { showGate('login'); return false; }
  return true;
}

/* ===========================================================================
   The speech model
   =========================================================================== */

async function loadModels() {
  let data;
  try { data = await api('/api/models'); } catch (err) { return; }
  state.models = data;

  $('#model-list').innerHTML = data.items.map((m) => `
    <div class="model ${m.name === data.selected ? 'chosen' : ''}">
      <div class="grow">
        <div class="title">${escapeHtml(m.name)}${
          m.name === data.selected ? ' <span class="chip auto">in use</span>' : ''}</div>
        <div class="sub">${m.ready
          ? `on disk · ${size(m.bytes)}`
          : `not downloaded · about ${escapeHtml(m.approx_size)}`}</div>
      </div>
      ${m.downloading
        ? '<span class="chip pending">downloading…</span>'
        : (m.ready
          ? '<span class="chip done">ready</span>'
          : `<button type="button" class="small" data-get-model="${escapeHtml(m.name)}">Download</button>`)}
    </div>`).join('')
    + `<p class="muted">Kept in <code>${escapeHtml(data.folder)}</code>, so it
        survives updating the container.</p>`;

  // Keep refreshing only while something is actually coming down.
  if (data.items.some((m) => m.downloading)) {
    clearTimeout(loadModels._t);
    loadModels._t = setTimeout(loadModels, 3000);
  }
}

/* ===========================================================================
   Ollama
   =========================================================================== */

async function checkJudge() {
  const out = $('#judge-result');
  out.textContent = 'asking…';
  $('#judge-pull').hidden = true;
  try {
    const data = await api('/api/judge/models');
    if (!data.reachable) {
      out.textContent = `could not reach Ollama: ${data.error || 'no answer'}`;
      return;
    }
    if (data.has_selected) {
      out.textContent = `Ollama has ${data.selected} — nothing to do.`;
    } else {
      out.textContent = `Ollama is up but does not have ${data.selected}.`;
      $('#judge-pull').hidden = false;
    }
  } catch (err) {
    out.textContent = err.message;
  }
}

async function pullJudge() {
  const out = $('#judge-result');
  try {
    const data = await api('/api/judge/pull', { method: 'POST', body: '{}' });
    out.textContent = `Ollama is downloading ${data.model}. This takes a few `
      + 'minutes and carries on if you leave this page.';
    $('#judge-pull').hidden = true;
    const poll = setInterval(async () => {
      const s2 = await api('/api/judge/pull').catch(() => ({}));
      if (!s2.downloading) { clearInterval(poll); checkJudge(); }
    }, 5000);
  } catch (err) {
    out.textContent = err.message;
  }
}

/* ===========================================================================
   Who can use this
   =========================================================================== */

/* Tell the person the truth about why Install is or is not on offer. */
function renderInstallState() {
  const note = document.querySelector('#install-note');
  const https = document.querySelector('#install-https');
  if (!note) return;
  const standalone = window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
  const secure = window.isSecureContext;
  https.hidden = secure;
  if (standalone) {
    note.textContent = 'Already running as an installed app.';
  } else if (!secure) {
    note.textContent = 'Not available on this address — see below.';
  } else if (installPrompt) {
    note.textContent = '';
  } else {
    note.textContent = 'If no button appears, use the browser menu — '
      + 'Install app, or Add to Home screen.';
  }
}

function renderSecurity() {
  const on = !!(window.__auth && window.__auth.configured);
  $('#security').innerHTML = on ? `
    <p class="help">Signed in as <strong>${escapeHtml(window.__auth.username)}</strong>.
      Everyone who opens Cleanarr needs this password.</p>
    <div class="grid">
      <label>Current password<input id="sec-current" type="password" autocomplete="current-password"></label>
      <label>New password<input id="sec-new" type="password" autocomplete="new-password"></label>
      <label>New username (optional)<input id="sec-user" placeholder="${escapeHtml(window.__auth.username)}"></label>
    </div>
    <div class="row">
      <button type="button" class="small" id="sec-save">Change password</button>
      <button type="button" class="small ghost" id="sec-off">Turn the login off</button>
      <button type="button" class="small ghost" id="sec-out">Sign out</button>
      <span id="sec-result" class="muted"></span>
    </div>
    <p class="help">Changing the password signs out every other browser.</p>
  ` : `
    <div class="callout">
      <strong>Anyone who can reach this address can use it.</strong>
      That is fine on a home network you trust. Set a password if this is
      reachable from anywhere else — or if you would rather it were not
      one click from the family iPad.
    </div>
    <div class="grid">
      <label>Username<input id="sec-user" autocapitalize="none"></label>
      <label>Password<input id="sec-new" type="password" autocomplete="new-password"></label>
    </div>
    <div class="row">
      <button type="button" class="small" id="sec-create">Turn the login on</button>
      <span id="sec-result" class="muted"></span>
    </div>`;
}

document.addEventListener('click', async (event) => {
  const get = event.target.closest('[data-get-model]');
  if (get) {
    get.disabled = true;
    get.textContent = 'starting…';
    try {
      await api(`/api/models/${encodeURIComponent(get.dataset.getModel)}/download`,
                { method: 'POST', body: '{}' });
      toast('downloading — it carries on if you leave this page');
    } catch (err) { toast(err.message); }
    return loadModels();
  }

  if (event.target.id === 'judge-check') return checkJudge();
  if (event.target.id === 'judge-pull') return pullJudge();

  const out = $('#sec-result');
  try {
    if (event.target.id === 'sec-create') {
      const username = $('#sec-user').value.trim();
      const password = $('#sec-new').value;
      await api('/api/auth/setup', { method: 'POST',
        body: JSON.stringify({ username, password }) });
      out.textContent = 'login is on';
      window.__auth = { configured: true, username };
      renderSecurity();
    } else if (event.target.id === 'sec-save') {
      const body = JSON.stringify({
        current: $('#sec-current').value,
        password: $('#sec-new').value,
        username: $('#sec-user').value.trim(),
      });
      const data = await api('/api/auth/change', { method: 'POST', body });
      window.__auth = { configured: true, username: data.username };
      out.textContent = 'changed — other browsers are signed out';
      renderSecurity();
    } else if (event.target.id === 'sec-off') {
      const answer = await ask('Turn the login off?',
        'Anyone who can reach this address will be able to use it, with no '
        + 'password. Only do this on a network you trust.',
        [{ label: 'Turn it off', value: 'yes', primary: true }]);
      if (answer !== 'yes') return;
      await api('/api/auth/change', { method: 'POST',
        body: JSON.stringify({ current: $('#sec-current').value, disable: true }) });
      window.__auth = { configured: false, username: '' };
      renderSecurity();
      toast('login turned off');
    } else if (event.target.id === 'sec-out') {
      await api('/api/auth/logout', { method: 'POST', body: '{}' });
      showGate('login');
    }
  } catch (err) {
    if (out) out.textContent = err.message;
  }
});


/* Which half of the Listening section applies. */

/* Which media server's fields to show. "Neither" hides the wait policy too -
   there is nothing to wait for. */
function renderMediaServer() {
  const chosen = $$('input[name="media_server"]').find((r) => r.checked);
  const which = chosen ? chosen.value : 'none';
  $('#server-plex').hidden = which !== 'plex';
  $('#server-jellyfin').hidden = which !== 'jellyfin';
  $('#hold-row').hidden = which === 'none';
  if (which === 'none') $('#plex-now').textContent = '';
}
$$('input[name="media_server"]').forEach((r) =>
  r.addEventListener('change', () => { renderMediaServer(); plexNow(); }));

function renderAsrBackend() {
  const chosen = $$('input[name="asr_backend"]').find((r) => r.checked);
  const remote = chosen && chosen.value === 'remote';
  $('#asr-remote').hidden = !remote;
  $('#asr-builtin').hidden = remote;
}
$$('input[name="asr_backend"]').forEach((r) =>
  r.addEventListener('change', renderAsrBackend));


/* Ask the remote server what it has, rather than making people guess its
   naming. Servers that have no /v1/models are not broken - the field stays
   free text and says so. */
async function listRemoteModels() {
  const out = $('#asr-result');
  const url = $('#settings-form').elements.asr_url.value.trim();
  if (!url) { out.textContent = 'put the server address in first'; return; }
  out.textContent = 'asking…';
  try {
    const data = await api(`/api/asr/remote-models?url=${encodeURIComponent(url)}`);
    const list = $('#asr-model-options');
    list.innerHTML = (data.models || [])
      .map((m) => `<option value="${escapeHtml(m)}">`).join('');
    out.textContent = data.ok && data.models.length
      ? `${data.models.length} model(s) — click the box to choose`
      : (data.ok ? 'it lists no models; type the name yourself'
                 : `could not list them (${data.error}) — type the name yourself`);
  } catch (err) {
    out.textContent = err.message;
  }
}
$('#asr-list').addEventListener('click', listRemoteModels);

$('#asr-test').addEventListener('click', async () => {
  const out = $('#asr-result');
  out.textContent = 'sending a second of silence…';
  try {
    const data = await api('/api/asr/test', { method: 'POST', body: JSON.stringify({
      url: $('#settings-form').elements.asr_url.value.trim(),
      model: $('#settings-form').elements.asr_remote_model.value.trim(),
    })});
    out.textContent = data.ok ? `works — ${data.note}` : `no: ${data.error}`;
  } catch (err) {
    out.textContent = err.message;
  }
});

// -------------------------------------------------------------- settings
async function plexNow() {
  try {
    const data = await api('/api/plex/sessions');
    $('#plex-now').innerHTML = data.sessions.length
      ? `Right now: ${data.sessions.map((s) => escapeHtml(s.description)).join('; ')}
         ${data.holding ? '— the queue is waiting' : '— not a reason to wait'}`
      : 'Nothing is playing right now.';
  } catch (err) { $('#plex-now').textContent = ''; }
}

async function loadSettings() {
  const settings = await api('/api/settings');
  state.settings = settings;
  const form = $('#settings-form');
  const set = (name, value) => { if (form.elements[name]) form.elements[name].value = value ?? ''; };

  $('#hold-policy').innerHTML = settings.hold_policies
    .map((p) => `<option value="${p.key}">${escapeHtml(p.label)}</option>`).join('');

  set('sonarr.url', settings.sonarr.url); set('sonarr.api_key', settings.sonarr.api_key);
  set('radarr.url', settings.radarr.url); set('radarr.api_key', settings.radarr.api_key);
  set('pad_start', settings.pad_start); set('pad_end', settings.pad_end);
  set('fade', settings.fade); set('track_title', settings.track_title);
  set('plex_url', settings.plex_url);
  set('plex_token', settings.plex_token);
  set('keep_backup', String(!!settings.keep_backup));
  set('trim_silence', String(!!settings.trim_silence));
  const backend = settings.asr_backend || 'builtin';
  $$('input[name="asr_backend"]').forEach((r) => { r.checked = r.value === backend; });
  const server = settings.media_server || 'none';
  $$('input[name="media_server"]').forEach((r) => { r.checked = r.value === server; });
  set('jellyfin_url', settings.jellyfin_url);
  set('jellyfin_api_key', settings.jellyfin_api_key);
  renderMediaServer();
  set('asr_url', settings.asr_url); set('asr_api_key', settings.asr_api_key);
  set('asr_remote_model', settings.asr_remote_model);
  renderAsrBackend();
  window.__auth = { configured: !!settings.auth_enabled,
                    username: settings.auth_user || '' };
  renderSecurity();
  renderInstallState();
  loadModels();
  set('custom_words', (settings.custom_words || []).join('\n'));
  set('allow_words', (settings.allow_words || []).join('\n'));
  set('judge_url', settings.judge_url); set('judge_model', settings.judge_model);
  set('judge_threads', String(settings.judge_threads));
  set('check_in_context', (settings.check_in_context || []).join('\n'));
  set('hold_policy', settings.hold_policy);

  $('#categories').innerHTML = settings.available_categories.map((c) => `
    <label><input type="checkbox" value="${c.key}"
      ${settings.categories.includes(c.key) ? 'checked' : ''}> ${escapeHtml(c.label)}</label>`).join('');
}

$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const lines = (name) => form.elements[name].value.split('\n').map((s) => s.trim()).filter(Boolean);
  const payload = {
    sonarr: { url: form.elements['sonarr.url'].value.trim(),
              api_key: form.elements['sonarr.api_key'].value.trim(), enabled: true },
    radarr: { url: form.elements['radarr.url'].value.trim(),
              api_key: form.elements['radarr.api_key'].value.trim(), enabled: true },
    categories: $$('#categories input:checked').map((i) => i.value),
    custom_words: lines('custom_words'),
    allow_words: lines('allow_words'),
    pad_start: Number(form.elements.pad_start.value),
    pad_end: Number(form.elements.pad_end.value),
    fade: Number(form.elements.fade.value),
    track_title: form.elements.track_title.value.trim() || 'Cleaned - English',
    keep_backup: form.elements.keep_backup.value === 'true',
    trim_silence: form.elements.trim_silence.value === 'true',
    asr_backend: ($$('input[name="asr_backend"]').find((r) => r.checked)
                  || {}).value || 'builtin',
    media_server: ($$('input[name="media_server"]').find((r) => r.checked)
                   || {}).value || 'none',
    jellyfin_url: form.elements.jellyfin_url.value.trim(),
    jellyfin_api_key: form.elements.jellyfin_api_key.value.trim(),
    asr_url: form.elements.asr_url.value.trim(),
    asr_api_key: form.elements.asr_api_key.value.trim(),
    asr_remote_model: form.elements.asr_remote_model.value.trim(),
    plex_url: form.elements.plex_url.value.trim(),
    plex_token: form.elements.plex_token.value.trim(),
    judge_url: form.elements.judge_url.value.trim(),
    judge_model: form.elements.judge_model.value.trim() || 'qwen3.5:9b',
    judge_threads: Number(form.elements.judge_threads.value),
    check_in_context: lines('check_in_context'),
    hold_policy: form.elements.hold_policy.value,
  };
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
    $('#save-result').textContent = 'saved';
    state.shows = []; state.movies = [];
    await loadSettings();
    plexNow();
    setTimeout(() => { $('#save-result').textContent = ''; }, 2500);
  } catch (err) {
    $('#save-result').textContent = `could not save: ${err.message}`;
  }
});


/* Installable-app plumbing.
   The worker is served from /static/ but the app lives at /, so it asks for
   the wider scope; the server sends the Service-Worker-Allowed header that
   permits it. Failing to register is not worth bothering anyone about - the
   app works fine, it just cannot be installed. */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js', { scope: '/' })
      .catch((err) => console.info('[cleanarr] no service worker:', err.message));
  });
}

/* Chrome fires this instead of showing its own prompt, so the offer has to be
   made somewhere. The button lives in Settings rather than in everyone's face. */
let installPrompt = null;
window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  installPrompt = event;
  const button = document.querySelector('#install-app');
  if (button) button.hidden = false;
});

document.addEventListener('click', async (event) => {
  if (event.target.id !== 'install-app') return;
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  event.target.hidden = true;
});

// ------------------------------------------------------------------ boot
async function boot() {
  if (!(await checkAuth())) return;    // the gate is up; stop here
  loadSettings().then(loadHome).catch(() => {});
  loadJobs();
}

boot();
setInterval(() => { if ($('#gate').hidden) loadJobs(); }, 3000);
/* Home asks both Sonarr and Radarr, so it refreshes on a slow beat rather
   than with the queue - and only while it is the page being looked at. */
setInterval(() => {
  if ($('#view-home').classList.contains('active')) loadHome();
}, 60000);
