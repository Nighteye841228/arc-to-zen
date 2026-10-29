#!/usr/bin/env python3
"""Install Firefox versions of Arc's Chrome extensions into Zen.

Chrome extensions cannot run in Zen, so each enabled Arc extension is looked
up on addons.mozilla.org (AMO): first in AMO's Chrome-to-Firefox mapping list,
then by name or homepage. Matches are downloaded as signed XPIs into the Zen
profile's ``extensions`` folder. Zen asks once, on its next start, whether to
enable each one. Extensions without a match are listed for manual install.
"""

import argparse
import glob
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .containers import arc_favorite_profiles, arc_space_profiles
from .profile_paths import resolve_arc_profile
from .sessions import resolve_zen_profile


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

AMO_API = "https://addons.mozilla.org/api/v5"
USER_AGENT = "arc2zen (https://github.com/Thinkscape/arc-to-zen)"
# Chromium Manifest::Location values for extensions the user installed.
USER_INSTALLED_LOCATIONS = {1, 2, 3, 6, 7, 8, 9}
# Arc extension GUID -> add-ons that already cover it in Zen.
SUPERSEDED_BY = {"uBOLiteRedux@raymondhill.net": {"uBlock0@raymondhill.net"}}
NAME_SEGMENTS = re.compile(r"\s+[-–—:|]\s+")
NAME_SUFFIXES = re.compile(r"\s*[\(\[]?\b(beta|dev|lite redux)\b[\)\]]?\s*$", re.IGNORECASE)


@dataclass
class ArcExtension:
    chrome_id: str
    name: str
    homepage: Optional[str]
    profiles: List[str]


def http_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def localized_manifest_name(manifest: Dict[str, Any], extension_dir: Path) -> str:
    """Resolve "__MSG_name__", preferring English so names match AMO listings."""
    name = manifest.get("name", "")
    match = re.fullmatch(r"__MSG_(\w+)__", name)
    if not match:
        return name
    for locale in ("en", "en_US", "en_GB", manifest.get("default_locale")):
        path = extension_dir / "_locales" / str(locale) / "messages.json"
        if locale and path.exists():
            with open(path, encoding="utf-8-sig") as f:
                messages = {key.lower(): value for key, value in json.load(f).items()}
            message = messages.get(match.group(1).lower(), {}).get("message")
            if message:
                return message
    return name


def arc_extensions(arc_profile: str | Path | None, arc_profiles: Iterable[str]) -> List[ArcExtension]:
    """Enabled, user-installed extensions of the given Arc profiles, merged by Chrome id."""
    user_data = resolve_arc_profile(arc_profile) / "User Data"
    found: Dict[str, ArcExtension] = {}
    for arc_dir in sorted(arc_profiles):
        settings = {}
        for prefs_name in ("Preferences", "Secure Preferences"):
            path = user_data / arc_dir / prefs_name
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    settings.update(json.load(f).get("extensions", {}).get("settings", {}))

        for chrome_id, setting in settings.items():
            if setting.get("location") not in USER_INSTALLED_LOCATIONS or setting.get("disable_reasons"):
                continue
            manifests = sorted(glob.glob(str(user_data / arc_dir / "Extensions" / chrome_id / "*" / "manifest.json")))
            if not manifests:
                continue
            manifest_path = Path(manifests[-1])
            with open(manifest_path, encoding="utf-8-sig") as f:
                manifest = json.load(f)
            if chrome_id in found:
                found[chrome_id].profiles.append(arc_dir)
                continue
            found[chrome_id] = ArcExtension(
                chrome_id=chrome_id,
                name=localized_manifest_name(manifest, manifest_path.parent),
                homepage=manifest.get("homepage_url"),
                profiles=[arc_dir],
            )
    return list(found.values())


def amo_chrome_mappings() -> Dict[str, str]:
    """AMO's curated Chrome extension id -> Firefox add-on GUID list."""
    mappings = {}
    url = f"{AMO_API}/addons/browser-mappings/?browser=chrome&page_size=100"
    while url:
        page = http_json(url)
        for result in page.get("results", []):
            mappings[result["extension_id"]] = result["addon_guid"]
        url = page.get("next")
    return mappings


def normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", NAME_SUFFIXES.sub("", text or "")).casefold()


def brand_names(text: str) -> set[str]:
    """Single-word parts of names like "Trancy - AI Translator", which AMO lists as "... - Trancy"."""
    return {normalize(part) for part in NAME_SEGMENTS.split(text or "") if " " not in part.strip() and len(part.strip()) >= 5}


def first_value(localized: Any) -> str:
    if isinstance(localized, dict):
        return localized.get("en-US") or next(iter(localized.values()), "") or ""
    return localized or ""


