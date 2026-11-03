import importlib.util
import json
import math
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "renderdoc_extension"
    / "utils"
    / "shader_debug_serializer.py"
)
SPEC = importlib.util.spec_from_file_location("shader_debug_serializer", MODULE_PATH)
SERIALIZER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERIALIZER)


def value(f32v=None, f64v=None, f16v=None, s32v=None, u32v=None,
          s16v=None, u16v=None, s64v=None, u64v=None, s8v=None, u8v=None):
    return SimpleNamespace(
        f32v=f32v or [0.0] * 16,
        f64v=f64v or [0.0] * 16,
        f16v=f16v or [0] * 16,
        s32v=s32v or [0] * 16,
        u32v=u32v or [0] * 16,
        s16v=s16v or [0] * 16,
        u16v=u16v or [0] * 16,
        s64v=s64v or [0] * 16,
        u64v=u64v or [0] * 16,
        s8v=s8v or [0] * 16,
        u8v=u8v or [0] * 16,
    )


def variable(name, type_name, rows=1, columns=1, flags=0, data=None, members=None):
    return SimpleNamespace(
        name=name,
        type="VarType." + type_name,
        rows=rows,
        columns=columns,
        flags=flags,
        value=data or value(),
        members=members or [],
    )


def absent_variable():
    return SimpleNamespace(
        name="", type="VarType.Unknown", rows=0, columns=0, flags=0,
        value=value(), members=[],
    )


def line_info(disassembly_line=0, file_index=-1, line_start=0, line_end=0,
              col_start=0, col_end=0):
    return SimpleNamespace(
        disassemblyLine=disassembly_line,
        fileIndex=file_index,
        lineStart=line_start,
        lineEnd=line_end,
        colStart=col_start,
        colEnd=col_end,
    )


def source_mapping(name="value", type_name="Float"):
    return SimpleNamespace(
        name=name,
        type="VarType." + type_name,
        undefinedValue=False,
        rows=1,
        columns=2,
        offset=0,
        signatureIndex=3,
        variables=[
            SimpleNamespace(name=name, type="VarType." + type_name, component=0),
            SimpleNamespace(name=name, type="VarType." + type_name, component=1),
        ],
    )


def test_float_variable_uses_float_array_sanitizes_non_finite_and_recurses():
    member = variable("member", "SInt", columns=2, data=value(s32v=[-1, 7, 0, 0]))
    item = variable(
        "tint", "Float", columns=4, flags=0x0003,
        data=value(f32v=[1.0, float("inf"), float("nan"), float("-inf")]),
        members=[member],
    )

    result = SERIALIZER.serialize_shader_variable(item)

    assert result["type"] == "Float"
    assert result["rows"] == 1
    assert result["columns"] == 4
    assert result["value"] == [1.0, "Infinity", "NaN", "-Infinity"]
    assert result["flags"] == ["RowMajorMatrix", "HexDisplay"]
    assert result["members"][0]["name"] == "member"
    assert result["members"][0]["value"] == [-1, 7]


def test_each_numeric_type_reads_its_own_renderdoc_array():
    double = variable("d", "Double", columns=2, data=value(f64v=[1.5, float("-inf")]))
    half = variable(
        "h", "Half", columns=4,
        data=value(f16v=[0x3C00, 0x7C00, 0xFC00, 0x7E00]),
    )
    uint = variable("u", "UInt", data=value(f32v=[99.0], u32v=[42]))
    enum = variable(
        "e", "Enum", flags=0x0200, data=value(s32v=[-3], u32v=[4294967293])
    )
    short = variable("s", "SShort", data=value(s16v=[-7]))

    assert SERIALIZER.serialize_shader_variable(double)["value"] == [1.5, "-Infinity"]
    assert SERIALIZER.serialize_shader_variable(half)["value"] == [
        1.0, "Infinity", "-Infinity", "NaN",
    ]
    assert SERIALIZER.serialize_shader_variable(uint)["value"] == [42]
    assert SERIALIZER.serialize_shader_variable(enum)["value"] == [-3]
    assert SERIALIZER.serialize_shader_variable(short)["value"] == [-7]


def test_64_bit_values_preserve_exact_json_precision():
    item = variable("large", "ULong", data=value(u64v=[18446744073709551615]))

    result = json.loads(SERIALIZER.json_dumps(SERIALIZER.serialize_shader_variable(item)))

    assert result["value"] == ["18446744073709551615"]


