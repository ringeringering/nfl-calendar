/*
  My Roster: a manually entered fantasy roster, stored per browser.

  Deliberately localStorage rather than a committed file. The site is public and
  shared with a whole league, so a committed roster would show everyone's team
  to everyone and need a commit for every waiver move. In localStorage each
  person's roster is private to their own browser and editable instantly --
  the same approach the viewing-ZIP field already uses.

  Players are keyed by name. Verified against the projections feed: all 487
  rostered-eligible players have names unique to a single team, so no ID
  mapping is needed. Names are matched case- and punctuation-insensitively so
  "aj brown", "A.J. Brown" and "AJ Brown" all resolve.

  If the league is later made public and rosters are fetched automatically, this
  stays useful as an override and as the path for anyone outside that league.
*/
(function () {
  'use strict';

  const KEY = 'nflMyRoster';
  const NAME_KEY = 'nflMyRosterName';
  const MAX_PLAYERS = 40;

  let roster = [];        // array of canonical player names
  let teamName = '';
  const listeners = [];

  /* Normalize for matching: lowercase, strip punctuation and suffixes. */
  function norm(s) {
    return String(s || '')
      .toLowerCase()
      .replace(/\b(jr|sr|ii|iii|iv|v)\b\.?/g, '')
      .replace(/[^a-z0-9]/g, '');
  }

  /* All players known to the projections feed, newest week first so a traded
     player resolves to their current team. */
  function allPlayers() {
    const data = window.NFL_FANTASY_DATA;
    if (!data || !Array.isArray(data.players)) return [];
    const seen = new Map();
    for (const p of data.players) {
      const k = norm(p.name);
      const prev = seen.get(k);
      if (!prev || p.week > prev.week) seen.set(k, p);
    }
    return [...seen.values()];
  }

  let indexCache = null;
  function index() {
    if (indexCache) return indexCache;
    indexCache = new Map();
    for (const p of allPlayers()) indexCache.set(norm(p.name), p);
    return indexCache;
  }

  /* Resolve free text to a known player, or null. */
  function resolve(input) {
    const k = norm(input);
    if (!k) return null;
    const exact = index().get(k);
    if (exact) return exact;
    // Fall back to a unique prefix/substring match so partial names work.
    const hits = [];
    for (const [key, p] of index()) {
      if (key.startsWith(k) || key.includes(k)) hits.push(p);
      if (hits.length > 1) break;
    }
    return hits.length === 1 ? hits[0] : null;
  }

  /* Suggestions for the input's typeahead. */
  function suggest(input, limit) {
    const k = norm(input);
    if (!k) return [];
    const starts = [], contains = [];
    for (const [key, p] of index()) {
      if (rosterHas(p.name)) continue;
      if (key.startsWith(k)) starts.push(p);
      else if (key.includes(k)) contains.push(p);
    }
    const rank = (a, b) => (Number(b.projected_points) || 0) - (Number(a.projected_points) || 0);
    return starts.sort(rank).concat(contains.sort(rank)).slice(0, limit || 6);
  }

  function rosterHas(name) {
    const k = norm(name);
    return roster.some(n => norm(n) === k);
  }

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      roster = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(roster)) roster = [];
      roster = roster.filter(n => typeof n === 'string').slice(0, MAX_PLAYERS);
    } catch (e) {
      roster = [];
    }
    teamName = localStorage.getItem(NAME_KEY) || '';
    return roster;
  }

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(roster));
      if (teamName) localStorage.setItem(NAME_KEY, teamName);
      else localStorage.removeItem(NAME_KEY);
    } catch (e) {
      // Quota or private-mode failure: keep the in-memory roster working.
      console.warn('Could not persist roster', e);
    }
    listeners.forEach(fn => { try { fn(); } catch (e) {} });
  }

  function add(input) {
    const p = resolve(input);
    if (!p) return { ok: false, reason: 'not_found' };
    if (rosterHas(p.name)) return { ok: false, reason: 'duplicate', player: p };
    if (roster.length >= MAX_PLAYERS) return { ok: false, reason: 'full' };
    roster.push(p.name);
    save();
    return { ok: true, player: p };
  }

  function remove(name) {
    const k = norm(name);
    const before = roster.length;
    roster = roster.filter(n => norm(n) !== k);
    if (roster.length !== before) save();
    return roster.length !== before;
  }

  function clear() {
    roster = [];
    teamName = '';
    save();
  }

  function setTeamName(n) {
    teamName = String(n || '').slice(0, 40);
    save();
  }

  /* Is this player on my roster? Accepts a player object or a name. */
  function has(player) {
    return rosterHas(typeof player === 'string' ? player : (player && player.name));
  }

  /* My players appearing in a given week, with their current projection. */
  function playersForWeek(week) {
    const data = window.NFL_FANTASY_DATA;
    if (!data || !Array.isArray(data.players)) return [];
    const mine = new Set(roster.map(norm));
    return data.players
      .filter(p => p.week === week && mine.has(norm(p.name)))
      .sort((a, b) => (Number(b.projected_points) || 0) - (Number(a.projected_points) || 0));
  }

  /* My players involved in one specific game. */
  function playersForGame(game) {
    if (!game) return [];
    return playersForWeek(game.week)
      .filter(p => p.team === game.away || p.team === game.home);
  }

  function onChange(fn) { if (typeof fn === 'function') listeners.push(fn); }

  load();

  window.NFL_MY_ROSTER = {
    load, save, add, remove, clear, has, resolve, suggest,
    playersForWeek, playersForGame, onChange,
    get list() { return [...roster]; },
    get teamName() { return teamName; },
    setTeamName,
    get size() { return roster.length; },
    MAX_PLAYERS
  };
})();
