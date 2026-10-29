#!/usr/bin/env python3
"""Map Arc profiles to Zen (Firefox) containers.

Every Arc space belongs to an Arc profile (``User Data/Default`` or
``User Data/Profile N``). Zen has no profiles inside one browser profile, but
Firefox containers give each one its own cookie jar. This step creates one Zen
container per Arc profile used by a space, sets it as the workspace default
container, and moves the workspace tabs into it.
"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .profile_paths import arc_json_path, resolve_arc_profile
from .sessions import backup_file, read_mozilla_lz4, resolve_zen_profile, write_mozilla_lz4


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ARC_DEFAULT_PROFILE = "Default"
NO_CONTAINER = 0
# Firefox reserves this id for extension storage; never allocate around it.
INTERNAL_USER_CONTEXT_ID = 4294967295
CONTAINER_COLORS = ["blue", "turquoise", "green", "yellow", "orange", "red", "pink", "purple"]
CONTAINER_ICON = "circle"


def alternating_pairs(values: list[Any]) -> Iterable[tuple[str, Any]]:
    for i in range(0, len(values) - 1, 2):
        if isinstance(values[i], str) and isinstance(values[i + 1], dict):
            yield values[i], values[i + 1]


def arc_space_profiles(arc_profile: str | Path | None = None) -> Dict[str, str]:
    """Return Arc space name -> Arc profile directory name (e.g. "Profile 8")."""
    with open(arc_json_path("StorableSidebar.json", arc_profile), encoding="utf-8") as f:
        sidebar = json.load(f)

    space_models = sidebar.get("firebaseSyncState", {}).get("syncData", {}).get("spaceModels", [])
    result = {}
    for _, model in alternating_pairs(space_models):
        space = model.get("value", {})
        name = space.get("title")
        if not name:
            continue
        custom = space.get("profile", {}).get("custom", {}).get("_0", {})
        result[name] = custom.get("directoryBasename") or ARC_DEFAULT_PROFILE
    return result


def arc_profile_display_names(arc_profile: str | Path | None = None) -> Dict[str, str]:
    """Return Arc profile directory name -> user-visible profile name from Local State."""
    local_state = resolve_arc_profile(arc_profile) / "User Data" / "Local State"
    if not local_state.exists():
        return {}
    with open(local_state, encoding="utf-8") as f:
        info_cache = json.load(f).get("profile", {}).get("info_cache", {})
    return {directory: info.get("name") or directory for directory, info in info_cache.items()}


def load_containers(path: Path) -> Dict[str, Any]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 5, "lastUserContextId": 0, "identities": []}


def next_user_context_id(data: Dict[str, Any]) -> int:
    ids = [
        identity.get("userContextId", 0)
        for identity in data.get("identities", [])
        if identity.get("userContextId", 0) < INTERNAL_USER_CONTEXT_ID
    ]
    return max([data.get("lastUserContextId", 0), *ids]) + 1


def ensure_zen_containers(
    arc_profile: str | Path | None = None,
    zen_profile: str | Path | None = None,
    default_profile_container: bool = False,
    create_backups: bool = True,
) -> Dict[str, int]:
    """Create Zen containers for Arc profiles and return Arc profile dir -> userContextId.

    The Arc ``Default`` profile maps to "no container" unless
    ``default_profile_container`` is set, so links opened from other apps still
    see its cookies.
    """
    profile = resolve_zen_profile(zen_profile)
    space_profiles = arc_space_profiles(arc_profile)
    display_names = arc_profile_display_names(arc_profile)
    containers_path = profile / "containers.json"
    data = load_containers(containers_path)
    identities = data.setdefault("identities", [])

    mapping: Dict[str, int] = {}
    created = 0
    for arc_dir in sorted(set(space_profiles.values())):
        if arc_dir == ARC_DEFAULT_PROFILE and not default_profile_container:
            mapping[arc_dir] = NO_CONTAINER
            continue

        name = display_names.get(arc_dir, arc_dir)
        existing = next((i for i in identities if i.get("public", True) and i.get("name") == name), None)
        if existing:
            mapping[arc_dir] = existing["userContextId"]
            continue

        user_context_id = next_user_context_id(data)
        identities.append(
            {
                "userContextId": user_context_id,
                "public": True,
                "icon": CONTAINER_ICON,
                "color": CONTAINER_COLORS[created % len(CONTAINER_COLORS)],
                "name": name,
                "accessKey": "",
            }
        )
        data["lastUserContextId"] = user_context_id
        mapping[arc_dir] = user_context_id
        created += 1
        logger.info(f"   Created container \"{name}\" (userContextId={user_context_id}) for Arc {arc_dir}")

    if created:
        if create_backups:
            backup_file(containers_path, "containers-backup", datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(containers_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    for arc_dir, user_context_id in mapping.items():
        label = "no container" if user_context_id == NO_CONTAINER else f"userContextId={user_context_id}"
        logger.info(f"   Arc {arc_dir} ({display_names.get(arc_dir, arc_dir)}) -> {label}")
    return mapping


def session_windows(data: Dict[str, Any]) -> list[Dict[str, Any]]:
    if "windows" in data:
        return data.get("windows", [])
    return [data]


def apply_containers(data: Dict[str, Any], space_containers: Dict[str, int]) -> int:
    """Set workspace default containers and tab containers by workspace name."""
    changed = 0
    for window in session_windows(data):
        container_by_uuid = {}
        for space in window.get("spaces", []):
            user_context_id = space_containers.get(space.get("name"))
            if user_context_id is None:
                continue
            container_by_uuid[space.get("uuid")] = user_context_id
            if space.get("containerTabId") != user_context_id:
                space["containerTabId"] = user_context_id
                changed += 1

        for tab in window.get("tabs", []):
            user_context_id = container_by_uuid.get(tab.get("zenWorkspace"))
            if user_context_id is None or tab.get("userContextId", 0) == user_context_id:
                continue
            tab["userContextId"] = user_context_id
            changed += 1
    return changed


def sync_containers(
    arc_profile: str | Path | None = None,
    zen_profile: str | Path | None = None,
    default_profile_container: bool = False,
    create_backups: bool = True,
) -> bool:
    """Create Zen containers for Arc profiles and assign workspaces and tabs to them."""
    profile = resolve_zen_profile(zen_profile)
    profile_containers = ensure_zen_containers(
        arc_profile, profile, default_profile_container=default_profile_container, create_backups=create_backups
    )
    space_containers = {
        name: profile_containers[arc_dir] for name, arc_dir in arc_space_profiles(arc_profile).items()
    }

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    files = [
        profile / "zen-sessions.jsonlz4",
        profile / "sessionstore.jsonlz4",
        profile / "sessionstore-backups" / "recovery.jsonlz4",
    ]
    total = 0
    for path in files:
        if not path.exists():
            logger.info(f"Skipping missing file: {path}")
            continue
        data = read_mozilla_lz4(path)
        changed = apply_containers(data, space_containers)
        if changed:
            if create_backups:
                backup_file(path, "containers-backup", timestamp)
            write_mozilla_lz4(path, data, create_backup=False)
        logger.info(f"{path.name}: changed {changed} workspace/tab containers")
        total += changed

    logger.info(f"Done. Changed {total} container fields across Zen session files.")
    return True


def main() -> bool:
    parser = argparse.ArgumentParser(description="Map Arc profiles to Zen containers.")
    parser.add_argument("--arc-profile", help="Path to the Arc profile root containing StorableSidebar.json.")
    parser.add_argument(
        "--zen-profile",
        help="Path to a Zen profile directory, or a Zen root containing profiles.ini.",
    )
    parser.add_argument(
        "--default-profile-container",
        action="store_true",
        help="Also put the Arc Default profile into its own container instead of no container.",
    )
    parser.add_argument("--no-backups", action="store_true", help="Do not create backups before changing Zen files.")
    args = parser.parse_args()
    return sync_containers(
        args.arc_profile,
        args.zen_profile,
        default_profile_container=args.default_profile_container,
        create_backups=not args.no_backups,
    )


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
