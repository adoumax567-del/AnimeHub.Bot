import hashlib
import json
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
        self.library_keys = self._load_library_keys()
        self.library_cdns = self._load_library_map("BUNNY_STREAM_LIBRARY_CDNS")

    @staticmethod
    def _load_library_map(name):
        raw = os.getenv(name, "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {str(key).strip(): str(value).strip() for key, value in data.items()
                if str(key).strip() and str(value).strip()}

    def _load_library_keys(self):
        """Load per-library write keys from a JSON environment secret."""
        raw = os.getenv("BUNNY_STREAM_LIBRARY_KEYS", "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {str(key).strip(): str(value).strip() for key, value in data.items()
                if str(key).strip() and str(value).strip()}

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
        requested_id = str(library_id or self.library_id).strip()
        allowed_ids = {str(entry.get("id")).strip() for entry in self.library_catalog}
        if requested_id not in allowed_ids:
            raise ValueError("Selected Bunny library is not configured")
        provider.library_id = requested_id
        provider.api_key = provider.library_keys.get(provider.library_id) or self.library_keys.get(provider.library_id) or self.api_key
        provider.base_url = self.base_url
        provider.cdn_hostname = self.cdn_hostname
        provider.player_base = self.player_base
        provider.library_keys = self.library_keys
        provider.library_cdns = self.library_cdns
        provider.cdn_hostname = provider.library_cdns.get(provider.library_id, provider.cdn_hostname)
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
        params = {"collectionId": str(collection_id)} if collection_id else None
        response = requests.post(self._url("/videos/fetch"), headers={**self._headers(), "Content-Type": "application/json"},
                                 params=params, json=body, timeout=60)
        response.raise_for_status()
        return response.json()

    def delete_video(self, video_id):
        response = requests.delete(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()

    def remote_fetch_and_wait(self, direct_url, title, quality=None, collection_id=None):
        """Submit a Bunny fetch, then poll until content arrives or fails.

        Bunny may accept the request (success=true) and still fail later while
        pulling from hosts that block datacenter IPs (PixelDrain, etc.). We poll
        the created video and only return success when storageSize grows or the
        status reaches encoding/finished. On failure we delete the empty video
        and raise so the caller can fall back to server-side upload.
        """
        body = {"url": direct_url, "title": str(title)[:255]}
        params = {"collectionId": str(collection_id)} if collection_id else None
        response = None
        for attempt in range(3):
            response = requests.post(
                self._url("/videos/fetch"), headers={**self._headers(), "Content-Type": "application/json"},
                params=params, json=body, timeout=60,
            )
            if response.status_code != 429 or attempt == 2:
                break
            time.sleep(2 ** attempt)
        if response is None:
            raise RuntimeError("Bunny remote fetch returned no response")
        response.raise_for_status()
        try:
            accepted = response.json()
        except ValueError as exc:
            raise RuntimeError("Bunny remote fetch returned invalid JSON") from exc
        if not isinstance(accepted, dict):
            raise RuntimeError("Bunny remote fetch returned an invalid response")
        if accepted.get("success") is not True:
            status = accepted.get("statusCode")
            message = str(accepted.get("message") or "request rejected")[:300]
            raise RuntimeError(f"Bunny remote fetch rejected ({status}): {message}")

        video_id = accepted.get("guid") or accepted.get("videoId")
        if not video_id:
            # Bunny sometimes returns only StatusModel; locate the new entry.
            deadline = time.time() + 20
            while time.time() < deadline and not video_id:
                time.sleep(2)
                try:
                    video = self.find_video(title, collection_id)
                except Exception:
                    video = None
                if video:
                    video_id = video.get("guid") or video.get("videoId")
        if not video_id:
            raise RuntimeError("Bunny remote fetch accepted but no video id was created")

        # Poll until Bunny actually received bytes or reported failure.
        # Status: 0 queued, 1 processing, 2 encoding, 3 finished, 4 res finished, 5 failed
        poll_seconds = max(15, min(int(os.getenv("BUNNY_FETCH_POLL_SECONDS", "75")), 180))
        deadline = time.time() + poll_seconds
        last = {}
        while time.time() < deadline:
            try:
                last = self.video(video_id) or {}
            except Exception as exc:
                last = {"_poll_error": str(exc)}
                time.sleep(3)
                continue
            status = last.get("status")
            storage = int(last.get("storageSize") or last.get("storage_size") or 0)
            length = int(last.get("length") or 0)
            # Content arrived or encoding started with real data.
            if storage > 0 or length > 0 or status in (2, 3, 4):
                result = {
                    "title": str(title)[:255],
                    "video_id": video_id,
                    "fetched_directly": True,
                    "accepted": True,
                    "encoding_pending": status not in (3, 4),
                    "validation": {
                        "source": "Bunny remote fetch verified",
                        "status": status,
                        "storageSize": storage,
                        "length": length,
                    },
                }
                result.update(self.playback(video_id))
                return result
            # Explicit failure from Bunny.
            if status == 5:
                try:
                    self.delete_video(video_id)
                except Exception:
                    pass
                raise RuntimeError(
                    f"Bunny remote fetch failed for source (status=5, storage={storage})"
                )
            time.sleep(4)

        # Timed out with empty video — treat as failure and clean up.
        try:
            self.delete_video(video_id)
        except Exception:
            pass
        raise RuntimeError(
            f"Bunny remote fetch timed out with empty video "
            f"(status={last.get('status')}, storage={last.get('storageSize') or 0})"
        )

    def video(self, video_id):
        response = requests.get(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def playback(self, video_id):
        return {"embed_url": f"{self.player_base}/{self.library_id}/{video_id}",
                "cdn_url": f"https://{self.cdn_hostname}/{video_id}/playlist.m3u8" if self.cdn_hostname else None}

    @staticmethod
    def validate_media(path):
        """Reject truncated/non-media downloads before creating a Bunny video.

        ffprobe is preferred but optional: on hosts without ffmpeg we fall back
        to a minimum size check so server-side upload still works on Render.
        """
        path = Path(path)
        size = path.stat().st_size if path.is_file() else 0
        if size < 50_000:
            raise RuntimeError(f"Downloaded media is too small ({size} bytes)")
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return {"duration": None, "size": size, "source": "size-only (ffprobe missing)"}
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
        return {"duration": duration, "size": size}

    def server_download_and_upload(self, direct_url, title, quality=None, collection_name=None, temp_dir="/tmp/animehub"):
        """Stage an authorized direct file on the server, validate it, then upload to Bunny.

        Strategy (fast + reliable for lists):
        1. Prefer Bunny remote fetch when enabled — Bunny pulls the file (fast).
           We poll until real content arrives; on failure we clean up and fall back.
        2. Fall back to download-on-server + binary upload (works when hosts block Bunny).
        """
        if not direct_url.startswith("https://"):
            raise ValueError("Only HTTPS direct media URLs are accepted")
        collection = self.ensure_collection(collection_name) if collection_name else None
        collection_id = collection.get("guid") if collection else None
        existing = self.find_video(title, collection_id)
        if existing:
            video_id = existing.get("guid") or existing.get("videoId")
            storage = int(existing.get("storageSize") or existing.get("storage_size") or 0)
            status = existing.get("status")
            # Reuse only videos that actually have content (skip prior Upload failed).
            if video_id and (storage > 0 or status in (2, 3, 4)):
                return {"video_id": video_id, "created": existing,
                        "deduplicated": True, "validation": {"source": "existing Bunny video"},
                        **self.playback(video_id)}
            if video_id:
                try:
                    self.delete_video(video_id)
                except Exception:
                    pass

        prefer_remote = os.getenv("BUNNY_REMOTE_FETCH", "true").strip().lower() in {"1", "true", "yes", "on"}
        remote_error = None
        if prefer_remote:
            try:
                return self.remote_fetch_and_wait(direct_url, title, quality, collection_id)
            except Exception as exc:
                remote_error = str(exc)[:300]
                # Fall through to server-side path.

        os.makedirs(temp_dir, exist_ok=True)
        filename = hashlib.sha256(direct_url.encode()).hexdigest()[:24] + ".mp4"
        path = Path(temp_dir) / filename
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
            }
            with requests.get(direct_url, stream=True, timeout=(20, 600), headers=headers, allow_redirects=True) as response:
                response.raise_for_status()
                with path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if path.stat().st_size < 50_000:
                raise RuntimeError("Downloaded media is empty or too small")
            validation = self.validate_media(path)
            if remote_error:
                validation["remote_fetch_fallback"] = remote_error
            created = self.create_video(title, collection_id)
            video_id = created.get("guid") or created.get("videoId")
            if not video_id:
                raise RuntimeError("Bunny did not return a video id")
            uploaded = self.upload_file(path, video_id, quality)
            return {
                "video_id": video_id,
                "created": created,
                "uploaded": uploaded,
                "fetched_directly": False,
                "accepted": True,
                "encoding_pending": True,
                "validation": validation,
                **self.playback(video_id),
            }
        finally:
            path.unlink(missing_ok=True)
