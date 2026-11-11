import json
import os
import shutil
import tempfile
import threading
import uuid
from .shader_state_codec import StateEncoder, StateDecoder


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
                "counts": {"states": 0, "instructions": 0, "sources": 0, "source_variables": 0, "source_steps": 0, "mapping_definitions": 0},
                "offsets": {"states": [], "instructions": [], "sources": [], "source_variables": [], "source_steps": [], "mapping_definitions": []},
                "bytes": 0, "cancel": False, "release": False,
                "state_encoder": StateEncoder(),
                "state_decoder": None, "decoded_offset": 0, "pending_state": None,
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
        with self._lock:
            job = self._job(trace_id)
            encoder = job["state_encoder"] if section == "states" else None
            checkpoint = encoder.checkpoint(value) if encoder is not None else None
            try:
                if encoder is not None:
                    value = encoder.encode(value)
                record = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
                if self._bytes + len(record) > self._max_bytes:
                    raise RuntimeError("Shader trace storage limit reached")
                path = self._path(trace_id, section)
                with open(path, "ab") as stream:
                    offset = stream.tell()
                    stream.write(record)
            except Exception:
                if encoder is not None:
                    encoder.rollback(checkpoint)
                raise
            job["offsets"][section].append((offset, len(record)))
            job["counts"][section] += 1
            job["bytes"] += len(record)
            self._bytes += len(record)

    def get(self, trace_id, section, offset, limit):
        if section not in ("status", "metadata", "states", "state_deltas", "instructions", "sources", "source_variables", "source_steps", "mapping_definitions"):
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
            stored_section = "states" if section == "state_deltas" else section
            offsets = job["offsets"][stored_section]
            if offset > len(offsets):
                raise ValueError("offset exceeds the number of available records")
            result["section"] = section
            result["offset"] = offset
            result["items"] = []
            total_bytes = 0
            if offset < len(offsets):
                with open(self._path(trace_id, stored_section), "rb") as stream:
                    decoder = None
                    if section == "states":
                        pending = job["pending_state"]
                        if (job["state_decoder"] is None or (job["decoded_offset"] > offset
                                and (pending is None or pending[0] != offset))):
                            job["state_decoder"] = StateDecoder()
                            job["decoded_offset"] = 0
                            job["pending_state"] = None
                        decoder = job["state_decoder"]
                    if decoder is not None:
                        for position, length in offsets[job["decoded_offset"]:offset]:
                            stream.seek(position)
                            decoder.decode(json.loads(stream.read(length).decode("utf-8")))
                            job["decoded_offset"] += 1
                    for item_offset in range(offset, min(offset + limit, len(offsets))):
                        position, length = offsets[item_offset]
                        stream.seek(position)
                        item = json.loads(stream.read(length).decode("utf-8"))
                        if decoder is not None:
                            pending = job["pending_state"]
                            if pending is not None and pending[0] == item_offset:
                                item = pending[1]
                                job["pending_state"] = None
                            else:
                                item = decoder.decode(item)
                                job["decoded_offset"] += 1
                        size = len(json.dumps(item, ensure_ascii=True, allow_nan=False).encode("utf-8"))
                        if result["items"] and total_bytes + size > 2097152:
                            if decoder is not None:
                                job["pending_state"] = (item_offset, item)
                            break
                        result["items"].append(item)
                        total_bytes += size
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
            job["state_encoder"] = None
            if job["release"]:
                self._remove(trace_id)

    def _remove(self, trace_id):
        for section in ("states", "instructions", "sources", "source_variables", "source_steps", "mapping_definitions"):
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
