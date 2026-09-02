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
  const SLOT_KEY = 'nflMyRosterSlots';
  const NAME_KEY = 'nflMyRosterName';
  const MAX_PLAYERS = 40;

  /* A standard ESPN-style starting lineup plus a bench. `pos` lists the
     positions eligible for the slot; FLEX takes RB/WR/TE, and bench slots
     (pos null) accept anything. Ids are stable so saved assignments survive
     changes here as long as the id is unchanged. */
  const SLOTS = [
    { id: 'QB',   label: 'QB',    pos: ['QB'] },
    { id: 'RB1',  label: 'RB',    pos: ['RB'] },
    { id: 'RB2',  label: 'RB',    pos: ['RB'] },
    { id: 'WR1',  label: 'WR',    pos: ['WR'] },
    { id: 'WR2',  label: 'WR',    pos: ['WR'] },
    { id: 'TE',   label: 'TE',    pos: ['TE'] },
    { id: 'FLEX', label: 'FLEX',  pos: ['RB', 'WR', 'TE'] },
    { id: 'K',    label: 'K',     pos: ['K'] },
    { id: 'DEF',  label: 'D/ST',  pos: ['DEF'] },
    { id: 'BN1',  label: 'Bench', pos: null },
    { id: 'BN2',  label: 'Bench', pos: null },
    { id: 'BN3',  label: 'Bench', pos: null },
    { id: 'BN4',  label: 'Bench', pos: null },
    { id: 'BN5',  label: 'Bench', pos: null },
    { id: 'BN6',  label: 'Bench', pos: null }
  ];

  /* Two rosters: the viewer's own team and an optional opponent, so a
     head-to-head matchup can be shown. Both use the same slot layout and the
     same storage shape; `side` selects which one an operation applies to.
     Legacy single-roster keys are migrated into the 'mine' side on load. */
  const SIDES = ['mine', 'opp'];
  const store = {
    mine: { roster: [], slots: {}, teamName: '' },
    opp:  { roster: [], slots: {}, teamName: '' }
  };
  let activeSide = 'mine';   // which side the editor is currently editing
  const listeners = [];

  function sideKeys(side) {
    return side === 'opp'
      ? { list: 'nflOppRoster', slots: 'nflOppRosterSlots', name: 'nflOppRosterName' }
      : { list: KEY, slots: SLOT_KEY, name: NAME_KEY };
  }
  function S(side) { return store[side === 'opp' ? 'opp' : 'mine']; }

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
      if (rosterHas(p.name, activeSide)) continue;
      if (key.startsWith(k)) starts.push(p);
      else if (key.includes(k)) contains.push(p);
    }
    const rank = (a, b) => (Number(b.projected_points) || 0) - (Number(a.projected_points) || 0);
    return starts.sort(rank).concat(contains.sort(rank)).slice(0, limit || 6);
  }

  function rosterHas(name, side) {
    const k = norm(name);
    return S(side).roster.some(n => norm(n) === k);
  }

  function loadSide(side) {
    const k = sideKeys(side), st = S(side);
    try {
      const raw = localStorage.getItem(k.list);
      st.roster = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(st.roster)) st.roster = [];
      st.roster = st.roster.filter(n => typeof n === 'string').slice(0, MAX_PLAYERS);
    } catch (e) { st.roster = []; }
    try {
      const raw = localStorage.getItem(k.slots);
      st.slots = raw ? JSON.parse(raw) : {};
      if (!st.slots || typeof st.slots !== 'object' || Array.isArray(st.slots)) st.slots = {};
    } catch (e) { st.slots = {}; }
    // Rosters saved before slots existed, or imported from ESPN, arrive as a
    // flat list. Auto-assign them so the editor opens populated, not blank.
    let migrated = false;
    if (st.roster.length && !Object.keys(st.slots).length) {
      st.slots = autoAssign(st.roster);
      migrated = true;
    }
    st.teamName = localStorage.getItem(k.name) || '';
    if (migrated) {
      try { localStorage.setItem(k.slots, JSON.stringify(st.slots)); } catch (e) {}
    }
    return st.roster;
  }

  function load() {
    SIDES.forEach(loadSide);
    return S('mine').roster;
  }

  /* Place a flat list of names into slots, best projection first. Used for
     legacy rosters and after an ESPN import. */
  function autoAssign(names) {
    const out = {};
    const pool = (names || [])
      .map(n => resolve(n))
      .filter(Boolean)
      .sort((a, b) => (Number(b.projected_points) || 0) - (Number(a.projected_points) || 0));
    const used = new Set();
    for (const s of SLOTS) {
      if (!s.pos) continue;
      const pick = pool.find(x => !used.has(x) && s.pos.includes(x.position));
      if (pick) { out[s.id] = pick.name; used.add(pick); }
    }
    const bench = SLOTS.filter(s => !s.pos);
    let bi = 0;
    for (const x of pool) {
      if (used.has(x) || bi >= bench.length) continue;
      out[bench[bi++].id] = x.name;
      used.add(x);
    }
    return out;
  }

  /* Keep the flat list derived from slot assignments so has() and the
     per-game marks can never drift from what the editor shows. */
  function syncFromSlots(side) {
    const st = S(side);
    const seen = new Set();
    const out = [];
    for (const s of SLOTS) {
      const n = st.slots[s.id];
      if (!n) continue;
      const k = norm(n);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(n);
    }
    st.roster = out.slice(0, MAX_PLAYERS);
  }

  function save(side) {
    const k = sideKeys(side || activeSide), st = S(side || activeSide);
    try {
      localStorage.setItem(k.list, JSON.stringify(st.roster));
      localStorage.setItem(k.slots, JSON.stringify(st.slots));
      if (st.teamName) localStorage.setItem(k.name, st.teamName);
      else localStorage.removeItem(k.name);
    } catch (e) {
      // Quota or private-mode failure: keep the in-memory roster working.
      console.warn('Could not persist roster', e);
    }
    listeners.forEach(fn => { try { fn(); } catch (e) {} });
  }

  /* Assign a player to a slot; null clears it. If that player already sits in
     another slot the two swap, which is what moving a player into an occupied
     slot should do. */
  function setSlot(slotId, input, side) {
    side = side || activeSide;
    const st = S(side);
    const slot = SLOTS.find(s => s.id === slotId);
    if (!slot) return { ok: false, reason: 'bad_slot' };
    if (input == null || input === '') {
      delete st.slots[slotId];
      syncFromSlots(side); save(side);
      return { ok: true, cleared: true };
    }
    const p = resolve(input);
    if (!p) return { ok: false, reason: 'not_found' };
    if (slot.pos && !slot.pos.includes(p.position)) {
      return { ok: false, reason: 'wrong_position', player: p, slot: slot };
    }
    const prior = Object.keys(st.slots).find(id => norm(st.slots[id]) === norm(p.name));
    if (prior && prior !== slotId) {
      const displaced = st.slots[slotId];
      if (displaced) st.slots[prior] = displaced; else delete st.slots[prior];
    }
    st.slots[slotId] = p.name;
    syncFromSlots(side); save(side);
    return { ok: true, player: p, swapped: !!(prior && prior !== slotId) };
  }

  /* Players eligible for a slot, ranked by projection, excluding anyone
     already placed in a different slot. */
  function candidatesFor(slotId, query, limit, side) {
    const st = S(side || activeSide);
    const slot = SLOTS.find(s => s.id === slotId);
    if (!slot) return [];
    // Only this side's placed players are excluded. The same player may
    // legitimately appear on both rosters -- leagues differ, and a viewer may
    // be modelling a hypothetical matchup.
    const placed = new Set(Object.keys(st.slots)
      .filter(id => id !== slotId)
      .map(id => norm(st.slots[id])));
    const k = norm(query || '');
    const hits = [];
    for (const p of allPlayers()) {
      if (slot.pos && !slot.pos.includes(p.position)) continue;
      if (placed.has(norm(p.name))) continue;
      if (k && !norm(p.name).includes(k)) continue;
      hits.push(p);
    }
    hits.sort((a, b) => (Number(b.projected_points) || 0) - (Number(a.projected_points) || 0));
    return limit ? hits.slice(0, limit) : hits;
  }



  function add(input, side) {
    side = side || activeSide;
    const st = S(side);
    const p = resolve(input);
    if (!p) return { ok: false, reason: 'not_found' };
    if (rosterHas(p.name, side)) return { ok: false, reason: 'duplicate', player: p };
    if (st.roster.length >= MAX_PLAYERS) return { ok: false, reason: 'full' };
    // Place into the first eligible empty slot so add() and the slot editor
    // stay consistent; fall back to appending if the lineup is full.
    const openSlot = SLOTS.find(s => !st.slots[s.id]
      && (!s.pos || s.pos.includes(p.position)));
    if (openSlot) {
      st.slots[openSlot.id] = p.name;
      syncFromSlots(side);
    } else {
      st.roster.push(p.name);
    }
    save(side);
    return { ok: true, player: p };
  }

  function remove(name, side) {
    side = side || activeSide;
    const st = S(side);
    const k = norm(name);
    const before = st.roster.length;
    for (const id of Object.keys(st.slots)) {
      if (norm(st.slots[id]) === k) delete st.slots[id];
    }
    syncFromSlots(side);
    if (st.roster.length === before) {
      st.roster = st.roster.filter(n => norm(n) !== k);
    }
    if (st.roster.length !== before) save(side);
    return st.roster.length !== before;
  }

  function clear(side) {
    side = side || activeSide;
    const st = S(side);
    st.roster = []; st.slots = {}; st.teamName = '';
    save(side);
  }

  function setTeamName(n, side) {
    S(side || activeSide).teamName = String(n || '').slice(0, 40);
    save(side || activeSide);
  }

  /* Is this player on a roster? Accepts a player object or a name.
     Defaults to the viewer's own team so existing callers are unchanged. */
  function has(player, side) {
    return rosterHas(typeof player === 'string' ? player : (player && player.name),
                     side || 'mine');
  }

  /* My players appearing in a given week, with their current projection. */
  function playersForWeek(week, side) {
    const data = window.NFL_FANTASY_DATA;
    if (!data || !Array.isArray(data.players)) return [];
    const want = new Set(S(side || 'mine').roster.map(norm));
    return data.players
      .filter(p => p.week === week && want.has(norm(p.name)))
      .sort((a, b) => (Number(b.projected_points) || 0) - (Number(a.projected_points) || 0));
  }

  /* My players involved in one specific game. */
  function playersForGame(game, side) {
    if (!game) return [];
    return playersForWeek(game.week, side)
      .filter(p => p.team === game.away || p.team === game.home);
  }

  /* Head-to-head: projected starting totals for both rosters in one week,
     slot by slot. Returns null unless both sides have at least one starter,
     since a matchup against an empty roster is meaningless. */
  function matchup(week) {
    const rows = [];
    let mineTotal = 0, oppTotal = 0, mineFilled = 0, oppFilled = 0;
    for (const s of SLOTS) {
      if (!s.pos) continue;   // starters only
      const mn = S('mine').slots[s.id], on = S('opp').slots[s.id];
      const mp = mn ? weekEntry(mn, week) : null;
      const op = on ? weekEntry(on, week) : null;
      const mv = mp ? Number(mp.projected_points) || 0 : 0;
      const ov = op ? Number(op.projected_points) || 0 : 0;
      if (mn) mineFilled++;
      if (on) oppFilled++;
      mineTotal += mv; oppTotal += ov;
      rows.push({ slot: s.label, slotId: s.id, mine: mp, opp: op,
                  mineName: mn || '', oppName: on || '',
                  mineValue: mv, oppValue: ov });
    }
    if (!mineFilled || !oppFilled) return null;
    return { week: week, rows: rows, mineTotal: mineTotal, oppTotal: oppTotal,
             mineName: S('mine').teamName, oppName: S('opp').teamName };
  }

  /* A player's row for a specific week, or null if they have no projection
     that week (bye, or not projected). */
  function weekEntry(name, week) {
    const data = window.NFL_FANTASY_DATA;
    if (!data || !Array.isArray(data.players)) return null;
    const k = norm(name);
    return data.players.find(p => p.week === week && norm(p.name) === k) || null;
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
    const side = arguments.length > 4 && arguments[4] ? arguments[4] : activeSide;
    const st = S(side);
    st.roster = names.slice(0, MAX_PLAYERS);
    st.slots = autoAssign(st.roster);
    syncFromSlots(side);
    st.teamName = String(label || '').slice(0, 40);
    save(side);
    return { added: st.roster.length, unmatched };
  }

  load();

  window.NFL_MY_ROSTER = {
    load, save, add, remove, clear, has, resolve, suggest,
    playersForWeek, playersForGame, onChange,
    parseLeagueId, fetchLeagueTeams, importTeam, espnSeason,
    SLOTS, SIDES, setSlot, candidatesFor, autoAssign, matchup, weekEntry,
    // Which side the editor is working on.
    get side() { return activeSide; },
    setSide(s) { activeSide = (s === 'opp') ? 'opp' : 'mine'; return activeSide; },
    slotsFor(side) { return Object.assign({}, S(side).slots); },
    listFor(side) { return [...S(side).roster]; },
    sizeFor(side) { return S(side).roster.length; },
    nameFor(side) { return S(side).teamName; },
    setTeamName,
    // Back-compatible accessors, always the viewer's own team.
    get slots() { return Object.assign({}, S(activeSide).slots); },
    get list() { return [...S('mine').roster]; },
    get teamName() { return S('mine').teamName; },
    get size() { return S('mine').roster.length; },
    MAX_PLAYERS
  };
})();
