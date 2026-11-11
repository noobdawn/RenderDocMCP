import json
import re

from ..utils.shader_debug_serializer import serialize_shader_variable


def _mask_source(text):
    return re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|^[ \t]*#[^\n]*',
                  lambda match: ''.join('\n' if c == '\n' else ' ' for c in match.group()),
                  text, flags=re.DOTALL | re.MULTILINE)


def _source_text(mapping, files):
    if files is None:
        return _mask_source("\n".join(item.text for item in mapping.lines))
    masked = {source.filename: _mask_source(source.contents).splitlines() for source in files}
    return '\n'.join(masked[item.filename][item.line - 1] for item in mapping.lines)


def _references(text, aliases):
    result = []
    for alias in aliases:
        pattern = r'(?<![A-Za-z0-9_])' + re.escape(alias).replace(r'\.', r'\s*\.\s*') + r'(?![A-Za-z0-9_])'
        for match in re.finditer(pattern, text):
            result.append(text[match.end():])
    return result


def _select(variable, aliases, text, path, result):
    references = _references(text, aliases)
    if not references:
        return
    members = list(getattr(variable, "members", []) or [])
    array_members = bool(members) and all(re.search(r'\[\d+\]$', member.name) for member in members)
    if array_members:
        indices = set()
        dynamic = False
        for tail in references:
            subscript = re.match(r'\s*\[\s*(\d+)\s*[uU]?\s*\]', tail)
            if subscript:
                indices.add(int(subscript.group(1)))
            else:
                dynamic = True
        chosen = members[:8] if dynamic else [member for member in members
            if int(re.search(r'\[(\d+)\]$', member.name).group(1)) in indices]
        for member in chosen:
            index = re.search(r'\[(\d+)\]$', member.name).group(1)
            _append_value(member, path + '[' + index + ']', result)
        if dynamic and len(members) > len(chosen):
            result.append({"name": path, "status": "preview", "reason": "dynamic or whole-array reference",
                           "shown_elements": len(chosen), "total_elements": len(members),
                           "omitted_elements": len(members) - len(chosen)})
        missing = sorted(indices - {int(re.search(r'\[(\d+)\]$', m.name).group(1)) for m in members})
        if missing:
            result.append({"name": path, "status": "unavailable", "requested_indices": missing})
    elif members:
        whole = any(not re.match(r'\s*\.', tail) for tail in references)
        if whole:
            for member in members[:8]:
                _append_value(member, path + '.' + member.name, result)
            if len(members) > 8:
                result.append({"name": path, "status": "preview", "omitted_members": len(members) - 8})
        else:
            for member in members:
                _select(member, [alias + '.' + member.name for alias in aliases], text,
                        path + '.' + member.name, result)
    else:
        _append_value(variable, path, result)


def _append_value(variable, path, result):
    members = list(getattr(variable, "members", []) or [])
    if members:
        for member in members[:8]:
            _append_value(member, path + '.' + member.name, result)
        if len(members) > 8:
            result.append({"name": path, "status": "preview", "omitted_members": len(members) - 8})
        return
    serialized = serialize_shader_variable(variable)
    if serialized is None:
        result.append({"name": path, "status": "unavailable"})
        return
    payload = {key: serialized[key] for key in ("value", "pointer", "binding", "direct_access") if key in serialized}
    result.append({"name": path, "type": serialized["type"], "rows": serialized["rows"],
                   "columns": serialized["columns"], "flags": serialized["flags"],
                   "data": payload, "status": "ok" if payload else "unavailable"})


def collect_selected_uniforms(controller, pipe, reflection, stage, mapping, files=None):
    import renderdoc as rd
    text = _source_text(mapping, files)
    buffers = []
    pipeline = pipe.GetComputePipelineObject() if stage == rd.ShaderStage.Compute else pipe.GetGraphicsPipelineObject()
    blocks = list(getattr(reflection, "constantBlocks", []) or [])
    alias_owners = {}
    for index, block in enumerate(blocks):
        for variable in getattr(block, "variables", []) or []:
            for alias in (variable.name, block.name + '.' + variable.name, block.name + '_' + variable.name):
                alias_owners.setdefault(alias, set()).add((index, variable.name))
    for index, block in enumerate(blocks):
        names = [variable.name for variable in getattr(block, "variables", []) or []]
        if not any(_references(text, [name, block.name + '.' + name, block.name + '_' + name]) for name in names):
            continue
        item = {"name": block.name, "reflection_index": index, "values": []}
        buffers.append(item)
        if int(getattr(block, "bindArraySize", 1)) != 1:
            item.update(status="unavailable", error="Constant-buffer binding arrays are not selected by lexical matching")
            continue
        try:
            if hasattr(pipe, "GetConstantBlock"):
                descriptor = pipe.GetConstantBlock(stage, index, 0).descriptor
                resource = descriptor.resource
            else:
                descriptor = pipe.GetConstantBuffer(stage, index, 0)
                resource = descriptor.resourceId
            item.update(resource_id=str(resource), byte_offset=int(descriptor.byteOffset),
                        byte_size=int(descriptor.byteSize))
            if getattr(block, "bufferBacked", True) and (resource == rd.ResourceId.Null() or descriptor.byteSize <= 0):
                item.update(status="unavailable", error="No bound data range; no values inferred")
                continue
            variables = controller.GetCBufferVariableContents(pipeline, reflection.resourceId,
                stage, reflection.entryPoint, index, resource, descriptor.byteOffset,
                min(descriptor.byteSize, block.byteSize) if getattr(block, "bufferBacked", True) else descriptor.byteSize)
            for variable in variables:
                aliases = [alias for alias in (variable.name, block.name + '.' + variable.name, block.name + '_' + variable.name)
                           if len(alias_owners.get(alias, [])) == 1]
                ambiguous = [alias for alias in (variable.name, block.name + '.' + variable.name, block.name + '_' + variable.name)
                             if len(alias_owners.get(alias, [])) > 1 and _references(text, [alias])]
                if ambiguous:
                    item["values"].append({"name": block.name + '.' + variable.name,
                                           "status": "ambiguous", "spellings": ambiguous})
                _select(variable, aliases,
                        text, block.name + '.' + variable.name, item["values"])
            item["status"] = "ok" if item["values"] else "unavailable"
            if getattr(block, "bufferBacked", True) and descriptor.byteSize < block.byteSize:
                item.update(status="partial", error="Bound range smaller than reflected block")
        except Exception as exc:
            item.update(status="unavailable", error="%s: %s" % (type(exc).__name__, exc))
    return buffers


def render_selected_uniforms(buffers):
    if not buffers:
        return ""
    lines = ["// Uniform values selected by mapped-source identifier references (not proof of GPU access):"]
    candidates = []
    for buffer in buffers:
        candidates.append('// ' + json.dumps({key: value for key, value in buffer.items() if key != 'values'}, ensure_ascii=True))
        for value in buffer["values"]:
            candidates.append('// ' + json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')))
    used = len(lines[0].encode('utf-8')) + 1
    for index, line in enumerate(candidates):
        size = len(line.encode('utf-8')) + 1
        if used + size > 65000:
            lines.append('// Header budget exceeded; omitted %d remaining entries.' % (len(candidates) - index))
            break
        lines.append(line)
        used += size
    return '\n'.join(lines) + '\n\n'
