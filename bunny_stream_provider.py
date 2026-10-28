"""Bunny Stream — direct-link only (no server download).

The free host was dying because the bot downloaded full videos into memory/disk.
Previous working approach: give Bunny the direct URL and let Bunny pull it.

No local download. No temp files. No full-file PUT from this server.
"""

import json
import os
import re
import time

import requests


class BunnyStreamProvider:
    def __init__(self):
        self.library_id = os.getenv("BUNNY_STREAM_LIBRARY_ID", "790172").strip()
        self.api_key = os.getenv("BUNNY_STREAM_API_KEY", "").strip()
        self.base_url = os.getenv("BUNNY_STREAM_API_BASE", "https://video.bunnycdn.com").rstrip("/")
        self.cdn_hostname = os.getenv("BUNNY_STREAM_CDN_HOSTNAME", "").strip()
        self.player_base = os.getenv(
            "BUNNY_STREAM_PLAYER_BASE", "https://iframe.mediadelivery.net/embed"
        ).rstrip("/")
        self.library_name = os.getenv("BUNNY_STREAM_LIBRARY_NAME", self.library_id).strip()
        self.library_catalog = self._load_library_catalog()
        self.library_keys = self._load_library_keys()
        self.library_cdns = self._load_library_map("BUNNY_STREAM_LIBRARY_CDNS")
        # How long to wait for Bunny to pull bytes (seconds)
        self.fetch_poll_seconds = int(os.getenv("BUNNY_FETCH_POLL_SECONDS", "90") or 90)

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
            entries.insert(0, {
                "id": self.library_id,
                "name": self.library_name or self.library_id,
            })
        return entries

    @property
    def configured(self):
        return bool(self.library_id and self.api_key)

    def for_library(self, library_id):
        provider = BunnyStreamProvider()
        requested_id = str(library_id or self.library_id).strip()
        allowed = {str(e.get("id")).strip() for e in self.library_catalog}
        if requested_id not in allowed:
            raise ValueError(f"[config] library {requested_id} not configured")
        provider.library_id = requested_id
        keys = self._load_library_keys()
        provider.library_keys = keys
        provider.api_key = keys.get(requested_id) or self.api_key
        if not provider.api_key:
            raise RuntimeError(f"[config] no API key for library {requested_id}")
        provider.base_url = self.base_url
        provider.player_base = self.player_base
        provider.library_cdns = self.library_cdns
        provider.cdn_hostname = self.library_cdns.get(requested_id, self.cdn_hostname)
        provider.library_catalog = self.library_catalog
        provider.fetch_poll_seconds = self.fetch_poll_seconds
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
            self._url("/videos"),
            headers=self._headers(),
            params={"page": page, "itemsPerPage": items_per_page},
            timeout=30,
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
            json={"name": clean},
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"[collection] HTTP {r.status_code}: {(r.text or '')[:200]}")
        return r.json()

    def create_video(self, title, collection_id=None):
        body = {"title": str(title)[:255]}
        if collection_id:
            body["collectionId"] = collection_id
        r = requests.post(
            self._url("/videos"),
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"[create] HTTP {r.status_code}: {(r.text or '')[:250]}")
        data = r.json()
        if not (data.get("guid") or data.get("videoId")):
            raise RuntimeError(f"[create] no guid: {str(data)[:200]}")
        return data

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
            raise RuntimeError(f"[collection] attach HTTP {r.status_code}")

    def delete_video(self, video_id):
        r = requests.delete(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def video(self, video_id):
        r = requests.get(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"[verify] HTTP {r.status_code}: {(r.text or '')[:200]}")
        return r.json()

    def playback(self, video_id):
        return {
            "embed_url": f"{self.player_base}/{self.library_id}/{video_id}",
            "cdn_url": (
                f"https://{self.cdn_hostname}/{video_id}/playlist.m3u8"
                if self.cdn_hostname else None
            ),
        }

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

    def fetch_direct_url(self, direct_url, title, collection_id=None):
        """Tell Bunny to pull the direct URL. No bytes pass through this server."""
        headers_for_source = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://pixeldrain.com/",
        }
        body = {
            "url": direct_url,
            "title": str(title)[:255],
            "headers": headers_for_source,
        }
        params = {}
        if collection_id:
            params["collectionId"] = str(collection_id)

        # Library-level fetch (Bunny pulls the URL itself)
        r = requests.post(
            self._url("/videos/fetch"),
            headers={**self._headers(), "Content-Type": "application/json"},
            params=params or None,
            json=body,
            timeout=60,
        )
        if r.status_code >= 400:
            # Fallback: create video then per-video fetch
            created = self.create_video(title, collection_id=collection_id)
            video_id = created.get("guid") or created.get("videoId")
            r2 = requests.post(
                self._url(f"/videos/{video_id}/fetch"),
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"url": direct_url, "headers": headers_for_source},
                timeout=60,
            )
            if r2.status_code >= 400:
                try:
                    self.delete_video(video_id)
                except Exception:
                    pass
                raise RuntimeError(
                    f"[fetch] HTTP {r.status_code}/{(r.text or '')[:120]} "
                    f"| per-video HTTP {r2.status_code}/{(r2.text or '')[:120]}"
                )
            return video_id, r2.json() if r2.content else {}

        data = r.json() if r.content else {}
        video_id = None
        if isinstance(data, dict):
            video_id = data.get("guid") or data.get("videoId") or data.get("id")
        if not video_id:
            # Accept-only response: locate by title shortly after
            for _ in range(8):
                time.sleep(2)
                found = self.find_video(title, collection_id)
                if found:
                    video_id = found.get("guid") or found.get("videoId")
                    break
        if not video_id:
            raise RuntimeError("[fetch] accepted but no video id returned")
        return video_id, data

    def wait_until_stored(self, video_id, timeout_seconds=90):
        deadline = time.time() + max(20, min(int(timeout_seconds), 180))
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
            # Success signals
            if storage > 0 or length > 0 or progress > 0 or status in (1, 2, 3, 4):
                return last
            # Explicit failure
            if status == 5:
                raise RuntimeError("[verify] Bunny status=5 (fetch/encode failed)")
            time.sleep(4)
        storage = int(last.get("storageSize") or last.get("storage_size") or 0)
        if storage > 0:
            return last
        raise RuntimeError(
            f"[verify] still empty after {timeout_seconds}s "
            f"(status={last.get('status')}, storage={storage})"
        )

    def server_download_and_upload(
        self, direct_url, title, quality=None, collection_name=None,
        temp_dir=None, public_base=None,
    ):
        """Compatibility entry used by animehub_api.

        Does NOT download on this server.
        Only forwards the direct URL to Bunny Fetch.
        """
        if not self.configured:
            raise RuntimeError("[config] Bunny not configured")

        direct = self.normalize_direct_download_url(direct_url)
        if not direct:
            raise RuntimeError(f"[fetch] not a direct media URL: {str(direct_url)[:120]}")

        # Reuse existing non-empty video with same title
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
                    "fetched_directly": True,
                    "validation": {"source": "existing", "storageSize": storage},
                    **self.playback(video_id),
                }
            # Remove empty shell left by a previous failed fetch
            if video_id and storage <= 0:
                try:
                    self.delete_video(video_id)
                except Exception:
                    pass

        collection_id = None
        # Attach collection only AFTER success when possible; still pass if known
        if collection_name:
            try:
                for c in self.list_collections() or []:
                    if str(c.get("name", "")).casefold() == str(collection_name).strip().casefold():
                        collection_id = c.get("guid")
                        break
            except Exception:
                pass

        video_id = None
        try:
            video_id, accepted = self.fetch_direct_url(direct, title, collection_id)
            stored = self.wait_until_stored(video_id, timeout_seconds=self.fetch_poll_seconds)
            storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)

            if collection_name and not collection_id:
                col = self.ensure_collection(collection_name)
                try:
                    self.set_video_collection(video_id, col.get("guid"))
                except Exception:
                    pass

            return {
                "title": str(title)[:255],
                "video_id": video_id,
                "direct_url": direct,
                "fetched_directly": True,
                "accepted": True,
                "encoding_pending": stored.get("status") not in (3, 4),
                "validation": {
                    "source": "bunny-fetch-direct-url",
                    "method": "no server download — direct link only",
                    "bunny_storageSize": storage,
                    "bunny_status": stored.get("status"),
                },
                **self.playback(video_id),
            }
        except Exception as exc:
            msg = str(exc)
            if not msg.startswith("["):
                msg = f"[fetch] {msg}"
            if video_id:
                try:
                    meta = self.video(video_id)
                    if int(meta.get("storageSize") or meta.get("storage_size") or 0) <= 0:
                        self.delete_video(video_id)
                except Exception:
                    pass
            raise RuntimeError(msg) from exc
