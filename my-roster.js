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

  // ---- ESPN league import (public leagues only) ----
  //
  // Runs entirely in the browser. ESPN's fantasy read host returns permissive
  // CORS headers (it echoes the requesting Origin), so no server or GitHub
  // Action is needed and the league ID never leaves the user's machine.
  //
  // Only PUBLIC leagues work. A private league returns 401 with type
  // AUTH_LEAGUE_NOT_VISIBLE, which needs espn_s2/SWID cookies -- full account
  // credentials that cannot be sent from a browser anyway. That case is
  // reported as a clear, actionable message rather than a generic failure.
  const LEAGUE_HOST = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons';

  /* Accept a bare league id or a pasted ESPN URL containing leagueId=.
     Also handles /football/team/<id> and /football/league/<id> path forms. */
  function parseLeagueId(input) {
    const s = String(input || '').trim();
    const m = s.match(/leagueId[=/](\d+)/i)
      || s.match(/\/(?:league|team)\/(\d+)/i)
      || s.match(/^(\d+)$/);
    return m ? m[1] : null;
  }

  function espnSeason() {
    const d = new Date();
    // Jan-Jun still belongs to the prior NFL season.
    return d.getMonth() >= 6 ? d.getFullYear() : d.getFullYear() - 1;
  }

  /* Fetch the teams in a public league: [{id, name, abbrev}]. */
  async function fetchLeagueTeams(leagueInput, season) {
    const id = parseLeagueId(leagueInput);
    if (!id) throw new Error('Enter a league ID (the number in your ESPN league URL).');
    const yr = season || espnSeason();
    const url = `${LEAGUE_HOST}/${yr}/segments/0/leagues/${id}?view=mTeam`;
    // ESPN omits CORS headers on some error responses, so a private or missing
    // league can surface either as a readable status or as an opaque network
    // failure the browser refuses to expose. Both are handled, and the opaque
    // case names both likely causes rather than guessing one.
    const PRIVATE_MSG = 'ESPN would not share that league. Either it is private '
      + '(ask your league manager to set it to public in League Settings '
      + '→ Basic Settings) or the ID/season is wrong. You can also build '
      + 'your roster manually.';
    let resp;
    try {
      resp = await fetch(url, { credentials: 'omit' });
    } catch (e) {
      throw new Error(PRIVATE_MSG);
    }
    if (resp.status === 401 || resp.status === 403) {
      throw new Error('That league is private. Ask your league manager to set it '
        + 'to public in League Settings → Basic Settings, or build your '
        + 'roster manually.');
    }
    if (resp.status === 404) {
      throw new Error(`No ${yr} league found with ID ${id}. Check the ID and the season.`);
    }
    if (!resp.ok) throw new Error(`ESPN returned ${resp.status}. Try again later.`);
    const data = await resp.json();
    const teams = (data && data.teams) || [];
    if (!teams.length) throw new Error('That league returned no teams.');
    return teams.map(t => ({
      id: t.id,
      name: (t.name || [t.location, t.nickname].filter(Boolean).join(' ') || `Team ${t.id}`).trim(),
      abbrev: t.abbrev || ''
    }));
  }

  /* Fetch one team's roster and replace the local roster with it.
     Returns {added, unmatched} -- unmatched are players ESPN rosters but the
     projections feed does not carry (deep bench, practice squad), reported
     rather than silently dropped. */
  async function importTeam(leagueInput, teamId, season, label) {
    const id = parseLeagueId(leagueInput);
    const yr = season || espnSeason();
    const url = `${LEAGUE_HOST}/${yr}/segments/0/leagues/${id}`
      + `?view=mRoster&forTeamId=${encodeURIComponent(teamId)}`;
    let resp;
    try {
      resp = await fetch(url, { credentials: 'omit' });
    } catch (e) {
      throw new Error('ESPN would not share that roster. The league may be private.');
    }
    if (resp.status === 401 || resp.status === 403) {
      throw new Error('That league is private.');
    }
    if (!resp.ok) throw new Error(`ESPN returned ${resp.status}.`);
    const data = await resp.json();
    const team = ((data && data.teams) || []).find(t => String(t.id) === String(teamId));
    const entries = (team && team.roster && team.roster.entries) || [];
    if (!entries.length) throw new Error('That team has no players on its roster.');

    const names = [];
    const unmatched = [];
    for (const e of entries) {
      const nm = e.playerPoolEntry && e.playerPoolEntry.player
        && e.playerPoolEntry.player.fullName;
      if (!nm) continue;
      // Keep ESPN's own name when the projections feed knows the player, so
      // later matching is exact; otherwise record it as unmatched.
      const known = resolve(nm);
      if (known) names.push(known.name);
      else unmatched.push(nm);
    }
    if (!names.length) {
      throw new Error('None of that roster\'s players are in the projections feed.');
    }
    roster = names.slice(0, MAX_PLAYERS);
    teamName = String(label || '').slice(0, 40);
    indexCache = null;
    save();
    return { added: roster.length, unmatched };
  }

  load();

  window.NFL_MY_ROSTER = {
    load, save, add, remove, clear, has, resolve, suggest,
    playersForWeek, playersForGame, onChange,
    parseLeagueId, fetchLeagueTeams, importTeam, espnSeason,
    get list() { return [...roster]; },
    get teamName() { return teamName; },
    setTeamName,
    get size() { return roster.length; },
    MAX_PLAYERS
  };
})();
