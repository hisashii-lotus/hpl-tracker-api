from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import os

RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")
REGIONAL = "americas"

app = FastAPI(title="HPL Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache per summoner
cache = {}

async def riot_get(url):
    if not RIOT_API_KEY:
        return None
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                await asyncio.sleep(2)
                return await riot_get(url)
        except:
            pass
    return None

async def get_puuid(name: str, tag: str):
    url = f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
    data = await riot_get(url)
    return data.get("puuid") if data else None

async def get_matches(puuid, count=5):
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}&queue=420"
    return await riot_get(url) or []

async def get_match(match_id):
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return await riot_get(url)

async def get_timeline(match_id):
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    return await riot_get(url)

def extract_player(match, puuid):
    for p in match.get("info", {}).get("participants", []):
        if p.get("puuid") == puuid:
            dur = match["info"]["gameDuration"]
            return {
                "matchId": match["metadata"]["matchId"],
                "win": p["win"],
                "kills": p["kills"],
                "deaths": p["deaths"],
                "assists": p["assists"],
                "champion": p["championName"],
                "cs": p["totalMinionsKilled"] + p.get("neutralMinionsKilled", 0),
                "duration": f"{dur//60}:{dur%60:02d}"
            }
    return None

def get_deaths(timeline, puuid, match):
    deaths = []
    pid = None
    for p in match.get("info", {}).get("participants", []):
        if p.get("puuid") == puuid:
            pid = p.get("participantId")
            break
    if not pid:
        return deaths
    for frame in timeline.get("info", {}).get("frames", []):
        for e in frame.get("events", []):
            if e.get("type") == "CHAMPION_KILL" and e.get("victimId") == pid:
                deaths.append(e.get("timestamp", 0) // 60000)
    return deaths

def calc_heatmap(games):
    b = {"0-5": 0, "5-10": 0, "10-15": 0, "15-20": 0, "20+": 0}
    for g in games:
        for d in g.get("deathTimings", []):
            if d < 5:
                b["0-5"] += 1
            elif d < 10:
                b["5-10"] += 1
            elif d < 15:
                b["10-15"] += 1
            elif d < 20:
                b["15-20"] += 1
            else:
                b["20+"] += 1
    n = max(len(games), 1)
    return {k: round(v/n, 2) for k, v in b.items()}

def get_patterns(heatmap):
    patterns = []
    if heatmap.get("10-15", 0) > 1.0:
        patterns.append({"title": "Mid-Game Deaths", "severity": "high", "description": f"{heatmap['10-15']} deaths 10-15min", "action": "Ward river before roaming"})
    if heatmap.get("20+", 0) > 1.2:
        patterns.append({"title": "Late Game Deaths", "severity": "medium", "description": f"{heatmap['20+']} deaths 20+min", "action": "Wait 2s before fights"})
    return patterns

@app.get("/")
async def root():
    return {"name": "HPL Tracker", "status": "online", "version": "2.0", "note": "Add ?summoner=Name&tag=NA1 to /api/dashboard"}

@app.get("/api/health")
async def health():
    return {"status": "healthy", "apiKeySet": bool(RIOT_API_KEY)}

@app.get("/api/dashboard")
async def dashboard(
    summoner: str = Query(default="Hisashii", description="Summoner name"),
    tag: str = Query(default="NA1", description="Tag (e.g., NA1, EUW)")
):
    cache_key = f"{summoner}#{tag}".lower()
    now = datetime.now()
    
    # Check cache (30 second TTL)
    if cache_key in cache:
        cached = cache[cache_key]
        if (now - cached["time"]).seconds < 30:
            return cached["data"]
    
    # Get PUUID
    puuid = await get_puuid(summoner, tag)
    if not puuid:
        return {"error": f"Account not found: {summoner}#{tag}", "games": []}
    
    # Fetch matches
    match_ids = await get_matches(puuid, 5)
    games = []
    for mid in match_ids:
        match = await get_match(mid)
        if match:
            player = extract_player(match, puuid)
            if player:
                tl = await get_timeline(mid)
                player["deathTimings"] = get_deaths(tl, puuid, match) if tl else []
                games.append(player)
        await asyncio.sleep(0.1)
    
    if not games:
        return {"games": [], "summoner": f"{summoner}#{tag}", "deathTimings": {}, "patterns": [], "winRate": 0, "avgDeaths": 0, "status": "READY"}
    
    wins = sum(1 for g in games if g["win"])
    deaths = sum(g["deaths"] for g in games)
    heatmap = calc_heatmap(games)
    
    result = {
        "summoner": f"{summoner}#{tag}",
        "games": games,
        "deathTimings": heatmap,
        "patterns": get_patterns(heatmap),
        "winRate": round(wins/len(games)*100, 1),
        "avgDeaths": round(deaths/len(games), 1),
        "status": "READY"
    }
    
    # Cache result
    cache[cache_key] = {"time": now, "data": result}
    
    return result
