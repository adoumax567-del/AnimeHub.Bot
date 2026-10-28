"""Bunny Stream upload — official server path (no proxy).

Bunny docs (https://docs.bunny.net/docs/stream-uploading-videos-through-our-http-api):
  1) POST /library/{id}/videos          → create video, get guid
  2) PUT  /library/{id}/videos/{guid}   → raw binary body (application/octet-stream)

PixelDrain direct URLs work for *our* server download; Bunny Fetch often cannot
pull them (blocked hosts). So we always: download on server → PUT to Bunny.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests


class BunnyStreamProvider:
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
            raise ValueError(f"[config] library {requested_id} not in BUNNY_STREAM_LIBRARIES")
        provider.library_id = requested_id
        keys = self._load_library_keys()
        provider.library_keys = keys
        provider.api_key = keys.get(requested_id) or self.api_key
        if not provider.api_key:
            raise RuntimeError(
                f"[config] missing API key for library {requested_id} "
                f"(set BUNNY_STREAM_API_KEY or BUNNY_STREAM_LIBRARY_KEYS)"
            )
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
        if r.status_code >= 400:
            raise RuntimeError(f"[collection] create HTTP {r.status_code}: {(r.text or '')[:200]}")
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
        if r.status_code >= 400:
            raise RuntimeError(f"[create] HTTP {r.status_code}: {(r.text or '')[:250]}")
        data = r.json()
        if not (data.get("guid") or data.get("videoId")):
            raise RuntimeError(f"[create] no guid in response: {str(data)[:200]}")
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
            raise RuntimeError(f"[collection] attach HTTP {r.status_code}: {(r.text or '')[:200]}")

    def delete_video(self, video_id):
        r = requests.delete(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def video(self, video_id):
        r = requests.get(self._url(f"/videos/{video_id}"), headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"[verify] get video HTTP {r.status_code}: {(r.text or '')[:200]}")
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
        """Official Bunny binary upload (PUT raw octets)."""
        path = Path(path)
        size = path.stat().st_size if path.is_file() else 0
        if size < 50_000:
            raise ValueError(f"[upload] file too small ({size} bytes)")
        params = {}
        # enabledResolutions expects values like 720p per docs
        if quality:
            q = str(quality).strip()
            if q.isdigit():
                q = f"{q}p"
            if re.fullmatch(r"\d{3,4}p", q, re.I):
                params["enabledResolutions"] = q.lower()
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
            raise RuntimeError(f"[upload] PUT HTTP {r.status_code}: {(r.text or '')[:300]}")
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {"raw": (r.text or "")[:120]}
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(f"[upload] Bunny rejected: {body.get('message') or body}")
        return body or {"success": True, "statusCode": r.status_code, "bytes": size}

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
                raise RuntimeError("[verify] Bunny status=5 (encoding/upload failed)")
            time.sleep(4)
        raise RuntimeError(
            f"[verify] storage still 0 after {timeout_seconds}s "
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
        last_err = None
        for attempt in range(2):
            try:
                with requests.get(
                    direct_url, stream=True, timeout=(30, 1200),
                    headers=headers, allow_redirects=True,
                ) as resp:
                    if resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    clen = resp.headers.get("Content-Length")
                    if "text/html" in ctype:
                        raise RuntimeError(f"HTML not media (Content-Type={ctype})")
                    written = 0
                    with dest_path.open("wb") as out:
                        for chunk in resp.iter_content(1024 * 1024):
                            if chunk:
                                out.write(chunk)
                                written += len(chunk)
                if written < 50_000:
                    dest_path.unlink(missing_ok=True)
                    raise RuntimeError(f"too small ({written} bytes)")
                if clen and abs(int(clen) - written) > max(1024, int(clen) * 0.01):
                    # incomplete download
                    dest_path.unlink(missing_ok=True)
                    raise RuntimeError(f"incomplete {written}/{clen} bytes")
                return written, ctype, clen
            except Exception as exc:
                last_err = exc
                dest_path.unlink(missing_ok=True)
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"[download] {direct_url[:80]} → {last_err}") from last_err
        raise RuntimeError(f"[download] failed: {last_err}")


    def fetch_from_url(self, source_url, title, collection_id=None, extra_headers=None):
        """Bot-style remote pull: tell Bunny to download the direct URL (with browser headers).

        Used by bots/scripts that want 'transfer by URL' without proxy.
        Library-level endpoint: POST /videos/fetch
        Body may include headers so hosts like PixelDrain accept the request.
        """
        body = {
            "url": source_url,
            "title": str(title)[:255],
            "headers": extra_headers or {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Referer": "https://pixeldrain.com/",
            },
        }
        params = {"collectionId": str(collection_id)} if collection_id else None
        r = requests.post(
            self._url("/videos/fetch"),
            headers={**self._headers(), "Content-Type": "application/json"},
            params=params,
            json=body,
            timeout=60,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"[fetch] HTTP {r.status_code}: {(r.text or '')[:250]}")
        data = r.json() if r.content else {}
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"[fetch] rejected: {data.get('message') or data}")
        video_id = None
        if isinstance(data, dict):
            video_id = data.get("guid") or data.get("videoId")
        if not video_id:
            # Accept-only response: locate by title briefly
            for _ in range(6):
                time.sleep(2)
                found = self.find_video(title, collection_id)
                if found:
                    video_id = found.get("guid") or found.get("videoId")
                    break
        if not video_id:
            raise RuntimeError("[fetch] accepted but no video id returned")
        stored = self.wait_until_stored(video_id, timeout_seconds=90)
        return video_id, stored, data

    def server_download_and_upload(
        self, direct_url, title, quality=None, collection_name=None,
        temp_dir="/tmp/animehub", public_base=None,
    ):
        """Server-only: download direct URL → create video → PUT binary → verify.

        Errors are tagged: [config] [download] [create] [upload] [verify] [collection]
        so the admin panel shows the exact failing stage.
        """
        if not self.configured:
            raise RuntimeError("[config] Bunny not configured (library id / API key)")

        direct = self.normalize_direct_download_url(direct_url)
        if not direct:
            raise RuntimeError(f"[download] not a direct media URL: {str(direct_url)[:120]}")

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

        # --- Path A: bot remote transfer (Bunny Fetch + browser headers) ---
        # Same idea as Telegram URL-uploader bots + Bunny advanced fetch headers.
        try:
            video_id, stored, accepted = self.fetch_from_url(direct, title, collection_id)
            storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)
            if collection_name and not collection_id:
                col = self.ensure_collection(collection_name)
                self.set_video_collection(video_id, col.get("guid"))
            return {
                "title": str(title)[:255],
                "video_id": video_id,
                "direct_url": direct,
                "fetched_directly": True,
                "accepted": True,
                "encoding_pending": stored.get("status") not in (3, 4),
                "validation": {
                    "source": "bot-fetch-with-headers",
                    "method": "Bunny Fetch + User-Agent/Referer (bot direct transfer)",
                    "bunny_storageSize": storage,
                    "bunny_status": stored.get("status"),
                },
                **self.playback(video_id),
            }
        except Exception as exc:
            errors.append(str(exc))

        # --- Path B: bot local transfer (download on server then PUT) ---
        # Same pattern as URL-uploader bots: bot downloads, then uploads destination.
        path = Path(temp_dir or "/tmp/animehub") / (
            hashlib.sha256(direct.encode()).hexdigest()[:24] + ".mp4"
        )
        video_id = None
        stage = "download"
        try:
            written, ctype, clen = self.download_direct(direct, path)

            stage = "create"
            created = self.create_video(title, collection_id=None)
            video_id = created.get("guid") or created.get("videoId")

            stage = "upload"
            uploaded = self.upload_file(path, video_id, quality)

            stage = "verify"
            stored = self.wait_until_stored(video_id, timeout_seconds=180)
            storage = int(stored.get("storageSize") or stored.get("storage_size") or 0)

            stage = "collection"
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
                    "source": "bot-download-then-put",
                    "method": "server download + Bunny PUT binary",
                    "downloaded_bytes": written,
                    "content_type": ctype,
                    "content_length": clen,
                    "bunny_storageSize": storage,
                    "bunny_status": stored.get("status"),
                    "prior_fetch_errors": errors,
                },
                **self.playback(video_id),
            }
        except Exception as exc:
            msg = str(exc)
            if not msg.startswith("["):
                msg = f"[{stage}] {msg}"
            if errors:
                msg = msg + " | fetch_tried: " + " ; ".join(errors)
            if video_id:
                try:
                    meta = self.video(video_id)
                    if int(meta.get("storageSize") or meta.get("storage_size") or 0) <= 0:
                        self.delete_video(video_id)
                except Exception:
                    pass
            raise RuntimeError(msg) from exc
        finally:
            path.unlink(missing_ok=True)
