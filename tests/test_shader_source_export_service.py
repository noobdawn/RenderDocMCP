import importlib
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
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
    module = importlib.import_module(
        "renderdoc_extension.services.shader_source_export_service")
    yield module
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)


SOURCE_NAME = r"C:\project\shaders\main.hlsl"
SOURCE_LINES = ["unused %d" % number for number in range(1, 21)]
SOURCE_LINES[4] = "UNMAPPED_SOURCE_LINE"
SOURCE_LINES[6] = "UNREFERENCED_LOCATION_LINE"
SOURCE_LINES[7] = "INVARIANT_LINE"
SOURCE_LINES[8] = "LIFETIME_LINE"
SOURCE_LINES[10] = "DBG_VALUE_LINE"
SOURCE_LINES[11] = "float mapped = a;"
SOURCE_LINES[12] = "\tfloat mapped2 = b;  "
SOURCE_LINES[13] = "if (mapped) {"
SOURCE_LINES[15] = "return;"
SOURCE_TEXT = "\n".join(SOURCE_LINES) + "\n"

DISASSEMBLY = r"""
target datalayout = "e-m:e-p:32:32-i1:32"
target triple = "dxil-ms-dx"

define void @main() {
entry:
  %a = call float @dx.op.unary.f32(i32 6, float 1.0), !dbg !10
  call void @llvm.dbg.value(metadata float %a, metadata !20, metadata !21), !dbg !11
  call void @llvm.lifetime.start.p0i8(i64 4, i8* %slot), !dbg !12
  call void @llvm.invariant.start.p0i8(i64 4, i8* %slot), !dbg !13
  %b = fadd float %a, 2.0, !dbg !14
  br i1 %a, label %then, label %endif, !dbg !15
then:
  %c = fmul float %b, 3.0, !dbg !10
  br label %endif, !dbg !15
endif:
  ret void, !dbg !16
}

!10 = !DILocation(line: 12, column: 3, scope: !30)
!11 = !DILocation(line: 11, column: 1, scope: !30)
!12 = !DILocation(line: 9, column: 1, scope: !30)
!13 = !DILocation(line: 8, column: 1, scope: !30)
!14 = !DILocation(line: 13, column: 5, scope: !31)
!15 = !DILocation(line: 14, column: 2, scope: !31)
!16 = !DILocation(line: 16, column: 1, scope: !31)
!20 = !DILocalVariable(name: "a", scope: !30, file: !41, line: 12, type: !42)
!21 = !DIExpression()
!30 = distinct !DISubprogram(name: "main", scope: !40, file: !41, line: 1, type: !42, isLocal: false, isDefinition: true)
!31 = distinct !DILexicalBlock(scope: !30, file: !41, line: 13, column: 1)
!40 = !DICompileUnit(language: DW_LANG_C_plus_plus, file: !41, producer: "dxc", isOptimized: true, flags: "", runtimeVersion: 1, emissionKind: 1)
!41 = !DIFile(filename: "shaders\\main.hlsl", directory: "C:\\project")
!42 = !DISubroutineType(types: !43)
!43 = !{null}
!50 = !DILocation(line: 7, column: 1, scope: !30)
"""


def single_location_disassembly(filename, directory, line=2):
    return r"""
define void @main() {
entry:
  ret void, !dbg !10
}

!10 = !DILocation(line: %d, column: 1, scope: !20)
!20 = distinct !DISubprogram(name: "main", scope: !30, file: !40, line: 1, isLocal: false, isDefinition: true)
!30 = !DICompileUnit(language: 1, file: !40)
!40 = !DIFile(filename: "%s", directory: "%s")
""" % (line, filename, directory)


def pair(filename, lines):
    return (filename, "\n".join(lines) + "\n")


def make_sources(api, pairs):
    return [api.SourceFile(filename, contents) for filename, contents in pairs]


