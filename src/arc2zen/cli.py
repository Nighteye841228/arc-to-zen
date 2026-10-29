#!/usr/bin/env python3
"""Unified Arc to Zen command-line interface."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .migration import MigrationOptions, nuke_zen_profile_only, run_migration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate Arc Browser data into a Zen Browser profile.")
    parser.add_argument(
        "--arc-profile",
        help="Path to the Arc data folder containing StorableSidebar.json.",
    )
    parser.add_argument(
        "--zen-profile",
        help="Path to a Zen profile directory, or a Zen root containing profiles.ini.",
    )
    parser.add_argument(
        "--export-file",
        type=Path,
        default=Path("arc_pinned_tabs_export.json"),
        help="Path for the intermediate Arc export JSON. Defaults to ./arc_pinned_tabs_export.json.",
    )
    parser.add_argument(
        "--nuke",
        action="store_true",
        help="Clear existing Zen tabs, folders, pins, groups, closed-tab state, and regular bookmarks before importing.",
    )
    parser.add_argument(
        "--nuke-only",
        action="store_true",
        help="Only clear the selected Zen profile; do not import Arc data.",
    )
    parser.add_argument(
        "--include-orphaned",
        action="store_true",
        help='Import Arc essential tabs that could not be matched to a workspace into an "Orphaned" workspace.',
    )
    parser.add_argument(
        "--no-backups",
        action="store_true",
        help="Do not create backups before changing Zen profile files.",
    )
    parser.add_argument("--no-favicons", action="store_true", help="Do not copy cached Arc favicons.")
    parser.add_argument(
        "--no-folder-states",
        action="store_true",
        help="Do not sync pinned-folder open/closed state.",
    )
    parser.add_argument("--no-workspace-icons", action="store_true", help="Do not sync workspace icons.")
    parser.add_argument(
        "--no-workspace-themes",
        action="store_true",
        help="Do not sync workspace colors/themes.",
    )
    parser.add_argument(
        "--no-containers",
        action="store_true",
        help="Do not map Arc profiles to Zen containers.",
    )
    parser.add_argument("--no-cookies", action="store_true", help="Do not import Arc cookies (macOS only).")
    parser.add_argument(
        "--default-profile-container",
        action="store_true",
        help="Also put the Arc Default profile into its own container instead of no container.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    args = parse_args()

    if args.nuke_only:
        nuke_zen_profile_only(args.zen_profile, create_backups=not args.no_backups)
        return 0

    options = MigrationOptions(
        arc_profile=args.arc_profile,
        zen_profile=args.zen_profile,
        export_file=args.export_file,
        nuke=args.nuke,
        favicons=not args.no_favicons,
        folder_states=not args.no_folder_states,
        workspace_icons=not args.no_workspace_icons,
        workspace_themes=not args.no_workspace_themes,
        containers=not args.no_containers,
        cookies=not args.no_cookies,
        default_profile_container=args.default_profile_container,
        skip_orphaned=not args.include_orphaned,
        create_backups=not args.no_backups,
    )
    run_migration(options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
