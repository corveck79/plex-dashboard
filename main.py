import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiosqlite
import httpx
import psutil
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

PLEX_URL   = os.environ.get("PLEX_URL",   "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
DEBRID_URL = os.environ.get("DEBRID_URL", "")
DB_PATH    = os.environ.get("DB_PATH",    "plex_dashboard.db")
RD_TOKEN   = os.environ.get("RD_TOKEN",   "")
ZILEAN_URL = os.environ.get("ZILEAN_URL", "")
DISK_PATH  = os.environ.get("DISK_PATH",  "/nas")
POLL_INTERVAL = 30  # seconds

PLEX_HEADERS = {
    "X-Plex-Token": PLEX_TOKEN,
    "Accept": "application/json",
}

# Module-level dict for network rate calculation
_prev_net: dict = {}

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

            CREATE TABLE IF NOT EXISTS rd_daily_usage (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT NOT NULL UNIQUE,
                downloaded_gb REAL DEFAULT 0,
                streams       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS system_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                cpu_pct     REAL DEFAULT 0,
                ram_pct     REAL DEFAULT 0,
                disk_pct    REAL DEFAULT 0,
                net_rx_mbps REAL DEFAULT 0,
                net_tx_mbps REAL DEFAULT 0,
                temp_c      REAL
            );

            CREATE INDEX IF NOT EXISTS idx_sys_ts ON system_snapshots(timestamp);
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
# System stats helper (synchronous psutil — run in executor)
# ---------------------------------------------------------------------------

def _get_system_stats() -> dict:
    global _prev_net

    cpu_pct = psutil.cpu_percent(interval=None)
    mem     = psutil.virtual_memory()
    ram_pct = mem.percent

    # Disk usage with fallbacks
    disk_pct = 0.0
    for path in [DISK_PATH, "/data", "/"]:
        try:
            du = psutil.disk_usage(path)
            disk_pct = du.percent
            break
        except Exception:
            continue

    # Network rate (Mbps)
    net_rx_mbps = 0.0
    net_tx_mbps = 0.0
    try:
        counters = psutil.net_io_counters()
        now_ts   = datetime.utcnow().timestamp()
        if _prev_net:
            elapsed      = max(now_ts - _prev_net["ts"], 0.001)
            rx_diff      = max(counters.bytes_recv - _prev_net["rx"], 0)
            tx_diff      = max(counters.bytes_sent - _prev_net["tx"], 0)
            net_rx_mbps  = round(rx_diff * 8 / elapsed / 1_000_000, 3)
            net_tx_mbps  = round(tx_diff * 8 / elapsed / 1_000_000, 3)
        _prev_net = {"ts": now_ts, "rx": counters.bytes_recv, "tx": counters.bytes_sent}
    except Exception:
        pass

    # CPU temperature (optional)
    temp_c = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key in ("coretemp", "k10temp", "cpu_thermal", "cpu-thermal"):
                if key in temps and temps[key]:
                    temp_c = round(temps[key][0].current, 1)
                    break
            if temp_c is None:
                for entries in temps.values():
                    if entries:
                        temp_c = round(entries[0].current, 1)
                        break
    except Exception:
        pass

    return {
        "cpu_pct":     round(cpu_pct, 1),
        "ram_pct":     round(ram_pct, 1),
        "ram_used_gb": round(mem.used / 1024**3, 2),
        "ram_total_gb": round(mem.total / 1024**3, 2),
        "disk_pct":    round(disk_pct, 1),
        "net_rx_mbps": net_rx_mbps,
        "net_tx_mbps": net_tx_mbps,
        "temp_c":      temp_c,
    }


# ---------------------------------------------------------------------------
# Background poll loops
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


async def _poll_system_once() -> None:
    loop  = asyncio.get_running_loop()
    stats = await loop.run_in_executor(None, _get_system_stats)
    now   = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO system_snapshots
                (timestamp, cpu_pct, ram_pct, disk_pct, net_rx_mbps, net_tx_mbps, temp_c)
            VALUES (?,?,?,?,?,?,?)
            """,
            (now, stats["cpu_pct"], stats["ram_pct"], stats["disk_pct"],
             stats["net_rx_mbps"], stats["net_tx_mbps"], stats["temp_c"]),
        )
        await db.commit()


async def _poll_rd_once() -> None:
    if not RD_TOKEN:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.real-debrid.com/rest/1.0/traffic",
                headers={"Authorization": f"Bearer {RD_TOKEN}"},
            )
            resp.raise_for_status()
            traffic = resp.json()
        # traffic is a dict keyed by date: {"2024-01-01": {"downloaded": bytes, "streams": n}}
        for date_str, info in traffic.items():
            dl_bytes = info.get("downloaded", 0) or 0
            streams  = info.get("streams", 0) or 0
            dl_gb    = round(dl_bytes / 1024**3, 3)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """
                    INSERT INTO rd_daily_usage (date, downloaded_gb, streams)
                    VALUES (?,?,?)
                    ON CONFLICT(date) DO UPDATE SET
                        downloaded_gb = excluded.downloaded_gb,
                        streams       = excluded.streams
                    """,
                    (date_str, dl_gb, streams),
                )
                await db.commit()
        logger.info("RD traffic updated, %d day(s)", len(traffic))
    except Exception as exc:
        logger.warning("RD poll error: %s", exc)


async def poll_loop() -> None:
    while True:
        try:
            await asyncio.gather(
                _poll_once(),
                _poll_system_once(),
            )
        except Exception as exc:
            logger.warning("Poll error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)


async def rd_poll_loop() -> None:
    while True:
        try:
            await _poll_rd_once()
        except Exception as exc:
            logger.warning("RD loop error: %s", exc)
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up psutil CPU counter (first call always returns 0)
    psutil.cpu_percent(interval=None)

    await init_db()
    task_poll = asyncio.create_task(poll_loop())
    task_rd   = asyncio.create_task(rd_poll_loop())
    yield
    task_poll.cancel()
    task_rd.cancel()
    for t in (task_poll, task_rd):
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Plex Dashboard", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

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

    # Extract items array from whichever endpoint responded
    items = None
    for payload in results.values():
        if isinstance(payload, list):
            items = payload
            break
        if isinstance(payload, dict):
            for key in ("queue", "downloads", "items", "data", "torrents", "results"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
            if items is not None:
                break

    categories = {
        "Wanted": [], "Upgrading": [], "Checking": [],
        "Downloading": [], "Completed": [], "Failed": [], "Other": [],
    }
    if items:
        for item in items:
            status = (item.get("state") or item.get("status") or "").lower()
            if "want" in status:
                categories["Wanted"].append(item)
            elif "upgrad" in status:
                categories["Upgrading"].append(item)
            elif "check" in status or "scraping" in status:
                categories["Checking"].append(item)
            elif "download" in status:
                categories["Downloading"].append(item)
            elif "complet" in status or "symlink" in status:
                categories["Completed"].append(item)
            elif "fail" in status or "error" in status:
                categories["Failed"].append(item)
            else:
                categories["Other"].append(item)

    counts = {k: len(v) for k, v in categories.items()}

    return {
        "endpoints": results,
        "url": DEBRID_URL,
        "error": first_error if not results else None,
        "items": items or [],
        "categories": categories,
        "counts": counts,
        "total": len(items) if items else 0,
    }


# ---------------------------------------------------------------------------
# New endpoints
# ---------------------------------------------------------------------------

@app.get("/api/plex/libraries")
async def api_plex_libraries():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get library sections
            r = await client.get(f"{PLEX_URL}/library/sections", headers=PLEX_HEADERS)
            r.raise_for_status()
            sections_data = r.json().get("MediaContainer", {}).get("Directory", [])

            libraries = []
            for sec in sections_data:
                sec_key = sec.get("key")
                sec_type = sec.get("type", "")
                sec_title = sec.get("title", "")
                # Get count for this section
                count = 0
                try:
                    cr = await client.get(
                        f"{PLEX_URL}/library/sections/{sec_key}/all",
                        headers=PLEX_HEADERS,
                        params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
                    )
                    count = cr.json().get("MediaContainer", {}).get("totalSize", 0)
                except Exception:
                    pass
                libraries.append({"key": sec_key, "title": sec_title, "type": sec_type, "count": count})

            # Recent additions (last 15)
            recent = []
            try:
                rr = await client.get(
                    f"{PLEX_URL}/library/recentlyAdded",
                    headers=PLEX_HEADERS,
                    params={"X-Plex-Container-Size": 15},
                )
                items = rr.json().get("MediaContainer", {}).get("Metadata", [])
                for item in items[:15]:
                    recent.append({
                        "title":      item.get("title", ""),
                        "type":       item.get("type", ""),
                        "year":       item.get("year"),
                        "addedAt":    item.get("addedAt"),
                        "show_title": item.get("grandparentTitle") or item.get("parentTitle") or "",
                        "library":    item.get("librarySectionTitle", ""),
                    })
            except Exception:
                pass

        return {"libraries": libraries, "recent": recent, "error": None}
    except Exception as exc:
        logger.error("Libraries fetch failed: %s", exc)
        return {"libraries": [], "recent": [], "error": str(exc)}


@app.get("/api/transcode/stats")
async def api_transcode_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        since_7d = (datetime.utcnow() - timedelta(days=7)).isoformat()
        async with db.execute(
            """
            SELECT transcode_decision, COUNT(*) AS count
            FROM viewing_sessions
            GROUP BY transcode_decision
            """
        ) as cur:
            all_time = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            """
            SELECT transcode_decision, COUNT(*) AS count
            FROM viewing_sessions
            WHERE last_seen > ?
            GROUP BY transcode_decision
            """,
            (since_7d,),
        ) as cur:
            last_7d = [dict(r) for r in await cur.fetchall()]
    return {"all_time": all_time, "last_7d": last_7d}


@app.get("/api/peaks")
async def api_peaks():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                COUNT(*)                                   AS stream_count,
                CAST(AVG(bandwidth_kbps) AS INTEGER)       AS avg_bw_kbps
            FROM bandwidth_history
            GROUP BY hour
            ORDER BY hour
            """
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    # Fill all 24 hours
    by_hour = {r["hour"]: r for r in rows}
    result = [
        {
            "hour":         h,
            "stream_count": by_hour.get(h, {}).get("stream_count", 0),
            "avg_bw_kbps":  by_hour.get(h, {}).get("avg_bw_kbps", 0),
        }
        for h in range(24)
    ]
    return {"data": result}


@app.get("/api/content/top")
async def api_content_top(days: int = 30):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                title,
                show_title,
                media_type,
                COUNT(*) AS play_count,
                COUNT(DISTINCT user) AS unique_users
            FROM viewing_sessions
            WHERE last_seen > ?
            GROUP BY title, show_title
            ORDER BY play_count DESC
            LIMIT 10
            """,
            (since,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"data": rows, "days": days}


@app.get("/api/users/month")
async def api_users_month():
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                user,
                COUNT(*) AS sessions,
                ROUND(SUM(bandwidth_kbps) * 30.0 / 8 / 1024, 1) AS approx_mb
            FROM bandwidth_history
            WHERE timestamp > ?
            GROUP BY user
            ORDER BY approx_mb DESC
            """,
            (month_start,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"data": rows, "month": now.strftime("%Y-%m")}


@app.get("/api/rd/usage")
async def api_rd_usage(days: int = 30):
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT date, downloaded_gb, streams
            FROM rd_daily_usage
            WHERE date >= ?
            ORDER BY date
            """,
            (since,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"data": rows, "days": days, "has_token": bool(RD_TOKEN)}


@app.get("/api/system/current")
async def api_system_current():
    loop  = asyncio.get_running_loop()
    stats = await loop.run_in_executor(None, _get_system_stats)
    return stats


@app.get("/api/system/history")
async def api_system_history(hours: int = 3):
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT timestamp, cpu_pct, ram_pct, disk_pct,
                   net_rx_mbps, net_tx_mbps, temp_c
            FROM system_snapshots
            WHERE timestamp > ?
            ORDER BY timestamp
            """,
            (since,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"data": rows, "hours": hours}


@app.get("/api/docker/containers")
async def api_docker_containers():
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return {"containers": [], "error": "Docker socket not mounted"}
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock_path)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            resp = await client.get("http://docker/containers/json?all=1")
            resp.raise_for_status()
            raw = resp.json()
        containers = [
            {
                "id":     c.get("Id", "")[:12],
                "name":   (c.get("Names") or [""])[0].lstrip("/"),
                "image":  c.get("Image", ""),
                "state":  c.get("State", ""),
                "status": c.get("Status", ""),
            }
            for c in raw
        ]
        return {"containers": containers, "error": None}
    except Exception as exc:
        logger.warning("Docker socket error: %s", exc)
        return {"containers": [], "error": str(exc)}


@app.get("/api/zilean")
async def api_zilean():
    result = {"healthy": False, "torrent_count": None, "error": None}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            h = await client.get(f"{ZILEAN_URL}/healthcheck")
            result["healthy"] = h.status_code == 200
            try:
                t = await client.get(f"{ZILEAN_URL}/v1/api/torrents/count")
                if t.status_code == 200:
                    data = t.json()
                    result["torrent_count"] = (
                        data.get("count") or data.get("torrents") or data
                    )
            except Exception:
                pass
    except Exception as exc:
        result["error"] = str(exc)
    return result


# Static files must be mounted last so API routes take priority.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
