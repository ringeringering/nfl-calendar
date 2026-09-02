/*
  Local CBS/FOX coverage resolver.

  Answers: for a viewer in a given DMA, which Sunday-afternoon CBS/FOX game is
  the local broadcast, and which require NFL Sunday Ticket?

  Two sources, in priority order:
    1. Actual published 506 coverage (window.NFL_DMA_DATA) when the week's real
       map exists. Historical seasons are fully populated.
    2. The prediction model (window.NFL_DMA_MODEL) for weeks with no map yet.

  Actual coverage always wins. The model is a prior, never an override.

  The model is ~73% accurate on a single pick but ~89% within its top two, so a
  prediction whose leader is not clear is reported as uncertain rather than
  asserted. Callers get {confident:false, alternatives:[...]} and should hedge.
*/
(function () {
  'use strict';

  // Sunday-afternoon window mapping. 4:05 and 4:25 are the single-header and
  // doubleheader slots; a network has one or the other in a given week, and 506
  // publishes a single LATE map per network, so both map to LATE.
  const EARLY_TIMES = new Set(['1:00 PM']);
  const LATE_TIMES = new Set(['4:05 PM', '4:25 PM']);

  // Schedule abbreviations -> the team names used in the historical dataset.
  const TEAM_FROM_ABBR = {
    ARI: 'Arizona', ATL: 'Atlanta', BAL: 'Baltimore', BUF: 'Buffalo',
    CAR: 'Carolina', CHI: 'Chicago', CIN: 'Cincinnati', CLE: 'Cleveland',
    DAL: 'Dallas', DEN: 'Denver', DET: 'Detroit', GB: 'Green Bay',
    HOU: 'Houston', IND: 'Indianapolis', JAX: 'Jacksonville',
    KC: 'Kansas City', LV: 'Las Vegas', LAC: 'LA Chargers', LAR: 'LA Rams',
    MIA: 'Miami', MIN: 'Minnesota', NE: 'New England', NO: 'New Orleans',
    NYG: 'NY Giants', NYJ: 'NY Jets', PHI: 'Philadelphia', PIT: 'Pittsburgh',
    SF: 'San Francisco', SEA: 'Seattle', TB: 'Tampa Bay', TEN: 'Tennessee',
    WAS: 'Washington', WSH: 'Washington'
  };

  // Absolute probability bands for how a projection is described. These are
  // thresholds on the game's OWN probability, not on the margin over the
  // runner-up: a margin-only rule labelled a 31% pick "likely" whenever the
  // field was split, which overstated it badly.
  //
  // Checked against the 2025 published maps, each band's actual rate of being
  // the local game is:
  //   likely   (>=0.60)  86%
  //   maybe    (>=0.35)  47%
  //   possibly (>=0.15)  29%
  //   remainder          3%
  const BAND_LIKELY = 0.60;
  const BAND_MAYBE = 0.35;
  const BAND_POSSIBLY = 0.15;

  // Networks carried over the air by a local affiliate. A game on one of these
  // is watchable without Sunday Ticket wherever it is the network's only game
  // in that slot. ESPN/Amazon/Netflix/NFL Network are cable or streaming and
  // are deliberately excluded.
  const OTA_NETWORKS = new Set(['CBS', 'FOX', 'NBC', 'ABC', 'ESPN/ABC']);

  function bandFor(p) {
    if (p >= BAND_LIKELY) return 'likely';
    if (p >= BAND_MAYBE) return 'maybe';
    if (p >= BAND_POSSIBLY) return 'possibly';
    return 'unlikely';
  }

  function isSunday(dateStr) {
    // Parse as UTC so the local timezone cannot shift the weekday.
    const d = new Date(dateStr + 'T12:00:00Z');
    return d.getUTCDay() === 0;
  }

  /* Return 'EARLY' | 'LATE' | null for a scheduled game. */
  function windowOf(game) {
    if (!game || !isSunday(game.date)) return null;
    if (game.broadcast !== 'CBS' && game.broadcast !== 'FOX') return null;
    if (EARLY_TIMES.has(game.time)) return 'EARLY';
    if (LATE_TIMES.has(game.time)) return 'LATE';
    return null;
  }

  function slotKey(game) {
    const win = windowOf(game);
    return win ? game.broadcast + '_' + win : null;
  }

  function teamsOf(game) {
    return [TEAM_FROM_ABBR[game.away], TEAM_FROM_ABBR[game.home]].filter(Boolean);
  }

  /* Games competing with this one: same week, network and window. */
  function competingGames(game, allGames) {
    const key = slotKey(game);
    if (!key) return [];
    return allGames.filter(g => g.week === game.week && slotKey(g) === key);
  }

  /* Is this an over-the-air game outside the Sunday-afternoon windows that the
     whole country receives? Thursday/Friday/Saturday/Sunday-night/Monday games
     on CBS, FOX, NBC or ABC are single national broadcasts, so a local
     affiliate carries them and no Sunday Ticket is required.

     Guarded by an explicit check that the network really has only one game in
     that slot, so a future doubleheader or a flexed TBD is not mislabelled. */
  function nationalBroadcast(game, allGames) {
    if (!game || !OTA_NETWORKS.has(game.broadcast)) return false;
    if (windowOf(game)) return false;  // Sunday afternoon: market-dependent.
    const sameSlot = (allGames || []).filter(g =>
      g.broadcast === game.broadcast && g.date === game.date && g.time === game.time);
    return sameSlot.length <= 1;
  }

  // ---- source 1: actual published coverage ----

  function actualCoverage(dmaCode, game) {
    const data = window.NFL_DMA_DATA;
    if (!data || !data.maps) return null;
    const season = String(game.date).slice(0, 4);
    const key = season + '|' + game.week + '|' + slotKey(game);
    const m = data.maps[key];
    if (!m) return null;
    const idx = (m.e && Object.prototype.hasOwnProperty.call(m.e, dmaCode))
      ? m.e[dmaCode] : m.d;
    const shown = m.g[idx];
    if (!shown) return null;
    const uncertain = m.u && m.u[dmaCode];
    return {
      source: 'actual',
      game: shown,
      confident: uncertain === undefined,
      confidence: uncertain === undefined ? 1 : uncertain,
      alternatives: []
    };
  }

  // ---- source 2: prediction model ----

  function calibrate(p, table) {
    if (!table || !table.length) return p;
    for (const [upper, corrected] of table) {
      if (p <= upper) return corrected;
    }
    return table[table.length - 1][1];
  }

  function predictCoverage(dmaCode, game, allGames) {
    const model = window.NFL_DMA_MODEL;
    if (!model || !model.rates) return null;
    const field = competingGames(game, allGames);
    if (!field.length) return null;

    const label = g => `${TEAM_FROM_ABBR[g.away]} @ ${TEAM_FROM_ABBR[g.home]}`;

    // Only one game in the slot: it is the local broadcast by construction.
    if (field.length === 1) {
      return {
        source: 'certain',
        game: label(field[0]),
        confident: true,
        confidence: 1,
        alternatives: []
      };
    }

    const rates = model.rates[String(dmaCode)] || {};
    const p = model.params || {};
    const prior = p.neutral_prior != null ? p.neutral_prior : 0.25;
    const maxW = p.max_weight != null ? p.max_weight : 0.75;
    const temp = p.temperature != null ? p.temperature : 10;

    const scored = field.map(g => {
      const vs = teamsOf(g).map(t => (rates[t] != null ? rates[t] : prior));
      if (!vs.length) return { g, score: prior };
      const mx = Math.max.apply(null, vs);
      const mean = vs.reduce((a, b) => a + b, 0) / vs.length;
      return { g, score: maxW * mx + (1 - maxW) * mean };
    });

    const top = Math.max.apply(null, scored.map(s => s.score));
    const exps = scored.map(s => ({ g: s.g, w: Math.exp((s.score - top) * temp) }));
    const total = exps.reduce((a, b) => a + b.w, 0) || 1;
    const ranked = exps
      .map(e => ({ game: label(e.g), p: e.w / total }))
      .sort((a, b) => b.p - a.p);

    // This game's own probability drives its label, so a game that is not the
    // leader can still be reported as a real possibility with a number.
    const thisLabel = label(game);
    const mine = ranked.find(r => r.game === thisLabel);
    const ownP = mine ? mine.p : 0;

    return {
      source: 'predicted',
      game: ranked[0].game,
      confidence: calibrate(ranked[0].p, model.calibration),
      ownProbability: ownP,
      band: bandFor(ownP),
      confident: ranked[0].p >= BAND_LIKELY,
      alternatives: ranked.slice(1, 3).map(r => ({ game: r.game, confidence: r.p }))
    };
  }

  /* Main entry point.

     Returns null when the game is not a Sunday-afternoon CBS/FOX game (there is
     nothing to resolve — the schedule's own service label already applies).
     Otherwise:
       { source, game, isLocal, confident, confidence, alternatives }
     where `game` is the matchup that airs locally and `isLocal` says whether
     THIS game is that one. */
  function resolve(dmaCode, game, allGames) {
    if (!dmaCode || !game) return null;

    // National over-the-air broadcasts are local everywhere, and need no
    // market data at all.
    if (nationalBroadcast(game, allGames)) {
      const thisGame = `${TEAM_FROM_ABBR[game.away]} @ ${TEAM_FROM_ABBR[game.home]}`;
      return {
        source: 'national',
        game: thisGame,
        localGame: thisGame,
        isLocal: true,
        possiblyLocal: false,
        confident: true,
        confidence: 1,
        ownProbability: 1,
        band: 'certain',
        alternatives: []
      };
    }

    if (!windowOf(game)) return null;
    const code = String(dmaCode);
    const result = actualCoverage(code, game)
      || predictCoverage(code, game, allGames || []);
    if (!result) return null;

    const thisGame = `${TEAM_FROM_ABBR[game.away]} @ ${TEAM_FROM_ABBR[game.home]}`;
    // The stored/predicted label may carry a "(LATE)" marker from a
    // single-header map; compare on the team pair, not the raw string.
    const norm = s => String(s).replace(/\s*\((?:LATE|EARLY)\)\s*$/i, '').trim();
    const isLocal = norm(result.game) === norm(thisGame);

    // Confirmed sources are all-or-nothing; only projections get a band.
    let band = result.band;
    if (result.source === 'actual' || result.source === 'certain') {
      band = isLocal ? (result.confident ? 'certain' : 'maybe') : 'unlikely';
    }

    return Object.assign({}, result, {
      localGame: result.game,
      isLocal,
      band,
      // Retained for callers that only need the coarse distinction.
      possiblyLocal: !isLocal && (band === 'maybe' || band === 'possibly')
    });
  }

  /* Full distribution over the games competing in one slot, for one market.

     Returns [{game, p}] sorted most likely first, with a `confirmed` flag on
     the array. For a published map or a national broadcast the "probabilities"
     are 1/0 — it is a known fact, not an estimate — and `confirmed` says so
     rather than dressing certainty up as 100%.

     Returns [] when the game has no market-dependent coverage. */
  function probabilities(dmaCode, game, allGames) {
    const empty = [];
    empty.confirmed = false;
    if (!dmaCode || !game) return empty;

    const field = competingGames(game, allGames);
    const label = g => `${TEAM_FROM_ABBR[g.away]} @ ${TEAM_FROM_ABBR[g.home]}`;

    if (nationalBroadcast(game, allGames)) {
      const out = [{ game: label(game), p: 1 }];
      out.confirmed = true;
      return out;
    }
    if (!windowOf(game) || field.length === 0) return empty;

    const code = String(dmaCode);
    const norm = s => String(s).replace(/\s*\((?:LATE|EARLY)\)\s*$/i, '').trim();

    // Published coverage: exactly one game is local here.
    const actual = actualCoverage(code, game);
    if (actual) {
      const out = field.map(g => ({
        game: label(g),
        p: norm(actual.game) === norm(label(g)) ? 1 : 0
      })).sort((a, b) => b.p - a.p);
      out.confirmed = true;
      return out;
    }

    if (field.length === 1) {
      const out = [{ game: label(field[0]), p: 1 }];
      out.confirmed = true;
      return out;
    }

    const model = window.NFL_DMA_MODEL;
    if (!model || !model.rates) return empty;
    const rates = model.rates[code] || {};
    const p = model.params || {};
    const prior = p.neutral_prior != null ? p.neutral_prior : 0.25;
    const maxW = p.max_weight != null ? p.max_weight : 0.75;
    const temp = p.temperature != null ? p.temperature : 10;

    const scored = field.map(g => {
      const vs = teamsOf(g).map(t => (rates[t] != null ? rates[t] : prior));
      if (!vs.length) return { g, score: prior };
      const mx = Math.max.apply(null, vs);
      const mean = vs.reduce((a, b) => a + b, 0) / vs.length;
      return { g, score: maxW * mx + (1 - maxW) * mean };
    });
    const top = Math.max.apply(null, scored.map(s => s.score));
    const exps = scored.map(s => ({ g: s.g, w: Math.exp((s.score - top) * temp) }));
    const total = exps.reduce((a, b) => a + b.w, 0) || 1;
    const out = exps
      .map(e => ({ game: label(e.g), p: e.w / total }))
      .sort((a, b) => b.p - a.p);
    out.confirmed = false;
    return out;
  }

  /* Canonical game label. Exported so callers key on the same strings that
     probabilities() returns instead of re-deriving them. */
  function gameLabel(game) {
    return `${TEAM_FROM_ABBR[game.away]} @ ${TEAM_FROM_ABBR[game.home]}`;
  }

  window.NFL_LOCAL_COVERAGE = {
    resolve,
    probabilities,
    gameLabel,
    windowOf,
    slotKey,
    competingGames,
    nationalBroadcast,
    bandFor,
    BANDS: { likely: BAND_LIKELY, maybe: BAND_MAYBE, possibly: BAND_POSSIBLY }
  };
})();
