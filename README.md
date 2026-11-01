# ClipGoblin clip maker

Turns a streamer clip into a 1080x1920 split-screen short:
- streamer clip on top, eye-catching loop on the bottom
- big word-by-word captions on the middle seam (auto transcribed with Whisper)
- optional hook title for the first 3.5 seconds, a credit tag, and a ClipGoblin watermark

## Setup (once)

1. Install Python 3.10+ and ffmpeg (Windows: `winget install ffmpeg`, Mac: `brew install ffmpeg`).
2. `pip install -r requirements.txt`

The first run downloads the Whisper caption model (about 500 MB for `small`).

## Use

```
# Make a placeholder bottom loop to test with (swap in real AI loops later)
python clipfarm.py --make-demo-loop loops/demo.mp4

# Local clip
python clipfarm.py --top clip.mp4 --bottom loops/ --hook "HE ACTUALLY LANDED IT" --credit "@streamer on Twitch" --out short.mp4

# Straight from a VOD/clip link, just the part you want
python clipfarm.py --top "https://www.twitch.tv/videos/..." --start 1:02:10 --end 1:02:45 --bottom loops/ --credit "@streamer on Twitch"
```

Options:
- `--bottom` takes one video, or a folder of loops (it picks one at random each time)
- `--max-len 60` caps length (default 60s); `--no-captions` skips captions
- `--model tiny|base|small|medium|large-v3` picks caption accuracy vs speed (default `small`)
- `--watermark ""` hides the watermark

## Find highlights in a whole stream

`highlights.py` scans a full VOD, finds the hype moments from volume spikes, transcribes them,
and has Claude pick the best ones and write a hook title, post caption and hashtags.

```
python highlights.py --vod "https://www.twitch.tv/videos/..." --streamer "@streamer on Twitch" --top 5 --render --bottom loops/
```

It writes `out/highlights.json` (timestamps, hooks, captions, hashtags) and, with `--render`,
a finished short per pick in `out/`. Set `ANTHROPIC_API_KEY` for the AI picks; without it
(or with `--no-ai`) it ranks by loudness only and leaves hooks blank.

Scanning uses the fast `base` Whisper model by default; a 3-hour stream takes a while on a laptop.

## Bottom-half loops

Put 5 to 10 second videos in `loops/`. Good sources:
- AI video tools (Kling, Runway, Veo, Sora): prompts like "satisfying kinetic sand cutting, macro, vivid colors, seamless loop" or "neon tunnel flythrough, seamless loop". Generate a library once and reuse it.
- Royalty-free stock (Pexels, Pixabay) for parkour, slime, or soap cutting.

Only clip streamers who gave you permission (see `docs/permission-dm.md`), and turn on the AI-generated label when you post.
