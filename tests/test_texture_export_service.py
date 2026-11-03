import importlib
import struct
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
    renderdoc.FileType = SimpleNamespace(
        DDS="DDS", PNG="PNG", TGA="TGA", EXR="EXR")
    renderdoc.TextureSave = TextureSave
    monkeypatch.setitem(sys.modules, "renderdoc", renderdoc)
    monkeypatch.setitem(sys.modules, "renderdoc_extension", package)
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)
    module = importlib.import_module(
        "renderdoc_extension.services.texture_export_service")
    yield module
    for name in list(sys.modules):
        if name.startswith("renderdoc_extension."):
            monkeypatch.delitem(sys.modules, name)


class TextureSave:
    def __init__(self):
        self.resourceId = None
        self.destType = None
        self.mip = -1
        self.comp = SimpleNamespace(blackPoint=0.0, whitePoint=1.0)
        self.sample = SimpleNamespace(mapToArray=False, sampleIndex=0xFFFFFFFF)
        self.slice = SimpleNamespace(
            sliceIndex=-1, slicesAsGrid=False, sliceGridWidth=1,
            cubeCruciform=False)


def texture(resource_id="ResourceId::42", width=8, height=8, depth=1,
            arraysize=1, mips=1, msSamp=1, texture_type="TextureType.Texture2D"):
    return SimpleNamespace(
        resourceId=resource_id,
        format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM", BlockFormat=lambda: False,
                               compType="UNorm", type="Regular", compCount=4,
                               compByteWidth=1),
        width=width, height=height, depth=depth, arraysize=arraysize,
        mips=mips, msSamp=msSamp, type=texture_type,
        dimension=3 if texture_type.endswith("Texture3D") else 2)


class Controller:
    def __init__(self, textures, writer=None, bad_events=(), restore_error=None):
        self.textures = list(textures)
        self.writer = writer
        self.bad_events = set(bad_events)
        self.restore_error = restore_error
        self.calls = []
        self.frame_events = []

    def SetFrameEvent(self, event_id, force):
        if event_id in self.bad_events:
            raise RuntimeError("event does not exist")
        if self.restore_error is not None and self.frame_events and event_id == self.restore_error:
            raise RuntimeError("restore failed")
        self.frame_events.append((event_id, force))

    def GetTextures(self):
        return list(self.textures)

    def SaveTexture(self, save, path):
        self.calls.append((save, path))
        if self.writer is None:
            return SimpleNamespace(OK=lambda: True, Message=lambda: "")
        result = self.writer(save, path)
        return SimpleNamespace(OK=lambda: True, Message=lambda: "") if result is None else result


def run(api, controller, method="2d", resource_id="ResourceId::42", event_id=123,
        path=None, file_type="png", current_event=7, **kwargs):
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: True,
                          CurEvent=lambda: current_event,
                          GetAction=lambda eid: SimpleNamespace(eventId=eid))
    service = api.TextureExportService(ctx, lambda callback: callback(controller))
    if method == "2d":
        return service.export_texture_2d(
            resource_id, event_id, str(path), file_type, **kwargs)
    return service.export_texture_3d_atlas(
        resource_id, event_id, str(path), file_type, **kwargs)


def rgba_dds(width, height, fill):
    header = bytearray(124)
    struct.pack_into("<I", header, 0, 124)
    struct.pack_into("<I", header, 4, 0x1 | 0x2 | 0x4 | 0x8 | 0x1000)
    struct.pack_into("<I", header, 8, height)
    struct.pack_into("<I", header, 12, width)
    struct.pack_into("<I", header, 16, width * 4)
    struct.pack_into("<I", header, 20, 1)
    struct.pack_into("<I", header, 24, 1)
    struct.pack_into("<I", header, 72, 32)
    struct.pack_into("<I", header, 76, 0x41)
    struct.pack_into("<I", header, 84, 32)
    struct.pack_into("<I", header, 88, 0xFF)
    struct.pack_into("<I", header, 92, 0xFF00)
    struct.pack_into("<I", header, 96, 0xFF0000)
    struct.pack_into("<I", header, 100, 0xFF000000)
    struct.pack_into("<I", header, 104, 0x1000)
    return b"DDS " + bytes(header) + bytes([fill]) * (width * height * 4)


