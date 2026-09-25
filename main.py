import os
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": FOOTBALL_API_KEY}

app = FastAPI(title="PitchOps API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("FRONTEND_ORIGIN", "*").split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Simple in-memory cache ──────────────────────────────────────────────────
import time
_cache: dict = {}

async def apf(path: str, ttl: int = 300) -> dict:
    """Fetch from API-Football with TTL cache to preserve daily quota."""
    now = time.time()
    if path in _cache and now - _cache[path]["ts"] < ttl:
        return _cache[path]["data"]
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE}{path}", headers=HEADERS)
    data = r.json()
    if r.status_code != 200 or data.get("errors"):
        raise HTTPException(status_code=r.status_code, detail=data.get("errors", data))
    _cache[path] = {"data": data, "ts": now}
    return data

def zone(rank: int, league_id: int) -> str | None:
    if league_id in (39, 140, 78, 135, 61):  # Top 5 domestic
        if rank <= 4:  return "ucl"
        if rank == 5:  return "uel"
        if rank == 6:  return "uecl"
        if rank == 17: return "relegation-playoff"
        if rank >= 18: return "relegation"
    if league_id in (2, 3):  # UCL/UEL group stage
        if rank <= 2:  return "ucl"
        if rank >= 3:  return "relegation"
    return None

# ── Status ──────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "version": "2.0"}

# ── Standings ───────────────────────────────────────────────────────────────

@app.get("/standings")
async def get_standings(
    league: int = Query(...),
    season: int = Query(...),
):
    data = await apf(f"/standings?league={league}&season={season}", ttl=3600)
    raw = data.get("response", [])
    if not raw:
        raise HTTPException(404, "No standings data")
    league_data = raw[0]["league"]
    rows = []
    for row in league_data["standings"][0]:
        row["zone"] = zone(row["rank"], league)
        rows.append(row)
    return {
        "league": {
            "id": league_data["id"], "name": league_data["name"],
            "country": league_data["country"], "logo": league_data["logo"],
            "flag": league_data.get("flag", ""), "season": league_data["season"],
        },
        "standings": [rows],
    }

# ── Fixtures ────────────────────────────────────────────────────────────────

@app.get("/fixtures")
async def get_fixtures(
    league: int | None = Query(None),
    season: int | None = Query(None),
    team:   int | None = Query(None),
    from_:  str | None = Query(None, alias="from"),
    to:     str | None = Query(None),
    round:  str | None = Query(None),
    id:     int | None = Query(None),
):
    params = []
    if id:     params.append(f"id={id}")
    if league: params.append(f"league={league}")
    if season: params.append(f"season={season}")
    if team:   params.append(f"team={team}")
    if from_:  params.append(f"from={from_}")
    if to:     params.append(f"to={to}")
    if round:  params.append(f"round={round}")
    qs = "&".join(params)
    data = await apf(f"/fixtures?{qs}", ttl=300)
    return {"fixtures": data.get("response", [])}


# ── Fixture detail ───────────────────────────────────────────────────────────

@app.get("/fixtures/{fixture_id}/events")
async def get_events(fixture_id: int):
    data = await apf(f"/fixtures/events?fixture={fixture_id}", ttl=86400)
    return {"fixtureId": fixture_id, "events": data.get("response", [])}

@app.get("/fixtures/{fixture_id}/statistics")
async def get_fixture_stats(fixture_id: int):
    data = await apf(f"/fixtures/statistics?fixture={fixture_id}", ttl=86400)
    return {"fixtureId": fixture_id, "teams": data.get("response", [])}

@app.get("/fixtures/{fixture_id}/lineups")
async def get_lineups(fixture_id: int):
    data = await apf(f"/fixtures/lineups?fixture={fixture_id}", ttl=86400)
    return {"fixtureId": fixture_id, "lineups": data.get("response", [])}

