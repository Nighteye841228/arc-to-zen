#!/usr/bin/env python3
"""Map Arc profiles to Zen (Firefox) containers.

Every Arc space belongs to an Arc profile (``User Data/Default`` or
``User Data/Profile N``). Zen has no profiles inside one browser profile, but
Firefox containers give each one its own cookie jar. This step creates one Zen
container per Arc profile used by a space or by Arc Favorites, sets it as the
workspace default container, moves the workspace tabs into it, and puts each
Essential into the container of the Arc profile it came from. Zen only shows
Essentials per container when ``zen.workspaces.separate-essentials`` is on, so
this step turns it on as well.
"""

import argparse
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .extract import ARC_DEFAULT_PROFILE, arc_profile_dir
from .profile_paths import arc_json_path, resolve_arc_profile
from .sessions import backup_file, read_mozilla_lz4, resolve_zen_profile, write_mozilla_lz4


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

NO_CONTAINER = 0
# Firefox reserves this id for extension storage; never allocate around it.
INTERNAL_USER_CONTEXT_ID = 4294967295
CONTAINER_COLORS = ["blue", "turquoise", "green", "yellow", "orange", "red", "pink", "purple"]
CONTAINER_ICON = "circle"
SEPARATE_ESSENTIALS_PREFS = (
    "zen.workspaces.separate-essentials",
    "zen.workspaces.container-specific-essentials-enabled",
)


def alternating_pairs(values: list[Any]) -> Iterable[tuple[str, Any]]:
    for i in range(0, len(values) - 1, 2):
        if isinstance(values[i], str) and isinstance(values[i + 1], dict):
            yield values[i], values[i + 1]


def load_arc_sidebar(arc_profile: str | Path | None = None) -> Dict[str, Any]:
    with open(arc_json_path("StorableSidebar.json", arc_profile), encoding="utf-8") as f:
        return json.load(f)


def global_sidebar_container(sidebar: Dict[str, Any]) -> Dict[str, Any]:
    containers = sidebar.get("sidebar", {}).get("containers", [])
    for i in range(0, len(containers) - 1, 2):
        if isinstance(containers[i], dict) and "global" in containers[i] and isinstance(containers[i + 1], dict):
            return containers[i + 1]
    return {}


def export_favorite_profiles(export_file: str | Path) -> list[tuple[str, str]]:
    """(URL, Arc profile) of the exported Essentials, in the order the import wrote them to Zen."""
    with open(export_file, encoding="utf-8") as f:
        export = json.load(f)
    return [
        (tab["url"], tab.get("profile") or ARC_DEFAULT_PROFILE)
        for space in export.get("spaces", [])
        for tab in space.get("pinned_tabs", [])
        if tab.get("is_essential")
    ]


def resolve_space_profile_overrides(
    overrides: Optional[Dict[str, str]], display_names: Dict[str, str]
) -> Dict[str, str]:
    """Accept Arc profile directories ("Profile 1") or display names ("Work") as override values."""
    by_name = {name: directory for directory, name in display_names.items()}
    resolved = {}
    for space, value in (overrides or {}).items():
        if value in display_names or value == ARC_DEFAULT_PROFILE:
            resolved[space] = value
        elif value in by_name:
            resolved[space] = by_name[value]
        else:
            raise ValueError(f"Unknown Arc profile \"{value}\" for space \"{space}\"")
    return resolved


