# Wishlist Monitor

A self-hosted dashboard for tracking Steam wishlist momentum. Everything is
driven by a single **`config.yaml`** — there is no admin panel, no login, and no
setup wizard. Point it at your games, open the page, and watch the numbers.

## Quick start

1. Install Python deps (only PyYAML is required beyond the stdlib):

   ```bash
   pip install pyyaml
   ```

2. Create your config from the template and fill in your Steam Partner API key
   and the games you want to show:

   ```bash
   cp config.example.yaml config.yaml
   ```

3. Build the frontend once (outputs to `web/dist/`, which the server serves):

   ```bash
   cd web && npm install && npm run build && cd ..
   ```

4. Run it:

   ```bash
   python main.py
   ```

   Then open the bind address (default `http://localhost:14520`).

## Configuration

All settings live in `config.yaml` (see [`config.example.yaml`](config.example.yaml)
for the annotated template):

| Key | Meaning | Default |
| --- | --- | --- |
| `steam_api_key` | Steam Partner API key used to fetch wishlist data | — |
| `games` | List of Steam App IDs or store URLs to display | `[]` |
| `bind` | `host:port` for the web server | `0.0.0.0:14520` |
| `database_path` | SQLite file location | OS app-data dir |
| `poll_interval_minutes` | Refresh cadence | `5` |
| `backfill_rate` | History backfill rate (req/sec) | `1.0` |
| `anomaly.*` | Anomaly-detection tuning | see template |

Use a custom path with `python main.py --config /path/to/config.yaml`
(or set `CONFIG_PATH`).

The `games` list is the source of truth: on start the app tracks any new games
and untracks ones you removed. Untracking is **non-destructive** — historical
snapshots are preserved, so re-adding a game restores its data immediately.
