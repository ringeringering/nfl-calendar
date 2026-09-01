# NFL Streaming Calendar + Odds

This static site is ready for GitHub Pages.

## Odds setup

1. Create an account/key at The Odds API.
2. In the GitHub repository, open **Settings -> Secrets and variables -> Actions**.
3. Create a repository secret named `ODDS_API_KEY` containing the API key.
4. The included workflow `.github/workflows/refresh-odds.yml` runs daily and can also be run manually from the Actions tab.
5. The workflow requests the NFL `h2h`, `spreads`, and `totals` markets for the `us` region and writes `odds.json` and `odds-data.js`.

The browser never receives the API key. It only loads the generated `odds-data.js` file.

## What the app displays

The Game Details panel shows a median consensus across available US sportsbooks:

- Away/home point spread and price
- Over/under total and prices
- Away/home moneyline
- Number of contributing books
- Last odds update time

If sportsbooks have not posted a line for a game, the panel displays an unavailable message instead of a placeholder number.
