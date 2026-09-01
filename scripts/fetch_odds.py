import json, os, statistics, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_KEY=os.environ.get('ODDS_API_KEY')
if not API_KEY:
    raise SystemExit('ODDS_API_KEY is not set')

TEAM_CODES={
'Arizona Cardinals':'ARI','Atlanta Falcons':'ATL','Baltimore Ravens':'BAL','Buffalo Bills':'BUF',
'Carolina Panthers':'CAR','Chicago Bears':'CHI','Cincinnati Bengals':'CIN','Cleveland Browns':'CLE',
'Dallas Cowboys':'DAL','Denver Broncos':'DEN','Detroit Lions':'DET','Green Bay Packers':'GB',
'Houston Texans':'HOU','Indianapolis Colts':'IND','Jacksonville Jaguars':'JAX','Kansas City Chiefs':'KC',
'Las Vegas Raiders':'LV','Los Angeles Chargers':'LAC','Los Angeles Rams':'LAR','Miami Dolphins':'MIA',
'Minnesota Vikings':'MIN','New England Patriots':'NE','New Orleans Saints':'NO','New York Giants':'NYG',
'New York Jets':'NYJ','Philadelphia Eagles':'PHI','Pittsburgh Steelers':'PIT','San Francisco 49ers':'SF',
'Seattle Seahawks':'SEA','Tampa Bay Buccaneers':'TB','Tennessee Titans':'TEN','Washington Commanders':'WAS'
}

def med(vals):
    vals=[v for v in vals if v is not None]
    if not vals: return None
    x=statistics.median(vals)
    return int(x) if float(x).is_integer() else round(float(x),2)

def outcome(market, name):
    if not market: return None
    return next((o for o in market.get('outcomes',[]) if o.get('name')==name),None)

params=urllib.parse.urlencode({
    'apiKey':API_KEY,'regions':'us','markets':'h2h,spreads,totals','oddsFormat':'american','dateFormat':'iso'
})
url='https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds?'+params
req=urllib.request.Request(url,headers={'User-Agent':'nfl-streaming-calendar/1.0'})
with urllib.request.urlopen(req,timeout=30) as r:
    raw=json.load(r)
    quota={k:r.headers.get(k) for k in ['x-requests-remaining','x-requests-used','x-requests-last']}

normalized=[]
for ev in raw:
    away_name,home_name=ev.get('away_team'),ev.get('home_team')
    away,home=TEAM_CODES.get(away_name),TEAM_CODES.get(home_name)
    if not away or not home: continue
    a_ml=[];h_ml=[];a_sp=[];h_sp=[];a_sprice=[];h_sprice=[];tot=[];over=[];under=[];updates=[]
    books=ev.get('bookmakers',[])
    for b in books:
        if b.get('last_update'): updates.append(b['last_update'])
        markets={m.get('key'):m for m in b.get('markets',[])}
        mh=markets.get('h2h'); ms=markets.get('spreads'); mt=markets.get('totals')
        oa,oh=outcome(mh,away_name),outcome(mh,home_name)
        if oa: a_ml.append(oa.get('price'))
        if oh: h_ml.append(oh.get('price'))
        sa,sh=outcome(ms,away_name),outcome(ms,home_name)
        if sa: a_sp.append(sa.get('point')); a_sprice.append(sa.get('price'))
        if sh: h_sp.append(sh.get('point')); h_sprice.append(sh.get('price'))
        ov,un=outcome(mt,'Over'),outcome(mt,'Under')
        if ov: tot.append(ov.get('point')); over.append(ov.get('price'))
        if un: 
            if un.get('point') is not None: tot.append(un.get('point'))
            under.append(un.get('price'))
        for m in b.get('markets',[]):
            if m.get('last_update'): updates.append(m['last_update'])
    normalized.append({
        'event_id':ev.get('id'),'commence_time':ev.get('commence_time'),'away':away,'home':home,
        'spread':{'away_point':med(a_sp),'away_price':med(a_sprice),'home_point':med(h_sp),'home_price':med(h_sprice)},
        'total':{'point':med(tot),'over_price':med(over),'under_price':med(under)},
        'moneyline':{'away':med(a_ml),'home':med(h_ml)},
        'books_count':len(books),'last_update':max(updates) if updates else None
    })

payload={'fetched_at':datetime.now(timezone.utc).isoformat(),'source':'The Odds API','method':'median consensus across US books','quota':quota,'games':normalized}
root=Path(__file__).resolve().parents[1]
(root/'odds.json').write_text(json.dumps(payload,indent=2)+'\n')
(root/'odds-data.js').write_text('window.NFL_ODDS_DATA = '+json.dumps(payload,separators=(',',':'))+';\n')
print(f'Wrote {len(normalized)} NFL games; last request cost={quota.get("x-requests-last")}, remaining={quota.get("x-requests-remaining")}')
