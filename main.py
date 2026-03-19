from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import os

RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")
SUMMONER_NAME = "Hisashii"
SUMMONER_TAG = "NA1"
REGION = "na1"
REGIONAL = "americas"

app = FastAPI(title="HPL Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

puuid_cache = None
match_cache = []
cache_time = None

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

async def get_puuid():
    global puuid_cache
    if puuid_cache:
        return puuid_cache
    url = f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{SUMMONER_NAME}/{SUMMONER_TAG}"
    data = await riot_get(url)
    if data:
        puuid_cache = data["puuid"]
    return puuid_cache

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
    return {"name": "HPL Tracker", "status": "online"}

@app.get("/api/health")
async def health():
    return {"status": "healthy", "apiKeySet": bool(RIOT_API_KEY)}

@app.get("/api/dashboard")
async def dashboard():
    global match_cache, cache_time
    
    puuid = await get_puuid()
    if not puuid:
        return {"error": "Account not found", "games": []}
    
    now = datetime.now()
    if cache_time and (now - cache_time).seconds < 30 and match_cache:
        games = match_cache
    else:
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
        match_cache = games
        cache_time = now
    
    if not games:
        return {"games": [], "deathTimings": {}, "patterns": [], "winRate": 0, "avgDeaths": 0, "status": "READY"}
    
    wins = sum(1 for g in games if g["win"])
    deaths = sum(g["deaths"] for g in games)
    heatmap = calc_heatmap(games)
    
    return {
        "games": games,
        "deathTimings": heatmap,
        "patterns": get_patterns(heatmap),
        "winRate": round(wins/len(games)*100, 1),
        "avgDeaths": round(deaths/len(games), 1),
        "status": "READY"
    }
