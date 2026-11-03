import json
import os
import shutil
import tempfile
import threading
import uuid


class ShaderTraceStore:
    def __init__(self, max_bytes=268435456):
        self._directory = tempfile.mkdtemp(prefix="renderdoc_mcp_traces_")
        self._jobs = {}
        self._lock = threading.RLock()
        self._max_bytes = max_bytes
        self._bytes = 0

    def create(self, stage, event_id, capture, invocation):
        with self._lock:
            if any(job["status"] in ("queued", "running", "cancelling")
                   for job in self._jobs.values()):
                raise ValueError("Another shader debug job is active")
            trace_id = uuid.uuid4().hex
            self._jobs[trace_id] = {
                "trace_id": trace_id, "stage": stage, "event_id": event_id,
                "capture": capture, "invocation": invocation, "status": "queued",
                "complete": False, "error": None, "metadata": None,
                "counts": {"states": 0, "instructions": 0, "sources": 0},
                "offsets": {"states": [], "instructions": [], "sources": []},
                "bytes": 0, "cancel": False, "release": False,
            }
            return self._public(self._jobs[trace_id])

    def _job(self, trace_id):
        if trace_id not in self._jobs:
            raise ValueError("Unknown or released shader trace: %s" % trace_id)
        return self._jobs[trace_id]

    def _path(self, trace_id, section):
        return os.path.join(self._directory, trace_id + "_" + section + ".jsonl")

    @staticmethod
    def _public(job):
        result = {key: job[key] for key in ("trace_id", "stage", "event_id", "capture",
                                             "invocation", "status", "complete", "error")}
        result["counts"] = dict(job["counts"])
        return result

    def update(self, trace_id, **values):
        with self._lock:
            job = self._job(trace_id)
            if "metadata" in values:
                size = len(json.dumps(values["metadata"], ensure_ascii=True, allow_nan=False).encode("utf-8"))
                if self._bytes + size > self._max_bytes:
                    raise RuntimeError("Shader trace storage limit reached")
                self._bytes += size
                job["bytes"] += size
            job.update(values)

    def cancelled(self, trace_id):
        with self._lock:
            return self._job(trace_id)["cancel"]

    def begin(self, trace_id):
        with self._lock:
            job = self._jobs.get(trace_id)
            if job is not None and job["status"] == "cancelling":
                self.finish(trace_id, "cancelled")
                return False
            if job is None or job["status"] != "queued":
                return False
            job["status"] = "running"
            return True

    def append(self, trace_id, section, value):
        record = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        with self._lock:
            job = self._job(trace_id)
            if self._bytes + len(record) > self._max_bytes:
                raise RuntimeError("Shader trace storage limit reached")
            path = self._path(trace_id, section)
            with open(path, "ab") as stream:
                offset = stream.tell()
                stream.write(record)
            job["offsets"][section].append((offset, len(record)))
            job["counts"][section] += 1
            job["bytes"] += len(record)
            self._bytes += len(record)

    def get(self, trace_id, section, offset, limit):
        if section not in ("status", "metadata", "states", "instructions", "sources"):
            raise ValueError("Unknown trace section: %s" % section)
        if offset < 0 or limit < 1 or limit > 1000:
            raise ValueError("offset must be non-negative and limit must be between 1 and 1000")
        with self._lock:
            job = self._job(trace_id)
            result = self._public(job)
            if section == "status":
                return result
            if section == "metadata":
                result["metadata"] = job["metadata"]
                return result
            offsets = job["offsets"][section]
            if offset > len(offsets):
                raise ValueError("offset exceeds the number of available records")
            result["section"] = section
            result["offset"] = offset
            result["items"] = []
            total_bytes = 0
            if offset < len(offsets):
                with open(self._path(trace_id, section), "rb") as stream:
                    for position, length in offsets[offset:offset + limit]:
                        if result["items"] and total_bytes + length > 2097152:
                            break
                        stream.seek(position)
                        result["items"].append(json.loads(stream.read(length).decode("utf-8")))
                        total_bytes += length
            next_offset = offset + len(result["items"])
            result["next_offset"] = (next_offset if next_offset < len(offsets)
                                     or job["status"] in ("queued", "running", "cancelling") else None)
            return result

    def release(self, trace_id):
        with self._lock:
            job = self._job(trace_id)
            if job["status"] in ("queued", "running", "cancelling"):
                job.update(cancel=True, release=True, status="cancelling")
                return {"trace_id": trace_id, "status": "cancelling"}
            self._remove(trace_id)
            return {"trace_id": trace_id, "status": "released"}

    def cancel_active(self):
        with self._lock:
            for job in self._jobs.values():
                if job["status"] in ("queued", "running", "cancelling"):
                    job.update(cancel=True, status="cancelling")

    def finish_cancelled(self):
        with self._lock:
            for trace_id, job in list(self._jobs.items()):
                if job["status"] in ("queued", "running", "cancelling"):
                    self.finish(trace_id, "cancelled", "Capture closed before replay completed")

    def finish(self, trace_id, status, error=None):
        with self._lock:
            job = self._job(trace_id)
            job.update(status=status, complete=(status == "complete"), error=error)
            if job["release"]:
                self._remove(trace_id)

    def _remove(self, trace_id):
        for section in ("states", "instructions", "sources"):
            path = self._path(trace_id, section)
            if os.path.exists(path):
                os.remove(path)
        self._bytes -= self._jobs[trace_id]["bytes"]
        del self._jobs[trace_id]

    def close(self):
        with self._lock:
            shutil.rmtree(self._directory)
            self._jobs.clear()
            self._bytes = 0
