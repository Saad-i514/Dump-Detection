"""Single-server video jobs; results expire after one hour or a server restart."""

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import shutil
from threading import Lock
from time import monotonic
from uuid import uuid4

from fastapi import HTTPException

log = logging.getLogger(__name__)


class VideoJobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.lock = Lock()
        self.records = {}

    def submit(self, run, directory: str, inference_lock: Lock) -> str:
        job_id = uuid4().hex
        with self.lock:
            now = monotonic()
            self.records = {key: value for key, value in self.records.items()
                            if now - value["updated"] < 3600}
            # Bound retained results even when many tiny clips are submitted.
            if len(self.records) >= 100:
                oldest = min(self.records, key=lambda key: self.records[key]["updated"])
                del self.records[oldest]
            self.records[job_id] = {"status": "processing", "updated": now}
        try:
            self.executor.submit(self._run, job_id, run, directory, inference_lock)
        except Exception:
            with self.lock:
                del self.records[job_id]
            raise
        return job_id

    def _run(self, job_id, run, directory, inference_lock):
        try:
            result = {"status": "completed", "result": run().model_dump(mode="json")}
        except HTTPException as exc:
            result = {"status": "failed", "error": exc.detail, "error_status": exc.status_code}
        except Exception:
            log.exception("Video job failed")
            result = {"status": "failed", "error": "Video processing failed", "error_status": 500}
        finally:
            shutil.rmtree(Path(directory), ignore_errors=True)
            inference_lock.release()
        with self.lock:
            self.records[job_id] = {**result, "updated": monotonic()}

    def get(self, job_id):
        with self.lock:
            record = self.records.get(job_id)
            if record is None or monotonic() - record["updated"] >= 3600:
                self.records.pop(job_id, None)
                raise HTTPException(status_code=404, detail="Job not found or expired")
            return {"job_id": job_id, **{k: v for k, v in record.items() if k != "updated"}}

    def close(self):
        self.executor.shutdown(wait=True)
