from bisect import bisect_right
import json

from ..utils.shader_debug_serializer import serialize_source_variable_mappings


class ExecutedMapping:
    def __init__(self, trace, files, disassembly, store, trace_id):
        self.entries = trace.instInfo
        self.keys = [int(info.instruction) for info in self.entries]
        if self.keys != sorted(self.keys):
            raise ValueError("Shader instruction mappings are not ordered")
        self.files = [(source.filename, source.contents) for source in files]
        self.file_lines = {}
        self.disassembly = disassembly.splitlines()
        self.store = store
        self.trace_id = trace_id
        self.pending = None
        self.seen = set()
        self.source_lines = set()
        self.variable_sets = {}
        self.mapping_variables = {}
        self.variable_definitions = {}

    def observe(self, state):
        current = int(state.nextInstruction)
        if self.pending is not None and self.pending not in self.seen:
            self._record(self.pending)
            self.seen.add(self.pending)
        self.pending = current

    def _record(self, instruction):
        index = bisect_right(self.keys, instruction) - 1
        if index < 0:
            self.store.append(self.trace_id, "instructions", {
                "instruction": instruction, "mapping_instruction": None,
                "line_info": None, "source_variables_id": None,
            })
            return
        info = self.entries[index]
        line = info.lineInfo
        file_index = int(line.fileIndex)
        start, end = int(line.lineStart), int(line.lineEnd)
        dis_line = int(line.disassemblyLine)
        filename = None
        if 0 <= file_index < len(self.files):
            filename, contents = self.files[file_index]
            if file_index not in self.file_lines:
                self.file_lines[file_index] = contents.splitlines()
            lines = self.file_lines[file_index]
            for number in range(max(1, start), min(end, len(lines)) + 1):
                key = (file_index, number)
                if key not in self.source_lines:
                    self.store.append(self.trace_id, "sources", {
                        "file_index": file_index, "filename": filename,
                        "line": number, "text": lines[number - 1],
                    })
                    self.source_lines.add(key)
        if index not in self.mapping_variables:
            variables = serialize_source_variable_mappings(getattr(info, "sourceVars", None))
            key = json.dumps(variables, sort_keys=True, separators=(",", ":"))
            if key not in self.variable_sets:
                variable_id = len(self.variable_sets)
                references = []
                for variable in variables:
                    definition_key = json.dumps(variable, sort_keys=True, separators=(",", ":"))
                    if definition_key not in self.variable_definitions:
                        definition_id = len(self.variable_definitions)
                        self.store.append(self.trace_id, "mapping_definitions", {
                            "id": definition_id, "mapping": variable})
                        self.variable_definitions[definition_key] = definition_id
                    references.append(self.variable_definitions[definition_key])
                self.store.append(self.trace_id, "source_variables", {"id": variable_id, "mapping_ids": references})
                self.variable_sets[key] = variable_id
            self.mapping_variables[index] = self.variable_sets[key]
        text = self.disassembly[dis_line - 1] if 0 < dis_line <= len(self.disassembly) else None
        self.store.append(self.trace_id, "instructions", {
            "instruction": instruction, "mapping_instruction": int(info.instruction),
            "line_info": {"file_index": file_index, "source_file": filename,
                          "line_start": start, "line_end": end,
                          "col_start": int(line.colStart), "col_end": int(line.colEnd),
                          "disassembly_line": dis_line, "disassembly_line_text": text},
            "source_variables_id": self.mapping_variables[index],
        })
