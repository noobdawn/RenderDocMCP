# RenderDoc MCP Server

[日本語版](readme_jp.md) | 中文

作为 RenderDoc UI 扩展运行的 MCP 服务，让 AI 助手访问 RenderDoc 捕获数据，辅助图形调试。

## 架构

```text
Claude / AI 客户端 (stdio)
        │
        ▼
MCP 服务进程 (Python + FastMCP 2.x)
        │ 基于文件的 IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc 进程 (扩展)
```

RenderDoc 内置的 Python 没有 socket 模块，因此两个进程通过文件 IPC 通信。

## 安装与配置

### 1. 安装 RenderDoc 扩展

```bash
python scripts/install_extension.py
```

扩展安装在 `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge`。

### 2. 在 RenderDoc 中启用扩展

1. 启动 RenderDoc。
2. 打开 Tools > Manage Extensions。
3. 启用 “RenderDoc MCP Bridge”。

### 3. 安装 MCP 服务

在项目目录下运行：

```bash
uv tool install .
uv tool update-shell  # 将工具目录加入 PATH
```

重新打开终端后即可使用 `renderdoc-mcp` 命令。开发时可使用 `uv tool install --editable .`，使源码修改直接生效。

### 4. 配置 MCP 客户端

Claude Desktop：在 `claude_desktop_config.json` 中添加；Claude Code：在 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

启动 RenderDoc 并打开 `.rdc` 捕获文件后，即可从 MCP 客户端访问捕获数据。

## MCP 工具

| 工具 | 说明 |
|------|------|
| `get_capture_status` | 查询捕获文件的加载状态 |
| `list_captures` / `open_capture` | 列出捕获文件 / 打开捕获文件（会关闭当前捕获） |
| `get_draw_calls` | 以层级结构获取绘制调用和其他动作 |
| `get_frame_summary` | 获取帧动作和资源的统计信息 |
| `find_draws_by_shader` / `find_draws_by_texture` / `find_draws_by_resource` | 查找使用指定着色器、贴图或资源的动作 |
| `get_draw_call_details` | 获取指定绘制调用的详细信息 |
| `get_action_timings` | 获取可用的 GPU 动作耗时 |
| `get_shader_info` | 获取着色器反汇编、调试信息中的源码及常量缓冲区值 |
| `export_shader_mapped_source` | 导出保留 DXIL 指令映射到的源码行 |
| `debug_vertex` / `debug_pixel` | 异步启动顶点或像素着色器调试 |
| `get_debug_trace` / `release_debug_trace` | 分页读取 / 释放调试轨迹 |
| `get_buffer_contents` | 获取缓冲区内容（Base64） |
| `get_texture_info` | 获取贴图元数据和资源格式 |
| `get_texture_data` | 获取贴图像素数据（Base64） |
| `get_texture_views` | 列出指定事件中贴图绑定的视图 |
| `export_texture_2d` | 将 2D 子资源导出为图像文件 |
| `export_texture_3d_atlas` | 将 3D 贴图导出为 2D 切片图集 |
| `get_pipeline_state` | 获取管线状态 |

## 使用示例

### 绘制调用和管线状态

```python
get_draw_calls(include_children=True)
get_pipeline_state(event_id=123)
```

### 着色器信息

```python
get_shader_info(event_id=123, stage="pixel")
```

`disassembly` 是 RenderDoc 默认的原生反汇编（例如 D3D 的 DXBC/DXIL）。`source_files` 包含从调试信息获取的文件名和内容；`source_available` 表示是否存在非空源码。支持时，`debug_info_loading_log` 会给出外部调试信息的查找结果。

对于 UE5 等使用外部 shader 调试文件的场景，请在 RenderDoc 的 Settings > Core > Shader Debug Search Path 中添加目录。如果打开捕获文件后才修改设置，请按 RenderDoc 提示重新加载 shader 调试信息，或重新打开捕获文件。若对应 shader 调试文件不含源码，即使有反汇编也无法获取源码；普通应用程序 PDB 或源码目录本身并不够。

### 导出编译后指令对应的源码

```python
export_shader_mapped_source(
    event_id=7279,
    stage="pixel",
    output_path="D:\\out\\mapped_source.hlsl",
)
```

