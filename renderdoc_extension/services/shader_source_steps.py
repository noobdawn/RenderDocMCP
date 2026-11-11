from bisect import bisect_right


def compact_value(variable):
    if variable is None:
        return None
    if variable.get("members"):
        return {member["name"]: compact_value(member) for member in variable["members"]}
    if "direct_access" in variable:
        return {"binding": variable["binding"], "direct_access": variable["direct_access"]}
    for field in ("value", "binding", "pointer"):
        if field in variable:
            return variable[field]
    return None


class SourceSteps:
    def __init__(self, mapping):
        self.mapping = mapping
        self.previous = None
        self.group = None
        self.key = None
        self.last_instruction = None

    def observe(self, state):
        if self.previous is None:
            self.previous = state
            return
        instruction = self.previous["next_instruction"]
        index = bisect_right(self.mapping.keys, instruction) - 1
        location = None
        auxiliary = False
        if index >= 0:
            line = self.mapping.entries[index].lineInfo
            dis_line = int(line.disassemblyLine)
            if self.mapping.keys[index] == instruction and 0 < dis_line <= len(self.mapping.disassembly):
                auxiliary = self.mapping.disassembly[dis_line - 1].strip() == "nop;"
            if 0 <= line.fileIndex < len(self.mapping.files) and line.lineStart > 0:
                location = {"file_index": int(line.fileIndex),
                            "line_start": int(line.lineStart), "line_end": int(line.lineEnd)}
        stack = tuple(self.previous["callstack"])
        folded = auxiliary and not state["changes"] and not state["flags_value"]
        key = ("auxiliary" if folded else tuple(sorted(location.items())) if location else instruction, stack)
        if (self.group is not None and (key != self.key
                or instruction <= self.last_instruction)):
            self.flush()
        if self.group is None:
            self.key = key
            self.group = {"step_start": state["step_index"], "step_end": state["step_index"],
                          "instruction_start": instruction, "instruction_end": instruction,
                          "transitions": 0, "location": location,
                          "kind": "auxiliary" if folded else "source",
                          "callstack": list(stack), "flags_value": 0, "changes": {}}
            if folded:
                self.group["location"] = None
        group = self.group
        group["step_end"] = state["step_index"]
        group["instruction_end"] = instruction
        group["transitions"] += 1
        group["flags_value"] |= state["flags_value"]
        for change in state["changes"]:
            variable = change["after"] or change["before"]
            if variable is None:
                continue
            name = variable["name"]
            if name not in group["changes"]:
                group["changes"][name] = {"before": compact_value(change["before"]),
                    "before_present": change["before_present"], "updates": 0}
            summary = group["changes"][name]
            summary["after"] = compact_value(change["after"])
            summary["after_present"] = change["after_present"]
            summary["updates"] += 1
        self.last_instruction = instruction
        self.previous = state

    def flush(self):
        if self.group is not None:
            self.mapping.store.append(self.mapping.trace_id, "source_steps", self.group)
            self.group = None
