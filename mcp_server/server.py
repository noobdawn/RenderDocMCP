"""
RenderDoc MCP Server
FastMCP 2.0 server providing access to RenderDoc capture data.
"""

from typing import Literal

from fastmcp import FastMCP

from .bridge.client import RenderDocBridge, RenderDocBridgeError
from .config import settings

# Initialize FastMCP server
mcp = FastMCP(
    name="RenderDoc MCP Server",
)

# RenderDoc bridge client
bridge = RenderDocBridge(host=settings.renderdoc_host, port=settings.renderdoc_port)


@mcp.tool
def get_capture_status() -> dict:
    """
    Check if a capture is currently loaded in RenderDoc.
    Returns the capture status and API type if loaded.
    """
    return bridge.call("get_capture_status")


@mcp.tool
def get_draw_calls(
    include_children: bool = True,
    marker_filter: str | None = None,
    exclude_markers: list[str] | None = None,
    event_id_min: int | None = None,
    event_id_max: int | None = None,
    only_actions: bool = False,
    flags_filter: list[str] | None = None,
) -> dict:
    """
    Get the list of all draw calls and actions in the current capture.

    Args:
        include_children: Include child actions in the hierarchy (default: True)
        marker_filter: Only include actions under markers containing this string (partial match)
        exclude_markers: Exclude actions under markers containing these strings (list of partial matches)
        event_id_min: Only include actions with event_id >= this value
        event_id_max: Only include actions with event_id <= this value
        only_actions: If True, exclude marker actions (PushMarker/PopMarker/SetMarker)
        flags_filter: Only include actions with these flags (list of flag names, e.g. ["Drawcall", "Dispatch"])

    Returns a hierarchical tree of actions including markers, draw calls,
    dispatches, and other GPU events.
    """
    params: dict[str, object] = {"include_children": include_children}
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if exclude_markers is not None:
        params["exclude_markers"] = exclude_markers
    if event_id_min is not None:
        params["event_id_min"] = event_id_min
    if event_id_max is not None:
        params["event_id_max"] = event_id_max
    if only_actions:
        params["only_actions"] = only_actions
    if flags_filter is not None:
        params["flags_filter"] = flags_filter
    return bridge.call("get_draw_calls", params)


@mcp.tool
def get_frame_summary() -> dict:
    """
    Get a summary of the current capture frame.

    Returns statistics about the frame including:
    - API type (D3D11, D3D12, Vulkan, etc.)
    - Total action count
    - Statistics: draw calls, dispatches, clears, copies, presents, markers
    - Top-level markers with event IDs and child counts
    - Resource counts: textures, buffers
    """
    return bridge.call("get_frame_summary")


@mcp.tool
def find_draws_by_shader(
    shader_name: str,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"] | None = None,
) -> dict:
    """
    Find all draw calls using a shader with the given name (partial match).

    Args:
        shader_name: Partial name to search for in shader names or entry points
        stage: Optional shader stage to search (if not specified, searches all stages)

    Returns a list of matching draw calls with event IDs and match reasons.
    """
    params: dict[str, object] = {"shader_name": shader_name}
    if stage is not None:
        params["stage"] = stage
    return bridge.call("find_draws_by_shader", params)


@mcp.tool
def find_draws_by_texture(texture_name: str) -> dict:
    """
    Find all draw calls using a texture with the given name (partial match).

    Args:
        texture_name: Partial name to search for in texture resource names

    Returns a list of matching draw calls with event IDs and match reasons.
    Searches SRVs, UAVs, and render targets.
    """
    return bridge.call("find_draws_by_texture", {"texture_name": texture_name})


@mcp.tool
def find_draws_by_resource(resource_id: str) -> dict:
    """
    Find all draw calls using a specific resource ID (exact match).

    Args:
        resource_id: Resource ID to search for (e.g. "ResourceId::12345" or "12345")

    Returns a list of matching draw calls with event IDs and match reasons.
    Searches shaders, SRVs, UAVs, render targets, and depth targets.
    """
    return bridge.call("find_draws_by_resource", {"resource_id": resource_id})


@mcp.tool
def get_draw_call_details(event_id: int) -> dict:
    """
    Get detailed information about a specific draw call.

    Args:
        event_id: The event ID of the draw call to inspect

    Includes vertex/index counts, resource outputs, and other metadata.
    """
    return bridge.call("get_draw_call_details", {"event_id": event_id})


