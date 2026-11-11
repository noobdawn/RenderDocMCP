"""Delta codec for serialized RenderDoc shader debug state streams.

Each encoded record is a JSON-safe delta against the previous state seen for
the same trace.  Variable objects are tracked by name, assigned monotonic
integer ids, and updated with generic dictionary/list patch operations.  The
decoder mirrors the encoder bookkeeping so ``decode(encode(state))``
reproduces the input state, including every ``before``/``after`` object and
the presence/type metadata attached to each change event.
"""
# noqa: SIZE_OK - single-file codec; encode/decode state machines are tightly coupled

import copy
import json
import math


CODEC_VERSION = 1

_OP_SET = "set"
_OP_UNSET = "unset"
_OP_INSERT = "insert"
_OP_DELETE = "delete"

_METADATA_KEYS = ("step_index", "next_instruction", "flags", "flags_value")


def _copy(value):
    return copy.deepcopy(value)


def _exact_equal(left, right):
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        if left != left and right != right:
            return True
        if left == 0.0 and right == 0.0:
            return math.copysign(1.0, left) == math.copysign(1.0, right)
        return left == right
    if isinstance(left, dict):
        left_keys = list(left.keys())
        right_keys = list(right.keys())
        if len(left_keys) != len(right_keys):
            return False
        if not all(_exact_equal(a, b) for a, b in zip(left_keys, right_keys)):
            return False
        return all(_exact_equal(left[key], right[key]) for key in left_keys)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _exact_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _diff_ops(base, target, path):
    if _exact_equal(base, target):
        return []
    if isinstance(base, dict) and isinstance(target, dict):
        base_keys = list(base.keys())
        target_keys = list(target.keys())
        if (len(base_keys) != len(target_keys)
                or not all(_exact_equal(a, b)
                           for a, b in zip(base_keys, target_keys))):
            return [{"op": _OP_SET, "path": list(path), "value": _copy(target)}]
        operations = []
        for key in base:
            operations.extend(_diff_ops(base[key], target[key], path + [key]))
        return operations
    if isinstance(base, list) and isinstance(target, list):
        operations = []
        common = min(len(base), len(target))
        for index in range(common):
            operations.extend(
                _diff_ops(base[index], target[index], path + [index])
            )
        if len(target) > len(base):
            for index in range(len(base), len(target)):
                operations.append({
                    "op": _OP_INSERT, "path": list(path) + [index],
                    "value": _copy(target[index]),
                })
        else:
            for index in range(len(base) - 1, len(target) - 1, -1):
                operations.append({
                    "op": _OP_DELETE, "path": list(path) + [index],
                })
        return operations
    return [{"op": _OP_SET, "path": list(path), "value": _copy(target)}]


def _resolve(container, path):
    for segment in path:
        container = container[segment]
    return container


def _apply_ops(root, operations):
    for operation in operations:
        kind = operation["op"]
        path = operation["path"]
        if kind == _OP_SET:
            if not path:
                root = _copy(operation["value"])
            else:
                parent = _resolve(root, path[:-1])
                parent[path[-1]] = _copy(operation["value"])
        elif kind == _OP_UNSET:
            parent = _resolve(root, path[:-1])
            del parent[path[-1]]
        elif kind == _OP_INSERT:
            _resolve(root, path[:-1]).insert(
                path[-1], _copy(operation["value"])
            )
        elif kind == _OP_DELETE:
            del _resolve(root, path[:-1])[path[-1]]
        else:
            raise ValueError("Unknown patch operation: %s" % (kind,))
    return root


