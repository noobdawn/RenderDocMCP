"""Export HLSL source lines referenced by retained DXC DXIL instructions.

RenderDoc's embedded interpreter is Python 3.6, so this module only uses the
standard library and Python 3.6 syntax.  No shader debugger invocation is used;
only the DXC DXIL disassembly and its ``!dbg !DILocation`` metadata are parsed.
"""

# noqa: SIZE_OK - one cohesive service: DXIL metadata parsing plus safe export.

import os
import re
from collections import namedtuple
from .shader_selected_uniforms import collect_selected_uniforms, render_selected_uniforms

_DXC_DXIL_TARGET = "DXC DXIL"
_SHADER_STAGES = ("vertex", "hull", "domain", "geometry", "pixel", "compute")
_ARCHIVAL_WARNING = (
    "Archival excerpt only; NOT recompile-ready. Only source lines referenced "
    "by retained executable DXC DXIL instructions are included; declarations, "
    "includes, control flow and unreferenced branches may be absent."
)
_WARNING_COMMENT = "// WARNING: " + _ARCHIVAL_WARNING

SourceFile = namedtuple("SourceFile", "filename contents")
MappedLine = namedtuple("MappedLine", "filename line text instructions")
DxilMapping = namedtuple(
    "DxilMapping",
    "lines counts files unresolved unreferenced_debug_locations",
)
DIFileRef = namedtuple("DIFileRef", "filename path")


class DxilSourceMappingError(ValueError):
    """Precise unsupported or failed DXIL source mapping.

    Raised when the DXC DXIL target is unavailable, the disassembly is not
    LLVM IR with define bodies, a referenced ``!dbg`` metadata entry is missing
    or malformed, or no source line can be mapped without guessing.
    """


_META_DEF = re.compile(r"^\s*!(\d+)\s*=\s*(.*?)\s*$")
_META_KIND = re.compile(r"^!([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$", re.DOTALL)
_META_KIND_PREFIX = re.compile(r"^(?:distinct\s+)?!([A-Za-z][A-Za-z0-9_]*)\s*\(")
_DBG_ATTACHMENT = re.compile(r"(?<![A-Za-z0-9_])!dbg\s+!(\d+)")
_LABEL = re.compile(r"^[A-Za-z$._][A-Za-z0-9$._-]*:\s*(?:;.*)?$")
_INLINE_BODY = re.compile(r"\{([^{}]*)\}\s*$")
_QUOTED = r'"(?:\\.|[^"\\])*"'
_EXCLUDED_INTRINSIC = re.compile(r"@llvm\.(?:dbg\.|lifetime\.|invariant)")
_HANDLE_INTRINSIC = re.compile(r"@dx\.op\.(?:createHandle(?:FromBinding|FromHeap)?|annotateHandle)(?:\.|\()")
_SCOPE_TYPES = frozenset((
    "DILocation", "DILexicalBlock", "DILexicalBlockFile", "DISubprogram",
    "DICompileUnit", "DINamespace",
))


def _renderdoc():
    import renderdoc

    return renderdoc


def _clip(value, limit=160):
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _brace_delta(text):
    delta = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            delta += 1
        elif char == "}":
            delta -= 1
    return delta


def _mask_strings(text):
    result = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                result.append('"')
            else:
                result.append(" ")
        else:
            if char == ";":
                break
            result.append(char)
            if char == '"':
                in_string = True
    return "".join(result)


