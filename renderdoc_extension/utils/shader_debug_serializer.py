"""RenderDoc shader debug serialization helpers.

Field names and enum values were checked against RenderDoc 1.45
(``renderdoc/api/replay/shader_types.h`` and ``replay_enums.h``) and remain
valid for the local 1.48 source.  This module stays Python 3.6 compatible
because RenderDoc's embedded interpreter is Python 3.6.
"""

import json
import math


VAR_TYPE_NAMES = (
    "Float", "Double", "Half", "SInt", "UInt", "SShort", "UShort", "SLong",
    "ULong", "SByte", "UByte", "Bool", "Enum", "Struct", "GPUPointer",
    "ConstantBlock", "ReadOnlyResource", "ReadWriteResource", "Sampler",
)
DEBUG_VARIABLE_TYPE_NAMES = (
    "Undefined", "Input", "Constant", "Sampler", "ReadOnlyResource",
    "ReadWriteResource", "Variable",
)
RESOURCE_TYPES = frozenset((
    "ConstantBlock", "ReadOnlyResource", "ReadWriteResource", "Sampler",
))
NUMERIC_ARRAYS = {
    "Float": ("f32v", "float"), "Double": ("f64v", "float"),
    "Half": ("f16v", "half"), "SInt": ("s32v", "int"),
    "UInt": ("u32v", "int"), "SShort": ("s16v", "int"),
    "UShort": ("u16v", "int"), "SLong": ("s64v", "int"),
    "ULong": ("u64v", "int"), "SByte": ("s8v", "int"),
    "UByte": ("u8v", "int"), "Bool": ("u32v", "int"),
    "Enum": ("u32v", "int"),
}
SHADER_EVENT_FLAGS = (
    (0x1, "SampleLoadGather"), (0x2, "GeneratedNanOrInf"), (0x4, "DebugBreak"),
)
SHADER_VARIABLE_FLAGS = (
    (0x0001, "RowMajorMatrix"), (0x0002, "HexDisplay"),
    (0x0004, "BinaryDisplay"), (0x0008, "RGBDisplay"),
    (0x0010, "R11G11B10"), (0x0020, "R10G10B10A2"), (0x0040, "UNorm"),
    (0x0080, "SNorm"), (0x0100, "Truncated"), (0x0200, "SignedEnum"),
    (0x0400, "SingleElementArray"),
)
SIGNED_ENUM_FLAG = 0x0200


def _to_int(value):
    if value is None:
        return 0
    return value if isinstance(value, int) else int(value)


def _type_name(value):
    if value is None:
        return "Unknown"
    if isinstance(value, int):
        return VAR_TYPE_NAMES[value] if 0 <= value < len(VAR_TYPE_NAMES) else "Unknown"
    text = str(value)
    return text.rsplit(".", 1)[1] if "." in text else text


def _debug_reference_type(value):
    if isinstance(value, int):
        return DEBUG_VARIABLE_TYPE_NAMES[value] if 0 <= value < len(DEBUG_VARIABLE_TYPE_NAMES) else "Unknown"
    return str(value).rsplit(".", 1)[-1]


def _flag_names(value, definitions):
    bits = _to_int(value)
    return [name for mask, name in definitions if bits & mask]


def _read_array(container, attribute, count):
    values = getattr(container, attribute, None)
    return [] if values is None else [values[i] for i in range(min(count, len(values)))]


def _element_count(variable):
    rows = _to_int(getattr(variable, "rows", 0))
    columns = _to_int(getattr(variable, "columns", 0))
    return rows * columns if rows > 0 and columns > 0 else 1


def _half_to_float(bits):
    sign = -1.0 if bits & 0x8000 else 1.0
    exponent = (bits >> 10) & 0x1F
    fraction = bits & 0x03FF
    if exponent == 0:
        return sign * ((fraction / 1024.0) * (2.0 ** -14)) if fraction else sign * 0.0
    if exponent == 31:
        return sign * float("inf") if fraction == 0 else float("nan")
    return sign * (1.0 + fraction / 1024.0) * (2.0 ** (exponent - 15))


def _sanitize_float(value):
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _numbers(container, attribute, count, kind):
    result = []
    for raw in _read_array(container, attribute, count):
        if kind == "half":
            raw = _half_to_float(_to_int(raw)) if isinstance(raw, int) else raw
            result.append(_sanitize_float(float(raw)))
        elif kind == "float":
            result.append(_sanitize_float(raw))
        else:
            number = int(raw)
            result.append(str(number) if kind == "int64" else number)
    return result


def is_absent_shader_variable(variable):
    """Return True for RenderDoc's default/uninitialised ShaderVariable."""
    if variable is None:
        return True
    if getattr(variable, "name", ""):
        return False
    if _type_name(getattr(variable, "type", None)) != "Unknown":
        return False
    return (
        _to_int(getattr(variable, "rows", 0)) == 0
        and _to_int(getattr(variable, "columns", 0)) == 0
        and not bool(getattr(variable, "members", None))
    )