def test_requires_loaded_capture(api, tmp_path):
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: False, CurEvent=lambda: 0)
    service = api.TextureExportService(ctx, lambda callback: None)

    with pytest.raises(ValueError, match="No capture loaded"):
        service.export_texture_2d(
            "ResourceId::42", 1, str(tmp_path / "a.png"), "png")


def test_2d_export_uses_selectors_and_restores_event(api, tmp_path):
    output = tmp_path / "texture.png"
    controller = Controller(
        [texture(width=64, height=32, mips=3)],
        writer=lambda save, path: Path(path).write_bytes(b"png") and None)

    result = run(api, controller, path=output, mip=1, array_slice=0, sample=0)

    save, written_path = controller.calls[0]
    assert Path(written_path).parent == output.parent
    assert save.destType == "PNG"
    assert save.mip == 1
    assert save.slice.sliceIndex == 0
    assert save.slice.slicesAsGrid is False
    assert save.sample.sampleIndex == 0
    assert controller.frame_events == [(123, True), (7, True)]
    assert output.read_bytes() == b"png"
    assert result["width"] == 32 and result["height"] == 16
    assert result["file_type"] == "png"
    assert result["format"] == "R8G8B8A8_UNORM"
    assert result["size_bytes"] == 3
    assert result["written"] is True


def test_2d_selectors_allow_msaa_sample_and_array_slice(api, tmp_path):
    controller = Controller(
        [texture(width=4, height=4, arraysize=2, msSamp=4)],
        writer=lambda save, path: Path(path).write_bytes(b"x") and None)

    result = run(api, controller, path=tmp_path / "msaa.png",
                 mip=0, array_slice=1, sample=3)

    save = controller.calls[0][0]
    assert save.slice.sliceIndex == 1
    assert save.sample.sampleIndex == 3
    assert result["array_slice"] == 1 and result["sample"] == 3


def test_selector_validation_rejects_unsafe_values(api, tmp_path):
    two_d = Controller([texture(width=8, height=8, mips=2)])
    with pytest.raises(ValueError, match="Invalid mip"):
        run(api, two_d, path=tmp_path / "a.png", mip=2)
    with pytest.raises(ValueError, match="Invalid array slice"):
        run(api, two_d, path=tmp_path / "b.png", array_slice=1)
    with pytest.raises(ValueError, match="Invalid sample"):
        run(api, two_d, path=tmp_path / "c.png", sample=1)

    msaa = Controller([texture(width=4, height=4, mips=2, msSamp=4)])
    with pytest.raises(ValueError, match="only have mip 0"):
        run(api, msaa, path=tmp_path / "d.png", mip=1)

    three_d = Controller([texture(
        width=8, height=8, depth=8, mips=4,
        texture_type="TextureType.Texture3D")])
    with pytest.raises(ValueError, match="requires a 2D texture"):
        run(api, three_d, path=tmp_path / "e.png", mip=2, array_slice=2)
    assert two_d.calls == [] and msaa.calls == [] and three_d.calls == []


def test_invalid_event_and_missing_resource_are_reported(api, tmp_path):
    bad_event = Controller([texture()], bad_events={999})
    with pytest.raises(ValueError, match="Invalid event ID"):
        run(api, bad_event, path=tmp_path / "a.png", event_id=999)
    assert bad_event.frame_events == [(7, True)]
    assert not (tmp_path / "a.png").exists()

    missing = Controller([texture()])
    with pytest.raises(ValueError, match="Texture not found"):
        run(api, missing, path=tmp_path / "b.png", resource_id="ResourceId::777")
    assert missing.frame_events == [(123, True), (7, True)]


