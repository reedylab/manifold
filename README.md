# Manifold

Self-hosted IPTV playlist manager and stream proxy. Ingest M3U playlists and XMLTV EPG data, curate channels, and serve a unified output to any IPTV player or Plex via HDHomeRun emulation.

## Features

- **M3U Ingest** -- Import channels from remote or local M3U playlists with auto-tagging (sports, news, movies, kids, events)
- **EPG Ingest** -- Fetch XMLTV guide data and match to channels by tvg-id, with dummy EPG fallback
- **Stream Proxy** -- Passthrough proxy or FFmpeg HLS re-encoding with automatic filler overlays when a source is loading
- **Filler Loop** -- Persistent background HLS stream from bump clips with branding overlays, shared across channels
- **Channel Management** -- Activate, deactivate, tag, renumber, and bulk-manage channels via web UI
- **Logo Caching** -- Downloads and serves channel logos from M3U or EPG sources
- **Image Enrichment** -- Programme poster lookup via TMDB, TVMaze, Wikipedia, Fanart.tv, and Google Images
- **HDHomeRun Emulation** -- Plex discovers Manifold as a tuner device for live TV integration
- **VPN Integration** -- Optional Gluetun sidecar routes all outbound traffic through WireGuard/OpenVPN
- **Scheduler** -- Background jobs for M3U/EPG refresh, output regeneration, logo sync, stream cleanup, and image enrichment
- **Web UI** -- Single-page management interface with TV guide grid, log viewer, and system stats

## Quick Start

### Prerequisites

- Docker and Docker Compose
- PostgreSQL 16 (bundled option available)
- A VPN provider account (optional, for Gluetun)

### 1. Clone and configure

```bash
git clone https://github.com/reedylab/manifold.git
cd manifold
cp .env.example .env
```

Edit `.env` with your database credentials and network settings.

### 2. Create Docker volumes

```bash
docker volume create manifold_output
docker volume create manifold_logos
docker volume create manifold_streams
docker volume create manifold_bumps
```

### 3a. With external PostgreSQL

```bash
docker compose up -d
```

### 3b. With bundled PostgreSQL

```bash
docker volume create manifold_pgdata
docker compose -f docker-compose.postgres.yml up -d
```

### 4. Access the UI

Open `http://<your-host>:40000` in your browser.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PG_HOST` | `localhost` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_USER` | `manifold` | Database user |
| `PG_PASS` | -- | Database password (required) |
| `PG_DB` | `manifold` | Database name |
| `MANIFOLD_HOST` | `192.168.20.34` | Host IP for generated stream URLs |
| `MANIFOLD_PORT` | `40000` | Port for the web interface |
| `BRIDGE_HOST` | `192.168.20.34` | Stream bridge host |
| `BRIDGE_PORT` | `8080` | Stream bridge port |
| `MAX_STREAMS` | `6` | Maximum concurrent FFmpeg streams |
| `STREAM_STALE_TIMEOUT` | `300` | Seconds before idle streams are cleaned up |

### VPN (Gluetun)

| Variable | Default | Description |
|----------|---------|-------------|
| `VPN_PROVIDER` | `mullvad` | VPN service provider |
| `VPN_TYPE` | `wireguard` | VPN protocol |
| `WG_PRIVATE_KEY` | -- | WireGuard private key (required if using VPN) |
| `WG_ADDRESSES` | `10.70.17.30/32` | WireGuard interface address |
| `VPN_CITIES` | `New York NY` | Preferred server location |

Additional settings (scheduler intervals, API keys, EPG options) are configurable through the web UI under Settings.

## Architecture

```
M3U Sources ──> Ingest ──> Manifests DB ──> M3U Generator ──> /manifold.m3u
EPG Sources ──> Ingest ──> EPG DB ───────> XMLTV Generator ──> /manifold.xml

Client Request ──> /stream/{id}.m3u8
                    ├─ passthrough: proxy to source
                    └─ ffmpeg: filler overlay -> live cutover -> HLS segments
```

### Stream Modes

- **Passthrough** (default): Proxies the source playlist and segments directly. Zero transcoding overhead.
- **FFmpeg**: Runs a per-channel HLS encoder. Shows a filler loop with "UP NEXT" overlay while probing the live source, then cuts over seamlessly when ready. Auto-retries on source loss.

## API

All endpoints are under `/api/`. See the [API blueprint](manifold/web/blueprints/api.py) for the full list. Key endpoints:

- `GET /api/channels` -- List channels
- `POST /api/m3u-sources` -- Add an M3U source
- `POST /api/m3u-sources/ingest` -- Trigger M3U ingest
- `POST /api/generate` -- Regenerate M3U + XMLTV output
- `GET /api/guide` -- TV guide grid data
- `GET /api/vpn/status` -- VPN tunnel status
- `GET /api/system/stats` -- CPU, RAM, disk usage

## Legal Disclaimer

This software is a tool for managing and proxying IPTV streams. It does not host, distribute, or provide any media content. Users are solely responsible for ensuring their use of this software complies with all applicable laws and regulations in their jurisdiction. The authors assume no liability for how the software is used.

## License

[MIT](LICENSE)
