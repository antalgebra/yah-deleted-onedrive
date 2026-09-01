#!/usr/bin/env python3
"""One-time or repeatable onboarding for the deleted-mail OneDrive exporter."""

from __future__ import annotations

import getpass
import grp
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path


CONFIG_DIRECTORY = Path("/etc/yah-deleted-onedrive")
CONFIG_PATH = CONFIG_DIRECTORY / "exporter.env"
RCLONE_CONFIG_PATH = CONFIG_DIRECTORY / "rclone.conf"
YAHOO_ACCOUNT_DIRECTORY = Path("/etc/yah-arch/accounts")
SERVICE_USER = "yahdeleted"
SERVICE_NAME = "yah-deleted-onedrive.service"
ACCOUNT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def load_existing() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
    return values


def prompt(label: str, current: str = "") -> str:
    suffix = f" [{current}]" if current else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or current


def secret_prompt(current_exists: bool) -> str:
    suffix = " (press Enter to keep current)" if current_exists else ""
    return getpass.getpass(f"B2 application key{suffix}: ").strip()


def discover_accounts() -> tuple[str, ...]:
    if not YAHOO_ACCOUNT_DIRECTORY.is_dir():
        return ()
    accounts = sorted(
        path.stem
        for path in YAHOO_ACCOUNT_DIRECTORY.glob("*.env")
        if ACCOUNT_PATTERN.fullmatch(path.stem)
    )
    return tuple(accounts)


def validate_b2(key_id: str, application_key: str, bucket_name: str) -> None:
    from b2sdk.v3 import AuthInfoCache, B2Api, InMemoryAccountInfo

    info = InMemoryAccountInfo()
    api = B2Api(info, cache=AuthInfoCache(info))
    api.authorize_account(
        application_key_id=key_id,
        application_key=application_key,
    )
    allowed = info.get_allowed()
    capabilities = set(allowed.get("capabilities") or ())
    missing = {"listFiles", "readFiles"} - capabilities
    if missing:
        raise RuntimeError(
            "The B2 key must be Read Only and include listFiles/readFiles"
        )
    api.get_bucket_by_name(bucket_name)


def atomic_write_config(values: dict[str, str]) -> None:
    account = pwd.getpwnam(SERVICE_USER)
    group = grp.getgrnam(SERVICE_USER)
    CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    content = "".join(f"{name}={value}\n" for name, value in values.items())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(temporary, 0, group.gr_gid)
    os.replace(temporary, CONFIG_PATH)
    os.chown(CONFIG_DIRECTORY, 0, group.gr_gid)
    os.chmod(CONFIG_DIRECTORY, 0o750)
    os.chown(RCLONE_CONFIG_PATH, account.pw_uid, group.gr_gid)
    os.chmod(RCLONE_CONFIG_PATH, 0o600)


def service_command(
    *arguments: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    account = pwd.getpwnam(SERVICE_USER)
    environment = os.environ.copy()
    environment["HOME"] = account.pw_dir
    return subprocess.run(
        ["runuser", "-u", SERVICE_USER, "--", *arguments],
        check=check,
        text=True,
        env=environment,
        capture_output=capture,
    )


def ensure_rclone_remote(remote: str) -> None:
    result = service_command(
        "rclone",
        "--config",
        str(RCLONE_CONFIG_PATH),
        "listremotes",
        check=False,
        capture=True,
    )
    remotes = set(result.stdout.splitlines()) if result.stdout else set()
    if f"{remote}:" in remotes:
        print(f"Using existing rclone remote: {remote}")
        return

    print("\nMicrosoft authorization will now be handled by rclone.")
    print(f"Create a remote named exactly: {remote}")
    print("Choose Microsoft OneDrive; Personal and Business are both supported.")
    print("Leave the Microsoft client ID and secret blank.")
    service_command(
        "rclone",
        "--config",
        str(RCLONE_CONFIG_PATH),
        "config",
    )
    verify = service_command(
        "rclone",
        "--config",
        str(RCLONE_CONFIG_PATH),
        "listremotes",
        check=False,
        capture=True,
    )
    if f"{remote}:" not in set((verify.stdout or "").splitlines()):
        raise RuntimeError(f"rclone remote {remote!r} was not created")


def main() -> int:
    if os.geteuid() != 0:
        print("Run this wizard with sudo.", file=sys.stderr)
        return 1

    try:
        pwd.getpwnam(SERVICE_USER)
    except KeyError:
        print("Run deploy/install.sh before this wizard.", file=sys.stderr)
        return 1

    existing = load_existing()
    detected = discover_accounts()
    existing_accounts = existing.get("YAHOO_ACCOUNTS", "")
    accounts = detected or tuple(
        part for part in existing_accounts.split(",") if ACCOUNT_PATTERN.fullmatch(part)
    )
    if not accounts:
        entered = prompt("Yahoo account IDs, comma separated")
        accounts = tuple(part.strip() for part in entered.split(",") if part.strip())
    if not accounts or any(not ACCOUNT_PATTERN.fullmatch(value) for value in accounts):
        raise RuntimeError("No valid Yahoo account IDs were found")

    print("Yahoo Deleted Mail → OneDrive onboarding\n")
    print("Detected Yahoo accounts: " + ", ".join(accounts))
    print("Create a Backblaze key restricted to the archive bucket with Read Only access.")
    key_id = prompt("B2 key ID", existing.get("B2_KEY_ID", ""))
    application_key = secret_prompt(bool(existing.get("B2_APPLICATION_KEY")))
    if not application_key:
        application_key = existing.get("B2_APPLICATION_KEY", "")
    bucket = prompt("B2 bucket", existing.get("B2_BUCKET", ""))
    if not key_id or not application_key or not bucket:
        raise RuntimeError("B2 key ID, application key, and bucket are required")

    print("Validating read-only Backblaze access...")
    validate_b2(key_id, application_key, bucket)
    print("Backblaze access verified.")

    remote = prompt("rclone OneDrive remote name", existing.get("ONEDRIVE_REMOTE", "onedrive"))
    root = prompt(
        "OneDrive destination folder",
        existing.get("ONEDRIVE_ROOT", "Yahoo Deleted Mail"),
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", remote):
        raise RuntimeError("The rclone remote name may contain letters, numbers, _ and -")
    if not root.strip(" /"):
        raise RuntimeError("The OneDrive destination folder cannot be empty")

    values = {
        "B2_KEY_ID": key_id,
        "B2_APPLICATION_KEY": application_key,
        "B2_BUCKET": bucket,
        "YAHOO_ACCOUNTS": ",".join(accounts),
        "ONEDRIVE_REMOTE": remote,
        "ONEDRIVE_ROOT": root.strip(" /"),
        "POLL_SECONDS": "3600",
    }
    atomic_write_config(values)
    ensure_rclone_remote(remote)

    print("Verifying OneDrive write access...")
    service_command(
        "rclone",
        "--config",
        str(RCLONE_CONFIG_PATH),
        "mkdir",
        f"{remote}:{root.strip(' /')}",
    )
    print("OneDrive access verified.")

    subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME], check=True)
    subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True)
    print(f"\nExporter started: {SERVICE_NAME}")
    print("It scans immediately after startup and then once per hour.")
    print(f"OneDrive folder: {root.strip(' /')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nOnboarding cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"\nOnboarding failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
