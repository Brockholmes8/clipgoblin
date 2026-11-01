#!/usr/bin/env python3
"""Scan a full stream VOD for its best short-form moments, and optionally render each one.

1. Pulls the audio (from a URL with yt-dlp, or a local file).
2. Finds hype moments from volume spikes (yelling, laughing, crowd noise).
3. Transcribes the stream with Whisper and cuts a 20-60s candidate around each spike.
4. Has Claude rank the candidates and write a hook title, post caption and hashtags
   (falls back to loudness-only ranking when no ANTHROPIC_API_KEY is set, or with --no-ai).
5. Writes highlights.json, and with --render makes a finished short for each pick.

Examples:
  python highlights.py --vod "https://www.twitch.tv/videos/123" --streamer "@streamer on Twitch"
  python highlights.py --vod stream.mp4 --top 5 --render --bottom loops/
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

import clipfarm

SAMPLE_RATE = 16000


def fetch_audio(vod, workdir):
    """Download just the audio track of a VOD URL; local files are used as-is."""
    if not vod.startswith(("http://", "https://")):
        return Path(vod)
    out = workdir / "vod_audio.m4a"
    if not out.exists():
        clipfarm.run(["yt-dlp", "-f", "ba/b", "-x", "--audio-format", "m4a", "-o", str(out), vod])
    return out


def loudness_per_second(audio):
    """RMS loudness in dB for each second of audio."""
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    secs = len(samples) // SAMPLE_RATE
    frames = samples[: secs * SAMPLE_RATE].reshape(secs, SAMPLE_RATE)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-6)


def find_spikes(db, count, min_gap):
    """Seconds where loudness jumps well above the surrounding few minutes, strongest first."""
    smooth = np.convolve(db, np.ones(3) / 3, mode="same")
    # Compare against a rolling ~3 minute baseline so quiet and loud stream segments are judged fairly.
    win = 180
    pad = np.pad(smooth, win // 2, mode="edge")
    kernel = np.ones(win) / win
    base = np.convolve(pad, kernel, mode="valid")[: len(smooth)]
    sq = np.convolve(pad ** 2, kernel, mode="valid")[: len(smooth)]
    spread = np.sqrt(np.maximum(sq - base ** 2, 0)) + 1.0
    score = (smooth - base) / spread
    spikes = []
    for t in np.argsort(score)[::-1]:
        if score[t] < 1.0 or len(spikes) >= count:
            break
        if all(abs(t - s) >= min_gap for s, _ in spikes):
            spikes.append((int(t), float(score[t])))
    return spikes


def transcribe_segments(audio, model_size, language):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(audio), language=language, vad_filter=True)
    return [(s.start, s.end, s.text.strip()) for s in segments]


def build_candidates(spikes, segments, min_len, max_len):
    """Cut a window around each spike: some setup before it, the payoff after, snapped to sentence edges."""
    cands = []
    for i, (t, score) in enumerate(spikes):
        start, end = max(0, t - 0.6 * max_len), t + 0.4 * max_len
        inside = [s for s in segments if s[1] > start and s[0] < end]
        if inside:
            start, end = max(start, inside[0][0]), min(end, inside[-1][1])
        if end - start < min_len:
            end = start + min_len
        text = " ".join(f"[{s[0] - start:.0f}s] {s[2]}" for s in inside)
        cands.append({"id": i, "start": round(start, 2), "end": round(end, 2), "spike_at": t,
                      "hype": round(score, 2), "transcript": text})
    return cands


def rank_with_claude(cands, top, streamer, min_len, max_len):
    import anthropic
    from pydantic import BaseModel

    class Pick(BaseModel):
        id: int
        start_offset: float  # seconds into the candidate where the short should begin
        end_offset: float
        hook: str
        post_caption: str
        hashtags: list[str]
        why: str

    class Picks(BaseModel):
        picks: list[Pick]

    prompt = (
        f"You pick moments from a livestream of {streamer or 'a streamer'} to post as TikTok/YouTube Shorts/"
        f"Reels clips. Below are candidate windows found from volume spikes, each with a timestamped transcript "
        f"(offsets in seconds from the window start) and a hype score.\n\n"
        f"Choose the best {top} for short-form virality: a clear setup and payoff, understandable without "
        f"context, funny/shocking/emotional. Skip anything that is just loud with no story, and skip moments "
        f"involving sensitive personal info, minors, or harassment. For each pick, tighten it to "
        f"{min_len}-{max_len}s by giving start/end offsets inside the window, so it opens on the hook. "
        f"Write an on-screen hook title (max 6 words, ALL CAPS, no emojis, no clickbait lies), a post caption "
        f"(one line), and 3-5 relevant hashtags. Order picks best first.\n\n"
        + json.dumps([{k: c[k] for k in ("id", "hype", "transcript")} | {"length": round(c["end"] - c["start"])}
                      for c in cands], indent=1)
    )
    client = anthropic.Anthropic()
    resp = client.beta.messages.parse(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        output_format=Picks,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        print("Claude declined to rank these; falling back to loudness ranking.", file=sys.stderr)
        return None
    by_id = {c["id"]: c for c in cands}
    results = []
    for p in resp.parsed_output.picks[:top]:
        c = by_id.get(p.id)
        if not c:
            continue
        start = c["start"] + max(0.0, p.start_offset)
        end = min(c["end"], c["start"] + p.end_offset) if p.end_offset > p.start_offset else c["end"]
        results.append({"start": round(start, 2), "end": round(end, 2), "hook": p.hook,
                        "post_caption": p.post_caption, "hashtags": p.hashtags, "why": p.why,
                        "hype": c["hype"]})
    return results


def rank_by_loudness(cands, top):
    return [{"start": c["start"], "end": c["end"], "hook": None, "post_caption": "", "hashtags": [],
             "why": f"volume spike (hype {c['hype']})", "hype": c["hype"]} for c in cands[:top]]


def hms(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vod", required=True, help="stream VOD URL or local file")
    ap.add_argument("--streamer", default="", help="credit shown on the clip, e.g. '@streamer on Twitch'")
    ap.add_argument("--top", type=int, default=5, help="how many highlights to pick")
    ap.add_argument("--min-len", type=float, default=20)
    ap.add_argument("--max-len", type=float, default=55)
    ap.add_argument("--candidates", type=int, default=15, help="volume spikes to consider")
    ap.add_argument("--model", default="base", help="whisper model for scanning (tiny/base/small...)")
    ap.add_argument("--language", default=None)
    ap.add_argument("--no-ai", action="store_true", help="rank by loudness only, no Claude call")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--render", action="store_true", help="also make a finished short for each pick")
    ap.add_argument("--bottom", default="loops/", help="bottom loop video or folder (with --render)")
    a = ap.parse_args()

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = fetch_audio(a.vod, out_dir)

    print("Measuring loudness...", file=sys.stderr)
    db = loudness_per_second(audio)
    spikes = find_spikes(db, a.candidates, min_gap=a.max_len)
    if not spikes:
        sys.exit("No clear hype moments found in this VOD.")
    print(f"Found {len(spikes)} spikes. Transcribing (this is the slow part)...", file=sys.stderr)
    segments = transcribe_segments(audio, a.model, a.language)
    cands = build_candidates(spikes, segments, a.min_len, a.max_len)

    picks = None
    if not a.no_ai and os.environ.get("ANTHROPIC_API_KEY"):
        picks = rank_with_claude(cands, a.top, a.streamer, a.min_len, a.max_len)
    elif not a.no_ai:
        print("No ANTHROPIC_API_KEY set; ranking by loudness only.", file=sys.stderr)
    picks = picks or rank_by_loudness(cands, a.top)

    for i, p in enumerate(picks, 1):
        p["file"] = str(out_dir / f"highlight_{i:02d}.mp4")
        print(f"{i}. {hms(p['start'])}-{hms(p['end'])}  {p['hook'] or ''}  ({p['why']})")
    (out_dir / "highlights.json").write_text(json.dumps({"vod": a.vod, "streamer": a.streamer, "picks": picks},
                                                        indent=2))
    print(f"Wrote {out_dir / 'highlights.json'}", file=sys.stderr)

    if a.render:
        for p in picks:
            clipfarm.make_short(a.vod, a.bottom, p["file"], start=p["start"], end=p["end"], hook=p["hook"],
                                credit=a.streamer or None, max_len=a.max_len + 5)
            print(f"Rendered {p['file']}", file=sys.stderr)


if __name__ == "__main__":
    main()
