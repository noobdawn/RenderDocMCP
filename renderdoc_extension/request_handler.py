"""
Request Handler for RenderDoc MCP Bridge
Routes incoming requests to appropriate facade methods.
"""

import traceback


class RequestHandler:
    """Handles incoming MCP bridge requests"""

    def __init__(self, facade):
        self.facade = facade
        self._methods = {
            "ping": self._handle_ping,
            "get_capture_status": self._handle_get_capture_status,
            "get_draw_calls": self._handle_get_draw_calls,
            "get_frame_summary": self._handle_get_frame_summary,
            "find_draws_by_shader": self._handle_find_draws_by_shader,
            "find_draws_by_texture": self._handle_find_draws_by_texture,
            "find_draws_by_resource": self._handle_find_draws_by_resource,
            "get_draw_call_details": self._handle_get_draw_call_details,
            "get_action_timings": self._handle_get_action_timings,
            "get_shader_info": self._handle_get_shader_info,
            "export_shader_mapped_source": self._handle_export_shader_mapped_source,
            "debug_vertex": self._handle_debug_vertex,
            "debug_pixel": self._handle_debug_pixel,
            "get_debug_trace": self._handle_get_debug_trace,
            "release_debug_trace": self._handle_release_debug_trace,
            "get_buffer_contents": self._handle_get_buffer_contents,
            "get_texture_info": self._handle_get_texture_info,
            "get_texture_data": self._handle_get_texture_data,
            "get_texture_views": self._handle_get_texture_views,
            "export_texture_2d": self._handle_export_texture_2d,
            "export_texture_3d_atlas": self._handle_export_texture_3d_atlas,
            "get_pipeline_state": self._handle_get_pipeline_state,
            "list_captures": self._handle_list_captures,
            "open_capture": self._handle_open_capture,
        }

    def handle(self, request):
        """Handle a request and return response"""
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        try:
            if method not in self._methods:
                return self._error_response(
                    request_id, -32601, "Method not found: %s" % method
                )

            result = self._methods[method](params)
            return {"id": request_id, "result": result}

        except ValueError as e:
            return self._error_response(request_id, -32602, str(e))
        except Exception as e:
            traceback.print_exc()
            return self._error_response(request_id, -32000, str(e))

    def _error_response(self, request_id, code, message):
        """Create an error response"""
        return {"id": request_id, "error": {"code": code, "message": message}}

    def _handle_ping(self, params):
        """Handle ping request"""
        return {"status": "ok", "message": "pong"}

    def _handle_get_capture_status(self, params):
        """Handle get_capture_status request"""
        return self.facade.get_capture_status()

    def _handle_get_draw_calls(self, params):
        """Handle get_draw_calls request"""
        include_children = params.get("include_children", True)
        marker_filter = params.get("marker_filter")
        exclude_markers = params.get("exclude_markers")
        event_id_min = params.get("event_id_min")
        event_id_max = params.get("event_id_max")
        only_actions = params.get("only_actions", False)
        flags_filter = params.get("flags_filter")
        return self.facade.get_draw_calls(
            include_children=include_children,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            only_actions=only_actions,
            flags_filter=flags_filter,
        )

    def _handle_get_frame_summary(self, params):
        """Handle get_frame_summary request"""
        return self.facade.get_frame_summary()

    def _handle_find_draws_by_shader(self, params):
        """Handle find_draws_by_shader request"""
        shader_name = params.get("shader_name")
        if shader_name is None:
            raise ValueError("shader_name is required")
        stage = params.get("stage")
        return self.facade.find_draws_by_shader(shader_name, stage)

    def _handle_find_draws_by_texture(self, params):
        """Handle find_draws_by_texture request"""
        texture_name = params.get("texture_name")
        if texture_name is None:
            raise ValueError("texture_name is required")
        return self.facade.find_draws_by_texture(texture_name)

    def _handle_find_draws_by_resource(self, params):
        """Handle find_draws_by_resource request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.find_draws_by_resource(resource_id)

    def _handle_get_draw_call_details(self, params):
        """Handle get_draw_call_details request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_draw_call_details(int(event_id))

    def _handle_get_action_timings(self, params):
        """Handle get_action_timings request"""
        event_ids = params.get("event_ids")
        marker_filter = params.get("marker_filter")
        exclude_markers = params.get("exclude_markers")
        return self.facade.get_action_timings(
            event_ids=event_ids,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
        )

    def _handle_get_shader_info(self, params):
        """Handle get_shader_info request"""
        event_id = params.get("event_id")
        stage = params.get("stage")
        if event_id is None:
            raise ValueError("event_id is required")
        if stage is None:
            raise ValueError("stage is required")
        return self.facade.get_shader_info(int(event_id), stage)

    def _handle_export_shader_mapped_source(self, params):
        for name in ("event_id", "stage", "output_path"):
            if params.get(name) is None:
                raise ValueError("%s is required" % name)
        return self.facade.export_shader_mapped_source(
            params["event_id"], params["stage"], params["output_path"])

    def _handle_debug_vertex(self, params):
        """Handle debug_vertex request"""
        event_id = params.get("event_id")
        vertex = params.get("vertex")
        if event_id is None:
            raise ValueError("event_id is required")
        if vertex is None:
            raise ValueError("vertex is required")
        instance = params.get("instance", 0)
        view = params.get("view", 0)
        return self.facade.debug_vertex(event_id, vertex, instance, view)

    def _handle_debug_pixel(self, params):
        """Handle debug_pixel request"""
        event_id = params.get("event_id")
        x = params.get("x")
        y = params.get("y")
        if event_id is None:
            raise ValueError("event_id is required")
        if x is None:
            raise ValueError("x is required")
        if y is None:
            raise ValueError("y is required")
        primitive = params.get("primitive")
        sample = params.get("sample")
        view = params.get("view")
        return self.facade.debug_pixel(event_id, x, y, primitive, sample, view)

    def _handle_get_debug_trace(self, params):
        """Handle get_debug_trace request"""
        trace_id = params.get("trace_id")
        if trace_id is None:
            raise ValueError("trace_id is required")
        section = params.get("section", "status")
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        return self.facade.get_debug_trace(trace_id, section, offset, limit)

    def _handle_release_debug_trace(self, params):
        """Handle release_debug_trace request"""
        trace_id = params.get("trace_id")
        if trace_id is None:
            raise ValueError("trace_id is required")
        return self.facade.release_debug_trace(trace_id)

    def _handle_get_buffer_contents(self, params):
        """Handle get_buffer_contents request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        offset = params.get("offset", 0)
        length = params.get("length", 0)
        return self.facade.get_buffer_contents(resource_id, offset, length)

    def _handle_get_texture_info(self, params):
        """Handle get_texture_info request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.get_texture_info(resource_id)

    def _handle_get_texture_data(self, params):
        """Handle get_texture_data request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        mip = params.get("mip", 0)
        slice_idx = params.get("slice", 0)
        sample = params.get("sample", 0)
        depth_slice = params.get("depth_slice")  # None = full volume
        return self.facade.get_texture_data(resource_id, mip, slice_idx, sample, depth_slice)

    def _handle_get_texture_views(self, params):
        """Handle get_texture_views request"""
        resource_id = params.get("resource_id")
        event_id = params.get("event_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_texture_views(resource_id, event_id)

    def _handle_export_texture_2d(self, params):
        """Handle export_texture_2d request"""
        resource_id = params.get("resource_id")
        event_id = params.get("event_id")
        output_path = params.get("output_path")
        file_type = params.get("file_type")
        if resource_id is None:
            raise ValueError("resource_id is required")
        if event_id is None:
            raise ValueError("event_id is required")
        if output_path is None:
            raise ValueError("output_path is required")
        if file_type is None:
            raise ValueError("file_type is required")
        mip = params.get("mip", 0)
        array_slice = params.get("array_slice", 0)
        sample = params.get("sample", 0)
        return self.facade.export_texture_2d(
            resource_id,
            event_id,
            output_path,
            file_type,
            mip,
            array_slice,
            sample,
        )

    def _handle_export_texture_3d_atlas(self, params):
        """Handle export_texture_3d_atlas request"""
        resource_id = params.get("resource_id")
        event_id = params.get("event_id")
        output_path = params.get("output_path")
        file_type = params.get("file_type")
        if resource_id is None:
            raise ValueError("resource_id is required")
        if event_id is None:
            raise ValueError("event_id is required")
        if output_path is None:
            raise ValueError("output_path is required")
        if file_type is None:
            raise ValueError("file_type is required")
        mip = params.get("mip", 0)
        columns = params.get("columns", 8)
        return self.facade.export_texture_3d_atlas(
            resource_id,
            event_id,
            output_path,
            file_type,
            mip,
            columns,
        )

    def _handle_get_pipeline_state(self, params):
        """Handle get_pipeline_state request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_pipeline_state(int(event_id))

    def _handle_list_captures(self, params):
        """Handle list_captures request"""
        directory = params.get("directory")
        if directory is None:
            raise ValueError("directory is required")
        return self.facade.list_captures(directory)

    def _handle_open_capture(self, params):
        """Handle open_capture request"""
        capture_path = params.get("capture_path")
        if capture_path is None:
            raise ValueError("capture_path is required")
        return self.facade.open_capture(capture_path)