def test_2d_export_rejects_3d_texture(api, tmp_path):
    controller = Controller(
        [texture(width=8, height=8, depth=8, mips=4,
                 texture_type="TextureType.Texture3D")],
        writer=lambda save, path: Path(path).write_bytes(b"x") and None)

    with pytest.raises(ValueError, match="requires a 2D texture"):
        run(api, controller, path=tmp_path / "slice.exr", mip=2,
            array_slice=1, file_type="exr")
    assert controller.calls == []


def test_native_3d_atlas_grid_mip_zero(api, tmp_path):
    output = tmp_path / "atlas.tga"
    controller = Controller(
        [texture(width=8, height=4, depth=5, mips=1,
                 texture_type="TextureType.Texture3D")],
        writer=lambda save, path: Path(path).write_bytes(b"atlas") and None)

    result = run(api, controller, method="atlas", path=output,
                 file_type="tga", columns=2)

    save = controller.calls[0][0]
    assert save.mip == 0
    assert save.slice.sliceIndex == -1
    assert save.slice.slicesAsGrid is True
    assert save.slice.sliceGridWidth == 2
    assert controller.frame_events == [(123, True), (7, True)]
    assert result["width"] == 16 and result["height"] == 12
    assert result["columns"] == 2 and result["rows"] == 3
    assert result["slice_count"] == 5 and result["padding_slices"] == 1
    assert result["atlas_mode"] == "native-grid"
    assert result["size_bytes"] == 5


def test_native_3d_atlas_nonzero_mip_is_explicitly_unsupported(api, tmp_path):
    output = tmp_path / "atlas.png"
    controller = Controller(
        [texture(width=8, height=8, depth=8, mips=3,
                 texture_type="TextureType.Texture3D")])

    with pytest.raises(ValueError, match="mip 0"):
        run(api, controller, method="atlas", path=output, mip=1)

    assert controller.calls == []
    assert not output.exists()


def test_atlas_requires_a_3d_texture(api, tmp_path):
    controller = Controller([texture()])

    with pytest.raises(ValueError, match="requires a 3D texture"):
        run(api, controller, method="atlas", path=tmp_path / "atlas.png")


def test_tga_dimensions_and_output_paths_are_validated(api, tmp_path):
    wide = Controller([texture(width=70000, height=1)])
    with pytest.raises(ValueError, match="TGA"):
        run(api, wide, path=tmp_path / "a.tga", file_type="tga")
    assert wide.calls == []

    with pytest.raises(ValueError, match="absolute"):
        run(api, wide, path="relative.png")
    with pytest.raises(ValueError, match="parent directory"):
        run(api, wide, path=tmp_path / "missing" / "a.png")
    with pytest.raises(ValueError, match="extension"):
        run(api, wide, path=tmp_path / "a.dds", file_type="png")
    with pytest.raises(ValueError, match="Unsupported file type"):
        run(api, wide, path=tmp_path / "a.bmp", file_type="bmp")


def test_existing_output_is_never_overwritten(api, tmp_path):
    output = tmp_path / "keep.png"
    output.write_bytes(b"keep")
    controller = Controller(
        [texture()], writer=lambda save, path: Path(path).write_bytes(b"new") and None)

    with pytest.raises(ValueError, match="already exists"):
        run(api, controller, path=output)

    assert output.read_bytes() == b"keep"
    assert controller.calls == []


def test_native_failure_removes_reserved_output(api, tmp_path):
    output = tmp_path / "failed.png"

    def fail(save, path):
        Path(path).write_bytes(b"partial")
        return SimpleNamespace(OK=lambda: False, Message=lambda: "disk full")

    controller = Controller([texture()], writer=fail)

    with pytest.raises(ValueError, match="disk full"):
        run(api, controller, path=output)

    assert not output.exists()
    assert controller.frame_events == [(123, True), (7, True)]


