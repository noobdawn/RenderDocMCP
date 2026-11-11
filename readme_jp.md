# RenderDoc MCP Server

[中文](README.md) | 日本語

RenderDoc UI拡張機能として動作するMCPサーバー。AIアシスタントがRenderDocのキャプチャデータにアクセスし、グラフィックスデバッグを支援する。

## アーキテクチャ

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (Python + FastMCP 2.0)
        │ File-based IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc Process (Extension)
```

RenderDoc内蔵のPythonにはsocketモジュールがないため、ファイルベースのIPCで通信を行う。

## セットアップ

### 1. RenderDoc拡張機能のインストール

```bash
python scripts/install_extension.py
```

拡張機能は `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge` にインストールされる。

### 2. RenderDocで拡張機能を有効化

1. RenderDocを起動
2. Tools > Manage Extensions
3. "RenderDoc MCP Bridge" を有効化

### 3. MCPサーバーのインストール

```bash
uv tool install .
uv tool update-shell  # PATHに追加
```

シェルを再起動すると `renderdoc-mcp` コマンドが使えるようになる。

> **Note**: `--editable` を付けると、ソースコードの変更が即座に反映される（開発時に便利）。
> 安定版としてインストールする場合は `uv tool install .` を使用。

### 4. MCPクライアントの設定

#### Claude Desktop

`claude_desktop_config.json` に追加:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

#### Claude Code

`.mcp.json` に追加:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

## 使い方

1. RenderDocを起動し、キャプチャファイル (.rdc) を開く
2. MCPクライアント (Claude等) から RenderDoc のデータにアクセス

## MCPツール一覧

| ツール | 説明 |
|--------|------|
| `get_capture_status` | キャプチャの読み込み状態を確認 |
| `list_captures` / `open_capture` | キャプチャファイルの一覧取得 / 読み込み（現在のキャプチャは閉じられます） |
| `get_draw_calls` | ドローコール一覧を階層構造で取得 |
| `get_frame_summary` | フレームのアクションとリソースの概要を取得 |
| `find_draws_by_shader` / `find_draws_by_texture` / `find_draws_by_resource` | シェーダー、テクスチャ、リソースを使用するアクションを検索 |
| `get_draw_call_details` | 特定のドローコールの詳細情報を取得 |
| `get_action_timings` | 利用可能な GPU タイミングを取得 |
| `get_shader_info` | シェーダーの逆アセンブリ、デバッグ情報内のソースコード、定数バッファの値を取得 |
| `export_shader_mapped_source` | 保持された DXIL 命令に対応するソース行を書き出し |
| `debug_vertex` / `debug_pixel` | 頂点またはピクセルのシェーダーデバッグを非同期で開始 |
| `get_debug_trace` / `release_debug_trace` | トレースをページ単位で取得 / 解放 |
| `get_buffer_contents` | バッファの内容を取得 (Base64) |
| `get_texture_info` | テクスチャのメタデータを取得 |
| `get_texture_data` | テクスチャのピクセルデータを取得 (Base64) |
| `get_texture_views` | 指定イベントでテクスチャがバインドされているビューを列挙 |
| `export_texture_2d` | 2D サブリソースを画像ファイルへ書き出し |
| `export_texture_3d_atlas` | 3D テクスチャを 2D アトラス画像へ書き出し |
| `get_pipeline_state` | パイプライン状態を取得 |

## 使用例

### ドローコール一覧の取得

```
get_draw_calls(include_children=True)
```

### シェーダー情報の取得

```
get_shader_info(event_id=123, stage="pixel")
```

`disassembly` は RenderDoc のデフォルトのネイティブ逆アセンブリ（D3D の
DXBC/DXIL など）です。`source_files` にはデバッグ情報から取得したファイル名と
内容が入り、`source_available` は空でないソースがあるかを示します。
`debug_info_loading_log` は対応する RenderDoc バージョンで取得できる場合、
外部デバッグ情報の検索結果を示します。

UE5 などで外部の shader デバッグファイルを使用する場合は、RenderDoc の
Settings > Core > Shader Debug Search Path にディレクトリを追加してください。
キャプチャを開いた後に変更した場合は、RenderDoc の確認ダイアログで shader
デバッグ情報を再読み込みするか、キャプチャを開き直します。対応する shader の
デバッグファイルにソースが含まれていなければ、逆アセンブリがあってもソースは
取得できません。通常のアプリケーション用 PDB やソースディレクトリだけでは不十分です。

### コンパイル後の命令に対応するソースを書き出し

```python
export_shader_mapped_source(event_id=7279, stage="pixel", output_path="D:\\out\\mapped_source.hlsl")
```

`DXC DXIL` のデバッグ位置から、保持された命令に対応するソース行のみを書き出します。
デバッグ実行は不要で、保持された実行時分岐も含みます。特定ピクセルの実行経路ではありません。
`#line` で元のファイル名と行番号を保持しますが、宣言やマクロが省略されるため再コンパイルは保証しません。
DXIL と対応するソース情報、`DXC DXIL` ターゲットが必要です。行番号情報がないことは最適化による削除の証明ではありません。
出力先は親ディレクトリが存在する絶対 `.hlsl` パスを指定し、既存ファイルは上書きしません。
RenderDoc は ILDB デバッグバイトコードを逆アセンブルする場合があるため、最終 GPU 機械語や最適化の完全な証明ではありません。
先頭にはソース中の識別子に一致する uniform 値のみを載せます。コメントと文字列は除外し、GPU アクセスやローカル変数の隠蔽を証明しません。固定添字は該当要素のみ、動的配列や構造体全体は最大 8 件をプレビューし省略を明示します。バインド配列は非対応として記録し、ヘッダーは 64 KiB 以下に制限します。