@mcp.tool
def get_action_timings(
    event_ids: list[int] | None = None,
    marker_filter: str | None = None,
    exclude_markers: list[str] | None = None,
) -> dict:
    """
    Get GPU timing information for actions (draw calls, dispatches, etc.).

    Args:
        event_ids: Optional list of specific event IDs to get timings for.
                   If not specified, returns timings for all actions.
        marker_filter: Only include actions under markers containing this string (partial match).
        exclude_markers: Exclude actions under markers containing these strings.

    Returns timing data including:
    - available: Whether GPU timing counters are supported
    - unit: Time unit (typically "seconds")
    - timings: List of {event_id, name, duration_seconds, duration_ms}
    - total_duration_ms: Sum of all durations
    - count: Number of timing entries

    Note: GPU timing counters may not be available on all hardware/drivers.
    """
    params: dict[str, object] = {}
    if event_ids is not None:
        params["event_ids"] = event_ids
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if exclude_markers is not None:
        params["exclude_markers"] = exclude_markers
    return bridge.call("get_action_timings", params)


@mcp.tool
def get_shader_info(
    event_id: int,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"],
) -> dict:
    """
    Get shader information for a specific stage at a given event.

    Args:
        event_id: The event ID to inspect the shader at
        stage: The shader stage (vertex, hull, domain, geometry, pixel, compute)

    Returns native shader disassembly, source files from shader debug information,
    debug loading diagnostics, constant buffer values, and resource bindings.
    For external shader debug files, configure RenderDoc's Settings > Core >
    Shader Debug Search Path and reload shader debug information or reopen the capture.
    Source may be unavailable if matching debug information is missing or stripped.
    """
    return bridge.call("get_shader_info", {"event_id": event_id, "stage": stage})


@mcp.tool
def export_shader_mapped_source(
    event_id: int,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"],
    output_path: str,
) -> dict:
    """Export source lines mapped to retained DXIL instructions to an HLSL file.

    Uses DXC DXIL debug locations without running a shader debug invocation.
    Requires available matching source/debug metadata and the DXC DXIL target.
    Output preserves original file names and line numbers with #line directives;
    it is a reading excerpt, not a guaranteed compilable shader. Missing mappings
    do not prove source was optimized away. Runtime branches remain included.
    output_path must be absolute, end in .hlsl, and have an existing parent.
    Existing files are never overwritten.
    """
    return bridge.call("export_shader_mapped_source", {
        "event_id": event_id, "stage": stage, "output_path": output_path,
    })


@mcp.tool
def debug_vertex(
    event_id: int,
    vertex: int,
    instance: int = 0,
    view: int = 0,
) -> dict:
    """
    Start debugging a vertex shader invocation and return a trace identifier.

    Args:
        event_id: Action event ID that issued the draw call to debug (not a
                  resource ID). Use get_draw_calls to find it.
        vertex: Zero-based vertex index within the draw call's vertex/index
                stream at that event, not a global buffer offset.
        instance: Zero-based instance index for instanced draws (default: 0).
        view: Zero-based multiview/array view index (default: 0).

    Shader debugging is performed asynchronously by RenderDoc. If the returned
    status is not complete, poll get_debug_trace(trace_id, "status") until the
    status is complete, failed, or cancelled. Partial data is available after
    failure. Release the trace when finished.
    """
    return bridge.call(
        "debug_vertex",
        {"event_id": event_id, "vertex": vertex, "instance": instance, "view": view},
    )


@mcp.tool
def debug_pixel(
    event_id: int,
    x: int,
    y: int,
    primitive: int | None = None,
    sample: int | None = None,
    view: int | None = None,
) -> dict:
    """
    Start debugging a pixel shader invocation and return a trace identifier.

    Args:
        event_id: Action event ID that issued the draw call (not a resource ID).
        x: Pixel X coordinate in the render target, not normalized UV space.
        y: Pixel Y coordinate in the render target, not normalized UV space.
        primitive: Optional zero-based primitive index. Several primitives can
                   cover the same pixel, so a pixel alone is ambiguous; when
                   omitted, RenderDoc selects a fragment without a primitive preference.
        sample: Optional MSAA sample index. When omitted, no preference is set;
                multisampled edges may need an explicit sample.
        view: Optional multiview/array view index. When omitted, no view preference is set.

    Coordinates are integer pixel coordinates in the render target's viewport,
    not normalized UVs. Shader debugging is asynchronous; poll
    get_debug_trace(trace_id, "status")
    until complete, failed, or cancelled. Partial data may also be available
    after failure. Release the trace with release_debug_trace when finished.
    """
    params: dict[str, object] = {"event_id": event_id, "x": x, "y": y}
    if primitive is not None:
        params["primitive"] = primitive
    if sample is not None:
        params["sample"] = sample
    if view is not None:
        params["view"] = view
    return bridge.call("debug_pixel", params)


