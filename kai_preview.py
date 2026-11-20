"""Select a recent Kai highlight using Twitch velocity and audiovisual review."""
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from common import WORK_DIR, probe, setup_logging
from stream_clips import ai_review


def summary(message):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
        print(message, file=out)


def views_per_hour(clip, now):
    created = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
    hours = (now - created).total_seconds() / 3600
    return clip["view_count"] / max(1, hours)


def main():
    setup_logging()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    clips = json.loads(Path("candidates.json").read_text())
    now = datetime.now(timezone.utc)
    shortlist = sorted(clips, key=lambda c: views_per_hour(c, now), reverse=True)[:3]
    summary(f"Matching clips: {len(clips)}. Shortlisted by average views/hour: {len(shortlist)}.")
    if shortlist and not os.environ.get("GEMINI_API_KEY", "").strip():
        summary("Missing GEMINI_API_KEY for clip review.")
        raise RuntimeError("Missing GEMINI_API_KEY for clip review.")
    best = None
    reviewed = 0
    with tempfile.TemporaryDirectory() as folder:
        for index, clip in enumerate(shortlist):
            base = Path(folder) / str(index)
            try:
                subprocess.run(["yt-dlp", "--no-playlist", "-S", "res,fps,br", "-o",
                                str(base) + ".%(ext)s", "--", clip["url"]], check=True)
                path, = base.parent.glob(base.name + ".*")
                info = probe(path)
                if not info["has_audio"]:
                    summary(f"Candidate {index + 1}: no readable audio stream.")
                    continue
                review = ai_review(path, {**clip, "streamer": "Kai Cenat", "platform": "twitch"})
                if not review:
                    summary(f"Candidate {index + 1}: Gemini returned no video review. See the review step log.")
                    continue
                score = int(review["hype_score"])
                start = max(0, float(review["trim_start"]) - 1)
                end = min(info["duration"], float(review["trim_end"]) + 1)
                if end <= start or not 1 <= score <= 10:
                    summary(f"Candidate {index + 1}: invalid score or trim interval.")
                    continue
            except (subprocess.CalledProcessError, ValueError, TypeError, KeyError) as exc:
                summary(f"Skipping candidate {index + 1}: {type(exc).__name__}")
                continue
            reviewed += 1
            rate = views_per_hour(clip, now)
            summary(f"- {clip['url']} — {rate:.0f} views/hour, moment score {score}/10.")
            if (review.get("is_gta6_footage") is not True or review.get("is_spoiler")
                    or review.get("unsafe") or review.get("is_english") is False):
                continue
            rank = (score, rate)
            if best is None or rank > best[0]:
                best = (rank, path, start, end, clip)
        if shortlist and not reviewed:
            raise RuntimeError("Could not download and review any shortlisted clip.")
        if best is None:
            summary("No eligible GTA VI moment found.")
            return
        _, path, start, end, clip = best
        output = Path("kai-preview.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(path),
                        "-t", str(end - start), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", str(output)], check=True)
        info = probe(output)
        if not info["has_audio"] or info["duration"] <= 0:
            raise RuntimeError("Preview is missing audio or has no duration.")
        summary(f"Selected clip: {clip['url']} ({start:.1f}–{end:.1f}s)")
        summary(f"Downloaded resolution: {info['width']}x{info['height']}")
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            print("ready=true", file=out)


if __name__ == "__main__":
    main()