def test_parser_maps_only_retained_dbg_instructions_and_dedups_lines(api):
    mapping = api.parse_dxil_source_mapping(
        DISASSEMBLY, make_sources(api, [pair(SOURCE_NAME, SOURCE_LINES)]))

    assert [(item.filename, item.line, item.text, item.instructions)
            for item in mapping.lines] == [
        (SOURCE_NAME, 12, "float mapped = a;", 2),
        (SOURCE_NAME, 13, "\tfloat mapped2 = b;  ", 1),
        (SOURCE_NAME, 14, "if (mapped) {", 2),
        (SOURCE_NAME, 16, "return;", 1),
    ]
    assert mapping.counts["define_functions"] == 1
    assert mapping.counts["executable_instructions"] == 6
    assert mapping.counts["instructions_with_debug_location"] == 6
    assert mapping.counts["mapped_instructions"] == 6
    assert mapping.counts["excluded_debug_instructions"] == 3
    assert mapping.counts["unreferenced_debug_locations"] == 4
    assert mapping.counts["unresolved_instructions"] == 0
    assert mapping.counts["mapped_source_lines"] == 4
    assert mapping.unresolved == []


def test_parser_uses_unique_basename_fallback(api):
    mapping = api.parse_dxil_source_mapping(
        single_location_disassembly("unique.hlsl", r"C:\\other"),
        make_sources(api, [
            pair(r"C:\a\other.hlsl", ["a", "b", "c"]),
            pair(r"C:\b\unique.hlsl", ["a", "TARGET", "c"]),
        ]))

    assert len(mapping.lines) == 1
    assert mapping.lines[0] == api.MappedLine(
        r"C:\b\unique.hlsl", 2, "TARGET", 1)
    assert mapping.counts["mapped_source_files"] == 1


def test_parser_does_not_guess_ambiguous_basenames(api):
    mapping = api.parse_dxil_source_mapping(
        single_location_disassembly("common.hlsl", r"C:\\other"),
        make_sources(api, [
            pair(r"C:\a\common.hlsl", ["a", "A", "c"]),
            pair(r"C:\b\common.hlsl", ["a", "B", "c"]),
        ]))

    assert mapping.lines == []
    assert mapping.counts["mapped_instructions"] == 0
    assert mapping.counts["unresolved_instructions"] == 1
    assert mapping.unresolved == [{
        "reason": "ambiguous_source_file",
        "detail": r"C:\other\common.hlsl",
        "count": 1,
        "matches": [r"C:\a\common.hlsl", r"C:\b\common.hlsl"],
    }]


def test_parser_reports_missing_source_files(api):
    mapping = api.parse_dxil_source_mapping(
        single_location_disassembly("missing.hlsl", r"C:\\nowhere"),
        make_sources(api, [pair(r"C:\a\other.hlsl", ["a", "b", "c"])]))

    assert mapping.lines == []
    assert mapping.counts["mapped_instructions"] == 0
    assert mapping.unresolved == [{
        "reason": "missing_source_file",
        "detail": r"C:\nowhere\missing.hlsl",
        "count": 1,
    }]


def test_parser_matches_normalized_full_path_after_unescaping(api):
    filename = r"C:\project\shader dir\main.hlsl"
    mapping = api.parse_dxil_source_mapping(
        single_location_disassembly(
            "main.hlsl", r"C:\5Cproject\5Cshader\20dir"),
        make_sources(api, [pair(filename, ["a", "ESCAPED", "c"])]))

    assert len(mapping.lines) == 1
    assert mapping.lines[0].filename == filename
    assert mapping.lines[0].text == "ESCAPED"
    assert mapping.unresolved == []


def test_parser_reports_out_of_range_source_lines(api):
    mapping = api.parse_dxil_source_mapping(
        single_location_disassembly("main.hlsl", "", line=99),
        make_sources(api, [pair("main.hlsl", ["a", "b", "c"])]))

    assert mapping.lines == []
    assert mapping.counts["mapped_instructions"] == 0
    assert mapping.unresolved[0]["reason"] == "source_line_out_of_range"
    assert mapping.unresolved[0]["detail"] == "main.hlsl:99"


