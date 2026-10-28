import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests


class BunnyStreamProvider:
    """Server-only upload: download direct URL on this host, then PUT to Bunny.

    No proxy. No Bunny remote-fetch of PixelDrain.
    """

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
        return {
            str(k).strip(): str(v).strip()
            for k, v in data.items()
            if str(k).strip() and str(v).strip()
        }

    def _load_library_keys(self):
        return self._load_library_map("BUNNY_STREAM_LIBRARY_KEYS")

    def _load_library_catalog(self):
        raw = os.getenv("BUNNY_STREAM_LIBRARIES", "").strip()
        entries = []
        if raw:
            for part in raw.split(","):
                library_id, _, name = part.partition(":")
                if library_id.strip():
                    entries.append({
                        "id": library_id.strip(),
                        "name": name.strip() or library_id.strip(),
                    })
        if not any(item["id"] == self.library_id for item in entries):
            entries.insert(0, {"id": self.library_id, "name": self.library_name or self.library_id})
        return entries

    @property
    def configured(self):
        return bool(self.library_id and self.api_key)

    def for_library(self, library_id):
        provider = BunnyStreamProvider()
        requested_id = str(library_id or self.library_id).strip()
        allowed = {str(e.get("id")).strip() for e in self.library_catalog}
        if requested_id not in allowed:
            raise ValueError("Selected Bunny library is not configured")
        provider.library_id = requested_id
        keys = self._load_library_keys()
        provider.library_keys = keys
        provider.api_key = keys.get(requested_id) or self.api_key
        if not provider.api_key:
            raise RuntimeError(f"No Bunny API key for library {requested_id}")
        provider.base_url = self.base_url
        provider.player_base = self.player_base
        provider.library_cdns = self.library_cdns
        provider.cdn_hostname = self.library_cdns.get(requested_id, self.cdn_hostname)
        provider.library_catalog = self.library_catalog
        return provider

    def _headers(self):
        return {"AccessKey": self.api_key, "Accept": "application/json"}

    def _url(self, path):
        return f"{self.base_url}/library/{self.library_id}{path}"

    def list_collections(self):
        r = requests.get(self._url("/collections"), headers=self._headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("items", data.get("data", []))

    def list_videos(self, page=1, items_per_page=100):
        r = requests.get(
            self._url("/videos"), headers=self._headers(),
            params={"page": page, "itemsPerPage": items_per_page}, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("items", data.get("data", []))

    def find_video(self, title, collection_id=None):
        wanted = str(title or "").strip().casefold()
        if not wanted:
            return None
        for video in self.list_videos():
            if str(video.get("title", "")).strip().casefold() != wanted:
                continue
            vc = video.get("collectionId") or video.get("collection_id")
            if collection_id and vc and str(vc) != str(collection_id):
                continue
            return video
        return None

    def ensure_collection(self, name):
        clean = re.sub(r'[\\/:*?"<>|]+', " ", str(name or "Unknown")).strip()[:255] or "Unknown"
        for c in self.list_collections() or []:
            if str(c.get("name", "")).casefold() == clean.casefold():
                return c
        r = requests.post(
            self._url("/collections"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"name": clean}, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def create_video(self, title, collection_id=None):
        body = {"title": str(title)[:255]}
        if collection_id:
            body["collectionId"] = collection_id
        r = requests.post(
            self._url("/videos"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def set_video_collection(self, video_id, collection_id):
        if not video_id or not collection_id:
            return
        r = requests.post(
            self._url(f"/videos/{video_id}"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"collectionId": str(collection_id)},
            timeout=30,
        )
        if r.status_code not in (200, 204):
            r.raise_for_status()

    def delete_video(self, video_id):
        r = requests.delete(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def video(self, video_id):
        r = requests.get(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def playback(self, video_id):
        return {
            "embed_url": f"{self.player_base}/{self.library_id}/{video_id}",
            "cdn_url": (
                f"https://{self.cdn_hostname}/{video_id}/playlist.m3u8"
                if self.cdn_hostname else None
            ),
        }

    def upload_file(self, path, video_id, quality=None):
        path = Path(path)
        size = path.stat().st_size if path.is_file() else 0
        if size < 50_000:
            raise ValueError(f"File too small ({size} bytes)")
        params = {}
        if quality:
            q = str(quality).rstrip("pP")
            if q.isdigit():
                params["enabledResolutions"] = q
        with path.open("rb") as stream:
            r = requests.put(
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
        if r.status_code >= 400:
            raise RuntimeError(f"Bunny PUT HTTP {r.status_code}: {(r.text or '')[:250]}")
        return r.json() if r.content else {"success": True, "statusCode": r.status_code}

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
            progress = int(last.get("encodeProgress") or 0)
            if storage > 0 or length > 0 or progress > 0 or status in (1, 2, 3, 4):
                return last
            if status == 5:
                raise RuntimeError(f"Bunny encoding failed (status=5)")
            time.sleep(4)
        raise RuntimeError(
            f"Bunny storage still 0 after upload "
            f"(status={last.get('status')}, storage={last.get('storageSize') or 0})"
        )

    @staticmethod
    def normalize_direct_download_url(url):
        u = str(url or "").strip()
        if not u.startswith("https://"):
            return None
        lower = u.lower()
        if "pixeldrain.com" in lower:
            m = re.search(r"(?:/u/|/api/file/|/l/)([a-zA-Z0-9]+)", u)
            if not m:
                return None
            return f"https://pixeldrain.com/api/file/{m.group(1)}?download"
        path = lower.split("?", 1)[0]
        if any(path.endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".m4v")):
            return u
        if "/api/file/" in lower:
            return u
        return None

    def download_direct(self, direct_url, dest_path):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(
            direct_url, stream=True, timeout=(30, 1200),
            headers=headers, allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise RuntimeError("Source returned HTML, not a video file")
            written = 0
            with dest_path.open("wb") as out:
                for chunk in resp.iter_content(1024 * 1024):
                    if chunk:
                        out.write(chunk)
                        written += len(chunk)
        if written < 50_000:
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(f"Download too small ({written} bytes)")
        return written

    def server_download_and_upload(
        self, direct_url, title, quality=None, collection_name=None,
        temp_dir="/tmp/animehub", public_base=None,
    ):
        """Download on this server from the direct link, then upload to Bunny."""
        if not self.configured:
            raise RuntimeError("Bunny Stream is not configured")

        direct = self.normalize_direct_download_url(direct_url)
        if not direct:
            raise RuntimeError(f"Not a direct download URL: {str(direct_url)[:120]}")

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
                    "deduplicated": True,
                    "accepted": True,
                    "direct_url": direct,
                    "validation": {"source": "existing", "storageSize": storage},
                    **self.playback(video_id),
                }

        path = Path(temp_dir or "/tmp/animehub") / (
            hashlib.sha256(direct.encode()).hexdigest()[:24] + ".mp4"
        )
        video_id = None
        try:
            # 1) Download direct link on server
            written = self.download_direct(direct, path)

            # 2) Create video on Bunny
            created = self.create_video(title, collection_id=None)
            video_id = created.get("guid") or created.get("videoId")
            if not video_id:
                raise RuntimeError("Bunny create_video returned no id")

            # 3) Upload binary
            uploaded = self.upload_file(path, video_id, quality)

            # 4) Confirm bytes on Bunny
            stored = self.wait_until_stored(video_id, timeout_seconds=180)
            storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)

            # 5) Collection only after success
            if collection_name:
                col = self.ensure_collection(collection_name)
                try:
                    self.set_video_collection(video_id, col.get("guid"))
                except Exception:
                    pass

            return {
                "title": str(title)[:255],
                "video_id": video_id,
                "direct_url": direct,
                "fetched_directly": False,
                "accepted": True,
                "encoding_pending": stored.get("status") not in (3, 4),
                "validation": {
                    "source": "server-download-then-upload",
                    "downloaded_bytes": written,
                    "bunny_storageSize": storage,
                    "bunny_status": stored.get("status"),
                },
                **self.playback(video_id),
            }
        except Exception as exc:
            if video_id:
                try:
                    meta = self.video(video_id)
                    if int(meta.get("storageSize") or meta.get("storage_size") or 0) <= 0:
                        self.delete_video(video_id)
                except Exception:
                    pass
            raise RuntimeError(f"Server upload failed: {type(exc).__name__}: {exc}") from exc
        finally:
            path.unlink(missing_ok=True)
