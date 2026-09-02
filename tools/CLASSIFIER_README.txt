NFL DMA polygon classifier
==========================

Files
-----
classify_dma_maps.py       map -> research CSV (full provenance, ~22 MB)
build_web_dataset.py       research CSV -> compact JSON for the website
build_prediction_model.py  research CSV -> prediction model for future weeks
build_map_geometry.py      nielsentopo.json -> DMA outlines for the site map
legend_override_template.csv

Coverage map geometry
---------------------
  py .\tools\build_map_geometry.py --js .\dma-geometry.js

  672 KB source -> 140 KB minified -> 49 KB gzipped (206 markets)

Stays TopoJSON: adjacent DMAs share borders and TopoJSON stores each shared
border once, so converting to GeoJSON roughly triples the payload. Geometry
simplification was measured and rejected -- an aggressive tolerance saved only
~9 KB gzipped while damaging coastlines.

coverage-map.js renders it to SVG with no mapping library: it decodes the
TopoJSON arcs, projects with an Albers conic (standard parallels 29.5N/45.5N),
and emits one path per market shaded by probability. Two gotchas worth keeping
in mind if that file is edited:
  * the projection's y axis increases northward while SVG's increases
    downward, so y must be flipped;
  * scale x and y uniformly, or the country comes out squashed.
Alaska and Hawaii (DMAs 743/744/745 and 725) are excluded rather than inset,
since a correct inset needs its own projection and frame.

PROVENANCE: these are Nielsen-derived boundaries redistributed by a public
repo. Acceptable for research and this prototype; review licensing before
treating the shapes as production data.

Winner map: home-team colors
----------------------------
The second map (which competing game leads each market) colors each region by
the HOME TEAM of the game projected there, so a region reads as "the team whose
home game airs here". Every competing game gets its own color; there is no
"Other" bucket.

Raw team primaries cannot be used directly. Measured across the 2026 schedule:
  * 48% of multi-game slates contain a home-color pair below the dE 15
    normal-vision readability floor;
  * some are the SAME hex - NE and SEA are both #002244, DAL and LAR both
    #003594 - so those regions would be literally indistinguishable;
  * 18 of 32 primaries are too dark (OKLab L < 0.43) to work as large fills.

Each fill therefore keeps the team's hue and chroma but is re-lightened, and
within a slate colliding teams are pushed onto different lightness steps
(0.62 / 0.50 / 0.74 / 0.42 / 0.82). That separates 94% of slates outright.
Hue is preserved within 0.2 degrees for 30 of 32 teams. Three exceptions are
handled explicitly:
  * LV  #000000 is achromatic; lightening yields flat grey that would read as
    "no projection". Overridden to Raiders silver plus a chroma floor.
  * PIT #FFB612 and NO #D3BC8D are pale, high-chroma golds that drift ~16
    degrees toward brown if darkened into the band, so they are pinned to a
    light step (L 0.82) instead.
  * any near-grey brand color gets a minimum chroma so no fill collides with
    the neutral used for markets with no projection.

Where two teams still cannot be separated (e.g. Atlanta red beside Tampa Bay
red), the smaller-footprint game is filled with a tone-on-tone diagonal hatch,
so identity never rests on a color pair the eye cannot split. Verified across
all 54 multi-game slates: zero legend pairs below dE 15 with neither hatched.

Toss-up markets (top two games within 12 percentage points, ~9% of market
cells) are filled with 45-degree stripes alternating the two games' colors, and
the tooltip appends "(too close to call)".

Accepted trade-off: Steelers/Saints gold sits above the validator's lightness
band (L 0.81 vs 0.77) and below 3:1 contrast. That is inherent to the brand
color - pulling it into the band is exactly what produced the brown that made
it unrecognizable. The always-present legend, which lists every game with its
market count, is the relief channel that trade requires. The checks that
actually protect readability still pass on the rendered fills: worst all-pairs
CVD dE 18.6, normal-vision dE 19.4.

