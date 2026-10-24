import os
import re
import logging
import requests

logger = logging.getLogger(__name__)


class AnimeSlayerProvider:
    """Optional adapter for an authorized Anime Slayer JSON endpoint.

    The APK exposes an internal resolver, not a stable public contract. The
    adapter therefore requires an explicit endpoint supplied by the operator;
    it never treats a player page as a media URL.
    """

    def __init__(self):
        self.enabled = os.getenv("ANIMESLAYER_ENABLED", "false").lower() == "true"
        self.endpoint = os.getenv("ANIMESLAYER_ENDPOINT", "").strip()

    @staticmethod
    def _is_direct(url):
        clean = str(url or "").lower().split("?", 1)[0]
        return any(x in clean for x in (".mp4", ".mkv", ".webm", ".m3u8", ".ts", "/api/file/"))

    @staticmethod
    def _download_url(url):
        if "pixeldrain.com" in url:
            match = re.search(r"(?:/u/|/api/file/|/l/)([A-Za-z0-9]+)", url)
            if match:
                return f"https://pixeldrain.com/api/file/{match.group(1)}?download"
        clean = str(url).lower().split("?", 1)[0]
        return url if any(x in clean for x in (".mp4", ".mkv", ".webm", "/api/file/")) else None

    def _walk(self, value, quality="غير محددة", language="غير محددة"):
        records = []
        if isinstance(value, dict):
            context = " ".join(f"{k} {v}" for k, v in value.items() if isinstance(v, (str, int, float))).lower()
            qualities = re.findall(r"(?:2160|1440|1080|720|576|480|360)p", context)
            quality = qualities[0] if qualities else quality
            for key, child in value.items():
                records.extend(self._walk(child, quality, language))
        elif isinstance(value, list):
            for child in value:
                records.extend(self._walk(child, quality, language))
        elif isinstance(value, str):
            for url in re.findall(r"https?://[^\s\"'<>]+", value):
                url = url.rstrip(")]}>;,)")
                if not self._is_direct(url):
                    continue
                download = self._download_url(url)
                if not download:
                    continue
                records.append({
                    "name": "Anime Slayer",
                    "source": "Anime Slayer API",
                    "quality": quality,
                    "language": language,
                    "view_url": download,
                    "download_url": download,
                    "link_type": "direct_download",
                    "verified": False,
                    "is_pd": "pixeldrain.com" in url,
                })
        return records

    def fetch(self, title, episode=1):
        if not self.enabled or not self.endpoint:
            return []
        try:
            response = requests.get(
                self.endpoint,
                params={"q": title, "title": title, "episode": episode},
                timeout=12,
                headers={"Accept": "application/json", "User-Agent": "AnimeHub.Bot/1.0"},
            )
            if response.status_code != 200:
                logger.warning("Anime Slayer endpoint returned HTTP %s", response.status_code)
                return []
            payload = response.json()
            unique = {}
            for record in self._walk(payload):
                unique[record["download_url"]] = record
            return list(unique.values())
        except Exception as exc:
            logger.warning("Anime Slayer provider unavailable: %s", type(exc).__name__)
            return []
