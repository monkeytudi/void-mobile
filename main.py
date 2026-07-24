from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
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
import io
import wave
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Railway-Container laufen in UTC - ohne explizite Zeitzone waere jede genannte/gespeicherte
# Uhrzeit 2h (Sommerzeit) bzw. 1h (Winterzeit) falsch gegenueber Oesterreich/Deutschland.
TZ = ZoneInfo("Europe/Vienna")

def local_now() -> datetime:
    return datetime.now(TZ)

import fortnite

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
FORTNITE_PUSH_SECRET = os.getenv("FORTNITE_PUSH_SECRET", "")

NOTES_FILE = Path(__file__).parent / "notes.json"
sessions: dict[str, list] = {}
stopwatches: dict[str, float] = {}   # session_id → start timestamp (0 = stopped)

BLOCKED = {"110", "112", "911", "118", "999", "0110", "0112", "0911"}
_EL_FALLBACKS = ["onwK4e9ZLuTAKqWW03F9", "pNInz6obpgDQGcFmaJgB", "N2lVS1w4EtoT3dr4eOWO"]
EL_VOICES = ([EL_VOICE_ID] if EL_VOICE_ID else []) + _EL_FALLBACKS

# Piper: lokale, kostenlose TTS-Notbremse ohne Account/Kontingent/Netzwerkabhaengigkeit -
# springt ein wenn Cartesia (nur Preview) und ElevenLabs (Free-Tier gerne mal gesperrt) beide
# ausfallen. Modell wird beim ersten Start einmalig heruntergeladen (nicht ins Repo committet).
PIPER_DIR = Path(__file__).parent / "piper_voice"
PIPER_MODEL_PATH = PIPER_DIR / "de_DE-thorsten-medium.onnx"
PIPER_CONFIG_PATH = PIPER_DIR / "de_DE-thorsten-medium.onnx.json"
PIPER_MODEL_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"
PIPER_CONFIG_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json"
_piper_voice = None


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

def _ensure_piper_model():
    """Laedt das Piper-Sprachmodell einmalig herunter, falls noch nicht vorhanden (blockierend,
    aber nur beim allerersten Piper-Einsatz pro Container-Start noetig)."""
    PIPER_DIR.mkdir(exist_ok=True)
    if not PIPER_MODEL_PATH.exists():
        with httpx.Client(timeout=60.0) as c:
            PIPER_MODEL_PATH.write_bytes(c.get(PIPER_MODEL_URL, follow_redirects=True).content)
    if not PIPER_CONFIG_PATH.exists():
        with httpx.Client(timeout=30.0) as c:
            PIPER_CONFIG_PATH.write_bytes(c.get(PIPER_CONFIG_URL, follow_redirects=True).content)


def _piperize(text: str) -> str:
    """Piper ist ein rein deutsches Modell und liest 'Sir' sonst komplett eingedeutscht -
    phonetische Ersatzschreibung nur fuer die Piper-Synthese, der eigentliche Text/Response
    bleibt unveraendert."""
    return re.sub(r'\bSir\b', 'Sör', text)


def _synthesize_piper_sync(text: str) -> bytes:
    """Blockierend (onnxruntime-Inferenz) - immer ueber asyncio.to_thread aufrufen, sonst
    haengt der Event-Loop fuer die Dauer der Synthese (~1-2s)."""
    global _piper_voice
    try:
        from piper import PiperVoice
        _ensure_piper_model()
        if _piper_voice is None:
            _piper_voice = PiperVoice.load(str(PIPER_MODEL_PATH), str(PIPER_CONFIG_PATH))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            _piper_voice.synthesize_wav(_piperize(text), wf)
        return buf.getvalue()
    except Exception as e:
        print(f"[TTS] Piper Exception: {e!r}")
        return b""


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
                    print(f"[TTS] ElevenLabs {vid} -> {r.status_code}: {r.text[:200]}")
                    if r.status_code != 402:
                        break
            except Exception as e:
                print(f"[TTS] ElevenLabs {vid} Exception: {e!r}")
                break

    # Lokale Piper-Stimme als letzte, garantiert verfuegbare Stufe (kein Account, kein
    # Kontingent, kein Netzwerk noetig) - besser als stumm zu bleiben, wenn beide Cloud-
    # Anbieter ausfallen. Android-App faengt leeres Audio zwar selbst per Geraete-TTS ab,
    # der Discord-Bot hat dieses Fallback nicht, deshalb hier zuverlaessig etwas liefern.
    audio = await asyncio.to_thread(_synthesize_piper_sync, text)
    if audio:
        return audio

    return b""  # Browser/Android-TTS-Fallback beim Client


