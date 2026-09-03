# Team logos

Drop one file per team here, named with the **lowercase team code**:

```
logos/ari.svg  logos/atl.svg  logos/bal.svg  logos/buf.svg
logos/car.svg  logos/chi.svg  logos/cin.svg  logos/cle.svg
logos/dal.svg  logos/den.svg  logos/det.svg  logos/gb.svg
logos/hou.svg  logos/ind.svg  logos/jax.svg  logos/kc.svg
logos/lv.svg   logos/lac.svg  logos/lar.svg  logos/mia.svg
logos/min.svg  logos/ne.svg   logos/no.svg   logos/nyg.svg
logos/nyj.svg  logos/phi.svg  logos/pit.svg  logos/sf.svg
logos/sea.svg  logos/tb.svg   logos/ten.svg  logos/was.svg
```

The app uses these as a faint watermark in the hero banner: one logo in team
view, the division's four in a 2x2 block in division view.

## Notes

- **Format.** SVG is preferred (it scales cleanly at the sizes used here). To
  use PNGs instead, change `LOGO_EXT` in `index.html` from `'svg'` to `'png'`.
- **Missing files are safe.** Any logo that fails to load hides itself, so the
  banner looks exactly as it did before rather than showing a broken image.
  You can add them a few at a time.
- **Transparent background.** The hero is a dark gradient, so logos need
  transparency; a white or coloured rectangle will show as a visible block.
- **Contrast.** Marks are drawn at 13-15% opacity. Very dark logos (e.g. the
  Raiders' black) may be nearly invisible against the dark hero — a white or
  light monochrome variant reads better if you have one.
- The codes match `TEAM_NAMES` / `TEAM_COLORS` in `index.html`.
