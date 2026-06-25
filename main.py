from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import json
import os
import re
import base64
import random
import time
from datetime import datetime
from pathlib import Path

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONFIG_PATH = Path(__file__).parent / "config.json"
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}

GROQ_KEY        = os.getenv("GROQ_API_KEY",        config.get("groq_api_key", ""))
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY",   config.get("anthropic_api_key", ""))
CARTESIA_KEY    = os.getenv("CARTESIA_API_KEY",     config.get("cartesia_api_key", ""))
CARTESIA_VOICE  = os.getenv("CARTESIA_VOICE_ID",   config.get("cartesia_voice_id", ""))
EL_KEY          = os.getenv("ELEVENLABS_API_KEY",   config.get("elevenlabs_api_key", ""))
EL_VOICE_ID     = os.getenv("ELEVENLABS_VOICE_ID",  config.get("elevenlabs_voice_id", ""))

NOTES_FILE = Path(__file__).parent / "notes.txt"
sessions: dict[str, list] = {}
stopwatches: dict[str, float] = {}   # session_id → start timestamp (0 = stopped)

BLOCKED = {"110", "112", "911", "118", "999", "0110", "0112", "0911"}
_EL_FALLBACKS = ["onwK4e9ZLuTAKqWW03F9", "pNInz6obpgDQGcFmaJgB", "N2lVS1w4EtoT3dr4eOWO"]
EL_VOICES = ([EL_VOICE_ID] if EL_VOICE_ID else []) + _EL_FALLBACKS


# ── STT ──────────────────────────────────────────────────────────────────────

async def transcribe(audio: bytes, name: str = "audio.webm") -> str:
    ext = Path(name).suffix or ".webm"
    mime = "audio/webm" if "webm" in ext else "audio/mp4" if "mp4" in ext else "audio/wav"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files={"file": (f"audio{ext}", audio, mime)},
            data={"model": "whisper-large-v3", "language": "de"},
        )
        return r.json().get("text", "") if r.status_code == 200 else ""


# ── TTS ──────────────────────────────────────────────────────────────────────

async def synthesize(text: str, preview_mode: bool = False) -> bytes:
    if preview_mode and CARTESIA_KEY and CARTESIA_VOICE:
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    "https://api.cartesia.ai/tts/bytes",
                    headers={"X-API-Key": CARTESIA_KEY, "Cartesia-Version": "2026-03-01",
                             "Content-Type": "application/json"},
                    json={"transcript": text, "model_id": "sonic-3.5",
                          "voice": {"mode": "id", "id": CARTESIA_VOICE},
                          "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
                          "language": "de",
                          "generation_config": {"speed": 1.1, "volume": 1.2, "emotion": "positivity:medium"}},
                )
                if r.status_code == 200:
                    return r.content
        except Exception:
            pass

    if EL_KEY:
        for vid in EL_VOICES:
            try:
                async with httpx.AsyncClient(timeout=30.0) as c:
                    r = await c.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                        headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
                        json={"text": text, "model_id": "eleven_multilingual_v2",
                              "voice_settings": {"stability": 0.45, "similarity_boost": 0.80,
                                                 "style": 0.0, "use_speaker_boost": True}},
                    )
                    if r.status_code == 200:
                        return r.content
                    if r.status_code != 402:
                        break
            except Exception:
                break

    return b""  # Browser TTS fallback


# ── Command detection ─────────────────────────────────────────────────────────

