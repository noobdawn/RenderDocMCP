"""Texture resource and event-bound view inspection for RenderDoc."""

from ..utils import Helpers
import renderdoc as rd

_BUFFER_TYPES = frozenset(
    ["Buffer", "TypedBuffer", "ReadWriteBuffer", "ReadWriteTypedBuffer"]
)
_FORMAT_FIELDS = (
    "name", "component_type", "component_count", "component_byte_width", "type", "srgb"
)


def describe_format(fmt):
    """Serialize a ResourceFormat using only its public API."""
    if fmt is None:
        return None
    comp_type = _enum_name(getattr(fmt, "compType", None))
    format_type = _enum_name(getattr(fmt, "type", None))
    srgb = None if comp_type in (None, "Typeless") else _call_bool(
        fmt, "SRGBCorrected", comp_type == "UNormSRGB")
    return {
        "name": _format_name(fmt), "component_type": comp_type,
        "component_count": _as_int(getattr(fmt, "compCount", 0)),
        "component_byte_width": _as_int(getattr(fmt, "compByteWidth", 0)),
        "type": format_type, "srgb": srgb,
        "is_block_compressed": None if format_type in (None, "Undefined") else bool(fmt.BlockFormat()) if hasattr(fmt, "BlockFormat") else None,
        "is_unorm": None if comp_type in (None, "Typeless") else comp_type in ("UNorm", "UNormSRGB"),
        "special": _call_bool(fmt, "Special", format_type != "Regular"),
        "bgra_order": _call_bool(fmt, "BGRAOrder", False),
    }


def _enum_name(value):
    return None if value is None else str(value).rsplit(".", 1)[-1]


def _format_name(fmt):
    return str(fmt.Name())


def _call_bool(obj, name, default):
    method = getattr(obj, name, None)
    return bool(method()) if method is not None else bool(default)


def _as_int(value):
    return int(value or 0)


def _numeric_id(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (bool, float)):
        raise ValueError("Invalid resource ID")
    text = str(value).strip()
    return int(text.rsplit("::", 1)[1] if "::" in text else text)


def _parse_id(value, label):
    try:
        numeric = _numeric_id(value)
    except (TypeError, ValueError):
        raise ValueError("Invalid %s ID: %s" % (label, value))
    if numeric < 0:
        raise ValueError("Invalid %s ID: %s" % (label, value))
    return numeric


def _resource_matches(value, target_id):
    try:
        return _numeric_id(value) == target_id
    except (TypeError, ValueError):
        return False


def _reflection_entries(reflection, attr):
    return list(getattr(reflection, attr, None) or [])


def _describe_swizzle(swizzle):
    channels = ("red", "green", "blue", "alpha")
    return None if swizzle is None else dict(
        (c, _enum_name(getattr(swizzle, c, None))) for c in channels)


def _format_differences(resource_format, view_format):
    if not resource_format or not view_format:
        return {}
    return dict((f, {"resource": resource_format.get(f), "view": view_format.get(f)})
                for f in _FORMAT_FIELDS if resource_format.get(f) != view_format.get(f))


def _describe_texture(texture, resource_id, resource_format):
    return {
        "resource_id": resource_id, "width": _as_int(getattr(texture, "width", 0)),
        "height": _as_int(getattr(texture, "height", 0)),
        "depth": _as_int(getattr(texture, "depth", 0)),
        "array_size": _as_int(getattr(texture, "arraysize", 0)),
        "mip_levels": _as_int(getattr(texture, "mips", 0)),
        "dimension": str(getattr(texture, "type", "")),
        "msaa_samples": _as_int(getattr(texture, "msSamp", 0)),
        "byte_size": _as_int(getattr(texture, "byteSize", 0)),
        "cubemap": bool(getattr(texture, "cubemap", False)),
        "format": resource_format,
    }


