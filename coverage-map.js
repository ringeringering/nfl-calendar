/*
  Coverage map: shades every US television market by how likely a given
  CBS/FOX game is to be the local broadcast there.

  Self-contained SVG renderer. No mapping library:
    * decodes TopoJSON arcs (delta-encoded integers + transform)
    * projects lon/lat with an Albers-style conic that suits the lower 48
    * builds one <path> per DMA, colored by probability

  Alaska and Hawaii are dropped rather than inset. They are DMAs 743/744/745
  and 725, and a correct inset needs its own projection and frame; showing them
  in a shared projection would put them far off-frame and squash the map.
*/
(function () {
  'use strict';

  // Albers equal-area conic, standard parallels 29.5N/45.5N (the usual choice
  // for a lower-48 map). Keeps state shapes recognizable.
  const LAT1 = 29.5 * Math.PI / 180;
  const LAT2 = 45.5 * Math.PI / 180;
  const LAT0 = 37.5 * Math.PI / 180;
  const LON0 = -96 * Math.PI / 180;
  const N = 0.5 * (Math.sin(LAT1) + Math.sin(LAT2));
  const C = Math.cos(LAT1) * Math.cos(LAT1) + 2 * N * Math.sin(LAT1);
  const RHO0 = Math.sqrt(C - 2 * N * Math.sin(LAT0)) / N;

  // Non-contiguous markets: excluded from the frame (see note above).
  const OFF_MAP = new Set(['743', '744', '745', '725']);

  function project(lon, lat) {
    const l = lon * Math.PI / 180, p = lat * Math.PI / 180;
    const rho = Math.sqrt(Math.max(0, C - 2 * N * Math.sin(p))) / N;
    const theta = N * (l - LON0);
    return [rho * Math.sin(theta), RHO0 - rho * Math.cos(theta)];
  }

  /* Decode TopoJSON arcs into absolute lon/lat point lists. */
  function decodeArcs(topo) {
    const s = topo.transform.scale, t = topo.transform.translate;
    return topo.arcs.map(arc => {
      let x = 0, y = 0;
      return arc.map(d => {
        x += d[0]; y += d[1];
        return [x * s[0] + t[0], y * s[1] + t[1]];
      });
    });
  }

  function ringFor(indexes, arcs) {
    const pts = [];
    for (const idx of indexes) {
      const arc = idx < 0 ? arcs[~idx].slice().reverse() : arcs[idx];
      // Consecutive arcs share an endpoint; drop the duplicate.
      for (let i = (pts.length && arc.length) ? 1 : 0; i < arc.length; i++) pts.push(arc[i]);
    }
    return pts;
  }

  function polygonsFor(geom, arcs) {
    if (geom.type === 'Polygon') return [geom.arcs];
    if (geom.type === 'MultiPolygon') return geom.arcs;
    return [];
  }

  let cache = null;
  /* Build projected paths once; reused for every game. */
  function buildPaths() {
    if (cache) return cache;
    const topo = window.NFL_DMA_GEO;
    if (!topo) return null;
    const arcs = decodeArcs(topo);
    const shapes = [];
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

    for (const geom of topo.objects.dma.geometries) {
      const code = String(geom.id);
      if (OFF_MAP.has(code)) continue;
      const rings = [];
      for (const poly of polygonsFor(geom, arcs)) {
        for (const ringIdx of poly) {
          const pts = ringFor(ringIdx, arcs).map(p => project(p[0], p[1]));
          if (pts.length < 3) continue;
          rings.push(pts);
          for (const [x, y] of pts) {
            if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
          }
        }
      }
      if (rings.length) shapes.push({ code, rings });
    }

    cache = { shapes, bounds: [minX, minY, maxX, maxY], names: topo.dma_names || {} };
    return cache;
  }

  function toPathData(rings, sx, sy) {
    let d = '';
    for (const ring of rings) {
      for (let i = 0; i < ring.length; i++) {
        const [x, y] = ring[i];
        d += (i ? 'L' : 'M') + sx(x).toFixed(1) + ' ' + sy(y).toFixed(1);
      }
      d += 'Z';
    }
    return d;
  }

  /* Theme-aware neutrals. The green ramp and the team fills stay exactly as
     measured -- they carry the dE-15 separation guarantee and brand accuracy --
     but "no projection" and "out of market" have to follow the surface or the
     map glows white on a dark page. Read from CSS so there is one source of
     truth for the palette. */
  function themeVar(name, fallback) {
    if (typeof getComputedStyle !== 'function') return fallback;
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function neutralNoData() { return themeVar('--map-nodata', '#f0f4f8'); }
  function neutralOut() { return themeVar('--map-out', '#c9d1dd'); }
  function hairline() { return themeVar('--map-surface', '#ffffff'); }

  /* Sequential green ramp: intensity reads as likelihood rather than hue as
     category. Dark mode gets its OWN steps rather than reusing the light ones --
     a ramp tuned for a white surface inverts perceptually on a dark one (its
     lightest step, the LOWEST probability, becomes the most prominent thing on
     the map). Both ramps are validated for monotonic OKLab lightness and >=8 dE
     between adjacent steps, and the faintest step is checked against its own
     surface and against the two neutrals it borders. */
  const RAMP_LIGHT = ['#1d7f45', '#3fa268', '#86c79b', '#c9e3d2'];
  const RAMP_DARK  = ['#7fe0a5', '#4cbd78', '#2f8a55', '#1d5a3a'];
  function ramp() {
    const dark = themeVar('--map-ramp-dark', '0') === '1';
    return dark ? RAMP_DARK : RAMP_LIGHT;
  }
  function fillFor(p, isConfirmed) {
    const R = ramp();
    if (p == null) return neutralNoData();
    if (isConfirmed) return p > 0 ? R[0] : neutralOut();
    if (p >= 0.85) return R[0];
    if (p >= 0.60) return R[1];
    if (p >= 0.35) return R[2];
    if (p >= 0.15) return R[3];
    if (p >= 0.05) return neutralNoData();
    return neutralOut();
  }

  /* Render the map for one game.
     Returns SVG markup, or '' when the game has no market-dependent coverage. */
  function render(game, allGames, opts) {
    opts = opts || {};
    const built = buildPaths();
    const C2 = window.NFL_LOCAL_COVERAGE;
    if (!built || !C2) return '';

    const width = opts.width || 560;
    const [minX, minY, maxX, maxY] = built.bounds;
    // Uniform scale so the projection's own aspect ratio is preserved; forcing
    // a fixed height ratio squashes the map. In the projection y increases
    // northward, so sy flips it for SVG's downward y axis.
    const scale = width / (maxX - minX);
    const height = Math.ceil((maxY - minY) * scale);
    const sx = x => (x - minX) * scale;
    const sy = y => (maxY - y) * scale;

    let confirmed = false;
    const parts = [];
    let localCount = 0, total = 0;

    for (const shape of built.shapes) {
      const r = C2.resolve(shape.code, game, allGames);
      let p = null;
      if (r) {
        total++;
        confirmed = confirmed || r.source === 'actual' || r.source === 'certain'
          || r.source === 'national';
        p = (r.source === 'actual' || r.source === 'certain' || r.source === 'national')
          ? (r.isLocal ? 1 : 0)
          : (r.ownProbability != null ? r.ownProbability : 0);
        if (p >= 0.5) localCount++;
      }
      const name = built.names[shape.code] || ('DMA ' + shape.code);
      const pctLabel = p == null ? 'no projection'
        : (confirmed && (p === 1 || p === 0)) ? (p ? 'local' : 'out of market')
        : Math.round(p * 100) + '%';
      parts.push(
        `<path d="${toPathData(shape.rings, sx, sy)}" fill="${fillFor(p, confirmed)}" ` +
        `stroke="${hairline()}" stroke-width="0.4" ` +
        `><title>${name} — ${pctLabel}</title></path>`
      );
    }
    if (!total) return '';

    return {
      svg: `<svg class="cov-map-svg" viewBox="0 0 ${Math.ceil(width)} ${height}" ` +
           `role="img" aria-label="Projected local coverage by television market">` +
           parts.join('') + `</svg>`,
      confirmed,
      marketsLocal: localCount,
      marketsTotal: total
    };
  }

  // ---- winner map: which competing game leads in each market ----

  /* Winner-map fills are derived from the HOME team's primary color, so a
     region reads as "the team whose home game airs here".

     Raw primaries cannot be used directly. Measured across the 2026 schedule,
     48% of multi-game slates contain a home-color pair below the ΔE 15
     readability floor, and some are the same hex (NE and SEA are both
     #002244; DAL and LAR both #003594). 18 of 32 primaries are also too dark
     to work as large fills.

     So each fill keeps the team's hue and chroma but is re-lightened into a
     readable band, and within a slate colliding teams are pushed onto
     different lightness steps. That separates 94% of slates outright; the
     remainder (e.g. Atlanta red beside Tampa Bay red) fall back to the same
     hatch channel used for toss-ups, so identity is never carried by a color
     pair the eye cannot split. */
  const FILL_LIGHTNESS_STEPS = [0.62, 0.50, 0.74, 0.42, 0.82];
  const FILL_CHROMA_BOOST = 1.15;
  const MIN_FILL_SEPARATION = 15;   // OKLab ΔE ×100, the normal-vision floor
  // "No projection" is resolved per render via neutralNoData() so a theme
  // switch is picked up.

  /* Fill source overrides. A team's listed primary is used unless it cannot
     work as a large fill, in which case the team's own secondary is used so the
     region still reads as that team:
       LV  #000000 is achromatic — lightening it yields flat grey, which would
           be confused with "no projection". Raiders silver is the alternative,
           still too neutral, so their secondary identity color is used.
       PIT #FFB612 gold is very light and high-chroma; lightening it into the
           band drifts the hue ~16° toward brown. Steelers black is likewise
           achromatic, so gold is kept but pinned to a light step instead
           (handled by PREFERRED_LIGHTNESS below).
       NO  #D3BC8D is a pale gold that darkens to khaki; keep it light. */
  const FILL_COLOR_OVERRIDE = {
    LV: '#A5ACAF'   // Raiders silver, given a chroma floor below
  };
  // Teams whose brand color only reads correctly at a specific lightness.
  const PREFERRED_LIGHTNESS = { PIT: 0.82, NO: 0.82, LV: 0.50 };
  // Below this OKLab chroma a fill reads as grey; nudge it toward a hue so it
  // is never confused with the "no projection" neutral.
  const MIN_FILL_CHROMA = 0.045;

  // --- OKLab helpers: hue-preserving lightness control and ΔE ---
  function hexToRgb(h) {
    h = String(h).replace('#', '');
    return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
  }
  function toLinear(c) { return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
  function fromLinear(c) { return c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055; }
  function rgbToOklab(rgb) {
    const [r, g, b] = rgb.map(toLinear);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s];
  }
  function oklabToHex(L, a, b) {
    const l = Math.pow(L + 0.3963377774 * a + 0.2158037573 * b, 3);
    const m = Math.pow(L - 0.1055613458 * a - 0.0638541728 * b, 3);
    const s = Math.pow(L - 0.0894841775 * a - 1.2914855480 * b, 3);
    const rgb = [
      4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    ].map(v => Math.max(0, Math.min(1, fromLinear(v))));
    return '#' + rgb.map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
  }
  function teamFill(hex, lightness) {
    const o = rgbToOklab(hexToRgb(hex));
    let a = o[1] * FILL_CHROMA_BOOST, b = o[2] * FILL_CHROMA_BOOST;
    // Keep a minimum chroma so a near-black or near-grey brand color does not
    // land on the same neutral used for "no projection".
    const chroma = Math.hypot(a, b);
    if (chroma < MIN_FILL_CHROMA) {
      if (chroma < 1e-6) { a = 0; b = -MIN_FILL_CHROMA; }   // steer to slate blue
      else { const k = MIN_FILL_CHROMA / chroma; a *= k; b *= k; }
    }
    return oklabToHex(lightness, a, b);
  }
  function colorDistance(a, b) {
    const A = rgbToOklab(hexToRgb(a)), B = rgbToOklab(hexToRgb(b));
    return 100 * Math.hypot(A[0] - B[0], A[1] - B[1], A[2] - B[2]);
  }

  /* Assign a readable fill per game, keyed by home team.
     Returns { colorOf: Map(gameLabel -> hex), hatched: Set(gameLabel) } where
     `hatched` holds games whose color could not be separated and therefore
     also carry a diagonal texture. */
  function assignFills(field, labelOf, teamColors) {
    const colorOf = new Map();
    const hatched = new Set();
    const used = [];
    for (const g of field) {
      const base = FILL_COLOR_OVERRIDE[g.home] ||
        (teamColors && teamColors[g.home]) || '#7a8798';
      // Try the team's preferred step first so brand-critical colors (Steelers
      // gold, Saints old gold) keep their character, then the standard ladder.
      const steps = PREFERRED_LIGHTNESS[g.home] != null
        ? [PREFERRED_LIGHTNESS[g.home]].concat(FILL_LIGHTNESS_STEPS)
        : FILL_LIGHTNESS_STEPS;
      let chosen = null;
      for (const L of steps) {
        const cand = teamFill(base, L);
        if (used.every(u => colorDistance(cand, u) >= MIN_FILL_SEPARATION)) {
          chosen = cand;
          break;
        }
      }
      if (!chosen) {
        // No separable step: keep the team's own color and mark it for texture
        // so the pair is still distinguishable without relying on hue.
        chosen = teamFill(base, steps[0]);
        hatched.add(labelOf(g));
      }
      used.push(chosen);
      colorOf.set(labelOf(g), chosen);
    }
    return { colorOf, hatched };
  }

  function shortLabel(game) {
    // "Pittsburgh @ NY Jets" -> "Pittsburgh @ NY Jets" is already short enough
    // for a legend row; strip only the window marker.
    return String(game).replace(/\s*\((?:LATE|EARLY)\)\s*$/i, '');
  }

  /* Render a map colored by which game leads each market.
     Returns null when fewer than two games compete (nothing to compare). */
  function renderWinners(game, allGames, opts) {
    opts = opts || {};
    const built = buildPaths();
    const C2 = window.NFL_LOCAL_COVERAGE;
    if (!built || !C2) return null;

    const field = C2.competingGames(game, allGames);
    if (!field || field.length < 2) return null;

    const width = opts.width || 560;
    const [minX, minY, maxX, maxY] = built.bounds;
    const scale = width / (maxX - minX);
    const height = Math.ceil((maxY - minY) * scale);
    const sx = x => (x - minX) * scale;
    const sy = y => (maxY - y) * scale;

    // Labels must match what probabilities() returns exactly, so reuse the
    // coverage module's own naming rather than re-deriving it here.
    const labelOf = g => C2.gameLabel(g);
    const teamColors = opts.teamColors || {};

    // First pass: per-market probabilities, and how many markets each game leads.
    const rows = [];
    const leadCount = new Map();
    let confirmed = false;
    for (const shape of built.shapes) {
      const probs = C2.probabilities(shape.code, game, allGames);
      if (!probs || !probs.length) { rows.push(null); continue; }
      confirmed = confirmed || probs.confirmed;
      rows.push(probs);
      leadCount.set(probs[0].game, (leadCount.get(probs[0].game) || 0) + 1);
    }

    // Fills come from each game's HOME team, so every competing game gets its
    // own color — there is no "Other" bucket and no footprint-based ordering.
    const nameOf = new Map();
    for (const g of field) nameOf.set(g, labelOf(g));
    const { colorOf, hatched } = assignFills(field, g => nameOf.get(g), teamColors);

    // A market is a toss-up when the top two games are within this margin.
    const tossupMargin = opts.tossupMargin != null ? opts.tossupMargin : 0.12;

    const defs = [];
    const patternFor = new Map();   // "a|b" -> pattern id
    function tossupPattern(cA, cB) {
      const key = cA + '|' + cB;
      if (patternFor.has(key)) return patternFor.get(key);
      const id = 'tu' + patternFor.size;
      // 45-degree alternating bands of the two games' colors.
      defs.push(
        `<pattern id="${id}" patternUnits="userSpaceOnUse" width="7" height="7" ` +
        `patternTransform="rotate(45)"><rect width="7" height="7" fill="${cA}"/>` +
        `<rect width="3.5" height="7" fill="${cB}"/></pattern>`
      );
      patternFor.set(key, id);
      return id;
    }
    function hatchPattern(c) {
      const key = 'h' + c;
      if (patternFor.has(key)) return patternFor.get(key);
      const id = 'hx' + patternFor.size;
      // Tone-on-tone lines: separates a colliding pair without a new hue.
      const dark = (() => { const o = rgbToOklab(hexToRgb(c));
        return oklabToHex(Math.max(0.28, o[0] - 0.22), o[1], o[2]); })();
      defs.push(
        `<pattern id="${id}" patternUnits="userSpaceOnUse" width="6" height="6" ` +
        `patternTransform="rotate(135)"><rect width="6" height="6" fill="${c}"/>` +
        `<rect width="2" height="6" fill="${dark}"/></pattern>`
      );
      patternFor.set(key, id);
      return id;
    }

    const parts = [];
    let tossupCount = 0;
    built.shapes.forEach((shape, i) => {
      const probs = rows[i];
      const name = built.names[shape.code] || ('DMA ' + shape.code);
      let fill = neutralNoData(), tip = name + ' — no projection';
      if (probs && probs.length) {
        const top = probs[0];
        const base = colorOf.get(top.game) || '#7a8798';
        const second = probs[1];
        const isTossup = !probs.confirmed && second &&
          (top.p - second.p) < tossupMargin;
        if (isTossup) {
          tossupCount++;
          fill = `url(#${tossupPattern(base, colorOf.get(second.game) || '#7a8798')})`;
        } else if (hatched.has(top.game)) {
          fill = `url(#${hatchPattern(base)})`;
        } else {
          fill = base;
        }
        const lines = probs.map(p => '  ' + (probs.confirmed
          ? (p.p >= 0.5 ? '* ' : '  ') + shortLabel(p.game)
          : String(Math.round(p.p * 100)).padStart(3) + '%  ' + shortLabel(p.game)));
        tip = name + (isTossup ? '  (too close to call)' : '') + '\n' + lines.join('\n');
      }
      parts.push(
        `<path d="${toPathData(shape.rings, sx, sy)}" fill="${fill}" ` +
        `stroke="${hairline()}" stroke-width="0.4"><title>${tip.replace(/[<&]/g, '')}</title></path>`
      );
    });

    // Legend order follows footprint so the biggest region is listed first.
    const legend = [...field]
      .map(g => ({
        game: nameOf.get(g),
        home: g.home,
        markets: leadCount.get(nameOf.get(g)) || 0,
        color: colorOf.get(nameOf.get(g)),
        hatched: hatched.has(nameOf.get(g)),
        isHome: false
      }))
      .sort((a, b) => b.markets - a.markets);

    if (opts.homeDma) {
      const p = C2.probabilities(String(opts.homeDma), game, allGames);
      if (p && p.length) {
        const hg = p[0].game;
        const row = legend.find(l => l.game === hg);
        if (row) row.isHome = true;
      }
    }

    return {
      svg: `<svg class="cov-map-svg" viewBox="0 0 ${Math.ceil(width)} ${height}" ` +
           `role="img" aria-label="Most likely local game by television market">` +
           (defs.length ? `<defs>${defs.join('')}</defs>` : '') +
           parts.join('') + `</svg>`,
      legend,
      confirmed,
      fieldSize: field.length,
      tossupCount,
      hatchedCount: hatched.size
    };
  }

  window.NFL_COVERAGE_MAP = { render, renderWinners, fillFor };
})();
