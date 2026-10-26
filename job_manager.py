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
                          "start_episode": item.get("start_episode"), "end_episode": item.get("end_episode"),
                          "episode_concurrency": item.get("episode_concurrency", 6),
                          "quality": item.get("quality"), "language": item.get("language"),
                          "mode": item.get("mode", "links"), "status": "queued", "result": None, "error": None})
        self.jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                             "total": len(items), "completed": 0, "failed": 0, "items": items,
                             "pause_requested": False}
        event = asyncio.Event()
        event.set()
        self.pause_events[job_id] = event
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, resolver, max(1, min(concurrency, 8))))
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
                item["status"] = "running"
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
