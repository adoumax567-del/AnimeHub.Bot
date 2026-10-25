import re
from collections import defaultdict


def safe_name(value):
    value = re.sub(r"[\\/:*?\"<>|]+", " ", str(value or "")).strip()
    return re.sub(r"\s+", " ", value)[:180] or "Unknown"


def quality_name(quality):
    return safe_name(quality or "source")


def extension_for(link_or_ext):
    text = str(link_or_ext or "").lower()
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        if ext in text:
            return ext[1:]
    return "mp4"


def build_media_path(work, kind, episode=1, season=1, quality="source", language=None, ext="mp4"):
    title = safe_name(work)
    q = quality_name(quality)
    lang = f" - {safe_name(language)}" if language and language != "غير محددة" else ""
    extension = extension_for(ext)
    if str(kind).lower() == "film":
        return f"{title}/Movie/{title} - {q}{lang}.{extension}"
    return f"{title}/Season {int(season):02d}/{title} S{int(season):02d}E{int(episode):02d} - {q}{lang}.{extension}"


def build_library_plan(items):
    """Create an organized, duplicate-free library manifest from resolved media records."""
    files = []
    seen_paths = set()
    duplicate_paths = []
    groups = defaultdict(list)
    for item in items or []:
        work = item.get("work") or item.get("title")
        kind = item.get("type", "series")
        episode = int(item.get("episode", 1))
        season = int(item.get("season", 1))
        quality = item.get("quality", "source")
        path = build_media_path(work, kind, episode, season, quality, item.get("language"), item.get("extension", "mp4"))
        record = {"path": path, "work": safe_name(work), "type": kind, "season": season,
                  "episode": episode, "quality": quality_name(quality), "language": item.get("language"),
                  "source_url": item.get("source_url"), "status": item.get("status", "planned")}
        if path in seen_paths:
            duplicate_paths.append(path)
            record["status"] = "duplicate"
        else:
            seen_paths.add(path)
            files.append(record)
        groups[(safe_name(work), str(kind).lower(), season)].append(episode)

    missing = []
    for (work, kind, season), episodes in groups.items():
        if kind == "film":
            continue
        expected = set(range(1, max(episodes) + 1)) if episodes else set()
        missing_numbers = sorted(expected - set(episodes))
        if missing_numbers:
            missing.append({"work": work, "season": season, "episodes": missing_numbers,
                            "reason": "Missing episode number in the requested library plan"})

    files.sort(key=lambda x: (x["work"].lower(), x["season"], x["episode"], x["quality"]))
    return {"root": "AnimeHub Library", "files": files, "file_count": len(files),
            "duplicates": sorted(set(duplicate_paths)), "missing": missing,
            "valid": not duplicate_paths and not missing}
