
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

RIOT_ACCOUNT_API = f"https://{REGIONAL}.api.riotgames.com"
RIOT_PLATFORM_API = f"https://{REGION}.api.riotgames.com"

app = FastAPI(title="HPL Tracker API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class State:
    puuid: Optional[str] = None
    cached_matches: List[dict] = []
    cache_time: Optional[datetime] = None

state = State()

async def riot_request(url: str) -> Optional[dict]:
    if not RIOT_API_KEY:
        return None
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                await asyncio.sleep(2)
                return await riot_request(url)
            return None
        except:
            return None

async def get_puuid() -> Optional[str]:
    if state.puuid:
        return state.puuid
    url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/{SUMMONER_NAME}/{SUMMONER_TAG}"
    data = await riot_request(url)
    if data:
        state.puuid = data["puuid"]
    return state.puuid

async def get_match_ids(puuid: str, count: int = 10) -> List[str]:
    url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}&queue=420"
    return await riot_request(url) or []

async def get_match(match_id: str) -> Optional[dict]:
    url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/{match_id}"
    return await riot_request(url)

async def get_timeline(match_id: str) -> Optional[dict]:
    url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/{match_id}/timeline"
    return await riot_request(url)

def extract_player(match: dict, puuid: str) -> Optional[dict]:
    participants = match.get("info", {}).get("participants", [])
    player = next((p for p in participants if p.get("puuid") == puuid), None)
    if not player:
        return None
    duration = match["info"]["gameDuration"]
    mins, secs = duration // 60, duration % 60
    return {
        "matchId": match["metadata"]["matchId"],
        "win": player["win"],
        "kills": player["kills"],
        "deaths": player["deaths"],
        "assists": player["assists"],
        "champion": player["championName"],
        "cs": player["totalMinionsKilled"] + player.get("neutralMinionsKilled", 0),
        "duration": f"{mins}:{secs:02d}",
        "durationMins": mins
    }

def get_death_timings(timeline: dict, puuid: str, match: dict) -> List[int]:
    deaths = []
    participants = match.get("info", {}).get("participants", [])
    player = next((p for p in participants if p.get("puuid") == puuid), None)
    if not player:
        return deaths
    pid = player.get("participantId")
    for frame in timeline.get("info", {}).get("frames", []):
        for event in frame.get("events", []):
            if event.get("type") == "CHAMPION_KILL" and event.get("victimId") == pid:
                deaths.append(event.get("timestamp", 0) // 60000)
    return deaths

def calc_heatmap(games: List[dict]) -> Dict[str, float]:
    brackets = {"0-5": 0, "5-10": 0, "10-15": 0, "15-20": 0, "20+": 0}
    for g in games:
        for d in g.get("deathTimings", []):
            if d < 5: brackets["0-5"] += 1
            elif d < 10: brackets["5-10"] += 1
            elif d < 15: brackets["10-15"] += 1
            elif d < 20: brackets["15-20"] += 1
            else: brackets["20+"] += 1
    n = max(len(games), 1)
    return {k: round(v/n, 2) for k, v in brackets.items()}

def get_patterns(games: List[dict], heatmap: Dict[str, float]) -> List[dict]:
    patterns = []
    if heatmap.get("10-15", 0) > 1.0:
        patterns.append({"id": "mid", "title": "Mid-Game Deaths", "severity": "high", "description": f"{heatmap['10-15']} deaths avg 10-15 min", "action": "Ward river before roaming"})
    if heatmap.get("20+", 0) > 1.2:
        patterns.append({"id": "late", "title": "Late Game Positioning", "severity": "high" if heatmap["20+"] > 2 else "medium", "description": f"{heatmap['20+']} deaths avg 20+ min", "action": "Wait 2s before entering fights"})
    if heatmap.get("0-5", 0) + heatmap.get("5-10", 0) < 0.8:
        patterns.append({"id": "early", "title": "Early Game Consistency", "severity": "low", "description": "Low early deaths", "action": "Keep respecting jungle timers"})
    return patterns

@app.get("/")
async def root():
    return {"name": "HPL Tracker API", "status": "online", "summoner": f"{SUMMONER_NAME}#{SUMMONER_TAG}"}

@app.get("/api/health")
async def health():
    return {"status": "healthy", "apiKeySet": bool(RIOT_API_KEY)}

@app.get("/api/dashboard")
async def dashboard():
    puuid = await get_puuid()
    if not puuid:
        return {"error": "Account not found"}
    
    if state.cache_time and datetime.now() - state.cache_time < timedelta(seconds=30) and state.cached_matches:
        games = state.cached_matches
    else:
        match_ids = await get_match_ids(puuid, 5)
        games = []
        for mid in match_ids:
            match = await get_match(mid)
            if match:
                player = extract_player(match, puuid)
                if player:
                    timeline = await get_timeline(mid)
                    player["deathTimings"] = get_death_timings(timeline, puuid, match) if timeline else []
                    games.append(player)
            await asyncio.sleep(0.1)
        state.cached_matches = games
        state.cache_time = datetime.now()
    
    if not games:
        return {"games": [], "deathTimings": {}, "patterns": [], "winRate": 0, "avgDeaths": 0}
    
    wins = sum(1 for g in games if g["win"])
    deaths = sum(g["deaths"] for g in games)
    heatmap = calc_heatmap(games)
    
    return {
        "games": games,
        "deathTimings": heatmap,
        "patterns": get_patterns(games, heatmap),
        "winRate": round(wins/len(games)*100, 1),
        "avgDeaths": round(deaths/len(games), 1),
        "status": "READY"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

---

## File 2: `requirements.txt`
```
fastapi==0.109.0
uvicorn==0.27.0
httpx==0.26.0
