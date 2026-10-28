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
        provider = BunnyStreamProvider()
        requested_id = str(library_id or self.library_id).strip()
        allowed_ids = {str(entry.get("id")).strip() for entry in self.library_catalog}
        if requested_id not in allowed_ids:
            raise ValueError("Selected Bunny library is not configured")
        provider.library_id = requested_id
        provider.api_key = (
            provider.library_keys.get(provider.library_id)
            or self.library_keys.get(provider.library_id)
            or self.api_key
        )
        if not provider.api_key:
            raise RuntimeError(
                f"No Bunny API key configured for library {provider.library_id}. "
                "Set BUNNY_STREAM_API_KEY or BUNNY_STREAM_LIBRARY_KEYS."
            )
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

    def ensure_collection(self, name):
        clean = re.sub(r"[\\/:*?\"<>|]+", " ", str(name or "Unknown")).strip()[:255] or "Unknown"
        for collection in self.list_collections() or []:
            if str(collection.get("name", "")).casefold() == clean.casefold():
                return collection
        response = requests.post(
            self._url("/collections"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"name": clean}, timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def create_video(self, title, collection_id=None):
        body = {"title": str(title)[:255]}
        if collection_id:
            body["collectionId"] = collection_id
        response = requests.post(
            self._url("/videos"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def set_video_collection(self, video_id, collection_id):
        if not video_id or not collection_id:
            return
        response = requests.post(
            self._url(f"/videos/{video_id}"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"collectionId": str(collection_id)},
            timeout=30,
        )
        if response.status_code not in (200, 204):
            response.raise_for_status()

    def upload_file(self, path, video_id, quality=None):
        path = Path(path)
        size = path.stat().st_size if path.is_file() else 0
        if size < 50_000:
            raise ValueError(f"Video file is missing or too small ({size} bytes)")
        params = {}
        if quality:
            params["enabledResolutions"] = str(quality).replace("p", "") if str(quality).endswith("p") else str(quality)
        # Bunny expects raw binary body. Use open file handle so requests streams from disk.
        with path.open("rb") as stream:
            response = requests.put(
                self._url(f"/videos/{video_id}"),
                headers={
                    **self._headers(),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
                params=params,
                data=stream,
                timeout=(60, 3600),
            )
        if response.status_code >= 400:
            body = (response.text or "")[:300]
            raise RuntimeError(f"Bunny upload HTTP {response.status_code}: {body}")
        return response.json() if response.content else {"success": True, "statusCode": response.status_code}

    def delete_video(self, video_id):
        response = requests.delete(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()

    def video(self, video_id):
        response = requests.get(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def playback(self, video_id):
        return {
            "embed_url": f"{self.player_base}/{self.library_id}/{video_id}",
            "cdn_url": (
                f"https://{self.cdn_hostname}/{video_id}/playlist.m3u8"
                if self.cdn_hostname else None
            ),
        }

    def wait_until_stored(self, video_id, timeout_seconds=180):
        deadline = time.time() + max(20, min(int(timeout_seconds), 600))
        last = {}
        while time.time() < deadline:
            try:
                last = self.video(video_id) or {}
            except Exception as exc:
                last = {"_error": str(exc)}
                time.sleep(4)
                continue
            status = last.get("status")
            storage = int(last.get("storageSize") or last.get("storage_size") or 0)
            length = int(last.get("length") or 0)
            if storage > 0 or length > 0 or status in (2, 3, 4):
                return last
            if status == 5:
                raise RuntimeError(f"Bunny encoding failed (status=5, storage={storage})")
            time.sleep(4)
        raise RuntimeError(
            f"Bunny video still empty after upload "
            f"(status={last.get('status')}, storage={last.get('storageSize') or 0})"
        )

    @staticmethod
    def validate_media(path):
        path = Path(path)
        size = path.stat().st_size if path.is_file() else 0
        if size < 50_000:
            raise RuntimeError(f"Downloaded media is too small ({size} bytes)")
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return {"duration": None, "size": size, "source": "size-only"}
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode != 0 or not result.stdout.strip():
            # Size is enough to proceed; ffprobe failure is a warning only.
            return {"duration": None, "size": size, "source": "size-only (ffprobe failed)"}
        try:
            duration = float(result.stdout.strip().splitlines()[0])
        except ValueError:
            return {"duration": None, "size": size, "source": "size-only (bad duration)"}
        if duration <= 0:
            raise RuntimeError("Downloaded media has zero duration")
        return {"duration": duration, "size": size}

    @staticmethod
    def _looks_like_direct_media(url):
        u = (url or "").lower()
        if "pixeldrain.com/api/file/" in u:
            return True
        if any(u.split("?")[0].endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".m4v", ".ts")):
            return True
        if "mediafire.com" in u:
            return False
        return False

    def _download_to_temp(self, direct_url, temp_dir):
        """Download a direct media URL to disk. Raises with a clear reason on failure."""
        if not self._looks_like_direct_media(direct_url):
            raise RuntimeError(f"Not a direct media URL (skip): {direct_url[:100]}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
        temp_dir = Path(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / (hashlib.sha256(direct_url.encode()).hexdigest()[:24] + ".mp4")

        with requests.get(
            direct_url, stream=True, timeout=(30, 1200),
            headers=headers, allow_redirects=True,
        ) as src:
            src.raise_for_status()
            content_type = (src.headers.get("Content-Type") or "").lower()
            content_length = src.headers.get("Content-Length")
            if "text/html" in content_type:
                raise RuntimeError("Source returned HTML, not a video file")
            if content_length and int(content_length) < 50_000:
                raise RuntimeError(f"Source too small: {content_length} bytes")
            if content_length and int(content_length) > 3_000_000_000:
                raise RuntimeError(f"Source too large for this host: {content_length} bytes")

            written = 0
            with path.open("wb") as out:
                for chunk in src.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
                        written += len(chunk)

        if written < 50_000:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"Download incomplete: only {written} bytes")
        return path, written, content_type, content_length

    def server_download_and_upload(
        self, direct_url, title, quality=None, collection_name=None, temp_dir="/tmp/animehub",
    ):
        """Root-fixed upload flow:

        1) Download file first (no Bunny side-effects yet).
        2) Create video + upload bytes.
        3) Verify storageSize > 0.
        4) Only then create/attach collection.

        This prevents empty collections when download/upload fails.
        """
        if not (self.library_id and self.api_key):
            raise RuntimeError("Bunny Stream is not configured (missing library id or API key)")
        if not direct_url.startswith("https://"):
            raise ValueError("Only HTTPS URLs are accepted")

        # Reuse only videos that already have real content.
        existing = self.find_video(title, None)
        if existing:
            video_id = existing.get("guid") or existing.get("videoId")
            storage = int(existing.get("storageSize") or existing.get("storage_size") or 0)
            status = existing.get("status")
            if video_id and (storage > 0 or status in (2, 3, 4)):
                if collection_name:
                    try:
                        col = self.ensure_collection(collection_name)
                        self.set_video_collection(video_id, col.get("guid"))
                    except Exception:
                        pass
                return {
                    "video_id": video_id,
                    "created": existing,
                    "deduplicated": True,
                    "accepted": True,
                    "validation": {"source": "existing Bunny video", "storageSize": storage},
                    **self.playback(video_id),
                }

        path = None
        video_id = None
        try:
            # --- Step 1: download ONLY (no Bunny writes) ---
            path, written, content_type, content_length = self._download_to_temp(
                direct_url, temp_dir or "/tmp/animehub",
            )
            validation = self.validate_media(path)

            # --- Step 2: create empty video object on Bunny ---
            created = self.create_video(title, collection_id=None)
            video_id = created.get("guid") or created.get("videoId")
            if not video_id:
                raise RuntimeError("Bunny create_video returned no guid")

            # --- Step 3: PUT binary ---
            uploaded = self.upload_file(path, video_id, quality)

            # --- Step 4: confirm bytes landed ---
            stored = self.wait_until_stored(
                video_id, timeout_seconds=int(os.getenv("BUNNY_VERIFY_SECONDS", "180")),
            )
            storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)
            if storage <= 0 and stored.get("status") not in (2, 3, 4):
                raise RuntimeError("Upload reported OK but Bunny storage is still 0")

            # --- Step 5: collection ONLY after success ---
            collection = None
            if collection_name:
                collection = self.ensure_collection(collection_name)
                try:
                    self.set_video_collection(video_id, collection.get("guid"))
                except Exception as col_exc:
                    # Video is stored; collection attach is secondary.
                    validation["collection_attach_error"] = str(col_exc)[:200]

            return {
                "title": str(title)[:255],
                "video_id": video_id,
                "created": created,
                "uploaded": uploaded,
                "collection": collection,
                "fetched_directly": False,
                "accepted": True,
                "encoding_pending": stored.get("status") not in (3, 4),
                "validation": {
                    **validation,
                    "source": "download-then-upload-verified",
                    "content_type": content_type,
                    "downloaded_bytes": written,
                    "content_length": int(content_length) if content_length else written,
                    "bunny_storageSize": storage,
                    "bunny_status": stored.get("status"),
                },
                **self.playback(video_id),
            }
        except Exception as exc:
            # Cleanup empty video shell if we created one but never stored bytes.
            if video_id:
                try:
                    meta = self.video(video_id)
                    storage = int(meta.get("storageSize") or meta.get("storage_size") or 0)
                    if storage <= 0:
                        self.delete_video(video_id)
                except Exception:
                    pass
            raise RuntimeError(f"Bunny upload failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)