def _unescape_llvm_string(literal):
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        raise DxilSourceMappingError(
            "Malformed DXIL string literal: %s" % _clip(literal))
    raw = literal[1:-1]
    data = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\":
            data.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(raw):
            raise DxilSourceMappingError(
                "Malformed DXIL string escape: %s" % _clip(literal))
        escaped = raw[index]
        if escaped in ('"', "\\"):
            data.append(ord(escaped))
            index += 1
        elif escaped in ("n", "t", "r"):
            data.append({"n": 10, "t": 9, "r": 13}[escaped])
            index += 1
        else:
            pair = raw[index:index + 2]
            if (len(pair) != 2 or
                    any(char not in "0123456789abcdefABCDEF" for char in pair)):
                raise DxilSourceMappingError(
                    "Malformed DXIL string escape: %s" % _clip(literal))
            data.append(int(pair, 16))
            index += 2
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _quoted_field(body, key):
    prefix = r"(?<![A-Za-z0-9_])" + re.escape(key) + r":\s*"
    if re.search(prefix, body) is None:
        return None
    match = re.search(prefix + "(" + _QUOTED + ")", body)
    if match is None:
        raise DxilSourceMappingError(
            "Malformed %s string in DXIL metadata" % key)
    return _unescape_llvm_string(match.group(1))


def _metadata_int(body, key):
    match = re.search(
        r"(?<![A-Za-z0-9_])" + re.escape(key) + r":\s*(\d+)", body)
    return int(match.group(1)) if match is not None else None


def _metadata_ref(body, key):
    match = re.search(
        r"(?<![A-Za-z0-9_])" + re.escape(key) + r":\s*!(\d+)", body)
    return int(match.group(1)) if match is not None else None


def _metadata_definition(rhs):
    text = rhs.strip()
    if text.startswith("distinct "):
        text = text[9:].lstrip()
    match = _META_KIND.match(text)
    if match is None:
        raise DxilSourceMappingError(
            "Unsupported DXIL metadata definition: %s" % _clip(rhs))
    return match.group(1), match.group(2)


def _is_absolute_path(value):
    if os.path.isabs(value):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in "\\/"


def _join_path(directory, filename):
    if not directory:
        return filename
    if directory.endswith(("/", "\\")):
        return directory + filename
    return directory + os.sep + filename


def _norm_path(value):
    return os.path.normcase(os.path.normpath(value.replace("\\", "/")))


def _basename(value):
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _difile_ref(body):
    filename = _quoted_field(body, "filename")
    directory = _quoted_field(body, "directory")
    if not filename:
        return None
    if _is_absolute_path(filename):
        return DIFileRef(filename, filename)
    return DIFileRef(filename, _join_path(directory, filename))


class _SourceIndex:
    def __init__(self, files):
        self.files = [source for source in files if source.filename]
        self.exact = {}
        self.normalized = {}
        self.basenames = {}
        self._lines = {}
        for source in self.files:
            self.exact.setdefault(source.filename, []).append(source)
            self.normalized.setdefault(
                _norm_path(source.filename), []).append(source)
            self.basenames.setdefault(
                _norm_path(_basename(source.filename)), []).append(source)

    def lines_for(self, source):
        key = id(source)
        if key not in self._lines:
            self._lines[key] = source.contents.splitlines()
        return self._lines[key]


def _unique_names(files):
    names = []
    for source in files:
        if source.filename not in names:
            names.append(source.filename)
    return names


def _match_source(index, reference):
    if reference is None or not reference.filename:
        return None, "missing_source_filename", []
    exact_ambiguous = None
    exact_values = []
    for value in (reference.filename, reference.path):
        if value and value not in exact_values:
            exact_values.append(value)
    for value in exact_values:
        files = index.exact.get(value)
        if files:
            if len(files) == 1:
                return files[0], None, []
            if exact_ambiguous is None:
                exact_ambiguous = files
    normalized_ambiguous = None
    normalized_values = []
    for value in exact_values:
        key = _norm_path(value)
        if key not in normalized_values:
            normalized_values.append(key)
    for key in normalized_values:
        files = index.normalized.get(key)
        if files:
            if len(files) == 1:
                return files[0], None, []
            if normalized_ambiguous is None:
                normalized_ambiguous = files
    if exact_ambiguous is not None:
        return None, "ambiguous_source_file", _unique_names(exact_ambiguous)
    if normalized_ambiguous is not None:
        return None, "ambiguous_source_file", _unique_names(normalized_ambiguous)
    files = index.basenames.get(_norm_path(_basename(reference.filename)))
    if files:
        if len(files) == 1:
            return files[0], None, []
        return None, "ambiguous_source_file", _unique_names(files)
    return None, "missing_source_file", []


