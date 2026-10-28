import asyncio
import time
import uuid
from typing import Any


class JobManager:
    """Concurrent job manager with per-item state and pause/resume control."""

    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.pause_events: dict[str, asyncio.Event] = {}

    async def create(self, plan, resolver, concurrency=4):
        job_id = uuid.uuid4().hex
        items = []
        for index, item in enumerate(plan):
            items.append({"id": index + 1, "title": item.get("title", ""), "type": item.get("type", "auto"),
                          "season": item.get("season"), "episodes": item.get("episodes"),
                          "episode_count": item.get("episode_count") or item.get("episodes_count") or item.get("count"),
                          "start_episode": item.get("start_episode"), "end_episode": item.get("end_episode"),
                          "episode_concurrency": item.get("episode_concurrency", 6),
                          "all_episodes": bool(item.get("all_episodes", False)),
                          "library_id": item.get("library_id"),
                          "quality": item.get("quality"), "language": item.get("language"),
                          "mode": item.get("mode", "download"), "status": "queued", "result": None, "error": None})
        self.jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                             "total": len(items), "completed": 0, "failed": 0, "items": items,
                             "pause_requested": False}
        event = asyncio.Event()
        event.set()
        self.pause_events[job_id] = event
        # Create one lightweight async task per submitted work. Expensive
        # Bunny submissions remain bounded by the global transfer semaphore in
        # the API, while the complete list can resolve and queue immediately.
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, resolver, max(1, int(concurrency))))
        return self.jobs[job_id]

    async def _run(self, job_id, resolver, concurrency):
        job = self.jobs[job_id]
        job["status"] = "running"
        semaphore = asyncio.Semaphore(concurrency)
        event = self.pause_events[job_id]

        async def process(item):
            async with semaphore:
                await event.wait()
                if job["status"] == "cancelled":
                    item["status"] = "cancelled"
                    return
                item["status"] = "resolving"
                try:
                    result = await resolver(item)
                    item["result"] = result
                    if result.get("failed_count", 0):
                        item["status"] = "completed_with_errors"
                        job["failed"] += result["failed_count"]
                    else:
                        item["status"] = "completed"
                    job["completed"] += 1
                except asyncio.CancelledError:
                    item["status"] = "cancelled"
                    raise
                except Exception as exc:
                    item["status"] = "failed"
                    item["error"] = {"code": "item_failed", "message": str(exc)}
                    job["failed"] += 1

        try:
            await asyncio.gather(*(process(item) for item in job["items"]))
            if job["status"] != "cancelled":
                job["status"] = "completed_with_errors" if job["failed"] else "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        finally:
            job["finished_at"] = time.time()

    def get(self, job_id):
        return self.jobs.get(job_id)

    async def remove(self, job_id):
        """Stop a job and remove its complete record and item details."""
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        existed = job_id in self.jobs
        self.jobs.pop(job_id, None)
        self.pause_events.pop(job_id, None)
        self.tasks.pop(job_id, None)
        return existed

    async def remove_active(self):
        """Remove queued, running, or paused jobs before replacing them."""
        removed = []
        for job_id, job in list(self.jobs.items()):
            if job.get("status") in ("queued", "running", "paused"):
                if await self.remove(job_id):
                    removed.append(job_id)
        return removed

    async def pause(self, job_id):
        job = self.jobs.get(job_id)
        event = self.pause_events.get(job_id)
        if not job or not event or job["status"] not in ("running", "queued"):
            return False
        job["pause_requested"] = True
        job["status"] = "paused"
        event.clear()
        return True

    async def resume(self, job_id):
        job = self.jobs.get(job_id)
        event = self.pause_events.get(job_id)
        if not job or not event or job["status"] != "paused":
            return False
        job["pause_requested"] = False
        job["status"] = "running"
        event.set()
        return True

    async def cancel(self, job_id):
        task = self.tasks.get(job_id)
        job = self.jobs.get(job_id)
        if job:
            job["status"] = "cancelled"
        if task and not task.done():
            task.cancel()
            return True
        return False
