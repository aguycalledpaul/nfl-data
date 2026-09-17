#!/usr/bin/env python3
"""
Fotbal-American.ro — NFL data builder.

Pulls open nflverse data (schedules, weekly player stats, play-by-play) plus a
small manual-overrides sheet, and emits ONE file, nfl-data.json, that is the
single source of truth for: the full weekly scoreboard, every per-game box-score
page, and the standings pages (division / conference / league).

Everything below is automatic EXCEPT the few fields nflverse doesn't carry
(TV network, recap link), which come from the overrides sheet keyed by game_id.
"""
import json, os, sys, io, urllib.request, datetime
import pandas as pd

SEASON        = int(os.environ.get("NFL_SEASON", "2026"))
OVERRIDES_URL = os.environ.get("OVERRIDES_CSV_URL", "").strip()   # Google Sheet published as CSV
OUT           = os.environ.get("OUT_PATH", "nfl-data.json")

DIVISION = {
 "BUF":"AFC East","MIA":"AFC East","NE":"AFC East","NYJ":"AFC East",
 "BAL":"AFC North","CIN":"AFC North","CLE":"AFC North","PIT":"AFC North",
 "HOU":"AFC South","IND":"AFC South","JAX":"AFC South","TEN":"AFC South",
 "DEN":"AFC West","KC":"AFC West","LV":"AFC West","LAC":"AFC West",
 "DAL":"NFC East","NYG":"NFC East","PHI":"NFC East","WAS":"NFC East",
 "CHI":"NFC North","DET":"NFC North","GB":"NFC North","MIN":"NFC North",
 "ATL":"NFC South","CAR":"NFC South","NO":"NFC South","TB":"NFC South",
 "ARI":"NFC West","LA":"NFC West","LAR":"NFC West","SF":"NFC West","SEA":"NFC West",
}
NICK = {
 "BUF":"Bills","MIA":"Dolphins","NE":"Patriots","NYJ":"Jets","BAL":"Ravens","CIN":"Bengals",
 "CLE":"Browns","PIT":"Steelers","HOU":"Texans","IND":"Colts","JAX":"Jaguars","TEN":"Titans",
 "DEN":"Broncos","KC":"Chiefs","LV":"Raiders","LAC":"Chargers","DAL":"Cowboys","NYG":"Giants",
 "PHI":"Eagles","WAS":"Commanders","CHI":"Bears","DET":"Lions","GB":"Packers","MIN":"Vikings",
 "ATL":"Falcons","CAR":"Panthers","NO":"Saints","TB":"Buccaneers","ARI":"Cardinals",
 "LA":"Rams","LAR":"Rams","SF":"49ers","SEA":"Seahawks",
}
conf = lambda c: (DIVISION.get(c,"").split() or [""])[0]

def nz(x, d=None):
    try:
        return d if pd.isna(x) else x
    except Exception:
        return x if x is not None else d

def short(name):
    p = str(name).split()
    return (p[0][0] + ". " + p[-1]) if len(p) >= 2 else str(name)

# ---------------------------------------------------------------- load nflverse (direct files — no library, verified URLs)
GAMES_URL  = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
WEEKLY_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_%d.parquet" % SEASON
PBP_URL    = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_%d.parquet" % SEASON

def _fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=120).read()

print("Loading nflverse season %d ..." % SEASON, file=sys.stderr)
games = pd.read_csv(io.StringIO(_fetch(GAMES_URL).decode("utf-8", "replace")))
games = games[games["season"] == SEASON].reset_index(drop=True)
weekly = pd.read_parquet(io.BytesIO(_fetch(WEEKLY_URL)))
try:
    pbp = pd.read_parquet(io.BytesIO(_fetch(PBP_URL)))
except Exception as e:
    print("pbp load failed (%s) — quarter line scores will be null" % e, file=sys.stderr)
    pbp = None

# nflverse has renamed columns over time — detect rather than assume
TEAM_COL = next((c for c in ("recent_team", "team", "posteam") if c in weekly.columns), None)
NAME_COL = next((c for c in ("player_display_name", "player_name", "player") if c in weekly.columns), None)
if TEAM_COL is None or NAME_COL is None:
    print("WARN: weekly team/name column not found (%s/%s) — leaders will be blank" % (TEAM_COL, NAME_COL), file=sys.stderr)

