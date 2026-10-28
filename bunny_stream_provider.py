import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

import requests


class BunnyStreamProvider:
    """Bunny Stream client: pass true direct-download URLs into Bunny Fetch.

    PixelDrain links from the catalog are already normalized to:
      https://pixeldrain.com/api/file/{id}?download
    Bunny often cannot pull that host itself, so we optionally wrap the same
    direct URL behind a signed proxy on our public host, then call Bunny Fetch.
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
        self.public_base = (
            os.getenv("ANIMEHUB_PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
        ).rstrip("/")
        self.proxy_secret = (
            os.getenv("ANIMEHUB_PROXY_SECRET")
            or os.getenv("ANIMEHUB_ADMIN_SESSION_SECRET")
            or self.api_key
            or "animehub-proxy"
        ).strip()

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
        keys = __class__._load_library_map("BUNNY_STREAM_LIBRARY_KEYS")
        provider.library_keys = keys
        provider.api_key = keys.get(requested_id) or self.api_key
        if not provider.api_key:
            raise RuntimeError(f"No Bunny API key for library {requested_id}")
        provider.base_url = self.base_url
        provider.player_base = self.player_base
        provider.library_cdns = self.library_cdns
        provider.cdn_hostname = self.library_cdns.get(requested_id, self.cdn_hostname)
        provider.public_base = self.public_base
        provider.proxy_secret = self.proxy_secret
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

    def wait_until_stored(self, video_id, timeout_seconds=400):
        deadline = time.time() + max(30, min(int(timeout_seconds), 900))
        last = {}
        while time.time() < deadline:
            try:
                last = self.video(video_id) or {}
            except Exception as exc:
                last = {"_error": str(exc)}
                time.sleep(5)
                continue
            status = last.get("status")
            storage = int(last.get("storageSize") or last.get("storage_size") or 0)
            length = int(last.get("length") or 0)
            if storage > 0 or length > 0 or status in (2, 3, 4):
                return last
            if status == 5:
                raise RuntimeError(f"Bunny fetch/encode failed (status=5, storage={storage})")
            time.sleep(5)
        raise RuntimeError(
            f"Bunny video still empty after fetch "
            f"(status={last.get('status')}, storage={last.get('storageSize') or 0})"
        )

    # ---- direct-download URL helpers (same rules as data_manager.resolve_pd) ----

    @staticmethod
    def normalize_direct_download_url(url):
        """Return a true direct-download URL, or None if not usable."""
        u = str(url or "").strip()
        if not u.startswith("https://"):
            return None
        lower = u.lower()
        # PixelDrain: force API file download endpoint
        if "pixeldrain.com" in lower:
            m = re.search(r"(?:/u/|/api/file/|/l/)([a-zA-Z0-9]+)", u)
            if not m:
                return None
            file_id = m.group(1)
            return f"https://pixeldrain.com/api/file/{file_id}?download"
        path = lower.split("?", 1)[0]
        if any(path.endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".m4v")):
            return u
        if "/api/file/" in lower:
            return u
        return None

    def sign_proxy(self, media_url, ttl_seconds=7200):
        exp = str(int(time.time()) + int(ttl_seconds))
        msg = f"{exp}:{media_url}".encode()
        sig = hmac.new(self.proxy_secret.encode(), msg, hashlib.sha256).hexdigest()
        return exp, sig

    @classmethod
    def verify_proxy(cls, media_url, exp, sig, secret):
        try:
            if int(exp) < int(time.time()):
                return False
        except ValueError:
            return False
        msg = f"{exp}:{media_url}".encode()
        expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(sig or ""))

    def build_proxy_url(self, media_url, public_base=None):
        base = (public_base or self.public_base or "").rstrip("/")
        if not base:
            return None
        exp, sig = self.sign_proxy(media_url)
        q = urllib.parse.urlencode({"u": media_url, "exp": exp, "sig": sig})
        return f"{base}/v1/storage/media-proxy?{q}"

    def bunny_fetch(self, fetch_url, title, collection_id=None):
        """Give Bunny a URL to pull. Returns (video_id, stored_meta)."""
        body = {"url": fetch_url, "title": str(title)[:255]}
        params = {"collectionId": str(collection_id)} if collection_id else None
        response = None
        for attempt in range(3):
            response = requests.post(
                self._url("/videos/fetch"),
                headers={**self._headers(), "Content-Type": "application/json"},
                params=params, json=body, timeout=60,
            )
            if response.status_code != 429 or attempt == 2:
                break
            time.sleep(2 ** attempt)
        response.raise_for_status()
        accepted = response.json()
        if not isinstance(accepted, dict) or accepted.get("success") is not True:
            raise RuntimeError(
                f"Bunny rejected fetch: {accepted.get('statusCode')} {accepted.get('message')}"
            )
        video_id = accepted.get("guid") or accepted.get("videoId")
        if not video_id:
            deadline = time.time() + 25
            while time.time() < deadline and not video_id:
                time.sleep(2)
                found = self.find_video(title, collection_id)
                if found:
                    video_id = found.get("guid") or found.get("videoId")
        if not video_id:
            raise RuntimeError("Bunny accepted fetch but returned no video id")
        stored = self.wait_until_stored(
            video_id, timeout_seconds=int(os.getenv("BUNNY_VERIFY_SECONDS", "400")),
        )
        return video_id, stored

    def server_download_and_upload(
        self, direct_url, title, quality=None, collection_name=None,
        temp_dir="/tmp/animehub", public_base=None,
    ):
        """Use the source direct-download URL with Bunny Fetch (original design).

        Steps:
        1) Normalize to a real direct-download URL (PixelDrain API ?download).
        2) Prefer Bunny Fetch of a signed proxy of that same URL (Bunny can reach us).
        3) Else Bunny Fetch of the direct URL itself (works when host allows Bunny).
        4) Verify storageSize > 0 before success.
        5) Attach collection only after success.
        """
        if not self.configured:
            raise RuntimeError("Bunny Stream is not configured")

        direct = self.normalize_direct_download_url(direct_url)
        if not direct:
            raise RuntimeError(f"Not a direct download URL: {str(direct_url)[:120]}")

        # Reuse finished videos only
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

        errors = []
        collection_id = None
        if collection_name:
            try:
                for c in self.list_collections() or []:
                    if str(c.get("name", "")).casefold() == str(collection_name).strip().casefold():
                        collection_id = c.get("guid")
                        break
            except Exception:
                pass

        # Candidate fetch URLs: proxy(direct) first, then raw direct
        candidates = []
        proxy = self.build_proxy_url(direct, public_base=public_base)
        if proxy:
            candidates.append(("proxy", proxy))
        candidates.append(("direct", direct))
        # Also try PD without ?download (inline) — sometimes easier for fetchers
        if "pixeldrain.com/api/file/" in direct and direct.endswith("?download"):
            candidates.append(("direct_inline", direct.replace("?download", "")))

        last_video_id = None
        for label, fetch_url in candidates:
            try:
                video_id, stored = self.bunny_fetch(fetch_url, title, collection_id)
                last_video_id = video_id
                storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)
                if collection_name and not collection_id:
                    col = self.ensure_collection(collection_name)
                    self.set_video_collection(video_id, col.get("guid"))
                    collection_id = col.get("guid")
                return {
                    "title": str(title)[:255],
                    "video_id": video_id,
                    "direct_url": direct,
                    "fetch_url": fetch_url,
                    "fetch_mode": label,
                    "fetched_directly": True,
                    "accepted": True,
                    "encoding_pending": stored.get("status") not in (3, 4),
                    "validation": {
                        "source": f"bunny-fetch-{label}",
                        "direct_download_url": direct,
                        "bunny_status": stored.get("status"),
                        "bunny_storageSize": storage,
                    },
                    **self.playback(video_id),
                }
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                # Remove empty shell if created
                if last_video_id:
                    try:
                        meta = self.video(last_video_id)
                        if int(meta.get("storageSize") or 0) <= 0:
                            self.delete_video(last_video_id)
                    except Exception:
                        pass
                    last_video_id = None

        raise RuntimeError(
            "Bunny could not pull the direct download URL. Tried: "
            + " | ".join(errors)
        )