def _resolve_file(metadata, meta_id, seen):
    if meta_id in seen:
        raise DxilSourceMappingError(
            "Cyclic DI scope while resolving DXIL metadata !%d" % meta_id)
    if meta_id not in metadata:
        raise DxilSourceMappingError(
            "Referenced DXIL metadata !%d is missing" % meta_id)
    kind, body = _metadata_definition(metadata[meta_id])
    if kind == "DIFile":
        return _difile_ref(body)
    file_id = _metadata_ref(body, "file")
    if file_id is not None:
        return _resolve_file(metadata, file_id, seen | {meta_id})
    if kind in _SCOPE_TYPES:
        scope_id = _metadata_ref(body, "scope")
        if scope_id is not None:
            return _resolve_file(metadata, scope_id, seen | {meta_id})
        return None
    raise DxilSourceMappingError(
        "Unsupported DI scope metadata !%s while resolving a DILocation file"
        % kind)


def _resolve_location(metadata, location_id):
    kind, body = _metadata_definition(metadata[location_id])
    if kind != "DILocation":
        raise DxilSourceMappingError(
            "Instruction !dbg references !%s, not !DILocation" % kind)
    line = _metadata_int(body, "line")
    if line is None or line < 1:
        return None, "missing_line"
    scope_id = _metadata_ref(body, "scope")
    if scope_id is None:
        return None, "missing_difile"
    reference = _resolve_file(metadata, scope_id, frozenset())
    if reference is None:
        return None, "missing_difile"
    return (reference, line), None


def _scan_instructions(disassembly):
    metadata = {}
    for line in disassembly.splitlines():
        match = _META_DEF.match(line)
        if match is not None:
            meta_id, value = int(match.group(1)), match.group(2).strip()
            if meta_id in metadata and metadata[meta_id] != value:
                raise DxilSourceMappingError("Conflicting DXIL metadata !%d" % meta_id)
            metadata[meta_id] = value

    retained = []
    stats = {
        "define_functions": 0,
        "executable_instructions": 0,
        "instructions_with_debug_location": 0,
        "excluded_debug_instructions": 0,
    }

    def record(text):
        stripped = text.strip()
        if (not stripped or stripped.startswith(";")
                or stripped.startswith("!") or stripped.startswith("}")
                or _LABEL.match(stripped)):
            return
        masked = _mask_strings(stripped)
        if (_EXCLUDED_INTRINSIC.search(masked) or _HANDLE_INTRINSIC.search(masked)
                or ("getelementptr" in masked and "@dx.nothing." in masked)):
            stats["excluded_debug_instructions"] += 1
            return
        stats["executable_instructions"] += 1
        dbg_match = _DBG_ATTACHMENT.search(masked)
        if dbg_match is not None:
            stats["instructions_with_debug_location"] += 1
            retained.append(int(dbg_match.group(1)))

    in_define = False
    in_header = False
    depth = 0
    pending = []
    for raw in disassembly.splitlines():
        stripped = raw.strip()
        if not in_define:
            if stripped.startswith("define "):
                stats["define_functions"] += 1
                in_define = True
                depth = _brace_delta(raw)
                in_header = "{" not in _mask_strings(raw)
                inline = _INLINE_BODY.search(raw)
                if inline is not None:
                    record(inline.group(1))
                if depth <= 0 and not in_header:
                    in_define = False
            continue
        if in_header:
            depth += _brace_delta(raw)
            if "{" in _mask_strings(raw):
                in_header = False
            if depth <= 0:
                in_define = False
            continue
        if depth >= 1:
            if pending:
                pending.append(raw)
                if "]" in _mask_strings(raw):
                    record(" ".join(pending))
                    pending = []
            elif re.match(r"^(?:%[^=]+\s*=\s*)?switch\b", stripped) and "[" in _mask_strings(raw):
                if "]" in _mask_strings(raw):
                    record(raw)
                else:
                    pending = [raw]
            else:
                record(raw)
        depth += _brace_delta(raw)
        if depth <= 0:
            in_define = False
            in_header = False
    if in_define or pending:
        raise DxilSourceMappingError("Truncated DXIL function body")
    return metadata, retained, stats


