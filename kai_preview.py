"""Select a recent Kai highlight from Twitch and YouTube using view velocity and audiovisual review."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from common import WORK_DIR, probe, setup_logging
from stream_clips import GTA6_RE, ai_review, subtitle_words
from video_processor import build_ass
from source_fetcher import _ytdlp_base


LAYOUT_PROMPT = """
Review the entire source clip, including gameplay, webcam, overlays and background.
Return sexual_content_status="clear" only if you can confidently exclude nudity
and sexual scenes throughout. Return "present" for any such imagery, even briefly,
including animated game characters. Return "uncertain" for ambiguous imagery or
incomplete visibility that prevents a decision. Evaluate this before trimming.
For transcript, use only clearly audible words spoken by Kai in English, with phrase start/end seconds relative to the source clip. Omit silence, game dialogue and unclear speech. Use [] when Kai does not speak clearly.
Also return kai_primary and fair_portrayal as JSON booleans.
Evaluate the entire proposed cut including one second of extra context at each end.
kai_primary is true only if Kai is present in the edit itself and his reaction,
quote or action is its primary focus beyond the intro. fair_portrayal requires the cut
to preserve context and avoid negative portrayals, hate farming or false narratives.
For any of these checks use false when uncertain.
In the same JSON, describe the visible layout throughout the chosen moment.
Return layout="stacked" only when the webcam already spans the top of a vertical
frame, Kai is horizontally centered there, and gameplay fills the bottom. If he is off-center, use crop even for vertical sources. For a stable visible webcam and gameplay
in another arrangement return layout="crop", with camera_box and game_box.
Both boxes are [left, top, right, bottom] in 0..1000 coordinates of the entire
frame. Provide face_center_x, the typical horizontal center of Kai’s face during this moment, in 0..1000 coordinates of the FULL frame. Keep his head and shoulders visible when choosing camera_box and choose a separate contiguous
gameplay region around the action, excluding the webcam, platform logos and viewer counter. If either panel is
missing, changes position during the moment, or is unclear, return layout="unknown".
"""


def crop_box(box, width, height):
    if not isinstance(box, list) or len(box) != 4:
        return None
    if not all(type(v) in (int, float) and 0 <= v <= 1000 for v in box):
        return None
    left, top, right, bottom = [int(v * size / 1000) // 2 * 2
                                for v, size in zip(box, (width, height, width, height))]
    if right - left < 16 or bottom - top < 16:
        return None
    return left, top, right, bottom


def layout_filter(review, info):
    width, height = info["width"], info["height"]
    if review.get("layout") == "stacked" and width * 16 == height * 9:
        return "[0:v]setsar=1[out]"
    camera = crop_box(review.get("camera_box"), width, height)
    game = crop_box(review.get("game_box"), width, height)
    if review.get("layout") != "crop" or not camera or not game:
        return None
    if max(camera[0], game[0]) < min(camera[2], game[2]) and max(camera[1], game[1]) < min(camera[3], game[3]):
        return None
    panels = []
    for name, box, panel_height in (("cam", camera, 640), ("game", game, 1280)):
        left, top, right, bottom = box
        if name == "cam":
            anchor = review.get("face_center_x")
            if type(anchor) in (int, float) and 0 <= anchor <= 1000:
                center = round(anchor * width / 1000)
                if left < center < right:
                    # Narrow the panel symmetrically where possible without excessive zoom.
                    span = min(right - left, max((right - left) * 2 // 3,
                               2 * min(center - left, right - center))) // 2 * 2
                    left = max(left, min(right - span, center - span // 2)) // 2 * 2
                    right = left + span
        panels.append(f"[{name}]crop={right-left}:{bottom-top}:{left}:{top},"
                      f"scale=1080:{panel_height}:force_original_aspect_ratio=increase,"
                      f"crop=1080:{panel_height},setsar=1[{name}out]")
    return ";".join(["[0:v]split[cam][game]", *panels, "[camout][gameout]vstack[out]"])


def has_audible_audio(path):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    peak = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    return result.returncode == 0 and peak is not None and float(peak[1]) > -60


def summary(message):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
        print(message, file=out)


def views_per_hour(clip, now):
    created = datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
    hours = (now - created).total_seconds() / 3600
    return clip["view_count"] / max(1, hours)



def youtube_error(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return "request timed out"
    detail = (getattr(exc, "stderr", "") or "").lower()
    for marker, reason in (
        ("does not have a streams tab", "channel has no streams tab"),
        ("not a bot", "YouTube requested sign-in verification"),
        ("http error 429", "YouTube rate limit"),
        ("http error 403", "YouTube denied access (403)"),
        ("video unavailable", "video unavailable"),
        ("requested format is not available", "no downloadable format"),
    ):
        if marker in detail:
            return reason
    if isinstance(exc, subprocess.CalledProcessError):
        return f"yt-dlp exit {exc.returncode} (unclassified)"
    return type(exc).__name__


def youtube_metadata(args):
    result = subprocess.run(_ytdlp_base() + args, capture_output=True, text=True,
                            timeout=45, check=True)
    return json.loads(result.stdout)


def youtube_candidates():
    channel = "https://www.youtube.com/@KaiCenatGaming"
    entries = {}
    for tab in ("streams", "videos"):
        try:
            page = youtube_metadata(["--flat-playlist", "--playlist-end", "30", "-J", f"{channel}/{tab}"])
        except (subprocess.SubprocessError, ValueError) as exc:
            summary(f"YouTube {tab} listing unavailable: {youtube_error(exc)}.")
            continue
        for entry in page.get("entries") or []:
            if entry and GTA6_RE.search(entry.get("title") or ""):
                entries.setdefault(entry["id"], entry)
    clips = []
    for video_id in list(entries)[:6]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            meta = youtube_metadata(["--no-playlist", "--ignore-no-formats-error", "-J", url])
            timestamp = meta.get("release_timestamp") or meta.get("timestamp")
            duration = int(meta.get("duration") or 0)
            if not timestamp or duration < 1 or meta.get("live_status") in ("is_upcoming", "is_live"):
                continue
            created = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            if created[:10] < "2026-11-18":
                continue
            peaks = meta.get("heatmap") or []
            peak = max(peaks, key=lambda p: p.get("value") or 0) if peaks else {}
            center = int(peak.get("start_time") or duration // 2)
            start = max(0, min(center - 30, duration - 60))
            clips.append({"platform": "youtube", "id": f"youtube:{video_id}@{start}",
                          "url": url, "title": meta.get("title") or "",
                          "view_count": int(meta.get("view_count") or 0), "created_at": created,
                          "section": (start, min(duration, start + 60))})
        except (subprocess.SubprocessError, TypeError, ValueError) as exc:
            summary(f"YouTube metadata unavailable for {url}: {youtube_error(exc)}.")
    summary(f"YouTube candidates from @KaiCenatGaming: {len(clips)}.")
    return clips


def main():
    setup_logging()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    clips = json.loads(Path("candidates.json").read_text())
    clips.extend(youtube_candidates())
    # Helix timestamps are UTC. Clip creation is an initial filter, not proof of VOD age.
    clips = [clip for clip in clips if clip["created_at"][:10] >= "2026-11-18"]
    history = Path(os.environ["KAI_REVIEW_HISTORY"]) if "KAI_REVIEW_HISTORY" in os.environ else None
    seen = set(history.read_text().splitlines()) if history else set()
    unseen = {}
    for clip in clips:
        if clip["id"] not in seen:
            unseen.setdefault(clip["id"], clip)
    summary(f"Previously reviewed clips skipped: {sum(c['id'] in seen for c in clips)}.")
    now = datetime.now(timezone.utc)
    group = int(os.getenv("CLIP_GROUP") or "1")
    if not 1 <= group <= 12:
        raise ValueError("Clip group must be between 1 and 12.")
    offset = (group - 1) * 9
    shortlist = sorted(unseen.values(), key=lambda c: views_per_hour(c, now), reverse=True)[offset:offset + 9]
    summary(f"Review group: {group}.")
    summary(f"Matching clips: {len(clips)}. Review limit (batches of 3 by views/hour): {len(shortlist)}.")
    if shortlist and not os.environ.get("GEMINI_API_KEY", "").strip():
        summary("Missing GEMINI_API_KEY for clip review.")
        raise RuntimeError("Missing GEMINI_API_KEY for clip review.")
    selected = []
    reviewed = 0
    with tempfile.TemporaryDirectory() as folder:
        for index, clip in enumerate(shortlist):
            base = Path(folder) / str(index)
            try:
                command = ["yt-dlp"]
                if clip.get("platform") == "youtube":
                    first, last = clip["section"]
                    command = _ytdlp_base() + ["--download-sections", f"*{first}-{last}", "--force-keyframes-at-cuts"]
                subprocess.run(command + [ "--no-playlist", "-S", "res,fps,br", "-o",
                                str(base) + ".%(ext)s", "--", clip["url"]], check=True)
                path, = base.parent.glob(base.name + ".*")
                info = probe(path)
                if not info["has_audio"]:
                    if history:
                        with history.open("a", encoding="utf-8") as out:
                            print(clip["id"], file=out)
                    summary(f"Candidate {index + 1}: no readable audio stream.")
                    continue
                review = ai_review(path, {**clip, "streamer": "Kai Cenat", "platform": clip.get("platform", "twitch")}, LAYOUT_PROMPT)
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
            if history:
                with history.open("a", encoding="utf-8") as out:
                    print(clip["id"], file=out)
            reviewed += 1
            rate = views_per_hour(clip, now)
            summary(f"- {clip['url']} — {rate:.0f} views/hour, moment score {score}/10.")
            reasons = [reason for key, reason in (
                ("kai_primary", "Kai not confirmed as primary focus"),
                ("fair_portrayal", "fair portrayal not confirmed"),
            ) if review.get(key) is not True]
            if review.get("is_gta6_footage") is not True:
                reasons.append("GTA VI gameplay not confirmed")
            if review.get("is_spoiler"):
                reasons.append("story spoiler")
            if review.get("sexual_content_status") != "clear":
                status = review.get("sexual_content_status")
                if status == "present":
                    reasons.append("review flagged nudity or sexual content")
                elif status == "uncertain":
                    reasons.append("review could not rule out nudity or sexual content")
                else:
                    reasons.append("missing or invalid sexual-content review")
            if review.get("unsafe") is not False:
                reasons.append("flagged by content review")
            if review.get("is_english") is False:
                reasons.append("non-English audio")
            if reasons:
                summary(f"Candidate {index + 1}: {', '.join(reasons)}.")
                continue
            filters = layout_filter(review, info)
            if not filters:
                # Preserve the full composition when the webcam moves or no split is suitable.
                filters = ("[0:v]scale=1080:1920:force_original_aspect_ratio=decrease:"
                           "force_divisible_by=2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[out]")
            rank = (score, rate)
            end = min(end, start + 179)
            words = subtitle_words(review.get("transcript"), start, end - start)
            if words:
                captions = Path(folder) / f"captions-{index}.ass"
                build_ass(captions, "", words, end - start, "", caption_y=850)
                filters += f";[out]ass={captions}[captioned]"
            output = Path(folder) / f"rendered-{index}.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(path),
                            "-t", str(end - start), "-filter_complex", filters, "-map", "[captioned]" if words else "[out]", "-map", "0:a:0", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", str(output)], check=True)
            info = probe(output)
            if not info["has_audio"] or info["duration"] <= 0:
                raise RuntimeError("Preview is missing audio or has no duration.")
            if info["width"] * 16 != info["height"] * 9 or info["duration"] > 180:
                raise RuntimeError("Rendered preview exceeds Shorts duration or has the wrong aspect ratio.")
            if not has_audible_audio(output):
                summary(f"Candidate {index + 1}: final edit is silent or has no verifiable audible signal.")
                continue
            final_review = ai_review(
                output, {**clip, "streamer": "Kai Cenat", "platform": clip.get("platform", "twitch")},
                "\nInspect the entire rendered video including both panels and all edges. "
                "Classify branding in JSON as branding: clean, kai_platform_only, other or uncertain. "
                "kai_platform_only means the only logos are native YouTube/Twitch badges belonging "
                "to Kai’s own channel overlay. For any other logo use other; if unclear use uncertain.",
            )
            branding = (final_review or {}).get("branding")
            if branding not in ("clean", "kai_platform_only"):
                summary(f"Candidate {index + 1}: logo-free final edit not confirmed.")
                continue
            selected.append((rank, output, start, end, clip, info, branding))
            if len(selected) >= 3:
                break
        if shortlist and not reviewed:
            raise RuntimeError("Could not download and review any shortlisted clip.")
        if not selected:
            summary("No eligible GTA VI moment found.")
            return
        summary(f"Previews created: {len(selected)}.")
        for number, (_, rendered, start, end, clip, info, branding) in enumerate(
            sorted(selected, key=lambda result: result[0], reverse=True), 1
        ):
            name = "kai-preview.mp4" if number == 1 else f"kai-preview-{number}.mp4"
            shutil.copyfile(rendered, name)
            source_offset = clip.get("section", (0,))[0]
            summary(f"{name}: {clip['url']} ({source_offset + start:.1f}–{source_offset + end:.1f}s). Branding: {branding}.")
        summary("Suggested caption: Kai Cenat during the GTA 6 marathon. #KAICENAT")
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            print("ready=true", file=out)


if __name__ == "__main__":
    main()