# ── Wake word + LLM intent routing ────────────────────────────────────────────

WAKE = re.compile(r'\b(void|foid|boid|boyd|woid|voit|voyd|foi|void\.)\b', re.IGNORECASE)

def strip_wake(text: str) -> str | None:
    """Gibt den Text nach dem Wake-Word zurück, oder None wenn 'Void' nicht gesagt wurde."""
    t = text.lower().strip()
    if not WAKE.search(t):
        return None
    return re.sub(r'^.*?' + WAKE.pattern + r'\s*[,.]?\s*', '', t, count=1).strip()


TOOLS = [
    {"name": "get_time", "description": "Aktuelle Uhrzeit nennen.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "set_timer", "description": "Einen Countdown-Timer stellen.",
     "input_schema": {"type": "object",
                       "properties": {"seconds": {"type": "integer", "description": "Timer-Dauer in Sekunden"}},
                       "required": ["seconds"]}},
    {"name": "toggle_music", "description": "Musik/Spotify starten oder pausieren.",
     "input_schema": {"type": "object",
                       "properties": {"state": {"type": "string", "enum": ["play", "pause"]}},
                       "required": ["state"]}},
    {"name": "save_note",
     "description": ("Eine wichtige Information dauerhaft merken (Notiz/Erinnerung/Fakt). "
                      "Extrahiere NUR die relevante Kernaussage aus dem Gesagten, nicht den kompletten Satz wörtlich "
                      "(z.B. aus 'mir hat jemand gesagt er mag Burger, merk dir das' wird 'Mag gerne Burger'). "
                      "Vergib 1-3 kurze thematische Tags (z.B. 'vorlieben', 'essen', 'termin', 'person')."),
     "input_schema": {"type": "object",
                       "properties": {
                           "content": {"type": "string", "description": "Bereinigte Kernaussage, kurz und klar"},
                           "tags": {"type": "array", "items": {"type": "string"}, "description": "1-3 Schlagworte"},
                       },
                       "required": ["content", "tags"]}},
    {"name": "start_stopwatch", "description": "Stoppuhr starten.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "stop_stopwatch", "description": "Stoppuhr stoppen und verstrichene Zeit nennen.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "stopwatch_status", "description": "Stand der laufenden Stoppuhr abfragen, ohne sie zu stoppen.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "flip_coin", "description": "Eine Münze werfen (Kopf oder Zahl).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "toggle_conversation_mode",
     "description": "Konversationsmodus ein-/ausschalten (danach automatisch weiterhören ohne erneutes 'Void').",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "set_preview_mode", "description": "Preview-Modus (hochwertigere, aber kostenpflichtige Stimme) umschalten.",
     "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]}},
    {"name": "start_call", "description": "Ein Telefonat starten, per Nummer oder per Namen.",
     "input_schema": {"type": "object",
                       "properties": {"number": {"type": "string", "description": "Telefonnummer, falls genannt"},
                                      "name": {"type": "string", "description": "Name, falls keine Nummer genannt wurde"}},
                       "required": []}},
    {"name": "check_tournament",
     "description": ("Aktuellen Stand des getrackten Fortnite-Turniers abfragen (Platzierung, Punkte, "
                      "Kills, Siege der getrackten Teams). Nutze dies bei Fragen wie 'wie steht's mit dem "
                      "Turnier', 'Fortnite Turnier', 'was machen meine Teams gerade', 'Leaderboard'."),
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]

ROUTER_SYSTEM = (
    "Du bist der Befehls-Router von Void, einem Sprachassistenten. Ordne die Nutzeräußerung genau EINEM "
    "passenden Tool zu, auch bei unterschiedlicher Formulierung (z.B. 'wirf eine Münze', 'Münzwurf' und "
    "'lass einen Münzwurf machen' sind derselbe Befehl). Rufe KEIN Tool auf, wenn es sich um eine offene "
    "Frage, Unterhaltung oder etwas handelt, das zu keinem Tool passt."
)

async def route(clean_text: str) -> tuple[str | None, dict]:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300, "tools": TOOLS,
                  "system": ROUTER_SYSTEM,
                  "messages": [{"role": "user", "content": clean_text}]},
        )
    if r.status_code != 200:
        return None, {}
    for block in r.json().get("content", []):
        if block.get("type") == "tool_use":
            return block["name"], block.get("input", {})
    return None, {}


