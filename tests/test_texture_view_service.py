import importlib
import sys
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
        "renderdoc_extension.services.texture_view_service"
    )
    yield module
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)


def fmt(name, comp_type, count=4, width=1, fmt_type="Regular"):
    return SimpleNamespace(
        Name=lambda: name, compType=comp_type, type=fmt_type,
        compCount=count, compByteWidth=width,
        SRGBCorrected=lambda: comp_type == "UNormSRGB",
        Special=lambda: fmt_type != "Regular",
        BGRAOrder=lambda: False,
    )


def texture(resource_id="ResourceId::42", format=None, width=1024, height=512):
    return SimpleNamespace(
        resourceId=resource_id,
        format=format or fmt("R8G8B8A8_TYPELESS", "Typeless"),
        width=width, height=height, depth=1, arraysize=1, mips=1,
        msSamp=1, cubemap=False, type="TextureType.Texture2D",
        byteSize=width * height * 4,
    )


def swizzle(red="Red", green="Green", blue="Blue", alpha="Alpha"):
    return SimpleNamespace(red=red, green=green, blue=blue, alpha=alpha)


def descriptor(resource="ResourceId::42", format=None, descriptor_type="Image",
               first_mip=0, num_mips=1, first_slice=0, num_slices=1, swizzle=None):
    return SimpleNamespace(
        resource=resource, format=format or fmt("R8G8B8A8_UNORM", "UNorm"),
        type=descriptor_type, firstMip=first_mip, numMips=num_mips,
        firstSlice=first_slice, numSlices=num_slices, swizzle=swizzle,
    )


def used(descriptor, stage="pixel", index=0, array_element=0):
    return SimpleNamespace(
        descriptor=descriptor,
        access=SimpleNamespace(stage=stage, index=index, arrayElement=array_element),
    )


def entry(name, fixed_bind_number=0):
    return SimpleNamespace(name=name, fixedBindNumber=fixed_bind_number)


def reflection(readonly=(), readwrite=()):
    return SimpleNamespace(
        readOnlyResources=list(readonly),
        readWriteResources=list(readwrite),
    )


class Pipe:
    def __init__(self, reflections=None, readonly=None, readwrite=None,
                 color_targets=(), depth_target=None, readwrite_errors=None):
        self.reflections = reflections or {}
        self.readonly = readonly or {}
        self.readwrite = readwrite or {}
        self.color_targets = list(color_targets)
        self.depth_target = depth_target
        self.readwrite_errors = readwrite_errors or {}

    def GetShaderReflection(self, stage):
        return self.reflections.get(stage)

    def GetReadOnlyResources(self, stage, only_used):
        return list(self.readonly.get(stage, []))

    def GetReadWriteResources(self, stage, only_used):
        if stage in self.readwrite_errors:
            raise RuntimeError(self.readwrite_errors[stage])
        return list(self.readwrite.get(stage, []))

    def GetOutputTargets(self):
        return list(self.color_targets)

    def GetDepthTarget(self):
        return self.depth_target

    def GetDescriptors(self, *args):
        raise AssertionError("the service must only read reported bindings")


class Controller:
    def __init__(self, textures, pipe, bad_events=()):
        self.textures = list(textures)
        self.pipe = pipe
        self.bad_events = set(bad_events)
        self.frame_events = []

    def SetFrameEvent(self, event_id, force):
        if event_id in self.bad_events:
            raise RuntimeError("event does not exist")
        self.frame_events.append((event_id, force))

    def GetTextures(self):
        return list(self.textures)

    def GetPipelineState(self):
        return self.pipe


def run(api, controller, resource_id="ResourceId::42", event_id=123,
        current_event=7):
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: True,
                          CurEvent=lambda: current_event,
                          GetAction=lambda eid: SimpleNamespace(eventId=eid))
    service = api.TextureViewService(ctx, lambda callback: callback(controller))
    return service.get_texture_views(resource_id, event_id)


