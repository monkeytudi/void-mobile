'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  convMode:    false,
  previewMode: false,
  recording:   false,
  timer:       null,
  sessionId:   crypto.randomUUID(),
};

// ── Elements ──────────────────────────────────────────────────────────────────
const orb          = document.getElementById('orb');
const orbHint      = document.getElementById('orb-hint');
const transcriptEl = document.getElementById('transcript');
const responseEl   = document.getElementById('response');
const badgeConv    = document.getElementById('badge-conv');
const badgePreview = document.getElementById('badge-preview');
const btnConv      = document.getElementById('btn-conv');
const btnPreview   = document.getElementById('btn-preview');

// ── MediaRecorder ─────────────────────────────────────────────────────────────
let mediaRec = null;
let chunks   = [];

async function initMic() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg', 'audio/mp4']
    .find(m => MediaRecorder.isTypeSupported(m)) || '';
  mediaRec = new MediaRecorder(stream, mimeType ? { mimeType } : {});
  mediaRec.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
  mediaRec.onstop = () => sendAudio(new Blob(chunks, { type: mediaRec.mimeType }));
}

// ── Orb: hold to record ───────────────────────────────────────────────────────
function startRec() {
  if (state.recording || !mediaRec) return;
  state.recording = true;
  chunks = [];
  mediaRec.start();
  setOrbState('listening');
  orbHint.textContent = 'Loslassen zum Senden';
}

function stopRec() {
  if (!state.recording || !mediaRec) return;
  state.recording = false;
  mediaRec.stop();
  setOrbState('idle');
  orbHint.textContent = state.convMode ? 'Konversation aktiv — halten zum Sprechen' : 'Halten zum Sprechen';
}

orb.addEventListener('mousedown',  startRec);
orb.addEventListener('touchstart', e => { e.preventDefault(); startRec(); }, { passive: false });
orb.addEventListener('mouseup',    stopRec);
orb.addEventListener('touchend',   stopRec);
orb.addEventListener('mouseleave', stopRec);

// ── Send to backend ───────────────────────────────────────────────────────────
async function sendAudio(blob) {
  setOrbState('idle');
  transcriptEl.textContent = '…';
  responseEl.textContent = '';

  const ext = blob.type.includes('ogg') ? '.ogg' : blob.type.includes('mp4') ? '.mp4' : '.webm';
  const fd  = new FormData();
  fd.append('audio', blob, `audio${ext}`);
  fd.append('session_id', state.sessionId);
  fd.append('conv_mode',  state.convMode  ? 'true' : 'false');
  fd.append('preview',    state.previewMode ? 'true' : 'false');

  try {
    const res  = await fetch('/api/voice', { method: 'POST', body: fd });
    const data = await res.json();
    handleResponse(data);
  } catch (err) {
    setOrbState('error');
    responseEl.textContent = 'Verbindungsfehler, Sir.';
    console.error(err);
  }
}

async function sendText(text) {
  transcriptEl.textContent = text;
  responseEl.textContent = '…';
  try {
    const res  = await fetch('/api/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, session_id: state.sessionId, conv_mode: state.convMode, preview: state.previewMode }),
    });
    const data = await res.json();
    handleResponse(data);
  } catch (err) {
    responseEl.textContent = 'Verbindungsfehler, Sir.';
  }
}

// ── Handle backend response ───────────────────────────────────────────────────
function handleResponse(data) {
  if (data.error) {
    transcriptEl.textContent = '';
    responseEl.textContent = data.error;
    return;
  }

  transcriptEl.textContent = data.text || '';
  responseEl.textContent   = data.response || '';

  // Play TTS audio
  if (data.audio) {
    playBase64Audio(data.audio);
  } else if (data.response) {
    browserTTS(data.response);
  }

  // Handle extras
  const ex = data.extra || {};

  if (ex.timer_seconds > 0) startTimer(ex.timer_seconds);
  if (ex.toggle_conv !== undefined) setConvMode(!state.convMode);
  if (ex.preview !== undefined) setPreviewMode(ex.preview);
  if (ex.spotify) handleSpotify(ex.spotify);
  if (ex.call)   window.location.href = `tel:${ex.call}`;
  if (ex.call_name) {
    // Open phone app; user selects contact manually
    window.location.href = `tel:`;
  }
}

