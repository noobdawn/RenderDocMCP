import copy
import importlib.util
import json
import math
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "renderdoc_extension"
    / "services"
    / "shader_state_codec.py"
)
SPEC = importlib.util.spec_from_file_location("shader_state_codec", str(MODULE_PATH))
CODEC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CODEC)


def variable(name, type_name="Float", rows=1, columns=1, extra=None):
    result = {
        "name": name,
        "type": type_name,
        "rows": rows,
        "columns": columns,
        "flags": [],
        "flags_value": 0,
    }
    if extra:
        result.update(copy.deepcopy(extra))
    return result


def change(before, after, before_present=None, after_present=None,
           change_type=None):
    if before_present is None:
        before_present = before is not None
    if after_present is None:
        after_present = after is not None
    if change_type is None:
        if before_present and after_present:
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


def state(step_index, next_instruction, flags, flags_value, callstack, changes):
    return {
        "step_index": step_index,
        "next_instruction": next_instruction,
        "flags": list(flags),
        "flags_value": flags_value,
        "callstack": list(callstack),
        "changes": list(changes),
    }


def assert_exact(actual, expected):
    assert type(actual) is type(expected), (actual, expected)
    if isinstance(expected, float):
        if expected != expected:
            assert actual != actual
        else:
            assert actual == expected
            if expected == 0.0:
                assert math.copysign(1.0, actual) == math.copysign(1.0, expected)
    elif isinstance(expected, dict):
        assert list(actual.keys()) == list(expected.keys()), (actual, expected)
        for key in expected:
            assert_exact(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected):
            assert_exact(actual_item, expected_item)
    else:
        assert actual == expected


def encode_stream(states):
    encoder = CODEC.StateEncoder()
    decoder = CODEC.StateDecoder()
    records = [encoder.encode(item) for item in states]
    decoded = [decoder.decode(record) for record in records]
    for record in records:
        json.dumps(record, allow_nan=False)
    return records, decoded


