# AnimeHub.Bot

AnimeHub.Bot is a REST-first anime metadata and direct-download link service. Applications send a title and optional episode number; the service searches the connected catalog, resolves the exact work and episode, and returns only downloadable direct media URLs with quality, language, provider, and source metadata.

## Direct-link policy

AnimeHub.Bot never returns iframe/player pages. It only returns records classified as `direct_download`, such as PixelDrain API file URLs or actual media/CDN URLs. A provider URL is not promised to be permanent: expiry and availability remain controlled by the provider.

## API

- `GET /v1/search?q=Naruto`
- `GET /v1/links?q=Naruto episode 1`
- `GET /v1/links?q=Naruto&episode=1`
- `GET /v1/links?q=Naruto&episode=1&quality=720p`
- `GET /v1/download?q=Naruto&episode=1&quality=720p` (307 redirect to the direct file)
- `GET /v1/batch-links?q=One%20Piece&start=1&limit=20&quality=720p`
- `POST /v1/library/plan` (preview organized folders and detect duplicates/missing episodes)
- `GET /v1/work?doc_ref=anime_list/Naruto`
- `GET /v1/sources`
- `GET /v1/storage/status`
- `POST /v1/storage/upload-url` (server-side Upload18 upload from an HTTPS direct media URL)
- `POST /v1/storage/download-and-upload` (download to server temp storage, validate, upload, then delete temp file)
- `GET /v1/storage/jobs/{job_id}` (poll background storage progress and exact failure reason)

Bunny Stream is the primary server-side library provider. The configured library is `BUNNY_STREAM_LIBRARY_ID`; each requested work is placed in an automatically created Bunny collection named after the work, and the requested quality is passed to Bunny encoding when supported. Keep `BUNNY_STREAM_API_KEY` server-side only.
- `GET /health`

## Multi-work jobs

Submit several films or series in one request:

```json
POST /v1/jobs
{
  "concurrency": 4,
  "items": [
    {"title": "Your Name", "type": "film", "mode": "download", "quality": "720p"},
    {"title": "Naruto", "type": "series", "start_episode": 1, "end_episode": 12, "mode": "links", "quality": "1080p"}
  ]
}
```

Poll `GET /v1/jobs/{job_id}` for progress. Every episode is marked `completed` or `failed`; failures include the requested episode number and a reason. A job can finish as `completed_with_errors` when only some episodes fail. Use `POST /v1/jobs/{job_id}/pause` and `/resume` to stop and continue without losing completed work, or `/cancel` to stop it permanently. `mode=links` returns direct URLs; `mode=download` returns the same direct URLs for the client application to download to the phone. The server does not proxy or store third-party media.

For production on Railway/Render, use **PostgreSQL** for jobs, work metadata, link history, and deduplication, plus **Redis** for the queue and concurrency control. Do not store video binaries in PostgreSQL. If authorized media must be cached, use an S3-compatible object store and keep only object keys and expiry metadata in PostgreSQL. The current local job manager is intentionally lightweight and in-memory; the next production step is swapping it for Redis/PostgreSQL persistence.

Upload18 is supported through its server API. Configure a restricted `Upload + Read` key on the server only; never put the master key in the APK. The service accepts an HTTPS direct media URL, submits it to Upload18, and records the returned video identifier and processing status. Folder IDs must be created or selected in Upload18 and supplied through `UPLOAD18_FOLDER_ID` or the upload request.

## Providers

The current enabled adapter uses the Anime Witcher catalog and its Firebase/Algolia data. Anime Slayer v1.5.10 was identified as using `https://anslayer.com/anime/public/` plus an internal resolver; it is listed in the source catalog but is not enabled until an authorized API contract or credentials are provided. This avoids returning player pages or attempting to bypass provider protections. A new adapter can be added without changing the public API.

## Deploy

The included `Procfile` targets Render and Railway:

```text
web: uvicorn animehub_api:app --host 0.0.0.0 --port $PORT
```

The `/v1/download` endpoint does not proxy or store media. It redirects the user's application to the selected direct URL, so the phone controls the download. `batch-links` is bounded and parallelized to avoid one very large request.
