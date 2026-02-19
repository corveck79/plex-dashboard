import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiosqlite
import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

PLEX_URL = "http://192.168.1.101:32400"
PLEX_TOKEN = "6eMBEX1VddA6wh5LZwFj"
DEBRID_URL = "http://192.168.1.101:5500"
DB_PATH = os.environ.get("DB_PATH", "plex_dashboard.db")
POLL_INTERVAL = 30  # seconds

PLEX_HEADERS = {
    "X-Plex-Token": PLEX_TOKEN,
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS viewing_sessions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key        TEXT    NOT NULL UNIQUE,
                user               TEXT    NOT NULL,
                title              TEXT    NOT NULL,
                show_title         TEXT    DEFAULT '',
                media_type         TEXT    DEFAULT '',
                year               INTEGER,
                platform           TEXT    DEFAULT '',
                player             TEXT    DEFAULT '',
                state              TEXT    DEFAULT '',
                progress_percent   REAL    DEFAULT 0,
                bandwidth_kbps     INTEGER DEFAULT 0,
                transcode_decision TEXT    DEFAULT 'direct',
                first_seen         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bandwidth_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user           TEXT    NOT NULL,
                bandwidth_kbps INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_bw_ts ON bandwidth_history(timestamp);
        """)
        await db.commit()
    logger.info("Database ready: %s", DB_PATH)


# ---------------------------------------------------------------------------
# Plex helpers
# ---------------------------------------------------------------------------

def _parse_sessions(data: dict) -> list[dict]:
    """Convert raw Plex /status/sessions JSON into normalised dicts."""
    container = data.get("MediaContainer", {})
    raw = container.get("Metadata") or []
    if isinstance(raw, dict):
        raw = [raw]

    sessions = []
    for s in raw:
        user_info      = s.get("User", {}) or {}
        player_info    = s.get("Player", {}) or {}
        sess_info      = s.get("Session", {}) or {}
        transcode_info = s.get("TranscodeSession") or {}

        view_offset = int(s.get("viewOffset", 0) or 0)
        duration    = int(s.get("duration", 1)   or 1) or 1
        progress    = round(view_offset / duration * 100, 1)

        sessions.append({
            "session_key": (
                sess_info.get("id")
                or f"{user_info.get('title', 'unknown')}_{s.get('ratingKey', '')}"
            ),
            "user":               user_info.get("title", "Unknown"),
            "title":              s.get("title", "Unknown"),
            "show_title":         s.get("grandparentTitle") or s.get("parentTitle") or "",
            "media_type":         s.get("type", ""),
            "year":               s.get("year"),
            "thumb":              s.get("thumb") or "",
            "state":              player_info.get("state", ""),
            "platform":           player_info.get("platform", ""),
            "player":             player_info.get("title", ""),
            "bandwidth_kbps":     int(sess_info.get("bandwidth", 0) or 0),
            "progress":           progress,
            "duration_ms":        duration,
            "view_offset_ms":     view_offset,
            "transcode_decision": transcode_info.get("videoDecision", "direct") if transcode_info else "direct",
            "location":           sess_info.get("location", ""),
        })
    return sessions


async def fetch_plex_sessions(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(f"{PLEX_URL}/status/sessions", headers=PLEX_HEADERS)
    resp.raise_for_status()
    return _parse_sessions(resp.json())


# ---------------------------------------------------------------------------
# Background poll loop
# ---------------------------------------------------------------------------

async def _poll_once() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        sessions = await fetch_plex_sessions(client)

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for s in sessions:
            await db.execute(
                """
                INSERT INTO viewing_sessions
                    (session_key, user, title, show_title, media_type, year,
                     platform, player, state, progress_percent, bandwidth_kbps,
                     transcode_decision, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    state              = excluded.state,
                    progress_percent   = excluded.progress_percent,
                    bandwidth_kbps     = excluded.bandwidth_kbps,
                    last_seen          = excluded.last_seen
                """,
                (
                    s["session_key"], s["user"], s["title"], s["show_title"],
                    s["media_type"], s["year"], s["platform"], s["player"],
                    s["state"], s["progress"], s["bandwidth_kbps"],
                    s["transcode_decision"], now, now,
                ),
            )
            if s["bandwidth_kbps"]:
                await db.execute(
                    "INSERT INTO bandwidth_history (timestamp, user, bandwidth_kbps) VALUES (?,?,?)",
                    (now, s["user"], s["bandwidth_kbps"]),
                )
        await db.commit()

    logger.info("Polled Plex: %d active session(s)", len(sessions))


async def poll_loop() -> None:
    while True:
        try:
            await _poll_once()
        except Exception as exc:
            logger.warning("Poll error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Plex Dashboard", lifespan=lifespan)


@app.get("/api/sessions")
async def api_sessions():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            sessions = await fetch_plex_sessions(client)
        return {"sessions": sessions, "error": None}
    except Exception as exc:
        logger.error("Session fetch failed: %s", exc)
        return {"sessions": [], "error": str(exc)}


@app.get("/api/bandwidth/history")
async def api_bandwidth_history(hours: int = 6):
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                strftime('%Y-%m-%dT%H:%M:00', timestamp) AS bucket,
                user,
                CAST(AVG(bandwidth_kbps) AS INTEGER)      AS avg_bw_kbps
            FROM bandwidth_history
            WHERE timestamp > ?
            GROUP BY bucket, user
            ORDER BY bucket
            """,
            (since,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"data": rows, "hours": hours}


@app.get("/api/history")
async def api_history(limit: int = 100):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT user, title, show_title, media_type, state,
                   ROUND(progress_percent, 1) AS progress_percent,
                   bandwidth_kbps, platform, transcode_decision,
                   first_seen, last_seen
            FROM viewing_sessions
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"history": rows}


@app.get("/api/stats")
async def api_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT user,
                   COUNT(*)                              AS total_sessions,
                   CAST(AVG(bandwidth_kbps) AS INTEGER)  AS avg_bw_kbps,
                   CAST(MAX(bandwidth_kbps) AS INTEGER)  AS peak_bw_kbps,
                   MAX(last_seen)                        AS last_seen
            FROM viewing_sessions
            GROUP BY user
            ORDER BY total_sessions DESC
            """
        ) as cur:
            user_stats = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT COUNT(*) AS n FROM viewing_sessions") as cur:
            total = (await cur.fetchone())["n"]
    return {"user_stats": user_stats, "total_sessions": total}


@app.get("/api/debrid")
async def api_debrid():
    """
    Probe common cli_debrid API endpoints and return whatever responds 200.
    The actual endpoint paths depend on your cli_debrid version/config.
    """
    results: dict = {}
    first_error: str | None = None

    probe_paths = [
        "/api/queue",
        "/queue",
        "/api/status",
        "/status",
        "/api/downloads",
        "/downloads",
        "/api/items",
        "/",
    ]

    async with httpx.AsyncClient(timeout=8.0) as client:
        for path in probe_paths:
            try:
                resp = await client.get(f"{DEBRID_URL}{path}")
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        results[path] = resp.json()
                    elif "text" in ct and len(resp.text) < 2000:
                        results[path] = {"_text": resp.text}
            except Exception as exc:
                if first_error is None:
                    first_error = str(exc)

    return {
        "endpoints": results,
        "url": DEBRID_URL,
        "error": first_error if not results else None,
    }


# Static files must be mounted last so API routes take priority.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
