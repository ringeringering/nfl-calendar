#!/usr/bin/env python3
"""Fetch weekly NFL fantasy projections and write fantasy-data.js.

Source
------
ESPN's public fantasy endpoint, which needs no API key:

  https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/<year>
      /segments/0/leaguedefaults/3?scoringPeriodId=<week>&view=kona_player_info

`leaguedefaults/3` is ESPN's standard PPR scoring profile, so `appliedTotal`
comes back already scored rather than as raw stat lines. Within each player's
`stats` array, `statSourceId` 1 is a projection and 0 is actual production; only
projections are used here.

This endpoint is undocumented. It is widely used and stable in practice, but it
can change without notice, so this script fails loudly and leaves the previous
fantasy-data.js in place rather than overwriting it with something empty. A
partially successful run (some weeks fetched) is still written, with the failed
weeks recorded in the payload.

Output
------
fantasy-data.js, matching the contract index.html already expects:

  window.NFL_FANTASY_DATA = {
    fetched_at, scoring, source, season, weeks, errors,
    players: [{week, team, name, position, projected_points,
               position_rank, injury_status}]
  }

Positional rank is computed here, not fetched: players are ranked by projected
points within (week, position), so "WR4" means 4th among WRs that week.

Usage
-----
  python scripts/fetch_fantasy.py                 # current season, all weeks
  python scripts/fetch_fantasy.py --weeks 1 2 3   # specific weeks
  python scripts/fetch_fantasy.py --season 2026
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
UA = "nfl-streaming-calendar/1.0 (+fantasy projections)"

# ESPN defaultPositionId -> label. D/ST (16) is included because the panel
# renders a fixed starting-lineup grid with a DEF row.
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# ESPN abbreviation -> the code used by index.html's GAMES array. Team IDs are
# read from ESPN at runtime; only genuine spelling differences live here.
TEAM_ALIAS = {"WSH": "WAS"}

# ESPN injury enum -> a short label for the UI. Anything not listed passes
# through title-cased so a new ESPN value is still shown rather than dropped.
INJURY_LABELS = {
    "ACTIVE": "Healthy",
    "NORMAL": "Healthy",
    "QUESTIONABLE": "Questionable",
    "DOUBTFUL": "Doubtful",
    "OUT": "Out",
    "INJURY_RESERVE": "IR",
    "SUSPENSION": "Suspended",
    "DAY_TO_DAY": "Day-to-day",
    "PROBABLE": "Probable",
}

# How many players to ask ESPN for per week, ordered by ownership. Ranks are
# computed from this pool, so it is deliberately wider than what ships.
# 900 is needed to fill every lineup slot for every team: at 400 the tail was
# truncated and 16% of team-weeks had fewer than 3 WRs, leaving empty slots.
PLAYER_LIMIT = 900

# The panel renders 9 starting slots (QB RB RB WR WR WR TE K DEF) plus a bench
# of whoever is left. ESPN projects a median of ~15 players per team per week
# and no team has more than 17, so 16 captures essentially all real depth;
# beyond that projections fall below 1 point and are noise.
# Ranks are still computed against the full request pool above, so a published
# "WR14" keeps its league-wide meaning.
KEEP_PER_TEAM_PER_WEEK = 16

# Minimum players to keep per position per team-week, so every lineup slot can
# be filled. One extra RB/WR/TE beyond the starters feeds the FLEX row.
MIN_DEPTH = {"QB": 2, "RB": 3, "WR": 4, "TE": 2, "K": 1, "DEF": 1}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get_json(url: str, fantasy_filter: dict | None = None, timeout: int = 45) -> Any:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if fantasy_filter is not None:
        headers["x-fantasy-filter"] = json.dumps(fantasy_filter)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_team_map(season: int) -> dict[int, str]:
    """ESPN proTeam id -> our team code, fetched rather than hardcoded."""
    data = get_json(f"{BASE}/{season}?view=proTeamSchedules_wl")
    teams = (data.get("settings") or {}).get("proTeams") or data.get("proTeams") or []
    out: dict[int, str] = {}
    for t in teams:
        tid, abbr = t.get("id"), t.get("abbrev")
        if not tid or not abbr or abbr == "FA":
            continue
        out[int(tid)] = TEAM_ALIAS.get(abbr, abbr)
    if len(out) < 32:
        raise RuntimeError(f"expected 32 pro teams, got {len(out)}")
    return out


def fetch_week(season: int, week: int, team_map: dict[int, str]) -> list[dict[str, Any]]:
    url = (f"{BASE}/{season}/segments/0/leaguedefaults/3"
           f"?scoringPeriodId={week}&view=kona_player_info")
    flt = {"players": {
        "limit": PLAYER_LIMIT,
        "filterStatsForCurrentSeasonScoringPeriodId": {"value": [week]},
        "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
    }}
    data = get_json(url, flt)
    entries = data.get("players") or []
    if not entries:
        raise RuntimeError("no players returned")

    rows: list[dict[str, Any]] = []
    for entry in entries:
        p = entry.get("player") or entry
        pos = POSITIONS.get(p.get("defaultPositionId"))
        team = team_map.get(p.get("proTeamId") or -1)
        if not pos or not team:
            continue  # unmapped position (e.g. D/ST) or free agent

        proj = None
        for s in p.get("stats") or []:
            if s.get("statSourceId") == 1 and s.get("scoringPeriodId") == week:
                proj = s.get("appliedTotal")
                break
        if proj is None:
            continue  # no projection for this week (bye, or not projected)

        raw_injury = (p.get("injuryStatus") or "ACTIVE").upper()
        rows.append({
            "week": week,
            "team": team,
            "name": p.get("fullName") or "Unknown player",
            "position": pos,
            "projected_points": round(float(proj), 1),
            "injury_status": INJURY_LABELS.get(
                raw_injury, raw_injury.replace("_", " ").title()),
        })
    return rows


def add_position_ranks(rows: list[dict[str, Any]]) -> None:
    """Rank by projected points within (week, position): WR1, WR2, ..."""
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[(r["week"], r["position"])].append(r)
    for (_week, pos), group in buckets.items():
        group.sort(key=lambda r: -r["projected_points"])
        for i, r in enumerate(group, start=1):
            r["position_rank"] = f"{pos}{i}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, default=None,
                    help="season year (default: inferred from today)")
    ap.add_argument("--weeks", type=int, nargs="*", default=None,
                    help="weeks to fetch (default: 1-18)")
    ap.add_argument("--output", type=Path, default=Path("fantasy-data.js"))
    ap.add_argument("--json-output", type=Path, default=None,
                    help="also write the raw payload as JSON")
    ap.add_argument("--sleep", type=float, default=0.4,
                    help="seconds between week requests")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    # The NFL season spans a calendar boundary: Jan-Jun belongs to the prior year.
    season = args.season or (now.year if now.month >= 7 else now.year - 1)
    weeks = args.weeks if args.weeks else list(range(1, 19))

    log(f"season {season}, weeks {weeks[0]}-{weeks[-1]}")
    team_map = load_team_map(season)
    log(f"team map: {len(team_map)} pro teams")

    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for wk in weeks:
        try:
            got = fetch_week(season, wk, team_map)
            rows.extend(got)
            log(f"  week {wk:2d}: {len(got)} players")
        except (urllib.error.URLError, urllib.error.HTTPError,
                RuntimeError, ValueError) as e:
            errors[str(wk)] = f"{type(e).__name__}: {str(e)[:160]}"
            log(f"  week {wk:2d}: FAILED {errors[str(wk)]}")
        if args.sleep:
            time.sleep(args.sleep)

    if not rows:
        # Never clobber a good file with an empty one; a total failure almost
        # certainly means the undocumented endpoint changed shape.
        raise SystemExit(
            "No projections fetched for any week — refusing to write. "
            "The ESPN endpoint may have changed. Existing fantasy-data.js kept.\n"
            + "\n".join(f"  week {k}: {v}" for k, v in errors.items()))

    # Rank against the full pool first so published ranks stay league-wide.
    add_position_ranks(rows)

    # A player on a bye has a 0.0 projection; those rows would render as
    # "0.0 proj pts" beside real ones, which reads as a data error rather than
    # a bye. Drop them and let the panel's own empty state handle it.
    before = len(rows)
    rows = [r for r in rows if r["projected_points"] > 0]
    byes = before - len(rows)

    # Then trim per team-week. A plain "top N by projection" cap can starve a
    # slot -- a team could ship 12 receivers and no kicker -- so keep a minimum
    # depth per position first, then fill the remainder by projection.
    kept: list[dict[str, Any]] = []
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[(r["week"], r["team"])].append(r)

    for _key, group in grouped.items():
        # A team on bye keeps a nonzero D/ST projection even though every
        # skill player zeroed out. Those team-weeks have no game to attach to,
        # so drop them rather than ship a lone DEF row.
        if not any(r["position"] != "DEF" for r in group):
            continue
        group.sort(key=lambda r: (-r["projected_points"], r["name"]))
        chosen: list[dict[str, Any]] = []
        picked: set[int] = set()
        taken: dict[str, int] = defaultdict(int)
        # Reserve the depth the lineup grid needs at each position.
        for r in group:
            if taken[r["position"]] < MIN_DEPTH.get(r["position"], 0):
                chosen.append(r)
                picked.add(id(r))
                taken[r["position"]] += 1
        # Then top up with the best remaining, for FLEX and spare depth.
        for r in group:
            if len(chosen) >= KEEP_PER_TEAM_PER_WEEK:
                break
            if id(r) not in picked:
                chosen.append(r)
                picked.add(id(r))
        kept.extend(chosen)

    log(f"kept {len(kept)} of {before} rows "
        f"({byes} bye-week zeros dropped, <={KEEP_PER_TEAM_PER_WEEK}/team/week "
        f"with per-position minimums)")
    rows = kept

    payload = {
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scoring": "PPR",
        "source": "ESPN Fantasy (projected)",
        "season": season,
        "weeks": sorted({r["week"] for r in rows}),
        "player_count": len(rows),
        "errors": errors,
        "players": rows,
    }

    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    args.output.write_text(
        "/* Generated by scripts/fetch_fantasy.py. Do not edit by hand. */\n"
        f"window.NFL_FANTASY_DATA={blob};\n", encoding="utf-8")
    if args.json_output:
        args.json_output.write_text(blob, encoding="utf-8")

    size = len(blob.encode("utf-8"))
    print(f"Wrote {args.output}")
    print(f"  players : {len(rows)}")
    print(f"  weeks   : {len(payload['weeks'])} of {len(weeks)} requested")
    print(f"  size    : {size/1024:.1f} KB")
    if errors:
        print(f"  WARNING : {len(errors)} week(s) failed: {sorted(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