def serialize_shader_variable(variable):
    """Serialize one ShaderVariable recursively, or return None if absent."""
    if is_absent_shader_variable(variable):
        return None
    type_name = _type_name(variable.type)
    flags = _to_int(getattr(variable, "flags", 0))
    result = {
        "name": variable.name,
        "type": type_name,
        "rows": _to_int(variable.rows),
        "columns": _to_int(variable.columns),
        "flags": _flag_names(flags, SHADER_VARIABLE_FLAGS),
        "flags_value": flags,
    }
    if type_name == "GPUPointer":
        raw = [str(int(value)) for value in _read_array(variable.value, "u64v", 4)]
        result["pointer"] = {
            "value": raw[0] if len(raw) > 0 else None,
            "type_id": raw[1] if len(raw) > 1 else None,
            "shader_id": raw[2] if len(raw) > 2 else None,
            "raw": raw,
        }
    elif type_name in RESOURCE_TYPES:
        raw = _read_array(variable.value, "u32v", 4)
        result["binding"] = {
            "category": raw[0] if len(raw) > 0 else None,
            "index": raw[1] if len(raw) > 1 else None,
            "array_element": raw[2] if len(raw) > 2 else None,
            "is_direct_access": len(raw) > 3 and raw[3] == 1,
            "raw": raw,
        }
        if len(raw) > 3 and raw[3] == 1:
            wide = _read_array(variable.value, "u64v", 3)
            result["direct_access"] = {
                "descriptor_type": raw[0] if len(raw) > 0 else None,
                "byte_offset": raw[1] if len(raw) > 1 else None,
                "byte_size": raw[2] if len(raw) > 2 else None,
                "descriptor_store": str(int(wide[2])) if len(wide) > 2 else None,
            }
    elif type_name in NUMERIC_ARRAYS:
        attribute, kind = NUMERIC_ARRAYS[type_name]
        if type_name == "Enum" and flags & SIGNED_ENUM_FLAG:
            attribute = "s32v"
        if type_name in ("SLong", "ULong"):
            kind = "int64"
        result["value"] = _numbers(variable.value, attribute, _element_count(variable), kind)
    members = getattr(variable, "members", None) or []
    if members:
        result["members"] = [serialize_shader_variable(member) for member in members]
    return result


def serialize_shader_variables(variables):
    return [serialize_shader_variable(variable) for variable in (variables or [])]


def serialize_source_variable_mapping(mapping):
    references = getattr(mapping, "variables", None) or []
    return {
        "name": mapping.name,
        "type": _type_name(mapping.type),
        "undefined_value": bool(mapping.undefinedValue),
        "rows": _to_int(mapping.rows),
        "columns": _to_int(mapping.columns),
        "offset": _to_int(mapping.offset),
        "signature_index": _to_int(mapping.signatureIndex),
        "variables": [
            {"name": ref.name, "type": _debug_reference_type(ref.type),
             "component": _to_int(ref.component)}
            for ref in references
        ],
    }


def serialize_source_variable_mappings(mappings):
    return [serialize_source_variable_mapping(mapping) for mapping in (mappings or [])]


def _source_lines(source_files, file_index):
    if file_index < 0 or file_index >= len(source_files or []):
        return None, None
    source = source_files[file_index]
    contents = getattr(source, "contents", None)
    if contents is None:
        return getattr(source, "filename", None), None
    return getattr(source, "filename", None), contents.splitlines()


def _line_at(lines, line_number):
    index = _to_int(line_number)
    if lines is None or index < 1 or index > len(lines):
        return None
    return lines[index - 1]


def serialize_instruction_source_info(info, source_files=None, disassembly=None):
    line = info.lineInfo
    file_index = _to_int(line.fileIndex)
    line_start = _to_int(line.lineStart)
    line_end = _to_int(line.lineEnd)
    disassembly_line = _to_int(line.disassemblyLine)
    filename, source_lines = _source_lines(source_files, file_index)
    available = bool(source_lines)
    disassembly_lines = disassembly.splitlines() if isinstance(disassembly, str) else (disassembly or [])
    source_text = None
    if available and line_start >= 1 and line_end >= line_start:
        source_text = "\n".join(source_lines[line_start - 1:line_end])
    return {
        "instruction": _to_int(info.instruction),
        "line_info": {
            "disassembly_line": disassembly_line,
            "file_index": file_index,
            "line_start": line_start,
            "line_end": line_end,
            "col_start": _to_int(line.colStart),
            "col_end": _to_int(line.colEnd),
            "source_available": available,
            "source_file": filename,
            "source_line": _line_at(source_lines, line_start),
            "source_text": source_text,
            "disassembly_line_text": _line_at(disassembly_lines, disassembly_line),
        },
        "source_vars": serialize_source_variable_mappings(getattr(info, "sourceVars", None)),
    }


def serialize_instruction_source_infos(instruction_infos, source_files=None, disassembly=None):
    return [serialize_instruction_source_info(info, source_files, disassembly)
            for info in (instruction_infos or [])]


def serialize_debug_variable_change(change):
    before_present = not is_absent_shader_variable(change.before)
    after_present = not is_absent_shader_variable(change.after)
    change_type = ("modified" if before_present and after_present else
                   "created" if after_present else
                   "destroyed" if before_present else "none")
    return {
        "before": serialize_shader_variable(change.before),
        "after": serialize_shader_variable(change.after),
        "before_present": before_present,
        "after_present": after_present,
        "change_type": change_type,
    }


def serialize_debug_state(state):
    flags = _to_int(getattr(state, "flags", 0))
    return {
        "step_index": _to_int(state.stepIndex),
        "next_instruction": _to_int(state.nextInstruction),
        "flags": _flag_names(flags, SHADER_EVENT_FLAGS),
        "flags_value": flags,
        "callstack": list(getattr(state, "callstack", None) or []),
        "changes": [serialize_debug_variable_change(change) for change in state.changes or []],
    }


def serialize_debug_states(states):
    return [serialize_debug_state(state) for state in (states or [])]


def sanitize_for_json(value):
    """Recursively replace non-finite floats with JSON-safe string tokens."""
    if isinstance(value, float):
        return _sanitize_float(value)
    if isinstance(value, dict):
        return dict((key, sanitize_for_json(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(item) for item in value]
    return value


def json_dumps(value, **kwargs):
    kwargs["allow_nan"] = False
    return json.dumps(sanitize_for_json(value), **kwargs)
