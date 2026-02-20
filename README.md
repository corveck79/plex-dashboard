# Plex Dashboard

A self-hosted, real-time monitoring dashboard for your Plex media server and surrounding infrastructure. Built with FastAPI, SQLite, and plain HTML/JS — runs as a single Docker container.

---

## Features

### 🔴 Live tab
- Active Plex streams with progress bars, playback state, platform and player info
- Per-session transcode decision (direct / copy / transcode)
- Total bandwidth in real time
- All-time user stats (sessions, avg/peak bandwidth)
- Full watch history (last 100 sessions)

### 📊 Analytics tab
- Bandwidth history chart (1h / 3h / 6h / 24h)
- Peak-hours bar chart — see which hours of the day are busiest
- Top 10 most-played content over the last 30 days
- Per-user estimated data consumption this calendar month

### 📚 Library tab
- Plex library cards with item counts per section
- Recently added items (last 15)
- Transcode decision breakdown (doughnut chart, all-time)
- Real-Debrid daily usage chart (downloaded GB + streams per day, last 30 days) — optional

### ⚡ Queue tab
- cli_debrid download queue with category filter buttons:
  `All / Wanted / Upgrading / Checking / Downloading / Completed / Failed`
- Count badges per category
- Progress bars on active downloads

### 🖥️ System tab
- Live gauges: CPU %, RAM (used / total GB), Disk %, CPU temperature
- Network RX / TX in Mbps
- CPU + RAM history chart (last 3 hours)
- Zilean status: healthy indicator + indexed torrent count
- Docker container overview: name, image, state, status

---

## Integrations

| Service | Required | Notes |
|---|---|---|
| **Plex Media Server** | ✅ Yes | Needs URL + token |
| **cli_debrid** | Optional | Probes common API paths automatically |
| **Real-Debrid** | Optional | API token needed for usage stats |
| **Zilean** | Optional | Set URL; dashboard shows health + torrent count |
| **Docker** | Optional | Mount the Docker socket for container overview |
| **NAS disk** | Optional | Mount any path for disk usage monitoring |

---

## Quick start

```bash
git clone https://github.com/corveck79/plex-dashboard.git
cd plex-dashboard
```

Edit `docker-compose.yml` — fill in your values (see Configuration below), then:

```bash
docker compose up -d --build
```

Open `http://YOUR-NAS-IP:8080` in a browser.

---

## Configuration

Settings live in `config.json` — **no restart or rebuild needed** when you change it.
The app detects file changes automatically and reloads on the next request.

### Setup

```bash
cp config.example.json config.json
# edit config.json with your values
```

```json
{
  "plex_url":   "http://YOUR_NAS_IP:32400",
  "plex_token": "your-plex-token-here",
  "debrid_url": "http://YOUR_NAS_IP:5500",
  "zilean_url": "http://YOUR_NAS_IP:8181",
  "rd_token":   "",
  "disk_path":  "/nas"
}
```

| Key | Required | Description |
|---|---|---|
| `plex_url` | ✅ | Plex server address |
| `plex_token` | ✅ | Plex authentication token |
| `debrid_url` | Optional | cli_debrid API address |
| `zilean_url` | Optional | Zilean torrent indexer address |
| `rd_token` | Optional | Real-Debrid API token (for usage chart) |
| `disk_path` | Optional | Mount path to monitor for disk usage (default `/nas`) |

To verify the config is loaded correctly, open `http://YOUR_NAS_IP:8080/api/config` — it shows active values with tokens masked.

> `config.json` is gitignored and never committed to the repo.

### Docker socket (container overview)

To enable the Docker tab, mount the socket:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

> **Note for Synology:** the container needs to run with the same GID as the `docker` group on the host. Check with `stat /var/run/docker.sock` and add `group_add` to the compose file if needed.

### NAS disk monitoring

```yaml
volumes:
  - /volume1:/nas:ro   # adjust /volume1 to your NAS volume path
```

---

## How to get your Plex token

1. Open Plex Web
2. Play any item → open **⋮ menu → Get Info → View XML**
3. The URL contains `?X-Plex-Token=XXXX` — that's your token

Or: **Settings → Troubleshooting → Your Token** (older Plex versions).

---

## Stack

| Component | Role |
|---|---|
| FastAPI | API server |
| aiosqlite | Async SQLite storage |
| httpx | HTTP client (Plex, RD, Zilean, Docker socket) |
| psutil | System metrics (CPU, RAM, disk, network, temp) |
| Chart.js | All charts in the browser |
| Tailwind CSS | Styling |

Data is polled every **30 seconds** for Plex sessions and system stats, and every **hour** for Real-Debrid traffic.

---

## Data persistence

SQLite is stored in `./data/plex_dashboard.db` on the host (mapped to `/data` inside the container). Rebuild the container without losing history:

```bash
docker compose up -d --build
```

---

## Updating

```bash
git pull
docker compose up -d --build
```