def _count_unreferenced_locations(metadata, retained):
    count = 0
    for meta_id, rhs in metadata.items():
        text = rhs.strip()
        if text.startswith("distinct "):
            text = text[9:].lstrip()
        match = _META_KIND_PREFIX.match(text)
        if (match is not None and match.group(1) == "DILocation"
                and meta_id not in retained):
            count += 1
    return count


def _aggregate_unresolved(unresolved, reason, detail, matches=None):
    key = (reason, detail)
    entry = unresolved.get(key)
    if entry is None:
        entry = {"reason": reason, "detail": detail, "count": 0}
        if matches:
            entry["matches"] = list(matches)
        unresolved[key] = entry
    entry["count"] += 1


def parse_dxil_source_mapping(disassembly, source_files):
    """Map retained DXC DXIL instructions to original HLSL source lines."""
    if not isinstance(disassembly, str):
        raise DxilSourceMappingError("DXC DXIL disassembly is not text")
    files = []
    for source in source_files:
        filename = getattr(source, "filename", None)
        contents = getattr(source, "contents", None)
        files.append(SourceFile(
            "" if filename is None else str(filename),
            "" if contents is None or not isinstance(contents, str) else contents,
        ))
    if not files:
        raise DxilSourceMappingError(
            "Shader debug information contains no source files; cannot match "
            "DXC DXIL debug locations")

    metadata, retained, stats = _scan_instructions(disassembly)
    if stats["define_functions"] == 0:
        raise DxilSourceMappingError(
            "DXC DXIL disassembly contains no define function bodies; the "
            "shader has no executable DXIL functions to map")
    if not retained:
        raise DxilSourceMappingError(
            "DXC DXIL disassembly contains no retained instructions with !dbg "
            "debug locations; debug information may be stripped or optimized out")

    unreferenced = _count_unreferenced_locations(metadata, retained)
    index = _SourceIndex(files)
    locations = {}
    for location_id in set(retained):
        if location_id not in metadata:
            raise DxilSourceMappingError(
                "DXIL instruction !dbg references missing metadata !%d"
                % location_id)
        locations[location_id] = _resolve_location(metadata, location_id)

    line_map = {}
    unresolved = {}
    failed_locations = set()
    mapped_instructions = 0
    for location_id in retained:
        resolved, reason = locations[location_id]
        if resolved is None:
            failed_locations.add(location_id)
            _aggregate_unresolved(unresolved, reason, "DILocation")
            continue
        reference, line = resolved
        source, reason, matches = _match_source(index, reference)
        if source is None:
            failed_locations.add(location_id)
            detail = reference.path or reference.filename
            _aggregate_unresolved(unresolved, reason, detail, matches)
            continue
        lines = index.lines_for(source)
        if line < 1 or line > len(lines):
            failed_locations.add(location_id)
            _aggregate_unresolved(
                unresolved, "source_line_out_of_range",
                "%s:%d" % (source.filename, line))
            continue
        key = (source.filename, line)
        entry = line_map.get(key)
        if entry is None:
            entry = {
                "filename": source.filename,
                "line": line,
                "text": lines[line - 1],
                "instructions": 0,
            }
            line_map[key] = entry
        entry["instructions"] += 1
        mapped_instructions += 1

    mapped_lines = [
        MappedLine(entry["filename"], entry["line"], entry["text"],
                   entry["instructions"])
        for _key, entry in sorted(line_map.items())
    ]
    per_file = {}
    for item in mapped_lines:
        info = per_file.get(item.filename)
        if info is None:
            info = {"filename": item.filename, "mapped_lines": 0,
                    "mapped_instructions": 0}
            per_file[item.filename] = info
        info["mapped_lines"] += 1
        info["mapped_instructions"] += item.instructions
    files_summary = [per_file[name] for name in sorted(per_file)]
    unresolved_summary = [
        unresolved[key] for key in sorted(unresolved)
    ]

    counts = {
        "define_functions": stats["define_functions"],
        "executable_instructions": stats["executable_instructions"],
        "instructions_with_debug_location":
            stats["instructions_with_debug_location"],
        "mapped_instructions": mapped_instructions,
        "excluded_debug_instructions": stats["excluded_debug_instructions"],
        "unreferenced_debug_locations": unreferenced,
        "unresolved_instructions":
            stats["instructions_with_debug_location"] - mapped_instructions,
        "unresolved_locations": len(failed_locations),
        "mapped_source_lines": len(mapped_lines),
        "mapped_source_files": len(files_summary),
        "source_files_available": len(files),
    }
    return DxilMapping(mapped_lines, counts, files_summary,
                       unresolved_summary, unreferenced)


