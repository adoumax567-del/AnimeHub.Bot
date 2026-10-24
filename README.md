# AnimeHub.Bot

AnimeHub.Bot is a REST-first anime metadata and direct-download link service. Applications send a title and optional episode number; the service searches the connected catalog, resolves the exact work and episode, and returns only downloadable direct media URLs with quality, language, provider, and source metadata.

## Direct-link policy

AnimeHub.Bot never returns iframe/player pages. It only returns records classified as `direct_download`, such as PixelDrain API file URLs or actual media/CDN URLs. A provider URL is not promised to be permanent: expiry and availability remain controlled by the provider.

## API

- `GET /v1/search?q=Naruto`
- `GET /v1/links?q=Naruto episode 1`
- `GET /v1/links?q=Naruto&episode=1`
- `GET /v1/work?doc_ref=anime_list/Naruto`
- `GET /v1/sources`
- `GET /health`

## Providers

The current enabled adapter uses the Anime Witcher catalog and its Firebase/Algolia data. Anime Slayer v1.5.10 was identified as using `https://anslayer.com/anime/public/` plus an internal resolver; it is listed in the source catalog but is not enabled until an authorized API contract or credentials are provided. This avoids returning player pages or attempting to bypass provider protections. A new adapter can be added without changing the public API.

## Deploy

The included `Procfile` targets Render and Railway:

```text
web: uvicorn animehub_api:app --host 0.0.0.0 --port $PORT
```
