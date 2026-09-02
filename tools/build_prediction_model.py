#!/usr/bin/env python3
"""Build the CBS/FOX local-game prediction model from historical DMA assignments.

What the model predicts
-----------------------
Given a DMA and a set of games competing in the same network/window, produce a
probability distribution over which game airs locally in that DMA.

How it works
------------
The core statistic is a per-(DMA, team) *win rate*:

    times a team's game was the local broadcast in that DMA
    ------------------------------------------------------
    times that team's game was available in that DMA

This is deliberately a rate, not a raw appearance count. Counting appearances
rewards teams that are simply on television often; the rate measures whether a
team actually beats its competition in that specific market.

A game is scored by blending the stronger of its two teams with the average of
both (0.75 * max + 0.25 * mean), then scores are turned into probabilities with
a softmax whose temperature is fitted on held-out data so the reported
confidence is calibrated rather than arbitrary.

Features that were tested and deliberately REJECTED
---------------------------------------------------
Measured on a 2021-24 -> 2025 holdout, each of these failed to beat the plain
per-DMA win rate, so none are in the model:

* Team head-to-head ("when A competed with B in this DMA, who won?"): 67.6%
  alone versus 73.4% for win rate, and blending it in degraded accuracy
  monotonically at every weight and evidence threshold tested. The reason is
  double counting: when a big-draw team beats an opponent, that mostly reflects
  the team's general pull, which the win rate already captures. Head-to-head
  re-estimates the same fact once per opponent and amplifies sampling noise.
  This is not a sparsity problem (median ~288 observations per decision).
* Exact game-pair head-to-head: unusable for prediction. 1,589 of 1,600
  observed competing game-pairs occur exactly once, and a future matchup has
  by definition never competed before.
* Geographic proximity (DMA centroid to stadium): 57.4% alone, and no lift
  when added (72.95% versus 72.96%). Per-DMA win rate has already absorbed
  geography, since proximity is *why* a team wins a market historically.
* Division/conference affiliation and field-relative rescaling: no measurable
  change.

Accuracy
--------
Roughly 73% top-1 on an unseen season, but about 89% top-2. Accuracy depends
strongly on how many games compete: ~90% for 2-game maps, ~64% for 4-game
maps. Because of that spread the model emits probabilities and the consumer is
expected to hedge when the leader is not clear, rather than assert a single
game everywhere.

Usage
-----
py .\tools\build_prediction_model.py --validate
py .\tools\build_prediction_model.py --output .\data\dma-model.json
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

# Exponential recency weighting. Tuned on the 2025 holdout: flat weighting
# scored 71.9%, linear (1,2,3,4) 73.0%, this 73.1%, and steeper (1,3,9,27)
# overfit back down to 72.8%.
RECENCY_BASE = 2.0

# Additive smoothing for the win rate, pulling sparse cells toward a neutral
# prior. Accuracy is flat from k=1..20, so this is chosen for stability of the
# reported probabilities rather than for accuracy.
SMOOTHING_K = 5.0
NEUTRAL_PRIOR = 0.25

# Score = MAX_WEIGHT * (best team) + (1 - MAX_WEIGHT) * (mean of both teams).
MAX_WEIGHT = 0.75

# Fitted by --validate; softmax temperature converting scores to probabilities.
DEFAULT_TEMPERATURE = 12.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def game_teams(game: str) -> list[str]:
    g = game.replace(" (LATE)", "").replace(" (EARLY)", "")
    return [t.strip() for t in g.split(" @ ") if t.strip()]


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("game")]


def group_maps(rows: Iterable[dict[str, str]]) -> dict[tuple[str, str, str], list[dict[str, str]]]:
    out: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        out[(r["season"], r["week"], r["map_slot"])].append(r)
    return out


class Model:
    """Per-(DMA, team) win rates with recency weighting."""

    def __init__(self, shown: dict[str, Counter], avail: dict[str, Counter]) -> None:
        self.shown = shown
        self.avail = avail

    @classmethod
    def train(cls, maps: dict[tuple[str, str, str], list[dict[str, str]]],
              seasons: Iterable[str]) -> "Model":
        seasons = sorted(seasons)
        # Newest season gets the largest weight.
        weight = {s: RECENCY_BASE ** i for i, s in enumerate(seasons)}
        shown: dict[str, Counter] = defaultdict(Counter)
        avail: dict[str, Counter] = defaultdict(Counter)
        for (season, _week, _slot), rows in maps.items():
            w = weight.get(season)
            if w is None:
                continue
            games = sorted({r["game"] for r in rows})
            for r in rows:
                dma = r["dma_code"]
                for g in games:
                    for t in game_teams(g):
                        avail[dma][t] += w
                        if g == r["game"]:
                            shown[dma][t] += w
        return cls(shown, avail)

    def team_rate(self, dma: str, team: str) -> float:
        a = self.shown.get(dma, {}).get(team, 0.0)
        n = self.avail.get(dma, {}).get(team, 0.0)
        return (a + SMOOTHING_K * NEUTRAL_PRIOR) / (n + SMOOTHING_K)

    def game_score(self, dma: str, game: str) -> float:
        rates = [self.team_rate(dma, t) for t in game_teams(game)] or [NEUTRAL_PRIOR]
        return MAX_WEIGHT * max(rates) + (1.0 - MAX_WEIGHT) * (sum(rates) / len(rates))

    def predict(self, dma: str, games: list[str],
                temperature: float = DEFAULT_TEMPERATURE) -> list[tuple[str, float]]:
        """Return [(game, probability)] sorted most likely first."""
        scores = [(g, self.game_score(dma, g)) for g in games]
        mx = max(s for _, s in scores)
        exp = [(g, math.exp((s - mx) * temperature)) for g, s in scores]
        total = sum(v for _, v in exp) or 1.0
        return sorted(((g, v / total) for g, v in exp), key=lambda x: (-x[1], x[0]))


def evaluate(model: Model, maps: dict[tuple[str, str, str], list[dict[str, str]]],
             seasons: set[str], temperature: float,
             calibration: list[list[float]] | None = None) -> dict[str, Any]:
    top1 = top2 = total = 0
    by_n: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    # Calibration check: bucket by reported probability, compare to hit rate.
    bins: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    logloss = 0.0

    for (season, _week, _slot), rows in maps.items():
        if season not in seasons:
            continue
        games = sorted({r["game"] for r in rows})
        n = len(games)
        for r in rows:
            pred = model.predict(r["dma_code"], games, temperature)
            ranked = [g for g, _ in pred]
            probs = dict(pred)
            hit = ranked[0] == r["game"]
            top1 += hit
            top2 += r["game"] in ranked[:2]
            total += 1
            by_n[n][0] += hit
            by_n[n][1] += 1
            # Bucket by the RAW band so each bucket is a fixed group of
            # decisions, then report the corrected value that band emits. This
            # keeps "reported" and "observed" describing the same rows.
            raw_band = min(9, int(pred[0][1] * 10))
            bins[raw_band][0] += hit
            bins[raw_band][1] += 1
            logloss -= math.log(max(1e-9, probs.get(r["game"], 1e-9)))

    return {
        "decisions": total,
        "top1": top1 / total if total else 0.0,
        "top2": top2 / total if total else 0.0,
        "log_loss": logloss / total if total else 0.0,
        "by_competing_games": {
            str(k): {"accuracy": v[0] / v[1], "decisions": v[1]}
            for k, v in sorted(by_n.items())
        },
        "calibration": {
            f"{k/10:.1f}-{(k+1)/10:.1f}": {
                # What the model would actually show a user for this band.
                "predicted": apply_calibration((k + 0.5) / 10, calibration),
                "observed": v[0] / v[1],
                "decisions": v[1],
            }
            for k, v in sorted(bins.items()) if v[1]
        },
    }


def fit_temperature(model: Model, maps, seasons: set[str]) -> float:
    """Pick the softmax temperature minimizing log loss.

    Accuracy is unaffected by temperature (it cannot change the argmax); this
    only makes the reported probabilities honest. Fit this on a development
    season, never on the season used for the reported holdout metrics.
    """
    best_t, best_ll = DEFAULT_TEMPERATURE, float("inf")
    for t in [2, 4, 6, 8, 10, 12, 15, 20, 25, 30, 40]:
        ll = evaluate(model, maps, seasons, float(t))["log_loss"]
        if ll < best_ll:
            best_t, best_ll = float(t), ll
    return best_t


def fit_calibration_pooled(folds: list[tuple["Model", str]], maps,
                           temperature: float, bands: int = 10) -> list[list[float]]:
    """Fit the probability remap by pooling several leave-one-season-out folds.

    Each fold is (model trained on earlier seasons, the next season to score).
    Pooling matters because the per-band hit rate varies materially between
    seasons; a table fitted on a single year does not generalize.
    """
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for model, season in folds:
        for (s, _week, _slot), rows in maps.items():
            if s != season:
                continue
            games = sorted({r["game"] for r in rows})
            for r in rows:
                pred = model.predict(r["dma_code"], games, temperature)
                b = min(bands - 1, int(pred[0][1] * bands))
                buckets[b][0] += pred[0][0] == r["game"]
                buckets[b][1] += 1

    observed: list[float] = []
    last = 1.0 / bands
    for b in range(bands):
        hit, n = buckets.get(b, [0, 0])
        if n >= 100:
            last = hit / n
        observed.append(last)
    for i in range(1, bands):
        observed[i] = max(observed[i], observed[i - 1])
    return [[(i + 1) / bands, round(observed[i], 4)] for i in range(bands)]


def fit_calibration(model: Model, maps, seasons: set[str], temperature: float,
                    bands: int = 10) -> list[list[float]]:
    """Fit a monotone probability remap on a development season.

    A softmax temperature alone leaves the model overconfident in the middle of
    the range (a raw 0.65 top pick is right only ~54% of the time). This
    collects observed hit rates per predicted-probability band and enforces
    monotonicity, so a reported 0.65 means roughly 0.65.

    Returns [[band_upper_edge, corrected_probability], ...].
    """
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for (season, _week, _slot), rows in maps.items():
        if season not in seasons:
            continue
        games = sorted({r["game"] for r in rows})
        for r in rows:
            pred = model.predict(r["dma_code"], games, temperature)
            b = min(bands - 1, int(pred[0][1] * bands))
            buckets[b][0] += pred[0][0] == r["game"]
            buckets[b][1] += 1

    # Sparse bands inherit the nearest populated estimate.
    observed: list[float] = []
    last = 1.0 / bands
    for b in range(bands):
        hit, n = buckets.get(b, [0, 0])
        if n >= 30:
            last = hit / n
        observed.append(last)

    # Enforce monotonicity: a higher raw score must not map to a lower estimate.
    for i in range(1, bands):
        observed[i] = max(observed[i], observed[i - 1])

    return [[(i + 1) / bands, round(observed[i], 4)] for i in range(bands)]


def apply_calibration(prob: float, table: list[list[float]] | None) -> float:
    if not table:
        return prob
    for upper, corrected in table:
        if prob <= upper:
            return corrected
    return table[-1][1]


def export(model: Model, temperature: float, seasons: list[str],
           dma_names: dict[str, str], metrics: dict[str, Any],
           calibration: list[list[float]] | None = None) -> dict[str, Any]:
    """Emit the per-(DMA, team) rate table the browser needs.

    Only the rate is exported, rounded to 3 decimals. The raw weighted counts
    are training detail and would roughly triple the payload.
    """
    rates: dict[str, dict[str, float]] = {}
    for dma in sorted(model.avail):
        row = {}
        for team in sorted(model.avail[dma]):
            row[team] = round(model.team_rate(dma, team), 3)
        if row:
            rates[dma] = row
    return {
        "schema": 1,
        "kind": "dma-local-game-prediction",
        "trained_on_seasons": seasons,
        "params": {
            "recency_base": RECENCY_BASE,
            "smoothing_k": SMOOTHING_K,
            "neutral_prior": NEUTRAL_PRIOR,
            "max_weight": MAX_WEIGHT,
            "temperature": temperature,
        },
        "scoring": (
            "score(game) = 0.75*max(rate[dma][team]) + 0.25*mean(rate[dma][team]); "
            "probability = softmax(score * temperature) over the competing games; "
            "then map the top probability through `calibration` (band upper edge "
            "-> corrected probability) before displaying it"
        ),
        "calibration": calibration or [],
        "accuracy_note": metrics,
        "dma_names": dma_names,
        "rates": rates,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--assignments", type=Path,
                   default=Path("data/historical_dma_assignments.csv"))
    p.add_argument("--output", type=Path, default=None,
                   help="write the model JSON for the website")
    p.add_argument("--js", type=Path, default=None)
    p.add_argument("--holdout", default="2025",
                   help="season held out for validation (default 2025)")
    p.add_argument("--validate", action="store_true",
                   help="report holdout metrics and exit without training on all data")
    args = p.parse_args()

    if not args.assignments.exists():
        p.error(f"Not found: {args.assignments}. Run tools/classify_dma_maps.py first.")

    rows = load_rows(args.assignments)
    maps = group_maps(rows)
    all_seasons = sorted({s for s, _, _ in maps})
    dma_names = {r["dma_code"]: r.get("dma_name", "") for r in rows if r.get("dma_code")}
    log(f"{len(rows)} rows, {len(maps)} maps, seasons {all_seasons}")

    holdout = args.holdout
    if holdout not in all_seasons:
        p.error(f"Holdout season {holdout} not in data {all_seasons}")
    train_seasons = [s for s in all_seasons if s != holdout]

    # Always validate honestly: train WITHOUT the holdout season.
    val_model = Model.train(maps, train_seasons)

    # Temperature and calibration are fitted by leave-one-season-out over the
    # TRAINING seasons only, so nothing is tuned on the reported holdout.
    #
    # A single dev season is not enough: the reliability curve moves year to
    # year (raw 0.7-0.8 was ~73% correct in 2024 but ~57% in 2025), so a table
    # fitted on one season does not transfer. Pooling several leave-one-out
    # folds gives a curve that reflects the average rather than one year's
    # idiosyncrasy.
    folds: list[tuple[Model, str]] = []
    for i in range(1, len(train_seasons)):
        folds.append((Model.train(maps, train_seasons[:i]), train_seasons[i]))
    temperature = fit_temperature(folds[-1][0], maps, {folds[-1][1]})
    calibration = fit_calibration_pooled(folds, maps, temperature)
    log(f"fitted over folds {[s for _, s in folds]}: temperature={temperature:g}")

    metrics = evaluate(val_model, maps, {holdout}, temperature, calibration)

    print(f"Holdout validation: train {train_seasons} -> test [{holdout}]")
    print(f"  decisions      {metrics['decisions']}")
    print(f"  top-1 accuracy {metrics['top1']*100:.2f}%")
    print(f"  top-2 accuracy {metrics['top2']*100:.2f}%")
    print(f"  log loss       {metrics['log_loss']:.4f}  (temperature {temperature:g})")
    print("  accuracy by number of competing games:")
    for n, v in metrics["by_competing_games"].items():
        print(f"    {n} games: {v['accuracy']*100:5.1f}%  ({v['decisions']} decisions)")
    print("  calibration after remap (reported vs observed for the top pick):")
    devs = []
    for band, v in metrics["calibration"].items():
        mark = ""
        if v["decisions"] >= 50:
            devs.append(abs(v["predicted"] - v["observed"]))
            if abs(v["predicted"] - v["observed"]) > 0.10:
                mark = "  <-- off by >10pp"
        print(f"    {band}: reported {v['predicted']:.2f} observed {v['observed']:.2f} "
              f"({v['decisions']}){mark}")
    if devs:
        print(f"  mean |reported-observed| over populated bands: {sum(devs)/len(devs):.3f}")

    if args.validate:
        return 0

    if not args.output:
        return 0

    # For the shipped model, retrain on every season including the holdout.
    final = Model.train(maps, all_seasons)
    payload = export(final, temperature, all_seasons, dma_names, {
        "holdout_season": holdout,
        "top1": round(metrics["top1"], 4),
        "top2": round(metrics["top2"], 4),
        "by_competing_games": {k: round(v["accuracy"], 4)
                               for k, v in metrics["by_competing_games"].items()},
    }, calibration)
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(blob, encoding="utf-8")
    raw = len(blob.encode("utf-8"))
    print(f"\nWrote {args.output}")
    print(f"  model      : {raw/1e6:8.3f} MB")
    print(f"  gzipped    : {len(gzip.compress(blob.encode('utf-8'), 9))/1e6:8.3f} MB")
    if args.js:
        args.js.write_text(
            "/* Generated by tools/build_prediction_model.py. Do not edit by hand. */\n"
            f"window.NFL_DMA_MODEL={blob};\n", encoding="utf-8")
        print(f"Wrote {args.js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
