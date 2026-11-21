#!/usr/bin/env python3
"""Remove the hidden `<!-- pdf2md:page=N -->` comments from notes that were converted before they became optional.

  strip_page_markers.py FOLDER_OR_FILE [...]            # dry run: lists what would change
  strip_page_markers.py FOLDER_OR_FILE [...] --apply    # rewrites the files (a copy of each goes to --backup-dir)

Only whole marker lines (and the blank lines directly after them) are removed; nothing else is touched. Back up your
vault first if it holds notes you care about.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

MARKER = re.compile(r"^<!-- pdf2md:page=\d+ -->[ \t]*\n(?:[ \t]*\n)*", re.M)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("page-marker-backup"),
                    help="where --apply copies the original files (default ./page-marker-backup)")
    a = ap.parse_args()
    files = sorted({f for p in a.paths for f in ([p] if p.is_file() else p.rglob("*.md"))})
    changed = total = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        new, n = MARKER.subn("", text)
        if not n:
            continue
        changed += 1
        total += n
        print(f"{f}: {n} marker(s)")
        if a.apply:
            dest = a.backup_dir / f.name
            i = 1
            while dest.exists():
                i += 1
                dest = a.backup_dir / f"{f.stem} ({i}){f.suffix}"
            a.backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            f.write_text(new, encoding="utf-8")
    print(f"{'removed' if a.apply else 'would remove'} {total} marker(s) in {changed} of {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
