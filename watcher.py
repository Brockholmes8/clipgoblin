#!/usr/bin/env python3
"""Watch approved streamers for new streams, cut highlights from each, and queue them for review.

Reads streamers.toml, checks each approved streamer's channel for new past broadcasts, runs the
highlight finder on anything new (rendering finished shorts), and adds every short to queue.json
with status "pending". Nothing is posted until you approve it with review.py.

Run it once (e.g. from Task Scheduler / cron every few hours), or keep it running:
  python watcher.py               # one pass
  python watcher.py --every 3     # check every 3 hours, forever
  python watcher.py --only "Kai Cenat" --backfill 2
"""
import argparse
import json
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
QUEUE = ROOT / "queue.json"


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def vod_list_url(channel):
    """The page listing a channel's past broadcasts, newest first."""
    channel = channel.rstrip("/")
    if "twitch.tv" in channel:
        return channel + "/videos?filter=archives&sort=time"
    if "youtube.com" in channel:
        return channel + "/streams"
    return channel


def kick_vods(channel, limit):
    slug = channel.rstrip("/").rsplit("/", 1)[-1]
    req = urllib.request.Request(f"https://kick.com/api/v2/channels/{slug}/videos",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        vids = json.load(r)
    out = []
    for v in vids[:limit]:
        vid = v.get("video", {}).get("uuid") or str(v.get("id"))
        out.append({"id": vid, "url": f"https://kick.com/{slug}/videos/{vid}", "title": v.get("session_title", "")})
    return out


def recent_vods(channel, limit):
    """Newest past broadcasts for a channel as [{id, url, title}], newest first."""
    if "kick.com" in channel:
        return kick_vods(channel, limit)
    res = subprocess.run(
        ["yt-dlp", "--flat-playlist", "-J", "--playlist-end", str(limit), vod_list_url(channel)],
        check=True, capture_output=True, text=True,
    )
    entries = [e for e in json.loads(res.stdout).get("entries", []) if e]
    return [{"id": e["id"], "url": e.get("url") or e.get("webpage_url"), "title": e.get("title", "")}
            for e in entries]


def process_vod(streamer, vod, args):
    out_dir = ROOT / "out" / slugify(streamer["name"]) / slugify(vod["id"])
    cmd = [sys.executable, str(ROOT / "highlights.py"), "--vod", vod["url"], "--streamer", streamer["credit"],
           "--top", str(args.top), "--out-dir", str(out_dir), "--render", "--bottom", args.bottom]
    subprocess.run(cmd, check=True)
    picks = json.loads((out_dir / "highlights.json").read_text())["picks"]
    return [{
        "id": f"{slugify(streamer['name'])}-{slugify(vod['id'])}-{i:02d}",
        "streamer": streamer["name"],
        "credit": streamer["credit"],
        "vod_url": vod["url"],
        "vod_title": vod["title"],
        "file": p["file"],
        "hook": p.get("hook"),
        "post_caption": p.get("post_caption", ""),
        "hashtags": p.get("hashtags", []),
        "status": "pending",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    } for i, p in enumerate(picks, 1)]


def slugify(s):
    return "".join(c if c.isalnum() else "-" for c in str(s).lower()).strip("-")


def run_once(args):
    config = tomllib.loads((ROOT / args.config).read_text())
    state = load_json(STATE, {"seen": {}})
    queue = load_json(QUEUE, [])

    for s in config.get("streamer", []):
        if args.only and s["name"].lower() != args.only.lower():
            continue
        if not s.get("approved"):
            print(f"- {s['name']}: not approved, skipping")
            continue
        try:
            vods = recent_vods(s["channel"], limit=5)
        except Exception as e:  # one broken channel shouldn't stop the others
            print(f"! {s['name']}: couldn't list streams ({e})")
            continue
        seen = state["seen"].setdefault(s["name"], None)
        if seen is None:
            # First time we see this streamer: only take the newest `backfill` streams, not the whole archive.
            new, seen = vods[: args.backfill], [v["id"] for v in vods[args.backfill:]]
        else:
            new = [v for v in vods if v["id"] not in seen]
        print(f"- {s['name']}: {len(new)} new stream(s)")
        for vod in reversed(new):  # oldest first
            try:
                queue += process_vod(s, vod, args)
                save_json(QUEUE, queue)
            except Exception as e:
                print(f"! {s['name']}: failed on {vod['url']} ({e})")
                continue
            seen.append(vod["id"])
            state["seen"][s["name"]] = seen
            save_json(STATE, state)
        state["seen"][s["name"]] = seen
        save_json(STATE, state)

    pending = sum(1 for q in queue if q["status"] == "pending")
    print(f"Done. {pending} clip(s) waiting for review: run `python review.py`.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="streamers.toml")
    ap.add_argument("--only", help="just this streamer (by name)")
    ap.add_argument("--top", type=int, default=3, help="highlights per stream")
    ap.add_argument("--backfill", type=int, default=1, help="streams to process the first time a streamer is seen")
    ap.add_argument("--bottom", default=str(ROOT / "loops"), help="bottom loop video or folder")
    ap.add_argument("--every", type=float, help="repeat every N hours")
    args = ap.parse_args()
    while True:
        run_once(args)
        if not args.every:
            break
        print(f"Sleeping {args.every}h...")
        time.sleep(args.every * 3600)


if __name__ == "__main__":
    main()
