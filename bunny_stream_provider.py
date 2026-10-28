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
            response = requests.put(
                self._url(f"/videos/{video_id}"),
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                params=params, data=stream, timeout=(30, 1800),
            )
        response.raise_for_status()
        return response.json() if response.content else {"success": True}

    def upload_stream(self, chunk_iter, video_id, quality=None, content_length=None):
        """Upload binary chunks to Bunny without buffering the whole file on disk."""
        params = {}
        if quality:
            params["enabledResolutions"] = str(quality)
        headers = {**self._headers(), "Content-Type": "application/octet-stream"}
        if content_length and int(content_length) > 0:
            headers["Content-Length"] = str(int(content_length))
        response = requests.put(
            self._url(f"/videos/{video_id}"),
            headers=headers,
            params=params,
            data=chunk_iter,
            timeout=(30, 3600),
        )
        response.raise_for_status()
        return response.json() if response.content else {"success": True}

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

        Bunny may accept the request then fail while pulling from hosts that
        block datacenter IPs (PixelDrain, etc.). We only treat the job as
        successful when storageSize grows or encoding starts. On failure the
        empty video is deleted and an error is raised for fallback.
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

        # Short poll — PixelDrain usually fails fast for Bunny; don't waste minutes.
        poll_seconds = max(10, min(int(os.getenv("BUNNY_FETCH_POLL_SECONDS", "40")), 120))
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
            if status == 5:
                try:
                    self.delete_video(video_id)
                except Exception:
                    pass
                raise RuntimeError(
                    f"Bunny remote fetch failed for source (status=5, storage={storage})"
                )
            time.sleep(3)

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

        ffprobe is preferred but optional so Render hosts without ffmpeg still work.
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

    @staticmethod
    def _looks_like_direct_media(url):
        u = (url or "").lower()
        if "pixeldrain.com/api/file/" in u:
            return True
        if any(u.endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".m4v", ".ts")):
            return True
        if "mediafire.com" in u and "/file/" in u:
            # MediaFire page links are not direct binaries.
            return False
        return u.startswith("https://")

    def wait_until_stored(self, video_id, timeout_seconds=120):
        """Poll Bunny until the video has real bytes or is marked failed."""
        deadline = time.time() + max(15, min(int(timeout_seconds), 300))
        last = {}
        while time.time() < deadline:
            try:
                last = self.video(video_id) or {}
            except Exception as exc:
                last = {"_error": str(exc)}
                time.sleep(3)
                continue
            status = last.get("status")
            storage = int(last.get("storageSize") or last.get("storage_size") or 0)
            length = int(last.get("length") or 0)
            if storage > 0 or length > 0 or status in (2, 3, 4):
                return last
            if status == 5:
                raise RuntimeError(
                    f"Bunny marked video as failed (status=5, storage={storage})"
                )
            time.sleep(3)
        raise RuntimeError(
            f"Bunny video still empty after upload "
            f"(status={last.get('status')}, storage={last.get('storageSize') or 0})"
        )

    def stream_url_to_bunny(self, direct_url, title, quality=None, collection_id=None):
        """Download from source and upload to Bunny.

        Uses a temp file (one film at a time) because pure generator streaming to
        Bunny often drops mid-transfer on small hosts, leaving an empty video
        that then disappears from the library.
        """
        if not direct_url.startswith("https://"):
            raise ValueError("Only HTTPS direct media URLs are accepted")
        if not self._looks_like_direct_media(direct_url):
            raise RuntimeError(f"URL is not a direct media file: {direct_url[:120]}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
        temp_dir = Path(os.getenv("ANIMEHUB_TEMP_DIR", "/tmp/animehub"))
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / (hashlib.sha256(direct_url.encode()).hexdigest()[:24] + ".mp4")
        video_id = None
        try:
            with requests.get(
                direct_url, stream=True, timeout=(20, 900),
                headers=headers, allow_redirects=True,
            ) as src:
                src.raise_for_status()
                content_type = (src.headers.get("Content-Type") or "").lower()
                content_length = src.headers.get("Content-Length")
                if "text/html" in content_type:
                    raise RuntimeError("Source returned HTML instead of a media file")
                if content_length and int(content_length) < 50_000:
                    raise RuntimeError(f"Source media is too small ({content_length} bytes)")
                # Guard free-tier disk: refuse absurd sizes
                if content_length and int(content_length) > 2_500_000_000:
                    raise RuntimeError(f"Source media too large for host ({content_length} bytes)")

                written = 0
                with path.open("wb") as out:
                    for chunk in src.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        out.write(chunk)
                        written += len(chunk)
                if written < 50_000:
                    raise RuntimeError(f"Downloaded media is too small ({written} bytes)")

            validation = self.validate_media(path)
            created = self.create_video(title, collection_id)
            video_id = created.get("guid") or created.get("videoId")
            if not video_id:
                raise RuntimeError("Bunny did not return a video id")

            uploaded = self.upload_file(path, video_id, quality)
            # Critical: confirm Bunny actually stored bytes (not an empty shell).
            stored = self.wait_until_stored(video_id, timeout_seconds=int(os.getenv("BUNNY_VERIFY_SECONDS", "90")))
            storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)

            return {
                "title": str(title)[:255],
                "video_id": video_id,
                "created": created,
                "uploaded": uploaded,
                "fetched_directly": False,
                "accepted": True,
                "encoding_pending": stored.get("status") not in (3, 4),
                "validation": {
                    **validation,
                    "source": "download-then-upload",
                    "content_type": content_type,
                    "content_length": int(content_length) if content_length else written,
                    "bunny_storageSize": storage,
                    "bunny_status": stored.get("status"),
                },
                **self.playback(video_id),
            }
        except Exception:
            # Do NOT delete the Bunny video on failure — empty/failed entries help
            # diagnose; collections alone without videos was the previous symptom
            # when we auto-deleted after a partial create.
            raise
        finally:
            path.unlink(missing_ok=True)

    def server_download_and_upload(self, direct_url, title, quality=None, collection_name=None, temp_dir="/tmp/animehub"):
        """Upload authorized media to Bunny (reliable path for PixelDrain).

        Default: download on server → upload binary → verify storageSize > 0.
        Optional: short Bunny remote-fetch first when BUNNY_REMOTE_FETCH=true.
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
            if video_id and (storage > 0 or status in (2, 3, 4)):
                return {
                    "video_id": video_id,
                    "created": existing,
                    "deduplicated": True,
                    "accepted": True,
                    "validation": {"source": "existing Bunny video", "storageSize": storage},
                    **self.playback(video_id),
                }
            # Old empty/failed shell with same title — remove only those with 0 bytes.
            if video_id and storage <= 0 and status in (0, 5, None):
                try:
                    self.delete_video(video_id)
                except Exception:
                    pass

        errors = []
        prefer_remote = os.getenv("BUNNY_REMOTE_FETCH", "false").strip().lower() in {"1", "true", "yes", "on"}
        if prefer_remote:
            try:
                return self.remote_fetch_and_wait(direct_url, title, quality, collection_id)
            except Exception as exc:
                errors.append(f"remote_fetch: {exc}")

        try:
            result = self.stream_url_to_bunny(direct_url, title, quality, collection_id)
            if errors:
                result.setdefault("validation", {})["prior_errors"] = errors
            return result
        except Exception as exc:
            errors.append(f"download_upload: {exc}")
            raise RuntimeError("Upload failed: " + " | ".join(errors)) from exc
