/*
  Game-time localisation.

  The schedule stores every kickoff as an Eastern wall-clock string ("1:00 PM")
  plus an ET calendar date, because that is how the NFL publishes it. This
  module turns that into the viewer's own zone, derived from the ZIP they set.

  Two things make this less trivial than adding an offset:

  1. ET is itself a DST-observing zone, so "1:00 PM" on 2026-09-13 and on
     2026-12-13 are different UTC instants. We recover the true instant by
     probing what America/New_York actually reports, rather than assuming -4 or
     -5.
  2. The target zone may not observe DST at all (Phoenix, most of Arizona) or
     may sit on a non-hour offset. Intl handles both, so all arithmetic is
     delegated to it and nothing is hardcoded.

  Everything degrades to ET when no ZIP is set or the ZIP is unknown.
*/
(function () {
  'use strict';

  const ET = 'America/New_York';

  // Short zone labels the way a viewer expects to read them. Intl's own
  // timeZoneName:'short' already yields EDT/PDT/HST/AKDT etc. for US zones, so
  // it is used directly; this map only covers the handful where Intl returns a
  // GMT offset instead of an abbreviation.
  const FALLBACK_ABBR = {
    'America/Puerto_Rico': 'AST',
  };

  function table() {
    return (typeof window !== 'undefined' && window.NFL_ZIP_TZ) || null;
  }

  /* ZIP -> IANA zone. Exact 5-digit entries win over the 3-digit prefix, which
     is how the generated table stays small without losing the ZIPs that
     straddle a zone boundary (Indiana, Kentucky, the Dakotas). */
  function zoneForZip(zip) {
    const t = table();
    if (!t) return null;
    const z = String(zip == null ? '' : zip).trim();
    if (!/^\d{5}$/.test(z)) return null;
    if (Object.prototype.hasOwnProperty.call(t.exact, z)) return t.zones[t.exact[z]];
    const idx = t.prefix[z.slice(0, 3)];
    return idx === undefined ? null : t.zones[idx];
  }

  function parts(date, timeZone, opts) {
    const fmt = new Intl.DateTimeFormat('en-US', Object.assign({ timeZone }, opts));
    return fmt.formatToParts(date).reduce(function (acc, p) {
      acc[p.type] = p.value;
      return acc;
    }, {});
  }

  /* "1:00 PM" -> {h:13, m:0}. Returns null for TBD and anything unparseable so
     callers can leave such rows untouched rather than inventing a time. */
  function parseEtTime(time) {
    const m = /^\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*$/i.exec(String(time || ''));
    if (!m) return null;
    let h = Number(m[1]) % 12;
    if (/pm/i.test(m[3])) h += 12;
    return { h: h, m: Number(m[2]) };
  }

  /* The UTC instant of an ET wall-clock date+time.

     Date.UTC gives a first guess; we then ask what ET actually shows at that
     instant and correct by the difference. Two iterations settle it even when
     the guess lands on the far side of a DST transition. */
  function etInstant(dateStr, time) {
    const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr || ''));
    const t = parseEtTime(time);
    if (!d || !t) return null;
    const Y = Number(d[1]), M = Number(d[2]), D = Number(d[3]);
    const want = Date.UTC(Y, M - 1, D, t.h, t.m);
    let guess = want;
    for (let i = 0; i < 3; i++) {
      const p = parts(new Date(guess), ET, {
        hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
      });
      const got = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute);
      if (got === want) break;
      guess += want - got;
    }
    return new Date(guess);
  }

  function abbr(date, timeZone) {
    const p = parts(date, timeZone, { timeZoneName: 'short' });
    const name = p.timeZoneName || '';
    // Intl falls back to "GMT-4"-style names for a few zones; prefer a real
    // abbreviation when we have one.
    if (/^GMT/i.test(name) && FALLBACK_ABBR[timeZone]) return FALLBACK_ABBR[timeZone];
    return name;
  }

  /* Format an instant as "1:00 PM" in the given zone, matching the schedule's
     own style (no leading zero, uppercase meridiem). */
  function clock(date, timeZone) {
    const p = parts(date, timeZone, { hour: 'numeric', minute: '2-digit', hour12: true });
    return p.hour + ':' + p.minute + ' ' + String(p.dayPeriod || '').toUpperCase();
  }

  function isoDate(date, timeZone) {
    const p = parts(date, timeZone, {
      year: 'numeric', month: '2-digit', day: '2-digit',
    });
    return p.year + '-' + p.month + '-' + p.day;
  }

  /* The one entry point callers need.

     Returns, for a game and a target zone:
       time   "10:00 AM"          local wall clock (or the raw value if TBD)
       zone   "PDT"               abbreviation to print next to it
       label  "10:00 AM PDT"      the two joined, ready to render
       date   "2026-09-13"        the LOCAL calendar date
       shifted true/false         local date differs from the ET date
       tz     "America/Los_Angeles"
     When no zone is known, falls back to the stored ET values so the UI keeps
     working exactly as before. */
  function forGame(game, timeZone) {
    const rawTime = game && game.time ? String(game.time) : '';
    const rawDate = game && game.date ? String(game.date) : '';
    const tz = timeZone || null;
    const instant = etInstant(rawDate, rawTime);

    if (!tz || !instant) {
      // TBD times and unknown zones: keep the schedule's own ET wording.
      const known = !!instant;
      return {
        time: rawTime,
        zone: known ? abbr(instant, ET) : '',
        label: known ? rawTime + ' ' + abbr(instant, ET) : rawTime,
        date: rawDate,
        shifted: false,
        tz: known ? ET : null,
        isEt: true,
      };
    }
    const localTime = clock(instant, tz);
    const localZone = abbr(instant, tz);
    const localDate = isoDate(instant, tz);
    return {
      time: localTime,
      zone: localZone,
      label: localTime + (localZone ? ' ' + localZone : ''),
      date: localDate,
      shifted: localDate !== rawDate,
      tz: tz,
      isEt: tz === ET,
    };
  }

  window.NFL_LOCAL_TIME = {
    zoneForZip: zoneForZip,
    forGame: forGame,
    etInstant: etInstant,
    parseEtTime: parseEtTime,
    abbr: abbr,
    ET: ET,
  };
})();