def render_mapped_hlsl(mapping, event_id, stage_name):
    """Render a mapped source excerpt with #line directives and a warning."""
    counts = mapping.counts
    parts = [
        "// DXC DXIL-mapped HLSL source excerpt (archival).",
        '// Event %d, %s stage, disassembly target "%s".'
        % (event_id, stage_name, _DXC_DXIL_TARGET),
        "// Mapped %d source line(s) from %d file(s) for %d retained "
        "instruction(s)." % (counts["mapped_source_lines"],
                             counts["mapped_source_files"],
                             counts["mapped_instructions"]),
        _WARNING_COMMENT,
        "",
    ]
    for item in mapping.lines:
        parts.append('#line %d "%s"' % (item.line, item.filename))
        parts.append(item.text)
    return "\n".join(parts) + "\n"


def source_files_from_reflection(reflection):
    """Extract source file names and contents from shader debug information."""
    debug_info = getattr(reflection, "debugInfo", None)
    source_files = (getattr(debug_info, "files", None)
                    if debug_info is not None else None)
    result = []
    for source in source_files or []:
        filename = getattr(source, "filename", None)
        contents = getattr(source, "contents", None)
        result.append(SourceFile(
            "" if filename is None else str(filename),
            "" if contents is None or not isinstance(contents, str) else contents,
        ))
    return result


def _shader_provenance(reflection):
    debug_info = getattr(reflection, "debugInfo", None)

    def value(obj, name):
        item = getattr(obj, name, None) if obj is not None else None
        return None if item is None else str(item)

    return {
        "resource_id": str(getattr(reflection, "resourceId", "")),
        "entry_point": value(reflection, "entryPoint"),
        "encoding": value(reflection, "encoding"),
        "source_entry_point": value(debug_info, "entrySourceName"),
        "source_encoding": value(debug_info, "encoding"),
        "source_debug_information": bool(
            getattr(debug_info, "sourceDebugInformation", False))
        if debug_info is not None else False,
    }


def _provenance():
    return {
        "disassembly_target": _DXC_DXIL_TARGET,
        "mapping_source": (
            "DXC DXIL instruction !dbg !DILocation -> DI scope/file -> DIFile "
            "-> shader debug source file"),
        "instruction_scope": (
            "define function bodies only; debug/lifetime/invariant calls, "
            "DXIL handle bookkeeping and dx.nothing loads are excluded"),
        "representation": (
            "RenderDoc DXC DXIL disassembly may use ILDB debug bytecode; "
            "this is not hardware ISA or exact source optimization coverage"),
        "unreferenced_locations": (
            "DILocations not referenced by retained executable instructions "
            "are ignored"),
        "line_source": (
            "DILocation line; inlinedAt chains are not followed"),
        "file_matching": (
            "exact filename, normalized full path, then unique basename; "
            "ambiguous or missing source files are not mapped"),
        "deduplication": (
            "unique (source filename, line), sorted by filename then line"),
        "fallback": (
            "mapped source excerpt only; there is no full-source fallback"),
        "warning": _ARCHIVAL_WARNING,
    }


