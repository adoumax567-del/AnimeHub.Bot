import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from data_manager import DataManager
from anime_slayer_provider import AnimeSlayerProvider

SERVICE_NAME = "AnimeHub.Bot"
DATA = DataManager()
SLAYER = AnimeSlayerProvider()


def direct_downloads(records):
    return [r for r in records if r.get("link_type") == "direct_download" and r.get("download_url")]


def select_links(records, quality=None, language=None):
    links = direct_downloads(records)
    if quality:
        q = quality.lower().replace(" ", "")
        links = [r for r in links if str(r.get("quality", "")).lower().replace(" ", "") == q]
    if language:
        needle = language.lower()
        links = [r for r in links if needle in str(r.get("language", "")).lower()]
    return links


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


def resolve_work_episode(title, episode_number):
    results = DATA.search_anime(title)
    if not results:
        raise LookupError("Work not found")
    target = results[0]
    episodes = DATA.get_episodes(target["doc_ref"])
    selected = next((e for e in episodes if e.get("order") == episode_number), None)
    # Firestore marks a standalone movie as order 999; expose it to clients as
    # the single requested item (episode 1) without changing the source data.
    if selected is None and episode_number == 1 and len(episodes) == 1:
        selected = dict(episodes[0])
        selected["order"] = 1
    if not selected:
        raise LookupError("Episode not found")
    # Both providers are attempted in the same request. The second provider
    # returns [] until an authorized JSON endpoint is configured.
    with ThreadPoolExecutor(max_workers=2) as executor:
        witcher_future = executor.submit(DATA.get_servers, target["doc_ref"], selected["id"])
        slayer_future = executor.submit(SLAYER.fetch, target.get("name"), selected.get("order", episode_number))
        witcher = witcher_future.result()
        slayer = slayer_future.result()
    records = select_links(witcher + slayer)
    unique = {}
    for record in records:
        unique[(record.get("download_url"), record.get("quality"), record.get("name"))] = record
    return target, selected, list(unique.values()), len(episodes)


def resolve_target_episode(target, selected):
    with ThreadPoolExecutor(max_workers=2) as executor:
        witcher_future = executor.submit(DATA.get_servers, target["doc_ref"], selected["id"])
        slayer_future = executor.submit(SLAYER.fetch, target.get("name"), selected.get("order", 1))
        records = select_links(witcher_future.result() + slayer_future.result())
    unique = {(r.get("download_url"), r.get("quality"), r.get("name")): r for r in records}
    return list(unique.values())


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(lambda: DATA.algolia_app_id)
    yield


app = FastAPI(title=SERVICE_NAME, version="1.1.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "status": "online", "api_version": "v1", "direct_only": True,
            "downloadable_only": True, "docs": "/docs",
            "endpoints": ["/v1/search", "/v1/links", "/v1/download", "/v1/batch-links", "/v1/sources"]}


@app.get("/health")
async def health():
    return {"service": SERVICE_NAME, "status": "healthy", "direct_only": True}


@app.get("/v1/sources")
async def sources():
    return {"service": SERVICE_NAME, "direct_only": True,
            "enabled": {"anime_witcher": True, "anime_slayer": SLAYER.enabled and bool(SLAYER.endpoint)},
            "sources": DATA.get_source_catalog()}


@app.get("/v1/search")
async def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(20, ge=1, le=50)):
    results = await asyncio.to_thread(DATA.search_anime, q)
    return {"service": SERVICE_NAME, "query": q, "count": min(len(results), limit), "results": results[:limit]}


@app.get("/v1/work")
async def work(doc_ref: str = Query(..., description="Document reference returned by /v1/search")):
    episodes = await asyncio.to_thread(DATA.get_episodes, doc_ref)
    return {"service": SERVICE_NAME, "doc_ref": doc_ref, "episodes": episodes, "count": len(episodes)}


@app.get("/v1/links")
async def links(q: str = Query(..., min_length=1, max_length=200), episode: Optional[int] = Query(None, ge=1),
                quality: Optional[str] = Query(None), language: Optional[str] = Query(None)):
    name, parsed_episode = DATA.parse_smart_query(q)
    episode_number = episode or parsed_episode or 1
    try:
        target, selected, records, episode_count = await asyncio.to_thread(resolve_work_episode, name, episode_number)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    records = select_links(records, quality, language)
    return {"service": SERVICE_NAME, "query": q,
            "work": {"name": target.get("name"), "doc_ref": target.get("doc_ref"), "sources": target.get("sources", [])},
            "episode": {"number": selected.get("order"), "id": selected.get("id"), "name": selected.get("name")},
            "episode_count": episode_count, "direct_only": True, "downloadable_only": True,
            "filters": {"quality": quality, "language": language}, "count": len(records),
            "links": [public_link(r) for r in records]}


@app.get("/v1/download")
async def download(q: str = Query(..., min_length=1, max_length=200), episode: Optional[int] = Query(None, ge=1),
                   quality: Optional[str] = Query(None), language: Optional[str] = Query(None)):
    name, parsed_episode = DATA.parse_smart_query(q)
    episode_number = episode or parsed_episode or 1
    try:
        _, _, records, _ = await asyncio.to_thread(resolve_work_episode, name, episode_number)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    records = select_links(records, quality, language)
    if not records:
        raise HTTPException(404, "No direct downloadable link matches the requested filters")
    return RedirectResponse(records[0]["download_url"], status_code=307)


@app.get("/v1/batch-links")
async def batch_links(q: str = Query(..., min_length=1, max_length=200), start: int = Query(1, ge=1),
                      limit: int = Query(20, ge=1, le=100), quality: Optional[str] = Query(None),
                      language: Optional[str] = Query(None)):
    results = await asyncio.to_thread(DATA.search_anime, q)
    if not results:
        raise HTTPException(404, "Work not found")
    target = results[0]
    episodes = await asyncio.to_thread(DATA.get_episodes, target["doc_ref"])
    selected = [e for e in episodes if start <= int(e.get("order", 0)) < start + limit]
    if not selected and start == 1 and len(episodes) == 1:
        selected = [dict(episodes[0], order=1)]

    async def one(ep):
        try:
            records = await asyncio.to_thread(resolve_target_episode, target, ep)
            return {"episode": ep, "links": [public_link(r) for r in select_links(records, quality, language)]}
        except LookupError:
            return {"episode": ep, "links": []}

    pages = await asyncio.gather(*(one(ep) for ep in selected))
    return {"service": SERVICE_NAME, "work": target.get("name"), "direct_only": True,
            "downloadable_only": True, "start": start, "limit": limit, "total_episodes": len(episodes),
            "filters": {"quality": quality, "language": language}, "episodes": pages}
