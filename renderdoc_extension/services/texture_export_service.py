"""Texture export to image files, including safe 3D volume atlases."""

import os
import shutil
import struct
import tempfile

from .texture_view_service import describe_format

_FILE_TYPES = frozenset(("dds", "png", "tga", "exr"))
_TGA_LIMIT = 65535
_MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
_DDS_MAGIC = b"DDS "
_DDSD_PITCH = 0x00000008
_DDSD_MIPMAPCOUNT = 0x00020000
_DDSD_LINEARSIZE = 0x00080000
_DDSD_DEPTH = 0x00800000
_DDSCAPS_TEXTURE = 0x00001000


class _ExportFailure(ValueError):
    """A validation or native export failure reported to the caller."""


def _renderdoc():
    import renderdoc

    return renderdoc


def _enum_name(value):
    return None if value is None else str(value).rsplit(".", 1)[-1]


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _numeric_id(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not an ID")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError("floating point is not an ID")
    text = str(value).strip()
    if "::" in text:
        text = text.rsplit("::", 1)[1]
    return int(text)


def _parse_id(value, label):
    try:
        numeric = _numeric_id(value)
    except (TypeError, ValueError):
        raise ValueError("Invalid %s ID: %s" % (label, value))
    if numeric < 0:
        raise ValueError("Invalid %s ID: %s" % (label, value))
    return numeric


def _parse_index(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Invalid %s: %s" % (label, value))
    number = value
    if number < 0:
        raise ValueError("Invalid %s: %s" % (label, value))
    return number


def _parse_file_type(value):
    if not isinstance(value, str):
        raise ValueError("Invalid file type: %s" % value)
    name = value.strip().lower()
    if name.startswith("."):
        name = name[1:]
    if name not in _FILE_TYPES:
        raise ValueError(
            "Unsupported file type: %s (expected dds, png, tga, or exr)" % value)
    return name


def _prepare_output_path(output_path, file_type):
    try:
        path = os.fsdecode(os.fspath(output_path))
    except TypeError:
        raise ValueError("output_path must be a filesystem path")
    if not path or not os.path.isabs(path):
        raise ValueError("output_path must be an absolute path: %s" % path)
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if not os.path.isabs(parent) or not os.path.isdir(parent):
        raise ValueError(
            "output_path parent directory does not exist: %s" % parent)
    extension = os.path.splitext(path)[1].lower()
    if extension != "." + file_type:
        raise ValueError(
            "output_path extension %s does not match file type %s"
            % (extension or "(none)", file_type))
    if os.path.lexists(path):
        raise ValueError(
            "Output file already exists and will not be overwritten: %s" % path)
    return path


def _remove_output(path):
    if os.path.lexists(path):
        os.remove(path)


def _mip_dimensions(texture, mip):
    width = max(1, _as_int(getattr(texture, "width", 0), 0))
    height = max(1, _as_int(getattr(texture, "height", 0), 0))
    depth = max(1, _as_int(getattr(texture, "depth", 1), 1))
    array_size = max(1, _as_int(getattr(texture, "arraysize", 1), 1))
    mip_width = max(1, width >> mip)
    mip_height = max(1, height >> mip)
    mip_depth = max(1, depth >> mip)
    return mip_width, mip_height, array_size, mip_depth


def _texture_type(texture):
    return _enum_name(getattr(texture, "type", None)) or ""


def _texture_dimension(texture):
    return texture.dimension


def _format_name(texture):
    return str(texture.format.Name())


def _resource_text(texture, target_id):
    value = getattr(texture, "resourceId", None)
    return str(value) if value is not None else "ResourceId::%s" % target_id


def _resource_matches(value, target_id):
    try:
        return _numeric_id(value) == target_id
    except (TypeError, ValueError):
        return False


def _validate_mip(texture, mip):
    mip_count = max(1, _as_int(getattr(texture, "mips", 1), 1))
    if mip >= mip_count:
        raise _ExportFailure(
            "Invalid mip level %d: texture has %d mip level(s)"
            % (mip, mip_count))


def _validate_2d_selectors(texture, mip, array_slice, sample):
    _validate_mip(texture, mip)
    width, height, array_size, mip_depth = _mip_dimensions(texture, mip)
    slice_count = array_size * mip_depth
    if array_slice >= slice_count:
        raise _ExportFailure(
            "Invalid array slice %d: texture has %d slice(s) at mip %d"
            % (array_slice, slice_count, mip))
    samples = max(1, _as_int(getattr(texture, "msSamp", 1), 1))
    if sample >= samples:
        raise _ExportFailure(
            "Invalid sample %d: texture has %d sample(s)" % (sample, samples))
    if samples > 1 and mip != 0:
        raise _ExportFailure(
            "Multisampled textures only have mip 0; requested mip %d" % mip)
    return width, height


def _validate_atlas_selectors(texture, mip, columns):
    _validate_mip(texture, mip)
    if _texture_dimension(texture) != 3:
        raise _ExportFailure(
            "export_texture_3d_atlas requires a 3D texture, got %s"
            % (_texture_type(texture) or "an unknown texture type"))
    width, height, array_size, mip_depth = _mip_dimensions(texture, mip)
    if array_size != 1:
        raise _ExportFailure("3D texture arrays are not supported for atlases")
    if max(1, _as_int(getattr(texture, "msSamp", 1), 1)) > 1:
        raise _ExportFailure(
            "Multisampled 3D textures are not supported for atlases")
    if columns < 1:
        raise _ExportFailure("columns must be at least 1")
    rows = (mip_depth + columns - 1) // columns
    return width, height, mip_depth, rows


def _check_budget(width, height, file_type, multiplier=1):
    if width < 1 or height < 1:
        raise _ExportFailure("Output image dimensions must be positive")
    if file_type == "tga" and (width > _TGA_LIMIT or height > _TGA_LIMIT):
        raise _ExportFailure(
            "TGA dimensions %dx%d exceed the 65535x65535 limit"
            % (width, height))
    bytes_per_pixel = 16 if file_type in ("dds", "exr") else 4
    required = width * height * bytes_per_pixel * multiplier
    if required > _MAX_OUTPUT_BYTES:
        raise _ExportFailure(
            "Output image %dx%d requires %d bytes, above the %d byte budget"
            % (width, height, required, _MAX_OUTPUT_BYTES))


def _file_type_value(name):
    file_types = getattr(_renderdoc(), "FileType", None)
    if file_types is None:
        raise _ExportFailure("RenderDoc FileType is unavailable")
    for attribute in (name.upper(), name):
        if hasattr(file_types, attribute):
            return getattr(file_types, attribute)
    raise _ExportFailure("RenderDoc does not expose FileType.%s" % name.upper())


def _new_save(texture, target_id, file_type, mip, slice_index, grid, columns,
              sample=0):
    renderdoc = _renderdoc()
    factory = getattr(renderdoc, "TextureSave", None)
    if factory is None:
        raise _ExportFailure("RenderDoc TextureSave is unavailable")
    save = factory()
    save.resourceId = getattr(texture, "resourceId", "ResourceId::%s" % target_id)
    save.destType = _file_type_value(file_type)
    save.mip = mip
    save.slice.sliceIndex = slice_index
    save.slice.slicesAsGrid = bool(grid)
    save.slice.sliceGridWidth = int(columns)
    save.slice.cubeCruciform = False
    save.sample.mapToArray = False
    save.sample.sampleIndex = int(sample)
    return save


def _result_message(result):
    for attribute in ("Message", "message"):
        value = getattr(result, attribute, None)
        if callable(value):
            try:
                return str(value())
            except Exception:
                continue
        if value is not None:
            return str(value)
    code = getattr(result, "code", None)
    return str(code) if code is not None else str(result)


def _check_native_result(result, description):
    if result is None:
        raise _ExportFailure("%s returned no result" % description)
    if not result.OK():
        raise _ExportFailure(
            "%s failed: %s" % (description, str(result.Message())))


def _save_texture(controller, save, path, description):
    saver = getattr(controller, "SaveTexture", None)
    if not callable(saver):
        raise _ExportFailure("Replay controller does not support SaveTexture")
    try:
        result = saver(save, path)
    except Exception as exc:
        raise _ExportFailure(
            "%s raised %s: %s" % (description, type(exc).__name__, exc))
    _check_native_result(result, description)


def _verify_output(path):
    if not os.path.isfile(path):
        raise _ExportFailure(
            "SaveTexture reported success but no output file was written: %s"
            % path)
    size = os.path.getsize(path)
    if size <= 0:
        raise _ExportFailure("Output file is empty: %s" % path)
    return size


def _publish_output(temporary, path):
    try:
        os.link(temporary, path)
    except FileExistsError:
        raise ValueError(
            "Output file already exists and will not be overwritten: %s" % path)
    except OSError as exc:
        raise ValueError("Could not finalize output file %s: %s" % (path, exc))


def _find_texture(controller, target_id):
    textures = controller.GetTextures()
    for texture in textures or []:
        if _resource_matches(getattr(texture, "resourceId", None), target_id):
            return texture
    return None


def _restore_event(controller, original_event, state):
    if original_event is None:
        return
    try:
        controller.SetFrameEvent(original_event, True)
    except Exception as exc:
        state["restore_error"] = (
            "Failed to restore replay event %s: %s" % (original_event, exc))


def _read_dds_slice(path, width, height, index):
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise _ExportFailure(
            "Could not read DDS slice %d: %s" % (index, exc))
    if len(raw) < 128 or raw[:4] != _DDS_MAGIC:
        raise _ExportFailure("SaveTexture wrote an invalid DDS for slice %d" % index)
    header = bytearray(raw[4:128])
    if struct.unpack_from("<I", header, 0)[0] != 124 or struct.unpack_from("<I", header, 72)[0] != 32:
        raise _ExportFailure("Invalid DDS header for slice %d" % index)
    slice_width = struct.unpack_from("<I", header, 12)[0]
    slice_height = struct.unpack_from("<I", header, 8)[0]
    depth = struct.unpack_from("<I", header, 20)[0]
    mips = struct.unpack_from("<I", header, 24)[0]
    if slice_width != width or slice_height != height:
        raise _ExportFailure(
            "DDS slice %d is %dx%d, expected %dx%d"
            % (index, slice_width, slice_height, width, height))
    if depth > 1 or mips > 1:
        raise _ExportFailure(
            "DDS slice %d unexpectedly contains multiple mips or depth slices"
            % index)
    fourcc = bytes(header[80:84])
    dx10 = fourcc == b"DX10"
    offset = 128 + (20 if dx10 else 0)
    if len(raw) <= offset:
        raise _ExportFailure("DDS slice %d has no pixel data" % index)
    if dx10:
        dxgi, dimension, misc, array_size, _alpha = struct.unpack_from("<5I", raw, 128)
        if dimension != 3 or array_size != 1 or misc & 4:
            raise _ExportFailure("DDS slice %d is not a single 2D image" % index)
        widths = {2: 16, 10: 8, 28: 4, 29: 4, 87: 4, 91: 4}
        block_widths = {71: 8, 72: 8, 80: 8, 81: 8,
                        74: 16, 75: 16, 77: 16, 78: 16, 83: 16, 84: 16,
                        95: 16, 96: 16, 98: 16, 99: 16}
        pixel_bytes = widths.get(dxgi)
        block_bytes = block_widths.get(dxgi)
    else:
        blocks = {b"DXT1": 8, b"BC4U": 8, b"BC4S": 8,
                  b"DXT3": 16, b"DXT5": 16, b"ATI2": 16, b"BC5S": 16}
        block_bytes = blocks.get(fourcc)
        bits = struct.unpack_from("<I", header, 84)[0]
        pixel_bytes = bits // 8 if fourcc == b"\x00\x00\x00\x00" and bits in (8, 16, 32, 64, 128) else None
    if block_bytes is not None:
        if not (struct.unpack_from("<I", header, 4)[0] & _DDSD_LINEARSIZE):
            raise _ExportFailure("DDS block format lacks linear-size flag for slice %d" % index)
        expected = ((width + 3) // 4) * ((height + 3) // 4) * block_bytes
    elif pixel_bytes is not None:
        if struct.unpack_from("<I", header, 4)[0] & _DDSD_LINEARSIZE:
            raise _ExportFailure("Unexpected DDS linear-size flag for slice %d" % index)
        expected = width * height * pixel_bytes
    else:
        raise _ExportFailure("Unsupported DDS format for slice %d" % index)
    if len(raw) - offset != expected:
        raise _ExportFailure("Unexpected DDS payload size for slice %d" % index)
    return {
        "header": header,
        "dx10": dx10,
        "dx10_header": bytearray(raw[128:offset]),
        "payload": raw[offset:],
    }


def _compose_dds_atlas(parts, atlas_width, atlas_height, columns, rows,
                       slice_width, slice_height):
    first = parts[0]
    for part in parts[1:]:
        if bytes(part["header"][72:104]) != bytes(first["header"][72:104]):
            raise _ExportFailure("DDS slices use inconsistent pixel formats")
        if part["dx10"] != first["dx10"]:
            raise _ExportFailure("DDS slices use inconsistent DDS headers")
        if part["dx10"] and (part["dx10_header"][0:4] !=
                             first["dx10_header"][0:4]):
            raise _ExportFailure("DDS slices use inconsistent DXGI formats")

    flags = struct.unpack_from("<I", first["header"], 4)[0]
    block_format = bool(flags & _DDSD_LINEARSIZE)
    payload = first["payload"]
    if block_format:
        if slice_width % 4 or slice_height % 4:
            raise _ExportFailure(
                "Block-compressed DDS atlases require slice dimensions that are "
                "multiples of 4 (got %dx%d)" % (slice_width, slice_height))
        block_rows = slice_height // 4
        if not payload or len(payload) % block_rows:
            raise _ExportFailure("DDS slice has an invalid block layout")
        src_row_bytes = len(payload) // block_rows
        if src_row_bytes % (slice_width // 4):
            raise _ExportFailure("DDS slice has an invalid block pitch")
        atlas_row_bytes = (atlas_width // 4) * (
            src_row_bytes // (slice_width // 4))
        atlas_payload = bytearray(atlas_row_bytes * (atlas_height // 4))
        source_rows = block_rows
    else:
        if not payload or len(payload) % slice_height:
            raise _ExportFailure("DDS slice has an invalid row layout")
        src_row_bytes = len(payload) // slice_height
        if src_row_bytes % slice_width:
            raise _ExportFailure("DDS slice has an invalid pixel pitch")
        atlas_row_bytes = atlas_width * (src_row_bytes // slice_width)
        atlas_payload = bytearray(atlas_row_bytes * atlas_height)
        source_rows = slice_height

    for index, part in enumerate(parts):
        source = part["payload"]
        if len(source) != src_row_bytes * source_rows:
            raise _ExportFailure(
                "DDS slice %d has an unexpected byte count" % index)
        grid_x = index % columns
        grid_y = index // columns
        for row in range(source_rows):
            destination = ((grid_y * source_rows + row) * atlas_row_bytes +
                           grid_x * src_row_bytes)
            start = row * src_row_bytes
            atlas_payload[destination:destination + src_row_bytes] = (
                source[start:start + src_row_bytes])

    header = bytearray(first["header"])
    header_flags = struct.unpack_from("<I", header, 4)[0]
    header_flags &= ~(_DDSD_DEPTH | _DDSD_MIPMAPCOUNT)
    header_flags &= ~(_DDSD_PITCH | _DDSD_LINEARSIZE)
    header_flags |= _DDSD_LINEARSIZE if block_format else _DDSD_PITCH
    struct.pack_into("<I", header, 4, header_flags)
    struct.pack_into("<I", header, 8, atlas_height)
    struct.pack_into("<I", header, 12, atlas_width)
    struct.pack_into("<I", header, 16,
                     len(atlas_payload) if block_format else atlas_row_bytes)
    struct.pack_into("<I", header, 20, 1)
    struct.pack_into("<I", header, 24, 1)
    struct.pack_into("<I", header, 104, _DDSCAPS_TEXTURE)
    struct.pack_into("<I", header, 108, 0)

    dx10_header = None
    if first["dx10"]:
        dx10_header = bytearray(first["dx10_header"])
        struct.pack_into("<I", dx10_header, 4, 3)
        struct.pack_into("<I", dx10_header, 8, 0)
        struct.pack_into("<I", dx10_header, 12, 1)
        struct.pack_into("<I", dx10_header, 16, 0)
    return _DDS_MAGIC + bytes(header) + (
        bytes(dx10_header) if dx10_header is not None else b"") + bytes(
            atlas_payload)


class TextureExportService:
    """Export 2D subresources and safe 3D volume atlases with SaveTexture."""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def export_texture_2d(self, resource_id, event_id, output_path, file_type,
                          mip=0, array_slice=0, sample=0):
        """Write one 2D subresource to an image file."""
        self._require_capture()
        target_id = _parse_id(resource_id, "resource")
        event = _parse_index(event_id, "event ID")
        self._validate_event(event)
        mip = _parse_index(mip, "mip")
        array_slice = _parse_index(array_slice, "array slice")
        sample = _parse_index(sample, "sample")
        file_type = _parse_file_type(file_type)
        path = _prepare_output_path(output_path, file_type)
        original_event = self.ctx.CurEvent()
        fd, temporary = tempfile.mkstemp(prefix=".renderdoc_export_", suffix="." + file_type,
                                         dir=os.path.dirname(path))
        os.close(fd)
        state = {"fatal": None, "restore_error": None, "metadata": None,
                 "original_event": original_event}
        try:
            def callback(controller):
                self._export_2d(controller, target_id, event, mip, array_slice,
                                sample, file_type, temporary, state)
            self._invoke_checked(callback)
            self._raise_state_errors(state)
            size = _verify_output(temporary)
            _publish_output(temporary, path)
            state["metadata"]["output_path"] = path
            return self._finish_metadata(state["metadata"], size)
        finally:
            _remove_output(temporary)

    def export_texture_3d_atlas(self, resource_id, event_id, output_path,
                                file_type, mip=0, columns=8):
        """Write a 3D volume as a flattened 2D atlas grid."""
        self._require_capture()
        target_id = _parse_id(resource_id, "resource")
        event = _parse_index(event_id, "event ID")
        self._validate_event(event)
        mip = _parse_index(mip, "mip")
        columns = _parse_index(columns, "columns")
        file_type = _parse_file_type(file_type)
        path = _prepare_output_path(output_path, file_type)
        original_event = self.ctx.CurEvent()
        fd, temporary = tempfile.mkstemp(prefix=".renderdoc_export_", suffix="." + file_type,
                                         dir=os.path.dirname(path))
        os.close(fd)
        state = {"fatal": None, "restore_error": None, "metadata": None,
                 "original_event": original_event}
        try:
            def callback(controller):
                self._export_atlas(controller, target_id, event, mip, columns,
                                   file_type, temporary, state)
            self._invoke_checked(callback)
            self._raise_state_errors(state)
            size = _verify_output(temporary)
            _publish_output(temporary, path)
            state["metadata"]["output_path"] = path
            return self._finish_metadata(state["metadata"], size)
        finally:
            _remove_output(temporary)

    def _require_capture(self):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

    def _validate_event(self, event):
        action = self.ctx.GetAction(event)
        if action is None or action.eventId != event:
            raise ValueError("Event %d is not an action" % event)

    def _invoke_checked(self, callback):
        try:
            self._invoke(callback)
        except Exception as exc:
            raise ValueError(
                "Replay invocation failed: %s: %s"
                % (type(exc).__name__, exc))

    @staticmethod
    def _raise_state_errors(state):
        if state["fatal"]:
            raise ValueError(state["fatal"])
        if state["restore_error"]:
            raise ValueError(state["restore_error"])

    @staticmethod
    def _finish_metadata(metadata, size):
        metadata = dict(metadata or {})
        metadata["size_bytes"] = size
        metadata["bytes_written"] = size
        metadata["written"] = True
        return metadata

    def _export_2d(self, controller, target_id, event, mip, array_slice, sample,
                   file_type, path, state):
        original_event = state["original_event"]
        applied = False
        try:
            try:
                applied = True
                controller.SetFrameEvent(event, True)
            except Exception as exc:
                raise _ExportFailure("Invalid event ID %s: %s" % (event, exc))
            texture = _find_texture(controller, target_id)
            if texture is None:
                raise _ExportFailure(
                    "Texture not found: ResourceId::%s" % target_id)
            if _texture_dimension(texture) != 2:
                raise _ExportFailure("export_texture_2d requires a 2D texture")
            width, height = _validate_2d_selectors(
                texture, mip, array_slice, sample)
            _check_budget(width, height, file_type)
            save = _new_save(texture, target_id, file_type, mip, array_slice,
                             False, 1, sample)
            _save_texture(controller, save, path,
                          "SaveTexture for ResourceId::%s" % target_id)
            state["metadata"] = {
                "resource_id": _resource_text(texture, target_id),
                "event_id": event,
                "output_path": path,
                "file_type": file_type,
                "format": _format_name(texture),
                "resource_format": describe_format(texture.format),
                "interpretation": "resource",
                "conversion": "RenderDoc SaveTexture may reinterpret or convert the source format",
                "dimension": _texture_type(texture),
                "mip": mip,
                "array_slice": array_slice,
                "sample": sample,
                "width": width,
                "height": height,
                "depth": 1,
            }
        except _ExportFailure as exc:
            state["fatal"] = state["fatal"] or str(exc)
        except Exception as exc:
            state["fatal"] = state["fatal"] or (
                "Failed to export texture: %s: %s"
                % (type(exc).__name__, exc))
        finally:
            if applied:
                _restore_event(controller, original_event, state)

    def _export_atlas(self, controller, target_id, event, mip, columns,
                      file_type, path, state):
        original_event = state["original_event"]
        applied = False
        try:
            try:
                applied = True
                controller.SetFrameEvent(event, True)
            except Exception as exc:
                raise _ExportFailure("Invalid event ID %s: %s" % (event, exc))
            texture = _find_texture(controller, target_id)
            if texture is None:
                raise _ExportFailure(
                    "Texture not found: ResourceId::%s" % target_id)
            width, height, slices, rows = _validate_atlas_selectors(
                texture, mip, columns)
            atlas_width = width * columns
            atlas_height = height * rows
            if slices == 1 and columns != 1:
                raise _ExportFailure("Single-slice 3D atlases require columns=1")
            _check_budget(atlas_width, atlas_height, file_type,
                          multiplier=1 if file_type == "dds" else 2)
            _check_budget(width, height * slices, file_type, multiplier=2)
            if file_type == "dds" and mip != 0:
                raise _ExportFailure("3D DDS atlases at nonzero mips are unsupported by RenderDoc 1.48")
            if file_type == "dds":
                if texture.format.BlockFormat() and (width % 4 or height % 4):
                    raise _ExportFailure("Block-compressed DDS atlas slices must be multiples of 4")
                mode = "dds-repack"
                self._write_dds_atlas(
                    controller, texture, target_id, mip, columns, rows,
                    slices, width, height, atlas_width, atlas_height, path)
            elif mip != 0:
                raise _ExportFailure(
                    "3D %s atlases at mip %d are not supported: RenderDoc's "
                    "native slice-grid export is only safe for mip 0. Export "
                    "mip 0." % (file_type.upper(), mip))
            else:
                mode = "native-grid"
                save = _new_save(texture, target_id, file_type, 0, -1, True,
                                 columns, 0)
                _save_texture(
                    controller, save, path,
                    "SaveTexture 3D atlas for ResourceId::%s" % target_id)
            state["metadata"] = {
                "resource_id": _resource_text(texture, target_id),
                "event_id": event,
                "output_path": path,
                "file_type": file_type,
                "format": _format_name(texture),
                "resource_format": describe_format(texture.format),
                "interpretation": "resource",
                "conversion": "Grid export may reinterpret or convert the source format",
                "dimension": _texture_type(texture),
                "mip": mip,
                "columns": columns,
                "rows": rows,
                "slice_count": slices,
                "slices": slices,
                "slice_width": width,
                "slice_height": height,
                "width": atlas_width,
                "height": atlas_height,
                "depth": 1,
                "atlas_mode": mode,
                "native_grid": mode == "native-grid",
                "padding_slices": columns * rows - slices,
                "slice_order": "ascending Z, left to right, top to bottom",
                "atlas": {
                    "columns": columns,
                    "rows": rows,
                    "slice_count": slices,
                    "slice_width": width,
                    "slice_height": height,
                    "width": atlas_width,
                    "height": atlas_height,
                    "padding_slices": columns * rows - slices,
                },
            }
        except _ExportFailure as exc:
            state["fatal"] = state["fatal"] or str(exc)
        except Exception as exc:
            state["fatal"] = state["fatal"] or (
                "Failed to export 3D atlas: %s: %s"
                % (type(exc).__name__, exc))
        finally:
            if applied:
                _restore_event(controller, original_event, state)

    def _write_dds_atlas(self, controller, texture, target_id, mip, columns,
                         rows, slices, width, height, atlas_width,
                         atlas_height, path):
        directory = tempfile.mkdtemp(prefix="renderdoc_dds_atlas_")
        try:
            parts = []
            for index in range(slices):
                slice_path = os.path.join(directory, "slice_%d.dds" % index)
                save = _new_save(texture, target_id, "dds", mip, index, False,
                                 1, 0)
                _save_texture(
                    controller, save, slice_path,
                    "SaveTexture DDS slice %d for ResourceId::%s"
                    % (index, target_id))
                parts.append(_read_dds_slice(
                    slice_path, width, height, index))
            data = _compose_dds_atlas(
                parts, atlas_width, atlas_height, columns, rows, width, height)
            try:
                with open(path, "wb") as handle:
                    handle.write(data)
            except OSError as exc:
                raise _ExportFailure(
                    "Could not write DDS atlas %s: %s" % (path, exc))
        finally:
            shutil.rmtree(directory, ignore_errors=True)