class StateEncoder(object):
    """Encode shader debug states into per-trace delta records."""

    def __init__(self):
        self._variable_ids = {}
        self._variables = {}
        self._next_variable_id = 0
        self._callstack_ids = {}
        self._callstacks = {}
        self._next_callstack_id = 0
        self._metadata = {}

    def encode(self, state):
        record = {"version": CODEC_VERSION}
        for key in _METADATA_KEYS:
            if key in state and (
                key not in self._metadata
                or not _exact_equal(self._metadata[key], state[key])
            ):
                record[key] = _copy(state[key])
                self._metadata[key] = _copy(state[key])
        callstack = state.get("callstack", [])
        callstack_key = json.dumps(
            callstack, sort_keys=True, separators=(",", ":")
        )
        callstack_id = self._callstack_ids.get(callstack_key)
        if callstack_id is None:
            callstack_id = self._next_callstack_id
            self._next_callstack_id += 1
            self._callstack_ids[callstack_key] = callstack_id
            self._callstacks[callstack_id] = _copy(callstack)
            record["callstack"] = _copy(callstack)
        record["callstack_id"] = callstack_id
        record["changes"] = [
            self._encode_change(change) for change in state.get("changes", [])
        ]
        return record

    def _new_variable_id(self):
        value = self._next_variable_id
        self._next_variable_id += 1
        return value

    def checkpoint(self, state):
        names = {variable.get("name") for change in state.get("changes", [])
                 for variable in (change.get("before"), change.get("after"))
                 if variable is not None}
        ids = {name: self._variable_ids.get(name) for name in names}
        values = {variable_id: self._variables[variable_id] for variable_id in ids.values()
                  if variable_id in self._variables}
        return (ids, values, self._next_variable_id, self._next_callstack_id, dict(self._metadata))

    def rollback(self, checkpoint):
        ids, values, variable_frontier, stack_frontier, metadata = checkpoint
        for variable_id in range(variable_frontier, self._next_variable_id):
            self._variables.pop(variable_id, None)
        self._variables.update(values)
        for name, variable_id in ids.items():
            if variable_id is None:
                self._variable_ids.pop(name, None)
            else:
                self._variable_ids[name] = variable_id
        for key, stack_id in list(self._callstack_ids.items()):
            if stack_id >= stack_frontier:
                del self._callstack_ids[key]
                self._callstacks.pop(stack_id, None)
        self._next_variable_id = variable_frontier
        self._next_callstack_id = stack_frontier
        self._metadata = metadata

    def _encode_before(self, before):
        if before is None:
            return None
        name = before.get("name")
        variable_id = self._variable_ids.get(name)
        if variable_id is not None and _exact_equal(
            self._variables.get(variable_id), before
        ):
            return {"id": variable_id}
        if variable_id is None:
            variable_id = self._new_variable_id()
        self._variable_ids[name] = variable_id
        self._variables[variable_id] = _copy(before)
        return {"id": variable_id, "seed": _copy(before)}

    def _encode_after(self, before, after):
        if after is None:
            if before is None:
                return None
            name = before.get("name")
            variable_id = self._variable_ids.pop(name, None)
            if variable_id is None:
                return None
            self._variables.pop(variable_id, None)
            return {"id": variable_id, "unset": True}
        name = after.get("name")
        same_identity = before is not None and before.get("name") == name
        variable_id = self._variable_ids.get(name)
        if not same_identity or variable_id is None:
            if variable_id is not None:
                self._variables.pop(variable_id, None)
            variable_id = self._new_variable_id()
            self._variable_ids[name] = variable_id
            self._variables[variable_id] = _copy(after)
            return {"id": variable_id, "create": _copy(after)}
        current = self._variables.get(variable_id)
        if _exact_equal(current, after):
            return {"id": variable_id}
        operations = _diff_ops(current, after, [])
        self._variables[variable_id] = _copy(after)
        return {"id": variable_id, "patch": operations}

    def _encode_change(self, change):
        before = change.get("before")
        after = change.get("after")
        before_record = self._encode_before(before)
        after_record = self._encode_after(before, after)
        if (
            before is not None
            and after is not None
            and before.get("name") != after.get("name")
        ):
            old_id = self._variable_ids.get(before.get("name"))
            if old_id is not None:
                self._variable_ids.pop(before.get("name"), None)
                self._variables.pop(old_id, None)
        if "before_present" in change:
            before_present = change["before_present"]
        else:
            before_present = before is not None
        if "after_present" in change:
            after_present = change["after_present"]
        else:
            after_present = after is not None
        if "change_type" in change:
            change_type = change["change_type"]
        elif before_present and after_present:
            change_type = "modified"
        elif after_present:
            change_type = "created"
        elif before_present:
            change_type = "destroyed"
        else:
            change_type = "none"
        return {
            "before": before_record,
            "after": after_record,
            "before_present": before_present,
            "after_present": after_present,
            "change_type": change_type,
        }


