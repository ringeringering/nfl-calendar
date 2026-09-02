#!/usr/bin/env python3
"""Compact the research DMA assignment CSV into a small file for the website.

The research CSV (tools/classify_dma_maps.py output) carries full provenance --
sample counts, SHA-256s, source URLs, local paths -- which makes it ~22 MB and
unsuitable for GitHub Pages. The browser only needs to answer one question:

    (season, week, network/window, DMA) -> which game aired locally?

Size comes down four ways, largest effect first:

1. Diagnostic columns are dropped. ~250 bytes per row of provenance is
   identical across all 206 DMAs of a single map and is not needed client-side.
   The research CSV remains the auditable source of truth (see CLASSIFIER_README).
2. Rows are not stored per DMA. Each map has only 2-7 distinct games, so games
   are listed once per map with a `default` index; only DMAs that differ from
   the default get an entry. ~65% of rows collapse into the default.
3. Game strings are interned per map and referenced by index.
4. Low-confidence markets keep their confidence so the UI can hedge rather
   than assert. These are ~2% of rows, so the cost is small and it preserves
   the "never silently guess" property of the pipeline.

Gzip (served automatically by Pages) then roughly halves the result again.

Usage
-----
py .\tools\build_web_dataset.py \
    --assignments .\data\historical_dma_assignments.csv \
    --output .\data\dma-assignments.json \
    --js .\dma-assignments.js
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Markets at or below this confidence are surfaced to the UI as uncertain.
# 0.70 matches the review_flag threshold used by the classifier.
CONFIDENCE_FLOOR = 0.70


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build(assignments: Path) -> dict[str, Any]:
    by_map: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    dma_names: dict[str, str] = {}
    skipped_no_game = 0

    with assignments.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("game"):
                # Unresolved colors stay out of the published file rather than
                # being guessed. They remain in the research CSV for review.
                skipped_no_game += 1
                continue
            by_map[(r["season"], r["week"], r["map_slot"])].append(r)
            if r.get("dma_code"):
                dma_names[r["dma_code"]] = r.get("dma_name", "")

    maps: dict[str, Any] = {}
    total_rows = 0
    default_rows = 0
    uncertain_rows = 0

    for (season, week, slot), rows in by_map.items():
        total_rows += len(rows)
        counts = Counter(r["game"] for r in rows)
        # Most frequent game first so it becomes the default and the exception
        # list stays as short as possible.
        games = [g for g, _ in counts.most_common()]
        index = {g: i for i, g in enumerate(games)}

        exceptions: dict[str, int] = {}
        uncertain: dict[str, float] = {}

        for r in rows:
            gi = index[r["game"]]
            if gi != 0:
                exceptions[r["dma_code"]] = gi
            else:
                default_rows += 1

            try:
                conf = float(r.get("confidence") or 0.0)
            except ValueError:
                conf = 0.0
            if conf < CONFIDENCE_FLOOR or r.get("review_flag") == "true":
                uncertain[r["dma_code"]] = round(conf, 2)
                uncertain_rows += 1

        # The "(LATE)"/"(EARLY)" marker a single-header map can carry is kept
        # inline in the game string, so no separate window map is needed.
        entry: dict[str, Any] = {"g": games, "d": 0}
        if exceptions:
            entry["e"] = exceptions
        if uncertain:
            entry["u"] = uncertain
        maps[f"{season}|{week}|{slot}"] = entry

    seasons = sorted({k.split("|")[0] for k in maps})
    payload = {
        "schema": 1,
        "generated_from": assignments.name,
        "seasons": seasons,
        "notes": (
            "Derived from 506 Sports weekly coverage maps (unofficial). "
            "'g' is the per-map game list, 'd' the default game index, "
            "'e' maps DMA code to a differing game index, and 'u' marks "
            "low-confidence markets with their confidence."
        ),
        "dma_names": dma_names,
        "maps": maps,
    }
    log(f"maps={len(maps)} dmas={len(dma_names)} rows={total_rows} "
        f"default={default_rows} exceptions={total_rows - default_rows} "
        f"uncertain={uncertain_rows} skipped_no_game={skipped_no_game}")
    return payload


def verify(payload: dict[str, Any], assignments: Path) -> int:
    """Reconstruct every game from the compact form and compare to the CSV."""
    maps = payload["maps"]
    mismatches = 0
    checked = 0
    with assignments.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("game"):
                continue
            m = maps.get(f'{r["season"]}|{r["week"]}|{r["map_slot"]}')
            if not m:
                mismatches += 1
                continue
            gi = m.get("e", {}).get(r["dma_code"], m["d"])
            if m["g"][gi] != r["game"]:
                mismatches += 1
            checked += 1
    log(f"verify: checked {checked} rows, {mismatches} mismatches")
    return mismatches


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--assignments", type=Path,
                   default=Path("data/historical_dma_assignments.csv"))
    p.add_argument("--output", type=Path, default=Path("data/dma-assignments.json"))
    p.add_argument("--js", type=Path, default=None,
                   help="also emit a window.NFL_DMA_DATA=... script for direct <script> use")
    args = p.parse_args()

    if not args.assignments.exists():
        p.error(f"Not found: {args.assignments}. Run tools/classify_dma_maps.py first.")

    payload = build(args.assignments)
    if verify(payload, args.assignments) != 0:
        raise SystemExit("Refusing to write: compact form is not lossless.")

    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(blob, encoding="utf-8")

    raw = len(blob.encode("utf-8"))
    gz = len(gzip.compress(blob.encode("utf-8"), 9))
    src = args.assignments.stat().st_size
    print(f"Wrote {args.output}")
    print(f"  source CSV : {src/1e6:8.2f} MB")
    print(f"  compact    : {raw/1e6:8.3f} MB  ({100*raw/src:.2f}% of source)")
    print(f"  gzipped    : {gz/1e6:8.3f} MB  (what Pages serves)")

    if args.js:
        args.js.write_text(
            "/* Generated by tools/build_web_dataset.py. Do not edit by hand. */\n"
            f"window.NFL_DMA_DATA={blob};\n", encoding="utf-8")
        print(f"Wrote {args.js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
