import importlib
import struct
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


DRAWCALL = 0x1
INSTANCED = 0x2
INDEXED = 0x4
NO_PREFERENCE = object()


class DebugPixelInputs:
    primitive = None
    sample = None
    view = None


@pytest.fixture
def api(monkeypatch):
    extension = Path(__file__).resolve().parents[1] / "renderdoc_extension"
    package = ModuleType("renderdoc_extension")
    package.__path__ = [str(extension)]
    renderdoc = ModuleType("renderdoc")
    renderdoc.ActionFlags = SimpleNamespace(
        Drawcall=DRAWCALL, Instanced=INSTANCED, Indexed=INDEXED,
        Dispatch=0x8, Clear=0x10, Copy=0x20, Present=0x40,
        PushMarker=0x80, SetMarker=0x100,
    )
    renderdoc.ShaderStage = SimpleNamespace(
        Vertex="vertex", Hull="hull", Domain="domain", Geometry="geometry",
        Pixel="pixel", Compute="compute",
    )
    renderdoc.DebugPixelInputs = DebugPixelInputs
    renderdoc.ReplayController = SimpleNamespace(NoPreference=NO_PREFERENCE)
    renderdoc.ResourceId = SimpleNamespace(Null=lambda: None)
    monkeypatch.setitem(sys.modules, "renderdoc", renderdoc)
    qtcore = ModuleType("PySide2.QtCore")

    class Timer:
        def __init__(self):
            self.timeout = SimpleNamespace(connect=lambda callback: None)

        def start(self, interval):
            return None

        def stop(self):
            return None

    qtcore.QTimer = Timer
    monkeypatch.setitem(sys.modules, "PySide2", ModuleType("PySide2"))
    monkeypatch.setitem(sys.modules, "PySide2.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "renderdoc_extension", package)
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)
    facade = importlib.import_module("renderdoc_extension.renderdoc_facade")
    handler = importlib.import_module("renderdoc_extension.request_handler")
    store = importlib.import_module("renderdoc_extension.services.shader_trace_store")
    yield SimpleNamespace(
        facade=facade.RenderDocFacade, handler=handler.RequestHandler,
        store=store.ShaderTraceStore,
    )
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)


def value(f32v=None, u32v=None):
    return SimpleNamespace(f32v=f32v or [0.0] * 16, u32v=u32v or [0] * 16)


def float_var(name, values, rows=1, columns=None):
    return SimpleNamespace(
        name=name, type="VarType.Float", rows=rows,
        columns=len(values) if columns is None else columns,
        flags=0, value=value(f32v=list(values)), members=[],
    )


def absent_var():
    return SimpleNamespace(
        name="", type="VarType.Unknown", rows=0, columns=0, flags=0,
        value=value(), members=[],
    )


def make_reflection(files=None, debuggable=True, status="debuggable"):
    files = files if files is not None else [
        SimpleNamespace(filename="shader.hlsl", contents="zero\nfloat4 main()\nthree\n")
    ]
    return SimpleNamespace(
        resourceId="ResourceId::77", entryPoint="main", encoding="DXIL",
        debugInfo=SimpleNamespace(
            debuggable=debuggable, debugStatus=status, files=files,
            entrySourceName="main", encoding="HLSL", sourceDebugInformation=True,
            debugInfoLoadingLog="loaded",
        ),
    )


def make_state(step, changes=None):
    return SimpleNamespace(
        stepIndex=step, nextInstruction=2 + step, flags=0,
        callstack=["main"], changes=changes or [],
    )


def make_trace():
    line = SimpleNamespace(
        disassemblyLine=1, fileIndex=0, lineStart=2, lineEnd=2,
        colStart=0, colEnd=4,
    )
    infos = [
        SimpleNamespace(instruction=3, lineInfo=line, sourceVars=[]),
        SimpleNamespace(instruction=4, lineInfo=line, sourceVars=[]),
    ]
    changes = [SimpleNamespace(before=absent_var(), after=float_var("tmp", [1.5]))]
    return SimpleNamespace(
        debugger=object(), inputs=[float_var("pos", [1.0, 2.0])],
        constantBlocks=[], readOnlyResources=[], readWriteResources=[],
        samplers=[], sourceVars=[], instInfo=infos, states=[make_state(1, changes)],
    )