def normalize_url(url: Optional[str]) -> str:
    return (url or "").casefold().rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")


def find_amo_addon(extension: ArcExtension) -> Optional[Dict[str, Any]]:
    """Search AMO for an add-on with the same name or homepage."""
    for query_text in [NAME_SUFFIXES.sub("", extension.name), *sorted(brand_names(extension.name))]:
        addon = search_amo(extension, query_text)
        if addon:
            return addon
    return None


def search_amo(extension: ArcExtension, query_text: str) -> Optional[Dict[str, Any]]:
    query = urllib.parse.urlencode({"q": query_text, "app": "firefox", "type": "extension", "page_size": 10})
    results = http_json(f"{AMO_API}/addons/search/?{query}").get("results", [])
    wanted_name = normalize(extension.name)
    wanted_homepage = normalize_url(extension.homepage)
    for addon in results:
        homepage = addon.get("homepage") or {}
        homepage_url = first_value(homepage.get("url") if isinstance(homepage, dict) else homepage)
        if wanted_homepage and normalize_url(homepage_url) == wanted_homepage:
            return addon
        names = addon.get("name")
        names = list(names.values()) if isinstance(names, dict) else [names or ""]
        if any(normalize(name) == wanted_name for name in names):
            return addon
        if brand_names(extension.name) & set().union(*(brand_names(name) for name in names)):
            return addon
    return None


def installed_addon_ids(profile: Path) -> set[str]:
    ids = {path.stem for path in (profile / "extensions").glob("*.xpi")}
    extensions_json = profile / "extensions.json"
    if extensions_json.exists():
        with open(extensions_json, encoding="utf-8") as f:
            ids |= {addon.get("id") for addon in json.load(f).get("addons", [])}
    return ids


def download_xpi(addon: Dict[str, Any], target: Path) -> None:
    file_url = addon["current_version"]["file"]["url"]
    request = urllib.request.Request(file_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".xpi.part")
    temp.write_bytes(data)
    temp.replace(target)


def migrate_extensions(
    arc_profile: str | Path | None = None,
    zen_profile: str | Path | None = None,
    install: bool = True,
    space_profile_overrides: Optional[Dict[str, str]] = None,
) -> bool:
    """Install Firefox equivalents of the extensions of every Arc profile in use."""
    profile = resolve_zen_profile(zen_profile)
    arc_profiles = set(arc_space_profiles(arc_profile, space_profile_overrides).values())
    arc_profiles |= {arc_dir for _, arc_dir in arc_favorite_profiles(arc_profile)}
    extensions = arc_extensions(arc_profile, arc_profiles)
    logger.info(f"Found {len(extensions)} enabled Arc extensions in {', '.join(sorted(arc_profiles))}")

    mappings = amo_chrome_mappings()
    already = installed_addon_ids(profile)
    installed, unmatched = [], []
    for extension in extensions:
        guid = mappings.get(extension.chrome_id)
        addon = http_json(f"{AMO_API}/addons/addon/{urllib.parse.quote(guid)}/") if guid else find_amo_addon(extension)
        if not addon:
            unmatched.append(extension)
            continue

        guid = addon["guid"]
        amo_name = first_value(addon.get("name"))
        if guid in already or SUPERSEDED_BY.get(guid, set()) & already:
            logger.info(f"   = {extension.name} -> {amo_name} (already in Zen)")
            continue
        if not install:
            logger.info(f"   ? {extension.name} -> {amo_name} ({addon.get('url')})")
            continue
        download_xpi(addon, profile / "extensions" / f"{guid}.xpi")
        installed.append(guid)
        already.add(guid)
        logger.info(f"   + {extension.name} -> {amo_name} ({addon.get('url')})")

    for extension in unmatched:
        search = "https://addons.mozilla.org/firefox/search/?" + urllib.parse.urlencode({"q": extension.name})
        logger.info(f"   ✗ {extension.name}: no Firefox match found, search manually: {search}")

    if installed:
        logger.info(f"Installed {len(installed)} add-ons. Zen asks to enable each one on its next start.")
    logger.info(f"Done. {len(installed)} installed, {len(unmatched)} without a Firefox version.")
    return True


def main() -> bool:
    parser = argparse.ArgumentParser(description="Install Firefox versions of Arc extensions into Zen.")
    parser.add_argument("--arc-profile", help="Path to the Arc profile root containing StorableSidebar.json.")
    parser.add_argument(
        "--zen-profile",
        help="Path to a Zen profile directory, or a Zen root containing profiles.ini.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only list the matches; do not download anything.")
    args = parser.parse_args()
    return migrate_extensions(args.arc_profile, args.zen_profile, install=not args.dry_run)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