def _describe_view(identity, descriptor, resource_format):
    kind, stage_name, index, array_element, name = identity
    view_format = describe_format(getattr(descriptor, "format", None))
    if view_format is not None and view_format["type"] == "Undefined":
        view_format = None
    differences = _format_differences(resource_format, view_format)
    descriptor_type = _enum_name(getattr(descriptor, "type", None))
    view = {
        "kind": kind,
        "binding": {"stage": stage_name, "index": index, "array_element": array_element,
                    "descriptor_type": descriptor_type, "name": name},
        "resource_id": str(getattr(descriptor, "resource", "")), "view_format": view_format,
        "view_id": str(getattr(descriptor, "view", "")),
        "view_type": _enum_name(getattr(descriptor, "textureType", None)),
        "min_lod_clamp": getattr(descriptor, "minLODClamp", None),
        "format_matches": None if view_format is None or resource_format is None else not differences,
        "format_differences": differences,
    }
    first = _as_int(getattr(descriptor, "firstMip", 0))
    count = _as_int(getattr(descriptor, "numMips", 1))
    if descriptor_type in _BUFFER_TYPES:
        view["element"] = {"first": first, "count": count}
    else:
        view["mip"] = {"first": first, "count": count}
        view["slice"] = {"first": _as_int(getattr(descriptor, "firstSlice", 0)),
                         "count": _as_int(getattr(descriptor, "numSlices", 1))}
        view["swizzle"] = _describe_swizzle(getattr(descriptor, "swizzle", None))
    return view


def _view_key(view):
    b, f = view["binding"], view["view_format"] or {}
    s = view.get("swizzle") or {}
    m, sl, e = (view.get(k) or {} for k in ("mip", "slice", "element"))
    ranges = [(r.get("first"), r.get("count")) for r in (m, sl, e)]
    return (view["kind"], b.get("stage"), b.get("index"), b.get("array_element"),
            b.get("descriptor_type"), view["resource_id"], ranges,
            (s.get("red"), s.get("green"), s.get("blue"), s.get("alpha")),
            (f.get("name"), f.get("component_type"), f.get("component_count"),
             f.get("component_byte_width"), f.get("type"), f.get("srgb")))


def _append_unique(views, view):
    key = _view_key(view)
    if not any(_view_key(existing) == key for existing in views):
        views.append(view)


def _record_error(state, scope, message, stage=None):
    error = {"scope": scope, "message": message}
    state["errors"].append(error if stage is None else dict(error, stage=stage))