class Pipe:
    def __init__(self, reflection, ibuffer):
        self.reflection = reflection
        self.ibuffer = ibuffer

    def GetShaderReflection(self, stage):
        return self.reflection

    def GetGraphicsPipelineObject(self):
        return "graphics-pipeline"

    def GetIBuffer(self):
        return self.ibuffer

    def IsRestartEnabled(self):
        return False

    def GetRestartIndex(self):
        return 0


class Controller:
    def __init__(self, reflection, trace, ibuffer=None, batches=None):
        self.pipe = Pipe(reflection, ibuffer)
        self.trace = trace
        self.batches = [list(batch) for batch in (batches or [])]
        self.buffer_data = {}
        self.debug_vertex_calls = []
        self.debug_pixel_calls = []
        self.freed = []
        self.frame_events = []
        self.continue_calls = 0
        self.on_continue = None
        self.continue_error = None

    def SetFrameEvent(self, event_id, force):
        self.frame_events.append((event_id, force))

    def GetPipelineState(self):
        return self.pipe

    def GetIBuffer(self):
        return self.pipe.GetIBuffer()

    def GetBufferData(self, resource_id, offset, length):
        return self.buffer_data.get((resource_id, offset, length), b"")

    def DebugVertex(self, vertex, instance, index, view):
        self.debug_vertex_calls.append((vertex, instance, index, view))
        return self.trace

    def DebugPixel(self, x, y, inputs):
        self.debug_pixel_calls.append((
            x, y, {name: getattr(inputs, name) for name in ("primitive", "sample", "view")}
        ))
        return self.trace

    def FreeTrace(self, trace):
        self.freed.append(trace)

    def DisassembleShader(self, pipeline, reflection, target):
        return "op zero\nop one\nop two"

    def ContinueDebug(self, debugger):
        self.continue_calls += 1
        if self.continue_error is not None:
            raise self.continue_error
        if self.on_continue is not None:
            self.on_continue()
        return self.batches.pop(0) if self.batches else []


class Replay:
    def __init__(self, controller):
        self.controller = controller
        self.pending = []
        self.pump = None

    def AsyncInvoke(self, callback):
        self.pending.append(callback)

    def BlockInvoke(self, callback):
        callback(self.controller)

    def drain(self):
        if self.pump is not None:
            self.pump()
        while self.pending:
            self.pending.pop(0)(self.controller)
            if self.pump is not None:
                self.pump()


class Context:
    def __init__(self, controller, loaded=True):
        self.controller = controller
        self.loaded = loaded
        self.replay = Replay(controller)
        self.actions = {}
        self.filename = "capture.rdc"
        self.current_event = 999

    def IsCaptureLoaded(self):
        return self.loaded

    def GetAction(self, event_id):
        return self.actions.get(event_id)

    def GetCaptureFilename(self):
        return self.filename

    def CurEvent(self):
        return self.current_event

    def Replay(self):
        return self.replay


def make_action(event_id=10, flags=DRAWCALL, indices=4, instances=1,
                index_offset=0, base_vertex=0):
    return SimpleNamespace(
        eventId=event_id, flags=flags, numIndices=indices,
        numInstances=instances, indexOffset=index_offset, baseVertex=base_vertex,
    )


def create_env(api, controller=None, action=None, loaded=True):
    if controller is None:
        controller = Controller(make_reflection(), make_trace())
    ctx = Context(controller, loaded)
    if action is not None:
        ctx.actions[action.eventId] = action
    facade = api.facade(ctx)
    ctx.replay.pump = facade._debug.pump
    return SimpleNamespace(
        facade=facade, handler=api.handler(facade), ctx=ctx, controller=controller,
    )