def _no_mapped_lines_message(mapping):
    reasons = ", ".join(
        "%s (%d)" % (entry["reason"], entry["count"])
        for entry in mapping.unresolved
    ) or "no resolvable !dbg source locations"
    return (
        "No source line could be mapped from DXC DXIL debug locations without "
        "guessing: %s. Shader debug information may be stripped, incomplete, "
        "or reference source files absent from the capture." % reasons)


def _parse_event(value):
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > 0xFFFFFFFE):
        raise ValueError(
            "event_id must be an integer between 0 and 4294967294")
    return value


def _parse_stage(value):
    if not isinstance(value, str):
        raise ValueError("stage must be one of: %s" % ", ".join(_SHADER_STAGES))
    name = value.strip().lower()
    if name not in _SHADER_STAGES:
        raise ValueError("Unknown shader stage: %s" % value)
    return name


def _prepare_output_path(output_path):
    try:
        path = os.fsdecode(os.fspath(output_path))
    except TypeError:
        raise ValueError("output_path must be a filesystem path")
    if not path or not os.path.isabs(path):
        raise ValueError("output_path must be an absolute path: %s" % path)
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ValueError(
            "output_path parent directory does not exist: %s" % parent)
    if os.path.splitext(path)[1].lower() != ".hlsl":
        raise ValueError("output_path must end in .hlsl: %s" % path)
    if os.path.lexists(path):
        raise ValueError(
            "Output file already exists and will not be overwritten: %s" % path)
    return path


def _write_output(path, text):
    data = text.encode("utf-8")
    try:
        handle = open(path, "xb")
    except FileExistsError:
        raise ValueError(
            "Output file already exists and will not be overwritten: %s" % path)
    except OSError as exc:
        raise ValueError("Could not create output file %s: %s" % (path, exc))
    try:
        with handle:
            handle.write(data)
    except OSError as exc:
        try:
            os.remove(path)
        except OSError as cleanup_error:
            raise ValueError(
                "Could not write output file %s: %s; could not remove the "
                "partial file: %s" % (path, exc, cleanup_error))
        raise ValueError("Could not write output file %s: %s" % (path, exc))
    return len(data)


def _restore_event(controller, original_event, state):
    if original_event is None:
        return
    try:
        controller.SetFrameEvent(original_event, True)
    except Exception as exc:
        state["restore_error"] = (
            "Failed to restore replay event %s: %s" % (original_event, exc))


