import sys

# Windows consoles often use cp1252; emoji in print() raises UnicodeEncodeError.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

import atexit
import json
import os
import threading
import time
from typing import Optional

from flask import Flask, jsonify, render_template, url_for
from flask_socketio import SocketIO, emit

# Background image: place this file in the `static` folder next to app.py.
BACKGROUND_IMAGE = "background.jpg"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_BASE_DIR, "static")
DATA_FILE = os.path.join(_BASE_DIR, "focus_storage.json")

app = Flask(__name__)
app.config["SECRET_KEY"] = "focus_level_ai_secret!"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Live snapshot (+ optional "ai" block from main.py heuristics)
focus_data = {
    "status": "🔴 Starting...",
    "score": 0,
    "total_time": 0,
    "ai": {
        "productivity": 0,
        "phone_risk": False,
        "fatigue_risk": False,
        "hint": "Smart posture & attention tracking active.",
    },
}

# Score history samples: { "total_time": int, "score": int, "status": str }
focus_history: list = []
MAX_STORED_SAMPLES = 5000
SESSION_RESET_DROP_SEC = 3

_storage_lock = threading.Lock()
_save_timer: Optional[threading.Timer] = None
_last_history_key: Optional[tuple] = None  # (t, s, phone_flag, fatigue_flag)
_last_total_time_seen = 0


def load_storage() -> None:
    """Load focus_data and focus_history from JSON on server start."""
    global focus_data, focus_history, _last_total_time_seen, _last_history_key
    if not os.path.isfile(DATA_FILE):
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        snap = raw.get("focus_data") or {}
        for key in ("status", "score", "total_time"):
            if key in snap:
                focus_data[key] = snap[key]
        ai_snap = snap.get("ai")
        if isinstance(ai_snap, dict):
            base_ai = focus_data.get("ai") if isinstance(focus_data.get("ai"), dict) else {}
            focus_data["ai"] = {
                "productivity": int(ai_snap.get("productivity", base_ai.get("productivity", 0)) or 0),
                "phone_risk": bool(ai_snap.get("phone_risk", False)),
                "fatigue_risk": bool(ai_snap.get("fatigue_risk", False)),
                "hint": str(ai_snap.get("hint", base_ai.get("hint", ""))),
            }
        hist = raw.get("history") or []
        if not isinstance(hist, list):
            hist = []
        focus_history = hist[-MAX_STORED_SAMPLES:]
        if focus_history:
            last = focus_history[-1]
            _last_total_time_seen = int(last.get("total_time", 0) or 0)
            lai = last.get("ai") if isinstance(last.get("ai"), dict) else {}
            _last_history_key = (
                _last_total_time_seen,
                int(last.get("score", 0) or 0),
                1 if lai.get("phone_risk") else 0,
                1 if lai.get("fatigue_risk") else 0,
            )
        print(f"📂 Loaded storage: {len(focus_history)} samples from {DATA_FILE}")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"⚠️ Storage load skipped: {e}")


def _write_storage_file() -> None:
    payload = {
        "version": 1,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "focus_data": dict(focus_data),
        "history": list(focus_history),
    }
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


def save_storage() -> None:
    """Persist focus_data and focus_history to disk (thread-safe)."""
    with _storage_lock:
        try:
            _write_storage_file()
        except OSError as e:
            print(f"⚠️ Storage save failed: {e}")


def schedule_save_storage() -> None:
    """Debounce disk writes so high-frequency focus_update does not thrash the disk."""
    global _save_timer

    def _run() -> None:
        global _save_timer
        with _storage_lock:
            _save_timer = None
        save_storage()

    with _storage_lock:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(2.0, _run)
        _save_timer.daemon = True
        _save_timer.start()