// ── Audio playback ────────────────────────────────────────────────────────────
function playBase64Audio(b64) {
  const bytes  = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const blob   = new Blob([bytes], { type: 'audio/mpeg' });
  const url    = URL.createObjectURL(blob);
  const audio  = new Audio(url);
  setOrbState('speaking');
  audio.play().catch(() => browserTTS(responseEl.textContent));
  audio.onended = () => { setOrbState('idle'); URL.revokeObjectURL(url); };
}

function browserTTS(text) {
  if (!text || !window.speechSynthesis) return;
  setOrbState('speaking');
  const utt  = new SpeechSynthesisUtterance(text);
  utt.lang   = 'de-DE';
  utt.rate   = 1.05;
  utt.onend  = () => setOrbState('idle');
  speechSynthesis.speak(utt);
}

// ── Orb states ────────────────────────────────────────────────────────────────
function setOrbState(s) {
  orb.classList.remove('idle', 'listening', 'speaking', 'error');
  orb.classList.add(s);
}

// ── Conversation mode ─────────────────────────────────────────────────────────
function toggleConv() { setConvMode(!state.convMode); }

function setConvMode(on) {
  state.convMode = on;
  badgeConv.classList.toggle('hidden', !on);
  btnConv.classList.toggle('conv-active', on);
  orbHint.textContent = on ? 'Konversation aktiv — halten zum Sprechen' : 'Halten zum Sprechen';
  if (on) browserTTS('Konversationsmodus aktiviert, Sir.');
  else    browserTTS('Konversation beendet, Sir.');
}

// ── Preview mode ──────────────────────────────────────────────────────────────
function togglePreview() { setPreviewMode(!state.previewMode); }

function setPreviewMode(on) {
  state.previewMode = on;
  badgePreview.classList.toggle('hidden', !on);
  btnPreview.classList.toggle('preview-active', on);
}

// ── Timer ─────────────────────────────────────────────────────────────────────
function startTimer(seconds) {
  if (state.timer) clearInterval(state.timer);
  let remaining = seconds;
  const overlay  = document.getElementById('timer-overlay');
  const display  = document.getElementById('timer-display');

  overlay.classList.remove('hidden');
  updateTimerDisplay(remaining, display);

  state.timer = setInterval(() => {
    remaining--;
    updateTimerDisplay(remaining, display);
    if (remaining <= 0) {
      clearInterval(state.timer);
      state.timer = null;
      overlay.classList.add('hidden');
      browserTTS('Timer abgelaufen, Sir.');
      vibrate([200, 100, 200, 100, 400]);
    }
  }, 1000);
}

function updateTimerDisplay(s, el) {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  el.textContent = `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}

function cancelTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  document.getElementById('timer-overlay').classList.add('hidden');
}

// ── Notes overlay ─────────────────────────────────────────────────────────────
async function showNotes() {
  document.getElementById('notes-overlay').classList.remove('hidden');
  await loadNotes();
}

function closeNotes() {
  document.getElementById('notes-overlay').classList.add('hidden');
}

async function loadNotes() {
  const list = document.getElementById('notes-list');
  list.innerHTML = '';
  try {
    const res   = await fetch('/api/notes');
    const data  = await res.json();
    if (!data.notes.length) {
      list.innerHTML = '<p class="notes-empty">Keine Notizen vorhanden.</p>';
      return;
    }
    data.notes.forEach(line => {
      const m    = line.match(/^\[(.+?)\]\s*(.*)$/);
      const time = m ? m[1] : '';
      const text = m ? m[2] : line;
      const div  = document.createElement('div');
      div.className = 'note-item';
      div.innerHTML = `<div class="note-time">${time}</div><div class="note-text">${text}</div>`;
      list.appendChild(div);
    });
  } catch {
    list.innerHTML = '<p class="notes-empty">Fehler beim Laden.</p>';
  }
}

async function clearNotes() {
  if (!confirm('Alle Notizen löschen?')) return;
  await fetch('/api/notes', { method: 'DELETE' });
  await loadNotes();
}

// ── Spotify ───────────────────────────────────────────────────────────────────
function handleSpotify(action) {
  // Opens Spotify app; proper API control in V2
  window.location.href = action === 'play'
    ? 'spotify:track:1JHXUzf2TzZz8anNRnmASO'
    : 'spotify:';
}

// ── Vibration ─────────────────────────────────────────────────────────────────
function vibrate(pattern) {
  if ('vibrate' in navigator) navigator.vibrate(pattern);
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  try {
    await initMic();
  } catch {
    orbHint.textContent = 'Mikrofon-Zugriff verweigert';
    setOrbState('error');
  }
});