# ---------------------------------------------------------------- overrides
overrides = {}
if OVERRIDES_URL:
    try:
        raw = urllib.request.urlopen(urllib.request.Request(OVERRIDES_URL, headers={"User-Agent":"Mozilla/5.0"}), timeout=30).read().decode("utf-8","replace")
        odf = pd.read_csv(io.StringIO(raw)).fillna("")
        for _, r in odf.iterrows():
            gid = str(r.get("game_id","")).strip()
            if gid:
                overrides[gid] = {c: str(r.get(c,"")).strip() for c in odf.columns}
        print("overrides: %d rows" % len(overrides), file=sys.stderr)
    except Exception as e:
        print("overrides load failed: %s" % e, file=sys.stderr)

# ---------------------------------------------------------------- quarter line scores (from pbp running score)
linescores = {}
if pbp is not None and "qtr" in pbp.columns:
    p = pbp.dropna(subset=["qtr"])
    for gid, g in p.groupby("game_id"):
        home = [0,0,0,0,0]; away = [0,0,0,0,0]; ph = pa = 0
        for q in range(1, 6):
            gq = g[g["qtr"] <= q]
            if len(gq) == 0:
                continue
            row = gq.iloc[-1]
            ch = int(nz(row.get("total_home_score"), 0) or 0)
            ca = int(nz(row.get("total_away_score"), 0) or 0)
            home[q-1] = ch - ph; away[q-1] = ca - pa
            ph, pa = ch, ca
        # trim trailing OT column if unused
        if home[4] == 0 and away[4] == 0:
            home = home[:4]; away = away[:4]
        linescores[gid] = {"home": home, "away": away}

# ---------------------------------------------------------------- per-team, per-game leaders (from weekly stats)
def leaders_for(week, team):
    out = {"passing":{"player":"","line":""}, "rushing":{"player":"","line":""}, "receiving":{"player":"","line":""}}
    if TEAM_COL is None or NAME_COL is None:
        return out
    w = weekly[(weekly["season"]==SEASON) & (weekly["week"]==week) & (weekly[TEAM_COL]==team)]
    if len(w) == 0:
        return out
    def top(col):
        if col not in w.columns: return None
        ww = w[w[col].fillna(0) > 0]
        return None if len(ww) == 0 else ww.sort_values(col, ascending=False).iloc[0]
    pr = top("passing_yards")
    if pr is not None:
        out["passing"] = {"player": short(pr[NAME_COL]), "line": "%d yds, %d TD" % (int(pr["passing_yards"]), int(nz(pr.get("passing_tds"),0) or 0))}
    ru = top("rushing_yards")
    if ru is not None:
        out["rushing"] = {"player": short(ru[NAME_COL]), "line": "%d yds, %d TD" % (int(ru["rushing_yards"]), int(nz(ru.get("rushing_tds"),0) or 0))}
    re = top("receiving_yards")
    if re is not None:
        out["receiving"] = {"player": short(re[NAME_COL]), "line": "%d rec, %d yds" % (int(nz(re.get("receptions"),0) or 0), int(re["receiving_yards"]))}
    return out

# ---------------------------------------------------------------- standings (computed from results + division map)
teams = {}
def T(c):
    if c not in teams:
        teams[c] = {"pf":0,"pa":0,"w":0,"l":0,"t":0,"hw":0,"hl":0,"ht":0,"rw":0,"rl":0,"rt":0,
                    "dw":0,"dl":0,"dt":0,"cw":0,"cl":0,"ct":0,"res":[]}
    return teams[c]

played = games[games["home_score"].notna() & games["away_score"].notna()].sort_values("week")
for _, g in played.iterrows():
    h,a = g["home_team"], g["away_team"]; hs,as_ = int(g["home_score"]), int(g["away_score"])
    th,ta = T(h), T(a)
    th["pf"]+=hs; th["pa"]+=as_; ta["pf"]+=as_; ta["pa"]+=hs
    dv = int(nz(g.get("div_game"),0) or 0)==1; cf = conf(h)==conf(a)
    if hs>as_:
        th["w"]+=1; ta["l"]+=1; th["hw"]+=1; ta["rl"]+=1
        if dv: th["dw"]+=1; ta["dl"]+=1
        if cf: th["cw"]+=1; ta["cl"]+=1
        th["res"].append("W"); ta["res"].append("L")
    elif as_>hs:
        ta["w"]+=1; th["l"]+=1; ta["rw"]+=1; th["hl"]+=1
        if dv: ta["dw"]+=1; th["dl"]+=1
        if cf: ta["cw"]+=1; th["cl"]+=1
        ta["res"].append("W"); th["res"].append("L")
    else:
        th["t"]+=1; ta["t"]+=1; th["ht"]+=1; ta["rt"]+=1
        if dv: th["dt"]+=1; ta["dt"]+=1
        if cf: th["ct"]+=1; ta["ct"]+=1
        th["res"].append("T"); ta["res"].append("T")