def test_parser_ignores_unreferenced_malformed_metadata(api):
    mapping = api.parse_dxil_source_mapping(
        single_location_disassembly("main.hlsl", "") +
        "!900 = !DIWeird(\n",
        make_sources(api, [pair("main.hlsl", ["a", "TARGET", "c"])]))

    assert len(mapping.lines) == 1
    assert mapping.lines[0].text == "TARGET"


def test_parser_accepts_single_line_define_bodies(api):
    disassembly = (
        "define void @main() { ret void, !dbg !10 }\n"
        "!10 = !DILocation(line: 2, column: 1, scope: !20)\n"
        "!20 = distinct !DISubprogram(name: \"main\", scope: !30, "
        "file: !40, line: 1, isLocal: false, isDefinition: true)\n"
        "!30 = !DICompileUnit(language: 1, file: !40)\n"
        "!40 = !DIFile(filename: \"main.hlsl\", directory: \"\")\n"
    )

    mapping = api.parse_dxil_source_mapping(
        disassembly,
        make_sources(api, [pair("main.hlsl", ["a", "SINGLE", "c"])]))

    assert [(item.line, item.text, item.instructions)
             for item in mapping.lines] == [(2, "SINGLE", 1)]


def test_multiline_switch_maps_its_instruction_and_ignores_comments_and_handles(api):
    text = single_location_disassembly("main.hlsl", "").replace(
        "  ret void, !dbg !10",
        "  %h = call %Handle @dx.op.createHandle(i32 57), !dbg !99\n"
        "  %v = add i32 1, 2 ; fake !dbg !99\n"
        "  switch i32 %v, label %done [\n    i32 1, label %done\n  ], !dbg !10\n"
        "done:\n  ret void")
    mapping = api.parse_dxil_source_mapping(
        text, make_sources(api, [pair("main.hlsl", ["a", "SWITCH", "c"])]))
    assert [(item.line, item.instructions) for item in mapping.lines] == [(2, 1)]
    assert mapping.counts["excluded_debug_instructions"] == 1


def test_truncated_function_is_rejected_instead_of_claiming_complete_mapping(api):
    text = single_location_disassembly("main.hlsl", "").replace("}\n", "")
    with pytest.raises(api.DxilSourceMappingError, match="Truncated"):
        api.parse_dxil_source_mapping(text, make_sources(api, [pair("main.hlsl", ["a", "b"])]))


def test_parser_rejects_missing_referenced_metadata(api):
    disassembly = (
        "define void @main() {\n"
        "  ret void, !dbg !999\n"
        "}\n"
    )

    with pytest.raises(api.DxilSourceMappingError, match=r"!999"):
        api.parse_dxil_source_mapping(
            disassembly,
            make_sources(api, [pair("main.hlsl", ["a"])]))


def test_renderer_emits_sorted_line_directives_and_archival_warning(api):
    mapping = api.parse_dxil_source_mapping(
        DISASSEMBLY, make_sources(api, [pair(SOURCE_NAME, SOURCE_LINES)]))

    text = api.render_mapped_hlsl(mapping, 123, "pixel")

    assert "NOT recompile-ready" in text
    assert '// Event 123, pixel stage, disassembly target "DXC DXIL".' in text
    assert '#line 12 "%s"' % SOURCE_NAME in text
    assert "\tfloat mapped2 = b;  " in text
    assert text.count('#line 12 "%s"' % SOURCE_NAME) == 1
    assert (text.index('#line 12 "') < text.index('#line 13 "')
            < text.index('#line 14 "') < text.index('#line 16 "'))
    for excluded in ("UNMAPPED_SOURCE_LINE", "UNREFERENCED_LOCATION_LINE",
                     "DBG_VALUE_LINE", "LIFETIME_LINE", "INVARIANT_LINE"):
        assert excluded not in text