def test_resource_and_pointer_data_stays_integer():
    bound = variable(
        "texture", "ReadOnlyResource",
        data=value(u32v=[2, 5, 1, 0]),
    )
    direct = variable(
        "heap", "ConstantBlock",
        data=value(u32v=[4, 16, 32, 1], u64v=[0, 0, 99]),
    )
    pointer = variable(
        "ptr", "GPUPointer", data=value(u64v=[0x1234, 7, 42])
    )

    bound_result = SERIALIZER.serialize_shader_variable(bound)
    direct_result = SERIALIZER.serialize_shader_variable(direct)
    pointer_result = SERIALIZER.serialize_shader_variable(pointer)

    assert "value" not in bound_result
    assert bound_result["binding"] == {
        "category": 2, "index": 5, "array_element": 1,
        "is_direct_access": False, "raw": [2, 5, 1, 0],
    }
    assert direct_result["binding"]["is_direct_access"] is True
    assert direct_result["direct_access"] == {
        "descriptor_type": 4, "byte_offset": 16, "byte_size": 32,
        "descriptor_store": "99",
    }
    assert pointer_result["pointer"] == {
        "value": "4660", "type_id": "7", "shader_id": "42", "raw": ["4660", "7", "42"],
    }
    assert "value" not in pointer_result


def test_absent_variable_is_distinguished_from_a_name_only_variable():
    absent = absent_variable()
    named = SimpleNamespace(
        name="x", type="VarType.Unknown", rows=1, columns=1, flags=0,
        value=value(), members=[],
    )

    assert SERIALIZER.is_absent_shader_variable(None) is True
    assert SERIALIZER.is_absent_shader_variable(absent) is True
    assert SERIALIZER.is_absent_shader_variable(named) is False
    assert SERIALIZER.serialize_shader_variable(absent) is None
    assert SERIALIZER.serialize_shader_variables([absent, named]) == [
        None, {"name": "x", "type": "Unknown", "rows": 1, "columns": 1,
               "flags": [], "flags_value": 0},
    ]


def test_debug_state_preserves_before_after_flags_callstack_and_change_kind():
    created = variable("created", "Float", data=value(f32v=[1.0]))
    destroyed = variable("destroyed", "Float", data=value(f32v=[2.0]))
    changed_before = variable("counter", "SInt", data=value(s32v=[-1]))
    changed_after = variable("counter", "SInt", data=value(s32v=[2]))
    state = SimpleNamespace(
        stepIndex=9,
        nextInstruction=11,
        flags=0x5,
        callstack=["main", "helper"],
        changes=[
            SimpleNamespace(before=absent_variable(), after=created),
            SimpleNamespace(before=destroyed, after=absent_variable()),
            SimpleNamespace(before=changed_before, after=changed_after),
        ],
    )

    result = SERIALIZER.serialize_debug_state(state)

    assert result["step_index"] == 9
    assert result["next_instruction"] == 11
    assert result["flags"] == ["SampleLoadGather", "DebugBreak"]
    assert result["flags_value"] == 5
    assert result["callstack"] == ["main", "helper"]
    assert [change["change_type"] for change in result["changes"]] == [
        "created", "destroyed", "modified",
    ]
    assert result["changes"][0]["before"] is None
    assert result["changes"][0]["before_present"] is False
    assert result["changes"][0]["after_present"] is True
    assert result["changes"][1]["after"] is None
    assert result["changes"][2]["before"]["value"] == [-1]
    assert result["changes"][2]["after"]["value"] == [2]


def test_instruction_info_maps_file_line_and_disassembly_text():
    info = SimpleNamespace(
        instruction=4,
        lineInfo=line_info(
            disassembly_line=2, file_index=0, line_start=2, line_end=3,
            col_start=5, col_end=9,
        ),
        sourceVars=[source_mapping("UV", "Float")],
    )
    files = [
        SimpleNamespace(filename="shader.hlsl", contents="line one\nline two\nline three\n")
    ]

    result = SERIALIZER.serialize_instruction_source_info(
        info, files, "op zero\nop one\nop two"
    )

    assert result["instruction"] == 4
    assert result["line_info"] == {
        "disassembly_line": 2,
        "file_index": 0,
        "line_start": 2,
        "line_end": 3,
        "col_start": 5,
        "col_end": 9,
        "source_available": True,
        "source_file": "shader.hlsl",
        "source_line": "line two",
        "source_text": "line two\nline three",
        "disassembly_line_text": "op one",
    }
    assert result["source_vars"][0]["name"] == "UV"
    assert result["source_vars"][0]["signature_index"] == 3
    assert result["source_vars"][0]["variables"][1]["component"] == 1