def call(env, method, **params):
    return env.handler.handle({"id": "req", "method": method, "params": params})


def section(env, trace_id, name, offset=0, limit=100):
    return call(
        env, "get_debug_trace", trace_id=trace_id, section=name,
        offset=offset, limit=limit,
    )


def test_vertex_indexed_trace_uses_offset_and_base_and_serializes_source(api):
    trace = make_trace()
    controller = Controller(
        make_reflection(), trace, ibuffer=SimpleNamespace(
            byteStride=2, byteOffset=4, resourceId="ibuf",
        ),
        batches=[[trace.states[0], make_state(2)], [make_state(3)]],
    )
    controller.buffer_data[("ibuf", 26, 2)] = struct.pack("<H", 7)
    env = create_env(
        api, controller,
        make_action(flags=DRAWCALL | INDEXED, indices=4,
                    index_offset=10, base_vertex=100),
    )

    response = call(env, "debug_vertex", event_id=10, vertex=1, instance=0, view=0)
    job = response["result"]
    assert job["status"] == "queued"
    assert controller.frame_events == []

    env.ctx.replay.drain()

    assert controller.debug_vertex_calls == [(1, 0, 107, 0)]
    status = section(env, job["trace_id"], "status")["result"]
    assert status["status"] == "complete"
    assert status["complete"] is True
    assert status["invocation"]["resolved_index"] == 107
    assert status["counts"] == {"states": 3, "instructions": 2, "sources": 1, "source_variables": 1, "source_steps": 1, "mapping_definitions": 0}

    metadata = section(env, job["trace_id"], "metadata")["result"]["metadata"]
    assert metadata["shader_id"] == "ResourceId::77"
    assert metadata["source_entry_point"] == "main"
    assert metadata["inputs"][0]["name"] == "pos"
    assert metadata["inputs"][0]["value"] == [1.0, 2.0]

    sources = section(env, job["trace_id"], "sources")["result"]["items"]
    assert sources == [{"file_index": 0, "filename": "shader.hlsl", "line": 2, "text": "float4 main()"}]
    instructions = section(env, job["trace_id"], "instructions")["result"]["items"]
    assert [item["instruction"] for item in instructions] == [3, 4]
    assert instructions[0]["line_info"]["line_start"] == 2
    assert instructions[0]["line_info"]["disassembly_line_text"] == "op zero"

    states = section(env, job["trace_id"], "states")["result"]["items"]
    assert [state["step_index"] for state in states] == [1, 2, 3]
    assert states[0]["callstack"] == ["main"]
    assert states[0]["changes"][0]["change_type"] == "created"
    assert states[0]["changes"][0]["after"]["value"] == [1.5]
    assert controller.freed == [trace]
    assert controller.frame_events[0] == (10, True)
    assert controller.frame_events[-1] == (999, True)


def test_vertex_without_index_buffer_keeps_the_invocation_index(api):
    controller = Controller(make_reflection(), make_trace(), batches=[[make_state(1)]])
    env = create_env(api, controller, make_action(indices=5, index_offset=10, base_vertex=100))

    job = call(env, "debug_vertex", event_id=10, vertex=1)["result"]
    env.ctx.replay.drain()

    assert controller.debug_vertex_calls == [(1, 0, 1, 0)]
    status = section(env, job["trace_id"], "status")["result"]
    assert status["status"] == "complete"
    assert status["invocation"]["resolved_index"] == 1


def test_empty_trace_is_reported_as_failure(api):
    trace = make_trace()
    controller = Controller(make_reflection(), trace)
    env = create_env(api, controller, make_action())

    job = call(env, "debug_vertex", event_id=10, vertex=0)["result"]
    env.ctx.replay.drain()

    status = section(env, job["trace_id"], "status")["result"]
    assert status["status"] == "failed"
    assert status["error"] == "ValueError: RenderDoc returned no shader debug states"
    assert controller.freed == [trace]


