import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Query
from data_manager import DataManager

SERVICE_NAME = "AnimeHub.Bot"
DATA = DataManager()


def direct_downloads(records):
    """Never expose player pages: only direct downloadable media records leave the API."""
    return [r for r in records if r.get("link_type") == "direct_download" and r.get("download_url")]


def public_link(record):
    return {
        "provider": record.get("name"),
        "source": record.get("source"),
        "quality": record.get("quality"),
        "language": record.get("language"),
        "url": record.get("download_url"),
        "watch_url": record.get("view_url"),
        "download_url": record.get("download_url"),
        "type": "direct_download",
        "verified": bool(record.get("verified")),
        "is_pixeldrain": bool(record.get("is_pd")),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the provider credentials once so the first application request is fast.
    await asyncio.to_thread(lambda: DATA.algolia_app_id)
    yield


app = FastAPI(title=SERVICE_NAME, version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "service": SERVICE_NAME,
        "status": "online",
        "api_version": "v1",
        "direct_only": True,
        "downloadable_only": True,
        "docs": "/docs",
        "endpoints": ["/v1/search", "/v1/links", "/v1/work", "/v1/sources"],
    }


@app.get("/health")
async def health():
    return {"service": SERVICE_NAME, "status": "healthy", "direct_only": True}


@app.get("/v1/sources")
async def sources():
    catalog = DATA.get_source_catalog()
    return {
        "service": SERVICE_NAME,
        "direct_only": True,
        "enabled": {
            "anime_witcher": True,
            "anime_slayer": os.getenv("ANIMESLAYER_ENABLED", "false").lower() == "true",
        },
        "sources": catalog,
    }


@app.get("/v1/search")
async def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(20, ge=1, le=50)):
    results = await asyncio.to_thread(DATA.search_anime, q)
    return {"service": SERVICE_NAME, "query": q, "count": min(len(results), limit), "results": results[:limit]}


@app.get("/v1/work")
async def work(doc_ref: str = Query(..., description="Document reference returned by /v1/search")):
    episodes = await asyncio.to_thread(DATA.get_episodes, doc_ref)
    return {"service": SERVICE_NAME, "doc_ref": doc_ref, "episodes": episodes, "count": len(episodes)}


@app.get("/v1/links")
async def links(
    q: str = Query(..., min_length=1, max_length=200, description="Work title plus optional episode number"),
    episode: Optional[int] = Query(None, ge=1),
):
    name, parsed_episode = DATA.parse_smart_query(q)
    episode_number = episode or parsed_episode or 1
    results = await asyncio.to_thread(DATA.search_anime, name)
    if not results:
        raise HTTPException(404, "Work not found")
    target = results[0]
    episodes = await asyncio.to_thread(DATA.get_episodes, target["doc_ref"])
    selected = next((e for e in episodes if e.get("order") == episode_number), None)
    if not selected:
        raise HTTPException(404, "Episode not found")
    records = await asyncio.to_thread(DATA.get_servers, target["doc_ref"], selected["id"])
    links_only = direct_downloads(records)
    return {
        "service": SERVICE_NAME,
        "query": q,
        "work": {"name": target.get("name"), "doc_ref": target.get("doc_ref"), "sources": target.get("sources", [])},
        "episode": {"number": selected.get("order"), "id": selected.get("id"), "name": selected.get("name")},
        "direct_only": True,
        "downloadable_only": True,
        "count": len(links_only),
        "links": [public_link(r) for r in links_only],
    }
