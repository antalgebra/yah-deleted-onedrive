#!/usr/bin/env python3
"""Copy B2-archived Yahoo messages with deletion evidence into OneDrive.

This service deliberately has no Yahoo credentials. It consumes immutable audit
events and RFC822 messages from Backblaze B2 using a read-only key, then uses
rclone's copy-only operation to place one verified .eml per content hash in
OneDrive. It never deletes or moves anything in either destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


CONFIG_DIRECTORY = Path("/etc/yah-deleted-onedrive")
CONFIG_PATH = CONFIG_DIRECTORY / "exporter.env"
RCLONE_CONFIG_PATH = Path("/var/lib/yah-deleted-onedrive/rclone.conf")
STATE_DIRECTORY = Path("/var/lib/yah-deleted-onedrive")
DATABASE_PATH = STATE_DIRECTORY / "state.sqlite3"
TEMP_DIRECTORY = STATE_DIRECTORY / "tmp"

DEFAULT_POLL_SECONDS = 3600
DELETION_EVENT_TYPES = {
    "trash_observed",
    "trash_disappeared",
    "unexplained_disappearance",
}
ACCOUNT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MESSAGE_SUFFIX_PATTERN = re.compile(r"_([0-9a-f]{16})\.eml$", re.IGNORECASE)
REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
INVALID_ONEDRIVE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

LOG = logging.getLogger("yah-deleted-onedrive")
STOP_REQUESTED = False


class RetryableExportError(RuntimeError):
    """The event should remain queued for a later hourly attempt."""


def request_stop(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise RuntimeError(f"Missing configuration: {path}") from error

    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid setting at {path}:{number}")
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def require(config: dict[str, str], names: tuple[str, ...]) -> None:
    missing = [name for name in names if not config.get(name)]
    if missing:
        raise RuntimeError(f"Missing settings in {CONFIG_PATH}: {', '.join(missing)}")


def parse_accounts(value: str) -> tuple[str, ...]:
    accounts = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not accounts:
        raise RuntimeError("YAHOO_ACCOUNTS must contain at least one account ID")
    invalid = [account for account in accounts if not ACCOUNT_PATTERN.fullmatch(account)]
    if invalid:
        raise RuntimeError(f"Unsafe Yahoo account IDs: {', '.join(invalid)}")
    return accounts


def initialize_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS b2_events (
            b2_file_id TEXT PRIMARY KEY,
            b2_file_name TEXT NOT NULL UNIQUE,
            account TEXT NOT NULL,
            event_key TEXT,
            event_type TEXT,
            observed_at TEXT,
            sha256 TEXT,
            folder TEXT,
            status TEXT NOT NULL DEFAULT 'discovered',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            onedrive_path TEXT,
            discovered_at TEXT NOT NULL,
            processed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS b2_events_pending_idx
            ON b2_events(status, discovered_at);

        CREATE TABLE IF NOT EXISTS message_objects (
            account TEXT NOT NULL,
            sha_prefix TEXT NOT NULL,
            b2_file_name TEXT NOT NULL,
            b2_file_id TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            indexed_at TEXT NOT NULL,
            PRIMARY KEY (account, b2_file_name)
        );

        CREATE INDEX IF NOT EXISTS message_objects_hash_idx
            ON message_objects(account, sha_prefix);

        CREATE TABLE IF NOT EXISTS onedrive_copies (
            account TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            source_b2_name TEXT NOT NULL,
            onedrive_path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL,
            first_event_key TEXT NOT NULL,
            copied_at TEXT NOT NULL,
            PRIMARY KEY (account, sha256)
        );

        CREATE TABLE IF NOT EXISTS health_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return database


def set_health(database: sqlite3.Connection, key: str, value: str | None = None) -> None:
    now = iso_utc()
    database.execute(
        "INSERT INTO health_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value or now, now),
    )


def create_b2_bucket(config: dict[str, str]):
    from b2sdk.v3 import AuthInfoCache, B2Api, InMemoryAccountInfo

    info = InMemoryAccountInfo()
    api = B2Api(info, cache=AuthInfoCache(info))
    api.authorize_account(
        application_key_id=config["B2_KEY_ID"],
        application_key=config["B2_APPLICATION_KEY"],
    )
    return api.get_bucket_by_name(config["B2_BUCKET"])


def download_b2_file(bucket, b2_name: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        bucket.download_file_by_name(b2_name).save_to(str(partial))
        os.replace(partial, destination)
    finally:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass


def folder_is_ignored(folder: str | None) -> bool:
    if not folder:
        return False
    upper = folder.upper()
    return any(marker in upper for marker in ("BULK", "SPAM", "JUNK"))


def discover_events(
    database: sqlite3.Connection, bucket, accounts: tuple[str, ...]
) -> int:
    discovered = 0
    now = iso_utc()
    with database:
        for account in accounts:
            prefix = f"mail/{account}/events/"
            for version, _folder in bucket.ls(prefix, latest_only=True, recursive=True):
                name = str(version.file_name)
                if not name.endswith(".json"):
                    continue
                cursor = database.execute(
                    "INSERT OR IGNORE INTO b2_events("
                    "b2_file_id, b2_file_name, account, discovered_at"
                    ") VALUES (?, ?, ?, ?)",
                    (str(version.id_), name, account, now),
                )
                discovered += cursor.rowcount
        set_health(database, "last_event_discovery")
    return discovered


def parse_event(database: sqlite3.Connection, bucket, row: sqlite3.Row) -> sqlite3.Row:
    event_path = TEMP_DIRECTORY / "events" / f"{row['b2_file_id']}.json"
    try:
        download_b2_file(bucket, str(row["b2_file_name"]), event_path)
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    finally:
        try:
            event_path.unlink()
        except FileNotFoundError:
            pass

    event_key = str(payload.get("event_key") or "")
    event_type = str(payload.get("event_type") or "")
    observed_at = str(payload.get("observed_at") or "")
    sha256 = payload.get("sha256")
    sha256 = None if sha256 is None else str(sha256).lower()
    folder = payload.get("folder")
    folder = None if folder is None else str(folder)
    account = str(payload.get("account") or row["account"])

    if account != row["account"] or not ACCOUNT_PATTERN.fullmatch(account):
        raise RuntimeError("Audit event account does not match its B2 namespace")
    if sha256 is not None and not SHA256_PATTERN.fullmatch(sha256):
        raise RuntimeError("Audit event contains an invalid SHA-256 value")

    if event_type not in DELETION_EVENT_TYPES or folder_is_ignored(folder):
        status = "ignored"
    elif not sha256:
        status = "unavailable"
    else:
        status = "pending"

    with database:
        database.execute(
            "UPDATE b2_events SET event_key = ?, event_type = ?, observed_at = ?, "
            "sha256 = ?, folder = ?, status = ?, attempts = attempts + 1, "
            "last_error = NULL, processed_at = CASE WHEN ? IN "
            "('ignored', 'unavailable') THEN ? ELSE processed_at END "
            "WHERE b2_file_id = ?",
            (
                event_key,
                event_type,
                observed_at,
                sha256,
                folder,
                status,
                status,
                iso_utc(),
                row["b2_file_id"],
            ),
        )
    return database.execute(
        "SELECT * FROM b2_events WHERE b2_file_id = ?", (row["b2_file_id"],)
    ).fetchone()


def refresh_message_index(database: sqlite3.Connection, bucket, account: str) -> int:
    prefix = f"mail/{account}/messages/"
    indexed = 0
    now = iso_utc()
    with database:
        for version, _folder in bucket.ls(prefix, latest_only=True, recursive=True):
            name = str(version.file_name)
            match = MESSAGE_SUFFIX_PATTERN.search(name)
            if not match:
                continue
            database.execute(
                "INSERT INTO message_objects("
                "account, sha_prefix, b2_file_name, b2_file_id, size_bytes, indexed_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account, b2_file_name) DO UPDATE SET "
                "b2_file_id = excluded.b2_file_id, size_bytes = excluded.size_bytes, "
                "indexed_at = excluded.indexed_at",
                (
                    account,
                    match.group(1).lower(),
                    name,
                    str(version.id_),
                    int(version.size),
                    now,
                ),
            )
            indexed += 1
        set_health(database, f"last_message_index:{account}")
    return indexed


def candidate_message_rows(
    database: sqlite3.Connection, account: str, sha256: str
) -> list[sqlite3.Row]:
    return database.execute(
        "SELECT * FROM message_objects WHERE account = ? AND sha_prefix = ? "
        "ORDER BY b2_file_name",
        (account, sha256[:16]),
    ).fetchall()


def obtain_verified_message(
    database: sqlite3.Connection, bucket, account: str, sha256: str
) -> tuple[Path, sqlite3.Row]:
    destination = TEMP_DIRECTORY / "messages" / f"{sha256}.eml"
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() == sha256:
            rows = candidate_message_rows(database, account, sha256)
            if rows:
                return destination, rows[0]
        destination.unlink()

    rows = candidate_message_rows(database, account, sha256)
    if not rows:
        refresh_message_index(database, bucket, account)
        rows = candidate_message_rows(database, account, sha256)

    for row in rows:
        download_b2_file(bucket, str(row["b2_file_name"]), destination)
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        if actual == sha256:
            return destination, row
        destination.unlink()

    # A message could have arrived since the prior cached listing. Refresh once
    # more before leaving the event queued for the next hourly pass.
    refresh_message_index(database, bucket, account)
    for row in candidate_message_rows(database, account, sha256):
        download_b2_file(bucket, str(row["b2_file_name"]), destination)
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        if actual == sha256:
            return destination, row
        destination.unlink()

    raise RetryableExportError(
        f"No verified B2 message object found for account={account} sha256={sha256}"
    )


def safe_onedrive_component(value: str) -> str:
    value = INVALID_ONEDRIVE_CHARS.sub("-", value).strip().rstrip(".")
    return (value or "unnamed")[:180]


def event_year(observed_at: str | None) -> int:
    try:
        return datetime.fromisoformat(str(observed_at)).astimezone(timezone.utc).year
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).year


def make_onedrive_path(
    root: str, account: str, observed_at: str | None, b2_message_name: str
) -> str:
    basename = safe_onedrive_component(PurePosixPath(b2_message_name).name)
    clean_root = "/".join(
        safe_onedrive_component(part) for part in root.split("/") if part.strip()
    )
    return f"{clean_root}/{safe_onedrive_component(account)}/{event_year(observed_at)}/{basename}"


def run_rclone(config: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [
        "rclone",
        "--config",
        str(RCLONE_CONFIG_PATH),
        *arguments,
    ]
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=900,
    )


def copy_to_onedrive(
    config: dict[str, str], source: Path, relative_path: str
) -> None:
    remote_spec = f"{config['ONEDRIVE_REMOTE']}:{relative_path}"
    run_rclone(
        config,
        "copyto",
        str(source),
        remote_spec,
        "--immutable",
        "--retries",
        "3",
        "--low-level-retries",
        "10",
    )
    result = run_rclone(config, "lsjson", "--stat", remote_spec)
    metadata = json.loads(result.stdout)
    remote_size = int(metadata["Size"])
    if remote_size != source.stat().st_size:
        raise RuntimeError(
            f"OneDrive size verification failed: local={source.stat().st_size} "
            f"remote={remote_size}"
        )


def export_event(
    database: sqlite3.Connection,
    bucket,
    config: dict[str, str],
    row: sqlite3.Row,
) -> bool:
    sha256 = str(row["sha256"])
    existing = database.execute(
        "SELECT onedrive_path FROM onedrive_copies "
        "WHERE account = ? AND sha256 = ?",
        (row["account"], sha256),
    ).fetchone()
    if existing:
        with database:
            database.execute(
                "UPDATE b2_events SET status = 'copied', onedrive_path = ?, "
                "processed_at = ?, last_error = NULL WHERE b2_file_id = ?",
                (existing["onedrive_path"], iso_utc(), row["b2_file_id"]),
            )
        return False

    source, object_row = obtain_verified_message(
        database, bucket, str(row["account"]), sha256
    )
    relative_path = make_onedrive_path(
        config["ONEDRIVE_ROOT"],
        str(row["account"]),
        row["observed_at"],
        str(object_row["b2_file_name"]),
    )
    try:
        copy_to_onedrive(config, source, relative_path)
        with database:
            copied_at = iso_utc()
            database.execute(
                "INSERT INTO onedrive_copies("
                "account, sha256, source_b2_name, onedrive_path, size_bytes, "
                "first_event_key, copied_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["account"],
                    sha256,
                    object_row["b2_file_name"],
                    relative_path,
                    source.stat().st_size,
                    row["event_key"],
                    copied_at,
                ),
            )
            database.execute(
                "UPDATE b2_events SET status = 'copied', onedrive_path = ?, "
                "processed_at = ?, last_error = NULL WHERE b2_file_id = ?",
                (relative_path, copied_at, row["b2_file_id"]),
            )
            set_health(database, "last_successful_copy", copied_at)
    finally:
        try:
            source.unlink()
        except FileNotFoundError:
            pass
    LOG.info(
        "Copied deleted message: account=%s sha256=%s onedrive=%s",
        row["account"],
        sha256,
        relative_path,
    )
    return True


def process_events(
    database: sqlite3.Connection, bucket, config: dict[str, str]
) -> tuple[int, int]:
    parsed = 0
    copied = 0
    discovered_rows = database.execute(
        "SELECT * FROM b2_events WHERE status = 'discovered' "
        "ORDER BY discovered_at, b2_file_name"
    ).fetchall()
    for row in discovered_rows:
        try:
            parse_event(database, bucket, row)
            parsed += 1
        except Exception as error:
            with database:
                database.execute(
                    "UPDATE b2_events SET attempts = attempts + 1, last_error = ? "
                    "WHERE b2_file_id = ?",
                    (f"{type(error).__name__}: {error}"[:1000], row["b2_file_id"]),
                )
            LOG.exception("Could not parse B2 audit event: %s", row["b2_file_name"])

    pending_rows = database.execute(
        "SELECT * FROM b2_events WHERE status = 'pending' "
        "ORDER BY observed_at, b2_file_name"
    ).fetchall()
    for row in pending_rows:
        try:
            if export_event(database, bucket, config, row):
                copied += 1
        except Exception as error:
            with database:
                database.execute(
                    "UPDATE b2_events SET attempts = attempts + 1, last_error = ? "
                    "WHERE b2_file_id = ?",
                    (f"{type(error).__name__}: {error}"[:1000], row["b2_file_id"]),
                )
            LOG.exception(
                "OneDrive export remains queued: account=%s event=%s",
                row["account"],
                row["event_key"],
            )
    return parsed, copied


def run_cycle(
    database: sqlite3.Connection,
    bucket,
    config: dict[str, str],
    accounts: tuple[str, ...],
) -> None:
    discovered = discover_events(database, bucket, accounts)
    parsed, copied = process_events(database, bucket, config)
    with database:
        set_health(database, "last_successful_cycle")
    pending = database.execute(
        "SELECT COUNT(*) FROM b2_events WHERE status IN ('discovered', 'pending')"
    ).fetchone()[0]
    LOG.info(
        "Hourly cycle complete: discovered=%s parsed=%s copied=%s pending=%s",
        discovered,
        parsed,
        copied,
        pending,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy deletion-evidenced Yahoo messages from B2 to OneDrive"
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    config = load_env(CONFIG_PATH)
    require(
        config,
        (
            "B2_KEY_ID",
            "B2_APPLICATION_KEY",
            "B2_BUCKET",
            "YAHOO_ACCOUNTS",
            "ONEDRIVE_REMOTE",
            "ONEDRIVE_ROOT",
        ),
    )
    if not REMOTE_PATTERN.fullmatch(config["ONEDRIVE_REMOTE"]):
        raise RuntimeError("ONEDRIVE_REMOTE contains unsafe characters")
    accounts = parse_accounts(config["YAHOO_ACCOUNTS"])
    poll_seconds = max(60, int(config.get("POLL_SECONDS", DEFAULT_POLL_SECONDS)))

    TEMP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    database = initialize_database(DATABASE_PATH)
    bucket = create_b2_bucket(config)
    LOG.info(
        "Exporter started: bucket=%s accounts=%s OneDrive=%s:%s poll=%ss",
        config["B2_BUCKET"],
        ",".join(accounts),
        config["ONEDRIVE_REMOTE"],
        config["ONEDRIVE_ROOT"],
        poll_seconds,
    )

    try:
        while not STOP_REQUESTED:
            try:
                run_cycle(database, bucket, config, accounts)
            except Exception:
                LOG.exception("Hourly export cycle failed; all copies remain queued")
            if args.once:
                break
            for _ in range(poll_seconds):
                if STOP_REQUESTED:
                    break
                time.sleep(1)
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