# ── Claude ───────────────────────────────────────────────────────────────────

async def claude(text: str, sid: str) -> str:
    hist = sessions.setdefault(sid, [])
    hist.append({"role": "user", "content": text})
    system = ("Du bist Void, ein persönlicher KI-Assistent. "
              "Antworte kurz und präzise auf Deutsch. "
              "Sprich den Nutzer mit 'Sir' an. Kein Markdown.")
    ctx = notes_context()
    if ctx:
        system += ("\n\n" + ctx + "\nNutze diese gemerkten Informationen wenn sie zur Frage passen, "
                   "ohne sie explizit als 'Notiz' zu bezeichnen, außer danach wird gefragt.")
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 180,
                  "system": system,
                  "messages": hist[-12:]},
        )
    if r.status_code == 200:
        reply = r.json()["content"][0]["text"]
        hist.append({"role": "assistant", "content": reply})
        sessions[sid] = hist[-20:]
        return reply
    return "Entschuldigung Sir, es gab einen Fehler."


# ── Helpers ───────────────────────────────────────────────────────────────────

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

def load_notes() -> list[dict]:
    if not NOTES_FILE.exists():
        return []
    try:
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_note(text: str, tags: list[str] | None = None, raw: str = ""):
    notes = load_notes()
    notes.append({
        "id": (notes[-1]["id"] + 1) if notes else 1,
        "ts": local_now().strftime("%d.%m.%Y %H:%M"),
        "text": text,
        "tags": tags or [],
        "raw": raw or text,
    })
    NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")

def notes_context(limit: int = 150) -> str:
    notes = load_notes()[-limit:]
    if not notes:
        return ""
    lines = [f"- [{', '.join(n.get('tags') or [])}] {n['text']}" for n in notes]
    return "Bekannte gemerkte Informationen:\n" + "\n".join(lines)

def _migrate_legacy_notes():
    """Übernimmt alte notes.txt einmalig nach notes.json, falls vorhanden."""
    legacy = Path(__file__).parent / "notes.txt"
    if NOTES_FILE.exists() or not legacy.exists():
        return
    notes = []
    for i, line in enumerate(legacy.read_text(encoding="utf-8").splitlines(), start=1):
        m = re.match(r'^\[(.*?)\]\s*(.*)$', line)
        ts, text = (m.group(1), m.group(2)) if m else (local_now().strftime("%d.%m.%Y %H:%M"), line)
        if text.strip():
            notes.append({"id": i, "ts": ts, "text": text.strip(), "tags": [], "raw": line})
    if notes:
        NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")

_migrate_legacy_notes()

def normalize(num: str) -> str:
    return re.sub(r"[\s\-\+\(\)]", "", num)


# ── Main processor ────────────────────────────────────────────────────────────