class Pipe:
    def __init__(self, reflection, shader="shader-1"):
        self.reflection = reflection
        self.shader = shader

    def GetShader(self, stage):
        return self.shader

    def GetShaderReflection(self, stage):
        return self.reflection

    def GetGraphicsPipelineObject(self):
        return "graphics-pipeline"

    def GetComputePipelineObject(self):
        return "compute-pipeline"


class Controller:
    def __init__(self, reflection, disassembly, targets=("DXC DXIL",),
                 restore_event=7, fail_restore=False, shader="shader-1"):
        self.reflection = reflection
        self.disassembly = disassembly
        self.targets = targets
        self.restore_event = restore_event
        self.fail_restore = fail_restore
        self.shader = shader
        self.frame_events = []
        self.disassembly_calls = []

    def SetFrameEvent(self, event_id, force):
        if (self.fail_restore and self.frame_events
                and event_id == self.restore_event):
            raise RuntimeError("restore failed")
        self.frame_events.append((event_id, force))

    def GetPipelineState(self):
        return Pipe(self.reflection, self.shader)

    def GetDisassemblyTargets(self, with_pipeline):
        return list(self.targets)

    def DisassembleShader(self, pipeline, reflection, target):
        self.disassembly_calls.append((pipeline, reflection, target))
        return self.disassembly


def build_reflection(files=None):
    pairs = files if files is not None else [(SOURCE_NAME, SOURCE_TEXT)]
    return SimpleNamespace(
        resourceId="shader-1",
        entryPoint="main",
        encoding="DXIL",
        debugInfo=SimpleNamespace(
            files=[SimpleNamespace(filename=name, contents=contents)
                   for name, contents in pairs],
            encoding="HLSL",
            entrySourceName="MainPS",
            sourceDebugInformation=True,
        ),
    )


def make_ctx(valid_event=123, current_event=7):
    return SimpleNamespace(
        IsCaptureLoaded=lambda: True,
        CurEvent=lambda: current_event,
        GetAction=lambda event: (
            SimpleNamespace(eventId=event) if event == valid_event else None),
    )


def make_service(api, controller, ctx=None):
    calls = []

    def invoke(callback):
        calls.append(True)
        callback(controller)

    service = api.ShaderSourceExportService(ctx or make_ctx(), invoke)
    return service, calls


def test_export_writes_mapped_source_and_restores_event(api):
    controller = Controller(build_reflection(), DISASSEMBLY)
    service, calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "mapped.hlsl")
        result = service.export_shader_mapped_source(123, "pixel", output)
        with open(output, "r", encoding="utf-8") as stream:
            text = stream.read()

        assert text == api.render_mapped_hlsl(
            api.parse_dxil_source_mapping(
                DISASSEMBLY,
                [api.SourceFile(SOURCE_NAME, SOURCE_TEXT)]),
            123, "pixel")
        assert "UNMAPPED_SOURCE_LINE" not in text
        assert result["written"] is True
        assert result["output_path"] == output
        assert result["size_bytes"] == len(text.encode("utf-8"))
        assert result["event_id"] == 123
        assert result["stage"] == "pixel"
        assert result["disassembly_target"] == "DXC DXIL"
        assert result["shader"]["entry_point"] == "main"
        assert result["shader"]["source_entry_point"] == "MainPS"
        assert result["counts"]["mapped_source_lines"] == 4
        assert result["counts"]["mapped_instructions"] == 6
        assert result["counts"]["disassembly_lines"] == len(
            DISASSEMBLY.splitlines())
        assert result["source_files"] == [{
            "filename": SOURCE_NAME,
            "mapped_lines": 4,
            "mapped_instructions": 6,
        }]
        assert result["unresolved"] == []
        assert result["provenance"]["disassembly_target"] == "DXC DXIL"
        assert "NOT recompile-ready" in result["warning"]

    assert calls == [True]
    assert controller.frame_events == [(123, True), (7, True)]
    assert controller.disassembly_calls == [
        ("graphics-pipeline", controller.reflection, "DXC DXIL")]


