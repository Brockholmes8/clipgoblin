#!/usr/bin/env python3
"""Approve or reject the clips the watcher queued. Nothing gets posted without your OK.

  python review.py          # go through pending clips one by one
  python review.py --list   # just show counts and what's approved
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue.json"


def open_file(path):
    """Open the video in the default player so you can watch it before deciding."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
    except Exception:
        pass


def save(queue):
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2))
    tmp.replace(QUEUE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open each video")
    a = ap.parse_args()

    queue = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
    if a.list:
        for status in ("pending", "approved", "rejected", "posted"):
            print(f"{status}: {sum(1 for q in queue if q['status'] == status)}")
        for q in queue:
            if q["status"] == "approved":
                print(f"  {q['id']}: {q['hook'] or ''}  {q['file']}")
        return

    pending = [q for q in queue if q["status"] == "pending"]
    if not pending:
        print("Nothing to review.")
        return
    for n, q in enumerate(pending, 1):
        print(f"\n[{n}/{len(pending)}] {q['streamer']}  ({q['vod_title']})")
        print(f"  file:    {q['file']}")
        print(f"  hook:    {q['hook'] or '(none)'}")
        print(f"  caption: {q['post_caption']} {' '.join(q['hashtags'])}")
        if not a.no_open:
            open_file(q["file"])
        while True:
            ans = input("  [a]pprove / [r]eject / [e]dit caption / [s]kip / [q]uit: ").strip().lower()
            if ans == "e":
                q["post_caption"] = input("  new caption: ").strip() or q["post_caption"]
                continue
            break
        if ans == "q":
            break
        if ans in ("a", "r"):
            q["status"] = "approved" if ans == "a" else "rejected"
            save(queue)
    print("\nSaved. Approved clips are ready to post.")


if __name__ == "__main__":
    main()
