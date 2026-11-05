#!/usr/bin/env python3
"""Tidy a Bandcamp Downloader library.

- strips the duplicated "Artist - " prefix from track filenames
- removes .flac files that are transcodes of an existing .mp3 twin
- renames stray artwork to cover.<ext>

Nothing is deleted outright: files are moved into --trash.
"""

import argparse
import re
import shutil
from pathlib import Path

AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
ART = {".jpg", ".jpeg", ".png", ".webp"}


def plan(root: Path):
    actions = []
    for album in sorted(p for p in root.iterdir() if p.is_dir()):
        artist = album.name.split(" - ")[0].strip()
        if not artist:
            continue

        files = [f for f in album.iterdir() if f.is_file()]
        doomed = set()

        # A .flac next to an identical-named .mp3 is a transcode of that mp3.
        for f in files:
            if f.suffix.lower() == ".flac" and f.with_suffix(".mp3").exists():
                actions.append(("duplicate", f, f.with_suffix(".mp3")))
                doomed.add(f)

        for f in files:
            if f in doomed or f.suffix.lower() not in AUDIO | ART:
                continue

            match = re.match(
                rf"^(?P<index>\d{{1,3}} - )?{re.escape(artist)} - (?P<rest>.+)$", f.stem
            )
            if not match:
                continue

            if f.suffix.lower() in ART:
                target = f.with_name("cover" + f.suffix.lower())
            else:
                target = f.with_name(
                    f"{match.group('index') or ''}{match.group('rest').strip()}{f.suffix}"
                )

            if target == f:
                continue
            if target.exists():
                actions.append(("conflict", f, target))
                continue
            actions.append(("rename", f, target))

    return actions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--trash", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    trash = Path(args.trash)

    actions = plan(root)
    if not actions:
        print("Nothing to clean up.")
        return

    renames = [a for a in actions if a[0] == "rename"]
    duplicates = [a for a in actions if a[0] == "duplicate"]
    conflicts = [a for a in actions if a[0] == "conflict"]

    print(f"{len(renames)} rename(s), {len(duplicates)} duplicate(s), {len(conflicts)} conflict(s)\n")

    for kind, src, extra in actions:
        rel = src.relative_to(root)
        if kind == "rename":
            print(f"  rename   {rel}\n        -> {extra.name}")
        elif kind == "duplicate":
            print(f"  trash    {rel}   (mp3 twin: {extra.name})")
        else:
            print(f"  SKIP     {rel}   (target exists: {extra.name})")

    if not args.apply:
        print("\nDry run. Re-run with --apply to make these changes.")
        return

    for kind, src, extra in actions:
        rel = src.relative_to(root)
        if kind == "rename":
            src.rename(extra)
        elif kind == "duplicate":
            destination = trash / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(destination))

    print(f"\nDone. Removed files moved to {trash}")


if __name__ == "__main__":
    main()