class StateDecoder(object):
    """Decode per-trace records back into the original state dictionaries."""

    def __init__(self):
        self._variables = {}
        self._name_to_id = {}
        self._callstacks = {}
        self._metadata = {}

    def decode(self, record):
        if not isinstance(record, dict):
            raise ValueError("State record must be a dictionary")
        version = record.get("version", CODEC_VERSION)
        if version != CODEC_VERSION:
            raise ValueError("Unsupported state codec version: %r" % (version,))
        for key in _METADATA_KEYS:
            if key in record:
                self._metadata[key] = _copy(record[key])
        callstack_id = record.get("callstack_id")
        if "callstack" in record:
            callstack = _copy(record["callstack"])
            self._callstacks[callstack_id] = callstack
        elif callstack_id in self._callstacks:
            callstack = _copy(self._callstacks[callstack_id])
        else:
            raise ValueError("Unknown callstack id: %r" % (callstack_id,))
        changes = [
            self._decode_change(change) for change in record.get("changes", [])
        ]
        state = {}
        for key in _METADATA_KEYS:
            if key in self._metadata:
                state[key] = _copy(self._metadata[key])
        state["callstack"] = callstack
        state["changes"] = changes
        return state

    def _decode_reference(self, reference):
        if reference is None:
            return None
        if "create" in reference or "seed" in reference:
            variable_id = reference["id"]
            value = reference["create"] if "create" in reference else reference["seed"]
            old_id = self._name_to_id.get(value.get("name"))
            if old_id is not None and old_id != variable_id:
                self._variables.pop(old_id, None)
            self._variables[variable_id] = _copy(value)
            self._name_to_id[value.get("name")] = variable_id
            return _copy(value)
        if reference.get("unset"):
            variable_id = reference["id"]
            self._variables.pop(variable_id, None)
            for name, mapped in list(self._name_to_id.items()):
                if mapped == variable_id:
                    del self._name_to_id[name]
            return None
        variable_id = reference["id"]
        if variable_id not in self._variables:
            raise ValueError("Unknown variable id: %r" % (variable_id,))
        if "patch" in reference:
            current = _apply_ops(
                self._variables[variable_id], reference["patch"]
            )
            self._variables[variable_id] = current
            self._name_to_id[current.get("name")] = variable_id
            return _copy(current)
        return _copy(self._variables[variable_id])

    def _retire_renamed(self, before, after):
        if (
            before is not None
            and after is not None
            and before.get("name") != after.get("name")
        ):
            old_name = before.get("name")
            old_id = self._name_to_id.get(old_name)
            new_id = self._name_to_id.get(after.get("name"))
            if old_id is not None and old_id != new_id:
                self._name_to_id.pop(old_name, None)
                self._variables.pop(old_id, None)

    def _decode_change(self, change):
        before = self._decode_reference(change.get("before"))
        after = self._decode_reference(change.get("after"))
        self._retire_renamed(before, after)
        if "before_present" in change:
            before_present = change["before_present"]
        else:
            before_present = before is not None
        if "after_present" in change:
            after_present = change["after_present"]
        else:
            after_present = after is not None
        if "change_type" in change:
            change_type = change["change_type"]
        elif before_present and after_present:
            change_type = "modified"
        elif after_present:
            change_type = "created"
        elif before_present:
            change_type = "destroyed"
        else:
            change_type = "none"
        return {
            "before": before,
            "after": after,
            "before_present": before_present,
            "after_present": after_present,
            "change_type": change_type,
        }