def test_source_variable_reference_uses_debug_variable_type_enum():
    class DebugVariableType(IntEnum):
        Input = 1
        Constant = 2
        Variable = 6

    mapping = source_mapping()
    mapping.variables = [
        SimpleNamespace(name="input", type=DebugVariableType.Input, component=0),
        SimpleNamespace(name="constant", type=DebugVariableType.Constant, component=1),
        SimpleNamespace(name="local", type=DebugVariableType.Variable, component=2),
    ]

    result = SERIALIZER.serialize_source_variable_mapping(mapping)

    assert [ref["type"] for ref in result["variables"]] == ["Input", "Constant", "Variable"]


def test_instruction_info_keeps_disassembly_when_source_is_unavailable():
    info = SimpleNamespace(
        instruction=1,
        lineInfo=line_info(disassembly_line=1, file_index=7, line_start=1),
        sourceVars=[],
    )

    result = SERIALIZER.serialize_instruction_source_info(
        info, [], "op zero\nop one"
    )

    assert result["line_info"]["source_available"] is False
    assert result["line_info"]["source_file"] is None
    assert result["line_info"]["source_line"] is None
    assert result["line_info"]["source_text"] is None
    assert result["line_info"]["disassembly_line"] == 1
    assert result["line_info"]["disassembly_line_text"] == "op zero"


def test_list_helpers_preserve_every_debug_trace_collection():
    trace = SimpleNamespace(
        stage="ShaderStage.Pixel",
        inputs=[variable("in", "Float", data=value(f32v=[0.5]))],
        constantBlocks=[variable("cb", "Struct", members=[variable("x", "SInt", data=value(s32v=[1]))])],
        readOnlyResources=[variable("tex", "ReadOnlyResource", data=value(u32v=[0, 0, 0, 0]))],
        readWriteResources=[],
        samplers=[variable("smp", "Sampler", data=value(u32v=[1, 2, 0, 0]))],
        sourceVars=[source_mapping()],
        debugger=object(),
        instInfo=[SimpleNamespace(
            instruction=2,
            lineInfo=line_info(disassembly_line=1, file_index=0, line_start=1),
            sourceVars=[source_mapping()],
        )],
    )

    inputs = SERIALIZER.serialize_shader_variables(trace.inputs)
    constants = SERIALIZER.serialize_shader_variables(trace.constantBlocks)
    read_only = SERIALIZER.serialize_shader_variables(trace.readOnlyResources)
    read_write = SERIALIZER.serialize_shader_variables(trace.readWriteResources)
    samplers = SERIALIZER.serialize_shader_variables(trace.samplers)
    source_vars = SERIALIZER.serialize_source_variable_mappings(trace.sourceVars)
    instructions = SERIALIZER.serialize_instruction_source_infos(
        trace.instInfo, [SimpleNamespace(filename="a.hlsl", contents="code")], "instruction"
    )

    assert len(inputs) == 1
    assert constants[0]["members"][0]["value"] == [1]
    assert len(read_only) == 1
    assert read_write == []
    assert len(samplers) == 1
    assert source_vars[0]["name"] == "value"
    assert trace.debugger is not None
    assert instructions[0]["line_info"]["source_line"] == "code"
    assert instructions[0]["line_info"]["disassembly_line_text"] == "instruction"


def test_json_dumps_is_strict_after_sanitizing_non_finite_values():
    item = variable(
        "special", "Float", columns=3,
        data=value(f32v=[float("nan"), float("inf"), float("-inf")]),
    )

    serialized = SERIALIZER.serialize_shader_variable(item)
    encoded = SERIALIZER.json_dumps(serialized, sort_keys=True)
    decoded = json.loads(encoded)

    assert decoded["value"] == ["NaN", "Infinity", "-Infinity"]
    strict = json.dumps(SERIALIZER.sanitize_for_json(serialized), allow_nan=False)
    assert json.loads(strict)["value"] == ["NaN", "Infinity", "-Infinity"]
    assert SERIALIZER.sanitize_for_json({"x": [math.inf]}) == {"x": ["Infinity"]}