def test_pixel_trace_maps_unset_inputs_to_no_preference(api):
    trace = make_trace()
    controller = Controller(make_reflection(), trace, batches=[[make_state(1)]])
    env = create_env(api, controller, make_action(flags=DRAWCALL))

    job = call(
        env, "debug_pixel", event_id=10, x=3, y=4,
        primitive=2, sample=None, view=7,
    )["result"]
    env.ctx.replay.drain()

    assert controller.debug_pixel_calls == [(
        3, 4, {"primitive": 2, "sample": NO_PREFERENCE, "view": 7},
    )]
    status = section(env, job["trace_id"], "status")["result"]
    assert status["stage"] == "pixel"
    assert status["status"] == "complete"
    assert status["invocation"] == {"x": 3, "y": 4, "primitive": 2, "sample": None, "view": 7}
    states = section(env, job["trace_id"], "states")["result"]["items"]
    assert states[0]["next_instruction"] == 3


def test_executed_mappings_follow_transitions_across_batches_and_deduplicate(api):
    trace = make_trace()
    states = [make_state(i) for i in range(1, 6)]
    for state, instruction in zip(states, [5, 9, 5, 9, 42]):
        state.nextInstruction = instruction
    trace.instInfo[0].instruction = 3
    trace.instInfo[1].instruction = 8
    controller = Controller(make_reflection(), trace, batches=[states[:2], states[2:]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.ctx.replay.drain()

    result = section(env, trace_id, "instructions", 0, 1)["result"]
    next_page = section(env, trace_id, "instructions", result["next_offset"], 1)["result"]
    items = result["items"] + next_page["items"]
    assert [(item["instruction"], item["mapping_instruction"]) for item in items] == [(5, 3), (9, 8)]
    assert next_page["next_offset"] is None
    assert len(section(env, trace_id, "states")["result"]["items"]) == 5
    assert len(section(env, trace_id, "sources")["result"]["items"]) == 1
    assert len(section(env, trace_id, "source_variables")["result"]["items"]) == 1
    assert env.facade._debug._job["mapping"] is None


def test_unmapped_instruction_remains_visible_without_fabricating_source(api):
    trace = make_trace()
    states = [make_state(1), make_state(2)]
    states[0].nextInstruction = 1
    controller = Controller(make_reflection(), trace, batches=[states])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.ctx.replay.drain()

    items = section(env, trace_id, "instructions")["result"]["items"]
    assert items == [{"instruction": 1, "mapping_instruction": None,
                      "line_info": None, "source_variables_id": None}]
    assert section(env, trace_id, "sources")["result"]["items"] == []


def test_large_unexecuted_instruction_inventory_does_not_consume_trace_quota(api):
    trace = make_trace()
    line = trace.instInfo[0].lineInfo
    trace.instInfo = [SimpleNamespace(instruction=i, lineInfo=line, sourceVars=[]) for i in range(2000)]
    states = [make_state(1), make_state(2)]
    states[0].nextInstruction = 1000
    controller = Controller(make_reflection(), trace, batches=[states])
    env = create_env(api, controller, make_action())
    env.facade._debug.store._max_bytes = 10000
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.ctx.replay.drain()

    status = section(env, trace_id, "status")["result"]
    assert status["status"] == "complete"
    assert status["counts"]["states"] == 2
    assert [item["instruction"] for item in section(env, trace_id, "instructions")["result"]["items"]] == [1000]


def test_failed_trace_keeps_confirmed_transitions_but_not_pending_instruction(api):
    trace = make_trace()
    controller = Controller(make_reflection(), trace, batches=[[make_state(1), make_state(2)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)
    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)
    controller.continue_error = RuntimeError("next batch failed")

    env.ctx.replay.drain()

    assert section(env, trace_id, "status")["result"]["status"] == "failed"
    assert [item["instruction"] for item in section(env, trace_id, "instructions")["result"]["items"]] == [3]
    assert controller.freed == [trace]


def test_source_steps_merge_consecutive_locations_and_preserve_loop_boundaries(api):
    trace = make_trace()
    states = [make_state(i) for i in range(5)]
    for state, instruction in zip(states, [3, 4, 3, 4, 5]):
        state.nextInstruction = instruction
    states[1].changes = [SimpleNamespace(before=float_var("x", [0.0]), after=float_var("x", [1.0]))]
    states[2].changes = [SimpleNamespace(before=float_var("x", [1.0]), after=float_var("x", [2.0]))]
    controller = Controller(make_reflection(), trace, batches=[states[:2], states[2:]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.ctx.replay.drain()

    first = section(env, trace_id, "source_steps", 0, 1)["result"]
    second = section(env, trace_id, "source_steps", first["next_offset"], 1)["result"]
    assert first["items"][0]["transitions"] == 2
    assert first["items"][0]["changes"]["x"] == {
        "before": [0.0], "before_present": True, "after": [2.0],
        "after_present": True, "updates": 2}
    assert [(g["step_start"], g["step_end"]) for g in first["items"] + second["items"]] == [(1, 2), (3, 4)]
    assert second["next_offset"] is None
    assert len(section(env, trace_id, "states")["result"]["items"]) == 5


def test_compact_source_step_resource_retains_direct_descriptor_store(api):
    module = importlib.import_module("renderdoc_extension.services.shader_source_steps")
    binding = {"index": 7}
    result = module.compact_value({"binding": binding,
                                  "direct_access": {"descriptor_store": "1234"}})
    assert result == {"binding": {"index": 7}, "direct_access": {"descriptor_store": "1234"}}


def test_mapping_failure_keeps_stored_transition_in_partial_source_steps(api):
    controller = Controller(make_reflection(), make_trace(), batches=[[make_state(0), make_state(1)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)
    original = env.facade._debug._job["mapping"].observe
    def fail_mapping(state):
        if state.stepIndex == 1:
            raise RuntimeError("mapping failed")
        original(state)
    env.facade._debug._job["mapping"].observe = fail_mapping

    env.ctx.replay.drain()

    assert section(env, trace_id, "status")["result"]["status"] == "failed"
    items = section(env, trace_id, "source_steps")["result"]["items"]
    assert [(item["step_start"], item["step_end"], item["transitions"]) for item in items] == [(1, 1, 1)]


def test_source_variable_snapshots_share_definitions_and_restore_order(api):
    def mapping(name):
        return SimpleNamespace(name=name, type="VarType.Float", undefinedValue=False,
            rows=1, columns=1, offset=0, signatureIndex=0,
            variables=[SimpleNamespace(name=name, type="DebugVariableType.Variable", component=0)])
    trace = make_trace()
    a, b = mapping("a"), mapping("b")
    trace.instInfo[0].sourceVars = [a, b]
    trace.instInfo[1].sourceVars = [b, a]
    controller = Controller(make_reflection(), trace, batches=[[make_state(1), make_state(2), make_state(3)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.ctx.replay.drain()

    definitions = {item["id"]: item["mapping"] for item in section(env, trace_id, "mapping_definitions")["result"]["items"]}
    snapshots = section(env, trace_id, "source_variables")["result"]["items"]
    assert len(definitions) == 2
    assert [[definitions[i]["name"] for i in item["mapping_ids"]] for item in snapshots] == [["a", "b"], ["b", "a"]]


def test_nop_runs_fold_but_changed_or_flagged_nops_remain_visible(api):
    trace = make_trace()
    line = trace.instInfo[0].lineInfo
    trace.instInfo = [SimpleNamespace(instruction=i, lineInfo=line, sourceVars=[]) for i in range(4)]
    states = [make_state(i) for i in range(5)]
    for state, instruction in zip(states, [0, 1, 2, 3, 3]):
        state.nextInstruction = instruction
    states[3].flags = 2
    states[4].changes = [SimpleNamespace(before=absent_var(), after=float_var("x", [1.0]))]
    controller = Controller(make_reflection(), trace, batches=[states])
    controller.DisassembleShader = lambda *args: "nop;"
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.ctx.replay.drain()

    groups = section(env, trace_id, "source_steps")["result"]["items"]
    assert [(g["kind"], g["transitions"]) for g in groups] == [("auxiliary", 2), ("source", 2)]
    assert groups[1]["flags_value"] == 2
    assert groups[1]["changes"]["x"]["after"] == [1.0]


def test_pagination_is_stable_and_tracks_queued_and_complete_status(api):
    states = [make_state(index) for index in range(1, 6)]
    controller = Controller(
        make_reflection(), make_trace(),
        batches=[states[0:2], states[2:3], states[3:5]],
    )
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    queued = section(env, trace_id, "states", 0, 2)["result"]
    assert queued["items"] == []
    assert queued["next_offset"] == 0

    env.ctx.replay.drain()

    first = section(env, trace_id, "states", 0, 2)["result"]
    assert [item["step_index"] for item in first["items"]] == [1, 2]
    assert first["next_offset"] == 2
    assert section(env, trace_id, "states", 0, 2)["result"] == first

    second = section(env, trace_id, "states", 2, 2)["result"]
    assert [item["step_index"] for item in second["items"]] == [3, 4]
    assert second["next_offset"] == 4

    last = section(env, trace_id, "states", 4, 2)["result"]
    assert [item["step_index"] for item in last["items"]] == [5]
    assert last["next_offset"] is None

    empty = section(env, trace_id, "states", 5, 2)["result"]
    assert empty["items"] == []
    assert empty["next_offset"] is None


def test_failure_after_trace_creation_frees_trace_and_restores_event(api):
    trace = make_trace()
    controller = Controller(make_reflection(), trace, batches=[[make_state(1)]])
    controller.continue_error = RuntimeError("boom")
    env = create_env(api, controller, make_action())

    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.ctx.replay.drain()

    status = section(env, trace_id, "status")["result"]
    assert status["status"] == "failed"
    assert status["complete"] is False
    assert status["error"] == "RuntimeError: boom"
    assert controller.freed == [trace]
    assert controller.frame_events[0] == (10, True)
    assert controller.frame_events[-1] == (999, True)
    states = section(env, trace_id, "states", 0, 2)["result"]
    assert states["items"] == []
    assert states["next_offset"] is None


def test_failure_before_trace_creation_reports_debuggability_error(api):
    reflection = make_reflection(debuggable=False, status="no debug info")
    controller = Controller(reflection, make_trace())
    env = create_env(api, controller, make_action())

    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.ctx.replay.drain()

    status = section(env, trace_id, "status")["result"]
    assert status["status"] == "failed"
    assert status["error"] == "ValueError: Shader is not debuggable: no debug info"
    assert status["counts"] == {"states": 0, "instructions": 0, "sources": 0, "source_variables": 0, "source_steps": 0, "mapping_definitions": 0}
    assert controller.freed == []
    assert controller.frame_events == [(10, True), (999, True)]


def test_release_before_drain_cancels_and_removes_the_trace(api):
    controller = Controller(make_reflection(), make_trace())
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    released = call(env, "release_debug_trace", trace_id=trace_id)["result"]
    assert released == {"trace_id": trace_id, "status": "cancelling"}
    assert section(env, trace_id, "status")["result"]["status"] == "cancelling"

    env.ctx.replay.drain()

    assert controller.freed == []
    response = section(env, trace_id, "status")
    assert response["error"]["code"] == -32602
    assert "Unknown or released" in response["error"]["message"]


def test_release_during_state_streaming_stops_and_frees_the_trace(api):
    trace = make_trace()
    controller = Controller(
        make_reflection(), trace, batches=[[make_state(1)], [make_state(2)]],
    )
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    controller.on_continue = lambda: call(env, "release_debug_trace", trace_id=trace_id)

    env.ctx.replay.drain()

    assert controller.freed == [trace]
    response = section(env, trace_id, "status")
    assert response["error"]["code"] == -32602
    assert "Unknown or released" in response["error"]["message"]


def test_capture_close_frees_trace_between_replay_slices(api):
    trace = make_trace()
    controller = Controller(make_reflection(), trace, batches=[[make_state(1)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)
    assert section(env, trace_id, "status")["result"]["status"] == "running"
    env.facade.capture_closing()

    assert controller.freed == [trace]
    assert section(env, trace_id, "status")["result"]["status"] == "cancelled"
    env.ctx.replay.drain()
    assert controller.freed == [trace]


def test_145_capture_close_cleans_trace_after_loaded_flag_clears(api):
    trace = make_trace()
    controller = Controller(make_reflection(), trace, batches=[[make_state(1)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)
    env.ctx.loaded = False

    env.facade.capture_closing(replay_available=True)

    assert controller.freed == [trace]
    assert section(env, trace_id, "status")["result"]["status"] == "cancelled"


def test_capture_close_after_releasing_completed_trace(api):
    controller = Controller(make_reflection(), make_trace(), batches=[[make_state(1)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.ctx.replay.drain()

    assert call(env, "release_debug_trace", trace_id=trace_id)["result"]["status"] == "released"
    env.facade.capture_closing()


def test_each_slice_restores_current_ui_event(api):
    controller = Controller(make_reflection(), make_trace(), batches=[[make_state(1)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)
    env.ctx.current_event = 1234
    env.facade._debug.pump()
    env.ctx.replay.pending.pop(0)(controller)

    assert controller.frame_events[-1] == (1234, True)
    env.ctx.replay.drain()
    assert section(env, trace_id, "status")["result"]["status"] == "complete"


def test_capture_closing_cancels_queued_work_and_queue_can_be_drained(api):
    controller = Controller(make_reflection(), make_trace())
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.facade._debug.pump()
    assert len(env.ctx.replay.pending) == 1

    env.facade.capture_closing()

    status = section(env, trace_id, "status")["result"]
    assert status["status"] == "cancelled"
    assert status["error"] == "Capture closed before replay completed"

    env.ctx.replay.drain()

    assert controller.freed == []
    assert section(env, trace_id, "status")["result"]["status"] == "cancelled"
    assert call(env, "release_debug_trace", trace_id=trace_id)["result"]["status"] == "released"
    assert section(env, trace_id, "status")["error"]["code"] == -32602


def test_shutdown_rejects_new_jobs_and_drops_existing_store(api):
    controller = Controller(make_reflection(), make_trace())
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]

    env.facade.shutdown()

    response = call(env, "debug_vertex", event_id=10, vertex=0)
    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "No capture loaded"
    assert section(env, trace_id, "status")["error"]["code"] == -32602


def test_start_validation_rejects_bad_invocations(api):
    controller = Controller(make_reflection(), make_trace())
    env = create_env(api, controller, make_action(indices=2, instances=1))

    cases = [
        (dict(event_id=10, vertex=True), "vertex must be an integer"),
        (dict(event_id=10, vertex=-1), "vertex must be an integer"),
        (dict(event_id=0xFFFFFFFF, vertex=0), "event_id must be an integer"),
        (dict(event_id=10, vertex=2), "outside the draw"),
        (dict(event_id=10, vertex=0, instance=1), "outside the draw"),
        (dict(event_id=11, vertex=0), "not a draw call"),
    ]
    for params, message in cases:
        response = call(env, "debug_vertex", **params)
        assert response["error"]["code"] == -32602
        assert message in response["error"]["message"]

    for params, message in [
        (dict(event_id=10, x=-1, y=0), "x must be an integer"),
        (dict(event_id=10, x=0, y=0, primitive=-1), "primitive must be an integer"),
        (dict(event_id=10, x=0, y=0, sample=True), "sample must be an integer"),
    ]:
        response = call(env, "debug_pixel", **params)
        assert response["error"]["code"] == -32602
        assert message in response["error"]["message"]

    assert call(env, "debug_vertex", event_id=10, vertex=0)["result"]["status"] == "queued"
    busy = call(env, "debug_vertex", event_id=10, vertex=0)
    assert busy["error"]["code"] == -32602
    assert busy["error"]["message"] == "Another shader debug job is active"

    unloaded = create_env(api, loaded=False)
    response = call(unloaded, "debug_vertex", event_id=10, vertex=0)
    assert response["error"]["message"] == "No capture loaded"

    not_draw = create_env(api, action=make_action(flags=INSTANCED))
    response = call(not_draw, "debug_vertex", event_id=10, vertex=0)
    assert response["error"]["message"] == "Event 10 is not a draw call"


def test_delta_storage_reconstructs_pages_without_duplicate_snapshots(api):
    controller = Controller(make_reflection(), make_trace(), batches=[[
        make_state(0, [SimpleNamespace(before=absent_var(), after=float_var("x", [1.0]))]),
        make_state(1, [SimpleNamespace(before=float_var("x", [1.0]), after=float_var("x", [2.0]))]),
        make_state(2)]])
    env = create_env(api, controller, make_action())
    trace_id = call(env, "debug_vertex", event_id=10, vertex=0)["result"]["trace_id"]
    env.ctx.replay.drain()

    page = section(env, trace_id, "states", 1, 1)["result"]
    repeated = section(env, trace_id, "states", 1, 1)["result"]
    delta = section(env, trace_id, "state_deltas", 1, 1)["result"]["items"][0]

    assert repeated == page
    assert page["items"][0]["changes"][0]["before"]["value"] == [1.0]
    assert page["items"][0]["changes"][0]["after"]["value"] == [2.0]
    assert delta["changes"][0]["before"] == {"id": 0}
    assert delta["changes"][0]["after"]["patch"] == [{"op": "set", "path": ["value", 0], "value": 2.0}]
    assert section(env, trace_id, "states", 2, 1)["result"]["items"][0]["callstack"] == ["main"]


def test_rejected_delta_append_does_not_publish_missing_baselines(api):
    store = api.store(max_bytes=300)
    trace_id = store.create("pixel", 10, "capture.rdc", {})["trace_id"]
    serializer = importlib.import_module("renderdoc_extension.utils.shader_debug_serializer")
    large = serializer.serialize_debug_state(make_state(0, [
        SimpleNamespace(before=absent_var(), after=float_var("huge", [1.0] * 100))]))
    with pytest.raises(RuntimeError, match="storage limit"):
        store.append(trace_id, "states", large)
    small = serializer.serialize_debug_state(make_state(1))

    store.append(trace_id, "states", small)

    assert store.get(trace_id, "states", 0, 1)["items"] == [small]
    store.close()


def test_trace_store_quota_and_read_validation(api):
    store = api.store(max_bytes=32)
    trace_id = store.create("vertex", 10, "capture.rdc", {})["trace_id"]

    with pytest.raises(RuntimeError, match="storage limit"):
        store.append(trace_id, "states", {"payload": "x" * 64})
    with pytest.raises(ValueError, match="Unknown trace section"):
        store.get(trace_id, "bogus", 0, 1)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        store.get(trace_id, "states", -1, 1)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        store.get(trace_id, "states", 0, 0)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        store.get(trace_id, "states", 0, 1001)
    with pytest.raises(ValueError, match="Unknown or released"):
        store.get("missing", "status", 0, 1)

    job = store.get(trace_id, "status", 0, 1)
    assert job["counts"] == {"states": 0, "instructions": 0, "sources": 0, "source_variables": 0, "source_steps": 0, "mapping_definitions": 0}
    store.close()