### 頂点・ピクセルシェーダーのデバッグ

```
debug_vertex(event_id=11824, vertex=0, instance=0, view=0)
debug_pixel(event_id=11824, x=620, y=208, primitive=0)

# 返された trace_id を使用する
get_debug_trace(trace_id="...", section="status")
get_debug_trace(trace_id="...", section="metadata")
get_debug_trace(trace_id="...", section="source_steps", offset=0, limit=50)
get_debug_trace(trace_id="...", section="states", offset=0, limit=50)
get_debug_trace(trace_id="...", section="instructions", offset=0, limit=50)
get_debug_trace(trace_id="...", section="sources")
release_debug_trace(trace_id="...")
```

開始後は `status` を確認し、`complete` / `failed` / `cancelled` のいずれかになるまで
待ちます。`states`、`instructions`、`sources` は `next_offset` で続きを取得できます。
ステップの `changes` はその状態に入る際の変更で、`next_instruction` は次に実行する
命令です。命令番号は連続とは限らず、命令情報が疎な場合は直前の番号が適用されます。
64 ビット整数と GPU ポインタは精度を保つため十進文字列で返し、非有限浮動小数点値は
`NaN`、`Infinity`、`-Infinity` の文字列で返します。

変数の初回観測時のベースラインとメンバー差分を保存します。`states` は元の状態を再構築し、`state_deltas` は同じ件数のコンパクトな順序付きストリームを返します。`version=1` の内訳は `seed`/`create`、パス付き `patch`、`unset` と共有 `callstack_id` です。offset 0 から `shader_state_codec.StateDecoder` で順番に復元してください。ローカル変数の初期値は途中で初めて観測される場合があります。`source_steps` の集約規則は変わりません。

`instructions` と `sources` は、選択した実行対象の確認済みデバッグステップと対応するソース行のみを表します。
`sources` は重複を除いたファイルインデックス、元の行番号とテキストを返します。
`instructions` の `source_variables_id` はページ取得可能な `source_variables` セクションを参照し、
`mapping_instruction` は疎な命令情報の継承元を示します。ソースを取得できない場合や、失敗・キャンセルにより
結果が部分的な場合があります。シェーダー全体のソースは `get_shader_info` で取得してください。

`vertex` と `instance` は描画内のゼロ基準の番号です。インデックス描画では実際の
頂点インデックスを解決し、結果の `invocation.resolved_index` に示します。
ピクセルの `x`, `y` は左上原点の整数座標です。同じピクセルを複数の図形が覆う場合は
`primitive`、MSAA の場合は `sample` を指定してください。RenderDoc は fragment index を
直接指定できず、指定した primitive が見つからない場合に別の fragment を選ぶ場合が
あります。選択の正確性が重要な場合は RenderDoc の Pixel History で確認してください。

トレースは RenderDoc が生成した選択対象の実行結果です。ソース・変数名はシェーダーの
デバッグ情報に依存し、すべての GPU lane やリソースの全内容を含むものではありません。
一度に一つのデバッグのみ実行できます。キャンセルは `release_debug_trace` で要求でき、
進行中の RenderDoc ネイティブ処理が戻るまで待機する場合があります。トレースの保存容量は
合計 256 MiB で、容量超過時は `failed` として部分結果が残ります。不要なトレースは
解放してください。RenderDoc が返す一つの実行バッチ自体のサイズは制限できません。
トレースは拡張機能内の一時データで、拡張機能の再読み込みまたは終了時に消えます。
異なる MCP サーバープロセスを同時に使う場合は、それぞれ対応する RenderDoc と同じ
`RENDERDOC_MCP_IPC_DIR` を設定してディレクトリを分離してください。