rec = lambda w,l,t: ("%d-%d-%d"%(w,l,t)) if t else ("%d-%d"%(w,l))
def streak(res):
    if not res: return "—"
    last=res[-1]; n=0
    for r in reversed(res):
        if r==last: n+=1
        else: break
    return "%s%d"%(last,n)

standings=[]
for c,t in teams.items():
    gp=t["w"]+t["l"]+t["t"]; pct=round((t["w"]+0.5*t["t"])/gp,3) if gp else 0.0
    standings.append({"code":c,"name":NICK.get(c,c),"conference":conf(c),"division":DIVISION.get(c,""),
        "wins":t["w"],"losses":t["l"],"ties":t["t"],"pct":pct,"pf":t["pf"],"pa":t["pa"],"net":t["pf"]-t["pa"],
        "home":rec(t["hw"],t["hl"],t["ht"]),"road":rec(t["rw"],t["rl"],t["rt"]),
        "div":rec(t["dw"],t["dl"],t["dt"]),"conf":rec(t["cw"],t["cl"],t["ct"]),
        "streak":streak(t["res"]),"record":rec(t["w"],t["l"],t["t"])})
from collections import defaultdict
bydiv=defaultdict(list)
for s in standings: bydiv[s["division"]].append(s)
for lst in bydiv.values():
    lst.sort(key=lambda s:(-s["pct"], -s["net"]))
    for i,s in enumerate(lst): s["rank_div"]=i+1
record_map={s["code"]:s["record"] for s in standings}

# ---------------------------------------------------------------- current display week (Tue/Wed rollover)
today=datetime.datetime.utcnow()
reg=games[(games["week"]>=1)&(games["week"]<=18)]
upc=reg[reg["home_score"].isna()]
cur=int(upc.sort_values("week").iloc[0]["week"]) if len(upc) else int(reg["week"].max())
wk_started=((reg["week"]==cur)&reg["home_score"].notna()).any()
if not wk_started and today.weekday() in (1,2) and cur-1>=1:   # Python: Mon=0 -> Tue=1, Wed=2
    cur-=1

# ---------------------------------------------------------------- games
def spread(g):
    sl=nz(g.get("spread_line"))
    if sl is None: return None
    if sl==0: return {"favorite":None,"favorite_code":None,"line":0,"display":"PK"}
    fav=g["home_team"] if sl>0 else g["away_team"]
    return {"favorite":NICK.get(fav,fav),"favorite_code":fav,"line":abs(sl),"display":"%s -%g"%(fav,abs(sl))}

def kickoff(g):
    gd=nz(g.get("gameday")); gt=nz(g.get("gametime")) or "00:00"
    if not gd: return None
    # nflverse gametime is US Eastern; weeks 1–9 ≈ EDT (-04), 10+ ≈ EST (-05)
    off="-04:00" if int(g["week"])<=9 else "-05:00"
    return "%sT%s:00%s"%(gd,gt,off)

out_games=[]
for _,g in games.iterrows():
    gid=g["game_id"]; h,a=g["home_team"],g["away_team"]
    hs=nz(g.get("home_score")); as_=nz(g.get("away_score")); pl=hs is not None and as_ is not None
    ov=overrides.get(gid,{}); ls=linescores.get(gid,{})
    def team(code, score, side):
        return {"code":code,"name":NICK.get(code,code),
                "score":int(score) if score is not None else None,
                "record":record_map.get(code,"0-0"),
                "linescore":ls.get(side),
                "leaders":leaders_for(int(g["week"]),code) if pl else {"passing":{"player":"","line":""},"rushing":{"player":"","line":""},"receiving":{"player":"","line":""}}}
    out_games.append({
        "game_id":gid,"season":int(g["season"]),"week":int(g["week"]),
        "status":"final" if pl else "scheduled","kickoff":kickoff(g),"weekday":nz(g.get("weekday")),
        "venue":ov.get("venue") or nz(g.get("stadium")),"roof":nz(g.get("roof")),
        "network":ov.get("network",""),"recap_url":ov.get("recap_url",""),
        "spread":spread(g),"total":nz(g.get("total_line")),
        "overtime":int(nz(g.get("overtime"),0) or 0)==1,"div_game":int(nz(g.get("div_game"),0) or 0)==1,
        "away":team(a,as_,"away"),"home":team(h,hs,"home")})

out={"meta":{"updated":today.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "source":"nflverse (open data) + manual overrides","season":SEASON,"current_week":cur},
     "games":out_games,"standings":standings}
with open(OUT,"w",encoding="utf-8") as f: json.dump(out,f,ensure_ascii=False,indent=1)
print("wrote %s: %d games, %d teams, current_week=%d"%(OUT,len(out_games),len(standings),cur))