该工具使用 `DXC DXIL` 反汇编中的调试位置，只导出保留指令关联的源码行，不运行像素或顶点调试。它包含编译后保留的运行时分支，不是单个像素的执行路径。文件使用 `#line` 保留原始文件名和行号，用于阅读；缺少映射的声明、宏和依赖可能被省略，因此不保证可重新编译。

目前支持 DXIL 且要求 RenderDoc 提供 `DXC DXIL` 目标及匹配的源码调试信息；映射不可用时明确报错。没有行号映射并不能证明源码被优化删除。输出路径必须是绝对 `.hlsl` 路径、父目录存在，已有文件不会覆盖。

RenderDoc 的反汇编可能来自 ILDB 调试字节码，因此结果是指令关联的源码摘录，不是驱动最终机器码或源码优化覆盖率的证明。

头部按映射源码的标识符引用筛选 uniform 成员值，忽略注释和字符串，不能证明实际 GPU 读取或排除局部变量遮蔽。固定数组下标只列对应元素；动态数组和整结构引用最多预览 8 个元素或成员并注明省略。绑定数组明确标记不支持，头部限制在 64 KiB 内，超限注明剩余省略条目。

### 顶点和像素着色器调试

```python
debug_vertex(event_id=11824, vertex=0, instance=0, view=0)
debug_pixel(event_id=11824, x=620, y=208, primitive=0)

# 使用返回的 trace_id
get_debug_trace(trace_id="...", section="status")
get_debug_trace(trace_id="...", section="metadata")
get_debug_trace(trace_id="...", section="source_steps", offset=0, limit=50)
get_debug_trace(trace_id="...", section="states", offset=0, limit=50)
get_debug_trace(trace_id="...", section="instructions", offset=0, limit=50)
get_debug_trace(trace_id="...", section="sources")
release_debug_trace(trace_id="...")
```

启动后查询 `status`，直到状态为 `complete`、`failed` 或 `cancelled`。`states`、`instructions` 和 `sources` 可通过 `next_offset` 继续分页读取。每个步骤的 `changes` 表示进入该状态时的变化，`next_instruction` 是下一条将执行的指令；指令编号不一定连续，指令信息稀疏时会使用前一个编号。为保持精度，64 位整数和 GPU 指针以十进制字符串返回；非有限浮点数返回为 `NaN`、`Infinity` 或 `-Infinity` 字符串。

调试轨迹内部保存变量首次出现时的基线及后续成员增量。`states` 按需返回重建后的原始状态，`state_deltas` 返回紧凑有序流，记录数与 `states` 相同。增量流的 `version=1`，使用内联 `seed`/`create`、成员路径 `patch` 和 `unset`，调用栈通过 `callstack_id` 共享；必须从 offset 0 开始用 `shader_state_codec.StateDecoder` 顺序解码。变量基线可能在后续步骤才出现，不能视作所有局部变量在开始时都有值。`source_steps` 分组规则不变。

`instructions` 和 `sources` 只包含所选执行对象已确认经过的调试步骤指令及其源码行，并非完整着色器。`sources` 返回去重后的文件索引、原始行号和文本；`instructions` 中的 `source_variables_id` 对应分页段 `source_variables` 的映射。稀疏映射使用 `mapping_instruction` 标明继承的映射锚点。源码可能不可用；失败或取消的任务只有部分结果。如需完整源码，请使用 `get_shader_info`。

`vertex` 和 `instance` 是绘制调用内从零开始的编号。索引绘制会解析实际顶点索引，结果位于 `invocation.resolved_index`。像素的 `x`、`y` 是以左上角为原点的整数坐标。同一像素被多个图元覆盖时可指定 `primitive`，使用 MSAA 时可指定 `sample`。RenderDoc 不支持直接指定 fragment index；找不到指定图元时可能选择其他 fragment。选择精度重要时，请使用 RenderDoc 的 Pixel History 核对。

轨迹是 RenderDoc 为选定执行对象生成的调试结果；源码和变量名依赖 shader 调试信息，不包含所有 GPU lane 或资源的全部内容。一次只能运行一个调试任务。`release_debug_trace` 可请求取消，但可能需要等待当前 RenderDoc 原生操作返回。轨迹总存储限额为 256 MiB，超限时状态为 `failed`，部分结果会保留；请释放不再需要的轨迹。RenderDoc 单次返回的执行批次大小不受该限制。轨迹仅保存在扩展中，扩展重新加载或结束时会消失。

