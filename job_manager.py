import asyncio
import time
import uuid
from typing import Any


class JobManager:
    """Process bounded link/download plans concurrently.

    `download` means resolve direct download URLs for the client app. The API
    does not proxy or store third-party media on the server.
    """

    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def create(self, plan, resolver, concurrency=4):
        job_id = uuid.uuid4().hex
        items = []
        for index, item in enumerate(plan):
            items.append({"id": index + 1, "title": item.get("title", ""), "type": item.get("type", "auto"),
                          "episodes": item.get("episodes"), "season": item.get("season"), "status": "queued",
                          "result": None, "error": None})
        self.jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                             "total": len(items), "completed": 0, "failed": 0, "items": items}
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, resolver, max(1, min(concurrency, 8))))
        return self.jobs[job_id]

    async def _run(self, job_id, resolver, concurrency):
        job = self.jobs[job_id]
        job["status"] = "running"
        semaphore = asyncio.Semaphore(concurrency)

        async def process(item):
            async with semaphore:
                item["status"] = "running"
                try:
                    item["result"] = await resolver(item)
                    item["status"] = "completed"
                    job["completed"] += 1
                except asyncio.CancelledError:
                    item["status"] = "cancelled"
                    raise
                except Exception as exc:
                    item["status"] = "failed"
                    item["error"] = str(exc)
                    job["failed"] += 1

        try:
            await asyncio.gather(*(process(item) for item in job["items"]))
            job["status"] = "completed" if not job["failed"] else "completed_with_errors"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        finally:
            job["finished_at"] = time.time()

    def get(self, job_id):
        return self.jobs.get(job_id)

    async def cancel(self, job_id):
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            return True
        return False
