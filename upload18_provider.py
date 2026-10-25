import hashlib
import os
import logging
import subprocess
from pathlib import Path
import requests

logger = logging.getLogger(__name__)


class Upload18Provider:
    """Server-side Upload18 API client. Keep the restricted key in env only."""

    def __init__(self):
        self.base_url = os.getenv("UPLOAD18_API_BASE", "https://upload18.net/api").rstrip("/")
        self.api_key = os.getenv("UPLOAD18_API_KEY", "").strip()
        self.category_id = os.getenv("UPLOAD18_CATEGORY_ID", "19")
        self.folder_id = os.getenv("UPLOAD18_FOLDER_ID", "").strip()

    @property
    def configured(self):
        return bool(self.api_key)

    def _headers(self, token=None):
        key = token or self.api_key
        return {"Authorization": f"Bearer {key}", "X-API-Key": key,
                "Accept": "application/json", "User-Agent": "AnimeHub.Bot/1.0"}

    def folders(self):
        if not self.configured:
            return []
        response = requests.get(f"{self.base_url}/getfolders", headers=self._headers(), timeout=20)
        response.raise_for_status()
        return response.json()

    def videos(self, page=1, per_page=100):
        if not self.configured:
            return []
        response = requests.get(f"{self.base_url}/myvideo", headers=self._headers(),
                                params={"page": page, "per_page": per_page}, timeout=20)
        response.raise_for_status()
        return response.json()

    def create_upload_token(self, expires_in=21600, folder_id=None):
        if not self.configured:
            raise RuntimeError("UPLOAD18_API_KEY is not configured")
        data = {"expires_in": min(max(int(expires_in), 60), 86400), "cid": self.category_id}
        chosen_folder = folder_id or self.folder_id
        if chosen_folder:
            data["fid"] = chosen_folder
        response = requests.post(f"{self.base_url}/uploadtoken", headers=self._headers(), data=data, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("upload_token"):
            raise RuntimeError("Upload18 did not return an upload token")
        return payload

    def upload_url(self, video_url, title=None, folder_id=None):
        if not self.configured:
            raise RuntimeError("UPLOAD18_API_KEY is not configured")
        data = {"videoUrl": video_url, "cid": self.category_id}
        chosen_folder = folder_id or self.folder_id
        if chosen_folder:
            data["fid"] = chosen_folder
        if title:
            data["title"] = title
        response = requests.post(f"{self.base_url}/uploadbyurl", headers=self._headers(), data=data, timeout=60)
        response.raise_for_status()
        return response.json()

    def upload_file(self, path, title=None, folder_id=None, retries=2):
        """Upload a local temporary file with a short-lived token; never expose the master key."""
        if not self.configured:
            raise RuntimeError("UPLOAD18_API_KEY is not configured")
        path = Path(path)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Upload file is missing or empty")
        token_payload = self.create_upload_token(21600, folder_id)
        token = token_payload["upload_token"]
        upload_url = token_payload.get("upload_url") or f"{self.base_url}/upload"
        data = {"apikey": token, "cid": self.category_id}
        chosen_folder = folder_id or self.folder_id
        if chosen_folder:
            data["fid"] = chosen_folder
        if title:
            data["title"] = title
        for attempt in range(retries + 1):
            with path.open("rb") as stream:
                response = requests.post(upload_url, headers=self._headers(token), data=data,
                                         files={"video": (path.name, stream, "video/mp4")}, timeout=300)
            if response.status_code < 500:
                response.raise_for_status()
                return response.json()
            if attempt < retries:
                import time
                time.sleep(60)
        response.raise_for_status()

    def download_and_upload(self, direct_url, title=None, folder_id=None, temp_dir="/tmp/animehub"):
        """Download to server temp storage, validate, upload to Upload18, then delete temp file."""
        if not direct_url.startswith("https://"):
            raise ValueError("Only HTTPS direct media URLs are accepted")
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
            if not path.exists() or path.stat().st_size < 1024:
                raise RuntimeError("Downloaded media is empty or too small")
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=30)
            if probe.returncode != 0 or not probe.stdout.strip():
                raise RuntimeError("Downloaded file failed media validation")
            return self.upload_file(path, title=title, folder_id=folder_id)
        finally:
            path.unlink(missing_ok=True)

    def detail(self, video_id):
        if not self.configured:
            raise RuntimeError("UPLOAD18_API_KEY is not configured")
        response = requests.get(f"{self.base_url}/getvideodetail/{video_id}", headers=self._headers(), timeout=20)
        response.raise_for_status()
        return response.json()
