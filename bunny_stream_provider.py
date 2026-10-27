import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests


class BunnyStreamProvider:
    """Bunny Stream server-side client. The library key must stay in env."""

    def __init__(self):
        self.library_id = os.getenv("BUNNY_STREAM_LIBRARY_ID", "790172").strip()
        self.api_key = os.getenv("BUNNY_STREAM_API_KEY", "").strip()
        self.base_url = os.getenv("BUNNY_STREAM_API_BASE", "https://video.bunnycdn.com").rstrip("/")
        self.cdn_hostname = os.getenv("BUNNY_STREAM_CDN_HOSTNAME", "").strip()
        self.player_base = os.getenv("BUNNY_STREAM_PLAYER_BASE", "https://iframe.mediadelivery.net/embed").rstrip("/")
        self.library_name = os.getenv("BUNNY_STREAM_LIBRARY_NAME", self.library_id).strip()
        self.library_catalog = self._load_library_catalog()

    def _load_library_catalog(self):
        """Load selectable libraries as id:name pairs without exposing API keys."""
        raw = os.getenv("BUNNY_STREAM_LIBRARIES", "").strip()
        entries = []
        if raw:
            for part in raw.split(","):
                library_id, _, name = part.partition(":")
                if library_id.strip():
                    entries.append({"id": library_id.strip(), "name": name.strip() or library_id.strip()})
        if not any(item["id"] == self.library_id for item in entries):
            entries.insert(0, {"id": self.library_id, "name": self.library_name or self.library_id})
        return entries

    @property
    def configured(self):
        return bool(self.library_id and self.api_key)

    def for_library(self, library_id):
        """Return an isolated provider instance for a selected Bunny library."""
        provider = BunnyStreamProvider()
        provider.library_id = str(library_id or self.library_id).strip()
        provider.api_key = self.api_key
        provider.base_url = self.base_url
        provider.cdn_hostname = self.cdn_hostname
        provider.player_base = self.player_base
        return provider

    def _headers(self):
        return {"AccessKey": self.api_key, "Accept": "application/json"}

    def _url(self, path):
        return f"{self.base_url}/library/{self.library_id}{path}"

    def list_collections(self):
        response = requests.get(self._url("/collections"), headers=self._headers(), timeout=30)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else data.get("items", data.get("data", []))

    def list_videos(self, page=1, items_per_page=100):
        response = requests.get(
            self._url("/videos"), headers=self._headers(),
            params={"page": page, "itemsPerPage": items_per_page}, timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else data.get("items", data.get("data", []))

    def find_video(self, title, collection_id=None):
        wanted = str(title or "").strip().casefold()
        if not wanted:
            return None
        for video in self.list_videos():
            if str(video.get("title", "")).strip().casefold() != wanted:
                continue
            video_collection = video.get("collectionId") or video.get("collection_id")
            if collection_id and video_collection and str(video_collection) != str(collection_id):
                continue
            return video
        return None

    def list_libraries(self):
        response = requests.get(f"{self.base_url}/libraries", headers=self._headers(), timeout=30)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else data.get("items", data.get("data", []))

    def ensure_collection(self, name):
        clean = re.sub(r"[\\/:*?\"<>|]+", " ", str(name or "Unknown")).strip()[:255] or "Unknown"
        for collection in self.list_collections() or []:
            if str(collection.get("name", "")).casefold() == clean.casefold():
                return collection
        response = requests.post(self._url("/collections"), headers={**self._headers(), "Content-Type": "application/json"},
                                 json={"name": clean}, timeout=30)
        response.raise_for_status()
        return response.json()

    def create_video(self, title, collection_id=None):
        body = {"title": str(title)[:255]}
        if collection_id:
            body["collectionId"] = collection_id
        response = requests.post(self._url("/videos"), headers={**self._headers(), "Content-Type": "application/json"},
                                 json=body, timeout=30)
        response.raise_for_status()
        return response.json()

    def upload_file(self, path, video_id, quality=None):
        path = Path(path)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Video file is missing or empty")
        params = {}
        if quality:
            params["enabledResolutions"] = str(quality)
        with path.open("rb") as stream:
            response = requests.put(self._url(f"/videos/{video_id}"), headers={**self._headers(), "Content-Type": "application/octet-stream"},
                                    params=params, data=stream, timeout=(30, 1800))
        response.raise_for_status()
        return response.json()

    def fetch_url(self, direct_url, title, collection_id=None):
        body = {"url": direct_url, "title": str(title)[:255]}
        if collection_id:
            body["collectionId"] = collection_id
        response = requests.post(self._url("/videos/fetch"), headers={**self._headers(), "Content-Type": "application/json"},
                                 json=body, timeout=60)
        response.raise_for_status()
        return response.json()

    def video(self, video_id):
        response = requests.get(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def playback(self, video_id):
        return {"embed_url": f"{self.player_base}/{self.library_id}/{video_id}",
                "cdn_url": f"https://{self.cdn_hostname}/{video_id}/playlist.m3u8" if self.cdn_hostname else None}

    @staticmethod
    def validate_media(path):
        """Reject truncated/non-media downloads before creating a Bunny video."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe is required for media validation")
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError("Downloaded file failed ffprobe validation")
        try:
            duration = float(result.stdout.strip().splitlines()[0])
        except ValueError as exc:
            raise RuntimeError("Downloaded file has invalid media duration") from exc
        if duration <= 0:
            raise RuntimeError("Downloaded media has zero duration")
        return {"duration": duration}

    def server_download_and_upload(self, direct_url, title, quality=None, collection_name=None, temp_dir="/tmp/animehub"):
        """Stage an authorized direct file on the server, validate it, then upload to Bunny."""
        if not direct_url.startswith("https://"):
            raise ValueError("Only HTTPS direct media URLs are accepted")
        collection = self.ensure_collection(collection_name) if collection_name else None
        collection_id = collection.get("guid") if collection else None
        existing = self.find_video(title, collection_id)
        if existing:
            video_id = existing.get("guid") or existing.get("videoId")
            if video_id:
                return {"video_id": video_id, "created": existing,
                        "deduplicated": True, "validation": {"source": "existing Bunny video"},
                        **self.playback(video_id)}
        os.makedirs(temp_dir, exist_ok=True)
        filename = hashlib.sha256(direct_url.encode()).hexdigest()[:24] + ".mp4"
        path = Path(temp_dir) / filename
        try:
            with requests.get(direct_url, stream=True, timeout=(20, 300), headers={"User-Agent": "AnimeHub.Bot/1.0"}) as response:
                response.raise_for_status()
                with path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if path.stat().st_size < 1024:
                raise RuntimeError("Downloaded media is empty or too small")
            validation = self.validate_media(path)
            created = self.create_video(title, collection_id)
            video_id = created.get("guid") or created.get("videoId")
            if not video_id:
                raise RuntimeError("Bunny did not return a video id")
            uploaded = self.upload_file(path, video_id, quality)
            return {"video_id": video_id, "created": created, "uploaded": uploaded,
                    "validation": validation, **self.playback(video_id)}
        finally:
            path.unlink(missing_ok=True)
