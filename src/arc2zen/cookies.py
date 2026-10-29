#!/usr/bin/env python3
"""Copy Arc (Chromium) cookies into Zen's cookies.sqlite.

Arc encrypts cookie values with a key kept in the macOS keychain item
"Arc Safe Storage". Each Arc profile used by a space is imported into the Zen
container chosen by the containers step, so logins stay separated per profile.
"""

import argparse
import hashlib
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import quote, urlsplit

from .containers import NO_CONTAINER, arc_profile_display_names, arc_space_profiles, ensure_zen_containers
from .profile_paths import resolve_arc_profile
from .sessions import backup_file, resolve_zen_profile


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "Arc Safe Storage"
KEYCHAIN_ACCOUNT = "Arc"
# Microseconds between 1601-01-01 (Chromium epoch) and 1970-01-01 (Unix epoch).
CHROMIUM_EPOCH_OFFSET_US = 11644473600 * 1_000_000
# Chromium prefixes the plaintext with SHA256(host_key) since cookie DB version 24.
DOMAIN_HASH_DB_VERSION = 24
# Firefox does not keep session cookies in cookies.sqlite; Arc restores them, so keep them for a while.
SESSION_COOKIE_LIFETIME_S = 30 * 24 * 3600
FIREFOX_SCHEME_HTTP = 1
FIREFOX_SCHEME_HTTPS = 2
CHROMIUM_SCHEME_NONSECURE = 1
CHROMIUM_SCHEME_SECURE = 2


class CookieDecryptor:
    """Decrypt Chromium "v10" cookie values on macOS."""

    def __init__(self, password: bytes):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
        self._cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))

    def decrypt(self, encrypted: bytes) -> bytes:
        if not encrypted.startswith(b"v10"):
            raise ValueError(f"Unsupported cookie encryption prefix: {encrypted[:3]!r}")
        decryptor = self._cipher.decryptor()
        plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
        padding = plain[-1]
        if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:
            raise ValueError("Bad padding; wrong keychain password?")
        return plain[:-padding]


def keychain_password() -> bytes:
    """Read the Arc Safe Storage password; macOS asks the user to allow access."""
    if sys.platform != "darwin":
        raise RuntimeError("Cookie import currently supports Arc on macOS only.")
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read \"{KEYCHAIN_SERVICE}\" from the keychain: {result.stderr.strip()}")
    return result.stdout.strip().encode("utf-8")


def chromium_to_unix_us(value: int) -> int:
    return max(0, value - CHROMIUM_EPOCH_OFFSET_US)


def scheme_map(source_scheme: int, is_secure: bool) -> int:
    if source_scheme == CHROMIUM_SCHEME_SECURE:
        return FIREFOX_SCHEME_HTTPS
    if source_scheme == CHROMIUM_SCHEME_NONSECURE:
        return FIREFOX_SCHEME_HTTP
    return FIREFOX_SCHEME_HTTPS if is_secure else FIREFOX_SCHEME_HTTP


def partition_key(top_frame_site_key: str) -> Optional[str]:
    """Convert a Chromium CHIPS site ("https://example.com") to Firefox's "(https,example.com)"."""
    if not top_frame_site_key:
        return None
    parts = urlsplit(top_frame_site_key)
    if not parts.scheme or not parts.hostname:
        return None
    return f"({parts.scheme},{parts.hostname})"


def origin_attributes(user_context_id: int, partition: Optional[str]) -> str:
    # Same key order as Firefox's OriginAttributes::CreateSuffix.
    params = []
    if user_context_id:
        params.append(f"userContextId={user_context_id}")
    if partition:
        params.append(f"partitionKey={quote(partition, safe='')}")
    return "^" + "&".join(params) if params else ""


def read_arc_cookies(cookies_path: Path, decryptor: CookieDecryptor) -> Iterator[Optional[Dict[str, Any]]]:
    """Yield decrypted cookies from a copy of an Arc profile Cookies database, or None when one fails."""
    with tempfile.TemporaryDirectory(prefix="arc-to-zen-cookies-") as temp_dir:
        copy_path = Path(temp_dir) / "Cookies"
        shutil.copy2(cookies_path, copy_path)
        con = sqlite3.connect(copy_path)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            db_version = int(row[0]) if row else 0
            rows = con.execute(
                "SELECT host_key, top_frame_site_key, name, value, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly, last_access_utc, has_expires, is_persistent, samesite, "
                "source_scheme, creation_utc, last_update_utc FROM cookies"
            ).fetchall()
        finally:
            con.close()

    for (
        host, top_frame_site_key, name, value, encrypted, path, expires_utc, is_secure, is_httponly,
        last_access_utc, has_expires, is_persistent, samesite, source_scheme, creation_utc, last_update_utc,
    ) in rows:
        if encrypted:
            try:
                plain = decryptor.decrypt(encrypted)
                if db_version >= DOMAIN_HASH_DB_VERSION:
                    if plain[:32] != hashlib.sha256(host.encode("utf-8")).digest():
                        raise ValueError(f"domain hash mismatch for {host}")
                    plain = plain[32:]
                value = plain.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as e:
                logger.debug(f"Skipping cookie {name} on {host}: {e}")
                yield None
                continue
        yield {
            "host": host,
            "top_frame_site_key": top_frame_site_key,
            "name": name,
            "value": value,
            "path": path,
            "expires_us": chromium_to_unix_us(expires_utc) if has_expires and is_persistent else None,
            "is_secure": int(bool(is_secure)),
            "is_httponly": int(bool(is_httponly)),
            "last_access_us": chromium_to_unix_us(last_access_utc),
            "creation_us": chromium_to_unix_us(creation_utc),
            "update_us": chromium_to_unix_us(last_update_utc or creation_utc),
            # Chromium: -1 unspecified, 0 none, 1 lax, 2 strict. Firefox: 0 none, 1 lax, 2 strict.
            "same_site": max(0, samesite),
            "scheme_map": scheme_map(source_scheme, bool(is_secure)),
        }


