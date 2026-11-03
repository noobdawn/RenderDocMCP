import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def shader_api(monkeypatch):
    extension = Path(__file__).resolve().parents[1] / "renderdoc_extension"
    package = ModuleType("renderdoc_extension")
    package.__path__ = [str(extension)]
    renderdoc = ModuleType("renderdoc")
    renderdoc.ShaderStage = SimpleNamespace(
        Vertex="vertex", Hull="hull", Domain="domain", Geometry="geometry",
        Pixel="pixel", Compute="compute",
    )
    renderdoc.ResourceId = SimpleNamespace(Null=lambda: None)
    monkeypatch.setitem(sys.modules, "renderdoc", renderdoc)
    monkeypatch.setitem(sys.modules, "renderdoc_extension", package)
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)
    pipeline = importlib.import_module("renderdoc_extension.services.pipeline_service")
    handler = importlib.import_module("renderdoc_extension.request_handler")
    yield pipeline.PipelineService, handler.RequestHandler
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)


class Pipe:
    def __init__(self, reflection):
        self.reflection = reflection

    def GetShader(self, stage):
        return "shader-1"

    def GetShaderEntryPoint(self, stage):
        return "main"

    def GetShaderReflection(self, stage):
        return self.reflection

    def GetGraphicsPipelineObject(self):
        return "graphics-pipeline"

    def GetComputePipelineObject(self):
        return "compute-pipeline"

    def GetConstantBuffer(self, stage, index, array_index):
        return SimpleNamespace(resourceId="buffer", byteOffset=0, byteSize=16)


class Controller:
    def __init__(self, reflection):
        self.pipe = Pipe(reflection)

    def SetFrameEvent(self, event_id, force):
        self.event_id = event_id

    def GetPipelineState(self):
        return self.pipe

    def GetDisassemblyTargets(self, with_pipeline):
        return ["DXBC/DXIL"]

    def DisassembleShader(self, pipeline, reflection, target):
        return "%s:%s:%s" % (self.event_id, pipeline, target)

    def GetCBufferVariableContents(self, pipeline, *args):
        return [SimpleNamespace(name=pipeline, type="unknown", rows=1, columns=1, members=[])]


def reflection_with_sources(files, loading_log=""):
    return SimpleNamespace(
        resourceId="shader-1", entryPoint="main", encoding="DXIL",
        debugInfo=SimpleNamespace(
            files=[SimpleNamespace(filename=name, contents=contents) for name, contents in files],
            encoding="HLSL", entrySourceName="MainPS", sourceDebugInformation=True,
            debugInfoLoadingLog=loading_log,
        ),
        constantBlocks=[SimpleNamespace(name="constants", byteSize=16)],
        readOnlyResources=[], readWriteResources=[],
    )


def request(shader_api, reflection, stage="pixel"):
    service_type, handler_type = shader_api
    controller = Controller(reflection)
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: True)
    service = service_type(ctx, lambda callback: callback(controller))
    return handler_type(service).handle({
        "id": "request", "method": "get_shader_info",
        "params": {"event_id": 123, "stage": stage},
    })


def test_shader_info_returns_all_source_files_and_native_assembly(shader_api):
    files = [("main.usf", "float4 MainPS() {}"), ("common.ush", "#define X 1")]

    result = request(shader_api, reflection_with_sources(files))["result"]

    assert result["source_files"] == [{"filename": name, "contents": text} for name, text in files]
    assert result["source_available"] is True
    assert result["disassembly"] == "123:graphics-pipeline:DXBC/DXIL"
    assert result["source_entry_point"] == "MainPS"


def test_missing_source_preserves_assembly_and_loading_diagnostics(shader_api):
    result = request(shader_api, reflection_with_sources([], "debug artifact missing"))["result"]

    assert result["source_files"] == []
    assert result["source_available"] is False
    assert result["debug_info_loading_log"] == "debug artifact missing"
    assert result["disassembly"] == "123:graphics-pipeline:DXBC/DXIL"


def test_compute_shader_uses_compute_pipeline_for_disassembly_and_constants(shader_api):
    result = request(shader_api, reflection_with_sources([]), "compute")["result"]

    assert result["disassembly"] == "123:compute-pipeline:DXBC/DXIL"
    assert result["constant_buffers"][0]["variables"][0]["name"] == "compute-pipeline"


def test_missing_reflection_returns_request_error(shader_api):
    response = request(shader_api, None)

    assert response["error"]["code"] == -32602
    assert "reflection unavailable" in response["error"]["message"]