def append_history_sample(data: dict) -> None:
    """Append one deduplicated sample; detect webcam restart via total_time step-down."""
    global focus_history, _last_history_key, _last_total_time_seen

    if not isinstance(data, dict):
        return
    try:
        t = int(data.get("total_time", 0))
        s = int(data.get("score", 0))
    except (TypeError, ValueError):
        return
    s = max(0, min(100, s))
    status = data.get("status")
    if not isinstance(status, str):
        status = ""

    # New capture session: total_time reset (e.g. main.py restarted)
    if _last_total_time_seen > SESSION_RESET_DROP_SEC and t < _last_total_time_seen - SESSION_RESET_DROP_SEC:
        focus_history = []
        _last_history_key = None

    _last_total_time_seen = max(_last_total_time_seen, t)
    ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}
    pr = 1 if ai.get("phone_risk") else 0
    fr = 1 if ai.get("fatigue_risk") else 0
    key = (t, s, pr, fr)
    if key == _last_history_key:
        return
    _last_history_key = key

    row = {"total_time": t, "score": s, "status": status}
    if ai:
        row["ai"] = dict(ai)
    focus_history.append(row)
    if len(focus_history) > MAX_STORED_SAMPLES:
        del focus_history[: len(focus_history) - MAX_STORED_SAMPLES]


load_storage()


def flush_storage_sync() -> None:
    """Cancel pending debounced save and write immediately (best-effort on shutdown)."""
    global _save_timer
    with _storage_lock:
        if _save_timer is not None:
            _save_timer.cancel()
            _save_timer = None
    save_storage()


atexit.register(flush_storage_sync)


@app.route("/")
def index():
    """Serve the main dashboard"""
    background_url = None
    if os.path.isfile(os.path.join(_STATIC_DIR, BACKGROUND_IMAGE)):
        background_url = url_for("static", filename=BACKGROUND_IMAGE)
    return render_template(
        "index.html",
        focus_data=focus_data,
        background_url=background_url,
    )


@app.route("/data")
def data():
    """JSON snapshot for polling (focus score, session time, status)."""
    return jsonify(focus_data)


@app.route("/session")
def session_bootstrap():
    """Full session snapshot + score history for chart / summary hydration (read-only for clients)."""
    with _storage_lock:
        hist = list(focus_history)
        snap = dict(focus_data)
    return jsonify({"focus": snap, "history": hist})


@socketio.on("connect")
def handle_connect():
    """Handle new client connection"""
    print("🔗 New client connected!")
    emit("focus_update", focus_data)


@socketio.on("focus_update")
def handle_focus_update(data):
    """Receive focus data from main.py and broadcast to all clients"""
    global focus_data
    if not isinstance(data, dict):
        return
    prev_ai = focus_data.get("ai") if isinstance(focus_data.get("ai"), dict) else {}
    focus_data = {
        "status": data.get("status", focus_data["status"]),
        "score": data.get("score", focus_data["score"]),
        "total_time": data.get("total_time", focus_data["total_time"]),
    }
    ai_in = data.get("ai")
    if isinstance(ai_in, dict):
        focus_data["ai"] = {
            "productivity": int(ai_in.get("productivity", prev_ai.get("productivity", 0)) or 0),
            "phone_risk": bool(ai_in.get("phone_risk", False)),
            "fatigue_risk": bool(ai_in.get("fatigue_risk", False)),
            "hint": str(ai_in.get("hint", prev_ai.get("hint", ""))),
        }
    else:
        focus_data["ai"] = dict(prev_ai) if prev_ai else {
            "productivity": 0,
            "phone_risk": False,
            "fatigue_risk": False,
            "hint": "",
        }
    append_history_sample(focus_data)
    schedule_save_storage()
    emit("focus_update", focus_data, broadcast=True)
    print(
        f"📊 Update: {focus_data['status']} | Score: {focus_data['score']}% | Time: {focus_data['total_time']}s"
    )


if __name__ == "__main__":
    print("🌐 Starting Focus Level AI Web Dashboard...")
    print("📱 Open: http://localhost:5000")
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