def detect(text: str):
    t = text.lower().strip()
    if not re.search(r'\bvoid\b', t):
        return None, text
    clean = re.sub(r'^.*?\bvoid\b\s*[,.]?\s*', '', t, count=1).strip()

    if re.search(r'wie spät|uhrzeit|wie viel uhr|wieviel uhr', clean):
        return "uhrzeit", clean
    if re.search(r'timer|wecker|alarm', clean):
        return "timer", clean
    if re.search(r'musik an|play|spotify an|musik starten|song', clean):
        return "spotify_play", clean
    if re.search(r'musik aus|stopp|pause|spotify aus', clean):
        return "spotify_pause", clean
    if re.search(r'notiz|schreib|aufschreiben|merke dir|merkzettel', clean):
        return "notiz", clean
    if re.search(r'ruf.*an|anruf|call|telefonier', clean):
        return "anruf", clean
    if re.search(r'konversation|unterhal|chat.*modus|gesprächs', clean):
        return "konversation", clean
    if re.search(r'preview.*an|preview.*ein|aktiviere preview', clean):
        return "preview_on", clean
    if re.search(r'preview.*aus|deaktiviere preview', clean):
        return "preview_off", clean
    if re.search(r'münze|münzwurf|kopf.*zahl|coin', clean):
        return "muenzwurf", clean
    if re.search(r'stoppuhr.*start|stoppuhr.*an|starte stoppuhr|stoppuhr starten', clean):
        return "stoppuhr_start", clean
    if re.search(r'stoppuhr.*stop|stoppuhr.*aus|stoppe stoppuhr|stoppuhr stoppen', clean):
        return "stoppuhr_stop", clean
    if re.search(r'stoppuhr|wie lang|wie viel.*sekund|wie viele.*sekund|verstrich', clean):
        return "stoppuhr_status", clean

    return "chat", clean


# ── Claude ───────────────────────────────────────────────────────────────────

async def claude(text: str, sid: str) -> str:
    hist = sessions.setdefault(sid, [])
    hist.append({"role": "user", "content": text})
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 180,
                  "system": ("Du bist Void, ein persönlicher KI-Assistent. "
                              "Antworte kurz und präzise auf Deutsch. "
                              "Sprich den Nutzer mit 'Sir' an. Kein Markdown."),
                  "messages": hist[-12:]},
        )
    if r.status_code == 200:
        reply = r.json()["content"][0]["text"]
        hist.append({"role": "assistant", "content": reply})
        sessions[sid] = hist[-20:]
        return reply
    return "Entschuldigung Sir, es gab einen Fehler."


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_timer(text: str) -> int:
    total = 0
    for pat, mul in [(r'(\d+)\s*stund', 3600), (r'(\d+)\s*minut', 60), (r'(\d+)\s*sekund', 1)]:
        m = re.search(pat, text)
        if m:
            total += int(m.group(1)) * mul
    return total

def fmt_duration(s: int) -> str:
    if s >= 3600:
        h = s // 3600
        return f"{h} Stunde{'n' if h > 1 else ''}"
    if s >= 60:
        m = s // 60
        return f"{m} Minute{'n' if m > 1 else ''}"
    return f"{s} Sekunde{'n' if s > 1 else ''}"

def _fmt_elapsed(s: float) -> str:
    s = int(s)
    if s >= 3600:
        h, rest = divmod(s, 3600)
        m = rest // 60
        return f"{h} Stunde{'n' if h>1 else ''} und {m} Minute{'n' if m!=1 else ''}"
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{m} Minute{'n' if m>1 else ''} und {sec} Sekunde{'n' if sec!=1 else ''}"
    return f"{s} Sekunde{'n' if s!=1 else ''}"

def save_note(text: str):
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")

def normalize(num: str) -> str:
    return re.sub(r"[\s\-\+\(\)]", "", num)


# ── Main processor ────────────────────────────────────────────────────────────