def test_describe_format_exposes_public_fields_and_typeless_srgb(api):
    typeless = fmt("R8G8B8A8_TYPELESS", "Typeless")
    srgb = fmt("R8G8B8A8_UNORM_SRGB", "UNormSRGB")
    unorm = fmt("R8G8B8A8_UNORM", "UNorm")
    block = fmt("BC1_UNORM", "UNorm", fmt_type="BC1")

    assert api.describe_format(None) is None
    assert api.describe_format(typeless) == {
        "name": "R8G8B8A8_TYPELESS", "component_type": "Typeless",
        "component_count": 4, "component_byte_width": 1, "type": "Regular",
        "srgb": None, "is_block_compressed": None, "is_unorm": None, "special": False, "bgra_order": False,
    }
    assert api.describe_format(srgb)["srgb"] is True
    assert api.describe_format(unorm)["srgb"] is False
    assert api.describe_format(block)["type"] == "BC1"


def test_reports_srv_uav_and_color_views_for_the_requested_texture(api):
    srv_format = fmt("R8G8B8A8_UNORM_SRGB", "UNormSRGB")
    uav_format = fmt("R8G8B8A8_UNORM", "UNorm")
    pipe = Pipe(
        reflections={"pixel": reflection(
            readonly=[entry("Albedo", 7)],
            readwrite=[entry("Unused", 9), entry("Output", 0)])},
        readonly={"pixel": [used(descriptor(
            format=srv_format, first_mip=2, num_mips=1,
            first_slice=1, num_slices=1, swizzle=swizzle()))]},
        readwrite={"pixel": [used(descriptor(
            format=uav_format, descriptor_type="ReadWriteImage"), index=1)]},
        color_targets=[
            descriptor(resource="ResourceId::99", format=srv_format),
            descriptor(format=srv_format, swizzle=swizzle()),
        ],
    )
    controller = Controller(
        [texture(format=fmt("R8G8B8A8_TYPELESS", "Typeless"))], pipe)

    result = run(api, controller)

    assert result["resource_id"] == "ResourceId::42"
    assert result["resource"]["format"]["name"] == "R8G8B8A8_TYPELESS"
    assert result["resource"]["format"]["srgb"] is None
    assert result["errors"] == []
    assert [view["kind"] for view in result["views"]] == ["srv", "uav", "color"]

    srv, uav, color = result["views"]
    assert srv["binding"] == {
        "stage": "pixel", "index": 0, "array_element": 0,
        "descriptor_type": "Image", "name": "Albedo", "direct_access": False, "descriptor_store": "",
        "byte_offset": None, "byte_size": None, "statically_unused": None,
        "register": 7, "register_space": None,
    }
    assert srv["view_format"]["name"] == "R8G8B8A8_UNORM_SRGB"
    assert srv["view_format"]["srgb"] is True
    assert srv["format_matches"] is False
    assert srv["format_differences"]["component_type"] == {
        "resource": "Typeless", "view": "UNormSRGB"}
    assert srv["mip"] == {"first": 2, "count": 1}
    assert srv["slice"] == {"first": 1, "count": 1}
    assert srv["swizzle"] == {
        "red": "Red", "green": "Green", "blue": "Blue", "alpha": "Alpha"}

    assert uav["binding"]["name"] == "Output"
    assert uav["view_format"]["srgb"] is False
    assert uav["format_differences"]["component_type"]["view"] == "UNorm"

    assert color["binding"]["stage"] is None
    assert color["binding"]["index"] == 1
    assert color["format_matches"] is False
    assert controller.frame_events == [(123, True), (7, True)]


def test_binding_name_uses_reflection_index_not_fixed_bind_number(api):
    pipe = Pipe(
        reflections={"pixel": reflection(
            readonly=[entry("IndexZero", 42), entry("IndexOne", 0)])},
        readonly={"pixel": [used(descriptor(), index=1)]},
    )

    result = run(api, Controller([texture()], pipe))

    assert result["views"][0]["binding"]["index"] == 1
    assert result["views"][0]["binding"]["name"] == "IndexOne"