@app.get("/fixtures/{fixture_id}/h2h")
async def get_h2h(fixture_id: int):
    # Get fixture to find team IDs
    fix_data = await apf(f"/fixtures?id={fixture_id}", ttl=86400)
    fixtures = fix_data.get("response", [])
    if not fixtures:
        raise HTTPException(404, "Fixture not found")
    f = fixtures[0]
    home_id = f["teams"]["home"]["id"]
    away_id = f["teams"]["away"]["id"]

    h2h_data = await apf(f"/fixtures/headtohead?h2h={home_id}-{away_id}&last=10", ttl=86400)
    meetings = h2h_data.get("response", [])

    home_wins  = sum(1 for m in meetings if m["teams"]["home"]["id"] == home_id and m["teams"]["home"]["winner"])
    away_wins  = sum(1 for m in meetings if m["teams"]["away"]["id"] == away_id and m["teams"]["away"]["winner"])
    draws      = sum(1 for m in meetings if not m["teams"]["home"]["winner"] and not m["teams"]["away"]["winner"])

    return {
        "fixtureId": fixture_id,
        "h2h": {
            "teamA": {
                "team": f["teams"]["home"],
                "wins": home_wins, "draws": draws, "losses": away_wins,
                "goalsScored":    sum(m["goals"]["home"] or 0 for m in meetings if m["teams"]["home"]["id"] == home_id),
                "goalsConceded":  sum(m["goals"]["away"] or 0 for m in meetings if m["teams"]["home"]["id"] == home_id),
            },
            "teamB": {
                "team": f["teams"]["away"],
                "wins": away_wins, "draws": draws, "losses": home_wins,
                "goalsScored":    sum(m["goals"]["away"] or 0 for m in meetings if m["teams"]["away"]["id"] == away_id),
                "goalsConceded":  sum(m["goals"]["home"] or 0 for m in meetings if m["teams"]["away"]["id"] == away_id),
            },
            "totalMatches": len(meetings),
            "recentFixtures": meetings[:5],
        },
    }

@app.get("/fixtures/{fixture_id}/prediction")
async def get_prediction(fixture_id: int):
    data = await apf(f"/predictions?fixture={fixture_id}", ttl=86400)
    items = data.get("response", [])
    if not items:
        raise HTTPException(404, "No prediction available")
    p = items[0]
    return {
        "fixtureId": fixture_id,
        "predictions": p.get("predictions", {}),
        "comparison":  p.get("comparison", {}),
        "teams": {
            "home": p.get("teams", {}).get("home", {}),
            "away": p.get("teams", {}).get("away", {}),
        },
    }

# ── Statistics ───────────────────────────────────────────────────────────────

@app.get("/statistics/top-scorers")
async def top_scorers(league: int = Query(...), season: int = Query(...)):
    data = await apf(f"/players/topscorers?league={league}&season={season}", ttl=3600)
    scorers = data.get("response", [])
    league_info = scorers[0]["statistics"][0]["league"] if scorers else {}
    return {"league": league_info, "scorers": scorers}

@app.get("/statistics/top-assists")
async def top_assists(league: int = Query(...), season: int = Query(...)):
    data = await apf(f"/players/topassists?league={league}&season={season}", ttl=3600)
    players = data.get("response", [])
    league_info = players[0]["statistics"][0]["league"] if players else {}
    return {"league": league_info, "scorers": players}

@app.get("/statistics/top-yellowcards")
async def top_yellowcards(league: int = Query(...), season: int = Query(...)):
    data = await apf(f"/players/topyellowcards?league={league}&season={season}", ttl=3600)
    players = data.get("response", [])
    league_info = players[0]["statistics"][0]["league"] if players else {}
    return {"league": league_info, "scorers": players}

@app.get("/statistics/top-redcards")
async def top_redcards(league: int = Query(...), season: int = Query(...)):
    data = await apf(f"/players/topredcards?league={league}&season={season}", ttl=3600)
    players = data.get("response", [])
    league_info = players[0]["statistics"][0]["league"] if players else {}
    return {"league": league_info, "scorers": players}

@app.get("/statistics/team/{team_id}")
async def team_stats(
    team_id: int,
    league: int = Query(...),
    season: int = Query(...),
):
    data = await apf(f"/teams/statistics?team={team_id}&league={league}&season={season}", ttl=3600)
    stats = data.get("response", {})
    return {"team": stats.get("team", {}), "statistics": stats}

@app.get("/players/{player_id}")
async def player_stats(player_id: int, season: int = Query(...)):
    data = await apf(f"/players?id={player_id}&season={season}", ttl=3600)
    players = data.get("response", [])
    if not players:
        raise HTTPException(404, "Player not found")
    return {"player": players[0]}

@app.get("/teams/{team_id}/squad")
async def team_squad(team_id: int):
    data = await apf(f"/players/squads?team={team_id}", ttl=86400)
    squads = data.get("response", [])
    if not squads:
        raise HTTPException(404, "Squad not found")
    return {"team": squads[0]["team"], "players": squads[0]["players"]}

@app.get("/injuries")
async def injuries(
    league: int = Query(...),
    season: int = Query(...),
):
    data = await apf(f"/injuries?league={league}&season={season}", ttl=3600)
    return {"injuries": data.get("response", [])}
