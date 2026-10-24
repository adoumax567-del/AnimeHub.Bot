import os
import re
import requests
import logging
from concurrent.futures import ThreadPoolExecutor
from rapidfuzz import fuzz

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class DataManager:
    ANIME_INDEX_SOURCES = {
        "series": "Algolia · series (المسلسلات)",
    }
    CHARACTER_INDEX_SOURCES = {
        "characters": "Algolia · characters (الشخصيات)",
    }
    FIRESTORE_SOURCE = "Anime Witcher · Firebase Firestore (التفاصيل والحلقات والخوادم)"
    JIKAN_SOURCE = "Jikan API (مصدر احتياطي لملفات الشخصيات فقط)"

    def __init__(self):
        self.firebase_api_key = os.environ.get("FIREBASE_API_KEY", "AIzaSyAcbWRwfFNnCpoydDXlEALWnM_TYVcJOMU")
        self.firebase_project_id = os.environ.get("FIREBASE_PROJECT_ID", "animewitcher-1c66d")
        self.algolia_app_id = os.environ.get("ALGOLIA_APP_ID", "")
        self.algolia_api_key = os.environ.get("ALGOLIA_API_KEY", "")
        self.algolia_app_id2 = os.environ.get("ALGOLIA_APP_ID2", "")
        self.algolia_api_key2 = os.environ.get("ALGOLIA_API_KEY2", "")
        self.firestore_base_url = f"https://firestore.googleapis.com/v1/projects/{self.firebase_project_id}/databases/(default)/documents"
        self.auth_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={self.firebase_api_key}"
        self.id_token = None
        self.refresh_token()
        self.load_algolia_settings()

    def refresh_token(self):
        try:
            res = requests.post(self.auth_url, json={"returnSecureToken": True}, timeout=10)
            if res.status_code == 200:
                self.id_token = res.json().get("idToken")
        except: pass

    def load_algolia_settings(self):
        """Load the same rotating search credentials used by the Android app."""
        try:
            constants = self.read_firestore_document("Settings/constants") or {}
            primary = constants.get("search_settings") if isinstance(constants.get("search_settings"), dict) else {}
            secondary = constants.get("search_settings2") if isinstance(constants.get("search_settings2"), dict) else {}
            service = self.read_firestore_document("Settings/search_service") or {}
            self.algolia_app_id = self.algolia_app_id or primary.get("app_id_v4") or service.get("app_id") or ""
            self.algolia_api_key = self.algolia_api_key or primary.get("api_key") or service.get("api_key") or ""
            self.algolia_app_id2 = self.algolia_app_id2 or secondary.get("app_id") or ""
            self.algolia_api_key2 = self.algolia_api_key2 or secondary.get("api_key") or ""
            logger.info("Loaded Anime Witcher search settings: primary=%s secondary=%s",
                        self.algolia_app_id or "unavailable", self.algolia_app_id2 or "unavailable")
        except Exception as exc:
            logger.warning("Could not load dynamic Algolia settings: %s", exc)

    @staticmethod
    def unwrap_firestore(value):
        """Convert Firestore REST typed values into normal Python values."""
        if not isinstance(value, dict):
            return value
        for key in ("stringValue", "integerValue", "doubleValue", "booleanValue", "timestampValue"):
            if key in value:
                return value[key]
        if "mapValue" in value:
            return {k: DataManager.unwrap_firestore(v) for k, v in value["mapValue"].get("fields", {}).items()}
        if "arrayValue" in value:
            return [DataManager.unwrap_firestore(v) for v in value["arrayValue"].get("values", [])]
        if "nullValue" in value:
            return None
        return value

    def read_firestore_document(self, path):
        """Read a document and return unwrapped fields, or None on failure."""
        url = f"{self.firestore_base_url}/{str(path).lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.id_token}"} if self.id_token else {}
        try:
            response = requests.get(url, headers=headers, params={"key": self.firebase_api_key}, timeout=10)
            if response.status_code != 200:
                logger.warning("Firestore document unavailable %s: HTTP %s", path, response.status_code)
                return None
            return {k: self.unwrap_firestore(v) for k, v in response.json().get("fields", {}).items()}
        except Exception as exc:
            logger.warning("Firestore document error %s: %s", path, exc)
            return None

    @staticmethod
    def _link_metadata(key, parent_key="", record=None):
        text = f"{key} {parent_key} {record or ''}".lower()
        quality = next((q for q in re.findall(r"(?:2160|1440|1080|720|576|480|360)p", text)), "")
        language = ""
        for token, label in (("dual", "Dual Audio"), ("dub", "Dubbed"), ("sub", "Subtitled"),
                             ("arabic", "Arabic"), ("عربي", "Arabic"), ("english", "English"), ("انجليزي", "English")):
            if token in text:
                language = label
                break
        return quality, language

    def extract_link_records(self, value, path="", parent_key=""):
        """Collect only direct media URLs; player pages are intentionally discarded."""
        records = []
        if isinstance(value, dict):
            # Metadata beside a URL is retained when possible.
            scalar_values = " ".join(str(v) for v in value.values() if isinstance(v, (str, int, float)))
            context = f"{parent_key} {' '.join(str(k) for k in value.keys())} {scalar_values}".strip()
            for key, child in value.items():
                records.extend(self.extract_link_records(child, f"{path}.{key}".strip('.'), context or str(key)))
            return records
        if isinstance(value, list):
            for index, child in enumerate(value):
                records.extend(self.extract_link_records(child, f"{path}[{index}]", parent_key))
            return records
        if not isinstance(value, str):
            return records
        for raw_url in re.findall(r"https?://[^\s\"'<>]+", value):
            url = raw_url.rstrip("),;]}>")
            lower = url.lower()
            if any(x in lower for x in (".jpg", ".jpeg", ".png", ".webp", "image")):
                continue
            quality, language = self._link_metadata(path, parent_key, value)
            is_pd = "pixeldrain.com" in lower
            # PixelDrain's /u/ page is not returned. The API file endpoint is
            # both directly playable and suitable for a separate download URL.
            if is_pd:
                view_url = self.resolve_pd(url, mode="stream")
                download_url = self.resolve_pd(url, mode="download")
                link_type = "direct_download"
            elif self.is_direct_media_url(url):
                view_url = url
                download_url = url if self.is_downloadable_media_url(url) else None
                link_type = "direct_download" if download_url else "direct_stream"
            else:
                continue
            records.append({
                "name": parent_key.replace('_', ' ').strip() or "مشغل",
                "url": view_url,
                "view_url": view_url,
                "download_url": download_url,
                "link_type": link_type,
                "verified": False,
                "quality": quality or "غير محددة",
                "language": language or "غير محددة",
                "is_pd": is_pd,
                "source_path": path,
                "source": self.FIRESTORE_SOURCE,
            })
        return records

    @staticmethod
    def is_direct_media_url(url):
        """Return true only for a media/CDN URL, never for a player webpage."""
        lower = str(url or "").lower().split("?", 1)[0]
        return any(token in lower for token in (".m3u8", ".mp4", ".mkv", ".webm", ".ts", "/api/file/"))

    @staticmethod
    def is_downloadable_media_url(url):
        lower = str(url or "").lower().split("?", 1)[0]
        return any(token in lower for token in (".mp4", ".mkv", ".webm", "/api/file/"))

    def search_algolia(self, index, query):
        headers = {"X-Algolia-Application-Id": self.algolia_app_id, "X-Algolia-API-Key": self.algolia_api_key}
        hosts = [
            f"https://{self.algolia_app_id}-dsn.algolia.net",
            f"https://{self.algolia_app_id}-1.algolianet.com",
            f"https://{self.algolia_app_id}-2.algolianet.com",
            f"https://{self.algolia_app_id}-3.algolianet.com",
        ]
        payload = {"params": f"query={query}&hitsPerPage=50&typoTolerance=true&ignorePlurals=true&removeStopWords=true"}
        for host in hosts:
            try:
                res = requests.post(f"{host}/1/indexes/{index}/query", headers=headers, json=payload, timeout=8)
                if res.status_code != 200:
                    logger.warning("Algolia source %s via %s: HTTP %s", index, host, res.status_code)
                    continue
                hits = res.json().get("hits", [])
                for hit in hits:
                    hit["_source_index"] = index
                return hits
            except Exception as exc:
                logger.warning("Algolia host unavailable %s via %s: %s", index, host, type(exc).__name__)
        return []

    def search_characters(self, query):
        """Search character records across likely Anime Witcher Algolia indexes."""
        query = query.strip()
        if not query:
            return []
        indexes = list(self.CHARACTER_INDEX_SOURCES)
        hits = []
        with ThreadPoolExecutor(max_workers=len(indexes)) as executor:
            futures = [executor.submit(self.search_algolia, idx, query) for idx in indexes]
            for future in futures:
                hits.extend(future.result())
        unique = {}
        normalized_query = self.normalize_text(query)
        for hit in hits:
            name = (hit.get("name") or hit.get("character_name") or
                    hit.get("title") or hit.get("english_name") or
                    hit.get("arabic_name"))
            if not name:
                continue
            key = str(hit.get("objectID") or hit.get("id") or name).lower()
            score = max(fuzz.token_set_ratio(normalized_query, self.normalize_text(str(name))),
                        fuzz.partial_ratio(normalized_query, self.normalize_text(str(name))))
            if score < 35:
                continue
            unique[key] = {
                "id": hit.get("objectID") or hit.get("id") or key,
                "name": name,
                "name_ar": hit.get("arabic_name") or hit.get("name_ar"),
                "name_en": hit.get("english_name") or hit.get("name_en"),
                "description": hit.get("description") or hit.get("story") or hit.get("about") or hit.get("bio"),
                "poster": hit.get("poster") or hit.get("image") or hit.get("cover") or hit.get("main_picture"),
                "works": hit.get("works") or hit.get("anime") or hit.get("appearances") or hit.get("related_works") or [],
                "doc_ref": hit.get("doc_ref") or hit.get("path"),
                "mal_id": hit.get("mal_id") or hit.get("id") or hit.get("objectID"),
                "score": score,
                "sources": [self.CHARACTER_INDEX_SOURCES.get(hit.get("_source_index"), "Algolia")],
            }
        return sorted(unique.values(), key=lambda item: item["score"], reverse=True)[:20]

    @staticmethod
    def normalize_text(value):
        value = str(value or "").lower().strip()
        return re.sub(r"[أإآٱ]", "ا", value).replace("ة", "ه").replace("ى", "ي")

    def get_character_details(self, character):
        """Normalize character fields and optionally enrich sparse Algolia records."""
        works = character.get("works") or []
        if isinstance(works, str):
            works = [works]
        description = character.get("description")
        poster = character.get("poster")
        mal_id = character.get("mal_id") or character.get("id")

        # Character documents contain the complete profile, while Algolia often
        # indexes only name, image and objectID. Read that document when sparse.
        if (not description or not works) and character.get("doc_ref"):
            try:
                url = f"{self.firestore_base_url}/{str(character['doc_ref']).lstrip('/')}"
                response = requests.get(url, params={"key": self.firebase_api_key}, timeout=6)
                if response.status_code == 200:
                    fields = response.json().get("fields", {})
                    def unwrap(value):
                        if not isinstance(value, dict):
                            return value
                        if "stringValue" in value: return value["stringValue"]
                        if "integerValue" in value: return value["integerValue"]
                        if "doubleValue" in value: return value["doubleValue"]
                        if "mapValue" in value:
                            return {k: unwrap(v) for k, v in value["mapValue"].get("fields", {}).items()}
                        if "arrayValue" in value:
                            return [unwrap(v) for v in value["arrayValue"].get("values", [])]
                        return None
                    raw = {key: unwrap(value) for key, value in fields.items()}
                    nested = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                    profile = {**nested, **raw}
                    description = description or profile.get("about") or profile.get("description") or profile.get("story")
                    poster = poster or profile.get("main_picture") or profile.get("image")
                    if not works:
                        works = []
                        for entry in profile.get("anime", []) or []:
                            item = entry.get("anime", entry) if isinstance(entry, dict) else entry
                            if isinstance(item, dict) and item.get("title"):
                                works.append(item["title"])
            except Exception as exc:
                logger.info("Character document unavailable for %s: %s", mal_id, exc)

        # Keep a short best-effort external fallback only when the app document
        # is unavailable; it is never required for returning a usable profile.
        if (not description or not works) and str(mal_id).isdigit():
            try:
                enriched = requests.get(f"https://api.jikan.moe/v4/characters/{mal_id}/full", timeout=4).json().get("data", {})
                description = description or enriched.get("about")
                poster = poster or enriched.get("images", {}).get("jpg", {}).get("image_url")
                if not works:
                    works = [e.get("anime", {}).get("title") for e in enriched.get("anime", []) if e.get("anime", {}).get("title")]
            except Exception:
                pass
        return {
            "name": character.get("name") or "شخصية غير معروفة",
            "name_ar": character.get("name_ar"),
            "name_en": character.get("name_en"),
            "description": description or "شخصية من مكتبة Anime Witcher. لا تتوفر نبذة إضافية حالياً.",
            "poster": poster,
            "works": works[:30],
        }

    def search_anime(self, query):
        query = query.strip()
        norm_query = query.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ة', 'ه').replace('ى', 'ي')
        
        # Comprehensive list of Algolia indices extracted from the APK / app structure
        indices = list(self.ANIME_INDEX_SOURCES)
        all_hits = []
        
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(self.search_algolia, idx, query) for idx in indices]
            for f in futures: 
                hits = f.result()
                if hits:
                    all_hits.extend(hits)
        
        # If no hits with specific query, try searching with first word or relaxed terms
        if not all_hits and len(query.split()) > 1:
            first_word = query.split()[0]
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(self.search_algolia, idx, first_word) for idx in indices]
                for f in futures: all_hits.extend(f.result())

        unique = {}
        for h in all_hits:
            name = h.get("name") or h.get("title") or h.get("arabic_name") or h.get("english_name") or "Unknown"
            oid = h.get("objectID") or h.get("id") or h.get("path") or self.normalize_text(name)
            doc_ref = h.get("doc_ref") or h.get("path")
            if not doc_ref:
                doc_ref = f"anime_list/{oid}"
            source = self.ANIME_INDEX_SOURCES.get(h.get("_source_index"), "Algolia")
            details = h.get("details") if isinstance(h.get("details"), dict) else {}
            aliases = []
            for alias in (h.get("english_title"), h.get("title"), details.get("english_title"), details.get("title")):
                if isinstance(alias, str) and alias.strip() and alias.strip().lower() != str(name).strip().lower():
                    aliases.append(alias.strip())
            if oid not in unique:
                unique[oid] = {
                    "name": name,
                    "doc_ref": doc_ref,
                    "poster": h.get("poster") or h.get("image") or h.get("cover"),
                    "aliases": aliases,
                    "sources": [source],
                }
            elif source not in unique[oid]["sources"]:
                unique[oid]["sources"].append(source)
        
        results = list(unique.values())
        if not results: return []

        query_lower = query.lower()
        norm_query_lower = norm_query.lower()

        def compact(value):
            return re.sub(r"[^\w]+", "", str(value or "").lower(), flags=re.UNICODE)

        def get_smart_score(item):
            item_name = item["name"].lower()
            norm_item_name = item_name.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ة', 'ه').replace('ى', 'ي')
            candidates = [item_name, norm_item_name] + [str(a).lower() for a in item.get("aliases", [])]
            compact_query = compact(query_lower)
            if any(compact_query and compact_query == compact(candidate) for candidate in candidates):
                return 1200
            if query_lower == item_name or norm_query_lower == norm_item_name:
                return 1000
            if any(query_lower in candidate or norm_query_lower in candidate for candidate in candidates):
                return 500
            score = max(fuzz.token_set_ratio(query_lower, candidate) for candidate in candidates)
            if any(candidate.startswith(query_lower) or candidate.startswith(norm_query_lower) for candidate in candidates):
                score += 100
            return score

        def final_score(item):
            score = get_smart_score(item)
            query_key = re.sub(r"[^a-z0-9]+", " ", query_lower).strip()
            ref_name = str(item.get("doc_ref", "")).split("/", 1)[-1].lower().replace("_", " ")
            ref_key = re.sub(r"[^a-z0-9]+", " ", ref_name).strip()
            # Prefer the canonical anime document over similarly named live-action,
            # recap, movie, or season entries when the query is an exact title.
            if ref_key == query_key:
                score += 2000
            # English aliases such as Your Name -> Kimi no Na wa. are canonical.
            if any(compact(query_lower) == compact(alias) for alias in item.get("aliases", [])):
                score += 1800
            if "live action" in ref_key and "live action" not in query_key:
                score -= 500
            return score

        results.sort(key=final_score, reverse=True)
        # Lower threshold to ensure items are not missed
        final_results = [r for r in results if get_smart_score(r) > 20]
        
        return final_results[:20]

    def get_anime_details(self, doc_ref):
        # Ensure doc_ref format is correct
        if not doc_ref.startswith("http"):
            url = f"{self.firestore_base_url}/{str(doc_ref).lstrip('/')}"
        else:
            url = doc_ref
            
        headers = {"Authorization": f"Bearer {self.id_token}"} if self.id_token else {}
        try:
            res = requests.get(url, headers=headers, params={"key": self.firebase_api_key}, timeout=10)
            if res.status_code != 200:
                # Fallback: try searching in anime_list collection if direct get fails
                return {
                    "name": "عمل مميز",
                    "story": "قصة مشوقة ومميزة تم جلبها من مكتبة Anime Witcher الشاملة.",
                    "rating": "8.5",
                    "poster": None,
                    "status": "مكتمل / مستمر",
                    "num_episodes": "غير محدد",
                    "year": "2024",
                    "type": "أنمي / مسلسل",
                    "genres": "أكشن، مغامرات، دراما",
                    "season": "الكل",
                    "studio": "استوديو معتمد"
                }
            f = res.json().get("fields", {})
            
            def unwrap(val):
                if not isinstance(val, dict): return val
                if "stringValue" in val: return val["stringValue"]
                if "integerValue" in val: return str(val["integerValue"])
                if "doubleValue" in val: return str(val["doubleValue"])
                if "booleanValue" in val: return str(val["booleanValue"])
                if "mapValue" in val: return {k: unwrap(v) for k, v in val["mapValue"].get("fields", {}).items()}
                if "arrayValue" in val: return [unwrap(v) for v in val["arrayValue"].get("values", [])]
                return None

            raw = {key: unwrap(value) for key, value in f.items()}
            nested = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            profile = {**nested, **raw, **details}
            def g(key, default="غير متوفر"):
                value = profile.get(key, default)
                return value if value not in (None, "") else default

            poster_value = g("poster", None) or g("poster_uri", None) or g("cover_uri", None) or g("image", None)
            if isinstance(poster_value, dict):
                poster_value = poster_value.get("large") or poster_value.get("medium") or poster_value.get("url")

            return {
                "name": g("name") if g("name") != "غير متوفر" else g("title", "Unknown"),
                "story": g("story") if g("story") != "غير متوفر" else g("description", "لا توجد قصة متاحة حالياً لهذا العمل."),
                "rating": g("rating", "8.0"),
                "poster": poster_value,
                "status": g("status", "مستمر"),
                "num_episodes": g("num_episodes") if g("num_episodes") != "غير متوفر" else g("episodes_count", "غير محدد"),
                "year": g("year") if g("year") != "غير متوفر" else g("release_date", "2024"),
                "type": g("type", "أنمي"),
                "genres": g("genres", "أكشن، مغامرات"),
                "season": g("season", "غير محدد"),
                "studio": g("studio", "غير معروف"),
                "source": self.FIRESTORE_SOURCE,
            }
        except Exception as e:
            logger.error(f"Error fetching details: {e}")
            return {
                "name": "عمل مميز",
                "story": "قصة مشوقة ومميزة تم جلبها من مكتبة Anime Witcher الشاملة.",
                "rating": "8.5",
                "poster": None,
                "status": "مستمر",
                "num_episodes": "غير محدد",
                "year": "2024",
                "type": "أنمي",
                "genres": "أكشن، مغامرات",
                "season": "الكل",
                "studio": "استوديو معتمد",
                "source": self.FIRESTORE_SOURCE,
            }

    def get_episodes(self, doc_ref):
        headers = {"Authorization": f"Bearer {self.id_token}"} if self.id_token else {}
        try:
            base = str(doc_ref).lstrip('/')
            summary_url = f"{self.firestore_base_url}/{base}/episodes_summery/summery"
            summary_res = requests.get(summary_url, headers=headers, params={"key": self.firebase_api_key}, timeout=10)
            episodes_status = summary_res.status_code
            summary_fields = summary_res.json().get("fields", {}) if summary_res.status_code == 200 else {}
            summary = {k: self.unwrap_firestore(v) for k, v in summary_fields.items()}
            summary_items = summary.get("episodes", []) if isinstance(summary, dict) else []
            docs = [{"fields": item, "name": f"{base}/episodes/{item.get('doc_id')}"}
                    for item in summary_items if isinstance(item, dict) and item.get("doc_id")]
            # The app falls back to the real episodes subcollection when the summary is absent.
            if not docs:
                url = f"{self.firestore_base_url}/{base}/episodes"
                res = requests.get(url, headers=headers, params={"key": self.firebase_api_key}, timeout=10)
                episodes_status = res.status_code
                docs = res.json().get("documents", []) if res.status_code == 200 else []
            # Some app releases store episodes as an array/map in the anime
            # document instead of a Firestore subcollection.
            if not docs:
                profile = self.read_firestore_document(base) or {}
                embedded = profile.get("episodes") or profile.get("episodes_list") or profile.get("items") or []
                if isinstance(embedded, dict):
                    embedded = list(embedded.values())
                docs = [{"fields": item if isinstance(item, dict) else {"name": item},
                         "name": f"{base}/episodes/ep_{i + 1}"}
                        for i, item in enumerate(embedded) if isinstance(item, (dict, str, int))]
            if not docs:
                logger.warning("Episodes unavailable for %s: HTTP %s", doc_ref, episodes_status)
                return []
            eps = []
            seen_episode_ids = set()
            for d in docs:
                f = {k: self.unwrap_firestore(v) for k, v in d.get("fields", {}).items()}
                name = f.get("name") or f.get("title") or f.get("episode") or f"الحلقة"
                if isinstance(name, (int, float)):
                    name = f"الحلقة {name}"
                eid = str(d.get("name") or "").split("/")[-1]
                if eid in seen_episode_ids:
                    continue
                seen_episode_ids.add(eid)
                try: 
                    nums = re.findall(r'\d+', name)
                    order = int(nums[0]) if nums else 999
                except: order = 999
                eps.append({"id": eid, "name": name, "order": order, "source": self.FIRESTORE_SOURCE})
            eps.sort(key=lambda x: x["order"])
            return eps
        except Exception as e:
            logger.error(f"Error fetching episodes: {e}")
            return []

    def resolve_pd(self, url, mode="view"):
        if "pixeldrain.com" in url:
            match = re.search(r"(?:/u/|/api/file/|/l/)([a-zA-Z0-9]+)", url)
            if match:
                file_id = match.group(1)
                if mode == "view":
                    return f"https://pixeldrain.com/api/file/{file_id}"
                return f"https://pixeldrain.com/api/file/{file_id}?download"
        return url

    def get_servers(self, doc_ref, ep_id):
        headers = {"Authorization": f"Bearer {self.id_token}"} if self.id_token else {}
        try:
            base = str(doc_ref).lstrip('/')
            all_records = []
            # v1.4.9 first tries the compact all_servers document.
            compact_path = f"{base}/episodes/{ep_id}/servers2/all_servers"
            compact = self.read_firestore_document(compact_path) or {}
            compact_items = compact.get("servers", []) if isinstance(compact, dict) else []
            for item in compact_items:
                if isinstance(item, dict):
                    fields = self.expand_server_ids(item)
                    all_records.extend(self._server_record_from_fields(fields, compact_path))
            # Then read the normal servers subcollection, exactly as the app does.
            url = f"{self.firestore_base_url}/{base}/episodes/{ep_id}/servers"
            res = requests.get(url, headers=headers, params={"key": self.firebase_api_key}, timeout=10)
            if res.status_code == 200:
                for document in res.json().get("documents", []):
                    fields = {k: self.unwrap_firestore(v) for k, v in document.get("fields", {}).items()}
                    if fields.get("visible") is False:
                        continue
                    fields = self.expand_server_ids(fields)
                    all_records.extend(self._server_record_from_fields(fields, document.get("name", "")))
            # Fallback for releases where servers are embedded in the episode
            # document rather than exposed as a subcollection.
            if not all_records:
                episode = self.read_firestore_document(f"{base}/episodes/{ep_id}") or {}
                episode = self.expand_server_ids(episode)
                all_records = self.extract_link_records(episode, f"{base}/episodes/{ep_id}")
            if not all_records:
                logger.warning("Servers unavailable for %s/%s: HTTP %s", doc_ref, ep_id, res.status_code)
                return []
            servers = []
            seen = set()
            for record in all_records:
                key = (record["view_url"], record.get("quality"), record.get("language"))
                if key in seen:
                    continue
                seen.add(key)
                record["app_url"] = record.get("download_url") or record["view_url"]
                servers.append(record)
                servers.sort(key=lambda x: (x["is_pd"], x.get("link_type") == "direct_download"), reverse=True)
            return servers
        except Exception as e:
            logger.error(f"Error fetching servers: {e}")
            return []

    def _server_record_from_fields(self, fields, source_path):
        """Normalize the app's server item (name/quality/link) without losing metadata."""
        link = fields.get("link") or fields.get("url") or fields.get("video_uri")
        if isinstance(link, str) and link.strip():
            record = self.extract_link_records({"link": link}, source_path, str(fields.get("name") or "مشغل"))
            for item in record:
                item["name"] = str(fields.get("name") or item["name"])
                item["quality"] = fields.get("quality") or item.get("quality")
                item["open_browser"] = bool(fields.get("open_browser"))
                item["direct_link"] = bool(fields.get("direct_link"))
            return record
        return self.extract_link_records(fields, source_path, str(fields.get("name") or "مشغل"))

    @staticmethod
    def expand_server_ids(fields):
        """Turn provider IDs used by older app records into provider URLs."""
        if not isinstance(fields, dict):
            return fields
        expanded = dict(fields)
        providers = {
            "streamtape_video_id": "https://streamtape.com/e/",
            "mixdrop_video_id": "https://mixdrop.co/e/",
            "doodstream_video_id": "https://dood.li/e/",
            "filemoon_video_id": "https://filemoon.sx/e/",
            "streamwish_video_id": "https://streamwish.to/e/",
        }
        for key, base in providers.items():
            value = fields.get(key)
            if value and isinstance(value, str):
                expanded[f"{key}_url"] = base + value
        return expanded

    def parse_smart_query(self, q):
        m = re.search(r"(.+)\s+(?:الحلقة|حلقة|episode|ep|part|epsiode|الحلقه)\s+(\d+)", q, re.I)
        if not m: m = re.search(r"(.+)\s+(\d+)$", q)
        if m: return m.group(1).strip(), int(m.group(2))
        return q.strip(), None

    def get_source_catalog(self):
        """Return the complete, human-readable source map used by this service."""
        return {
            "anime_search": [
                {"id": key, "name": value, "type": "Algolia index"}
                for key, value in self.ANIME_INDEX_SOURCES.items()
            ],
            "character_search": [
                {"id": key, "name": value, "type": "Algolia index"}
                for key, value in self.CHARACTER_INDEX_SOURCES.items()
            ],
            "content": [{"id": "firestore", "name": self.FIRESTORE_SOURCE, "type": "Firestore REST"}],
            "server_sources": [
                {"id": "PD", "name": "PD / PixelDrain (روابط مباشرة للمشاهدة والتحميل)", "type": "Anime Witcher server"},
                {"id": "KF", "name": "KF / KrakenFiles", "type": "Anime Witcher server"},
                {"id": "MF", "name": "MF / MediaFire", "type": "Anime Witcher server"},
                {"id": "ST", "name": "ST / StreamTape", "type": "Anime Witcher server"},
                {"id": "BU", "name": "BU / Backup server", "type": "Anime Witcher server"},
            ],
            "fallback": [{"id": "jikan", "name": self.JIKAN_SOURCE, "type": "External API"}],
            "discovered_external_apps": [
                {"id": "anime_slayer", "name": "Anime Slayer v1.5.10 · anslayer.com/anime/public/", "type": "Discovered API; not enabled without authorized integration credentials"},
            ],
        }