def test_callback_exceptions_are_raised_outside_and_clean_up(api, tmp_path):
    output = tmp_path / "boom.png"

    def explode(save, path):
        raise RuntimeError("boom")

    controller = Controller([texture()], writer=explode)

    with pytest.raises(ValueError, match="boom"):
        run(api, controller, path=output)

    assert not output.exists()


def test_dds_atlas_is_repacked_as_a_flattened_2d_texture(api, tmp_path):
    output = tmp_path / "atlas.dds"

    def write_slice(save, path):
        Path(path).write_bytes(
            rgba_dds(2, 2, save.slice.sliceIndex + 1))
        return None

    controller = Controller(
        [texture(width=2, height=2, depth=3, mips=1,
                 texture_type="TextureType.Texture3D")],
        writer=write_slice)

    result = run(api, controller, method="atlas", path=output,
                 file_type="dds", columns=2)

    assert len(controller.calls) == 3
    assert [(save.mip, save.slice.sliceIndex) for save, _ in controller.calls] == [
        (0, 0), (0, 1), (0, 2)]
    raw = output.read_bytes()
    assert raw[:4] == b"DDS "
    flags = struct.unpack_from("<I", raw, 8)[0]
    assert flags & 0x800000 == 0
    assert flags & 0x8
    assert struct.unpack_from("<I", raw, 12)[0] == 4
    assert struct.unpack_from("<I", raw, 16)[0] == 4
    assert struct.unpack_from("<I", raw, 20)[0] == 16
    assert struct.unpack_from("<I", raw, 24)[0] == 1
    assert struct.unpack_from("<I", raw, 4)[0] == 124
    data = raw[128:]
    assert len(data) == 64
    assert data[0:8] == b"\x01" * 8
    assert data[8:16] == b"\x02" * 8
    assert data[16:24] == b"\x01" * 8
    assert data[24:32] == b"\x02" * 8
    assert data[32:40] == b"\x03" * 8
    assert data[40:48] == b"\x00" * 8
    assert data[48:56] == b"\x03" * 8
    assert data[56:64] == b"\x00" * 8
    assert result["width"] == 4 and result["height"] == 4
    assert result["slice_count"] == 3
    assert result["atlas_mode"] == "dds-repack"
    assert result["size_bytes"] == len(raw)


def test_dds_atlas_rejects_nonzero_mip(api, tmp_path):
    output = tmp_path / "mip.dds"

    def write_slice(save, path):
        Path(path).write_bytes(
            rgba_dds(2, 1, save.slice.sliceIndex + 1))
        return None

    controller = Controller(
        [texture(width=8, height=4, depth=8, mips=3,
                 texture_type="TextureType.Texture3D")],
        writer=write_slice)

    with pytest.raises(ValueError, match="nonzero mips"):
        run(api, controller, method="atlas", path=output,
            file_type="dds", mip=2, columns=1)
    assert controller.calls == []


def test_dds_atlas_rejects_truncated_slice_payload(api, tmp_path):
    output = tmp_path / "truncated.dds"

    def write_slice(save, path):
        Path(path).write_bytes(rgba_dds(4, 4, 1)[:-32])
        return None

    controller = Controller(
        [texture(width=4, height=4, depth=2, texture_type="TextureType.Texture3D")],
        writer=write_slice)

    with pytest.raises(ValueError, match="payload size"):
        run(api, controller, method="atlas", path=output, file_type="dds", columns=2)
    assert not output.exists()


def test_restore_failure_is_reported_and_output_removed(api, tmp_path):
    output = tmp_path / "restore.png"
    controller = Controller(
        [texture()],
        writer=lambda save, path: Path(path).write_bytes(b"x") and None,
        restore_error=7)

    with pytest.raises(ValueError, match="restore"):
        run(api, controller, path=output)

    assert not output.exists()