`source_steps` は連続するソース位置とコールスタックごとに確定済みステップを集約します。
命令番号の後退や反復は新しいグループに分け、変数の最初と最後の値を保持します。
途中の値は省略しますが、元の `states` から取得できます。実行中の末尾グループは位置変更または終了時に公開します。

`source_variables` の `mapping_ids` は `mapping_definitions` の完全な定義を順番通りに参照します。
`source_steps` の `kind="auxiliary"` は変数変更もイベントフラグもない連続した正確な `nop;` を表し、範囲と件数を保持します。

### パイプライン状態の取得

```
get_pipeline_state(event_id=123)
```

### テクスチャデータの取得

```
# 2Dテクスチャのmip 0を取得
get_texture_data(resource_id="ResourceId::123")

# 特定のmipレベルを取得
get_texture_data(resource_id="ResourceId::123", mip=2)

# キューブマップの特定の面を取得 (0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-)
get_texture_data(resource_id="ResourceId::456", slice=3)

# 3Dテクスチャの特定の深度スライスを取得
get_texture_data(resource_id="ResourceId::789", depth_slice=5)
```

### テクスチャビューの取得

```
# イベント 123 で ResourceId::123 がどのビューとしてバインドされているかを列挙
get_texture_views(resource_id="ResourceId::123", event_id=123)
```

ビューはイベントのパイプライン状態から解決されるため、同じテクスチャでも
イベントによってバインド方法が異なります。SRV/UAV/レンダーターゲットなどの
種別、スロット、ビューのメタデータが返ります。

### テクスチャのエクスポート

```
# 2D テクスチャの mip 0 を PNG で書き出す
export_texture_2d(
    resource_id="ResourceId::123",
    event_id=123,
    output_path="D:\\out\\texture.png",
    file_type="png",
)

# キューブマップの面 3 (Y-) を EXR で書き出す
export_texture_2d(
    resource_id="ResourceId::456",
    event_id=123,
    output_path="D:\\out\\face3.exr",
    file_type="exr",
    array_slice=3,
)

# 3D テクスチャを 8 列のアトラスとして書き出す
export_texture_3d_atlas(
    resource_id="ResourceId::789",
    event_id=123,
    output_path="D:\\out\\volume_atlas.png",
    file_type="png",
    columns=8,
)
```

`export_texture_2d` は 1 つの 2D サブリソース（`mip` / `array_slice` / `sample`）を
書き出します。Texture3D は受け付けません。
`export_texture_3d_atlas` は深度スライスを `columns` 列のグリッドに並べた
2D 画像を書き出し、最終行はスライス数に応じて余白が入ります。

`file_type` は `dds` / `png` / `tga` / `exr` のいずれかです。DDS の出力も変換が
発生する場合があります。3D アトラスは DDS でも平坦化された 2D 画像です。
`png` / `tga` / `exr` は表示用に変換された 2D 画像で、`exr` は HDR 値を保持できます。
変換時は形式に応じてビット深度や値域が変わり、クランプや量子化が起こり得ます。
`event_id` は既存のアクションイベントを指定し、そのリプレイ状態から出力します。
`output_path` は絶対パスで、親ディレクトリが存在し、拡張子が `file_type` と一致する必要があります。
既存ファイルは上書きせず、出力先ファイルシステムはハードリンクに対応している必要があります。
RenderDoc 1.48 の 3D 高 mip 出力には安全性の問題があるため、3D アトラスは現在 mip 0
のみです。深度 1 の 3D テクスチャには `columns=1` を指定してください。
出力はリソース形式として解釈され、特定ビューの swizzle は適用されません。

出力サイズと形式から推定したメモリ使用量を 1 GiB に制限し、アトラスには追加のチェックも行います。
これは実際のピークメモリ使用量を保証するものではありません。TGA の幅と高さは 65,535 以下です。

> **注意**: テクスチャの書き出しと 3D アトラスは RenderDoc 1.48 での実機検証がまだ完了していません。
> DDS アトラスは一部の形式を受け付けず、ブロック圧縮の場合は各スライスの幅と高さが 4 の倍数である必要があります。
> 大きな出力は大量のメモリを使用し、RenderDoc の UI を長時間ブロックする場合があります。
> タイムアウトは処理のキャンセルを意味せず、後からファイルが作成される場合があります。

### バッファデータの部分取得

```
# バッファ全体を取得
get_buffer_contents(resource_id="ResourceId::123")

# オフセット256から512バイト取得
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

## 要件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.48 を対象（1.48 での書き出しは未検証）

> **Note**: Windows の RenderDoc 1.45 と DirectX 12 キャプチャで一部のシェーダーデバッグを確認しています。
> すべての機能、他の API、Linux/macOS での動作を保証するものではありません。

## ライセンス

MIT