同时使用多个 MCP 服务进程时，应为每个进程及其对应 RenderDoc 设置相同的 `RENDERDOC_MCP_IPC_DIR`，并使用不同目录隔离各组进程。

`source_steps` 按连续源码位置和调用栈聚合已确认的执行步骤，指令回退或重复会开始新组。每组保留起止步骤、标志及变量首末值，省略组内中间值；原始 `states` 仍可查询。正在运行的最后一组在结束或位置变化时才发布，因此分页只返回已完成组。

`source_variables` 的每个快照保存有序 `mapping_ids`，通过 `mapping_definitions` 的 `id` 查询完整定义，重复引用和顺序均保留。`source_steps` 中 `kind="auxiliary"` 表示连续、无变量变化且无事件标志的精确 `nop;` 辅助步骤；其步骤范围和数量保留，源码位置不作为有效计算展示。

### 获取贴图数据与视图

```python
# 获取 2D 贴图的 mip 0 或指定 mip
get_texture_data(resource_id="ResourceId::123")
get_texture_data(resource_id="ResourceId::123", mip=2)

# 立方体贴图面：0=X+、1=X-、2=Y+、3=Y-、4=Z+、5=Z-
get_texture_data(resource_id="ResourceId::456", slice=3)

# 获取 3D 贴图的指定深度切片
get_texture_data(resource_id="ResourceId::789", depth_slice=5)

# 查询指定事件中该资源绑定的视图
get_texture_views(resource_id="ResourceId::123", event_id=123)
```

视图由指定事件的管线状态决定；同一贴图在不同事件中可能有不同绑定。返回内容包括 SRV、UAV、渲染目标等类型、绑定位置及视图元数据。资源格式和视图格式分别报告，未知信息不会当作确定值。

### 导出贴图

```python
export_texture_2d(
    resource_id="ResourceId::123",
    event_id=123,
    output_path="D:\\out\\texture.png",
    file_type="png",
)

# 导出立方体贴图索引为 3 的面 (Y-)
export_texture_2d(
    resource_id="ResourceId::456",
    event_id=123,
    output_path="D:\\out\\face3.exr",
    file_type="exr",
    array_slice=3,
)

# 每行排列 8 个 3D 深度切片
export_texture_3d_atlas(
    resource_id="ResourceId::789",
    event_id=123,
    output_path="D:\\out\\volume_atlas.png",
    file_type="png",
    columns=8,
)
```

`export_texture_2d` 导出一个 2D 子资源，可指定 `mip`、`array_slice` 和 `sample`；不接受 Texture3D。`export_texture_3d_atlas` 将深度切片按 Z 递增、从左到右、从上到下排列成 `columns` 列，最后一行可能有空白。图集包括 DDS 在内都是展平的 2D 图像，不再保留体积语义。

`file_type` 支持 `dds`、`png`、`tga`、`exr`。导出时可能发生格式转换、数值截断或量化，DDS 也可能被转换；EXR 可以保留 HDR 值。`event_id` 必须是已有的动作事件，其回放状态决定导出内容。`output_path` 必须是绝对路径、父目录必须存在，扩展名必须与 `file_type` 一致；已有文件不会被覆盖，输出文件系统需支持硬链接。由于 RenderDoc 1.48 的 3D 高 mip 导出存在安全问题，3D 图集目前只支持 mip 0；深度为 1 的 3D 贴图必须设置 `columns=1`。导出按资源格式解释，不应用特定视图的 swizzle。

导出会按图像尺寸和文件类型估算内存用量并限制在 1 GiB，图集还会经过额外检查；这不是实际峰值内存的保证。TGA 的宽和高不能超过 65,535。

> **注意**：贴图导出和 3D 图集尚未完成 RenderDoc 1.48 实机验证。DDS 图集不接受部分格式，块压缩切片的宽高必须为 4 的倍数。大尺寸导出可能占用大量内存并长时间阻塞 RenderDoc UI。超时不代表处理已取消，文件可能在超时后才出现。

### 获取缓冲区数据

```python
get_buffer_contents(resource_id="ResourceId::123")
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

## 要求与验证范围

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- 以 RenderDoc 1.48 为目标版本（1.48 导出尚未验证）

部分着色器调试已在 Windows、RenderDoc 1.45 和 DirectX 12 捕获上验证；这不代表全部功能、其他图形 API 或 Linux/macOS 已验证。

## 许可证

MIT
