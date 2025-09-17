import os
import json
import subprocess
import sys
from typing import Dict, Any
from pathlib import Path

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import init_db, db, DB_PATH
from schemas import ProfileCreate, ProfileUpdate

BASE_DIR = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
LOGS_DIR = BASE_DIR / "logs"

WS_UI_PATH = "/ws"        # UI клиенты (подписка на события)
WS_INGEST_PATH = "/ingest"  # Воркеры (шлют heartbeats)

app = FastAPI(title="antygui-minimal")

# --- Simple in-memory WS hub (только отправка в UI) ---
class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

hub = Hub()

@app.on_event("startup")
def _startup():
    init_db()
    PROFILES_DIR.mkdir(exist_ok=True, parents=True)
    LOGS_DIR.mkdir(exist_ok=True, parents=True)

# --- UI websocket: только подписка ---
@app.websocket(WS_UI_PATH)
async def ws_ui(ws: WebSocket):
    # здесь НЕ читаем ничего, только держим соединение и шлём broadcast
    await hub.connect(ws)
    try:
        while True:
            # Просто держим соединение живым; если клиент закроет — отловим исключение
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)

# --- Worker websocket: ingest heartbeats ---
@app.websocket(WS_INGEST_PATH)
async def ws_ingest(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_text()
            try:
                payload = json.loads(data)
            except Exception:
                continue

            if payload.get("type") == "heartbeat" and "profile_id" in payload:
                # Обновляем DB
                with db() as conn:
                    conn.execute(
                        "UPDATE profiles SET state=?, last_url=? WHERE id=?",
                        (payload.get("state", "running"), payload.get("url", ""), payload["profile_id"]),
                    )
                # Шлём UI
                await hub.broadcast({"type": "state", **payload})
    except WebSocketDisconnect:
        pass
    except Exception:
        # Логировать можно в файл / консоль; оставим молча
        pass

@app.get("/")
def index():
    return FileResponse((BASE_DIR / "static" / "index.html").as_posix())

app.mount("/static", StaticFiles(directory=(BASE_DIR / "static").as_posix()), name="static")

@app.get("/api/profiles")
def list_profiles():
    with db() as conn:
        rows = conn.execute("SELECT id, name, proxy, state, pid, last_url FROM profiles ORDER BY name").fetchall()
        return [dict(r) for r in rows]

@app.post("/api/profiles")
def create_profile(payload: ProfileCreate):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO profiles(id, name, proxy, state) VALUES(hex(randomblob(16)), ?, ?, 'stopped') RETURNING id",
            (payload.name, payload.proxy or ""),
        )
        row = cur.fetchone()
        return {"id": row[0]}

@app.patch("/api/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ProfileUpdate):
    sets = []
    args = []
    if payload.name is not None:
        sets.append("name=?"); args.append(payload.name)
    if payload.proxy is not None:
        sets.append("proxy=?"); args.append(payload.proxy)
    if not sets:
        return {"updated": 0}
    args.extend([profile_id])
    with db() as conn:
        conn.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id=?", args)
    return {"updated": 1}

@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    with db() as conn:
        row = conn.execute("SELECT pid FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        pid = row[0]
        if pid:
            try:
                p = psutil.Process(pid)
                p.terminate()
            except Exception:
                pass
        conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    # remove profile dir
    pdir = PROFILES_DIR / profile_id
    if pdir.exists():
        for child in pdir.glob("**/*"):
            try:
                child.unlink()
            except IsADirectoryError:
                pass
        try:
            pdir.rmdir()
        except Exception:
            pass
    return {"deleted": True}

def _worker_cmd(profile_id: str, name: str, proxy: str):
    py = sys.executable
    ws = f"ws://127.0.0.1:8000{WS_INGEST_PATH}"  # <-- ВАЖНО: воркер теперь ходит на /ingest
    profile_dir = (PROFILES_DIR / profile_id).as_posix()
    return [
        py, (BASE_DIR / "worker.py").as_posix(),
        "--id", profile_id,
        "--name", name,
        "--proxy", proxy,
        "--ws", ws,
        "--dir", profile_dir
    ]

@app.post("/api/profiles/{profile_id}/start")
def start_profile(profile_id: str):
    with db() as conn:
        row = conn.execute("SELECT name, proxy, state, pid FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        name, proxy, state, pid = row
        if state == 'running' and pid and psutil.pid_exists(pid):
            return {"status": "already_running", "pid": pid}
        # launch worker
        cmd = _worker_cmd(profile_id, name, proxy or "")
        log_path = (LOGS_DIR / f"{profile_id}.log").as_posix()
        # на Windows stdout/stderr воркера идёт в файл
        with open(log_path, "ab", buffering=0) as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, close_fds=os.name != 'nt')
        conn.execute("UPDATE profiles SET state='running', pid=? WHERE id=?", (proc.pid, profile_id))
        return {"status": "launched", "pid": proc.pid}

@app.post("/api/profiles/{profile_id}/stop")
def stop_profile(profile_id: str):
    with db() as conn:
        row = conn.execute("SELECT pid FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        pid = row[0]
        if not pid:
            return {"status": "not_running"}
        try:
            p = psutil.Process(pid)
            p.terminate()
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        conn.execute("UPDATE profiles SET state='stopped', pid=NULL WHERE id=?", (profile_id,))
    return {"status": "stopped"}