def test_round_trips_independent_state_stream_exactly():
    value = variable("value", extra={"value": [1, 2, 3]})
    states = [
        state(0, 4, ["GeneratedNanOrInf"], 2, [1, 2, 3], [
            change(None, value, False, True, "created"),
        ]),
        state(1, 5, ["GeneratedNanOrInf"], 2, [1, 2, 3], [
            change(copy.deepcopy(value),
                   variable("value", extra={"value": [1, 2, 4]}),
                   True, True, "modified"),
        ]),
        state(2, 6, ["DebugBreak"], 4, [1, 2, 3], []),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    assert records[0]["changes"][0]["after"]["create"] == value


def test_incremental_record_is_compact_and_omits_full_before():
    value = variable("value", extra={"value": [1, 2, 3]})
    updated = variable("value", extra={"value": [1, 2, 4]})
    states = [
        state(0, 4, [], 0, [7], [change(None, value, False, True, "created")]),
        state(0, 5, [], 0, [7], [
            change(copy.deepcopy(value), updated, True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    before_reference = records[1]["changes"][0]["before"]
    assert before_reference == {"id": records[0]["changes"][0]["after"]["id"]}
    assert "seed" not in before_reference
    operations = records[1]["changes"][0]["after"]["patch"]
    assert operations == [{"op": "set", "path": ["value", 2], "value": 4}]
    assert isinstance(operations[0]["path"][0], str)
    assert isinstance(operations[0]["path"][1], int)
    assert not isinstance(operations[0]["path"][1], bool)
    assert "name" not in json.dumps(records[1])


def test_create_destroy_recreate_and_rename_use_monotonic_ids():
    original = variable("value", extra={"value": [1]})
    renamed = variable("renamed", extra={"value": [1]})
    states = [
        state(0, 1, [], 0, [1], [
            change(None, original, False, True, "created"),
        ]),
        state(1, 2, [], 0, [1], [
            change(copy.deepcopy(original), None, True, False, "destroyed"),
        ]),
        state(2, 3, [], 0, [1], [
            change(None, copy.deepcopy(original), False, True, "created"),
        ]),
        state(3, 4, [], 0, [1], [
            change(copy.deepcopy(original), renamed, True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    first_id = records[0]["changes"][0]["after"]["id"]
    assert records[1]["changes"][0]["after"] == {
        "id": first_id, "unset": True,
    }
    recreated_id = records[2]["changes"][0]["after"]["id"]
    assert recreated_id > first_id
    assert "create" in records[2]["changes"][0]["after"]
    renamed_id = records[3]["changes"][0]["after"]["id"]
    assert renamed_id > recreated_id
    assert "create" in records[3]["changes"][0]["after"]
    assert records[3]["changes"][0]["after"]["create"]["name"] == "renamed"


def test_member_array_growth_and_shrink_use_insert_and_delete_patches():
    first = variable("first", extra={"value": [1]})
    second = variable("second", extra={"value": [2]})
    initial = variable("struct", "Struct", extra={"members": [first]})
    grown = variable("struct", "Struct", extra={"members": [first, second]})
    shrunk = variable("struct", "Struct", extra={"members": [second]})
    states = [
        state(0, 1, [], 0, [], [
            change(None, initial, False, True, "created"),
        ]),
        state(1, 2, [], 0, [], [
            change(copy.deepcopy(initial), grown, True, True, "modified"),
        ]),
        state(2, 3, [], 0, [], [
            change(copy.deepcopy(grown), shrunk, True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    grown_ops = records[1]["changes"][0]["after"]["patch"]
    insert_ops = [op for op in grown_ops if op["op"] == "insert"]
    assert len(insert_ops) == 1
    assert insert_ops[0]["path"] == ["members", 1]
    shrink_ops = records[2]["changes"][0]["after"]["patch"]
    delete_ops = [op for op in shrink_ops if op["op"] == "delete"]
    assert len(delete_ops) == 1
    assert delete_ops[0]["path"] == ["members", 1]
    assert all(
        operation["op"] != "set" or operation["path"]
        for operation in grown_ops + shrink_ops
    )


def test_compact_multi_member_struct_update_only_emits_changed_leaves():
    alpha = variable("alpha", extra={"value": [1]})
    beta = variable("beta", extra={"value": [2]})
    gamma = variable("gamma", extra={"value": [3]})
    initial = variable("struct", "Struct", extra={
        "members": [alpha, beta, gamma],
    })
    changed_alpha = variable("alpha", extra={"value": [9]})
    changed_beta = variable("beta", extra={"value": [8]})
    updated = variable("struct", "Struct", extra={
        "members": [changed_alpha, changed_beta, gamma],
    })
    states = [
        state(0, 1, [], 0, [], [
            change(None, initial, False, True, "created"),
        ]),
        state(1, 2, [], 0, [], [
            change(copy.deepcopy(initial), updated, True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    operations = records[1]["changes"][0]["after"]["patch"]
    assert operations == [
        {"op": "set", "path": ["members", 0, "value", 0], "value": 9},
        {"op": "set", "path": ["members", 1, "value", 0], "value": 8},
    ]
    assert "members" not in json.dumps(records[1]["changes"][0]["before"])
    assert "seed" not in records[1]["changes"][0]["before"]
    assert records[1]["changes"][0]["before"] == {
        "id": records[0]["changes"][0]["after"]["id"],
    }


def test_metadata_changes_and_noop_change_events_are_preserved():
    value = variable("value", extra={"value": [5]})
    states = [
        state(0, 10, ["SampleLoadGather"], 1, [10], [
            change(None, value, False, True, "created"),
            change(None, None, False, False, "none"),
        ]),
        state(1, 11, ["SampleLoadGather"], 1, [10], [
            change(copy.deepcopy(value), copy.deepcopy(value),
                   True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    assert records[1]["step_index"] == 1
    assert records[1]["next_instruction"] == 11
    assert "flags" not in records[1]
    assert "flags_value" not in records[1]
    assert len(decoded[1]["changes"]) == 1
    assert decoded[1]["changes"][0]["change_type"] == "modified"
    assert records[1]["changes"][0]["after"] == records[1]["changes"][0]["before"]
    assert len(decoded[0]["changes"]) == 2
    assert decoded[0]["changes"][1]["change_type"] == "none"


def test_pointer_and_resource_direct_access_round_trip():
    pointer = variable("pointer", "GPUPointer", extra={
        "pointer": {
            "value": "1", "type_id": "2", "shader_id": "3",
            "raw": ["1", "2", "3", "0"],
        },
    })
    resource = variable("resource", "ReadWriteResource", extra={
        "binding": {
            "category": 1, "index": 2, "array_element": 0,
            "is_direct_access": True, "raw": [1, 2, 0, 1],
        },
        "direct_access": {
            "descriptor_type": 1, "byte_offset": 16,
            "byte_size": 32, "descriptor_store": "99",
        },
    })
    changed_pointer = copy.deepcopy(pointer)
    changed_pointer["pointer"]["shader_id"] = "4"
    changed_resource = copy.deepcopy(resource)
    changed_resource["direct_access"]["byte_offset"] = 32
    states = [
        state(0, 1, [], 0, [1], [
            change(None, pointer, False, True, "created"),
            change(None, resource, False, True, "created"),
        ]),
        state(1, 2, [], 0, [1], [
            change(copy.deepcopy(pointer), changed_pointer,
                   True, True, "modified"),
            change(copy.deepcopy(resource), changed_resource,
                   True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    resource_ops = records[1]["changes"][1]["after"]["patch"]
    assert {"op": "set", "path": ["direct_access", "byte_offset"],
            "value": 32} in resource_ops


def test_signed_zero_and_bool_int_values_are_distinct():
    signed_zero = variable("signed", extra={"value": [0.0]})
    negative_zero = variable("signed", extra={"value": [-0.0]})
    boolean = variable("boolean", extra={"debug": {"enabled": True}})
    integer = variable("boolean", extra={"debug": {"enabled": 1}})
    states = [
        state(0, 1, [], 0, [], [
            change(None, signed_zero, False, True, "created"),
            change(None, boolean, False, True, "created"),
        ]),
        state(1, 2, [], 0, [], [
            change(copy.deepcopy(signed_zero), negative_zero,
                   True, True, "modified"),
            change(copy.deepcopy(boolean), integer, True, True, "modified"),
        ]),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    assert "patch" in records[1]["changes"][0]["after"]
    assert "patch" in records[1]["changes"][1]["after"]
    decoded_zero = decoded[1]["changes"][0]["after"]["value"][0]
    assert math.copysign(1.0, decoded_zero) == -1.0
    assert type(decoded[1]["changes"][1]["before"]["debug"]["enabled"]) is bool
    assert type(decoded[1]["changes"][1]["after"]["debug"]["enabled"]) is int


def test_callstack_is_interned_once_and_new_stacks_get_new_ids():
    states = [
        state(index, index + 1, [], 0, [1, 2, 3], []) for index in range(3)
    ]
    states.append(state(3, 4, [], 0, [1, 2, 4], []))
    states.append(state(4, 5, [], 0, [1, 2, 4], []))

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    first_stack_id = records[0]["callstack_id"]
    assert "callstack" in records[0]
    for record in records[1:3]:
        assert "callstack" not in record
        assert record["callstack_id"] == first_stack_id
    second_stack_id = records[3]["callstack_id"]
    assert second_stack_id != first_stack_id
    assert "callstack" in records[3]
    assert "callstack" not in records[4]
    assert records[4]["callstack_id"] == second_stack_id


def test_before_is_seeded_when_unseen_or_inconsistent():
    unseen = variable("value", extra={"value": [1]})
    changed = variable("value", extra={"value": [2]})
    encoder = CODEC.StateEncoder()
    decoder = CODEC.StateDecoder()
    first_state = state(0, 1, [], 0, [], [
        change(copy.deepcopy(unseen), changed, True, True, "modified"),
    ])

    first_record = encoder.encode(first_state)

    assert "seed" in first_record["changes"][0]["before"]
    assert_exact(decoder.decode(first_record), first_state)

    inconsistent = variable("value", extra={"value": [99]})
    third = variable("value", extra={"value": [3]})
    next_state = state(1, 2, [], 0, [], [
        change(copy.deepcopy(inconsistent), third, True, True, "modified"),
    ])

    second_record = encoder.encode(next_state)

    assert "seed" in second_record["changes"][0]["before"]
    assert_exact(decoder.decode(second_record), next_state)


def test_state_without_changes_encodes_only_identity_fields():
    states = [
        state(0, 1, [], 0, [4], []),
        state(0, 1, [], 0, [4], []),
    ]

    records, decoded = encode_stream(states)

    assert_exact(decoded, states)
    assert records[1] == {
        "version": 1,
        "callstack_id": records[0]["callstack_id"],
        "changes": [],
    }


def test_decoder_rejects_unknown_variable_reference():
    decoder = CODEC.StateDecoder()
    record = {
        "version": 1,
        "step_index": 0,
        "next_instruction": 0,
        "flags": [],
        "flags_value": 0,
        "callstack_id": 0,
        "callstack": [],
        "changes": [{
            "before": {"id": 42},
            "after": None,
            "before_present": True,
            "after_present": False,
            "change_type": "destroyed",
        }],
    }

    with pytest.raises(ValueError):
        decoder.decode(record)