@mcp.tool
def get_debug_trace(
    trace_id: str,
    section: Literal[
        "status", "metadata", "states", "state_deltas", "instructions", "sources", "source_variables", "source_steps", "mapping_definitions"
    ] = "status",
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """
    Read a previously started shader debug trace.

    Args:
        trace_id: Trace identifier returned by debug_vertex or debug_pixel.
        section: Which part of the trace to return:
                 - "status": asynchronous debugger progress/state.
                 - "metadata": shader/event/input summary for the trace.
                 - "states": debugger steps (variable changes per step).
                 - "state_deltas": ordered compact states with inline baselines and patches;
                   decode from offset 0 with StateDecoder. Counts share the states count.
                 - "source_steps": consecutive source-location groups with net variable changes.
                 - "instructions": confirmed executed instructions and source mappings.
                 - "sources": deduplicated observed source lines.
                 - "source_variables": mapping_ids referencing shared mapping definitions.
                 - "mapping_definitions": complete deduplicated source variable mappings.
        offset: Zero-based item offset for paged sections (default: 0).
        limit: Maximum number of items to return per page (default: 100).

    Shader debugging runs asynchronously after debug_vertex/debug_pixel, so
    "status" may report queued, running, cancelling, complete, cancelled or failed.
    Pages may be read while running or after failure; complete is true only for a
    finished trace. Instructions are unique confirmed transitions in first-execution
    order; source lines use original file indices and line numbers. Source variable
    snapshots are referenced by source_variables_id. For complete shader source,
    use get_shader_info. A failed or cancelled trace can contain partial data.
    """
    return bridge.call(
        "get_debug_trace",
        {"trace_id": trace_id, "section": section, "offset": offset, "limit": limit},
    )


@mcp.tool
def release_debug_trace(trace_id: str) -> dict:
    """
    Release a shader debug trace, or request cancellation if it is still running.

    Args:
        trace_id: Trace identifier returned by debug_vertex or debug_pixel.

    A running trace is removed after cooperative cancellation finishes. A native
    RenderDoc call already in progress cannot be interrupted immediately.
    """
    return bridge.call("release_debug_trace", {"trace_id": trace_id})


@mcp.tool
def get_buffer_contents(
    resource_id: str,
    offset: int = 0,
    length: int = 0,
) -> dict:
    """
    Read the contents of a buffer resource.

    Args:
        resource_id: The resource ID of the buffer to read
        offset: Byte offset to start reading from (default: 0)
        length: Number of bytes to read, 0 for entire buffer (default: 0)

    Returns buffer data as base64-encoded bytes along with metadata.
    """
    return bridge.call(
        "get_buffer_contents",
        {"resource_id": resource_id, "offset": offset, "length": length},
    )


@mcp.tool
def get_texture_info(resource_id: str) -> dict:
    """
    Get metadata about a texture resource.

    Args:
        resource_id: The resource ID of the texture

    Includes dimensions and structured resource format (UNorm, sRGB and
    compression where known). Use get_texture_views for event-specific view formats.
    """
    return bridge.call("get_texture_info", {"resource_id": resource_id})


@mcp.tool
def get_texture_data(
    resource_id: str,
    mip: int = 0,
    slice: int = 0,
    sample: int = 0,
    depth_slice: int | None = None,
) -> dict:
    """
    Read the pixel data of a texture resource.

    Args:
        resource_id: The resource ID of the texture to read
        mip: Mip level to retrieve (default: 0)
        slice: Array slice or cube face index (default: 0)
               For cube maps: 0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-
        sample: MSAA sample index (default: 0)
        depth_slice: For 3D textures only, extract a specific depth slice (default: None = full volume)
                     When specified, returns only the 2D slice at that depth index

    Returns texture pixel data as base64-encoded bytes along with metadata
    including dimensions at the requested mip level and format information.
    """
    params = {"resource_id": resource_id, "mip": mip, "slice": slice, "sample": sample}
    if depth_slice is not None:
        params["depth_slice"] = depth_slice
    return bridge.call("get_texture_data", params)


@mcp.tool
def get_texture_views(resource_id: str, event_id: int) -> dict:
    """
    Enumerate the views through which a texture resource is bound at an event.

    Args:
        resource_id: The resource ID of the texture to inspect
        event_id: The event ID whose pipeline bindings should be inspected

    Views are resolved from the pipeline state at event_id, so different events
    can bind the same texture with different view descriptions. Returns the
    matching views with their binding kind, slot, and view metadata.
    """
    return bridge.call(
        "get_texture_views", {"resource_id": resource_id, "event_id": event_id}
    )


@mcp.tool
def export_texture_2d(
    resource_id: str,
    event_id: int,
    output_path: str,
    file_type: Literal["dds", "png", "tga", "exr"],
    mip: int = 0,
    array_slice: int = 0,
    sample: int = 0,
) -> dict:
    """
    Export one 2D subresource of a texture to a file.

    Args:
        resource_id: The resource ID of the texture to export
        event_id: Action event ID whose replay state supplies the texture contents
        output_path: Destination file path for the exported image
        file_type: Output container format (dds, png, tga, exr)
        mip: Mip level to export (default: 0)
        array_slice: Array slice or cube face index (default: 0)
                     For cube maps: 0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-
        sample: MSAA sample index (default: 0)

    Exports a Texture2D subresource, not a Texture3D depth slice. RenderDoc may
    convert unsupported formats even when writing DDS. PNG/TGA/EXR may clamp or
    quantize values. This exports the resource interpretation, not a selected
    descriptor view or its swizzle. Existing files are not overwritten.
    """
    return bridge.call(
        "export_texture_2d",
        {
            "resource_id": resource_id,
            "event_id": event_id,
            "output_path": output_path,
            "file_type": file_type,
            "mip": mip,
            "array_slice": array_slice,
            "sample": sample,
        },
    )


@mcp.tool
def export_texture_3d_atlas(
    resource_id: str,
    event_id: int,
    output_path: str,
    file_type: Literal["dds", "png", "tga", "exr"],
    mip: int = 0,
    columns: int = 8,
) -> dict:
    """
    Export a 3D texture volume as a 2D atlas grid.

    Args:
        resource_id: The resource ID of the 3D texture to export
        event_id: Action event ID whose replay state supplies the texture contents
        output_path: Destination file path for the exported atlas
        file_type: Output container format (dds, png, tga, exr)
        mip: Mip level to export (currently only 0 due to RenderDoc 1.48's
             unsafe higher-mip 3D save path)
        columns: Number of depth slices per atlas row (default: 8)

    Slices are laid out left to right, top to bottom, and the last row is padded
    when the slice count is not a multiple of columns. The atlas is a flattened
    2D image: depth becomes a grid, so the result no longer carries volume
    semantics. Output conversion may change precision and numeric values.
    DDS atlases require compatible slice formats and block alignment.
    """
    return bridge.call(
        "export_texture_3d_atlas",
        {
            "resource_id": resource_id,
            "event_id": event_id,
            "output_path": output_path,
            "file_type": file_type,
            "mip": mip,
            "columns": columns,
        },
    )


@mcp.tool
def get_pipeline_state(event_id: int) -> dict:
    """
    Get the full graphics pipeline state at a specific event.

    Args:
        event_id: The event ID to get pipeline state at

    Returns detailed pipeline state including:
    - Bound shaders with entry points for each stage
    - Shader resources (SRVs): textures and buffers with dimensions, format, slot, name
    - UAVs (RWTextures/RWBuffers): resource details with dimensions and format
    - Samplers: addressing modes, filter settings, LOD parameters
    - Constant buffers: slot, size, variable count
    - Render targets and depth target
    - Viewports and input assembly state
    """
    return bridge.call("get_pipeline_state", {"event_id": event_id})


@mcp.tool
def list_captures(directory: str) -> dict:
    """
    List all RenderDoc capture files (.rdc) in the specified directory.

    Args:
        directory: The directory path to search for capture files

    Returns a list of capture files with their metadata including:
    - filename: The capture file name
    - path: Full path to the file
    - size_bytes: File size in bytes
    - modified_time: Last modified timestamp (ISO format)
    """
    return bridge.call("list_captures", {"directory": directory})


@mcp.tool
def open_capture(capture_path: str) -> dict:
    """
    Open a RenderDoc capture file (.rdc).

    Args:
        capture_path: Full path to the capture file to open

    Returns success status and information about the opened capture.
    Note: This will close any currently open capture.
    """
    return bridge.call("open_capture", {"capture_path": capture_path})


def main():
    """Run the MCP server"""
    mcp.run()


if __name__ == "__main__":
    main()