def import_profile_cookies(
    zen_con: sqlite3.Connection,
    cookies_path: Path,
    decryptor: CookieDecryptor,
    user_context_id: int,
) -> tuple[int, int, int]:
    """Insert one Arc profile's cookies. Returns (imported, expired, failed)."""
    now_us = int(time.time() * 1_000_000)
    session_expiry_ms = (now_us // 1000) + SESSION_COOKIE_LIFETIME_S * 1000
    imported = expired = failed = 0
    rows = []
    for cookie in read_arc_cookies(cookies_path, decryptor):
        if cookie is None:
            failed += 1
            continue

        if cookie["expires_us"] is not None:
            if cookie["expires_us"] <= now_us:
                expired += 1
                continue
            expiry_ms = cookie["expires_us"] // 1000
        else:
            expiry_ms = session_expiry_ms

        partition = partition_key(cookie["top_frame_site_key"])
        rows.append(
            (
                origin_attributes(user_context_id, partition),
                cookie["name"],
                cookie["value"],
                cookie["host"],
                cookie["path"],
                expiry_ms,
                cookie["last_access_us"],
                cookie["creation_us"],
                cookie["is_secure"],
                cookie["is_httponly"],
                cookie["same_site"],
                cookie["scheme_map"],
                int(partition is not None),
                cookie["update_us"],
            )
        )
        imported += 1

    zen_con.executemany(
        "INSERT OR REPLACE INTO moz_cookies (originAttributes, name, value, host, path, expiry, lastAccessed, "
        "creationTime, isSecure, isHttpOnly, inBrowserElement, sameSite, schemeMap, "
        "isPartitionedAttributeSet, updateTime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        rows,
    )
    return imported, expired, failed


def migrate_cookies(
    arc_profile: str | Path | None = None,
    zen_profile: str | Path | None = None,
    use_containers: bool = True,
    default_profile_container: bool = False,
    create_backups: bool = True,
    space_profile_overrides: Optional[Dict[str, str]] = None,
) -> bool:
    """Import cookies from every Arc profile used by a space into Zen."""
    profile = resolve_zen_profile(zen_profile)
    zen_cookies = profile / "cookies.sqlite"
    if not zen_cookies.exists():
        logger.error(f"❌ Zen cookie database not found: {zen_cookies}. Start and close Zen once first.")
        return False

    if use_containers:
        profile_containers = ensure_zen_containers(
            arc_profile,
            profile,
            default_profile_container=default_profile_container,
            create_backups=create_backups,
            space_profile_overrides=space_profile_overrides,
        )
    else:
        profile_containers = {arc_dir: NO_CONTAINER for arc_dir in set(arc_space_profiles(arc_profile).values())}

    user_data = resolve_arc_profile(arc_profile) / "User Data"
    display_names = arc_profile_display_names(arc_profile)
    decryptor = CookieDecryptor(keychain_password())

    zen_con = sqlite3.connect(zen_cookies)
    try:
        version = zen_con.execute("PRAGMA user_version").fetchone()[0]
        # Schema 16 switched moz_cookies.expiry from seconds to milliseconds.
        if version < 16:
            logger.error(f"❌ Unsupported Zen cookie schema version {version}; update Zen first.")
            return False

        if create_backups:
            zen_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            backup_file(zen_cookies, "cookies-backup", datetime.now().strftime("%Y%m%d-%H%M%S"))

        total = 0
        with zen_con:
            for arc_dir, user_context_id in sorted(profile_containers.items()):
                cookies_path = user_data / arc_dir / "Cookies"
                name = display_names.get(arc_dir, arc_dir)
                if not cookies_path.exists():
                    logger.info(f"   Arc {arc_dir} ({name}): no Cookies database")
                    continue
                imported, expired, failed = import_profile_cookies(zen_con, cookies_path, decryptor, user_context_id)
                target = "no container" if user_context_id == NO_CONTAINER else f"container {user_context_id}"
                logger.info(
                    f"   Arc {arc_dir} ({name}) -> {target}: "
                    f"imported {imported}, skipped {expired} expired, {failed} undecryptable"
                )
                total += imported
    finally:
        zen_con.close()

    logger.info(f"Done. Imported {total} cookies into {zen_cookies.name}.")
    return True


def main() -> bool:
    parser = argparse.ArgumentParser(description="Import Arc cookies into Zen.")
    parser.add_argument("--arc-profile", help="Path to the Arc profile root containing StorableSidebar.json.")
    parser.add_argument(
        "--zen-profile",
        help="Path to a Zen profile directory, or a Zen root containing profiles.ini.",
    )
    parser.add_argument(
        "--no-containers",
        action="store_true",
        help="Import every Arc profile's cookies without a container.",
    )
    parser.add_argument(
        "--default-profile-container",
        action="store_true",
        help="Also put the Arc Default profile into its own container instead of no container.",
    )
    parser.add_argument("--no-backups", action="store_true", help="Do not create backups before changing Zen files.")
    args = parser.parse_args()
    return migrate_cookies(
        args.arc_profile,
        args.zen_profile,
        use_containers=not args.no_containers,
        default_profile_container=args.default_profile_container,
        create_backups=not args.no_backups,
    )


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
