import struct
import traceback

import renderdoc as rd

from .shader_trace_store import ShaderTraceStore
from .shader_executed_mapping import ExecutedMapping
from .shader_source_steps import SourceSteps
from ..utils.shader_debug_serializer import (
    serialize_debug_state,
    serialize_shader_variables,
    serialize_source_variable_mappings,
)


class ShaderDebugService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = ShaderTraceStore()
        self._closed = False
        self._job = None
        self._timer = None

    def start_vertex(self, event_id, vertex, instance, view):
        self._check_numbers(event_id=event_id, vertex=vertex, instance=instance, view=view)
        invocation = {"vertex": vertex, "instance": instance, "view": view}
        return self._start("vertex", event_id, invocation)

    def start_pixel(self, event_id, x, y, primitive, sample, view):
        self._check_numbers(event_id=event_id, x=x, y=y)
        for name, value in (("primitive", primitive), ("sample", sample), ("view", view)):
            if value is not None:
                self._check_numbers(**{name: value})
        invocation = {"x": x, "y": y, "primitive": primitive, "sample": sample, "view": view}
        return self._start("pixel", event_id, invocation)

    @staticmethod
    def _check_numbers(**values):
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0xFFFFFFFE:
                raise ValueError("%s must be an integer between 0 and 4294967294" % name)

    def _start(self, stage, event_id, invocation):
        if self._closed or not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        action = self.ctx.GetAction(event_id)
        if action is None or action.eventId != event_id or not (action.flags & rd.ActionFlags.Drawcall):
            raise ValueError("Event %d is not a draw call" % event_id)
        if stage == "vertex":
            instances = action.numInstances if action.flags & rd.ActionFlags.Instanced else 1
            if invocation["vertex"] >= action.numIndices or invocation["instance"] >= instances:
                raise ValueError("Vertex or instance is outside the draw")
        action_data = {"event_id": action.eventId, "flags": action.flags,
                       "index_offset": action.indexOffset, "base_vertex": action.baseVertex}
        capture = self.ctx.GetCaptureFilename()
        job = self.store.create(stage, event_id, capture, invocation)
        trace_id = job["trace_id"]
        self._job = {"trace_id": trace_id, "stage": stage, "action": action_data,
                 "invocation": invocation, "trace": None, "has_states": False, "mapping": None,
                     "in_flight": False, "finalized": False}
        try:
            from PySide2.QtCore import QTimer
            if self._timer is None:
                self._timer = QTimer()
                self._timer.timeout.connect(self.pump)
                self._timer.start(100)
        except Exception as error:
            self.store.finish(trace_id, "failed", str(error))
            self.store.release(trace_id)
            self._job = None
            raise
        return job

    def pump(self):
        job = self._job
        if self._closed or job is None or job["finalized"] or job["in_flight"]:
            return
        if not self.ctx.IsCaptureLoaded():
            self.store.finish(job["trace_id"], "failed", "Capture is no longer loaded")
            self._closed = True
            if job["trace"] is None:
                job["finalized"] = True
            return
        job["in_flight"] = True
        original_event = self.ctx.CurEvent()
        try:
            self.ctx.Replay().AsyncInvoke(
                lambda controller: self._slice(controller, job, original_event)
            )
        except Exception:
            job["in_flight"] = False
            self._closed = True
            self.capture_closing()
            raise

    def _vertex_index(self, controller, pipe, action, vertex):
        if not (action["flags"] & rd.ActionFlags.Indexed):
            return vertex
        ib = pipe.GetIBuffer()
        stride = ib.byteStride
        if stride not in (1, 2, 4):
            raise ValueError("Unsupported index width: %s" % stride)
        offset = ib.byteOffset + (action["index_offset"] + vertex) * stride
        raw = controller.GetBufferData(ib.resourceId, offset, stride)
        if len(raw) != stride:
            raise ValueError("Could not read the requested index")
        index = struct.unpack({1: "<B", 2: "<H", 4: "<I"}[stride], bytes(raw))[0]
        if pipe.IsRestartEnabled() and index == (pipe.GetRestartIndex() & ((1 << (8 * stride)) - 1)):
            raise ValueError("The requested vertex is a primitive-restart index")
        index += action["base_vertex"]
        if index < 0 or index > 0xFFFFFFFF:
            raise ValueError("Resolved vertex index is outside the supported range")
        return index

    def _finalize(self, controller, job, status, error=None):
        if job["finalized"]:
            return
        job["finalized"] = True
        trace, job["trace"] = job["trace"], None
        if job.get("source_steps") is not None:
            try:
                job["source_steps"].flush()
            except Exception as exc:
                status, error = "failed", "Failed to store source steps: %s" % exc
            job["source_steps"] = None
        job["mapping"] = None
        if trace is not None:
            try:
                controller.FreeTrace(trace)
            except Exception as exc:
                self._closed = True
                status, error = "failed", "Failed to release shader trace: %s" % exc
        self.store.finish(job["trace_id"], status, error)

    def _slice(self, controller, job, original_event):
        if job["finalized"]:
            job["in_flight"] = False
            return
        trace_id = job["trace_id"]
        status, failure = None, None
        try:
            if job["finalized"]:
                return
            if self.store.cancelled(trace_id):
                if job["trace"] is None and not self.store.begin(trace_id):
                    job["finalized"] = True
                    return
                status = "cancelled"
                return
            if job["trace"] is None:
                if not self.store.begin(trace_id):
                    job["finalized"] = True
                    return
                controller.SetFrameEvent(job["action"]["event_id"], True)
                pipe = controller.GetPipelineState()
                stage = rd.ShaderStage.Vertex if job["stage"] == "vertex" else rd.ShaderStage.Pixel
                reflection = pipe.GetShaderReflection(stage)
                if reflection is None:
                    raise ValueError("No shader reflection for %s stage" % job["stage"])
                if not reflection.debugInfo.debuggable:
                    raise ValueError("Shader is not debuggable: %s" % reflection.debugInfo.debugStatus)
                invocation = job["invocation"]
                resolved = dict(invocation)
                if job["stage"] == "vertex":
                    index = self._vertex_index(controller, pipe, job["action"], invocation["vertex"])
                    resolved["resolved_index"] = index
                    job["trace"] = controller.DebugVertex(invocation["vertex"], invocation["instance"], index, invocation["view"])
                else:
                    inputs = rd.DebugPixelInputs()
                    for name in ("primitive", "sample", "view"):
                        value = invocation[name]
                        setattr(inputs, name, rd.ReplayController.NoPreference if value is None else value)
                    job["trace"] = controller.DebugPixel(invocation["x"], invocation["y"], inputs)
                self.store.update(trace_id, invocation=resolved)
                if job["trace"] is None or job["trace"].debugger is None:
                    raise ValueError("RenderDoc could not initialize shader debugging for this invocation")
                job["mapping"] = self._write_trace(controller, pipe, reflection, trace_id, job["trace"])
                job["source_steps"] = SourceSteps(job["mapping"])
            elif not self.store.cancelled(trace_id):
                batch = controller.ContinueDebug(job["trace"].debugger)
                if not batch:
                    if not job["has_states"]:
                        raise ValueError("RenderDoc returned no shader debug states")
                    status = "complete"
                else:
                    for state in batch:
                        if self.store.cancelled(trace_id):
                            break
                        serialized = serialize_debug_state(state)
                        self.store.append(trace_id, "states", serialized)
                        job["source_steps"].observe(serialized)
                        job["mapping"].observe(state)
                        job["has_states"] = True
            if self.store.cancelled(trace_id):
                status = "cancelled"
        except Exception as error:
            status, failure = "failed", "%s: %s" % (type(error).__name__, error)
            traceback.print_exc()
        finally:
            try:
                controller.SetFrameEvent(original_event, True)
            except Exception as error:
                status, failure = "failed", "Failed to restore replay event: %s" % error
                traceback.print_exc()
            if status is not None:
                self._finalize(controller, job, status, failure)
            job["in_flight"] = False

    def _write_trace(self, controller, pipe, reflection, trace_id, trace):
        disassembly = controller.DisassembleShader(pipe.GetGraphicsPipelineObject(), reflection, "")
        debug = reflection.debugInfo
        metadata = {
            "shader_id": str(reflection.resourceId), "entry_point": reflection.entryPoint,
            "source_entry_point": debug.entrySourceName, "encoding": str(reflection.encoding),
            "source_encoding": str(debug.encoding), "source_debug_information": debug.sourceDebugInformation,
            "debug_info_loading_log": getattr(debug, "debugInfoLoadingLog", None),
            "inputs": serialize_shader_variables(trace.inputs),
            "constant_blocks": serialize_shader_variables(trace.constantBlocks),
            "read_only_resources": serialize_shader_variables(trace.readOnlyResources),
            "read_write_resources": serialize_shader_variables(trace.readWriteResources),
            "samplers": serialize_shader_variables(trace.samplers),
            "source_variables": serialize_source_variable_mappings(trace.sourceVars),
            "step_semantics": "changes describe the transition into this state; next_instruction executes next",
            "instruction_scope": "confirmed state transitions for the selected invocation",
            "state_encoding": "delta-v1; states is reconstructed, state_deltas must be decoded from start",
            "pixel_selection_warning": "RenderDoc may select another overlapping fragment; fragIndex is not a selector",
        }
        self.store.update(trace_id, metadata=metadata)
        return ExecutedMapping(trace, debug.files, disassembly, self.store, trace_id)

    def get_trace(self, trace_id, section, offset, limit):
        self._check_numbers(offset=offset, limit=limit)
        return self.store.get(trace_id, section, offset, limit)

    def release(self, trace_id):
        job = self._job
        result = self.store.release(trace_id)
        if job is not None and job["trace_id"] == trace_id and job["finalized"]:
            self._job = None
        return result

    def shutdown(self):
        self._closed = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.capture_closing()
        self.store.close()

    def capture_closing(self, replay_available=False):
        self.store.cancel_active()
        job = self._job
        if replay_available or self.ctx.IsCaptureLoaded():
            executed = [False]
            cleanup_errors = []
            def cleanup(controller):
                executed[0] = True
                if job is not None and not job["finalized"]:
                    try:
                        self._finalize(controller, job, "cancelled", "Capture closed before replay completed")
                    except Exception as error:
                        cleanup_errors.append(error)
            self.ctx.Replay().BlockInvoke(cleanup)
            if cleanup_errors:
                self._closed = True
                raise RuntimeError("Shader debugger cleanup failed: %s" % cleanup_errors[0])
            if not executed[0] and job is not None and job["trace"] is not None:
                self._closed = True
                self.store.update(job["trace_id"], status="failed",
                                  error="Replay stopped before shader debugger cleanup", complete=False)
                raise RuntimeError("Replay stopped before the shader debugger could be released")
        elif job is not None and job["trace"] is not None:
            self._closed = True
            self.store.update(job["trace_id"], status="failed",
                              error="Capture closed before shader debugger cleanup", complete=False)
            raise RuntimeError("Capture closed before the shader debugger could be released")
        self.store.finish_cancelled()
        if job is not None:
            job["finalized"] = True
        self._job = None
