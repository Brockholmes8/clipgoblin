#!/usr/bin/env python3
"""Build a 9:16 split-screen short: streamer clip on top, eye-catching loop on the bottom,
word-by-word captions on the seam, optional hook title and credit tag.

Examples:
  python clipfarm.py --top clip.mp4 --bottom loops/ --hook "HE DIDN'T SEE IT COMING" --credit "@streamer on Twitch"
  python clipfarm.py --top "https://www.twitch.tv/..." --start 01:02:10 --end 01:02:45 --bottom loop.mp4
  python clipfarm.py --make-demo-loop loops/demo.mp4   # procedural placeholder loop for testing
"""
import argparse
import random
import subprocess
import sys
import tempfile
from pathlib import Path

W, H = 1080, 1920
HALF = H // 2
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}


def run(cmd):
    print("+", " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    )
    return float(out)


def to_seconds(t):
    if t is None:
        return None
    secs = 0.0
    for part in str(t).split(":"):
        secs = secs * 60 + float(part)
    return secs


def fetch_top(src, start, end, workdir):
    """Download a URL with yt-dlp (optionally just a section), or trim a local file."""
    out = workdir / "top.mp4"
    if src.startswith(("http://", "https://")):
        cmd = ["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
               "-o", str(out), "--force-overwrites"]
        if start or end:
            cmd += ["--download-sections", f"*{start or '0'}-{end or 'inf'}", "--force-keyframes-at-cuts"]
        run(cmd + [src])
        return out
    if not (start or end):
        return Path(src)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", str(to_seconds(start))]
    if end:
        cmd += ["-to", str(to_seconds(end))]
    run(cmd + ["-i", src, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out)])
    return out


def pick_bottom(bottom):
    p = Path(bottom)
    if p.is_dir():
        loops = [f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS]
        if not loops:
            sys.exit(f"No videos found in {p}")
        return random.choice(loops)
    return p


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def transcribe(path, model_size, language):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(path), word_timestamps=True, language=language, vad_filter=True)
    return [w for seg in segments for w in (seg.words or [])]


def chunk_words(words, max_words=3, max_gap=0.6):
    """Group words into short punchy caption chunks, breaking on pauses and punctuation."""
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w.word.strip()[-1:] in ".?!,":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(chunks, hook, credit, watermark, font, total):
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Captions sit on the seam between the two halves (alignment 5 = middle center).
        f"Style: Cap,{font},92,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1",
        f"Style: Hook,{font},76,&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,1,0,0,0,100,100,0,0,3,18,0,8,60,60,90,1",
        # Semi-transparent channel watermark in the top half's top-right corner (alignment 9).
        f"Style: Mark,{font},44,&H60FFFFFF,&H60FFFFFF,&H90000000,&H00000000,1,0,0,0,100,100,0,0,1,3,0,9,40,40,40,1",
        f"Style: Credit,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,40,40,50,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in chunks:
        text = " ".join(ass_escape(w.word.strip()) for w in c).upper()
        # Quick pop-in: start at 80% scale and grow to 100%.
        fx = r"{\fscx80\fscy80\t(0,80,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 1,{ass_time(c[0].start)},{ass_time(c[-1].end + 0.05)},Cap,,0,0,0,,{fx}{text}")
    if hook:
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(3.5, total))},Hook,,0,0,0,,{ass_escape(hook)}")
    if watermark:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Mark,,0,0,0,,{ass_escape(watermark)}")
    if credit:
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(total)},Credit,,0,0,0,,{ass_escape(credit)}")
    return "\n".join(lines) + "\n"


def compose(top, bottom, ass_path, out, max_len):
    total = duration(top)
    if max_len:
        total = min(total, max_len)
    fill = f"scale={W}:{HALF}:force_original_aspect_ratio=increase,crop={W}:{HALF},setsar=1,fps=30"
    vf = f"[0:v]{fill}[t];[1:v]{fill}[b];[t][b]vstack=inputs=2[v]"
    if ass_path:
        # Escape for the filtergraph parser (colons and backslashes in paths).
        esc = str(ass_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        vf += f";[v]ass='{esc}'[v]"
    run([
        "ffmpeg", "-y", "-v", "error", "-stats",
        "-i", str(top), "-stream_loop", "-1", "-i", str(bottom),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out),
    ])
    return total


def make_demo_loop(out, seconds=8):
    """Procedural colour-cycling spiral; a stand-in until real AI loops are generated."""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", f"gradients=s={W // 2}x{HALF // 2}:r=30:n=5:speed=0.03:type=spiral,hue=h=t*45:s=1.5,scale={W}:{HALF}",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out),
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", help="streamer clip: local file or URL")
    ap.add_argument("--bottom", help="bottom loop video, or a folder of loops to pick from at random")
    ap.add_argument("--start", help="trim start (seconds or HH:MM:SS)")
    ap.add_argument("--end", help="trim end (seconds or HH:MM:SS)")
    ap.add_argument("--hook", help="title shown at the top for the first 3.5 seconds")
    ap.add_argument("--credit", help="credit tag shown at the bottom the whole time, e.g. '@streamer on Twitch'")
    ap.add_argument("--watermark", default="ClipGoblin", help="channel watermark ('' to hide)")
    ap.add_argument("--out", default="short.mp4")
    ap.add_argument("--max-len", type=float, default=60, help="cap length in seconds (0 = no cap)")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--model", default="small", help="whisper model: tiny, base, small, medium, large-v3")
    ap.add_argument("--language", default=None, help="force caption language, e.g. en")
    ap.add_argument("--font", default="Inter Display")
    ap.add_argument("--make-demo-loop", metavar="OUT", help="write a procedural test loop and exit")
    a = ap.parse_args()

    if a.make_demo_loop:
        make_demo_loop(a.make_demo_loop)
        return
    if not (a.top and a.bottom):
        ap.error("--top and --bottom are required")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        top = fetch_top(a.top, a.start, a.end, tmp)
        bottom = pick_bottom(a.bottom)
        total = duration(top)
        if a.max_len:
            total = min(total, a.max_len)
        chunks = []
        if not a.no_captions:
            words = [w for w in transcribe(top, a.model, a.language) if w.start < total]
            chunks = chunk_words(words)
        ass_path = None
        if chunks or a.hook or a.credit or a.watermark:
            ass_path = tmp / "subs.ass"
            ass_path.write_text(build_ass(chunks, a.hook, a.credit, a.watermark, a.font, total), encoding="utf-8")
        compose(top, bottom, ass_path, a.out, a.max_len)
    print(f"Wrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