class TextureViewService:
    """Inspect a texture resource and the views bound to it at one event."""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_texture_views(self, resource_id, event_id):
        """Return the resource format and every reported view of it at an event."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        target_id = _parse_id(resource_id, "resource")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 0:
            raise ValueError("Invalid event ID: %s" % event_id)
        event = event_id
        action = self.ctx.GetAction(event)
        if action is None or action.eventId != event:
            raise ValueError("Event %s is not an action" % event)
        original_event = self.ctx.CurEvent()
        state = {"texture": None, "resource_id": None, "views": [],
                 "errors": [], "fatal": None}

        def callback(controller):
            self._inspect(controller, target_id, event, original_event, state)

        self._invoke(callback)
        if state["fatal"]:
            raise ValueError(state["fatal"])
        return {"resource_id": state["resource_id"], "event_id": event,
                "resource": state["texture"], "views": state["views"],
                "status": "partial" if state["errors"] else "ok",
                "scope": "reported_shader_descriptors_and_output_targets",
                "errors": state["errors"]}

    def _inspect(self, controller, target_id, event, original_event, state):
        try:
            controller.SetFrameEvent(event, True)
            texture = self._find_texture(controller, target_id)
            if texture is None:
                state["fatal"] = "Texture not found: ResourceId::%s" % target_id
                return
            resource_id = str(getattr(texture, "resourceId", "ResourceId::%s" % target_id))
            resource_format = describe_format(getattr(texture, "format", None))
            state["resource_id"] = resource_id
            state["texture"] = _describe_texture(texture, resource_id, resource_format)
            pipe = controller.GetPipelineState()
            if pipe is None:
                _record_error(state, "pipeline_state", "Pipeline state unavailable")
            else:
                self._collect_shader_views(pipe, target_id, resource_format, state)
                self._collect_output_views(pipe, target_id, resource_format, state)
        except Exception as exc:
            state["fatal"] = "Failed to inspect texture views: %s: %s" % (
                type(exc).__name__, exc)
        finally:
            self._restore_event(controller, original_event, state)

    @staticmethod
    def _find_texture(controller, target_id):
        for texture in controller.GetTextures() or []:
            if _resource_matches(getattr(texture, "resourceId", None), target_id):
                return texture
        return None

    @staticmethod
    def _restore_event(controller, original_event, state):
        if original_event is not None:
            try: controller.SetFrameEvent(original_event, True)
            except Exception as exc: _record_error(state, "restore_event", str(exc))

    def _collect_shader_views(self, pipe, target_id, resource_format, state):
        def collect(stage_info, reflection, spec):
            stage, stage_name = stage_info
            kind, method_name, reflection_attr = spec
            getter = getattr(pipe, method_name, None)
            if not callable(getter):
                _record_error(state, method_name, "Not supported", stage_name)
                return False
            try:
                used_descriptors = getter(stage, False)
            except Exception as exc:
                _record_error(state, method_name, str(exc), stage_name)
                return False
            entries = _reflection_entries(reflection, reflection_attr)
            matched = False
            for used in used_descriptors or []:
                descriptor = getattr(used, "descriptor", None)
                if descriptor is None or not _resource_matches(
                        getattr(descriptor, "resource", None), target_id):
                    continue
                matched = True
                access = getattr(used, "access", None)
                index = _as_int(getattr(access, "index", 0))
                array_element = _as_int(getattr(access, "arrayElement", 0))
                is_direct = index == 0xFFFF
                name = getattr(entries[index], "name", None) if not is_direct and 0 <= index < len(entries) else None
                view = _describe_view(
                    (kind, stage_name, None if is_direct else index, array_element, name),
                    descriptor, resource_format)
                view["binding"]["direct_access"] = is_direct
                view["binding"]["descriptor_store"] = str(getattr(access, "descriptorStore", ""))
                view["binding"]["byte_offset"] = getattr(access, "byteOffset", None)
                view["binding"]["byte_size"] = getattr(access, "byteSize", None)
                view["binding"]["statically_unused"] = getattr(access, "staticallyUnused", None)
                if not is_direct and 0 <= index < len(entries):
                    view["binding"]["register"] = getattr(entries[index], "fixedBindNumber", None)
                    view["binding"]["register_space"] = getattr(entries[index], "fixedBindSetOrSpace", None)
                state["views"].append(view)
            return matched

        specs = (("srv", "GetReadOnlyResources", "readOnlyResources"),
                 ("uav", "GetReadWriteResources", "readWriteResources"))
        stages = list(Helpers.get_all_shader_stages())
        for name in ("Task", "Mesh", "Amplification"):
            stage = getattr(rd.ShaderStage, name, None)
            if stage is not None and stage not in stages:
                stages.append(stage)
        for stage in stages:
            stage_info = (stage, _enum_name(stage))
            reflection, reflection_error = None, None
            try:
                reflection = pipe.GetShaderReflection(stage)
            except Exception as exc:
                reflection_error = str(exc)
            matched = False
            for spec in specs:
                matched = collect(stage_info, reflection, spec) or matched
            if matched and reflection is None:
                _record_error(state, "reflection",
                              reflection_error or "Shader reflection unavailable",
                              stage_info[1])

    def _collect_output_views(self, pipe, target_id, resource_format, state):
        try:
            targets = pipe.GetOutputTargets()
        except Exception as exc:
            _record_error(state, "GetOutputTargets", str(exc))
            targets = []
        for index, descriptor in enumerate(targets or []):
            if descriptor is not None and _resource_matches(
                    getattr(descriptor, "resource", None), target_id):
                _append_unique(state["views"], _describe_view(
                    ("color", None, index, 0, None), descriptor, resource_format))
        try:
            depth = pipe.GetDepthTarget()
        except Exception as exc:
            _record_error(state, "GetDepthTarget", str(exc))
            return
        if depth is not None and _resource_matches(
                getattr(depth, "resource", None), target_id):
            _append_unique(state["views"], _describe_view(
                ("depth", None, 0, 0, None), depth, resource_format))
