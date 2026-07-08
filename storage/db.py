from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from models import ChartPoint, CountryDailyMax, CountryReport, DailyMax, GameSyncRow, GameTotals, WishlistReport


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_games (
                app_id INTEGER PRIMARY KEY,
                tracked_since TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS wishlist_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                adds INTEGER NOT NULL DEFAULT 0,
                deletes INTEGER NOT NULL DEFAULT 0,
                purchases INTEGER NOT NULL DEFAULT 0,
                gifts INTEGER NOT NULL DEFAULT 0,
                fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_app_date ON wishlist_snapshots(app_id, date);
            CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON wishlist_snapshots(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_app_fetched ON wishlist_snapshots(app_id, fetched_at);

            CREATE TABLE IF NOT EXISTS app_info (
                app_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_subscriptions (
                provider TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                app_id INTEGER NOT NULL,
                subscribed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                PRIMARY KEY (provider, channel_id, app_id)
            );

            CREATE TABLE IF NOT EXISTS snapshot_countries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES wishlist_snapshots(id) ON DELETE CASCADE,
                country_code TEXT NOT NULL,
                adds INTEGER NOT NULL DEFAULT 0,
                deletes INTEGER NOT NULL DEFAULT 0,
                purchases INTEGER NOT NULL DEFAULT 0,
                gifts INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_snapshot_countries_snapshot ON snapshot_countries(snapshot_id);

            CREATE TABLE IF NOT EXISTS crawled_dates (
                app_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                PRIMARY KEY (app_id, date)
            );

            CREATE TABLE IF NOT EXISTS backfill_failed_dates (
                app_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                fail_count INTEGER NOT NULL DEFAULT 1,
                last_failed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                PRIMARY KEY (app_id, date)
            );

            CREATE TABLE IF NOT EXISTS game_sync_status (
                app_id INTEGER PRIMARY KEY,
                sync_type TEXT NOT NULL DEFAULT 'initial',
                status TEXT NOT NULL DEFAULT 'in_progress',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                total_dates INTEGER NOT NULL DEFAULT 0,
                requested_by TEXT
            );
            """
        )
        for col in ("adds_windows", "adds_mac", "adds_linux"):
            try:
                conn.execute(f"ALTER TABLE wishlist_snapshots ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE app_info ADD COLUMN min_date TEXT")
        except sqlite3.OperationalError:
            pass

    def get_config(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def delete_config(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM app_config WHERE key = ?", (key,))

    def get_all_config(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_config").fetchall()
            return {row["key"]: row["value"] for row in rows}

    def add_tracked_game(self, app_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO tracked_games (app_id) VALUES (?)", (app_id,))
            return cur.rowcount > 0

    def remove_tracked_game(self, app_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM tracked_games WHERE app_id = ?", (app_id,))
            changed = cur.rowcount > 0
            if changed:
                conn.execute("DELETE FROM wishlist_snapshots WHERE app_id = ?", (app_id,))
                conn.execute("DELETE FROM app_info WHERE app_id = ?", (app_id,))
                conn.execute("DELETE FROM crawled_dates WHERE app_id = ?", (app_id,))
                conn.execute("DELETE FROM backfill_failed_dates WHERE app_id = ?", (app_id,))
                conn.execute("DELETE FROM game_sync_status WHERE app_id = ?", (app_id,))
            conn.execute("COMMIT")
            return changed

    def reconcile_tracked_games(self, app_ids: list[int]) -> tuple[list[int], list[int]]:
        """Make tracked_games match ``app_ids`` (the config source of truth).

        Adds newly listed games and untracks games no longer listed. Removal is
        non-destructive: historical snapshots are kept so re-adding a game to the
        config restores its data instantly. Returns ``(added, removed)``.
        """
        desired = list(dict.fromkeys(app_ids))
        desired_set = set(desired)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = {int(row["app_id"]) for row in conn.execute("SELECT app_id FROM tracked_games").fetchall()}
                added = [app_id for app_id in desired if app_id not in existing]
                removed = [app_id for app_id in existing if app_id not in desired_set]
                for app_id in added:
                    conn.execute("INSERT OR IGNORE INTO tracked_games (app_id) VALUES (?)", (app_id,))
                for app_id in removed:
                    conn.execute("DELETE FROM tracked_games WHERE app_id = ?", (app_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return added, removed

    def get_tracked_game_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT app_id FROM tracked_games ORDER BY tracked_since").fetchall()
            return [int(row["app_id"]) for row in rows]

    def get_tracked_games_with_dates(self) -> dict[int, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT app_id, tracked_since FROM tracked_games").fetchall()
            return {int(row["app_id"]): row["tracked_since"] for row in rows}

    def is_tracked(self, app_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM tracked_games WHERE app_id = ?", (app_id,)).fetchone()
            return bool(row and row["c"])

    def _load_countries(self, conn: sqlite3.Connection, snapshot_id: int) -> list[CountryReport]:
        rows = conn.execute(
            """
            SELECT country_code, adds, deletes, purchases, gifts
            FROM snapshot_countries
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        return [
            CountryReport(
                country_code=row["country_code"],
                adds=int(row["adds"]),
                deletes=int(row["deletes"]),
                purchases=int(row["purchases"]),
                gifts=int(row["gifts"]),
            )
            for row in rows
        ]

    def _save_countries(self, conn: sqlite3.Connection, snapshot_id: int, countries: list[CountryReport]) -> None:
        conn.executemany(
            """
            INSERT INTO snapshot_countries (snapshot_id, country_code, adds, deletes, purchases, gifts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(snapshot_id, c.country_code, c.adds, c.deletes, c.purchases, c.gifts) for c in countries],
        )

    def _row_to_report(self, row: sqlite3.Row) -> WishlistReport:
        return WishlistReport(
            app_id=int(row["app_id"]),
            date=row["date"] or "",
            adds=int(row["adds"] or 0),
            deletes=int(row["deletes"] or 0),
            purchases=int(row["purchases"] or 0),
            gifts=int(row["gifts"] or 0),
            adds_windows=int(row["adds_windows"] or 0),
            adds_mac=int(row["adds_mac"] or 0),
            adds_linux=int(row["adds_linux"] or 0),
            fetched_at=row["fetched_at"],
        )

    def get_latest_snapshot(self, app_id: int) -> WishlistReport | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, app_id, date, adds, deletes, purchases, gifts,
                       adds_windows, adds_mac, adds_linux, fetched_at
                FROM wishlist_snapshots
                WHERE app_id = ?
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (app_id,),
            ).fetchone()
            if not row:
                return None
            report = self._row_to_report(row)
            report.countries = self._load_countries(conn, int(row["id"]))
            return report

    def insert_snapshot_if_changed(self, report: WishlistReport) -> tuple[str, WishlistReport | None]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT id, app_id, date, adds, deletes, purchases, gifts,
                           adds_windows, adds_mac, adds_linux, fetched_at
                    FROM wishlist_snapshots
                    WHERE app_id = ?
                    ORDER BY fetched_at DESC
                    LIMIT 1
                    """,
                    (report.app_id,),
                ).fetchone()
                previous = None
                if row:
                    previous = self._row_to_report(row)
                    previous.countries = self._load_countries(conn, int(row["id"]))
                    if (
                        previous.date == report.date
                        and previous.adds == report.adds
                        and previous.deletes == report.deletes
                        and previous.purchases == report.purchases
                        and previous.gifts == report.gifts
                    ):
                        conn.execute("ROLLBACK")
                        return "no_change", previous

                cur = conn.execute(
                    """
                    INSERT INTO wishlist_snapshots
                        (app_id, date, adds, deletes, purchases, gifts, adds_windows, adds_mac, adds_linux)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.app_id,
                        report.date,
                        report.adds,
                        report.deletes,
                        report.purchases,
                        report.gifts,
                        report.adds_windows,
                        report.adds_mac,
                        report.adds_linux,
                    ),
                )
                self._save_countries(conn, cur.lastrowid, report.countries)
                conn.execute("COMMIT")
                return ("changed", previous) if previous else ("first_snapshot", None)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_crawled_dates_for_game(self, app_id: int, include_snapshots: bool) -> set[str]:
        query = (
            """
            SELECT DISTINCT date FROM wishlist_snapshots WHERE app_id = ?
            UNION
            SELECT date FROM crawled_dates WHERE app_id = ?
            """
            if include_snapshots
            else "SELECT date FROM crawled_dates WHERE app_id = ?"
        )
        params = (app_id, app_id) if include_snapshots else (app_id,)
        with self.connect() as conn:
            return {row["date"] for row in conn.execute(query, params).fetchall()}

    def mark_date_crawled(self, app_id: int, date: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO crawled_dates (app_id, date) VALUES (?, ?)", (app_id, date))

    def insert_backfill_snapshot(self, report: WishlistReport, fetched_at: str, force: bool) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if force:
                    conn.execute(
                        "DELETE FROM wishlist_snapshots WHERE app_id = ? AND date = ?",
                        (report.app_id, report.date),
                    )
                else:
                    existing = conn.execute(
                        "SELECT id, fetched_at FROM wishlist_snapshots WHERE app_id = ? AND date = ?",
                        (report.app_id, report.date),
                    ).fetchone()
                    if existing and str(existing["fetched_at"]).endswith("T23:59:59Z"):
                        snapshot_id = int(existing["id"])
                        conn.execute(
                            """
                            UPDATE wishlist_snapshots
                            SET adds = ?, deletes = ?, purchases = ?, gifts = ?,
                                adds_windows = ?, adds_mac = ?, adds_linux = ?, fetched_at = ?
                            WHERE id = ?
                            """,
                            (
                                report.adds,
                                report.deletes,
                                report.purchases,
                                report.gifts,
                                report.adds_windows,
                                report.adds_mac,
                                report.adds_linux,
                                fetched_at,
                                snapshot_id,
                            ),
                        )
                        conn.execute("DELETE FROM snapshot_countries WHERE snapshot_id = ?", (snapshot_id,))
                        self._save_countries(conn, snapshot_id, report.countries)
                        conn.execute("COMMIT")
                        return
                    if existing:
                        conn.execute("COMMIT")
                        return

                cur = conn.execute(
                    """
                    INSERT INTO wishlist_snapshots
                        (app_id, date, adds, deletes, purchases, gifts,
                         adds_windows, adds_mac, adds_linux, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.app_id,
                        report.date,
                        report.adds,
                        report.deletes,
                        report.purchases,
                        report.gifts,
                        report.adds_windows,
                        report.adds_mac,
                        report.adds_linux,
                        fetched_at,
                    ),
                )
                self._save_countries(conn, cur.lastrowid, report.countries)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_date_failed(self, app_id: int, date: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO backfill_failed_dates (app_id, date, fail_count, last_failed_at)
                VALUES (?, ?, 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(app_id, date) DO UPDATE SET
                    fail_count = fail_count + 1,
                    last_failed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """,
                (app_id, date),
            )

    def clear_failed_date(self, app_id: int, date: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM backfill_failed_dates WHERE app_id = ? AND date = ?", (app_id, date))

    def get_failed_dates(self, app_id: int) -> set[str]:
        with self.connect() as conn:
            return {row["date"] for row in conn.execute("SELECT date FROM backfill_failed_dates WHERE app_id = ?", (app_id,))}

    def start_sync(self, app_id: int, sync_type: str, requested_by: str, total_dates: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO game_sync_status
                    (app_id, sync_type, status, started_at, completed_at, total_dates, requested_by)
                VALUES (?, ?, 'in_progress', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), NULL, ?, ?)
                """,
                (app_id, sync_type, total_dates, requested_by),
            )

    def complete_sync(self, app_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE game_sync_status SET status = 'completed', completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE app_id = ?",
                (app_id,),
            )

    def fail_sync(self, app_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE game_sync_status SET status = 'failed', completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE app_id = ?",
                (app_id,),
            )

    def _row_to_sync(self, row: sqlite3.Row) -> GameSyncRow:
        return GameSyncRow(
            app_id=int(row["app_id"]),
            sync_type=row["sync_type"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            total_dates=int(row["total_dates"] or 0),
            requested_by=row["requested_by"],
        )

    def get_sync_status(self, app_id: int) -> GameSyncRow | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT app_id, sync_type, status, started_at, completed_at, total_dates, requested_by
                FROM game_sync_status WHERE app_id = ?
                """,
                (app_id,),
            ).fetchone()
            return self._row_to_sync(row) if row else None

    def get_all_sync_statuses(self) -> list[GameSyncRow]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT app_id, sync_type, status, started_at, completed_at, total_dates, requested_by
                FROM game_sync_status
                """
            ).fetchall()
            return [self._row_to_sync(row) for row in rows]

    def get_crawled_dates_count(self, app_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT date FROM wishlist_snapshots WHERE app_id = ?
                    UNION
                    SELECT date FROM crawled_dates WHERE app_id = ?
                )
                """,
                (app_id, app_id),
            ).fetchone()
            return int(row["c"] or 0)

    def clear_sync_progress(self, app_id: int) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM crawled_dates WHERE app_id = ?", (app_id,))
            conn.execute("DELETE FROM backfill_failed_dates WHERE app_id = ?", (app_id,))
            conn.execute("COMMIT")

    def store_app_min_date(self, app_id: int, min_date: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_info (app_id, name, image_url, min_date)
                VALUES (?, ?, '', ?)
                ON CONFLICT(app_id) DO UPDATE SET min_date = excluded.min_date
                """,
                (app_id, f"App {app_id}", min_date),
            )

    def get_app_min_date(self, app_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT min_date FROM app_info WHERE app_id = ?", (app_id,)).fetchone()
            return row["min_date"] if row else None

    def upsert_app_info(self, app_id: int, name: str, image_url: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_info (app_id, name, image_url) VALUES (?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET name = excluded.name, image_url = excluded.image_url
                """,
                (app_id, name, image_url),
            )

    def get_all_app_info(self) -> dict[int, tuple[str, str]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT app_id, name, image_url FROM app_info").fetchall()
            return {int(row["app_id"]): (row["name"], row["image_url"]) for row in rows}

    def get_latest_snapshots(self) -> list[WishlistReport]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id,
                       t.app_id,
                       COALESCE(s.date, '') AS date,
                       COALESCE(s.adds, 0) AS adds,
                       COALESCE(s.deletes, 0) AS deletes,
                       COALESCE(s.purchases, 0) AS purchases,
                       COALESCE(s.gifts, 0) AS gifts,
                       COALESCE(s.adds_windows, 0) AS adds_windows,
                       COALESCE(s.adds_mac, 0) AS adds_mac,
                       COALESCE(s.adds_linux, 0) AS adds_linux,
                       s.fetched_at
                FROM tracked_games t
                LEFT JOIN wishlist_snapshots s ON s.app_id = t.app_id
                   AND s.id = (
                       SELECT s2.id FROM wishlist_snapshots s2
                       WHERE s2.app_id = t.app_id
                       ORDER BY s2.fetched_at DESC
                       LIMIT 1
                   )
                ORDER BY t.tracked_since
                """
            ).fetchall()
            reports: list[WishlistReport] = []
            for row in rows:
                report = self._row_to_report(row)
                if row["id"] is not None:
                    report.countries = self._load_countries(conn, int(row["id"]))
                reports.append(report)
            return reports

    def get_snapshot_count(self, app_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM wishlist_snapshots WHERE app_id = ?", (app_id,)).fetchone()
            return int(row["c"] or 0)

    def get_game_totals(self, app_id: int) -> GameTotals:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(daily_adds), 0) AS adds,
                       COALESCE(SUM(daily_deletes), 0) AS deletes,
                       COALESCE(SUM(daily_purchases), 0) AS purchases,
                       COALESCE(SUM(daily_gifts), 0) AS gifts
                FROM (
                    SELECT MAX(adds) AS daily_adds,
                           MAX(deletes) AS daily_deletes,
                           MAX(purchases) AS daily_purchases,
                           MAX(gifts) AS daily_gifts
                    FROM wishlist_snapshots
                    WHERE app_id = ?
                    GROUP BY date
                )
                """,
                (app_id,),
            ).fetchone()
            return GameTotals(int(row["adds"] or 0), int(row["deletes"] or 0), int(row["purchases"] or 0), int(row["gifts"] or 0))

    def get_all_game_totals(self) -> dict[int, GameTotals]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT app_id,
                       COALESCE(SUM(daily_adds), 0) AS adds,
                       COALESCE(SUM(daily_deletes), 0) AS deletes,
                       COALESCE(SUM(daily_purchases), 0) AS purchases,
                       COALESCE(SUM(daily_gifts), 0) AS gifts
                FROM (
                    SELECT app_id,
                           MAX(adds) AS daily_adds,
                           MAX(deletes) AS daily_deletes,
                           MAX(purchases) AS daily_purchases,
                           MAX(gifts) AS daily_gifts
                    FROM wishlist_snapshots
                    GROUP BY app_id, date
                )
                GROUP BY app_id
                """
            ).fetchall()
            return {
                int(row["app_id"]): GameTotals(int(row["adds"] or 0), int(row["deletes"] or 0), int(row["purchases"] or 0), int(row["gifts"] or 0))
                for row in rows
            }

    def get_daily_maxes(self, app_id: int, lookback_days: int, exclude_date: str) -> list[DailyMax]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT date, MAX(adds) AS adds, MAX(deletes) AS deletes,
                       MAX(purchases) AS purchases, MAX(gifts) AS gifts
                FROM wishlist_snapshots
                WHERE app_id = ? AND date >= date('now', ?) AND date < ?
                GROUP BY date
                ORDER BY date ASC
                """,
                (app_id, f"-{lookback_days} days", exclude_date),
            ).fetchall()
            return [DailyMax(row["date"], int(row["adds"] or 0), int(row["deletes"] or 0), int(row["purchases"] or 0), int(row["gifts"] or 0)) for row in rows]

    def get_daily_country_maxes(self, app_id: int, lookback_days: int, exclude_date: str) -> dict[str, list[CountryDailyMax]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ws.date, sc.country_code, MAX(sc.adds) AS adds, MAX(sc.deletes) AS deletes
                FROM snapshot_countries sc
                JOIN wishlist_snapshots ws ON ws.id = sc.snapshot_id
                WHERE ws.app_id = ? AND ws.date >= date('now', ?) AND ws.date < ?
                GROUP BY ws.date, sc.country_code
                ORDER BY ws.date ASC
                """,
                (app_id, f"-{lookback_days} days", exclude_date),
            ).fetchall()
        result: dict[str, list[CountryDailyMax]] = {}
        for row in rows:
            result.setdefault(row["country_code"], []).append(CountryDailyMax(row["date"], int(row["adds"] or 0), int(row["deletes"] or 0)))
        return result

    def get_daily_max_for_date(self, app_id: int, date: str) -> DailyMax | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT date, MAX(adds) AS adds, MAX(deletes) AS deletes,
                       MAX(purchases) AS purchases, MAX(gifts) AS gifts
                FROM wishlist_snapshots
                WHERE app_id = ? AND date = ?
                GROUP BY date
                """,
                (app_id, date),
            ).fetchone()
            if not row:
                return None
            return DailyMax(row["date"], int(row["adds"] or 0), int(row["deletes"] or 0), int(row["purchases"] or 0), int(row["gifts"] or 0))

    def replace_snapshots_for_date(self, report: WishlistReport) -> None:
        fetched_at = f"{report.date}T23:59:59Z"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM wishlist_snapshots WHERE app_id = ? AND date = ?", (report.app_id, report.date))
            cur = conn.execute(
                """
                INSERT INTO wishlist_snapshots
                    (app_id, date, adds, deletes, purchases, gifts, adds_windows, adds_mac, adds_linux, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.app_id,
                    report.date,
                    report.adds,
                    report.deletes,
                    report.purchases,
                    report.gifts,
                    report.adds_windows,
                    report.adds_mac,
                    report.adds_linux,
                    fetched_at,
                ),
            )
            self._save_countries(conn, cur.lastrowid, report.countries)
            conn.execute("COMMIT")

    def get_chart_data(self, app_id: int, since: str, until: str, resolution: str) -> list[ChartPoint]:
        with self.connect() as conn:
            if resolution == "raw":
                query = """
                    SELECT COALESCE(fetched_at, date) AS label, adds, deletes, purchases, gifts,
                           adds_windows, adds_mac, adds_linux, date
                    FROM wishlist_snapshots
                    WHERE app_id = ? AND fetched_at >= ? AND fetched_at <= ?
                    ORDER BY fetched_at ASC
                """
            elif resolution == "daily":
                query = """
                    SELECT date AS label, MAX(adds) AS adds, MAX(deletes) AS deletes,
                           MAX(purchases) AS purchases, MAX(gifts) AS gifts,
                           MAX(adds_windows) AS adds_windows, MAX(adds_mac) AS adds_mac,
                           MAX(adds_linux) AS adds_linux, date
                    FROM wishlist_snapshots
                    WHERE app_id = ? AND fetched_at >= ? AND fetched_at <= ?
                    GROUP BY date
                    ORDER BY date ASC
                """
            elif resolution == "weekly":
                query = """
                    SELECT strftime('%Y-W%W', date) AS label,
                           SUM(daily_adds) AS adds, SUM(daily_deletes) AS deletes,
                           SUM(daily_purchases) AS purchases, SUM(daily_gifts) AS gifts,
                           SUM(daily_adds_windows) AS adds_windows,
                           SUM(daily_adds_mac) AS adds_mac,
                           SUM(daily_adds_linux) AS adds_linux,
                           strftime('%Y-W%W', date) AS date
                    FROM (
                        SELECT date, MAX(adds) AS daily_adds, MAX(deletes) AS daily_deletes,
                               MAX(purchases) AS daily_purchases, MAX(gifts) AS daily_gifts,
                               MAX(adds_windows) AS daily_adds_windows,
                               MAX(adds_mac) AS daily_adds_mac,
                               MAX(adds_linux) AS daily_adds_linux
                        FROM wishlist_snapshots
                        WHERE app_id = ? AND fetched_at >= ? AND fetched_at <= ?
                        GROUP BY date
                    )
                    GROUP BY strftime('%Y-W%W', date)
                    ORDER BY label ASC
                """
            elif resolution == "monthly":
                query = """
                    SELECT strftime('%Y-%m', date) AS label,
                           SUM(daily_adds) AS adds, SUM(daily_deletes) AS deletes,
                           SUM(daily_purchases) AS purchases, SUM(daily_gifts) AS gifts,
                           SUM(daily_adds_windows) AS adds_windows,
                           SUM(daily_adds_mac) AS adds_mac,
                           SUM(daily_adds_linux) AS adds_linux,
                           strftime('%Y-%m', date) AS date
                    FROM (
                        SELECT date, MAX(adds) AS daily_adds, MAX(deletes) AS daily_deletes,
                               MAX(purchases) AS daily_purchases, MAX(gifts) AS daily_gifts,
                               MAX(adds_windows) AS daily_adds_windows,
                               MAX(adds_mac) AS daily_adds_mac,
                               MAX(adds_linux) AS daily_adds_linux
                        FROM wishlist_snapshots
                        WHERE app_id = ? AND fetched_at >= ? AND fetched_at <= ?
                        GROUP BY date
                    )
                    GROUP BY strftime('%Y-%m', date)
                    ORDER BY label ASC
                """
            else:
                return []
            rows = conn.execute(query, (app_id, since, until)).fetchall()
            return [
                ChartPoint(
                    label=row["label"],
                    date=row["date"],
                    adds=int(row["adds"] or 0),
                    deletes=int(row["deletes"] or 0),
                    purchases=int(row["purchases"] or 0),
                    gifts=int(row["gifts"] or 0),
                    adds_windows=int(row["adds_windows"] or 0),
                    adds_mac=int(row["adds_mac"] or 0),
                    adds_linux=int(row["adds_linux"] or 0),
                )
                for row in rows
            ]

    def get_aggregated_countries(self, app_id: int, since: str, until: str) -> list[CountryReport]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT country_code,
                       SUM(daily_adds) AS adds, SUM(daily_deletes) AS deletes,
                       SUM(daily_purchases) AS purchases, SUM(daily_gifts) AS gifts
                FROM (
                    SELECT ws.date, sc.country_code,
                           MAX(sc.adds) AS daily_adds, MAX(sc.deletes) AS daily_deletes,
                           MAX(sc.purchases) AS daily_purchases, MAX(sc.gifts) AS daily_gifts
                    FROM snapshot_countries sc
                    INNER JOIN wishlist_snapshots ws ON ws.id = sc.snapshot_id
                    WHERE ws.app_id = ? AND ws.fetched_at >= ? AND ws.fetched_at <= ?
                    GROUP BY ws.date, sc.country_code
                )
                GROUP BY country_code
                ORDER BY SUM(daily_adds) DESC
                """,
                (app_id, since, until),
            ).fetchall()
            return [CountryReport(row["country_code"], int(row["adds"] or 0), int(row["deletes"] or 0), int(row["purchases"] or 0), int(row["gifts"] or 0)) for row in rows]

    def get_snapshots_paginated(self, app_id: int, page: int, per_page: int) -> tuple[list[tuple[int, WishlistReport]], int]:
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) AS c FROM wishlist_snapshots WHERE app_id = ?", (app_id,)).fetchone()["c"] or 0)
            offset = max(page - 1, 0) * per_page
            rows = conn.execute(
                """
                SELECT id, app_id, date, adds, deletes, purchases, gifts,
                       adds_windows, adds_mac, adds_linux, fetched_at
                FROM wishlist_snapshots
                WHERE app_id = ?
                ORDER BY fetched_at DESC
                LIMIT ? OFFSET ?
                """,
                (app_id, per_page, offset),
            ).fetchall()
            return [(int(row["id"]), self._row_to_report(row)) for row in rows], total

    def get_raw_snapshots_between(self, app_id: int, since: str, until: str) -> list[ChartPoint]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(fetched_at, date) AS label, adds, deletes, purchases, gifts,
                       adds_windows, adds_mac, adds_linux, date
                FROM wishlist_snapshots
                WHERE app_id = ? AND fetched_at >= ? AND fetched_at <= ?
                ORDER BY fetched_at ASC
                """,
                (app_id, since, until),
            ).fetchall()
            return [
                ChartPoint(
                    label=row["label"],
                    date=row["date"],
                    adds=int(row["adds"] or 0),
                    deletes=int(row["deletes"] or 0),
                    purchases=int(row["purchases"] or 0),
                    gifts=int(row["gifts"] or 0),
                    adds_windows=int(row["adds_windows"] or 0),
                    adds_mac=int(row["adds_mac"] or 0),
                    adds_linux=int(row["adds_linux"] or 0),
                )
                for row in rows
            ]

    def get_snapshot_countries(self, app_id: int, snapshot_id: int) -> list[CountryReport] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM wishlist_snapshots WHERE id = ? AND app_id = ?",
                (snapshot_id, app_id),
            ).fetchone()
            if not row or int(row["c"] or 0) == 0:
                return None
            return self._load_countries(conn, snapshot_id)