async def process(text: str, sid: str, conv_mode: bool, preview: bool):
    extra = {}

    if conv_mode:
        resp = await claude(text, sid)
        action = "chat"
    else:
        action, clean = detect(text)

        if action is None:
            resp = "Bitte beginne deinen Befehl mit 'Void', Sir."

        elif action == "uhrzeit":
            now = datetime.now()
            resp = (f"Es ist {now.hour} Uhr {now.minute:02d}, Sir."
                    if now.minute else f"Es ist {now.hour} Uhr, Sir.")

        elif action == "timer":
            sek = parse_timer(clean)
            if sek:
                extra["timer_seconds"] = sek
                resp = f"Timer auf {fmt_duration(sek)} gestellt, Sir."
            else:
                resp = "Wie lange soll der Timer laufen, Sir?"

        elif action == "spotify_play":
            extra["spotify"] = "play"
            resp = "Spotify wird gestartet, Sir."

        elif action == "spotify_pause":
            extra["spotify"] = "pause"
            resp = "Musik wird pausiert, Sir."

        elif action == "notiz":
            note = re.sub(r'(notiz|schreib(e)?|aufschreiben|merke dir)[:\s]*', '', clean).strip(" :,.")
            note = note or clean
            save_note(note)
            extra["note"] = note
            resp = "Notiz gespeichert, Sir."

        elif action == "anruf":
            nums = re.findall(r'[\d\+][\d\s\-]{4,}', clean)
            if nums:
                num = normalize(nums[0])
                if num in BLOCKED:
                    resp = "Diese Nummer ist blockiert, Sir."
                    action = "blocked"
                else:
                    extra["call"] = num
                    resp = f"Verbinde mit {num}, Sir."
            else:
                name = re.sub(r'(ruf|an|anruf|telefonier(e)?|call)', '', clean).strip()
                extra["call_name"] = name
                resp = f"Öffne Telefon für {name}, Sir."

        elif action == "muenzwurf":
            resp = f"{random.choice(['Kopf', 'Zahl'])}, Sir."

        elif action == "stoppuhr_start":
            stopwatches[sid] = time.time()
            resp = "Stoppuhr gestartet, Sir."

        elif action == "stoppuhr_stop":
            start = stopwatches.pop(sid, 0)
            resp = (_fmt_elapsed(time.time() - start) + ", Sir.") if start else "Keine laufende Stoppuhr, Sir."

        elif action == "stoppuhr_status":
            start = stopwatches.get(sid, 0)
            resp = (_fmt_elapsed(time.time() - start) + " bisher, Sir.") if start else "Die Stoppuhr läuft nicht, Sir."

        elif action == "konversation":
            extra["toggle_conv"] = True
            resp = "Konversationsmodus umgeschaltet, Sir."

        elif action == "preview_on":
            extra["preview"] = True
            resp = "Preview Mode aktiviert, Sir."

        elif action == "preview_off":
            extra["preview"] = False
            resp = "Preview Mode deaktiviert, Sir."

        elif action == "chat":
            resp = await claude(text, sid)

        else:
            resp = "Diesen Befehl kenne ich nicht, Sir."

    audio = await synthesize(resp, preview_mode=preview)
    return {
        "text": text,
        "response": resp,
        "action": action,
        "audio": base64.b64encode(audio).decode() if audio else "",
        "extra": extra,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/voice")
async def api_voice(
    audio: UploadFile = File(...),
    session_id: str = Form("default"),
    conv_mode: str = Form("false"),
    preview: str = Form("false"),
):
    raw = await audio.read()
    text = await transcribe(raw, audio.filename or "audio.webm")
    if not text:
        return {"error": "Nicht verstanden", "text": ""}
    return await process(text, session_id, conv_mode == "true", preview == "true")


class TextReq(BaseModel):
    text: str
    session_id: str = "default"
    conv_mode: bool = False
    preview: bool = False

@app.post("/api/text")
async def api_text(req: TextReq):
    return await process(req.text, req.session_id, req.conv_mode, req.preview)


@app.get("/api/notes")
async def api_notes():
    if not NOTES_FILE.exists():
        return {"notes": []}
    lines = NOTES_FILE.read_text(encoding="utf-8").strip().splitlines()
    return {"notes": list(reversed(lines[-100:]))}


@app.delete("/api/notes")
async def clear_notes():
    if NOTES_FILE.exists():
        NOTES_FILE.write_text("", encoding="utf-8")
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