Prediction model
----------------
For weeks whose real 506 maps are not published yet. Actual coverage must
always take precedence once available; this is a prior, not an override.

  py .\tools\build_prediction_model.py --validate
  py .\tools\build_prediction_model.py --output .\data\dma-model.json

Core statistic is a per-(DMA, team) WIN RATE -- times a team's game was the
local broadcast divided by times it was available -- recency weighted 1,2,4,8
by season. A game scores 0.75*max(team rate) + 0.25*mean(team rate); a softmax
over the competing games gives probabilities, and a pooled leave-one-season-out
table remaps the top probability so reported confidence is roughly honest.

Holdout (train 2021-24, test 2025, 10,918 decisions):
  top-1 73.4%   top-2 88.8%
  by competing games: 2 -> 87%, 3 -> 70%, 4 -> 61%, 5 -> 72%, 6 -> 69%
  mean |reported - observed| confidence error: 0.071

Because top-1 is only ~73% while top-2 is ~89%, consumers should hedge when
the top two probabilities are close rather than assert a single game.

Features tested and REJECTED (none are in the model):
  * team head-to-head, 67.6% alone vs 73.4% for win rate, and blending it in
    degraded accuracy at every weight tried. It double counts general team
    draw, which win rate already measures, and adds noise. Not a sparsity
    issue (median ~288 observations per decision).
  * exact game-pair head-to-head: 1,589 of 1,600 observed competing pairs
    occur exactly once, so it cannot inform an unseen future matchup.
  * geographic proximity: 57.4% alone, no lift when added. Per-DMA win rate
    has already absorbed geography.
  * division/conference, field-relative rescaling: no measurable change.

Known limitation: predicting a 2026 week also requires knowing which games CBS
and FOX put in which window, which is itself unpublished in advance. Early
season predictions therefore carry slot-assumption error on top of model error.

Publishing to GitHub Pages
--------------------------
The research CSV is gitignored: it is large and regenerable. Build the
compact file the browser actually loads with:

  py .\tools\build_web_dataset.py --js .\dma-assignments.js

  22.25 MB CSV -> 0.206 MB JSON (0.93%) -> 0.036 MB gzipped

Reductions, largest effect first: diagnostic columns dropped; games stored
once per map with a default index and only differing DMAs listed (~65% of
rows collapse into the default); game strings interned; then gzip. The
build refuses to write unless reconstructing every row from the compact
form reproduces the CSV exactly.

Compact schema (schema: 1):
  maps["<season>|<week>|<SLOT>"] = {
    g: [game strings, most common first],
    d: default game index (always 0),
    e: { "<dma_code>": game_index }   # only DMAs differing from the default
    u: { "<dma_code>": confidence }   # low-confidence markets, so the UI can
  }                                   # hedge instead of asserting
  dma_names["<dma_code>"] = display name

Unresolved colors are omitted rather than guessed; they stay in the research
CSV and legend_review.csv for review.

Required Python packages
------------------------
py -m pip install pillow shapely beautifulsoup4 requests truststore

truststore is optional but strongly recommended on a corporate network: it
makes certificate validation use the Windows trust store, which is what allows
the 506 legend fetch to succeed without --insecure-tls.

DMA boundary file
-----------------
Use the public simzou/nielsen-dma nielsentopo.json file. It is TopoJSON in
longitude/latitude and is preferred over nielsen-mkt-map_simplified.json,
whose polygon coordinates are already projected.

Put nielsentopo.json in the same working directory as the script (or provide a
full path with --dma-boundaries).

Recommended first test: one week
--------------------------------
py .\classify_dma_maps.py `
  --manifest .\map_archive\manifest.csv `
  --map-archive .\map_archive `
  --dma-boundaries .\nielsentopo.json `
  --season 2025 `
  --week 1 `
  --output .\week1_dma_assignments.csv `
  --diagnostics .\week1_map_diagnostics.csv `
  --legend-review .\week1_legend_review.csv `
  --summary .\week1_classifier_summary.json

Full archive
------------
py .\classify_dma_maps.py `
  --manifest .\map_archive\manifest.csv `
  --map-archive .\map_archive `
  --dma-boundaries .\nielsentopo.json `
  --output .\historical_dma_assignments.csv `
  --diagnostics .\map_diagnostics.csv `
  --legend-review .\legend_review.csv `
  --summary .\classifier_summary.json

