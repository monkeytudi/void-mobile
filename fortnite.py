"""Liest das Fortnite-Tracker-Leaderboard eines Turnier-Events aus und fasst die Platzierung der
getrackten Teams zusammen.

Die Event-Seite (fortnitetracker.com) sitzt hinter Cloudflare-Bot-Schutz - ein einfacher HTTP-Request
bekommt nur die "Just a moment..."-Challenge-Seite (403). Mit einem Browser-TLS-Fingerprint (curl_cffi,
impersoniert echtes Chrome) kommt man durch, auch von einer Rechenzentrums-IP aus (wie Railway) getestet.
Die komplette Leaderboard-Tabelle liegt zudem schon als JSON-Variable (`var imp_leaderboard = ...`) im
HTML - kein HTML-Tabellen-Parsing noetig, nur die Variable extrahieren.
"""
import asyncio
import json
import os
import re
import time

from curl_cffi import requests as cf_requests

EVENT_URL = os.getenv(
    "FORTNITE_EVENT_URL",
    "https://fortnitetracker.com/events/epicgames_S41_FNCSMajor2_PlayInStage_EU",
)

# Teams werden ueber Spieler-Namensfragmente gefunden, nicht ueber exakte Anzeige-Namen -
# die aendern sich (Tags, Sonderzeichen), die Fragmente bleiben stabil genug.
TRACKED_TEAMS = [
    {"label": "Kaan & Rezon", "fragments": ["kaan", "rezon"]},
    {"label": "Vadeal & Juu", "fragments": ["vadeal", "juu"]},
]

_CACHE_TTL_S = 300  # Quelle aktualisiert laut internal_Cache_Mins ohnehin nur stuendlich
_cache: dict = {"ts": 0.0, "data": None, "url": None}


def _fetch_sync(url: str) -> dict | None:
    r = cf_requests.get(url, impersonate="chrome124", timeout=20)
    if r.status_code != 200:
        return None
    m = re.search(r"var imp_leaderboard = (\{.*?\});", r.text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


async def _get_leaderboard() -> dict | None:
    now = time.time()
    if _cache["data"] is not None and _cache["url"] == EVENT_URL and now - _cache["ts"] < _CACHE_TTL_S:
        return _cache["data"]
    data = await asyncio.to_thread(_fetch_sync, EVENT_URL)
    if data is not None:
        _cache.update(ts=now, data=data, url=EVENT_URL)
        return data
    return _cache["data"]  # Fehlschlag -> letzten bekannten Stand weiterverwenden, falls vorhanden


def _find_account_id(accounts: dict, fragment: str) -> str | None:
    frag = fragment.lower()
    for aid, info in accounts.items():
        nick = (info.get("nickname") or "").lower()
        esn = (info.get("esportsNickname") or "").lower()
        if frag in nick or frag in esn:
            return aid
    return None


def _find_entry(entries: list, account_id: str) -> dict | None:
    for e in entries:
        if account_id in (e.get("teamAccountIds") or []):
            return e
    return None


def _summarize(entry: dict, label: str) -> str:
    rank = entry.get("rank")
    pts = entry.get("pointsEarned")
    stats = entry.get("sessionStats") or {}
    matches = stats.get("matches")
    wins = stats.get("wins") or 0
    avg_elims = stats.get("avgElims") or 0
    avg_place = stats.get("avgPlace") or 0
    wins_word = "Sieg" if wins == 1 else "Siege"
    return (f"{label} liegen auf Platz {rank} mit {pts} Punkten aus {matches} Spielen "
            f"({wins} {wins_word}, im Schnitt {avg_elims:.1f} Kills, Platz {avg_place:.1f})")


async def tournament_status() -> str:
    data = await _get_leaderboard()
    if data is None:
        return "Ich komme gerade nicht ans Turnier-Leaderboard ran, Sir."

    accounts = data.get("internal_Accounts") or {}
    entries = data.get("entries") or []

    lines = []
    for team in TRACKED_TEAMS:
        entry = None
        for frag in team["fragments"]:
            aid = _find_account_id(accounts, frag)
            if aid:
                entry = _find_entry(entries, aid)
                if entry:
                    break
        lines.append(_summarize(entry, team["label"]) if entry
                     else f"{team['label']} sind aktuell nicht auf dem Leaderboard")

    return ". ".join(lines) + ", Sir."