async def process(text: str, sid: str, conv_mode: bool, preview: bool):
    extra = {}

    if conv_mode:
        resp = await claude(text, sid)
        action = "chat"
    else:
        clean = strip_wake(text)

        if clean is None:
            action = None
            resp = "Bitte beginne deinen Befehl mit 'Void', Sir."
        else:
            tool_name, params = await route(clean)

            if tool_name is None:
                action = "chat"
                resp = await claude(text, sid)

            elif tool_name == "get_time":
                action = "uhrzeit"
                now = local_now()
                resp = (f"Es ist {now.hour} Uhr {now.minute:02d}, Sir."
                        if now.minute else f"Es ist {now.hour} Uhr, Sir.")

            elif tool_name == "set_timer":
                action = "timer"
                sek = int(params.get("seconds") or 0)
                if sek:
                    extra["timer_seconds"] = sek
                    resp = f"Timer auf {fmt_duration(sek)} gestellt, Sir."
                else:
                    resp = "Wie lange soll der Timer laufen, Sir?"

            elif tool_name == "toggle_music":
                if params.get("state") == "pause":
                    action = "spotify_pause"
                    extra["spotify"] = "pause"
                    resp = "Musik wird pausiert, Sir."
                else:
                    action = "spotify_play"
                    extra["spotify"] = "play"
                    resp = "Spotify wird gestartet, Sir."

            elif tool_name == "save_note":
                action = "notiz"
                note = (params.get("content") or clean).strip()
                tags = params.get("tags") or []
                save_note(note, tags, raw=text)
                extra["note"] = note
                resp = "Notiz gespeichert, Sir."

            elif tool_name == "start_call":
                action = "anruf"
                number = (params.get("number") or "").strip()
                name = (params.get("name") or "").strip()
                if number:
                    num = normalize(number)
                    if num in BLOCKED:
                        resp = "Diese Nummer ist blockiert, Sir."
                        action = "blocked"
                    else:
                        extra["call"] = num
                        resp = f"Verbinde mit {num}, Sir."
                else:
                    extra["call_name"] = name or clean
                    resp = f"Öffne Telefon für {name or clean}, Sir."

            elif tool_name == "flip_coin":
                action = "muenzwurf"
                resp = f"{random.choice(['Kopf', 'Zahl'])}, Sir."

            elif tool_name == "start_stopwatch":
                action = "stoppuhr_start"
                stopwatches[sid] = time.time()
                resp = "Stoppuhr gestartet, Sir."

            elif tool_name == "stop_stopwatch":
                action = "stoppuhr_stop"
                start = stopwatches.pop(sid, 0)
                resp = (_fmt_elapsed(time.time() - start) + ", Sir.") if start else "Keine laufende Stoppuhr, Sir."

            elif tool_name == "stopwatch_status":
                action = "stoppuhr_status"
                start = stopwatches.get(sid, 0)
                resp = (_fmt_elapsed(time.time() - start) + " bisher, Sir.") if start else "Die Stoppuhr läuft nicht, Sir."

            elif tool_name == "toggle_conversation_mode":
                action = "konversation"
                extra["toggle_conv"] = True
                resp = "Konversationsmodus umgeschaltet, Sir."

            elif tool_name == "set_preview_mode":
                enabled = bool(params.get("enabled", True))
                action = "preview_on" if enabled else "preview_off"
                extra["preview"] = enabled
                resp = f"Preview Mode {'aktiviert' if enabled else 'deaktiviert'}, Sir."

            elif tool_name == "check_tournament":
                action = "turnier"
                resp = await fortnite.tournament_status()

            else:
                action = "chat"
                resp = await claude(text, sid)

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
    lines = [f"[{n['ts']}] {n['text']}" for n in load_notes()]
    return {"notes": list(reversed(lines[-100:]))}


class FortnitePush(BaseModel):
    data: dict

@app.post("/api/fortnite/push")
async def fortnite_push(req: FortnitePush, x_push_secret: str = Header(default="")):
    # Railways eigene Server-IP wird von Cloudflare geblockt (403) - dieser Endpoint nimmt stattdessen
    # ein von aussen (funktionierender Standort) bereits geholtes Leaderboard-JSON entgegen.
    if FORTNITE_PUSH_SECRET and x_push_secret != FORTNITE_PUSH_SECRET:
        raise HTTPException(status_code=403, detail="Falsches Secret")
    fortnite.store_pushed(req.data)
    return {"ok": True}


@app.delete("/api/notes")
async def clear_notes():
    NOTES_FILE.write_text("[]", encoding="utf-8")
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