def arc_space_profiles(
    arc_profile: str | Path | None = None,
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return Arc space name -> Arc profile directory name (e.g. "Profile 8")."""
    sidebar = load_arc_sidebar(arc_profile)
    space_models = sidebar.get("firebaseSyncState", {}).get("syncData", {}).get("spaceModels", [])
    result = {}
    for _, model in alternating_pairs(space_models):
        space = model.get("value", {})
        name = space.get("title")
        if name:
            result[name] = arc_profile_dir(space.get("profile"))
    result.update(resolve_space_profile_overrides(overrides, arc_profile_display_names(arc_profile)))
    return result


def arc_favorite_profiles(arc_profile: str | Path | None = None) -> list[tuple[str, str]]:
    """Return (Favorite URL, Arc profile directory) pairs in Arc order, from the per-profile Favorites bars."""
    container = global_sidebar_container(load_arc_sidebar(arc_profile))
    items = {item_id: item for item_id, item in alternating_pairs(container.get("items", []))}

    def tab_urls(parent_id: str, depth: int = 0) -> Iterable[str]:
        if depth > 32:
            return
        children = [items[i] for i in items.get(parent_id, {}).get("childrenIds", []) if i in items]
        if not children:
            children = [item for item in items.values() if item.get("parentID") == parent_id]
        for item in children:
            data = item.get("data", {})
            if data.get("tab", {}).get("savedURL"):
                yield data["tab"]["savedURL"]
            elif "list" in data or "splitView" in data:
                yield from tab_urls(item.get("id"), depth + 1)

    result = []
    top_apps = container.get("topAppsContainerIDs", [])
    for i in range(0, len(top_apps) - 1, 2):
        if isinstance(top_apps[i + 1], str):
            profile = arc_profile_dir(top_apps[i])
            result.extend((url, profile) for url in tab_urls(top_apps[i + 1]))
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
    space_profile_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """Create Zen containers for Arc profiles and return Arc profile dir -> userContextId.

    The Arc ``Default`` profile maps to "no container" unless
    ``default_profile_container`` is set, so links opened from other apps still
    see its cookies.
    """
    profile = resolve_zen_profile(zen_profile)
    used_profiles = set(arc_space_profiles(arc_profile, space_profile_overrides).values())
    used_profiles |= {arc_dir for _, arc_dir in arc_favorite_profiles(arc_profile)}
    display_names = arc_profile_display_names(arc_profile)
    containers_path = profile / "containers.json"
    data = load_containers(containers_path)
    identities = data.setdefault("identities", [])

    mapping: Dict[str, int] = {}
    created = 0
    for arc_dir in sorted(used_profiles):
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


def apply_containers(
    data: Dict[str, Any],
    space_containers: Dict[str, int],
    essential_containers: Optional[list[tuple[str, int]]] = None,
) -> int:
    """Set workspace default containers, tab containers by workspace, and Essentials by Arc profile.

    ``essential_containers`` lists (URL, container) in Arc order; the same URL
    can be a Favorite of several profiles, so Essentials are matched in order.
    """
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

        pending: Dict[str, list[int]] = {}
        for url, user_context_id in essential_containers or []:
            pending.setdefault(url, []).append(user_context_id)

        for tab in window.get("tabs", []):
            if tab.get("zenEssential"):
                entries = tab.get("entries") or [{}]
                queue = pending.get(entries[0].get("url")) or [None]
                user_context_id = queue.pop(0) if len(queue) > 1 else queue[0]
            else:
                user_context_id = container_by_uuid.get(tab.get("zenWorkspace"))
            if user_context_id is None or tab.get("userContextId", 0) == user_context_id:
                continue
            tab["userContextId"] = user_context_id
            changed += 1
    return changed


def set_user_prefs(profile: Path, prefs: Dict[str, bool], create_backups: bool = True) -> int:
    """Set boolean prefs in prefs.js. Zen must be closed, or it overwrites the file on exit."""
    path = profile / "prefs.js"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = 0
    for name, value in prefs.items():
        line = f'user_pref("{name}", {"true" if value else "false"});'
        pattern = re.compile(rf'^user_pref\("{re.escape(name)}",.*\);$', re.MULTILINE)
        if pattern.search(text):
            new_text = pattern.sub(line, text)
        else:
            new_text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        if new_text != text:
            text = new_text
            changed += 1
    if changed:
        if create_backups:
            backup_file(path, "prefs-backup", datetime.now().strftime("%Y%m%d-%H%M%S"))
        path.write_text(text, encoding="utf-8")
    return changed


def sync_containers(
    arc_profile: str | Path | None = None,
    zen_profile: str | Path | None = None,
    default_profile_container: bool = False,
    create_backups: bool = True,
    space_profile_overrides: Optional[Dict[str, str]] = None,
    export_file: str | Path | None = None,
) -> bool:
    """Create Zen containers for Arc profiles and assign workspaces, tabs and Essentials to them.

    With ``export_file`` Essentials are matched in import order, which keeps a
    URL that is a Favorite of several Arc profiles in the right containers.
    """
    profile = resolve_zen_profile(zen_profile)
    profile_containers = ensure_zen_containers(
        arc_profile,
        profile,
        default_profile_container=default_profile_container,
        create_backups=create_backups,
        space_profile_overrides=space_profile_overrides,
    )
    space_containers = {
        name: profile_containers[arc_dir]
        for name, arc_dir in arc_space_profiles(arc_profile, space_profile_overrides).items()
    }
    essential_containers = [
        (url, profile_containers[arc_dir])
        for url, arc_dir in (
            export_favorite_profiles(export_file)
            if export_file and Path(export_file).exists()
            else arc_favorite_profiles(arc_profile)
        )
    ]
    # Arc shows only a profile's own Favorites in its spaces; Zen does the same per container with this pref.
    if (set(space_containers.values()) | {c for _, c in essential_containers}) - {NO_CONTAINER}:
        changed = set_user_prefs(profile, dict.fromkeys(SEPARATE_ESSENTIALS_PREFS, True), create_backups)
        logger.info(f"prefs.js: {'enabled' if changed else 'already has'} per-container Essentials")

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
        changed = apply_containers(data, space_containers, essential_containers)
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
    add_space_profile_argument(parser)
    parser.add_argument("--no-backups", action="store_true", help="Do not create backups before changing Zen files.")
    args = parser.parse_args()
    return sync_containers(
        args.arc_profile,
        args.zen_profile,
        default_profile_container=args.default_profile_container,
        create_backups=not args.no_backups,
        space_profile_overrides=parse_space_profiles(args.space_profile),
    )


def add_space_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--space-profile",
        action="append",
        default=[],
        metavar="SPACE=PROFILE",
        help='Put an Arc space into another Arc profile\'s container, e.g. "工作=Work" or "工作=Profile 1". Repeatable.',
    )


def parse_space_profiles(values: Iterable[str]) -> Dict[str, str]:
    result = {}
    for value in values:
        space, sep, profile = value.partition("=")
        if not sep or not space.strip() or not profile.strip():
            raise ValueError(f"--space-profile expects SPACE=PROFILE, got \"{value}\"")
        result[space.strip()] = profile.strip()
    return result


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
