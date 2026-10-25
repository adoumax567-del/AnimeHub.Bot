import os
import logging
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

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

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

    def upload_url(self, video_url, title=None, folder_id=None):
        if not self.configured:
            raise RuntimeError("UPLOAD18_API_KEY is not configured")
        data = {"videoUrl": video_url, "cid": self.category_id}
        chosen_folder = folder_id or self.folder_id
        if chosen_folder:
            data["fid"] = chosen_folder
        if title:
            data["title"] = title
        response = requests.post(f"{self.base_url}/uploadbyurl", headers=self._headers(), data=data, timeout=30)
        response.raise_for_status()
        return response.json()

    def detail(self, video_id):
        if not self.configured:
            raise RuntimeError("UPLOAD18_API_KEY is not configured")
        response = requests.get(f"{self.base_url}/getvideodetail/{video_id}", headers=self._headers(), timeout=20)
        response.raise_for_status()
        return response.json()
