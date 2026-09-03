# Team logos

One file per team, named with the **lowercase team code** matching
`TEAM_NAMES` / `TEAM_COLORS` in `index.html`:

```
logos/ari.png  logos/atl.png  logos/bal.png  logos/buf.png
logos/car.png  logos/chi.png  logos/cin.png  logos/cle.png
logos/dal.png  logos/den.png  logos/det.png  logos/gb.png
logos/hou.png  logos/ind.png  logos/jax.png  logos/kc.png
logos/lv.png   logos/lac.png  logos/lar.png  logos/mia.png
logos/min.png  logos/ne.png   logos/no.png   logos/nyg.png
logos/nyj.png  logos/phi.png  logos/pit.png  logos/sf.png
logos/sea.png  logos/tb.png   logos/ten.png  logos/was.png
```

The app draws these as a faint watermark in the hero banner: one logo in team
view, the division's four in a row in division view.

## Refreshing them

```bash
python tools/fetch_team_logos.py --dark      # fill in anything missing
python tools/fetch_team_logos.py --dark --force   # re-download all 32
```

Source is ESPN's public team-logo endpoint. `--dark` requests the
dark-background variant, which matters here: the hero is a dark navy gradient,
and several teams' primary marks (Raiders black, Ravens purple, Bears navy) all
but disappear against it. The script falls back to the standard variant for any
team without a dark one.

Files land at 500px. Three teams (GB, LV, NYJ) come back at 4096px from ESPN and
were downscaled to 500px to match — re-running `--force` will re-fetch the large
originals, so downscale again if size matters to you.

## Notes

- **Format** is set by `LOGO_EXT` in `index.html` (currently `'png'`). It
  applies to all teams, so don't mix formats.
- **Missing files are safe.** The app checks once per team whether a logo
  exists and omits any that don't, so a partial set renders cleanly and emits
  no console errors.
- **Transparency is required** — all 32 current files were verified to have a
  real alpha channel. A logo with a solid background would show as a rectangle
  on the hero gradient.
- **Browser caching:** replacing a file you've already loaded may need a hard
  refresh (Ctrl+Shift+R).
- **Trademark:** these are NFL team marks fetched from ESPN. Fine for private
  use; if this repo is ever made public, consider replacing `!logos/*.png` in
  `.gitignore` with an ignore rule so each user fetches their own copy.
