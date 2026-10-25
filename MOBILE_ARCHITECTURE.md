# AnimeHub.Bot Android + Managed Media Library

## User flow

1. The user installs the Android APK.
2. They paste a list, type items manually, or import a TXT/CSV/JSON file.
3. Each item is normalized and previewed before execution:
   - exact title selected from search results;
   - type: film or series;
   - seasons: one, several, or all available seasons;
   - episode range: all available or a selected range;
   - quality: automatic, one quality, or multiple qualities;
   - language/subtitle preference;
   - operation: collect direct links or download to the managed library.
4. The user confirms the plan. The APK sends a job manifest to `POST /v1/jobs`.
5. A background worker resolves each title and episode, attempts the enabled providers in parallel, keeps only direct downloadable media, and updates progress.
6. The worker downloads authorized media to temporary storage, validates it, moves it to permanent object storage, and writes the library record.
7. The app displays progress, failures, retry actions, and the organized library.

Each episode is a separate result with `completed` or `failed` status. A failed episode includes its requested number, error code, human-readable reason, and an empty link list. The job is marked `completed_with_errors` when the rest of the plan finishes.

## Library layout

```text
AnimeHub Library/
  Your Name/
    Movie/
      Your Name - 1080p - English.mp4
  Naruto/
    Season 01/
      Naruto S01E01 - 720p.mp4
    Season 02/
      Naruto S02E01 - 720p.mp4
```

The database stores logical paths and metadata; the video bytes are not stored in PostgreSQL.

## Recommended production architecture

- **Android:** Expo/React Native APK with a local SQLite cache for job state and library browsing.
- **API:** FastAPI on Render or Railway.
- **Worker:** separate Railway/Render worker process consuming Redis jobs.
- **Database:** managed PostgreSQL for users, plans, jobs, work/episode identity, media records, quality, language, checksums, and object keys.
- **Queue:** Redis for concurrency, retry, cancellation, and progress events.
- **Media storage:** S3-compatible storage or Cloudflare R2. Render/Railway local disks are not a durable video library.
- **Validation:** `ffprobe` plus byte-size/checksum checks before a media object is marked ready.

## API additions required for the APK

- `POST /v1/plan/preview`: normalize a pasted/imported list and return exact matches before any download.
- `POST /v1/jobs`: create a multi-work job with seasons, episodes, quality, and operation.
- `GET /v1/jobs/{id}`: progress by work, season, episode, provider, and download state.
- `POST /v1/jobs/{id}/cancel`: cancel pending work.
- `POST /v1/jobs/{id}/pause`: pause pending work without losing completed episodes.
- `POST /v1/jobs/{id}/resume`: resume queued work after connectivity returns.
- `POST /v1/jobs/{id}/retry`: retry only failed items.
- `GET /v1/library`: browse organized folders.
- `GET /v1/library/{id}/stream`: authorized playback URL.
- `GET /v1/library/{id}/download`: authorized download URL.

## Safety and availability rules

- The APK must show a confirmation preview for ambiguous titles; it must not silently download an approximate match.
- A film is treated as one item. A series is expanded only after the selected seasons and available episodes are confirmed.
- The server never labels a player page as a direct file.
- Media is downloaded and stored only when the operator has the right to use and store it. Provider links can expire; the library copy remains subject to the source's terms.
- A Render/Railway restart must not lose a job or a completed media object; that is why PostgreSQL, Redis, and object storage are required.
- The phone may close or lose internet after submitting a job; the server worker can continue independently. If the source connection fails, the worker records the episode error and the production queue retries it. The APK can reconnect later, read the saved progress, pause, resume, or retry failures.

## Recommended default

Use PostgreSQL + Redis + Cloudflare R2, with Render/Railway hosting the API and worker. This is more reliable and less expensive for a growing video library than storing files on the application container or inside PostgreSQL.