Outputs
-------
historical_dma_assignments.csv
  One row per selected map / DMA. Includes normalized network/window, DMA code,
  DMA name, dominant coverage color, resolved game (when available), polygon
  sampling confidence, sample counts, map revision, source, and review flags.

map_diagnostics.csv
  One row per selected final map, including image dimensions and legend status.

legend_review.csv
  Only colors/maps where the script could classify the map but could not
  resolve color -> game from 506 HTML. Do not guess these. Add reviewed mappings
  to a copy of legend_override_template.csv and rerun with --legend-csv.

classifier_summary.json
  Run totals.

legend_cache.json
  Cached color->game legends so reruns do not repeatedly fetch the same pages.

Legend resolution (color -> matchup)
-----------------------------------
506 weekly pages carry the matchup legend as structured markup, not as text
inside the map PNG:

  <div id='game'>
    <div id='square'><img src="nfl/swatches/1.png"></div>
    <div id='matchup'>Pittsburgh @ NY Jets</div>
    <div id='anncrs'>Ian Eagle, J.J. Watt</div>
  </div>

Each game div is attributed to the nearest preceding network/window heading
("CBS EARLY", "CBS LATE", "FOX SINGLE"), which tolerates the ad and Patreon
blocks that appear between sections.

Swatch number -> color is resolved ONCE per run from the static site-wide
swatch PNGs (https://506sports.com/nfl/swatches/<n>.png), then reused for every
week. Verified dominant fills, which match the map fills exactly:

  1 red (255,129,129)      5 orange     (255,193,131)
  2 blue (131,172,255)     6 light_blue (131,255,255)
  3 green (158,229,158)    7 purple     (231,193,228)
  4 yellow (255,255,130)   8 brown      (200,161,140)

If the swatch PNGs cannot be fetched, the built-in verified table above is used
and the fallback is logged. Pages also embed an authoring color key as an HTML
comment (red: #FF7F7F, ...); it is stored in legend_cache.json for auditing but
is NOT used to match pixels, because those hex values are lighter than the
rendered PNG fills.

Brown and orange share a hue band and are separated by saturation, so a map
using both does not silently merge two games into one color.

On a single-header map, a matchup may be suffixed "(LATE)". That marker is
preserved and surfaced in the effective_window column, since it changes whether
a market's game is local in the early or late window.

TLS on corporate networks
-------------------------
Preference order:
  1. REQUESTS_CA_BUNDLE / SSL_CERT_FILE, if set and present.
  2. `truststore` (pip install truststore), which validates against the OS
     trust store and so sees corporate root CAs that certifi does not ship.
     This is the recommended fix on a TLS-inspecting network.
  3. certifi defaults.

--insecure-tls disables verification entirely. It is a last resort for research
runs; the choice is recorded as tls_mode=insecure_unverified in
legend_cache.json so anything collected that way stays auditable. Prefer
installing truststore instead.

--refresh-legends ignores cached entries and re-fetches. Cache entries that
previously failed are always retried automatically.

Notes
-----
* The script does not trust the old manifest slot column. It derives CBS/FOX and
  Early/Late/Single from the original image_url filename.
* It chooses one final/highest map revision per season/week/network/window.
* Full DMA polygons are sampled, not just market-center points.
* A color whose legend text is contradictory is marked legend_status=ambiguous
  and flagged for review rather than resolved to a guess.
* 506 maps are unofficial and can be revised; provenance is retained in output.

Validation status (2025 Week 1)
-------------------------------
resolved_games 618/618, legend_review 0, review_flag rows 2.
Matchup-level agreement with the markets named in the page's own UPDATES
blocks: 26/26. This includes Nashville and Kansas City, which disagreed with
the older secondary 41-market table; the live final map supports the
classifier, so those were map-revision differences rather than errors.
The 2 flagged rows (Spokane WA, Lafayette IN) are genuine low-confidence
boundary markets and are both correctly classified.