class ShaderSourceExportService:
    """Export mapped HLSL source without initializing the shader debugger."""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def export_shader_mapped_source(self, event_id, stage, output_path):
        """Write source lines referenced by retained DXC DXIL instructions."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        event = _parse_event(event_id)
        stage_name = _parse_stage(stage)
        path = _prepare_output_path(output_path)
        action = self.ctx.GetAction(event)
        if action is None or getattr(action, "eventId", None) != event:
            raise ValueError("Event %d is not an action" % event)

        state = {
            "original_event": self.ctx.CurEvent(),
            "fatal": None,
            "restore_error": None,
            "mapping": None,
            "shader": None,
            "disassembly_lines": 0,
        }

        def callback(controller):
            self._collect(controller, event, stage_name, state)

        self._invoke_checked(callback)
        self._raise_state_errors(state)
        mapping = state["mapping"]
        if mapping is None:
            raise ValueError("Shader source mapping failed without a diagnostic")

        text = render_selected_uniforms(state["selected_uniforms"]) + render_mapped_hlsl(mapping, event, stage_name)
        size = _write_output(path, text)
        counts = dict(mapping.counts)
        counts["disassembly_lines"] = state["disassembly_lines"]
        return {
            "output_path": path,
            "written": True,
            "size_bytes": size,
            "bytes_written": size,
            "event_id": event,
            "stage": stage_name,
            "disassembly_target": _DXC_DXIL_TARGET,
            "shader": state["shader"],
            "selected_uniform_buffers": len(state["selected_uniforms"]),
            "selected_uniform_entries": sum(len(buffer["values"]) for buffer in state["selected_uniforms"]),
            "counts": counts,
            "source_files": mapping.files,
            "unresolved": mapping.unresolved,
            "provenance": _provenance(),
            "warning": _ARCHIVAL_WARNING,
        }

    def _invoke_checked(self, callback):
        try:
            self._invoke(callback)
        except Exception as exc:
            raise ValueError(
                "Replay invocation failed: %s: %s"
                % (type(exc).__name__, exc))

    @staticmethod
    def _raise_state_errors(state):
        if state["restore_error"]:
            if state["fatal"]:
                raise ValueError("%s; %s" % (state["fatal"],
                                             state["restore_error"]))
            raise ValueError(state["restore_error"])
        if state["fatal"]:
            raise ValueError(state["fatal"])

    def _collect(self, controller, event, stage_name, state):
        applied = False
        try:
            applied = True
            controller.SetFrameEvent(event, True)
            renderdoc = _renderdoc()
            pipe = controller.GetPipelineState()
            stage_enum = _stage_enum(stage_name, renderdoc)
            shader = pipe.GetShader(stage_enum)
            if shader == renderdoc.ResourceId.Null():
                raise ValueError(
                    "No %s shader bound at event %d" % (stage_name, event))
            reflection = pipe.GetShaderReflection(stage_enum)
            if reflection is None:
                raise ValueError(
                    "Shader reflection unavailable for %s shader" % stage_name)
            if str(reflection.encoding).rsplit(".", 1)[-1] != "DXIL":
                raise DxilSourceMappingError("Mapped source export currently supports DXIL shaders only")
            files = source_files_from_reflection(reflection)
            if not files:
                raise DxilSourceMappingError(
                    "Shader debug information contains no source files; "
                    "cannot match DXC DXIL debug locations")
            if not any(source.contents for source in files):
                raise DxilSourceMappingError(
                    "Shader debug information source files are empty; cannot "
                    "export mapped source")
            targets = [
                str(target)
                for target in (controller.GetDisassemblyTargets(True) or [])
            ]
            if _DXC_DXIL_TARGET not in targets:
                raise DxilSourceMappingError(
                    "DXC DXIL disassembly target is not advertised by this "
                    "capture (available: %s). export_shader_mapped_source "
                    "requires a D3D12 DXIL shader with debug metadata."
                    % (", ".join(targets) if targets else "none"))
            pipeline = (pipe.GetComputePipelineObject()
                        if stage_name == "compute"
                        else pipe.GetGraphicsPipelineObject())
            disassembly = controller.DisassembleShader(
                pipeline, reflection, _DXC_DXIL_TARGET)
            if not isinstance(disassembly, str) or not disassembly.strip():
                raise DxilSourceMappingError(
                    "DXC DXIL disassembly was empty; cannot map source")
            state["disassembly_lines"] = len(disassembly.splitlines())
            mapping = parse_dxil_source_mapping(disassembly, files)
            if not mapping.lines:
                raise DxilSourceMappingError(_no_mapped_lines_message(mapping))
            state["mapping"] = mapping
            state["shader"] = _shader_provenance(reflection)
            state["selected_uniforms"] = collect_selected_uniforms(controller, pipe, reflection, stage_enum, mapping, files)
        except DxilSourceMappingError as exc:
            state["fatal"] = str(exc)
        except Exception as exc:
            state["fatal"] = (
                "Failed to export mapped shader source: %s: %s"
                % (type(exc).__name__, exc))
        finally:
            if applied:
                _restore_event(controller, state["original_event"], state)


def _stage_enum(stage_name, renderdoc):
    stages = {
        "vertex": renderdoc.ShaderStage.Vertex,
        "hull": renderdoc.ShaderStage.Hull,
        "domain": renderdoc.ShaderStage.Domain,
        "geometry": renderdoc.ShaderStage.Geometry,
        "pixel": renderdoc.ShaderStage.Pixel,
        "compute": renderdoc.ShaderStage.Compute,
    }
    return stages[stage_name]
