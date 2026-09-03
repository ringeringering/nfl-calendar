"""Fetch the 32 NFL team logos into logos/ for the hero-banner watermark.

Source is ESPN's public team-logo endpoint, the same host family the fantasy
data already comes from:

    https://a.espncdn.com/i/teamlogos/nfl/500/<code>.png

Files land as logos/<lowercase code>.png, matching what renderHeroLogos()
looks for in index.html. Note that index.html's LOGO_EXT must be 'png' for
these to be picked up.

Usage:
    python tools/fetch_team_logos.py              # fetch anything missing
    python tools/fetch_team_logos.py --force      # re-fetch everything
    python tools/fetch_team_logos.py --dark       # prefer the dark-background variant
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "logos")

# ESPN's own code for each team, keyed by the code this app uses. They agree
# for most teams; the exceptions are listed explicitly rather than guessed.
ESPN_CODE = {
    "ARI": "ari", "ATL": "atl", "BAL": "bal", "BUF": "buf",
    "CAR": "car", "CHI": "chi", "CIN": "cin", "CLE": "cle",
    "DAL": "dal", "DEN": "den", "DET": "det", "GB": "gb",
    "HOU": "hou", "IND": "ind", "JAX": "jax", "KC": "kc",
    "LV": "lv", "LAC": "lac", "LAR": "lar", "MIA": "mia",
    "MIN": "min", "NE": "ne", "NO": "no", "NYG": "nyg",
    "NYJ": "nyj", "PHI": "phi", "PIT": "pit", "SF": "sf",
    "SEA": "sea", "TB": "tb", "TEN": "ten",
    "WAS": "wsh",   # ESPN uses WSH where the schedule data uses WAS
}

BASE = "https://a.espncdn.com/i/teamlogos/nfl/500/{code}.png"
BASE_DARK = "https://a.espncdn.com/i/teamlogos/nfl/500-dark/{code}.png"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

try:  # corporate TLS interception: trust the OS store, as the other tools do
    import truststore
    truststore.inject_into_ssl()
except Exception as exc:  # pragma: no cover - environment dependent
    print(f"note: truststore unavailable ({exc}); using the default TLS chain")


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def is_png(blob: bytes) -> bool:
    """Guard against a CDN error page being saved as a .png."""
    return blob[:8] == b"\x89PNG\r\n\x1a\n"


def png_size(blob: bytes) -> tuple[int, int] | None:
    """Width/height from the IHDR chunk, so we can report what we got."""
    if not is_png(blob) or len(blob) < 24:
        return None
    return (int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download logos that are already present")
    ap.add_argument("--dark", action="store_true",
                    help="prefer ESPN's dark-background variant (lighter marks, "
                         "which read better on this app's dark hero)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    template = BASE_DARK if args.dark else BASE

    ok, skipped, failed = 0, 0, []
    for app_code, espn_code in sorted(ESPN_CODE.items()):
        dest = os.path.join(OUT_DIR, app_code.lower() + ".png")
        if os.path.exists(dest) and not args.force:
            print(f"  {app_code:4s} skip   already present")
            skipped += 1
            continue

        url = template.format(code=espn_code)
        try:
            blob = fetch(url)
        except urllib.error.HTTPError as exc:
            # The dark variant does not exist for every team; fall back.
            if args.dark and exc.code == 404:
                try:
                    blob = fetch(BASE.format(code=espn_code))
                    print(f"  {app_code:4s} note   no dark variant, used standard")
                except Exception as exc2:
                    failed.append((app_code, f"{exc} then {exc2}"))
                    print(f"  {app_code:4s} FAIL   {exc2}")
                    continue
            else:
                failed.append((app_code, str(exc)))
                print(f"  {app_code:4s} FAIL   {exc}")
                continue
        except Exception as exc:
            failed.append((app_code, str(exc)))
            print(f"  {app_code:4s} FAIL   {exc}")
            continue

        if not is_png(blob):
            failed.append((app_code, "response was not a PNG"))
            print(f"  {app_code:4s} FAIL   not a PNG ({len(blob)} bytes)")
            continue

        with io.open(dest, "wb") as fh:
            fh.write(blob)
        dims = png_size(blob)
        dim_text = f"{dims[0]}x{dims[1]}" if dims else "?"
        print(f"  {app_code:4s} OK     {len(blob)/1024:6.1f} KB  {dim_text}")
        ok += 1
        time.sleep(0.15)   # be polite to the CDN

    print(f"\nfetched {ok}, skipped {skipped}, failed {len(failed)}"
          f"  ->  {OUT_DIR}")
    if failed:
        print("failures:")
        for code, why in failed:
            print(f"  {code}: {why}")
        return 1

    missing = [c for c in ESPN_CODE if not os.path.exists(
        os.path.join(OUT_DIR, c.lower() + ".png"))]
    if missing:
        print("still missing:", ", ".join(sorted(missing)))
        return 1
    print("all 32 teams present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