def test_compute_stage_uses_compute_pipeline(api):
    controller = Controller(build_reflection(), DISASSEMBLY)
    service, _calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        service.export_shader_mapped_source(
            123, "compute", os.path.join(directory, "compute.hlsl"))

    assert controller.disassembly_calls[0][0] == "compute-pipeline"
    assert controller.disassembly_calls[0][2] == "DXC DXIL"


def test_missing_dxc_dxil_target_is_unsupported_and_restores(api):
    controller = Controller(
        build_reflection(), DISASSEMBLY, targets=("DXBC/DXIL",))
    service, _calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "mapped.hlsl")
        with pytest.raises(ValueError, match="DXC DXIL"):
            service.export_shader_mapped_source(123, "pixel", output)
        assert not os.path.exists(output)

    assert controller.frame_events == [(123, True), (7, True)]
    assert controller.disassembly_calls == []


def test_missing_debug_locations_is_unsupported_and_restores(api):
    disassembly = "define void @main() {\n  ret void\n}\n"
    controller = Controller(build_reflection(), disassembly)
    service, _calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "mapped.hlsl")
        with pytest.raises(ValueError, match="debug locations"):
            service.export_shader_mapped_source(123, "pixel", output)
        assert not os.path.exists(output)

    assert controller.frame_events == [(123, True), (7, True)]


def test_missing_referenced_metadata_is_unsupported(api):
    disassembly = (
        "define void @main() {\n"
        "  ret void, !dbg !999\n"
        "}\n"
    )
    controller = Controller(build_reflection(), disassembly)
    service, _calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "mapped.hlsl")
        with pytest.raises(ValueError, match="missing metadata"):
            service.export_shader_mapped_source(123, "pixel", output)
        assert not os.path.exists(output)


def test_restore_failure_is_reported_before_output_is_written(api):
    controller = Controller(
        build_reflection(), DISASSEMBLY, fail_restore=True)
    service, _calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "mapped.hlsl")
        with pytest.raises(ValueError, match="restore"):
            service.export_shader_mapped_source(123, "pixel", output)
        assert not os.path.exists(output)

    assert controller.frame_events == [(123, True)]


def test_existing_output_is_never_overwritten(api):
    controller = Controller(build_reflection(), DISASSEMBLY)
    service, calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "keep.hlsl")
        with open(output, "wb") as stream:
            stream.write(b"keep")
        with pytest.raises(ValueError, match="already exists"):
            service.export_shader_mapped_source(123, "pixel", output)
        with open(output, "rb") as stream:
            assert stream.read() == b"keep"

    assert calls == []
    assert controller.frame_events == []


def test_event_stage_and_output_path_validation(api):
    controller = Controller(build_reflection(), DISASSEMBLY)
    service, calls = make_service(api, controller)

    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="not an action"):
            service.export_shader_mapped_source(
                999, "pixel", os.path.join(directory, "a.hlsl"))
        with pytest.raises(ValueError, match="Unknown shader stage"):
            service.export_shader_mapped_source(
                123, "mesh", os.path.join(directory, "b.hlsl"))
        with pytest.raises(ValueError, match="absolute"):
            service.export_shader_mapped_source(123, "pixel", "relative.hlsl")
        with pytest.raises(ValueError, match=r"end in \.hlsl"):
            service.export_shader_mapped_source(
                123, "pixel", os.path.join(directory, "c.txt"))
        with pytest.raises(ValueError, match="parent directory"):
            service.export_shader_mapped_source(
                123, "pixel", os.path.join(directory, "missing", "d.hlsl"))

    assert calls == []
    assert controller.frame_events == []


def test_requires_loaded_capture(api):
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: False, CurEvent=lambda: 0)
    service = api.ShaderSourceExportService(ctx, lambda callback: None)

    with pytest.raises(ValueError, match="No capture loaded"):
        service.export_shader_mapped_source(123, "pixel", r"C:\a.hlsl")