def test_direct_heap_view_does_not_invent_a_shader_register(api):
    view = used(descriptor(), index=65535)
    view.access.descriptorStore = "ResourceId::100"
    view.access.byteOffset = 12
    view.access.byteSize = 1
    pipe = Pipe(readonly={"pixel": [view]})

    result = run(api, Controller([texture()], pipe))

    binding = result["views"][0]["binding"]
    assert binding["index"] is None
    assert binding["direct_access"] is True
    assert binding["descriptor_store"] == "ResourceId::100"
    assert binding["byte_offset"] == 12


def test_duplicate_reported_bindings_and_arrays_are_preserved(api):
    repeated = used(descriptor(), stage="pixel", index=0, array_element=0)
    array_one = used(descriptor(), stage="pixel", index=0, array_element=1)
    vertex = used(descriptor(), stage="vertex", index=0, array_element=0)
    pipe = Pipe(
        reflections={
            "pixel": reflection(readonly=[entry("Tex")]),
            "vertex": reflection(readonly=[entry("Tex")]),
        },
        readonly={"pixel": [repeated, repeated, array_one], "vertex": [vertex]},
    )

    result = run(api, Controller([texture()], pipe))

    srv = [view for view in result["views"] if view["kind"] == "srv"]
    assert [(view["binding"]["stage"], view["binding"]["array_element"])
            for view in srv] == [("vertex", 0), ("pixel", 0), ("pixel", 0), ("pixel", 1)]


def test_depth_target_reports_mip_slice_and_swizzle(api):
    depth_format = fmt("D32_FLOAT", "Depth", count=1, width=4)
    resource_format = fmt(
        "D32_FLOAT_S8X24_UINT", "Depth", count=1, width=4, fmt_type="D24S8")
    depth = descriptor(
        format=depth_format, first_mip=3, num_mips=1, first_slice=2,
        num_slices=1, swizzle=swizzle("Alpha", "Zero", "One", "Red"))
    pipe = Pipe(depth_target=depth)

    result = run(api, Controller([texture(format=resource_format)], pipe))

    assert result["errors"] == []
    assert [view["kind"] for view in result["views"]] == ["depth"]
    view = result["views"][0]
    assert view["binding"]["stage"] is None
    assert view["view_format"]["name"] == "D32_FLOAT"
    assert view["format_differences"]["type"] == {
        "resource": "D24S8", "view": "Regular"}
    assert view["mip"] == {"first": 3, "count": 1}
    assert view["slice"] == {"first": 2, "count": 1}
    assert view["swizzle"] == {
        "red": "Alpha", "green": "Zero", "blue": "One", "alpha": "Red"}


def test_partial_failures_keep_views_and_report_scopes(api):
    pipe = Pipe(
        reflections={},
        readonly={"pixel": [used(descriptor())]},
        readwrite_errors={"vertex": "vertex UAVs unavailable"},
    )

    result = run(api, Controller([texture()], pipe))

    assert [view["kind"] for view in result["views"]] == ["srv"]
    assert result["status"] == "partial"
    assert result["views"][0]["binding"]["name"] is None
    assert [(error["scope"], error.get("stage")) for error in result["errors"]] == [
        ("GetReadWriteResources", "vertex"),
        ("reflection", "pixel"),
    ]


def test_invalid_and_missing_resources_raise_value_error(api):
    controller = Controller([texture()], Pipe())

    with pytest.raises(ValueError, match="Invalid resource ID"):
        run(api, controller, resource_id="not-an-id")
    with pytest.raises(ValueError, match="Texture not found"):
        run(api, controller, resource_id="ResourceId::777")


def test_invalid_event_raises_without_restoring(api):
    controller = Controller([texture()], Pipe(), bad_events={999})

    with pytest.raises(ValueError, match="event does not exist"):
        run(api, controller, event_id=999)

    assert controller.frame_events == [(7, True)]


def test_requires_loaded_capture(api):
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: False, CurEvent=lambda: 0)
    service = api.TextureViewService(ctx, lambda callback: None)

    with pytest.raises(ValueError, match="No capture loaded"):
        service.get_texture_views("ResourceId::42", 1)
