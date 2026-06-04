# Grok AI 助手（xAI版）

Grok 全能插件：文生图、图生图、图生视频、视频编辑、视频扩展、智能对话（自动联网）、LLM Tool 调用，一站式 AI 多媒体体验。

## 功能

| 功能 | 命令 | 说明 |
|------|------|------|
| 文生图 | `/grok生图 [数量] [尺寸] 提示词` | 根据文字描述生成图片 |
| 图生图 | `/grok生图 提示词 + 图片` | 基于参考图片进行编辑/重绘 |
| 生视频 | `/grok视频 [比例/null] [时长] [分辨率] [提示词] [+图片可选]` | 支持文生视频、图生视频与参考图视频 |
| 视频编辑 | `/grok视频编辑 [视频URL] 提示词 [+视频可选]` | 基于输入视频做内容编辑 |
| 视频扩展 | `/grok视频扩展 [时长] [视频URL] 提示词 [+视频可选]` | 继续生成后续片段 |
| 智能对话 | `/grok 内容 [+图片/语音/文件可选]` | 与 Grok 对话，自动判断是否需要联网 |
| 帮助 | `/grok帮助` | 查看使用说明 |

## 配置说明

### API 配置

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `grok_api_url` | 全局 API 基础地址 | `https://api.x.ai` |
| `grok_api_key` | 全局 API 密钥 | 你的 xAI API Key |
| `grok_image_api_url` | 生图 API 基础地址；为空继承全局地址 | `https://api.x.ai` |
| `grok_image_api_key` | 生图 API 密钥；为空继承全局密钥 | 生图专用 Key |
| `grok_video_api_url` | 视频 API 基础地址；为空继承全局地址 | `https://api.x.ai` |
| `grok_video_api_key` | 视频 API 密钥；为空继承全局密钥 | 视频专用 Key |

**URL 配置说明**：只需填写基础 URL，插件会自动拼接正确的接口路径。对话/搜索始终使用全局 `grok_api_url` / `grok_api_key`；生图和视频优先使用各自专用配置，专用配置为空时才继承全局配置。

支持的 URL 格式（以下均可正常工作）：
- `https://api.x.ai`
- `https://api.x.ai/v1`
- `https://api.x.ai/v1/chat/completions`

### 模型配置

| 配置项 | 功能 | 默认值 | 接口 |
|--------|------|--------|------|
| `grok_image_backend_type` | 生图后端类型 | `xAI` | `xAI` / `grok2api` / `OpenAI` |
| `grok_image_model` | 文生图 | `grok-imagine-image-quality` | `/v1/images/generations` |
| `grok_edit_model` | 图生图 | `grok-imagine-image-quality` | `/v1/images/edits` |
| `grok_image_resolution` | 图片分辨率 | `2k` | `/v1/images/generations` / `/v1/images/edits` |
| `grok_image_response_format` | 图片响应格式 | `b64_json` | 适用于 xAI/grok2api/OpenAI 图片后端；`b64_json` / `auto` / `url` |
| `grok_image_timeout_seconds` | 图片请求超时时间 | `150` | 生图/图生图 |
| `grok_video_backend_type` | 视频后端类型 | `xAI` | `xAI` / `grok2api` / `OpenAI` |
| `grok2api_video_preset` | grok2api 视频模式 | `custom` | `custom` / `fun` / `normal` / `spicy` |
| `grok_video_model` | 生视频 | `grok-imagine-video` | `/v1/videos/generations` |
| `grok_video_edit_model` | 视频编辑 | `grok-imagine-video` | `/v1/videos/edits` |
| `grok_video_extension_model` | 视频扩展 | `grok-imagine-video` | `/v1/videos/extensions` |
| `grok_video_resolution` | 视频分辨率 | `720p` | `/v1/videos/generations` |
| `grok_video_aspect_ratio` | 视频默认比例 | `16:9` | `/v1/videos/generations`，可选 `null` 发送 JSON null |
| `grok_video_duration` | 视频默认时长 | `8` | `/v1/videos/generations` |
| `grok_video_output_upload_url` | 视频输出上传 URL | 空 | `/v1/videos/*` |
| `grok_video_timeout_seconds` | 视频任务最长等待秒数 | `300` | 视频创建/轮询/下载 |
| `grok_video_poll_interval_seconds` | 视频轮询间隔秒数 | `5` | 视频状态查询 |
| `grok_video_extension_duration` | 视频扩展时长 | `6` | `/v1/videos/extensions` |
| `grok_search_model` | 对话/搜索 | `grok-4-fast` | `/v1/chat/completions` |

**后端说明**：图片和视频后端在插件 UI 中分别配置。`xAI` 使用 xAI 官方 JSON 接口；`grok2api` 使用 [chenyme/grok2api](https://github.com/chenyme/grok2api) 源码确认的独立接口格式；`OpenAI` 使用 OpenAI Python SDK。对话/搜索不受图片/视频后端选择影响。

**模型说明**：所有模型均通过配置项读取，代码中的默认值仅作为备用。你可以根据 API 提供商支持的模型自行修改。

常见可用模型（参考 [grok2api](https://github.com/chenyme/grok2api)）：

| 模型 | 类型 | 说明 |
|------|------|------|
| `grok-imagine-image-quality` | 图像生成/编辑 | 官方高质量图片模型 |
| `grok-imagine-image` | 图像生成/编辑 | 官方图片模型 |
| `grok-imagine-1.0` / `grok-imagine-1.0-edit` | 图像生成/编辑 | 兼容部分第三方代理 |
| `grok-imagine-video` | 视频生成/编辑/扩展 | 官方视频模型 |
| `grok-imagine-1.0-video` | 视频生成 | 兼容部分第三方代理 |
| `gpt-image-1` | OpenAI 图像生成/编辑 | 选择 `OpenAI` 图片后端时的默认 SDK 模型 |
| `sora-2` | OpenAI 视频生成 | 选择 `OpenAI` 视频后端时的默认 SDK 模型 |
| `grok-3` / `grok-4` / `grok-4-fast` | 对话+搜索 | 支持对话和联网搜索 |

### 对话/搜索配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `grok_search_model` | 对话/搜索模型 | `grok-4-fast` |
| `grok_search_mode` | 搜索模式 | `auto` |
| `grok_search_enable_thinking` | 开启思考模式 | `true` |
| `grok_search_thinking_budget` | 思考 token 预算 | `32000` |
| `grok_search_timeout_seconds` | 请求超时时间 | `60` |
| `grok_search_show_sources` | 显示来源链接 | `false` |
| `grok_search_max_sources` | 最多显示来源数 | `5` |
| `grok_search_extra_body` | 额外请求体 (JSON) | `{}` |
| `grok_search_extra_headers` | 额外请求头 (JSON) | `{}` |
| `grok_search_enable_skill` | 启用 Skill 模式 | `false` |

**搜索模式说明**：
- `auto`（默认）：模型自动判断是否需要联网搜索
- `on`：始终使用联网搜索
- `off`：纯对话模式，不联网

### 其他配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `save_media` | 是否保存生成的媒体文件 | `false` |
| `user_whitelist` | 用户白名单（空=不限制） | `[]` |
| `user_blacklist` | 用户黑名单 | `[]` |
| `group_whitelist` | 群聊白名单（空=不限制） | `[]` |
| `group_blacklist` | 群聊黑名单 | `[]` |

## 使用示例

### 文生图

```
/grok生图 一只可爱的猫咪
/grok生图 4 3:2 日落海滩风景
/grok生图 1:1 赛博朋克城市夜景
/grok生图 9:16 一只猫
/grok生图 4 1792x1024 日落海滩风景
```

参数说明：
- 数量：1-10（默认 1）
- 尺寸支持两种格式：
  - 比例格式：`1:1` / `2:3` / `3:2` / `9:16` / `16:9`
  - 像素格式：`1024x1024` / `1024x1792` / `1280x720` / `1792x1024` / `720x1280`
- 不加尺寸参数时，默认使用：`9:16`（720x1280）
- 参数顺序任意，如 `4 3:2` 或 `3:2 4` 均可

### 图生图

发送图片或引用图片，附带命令：
```
/grok生图 把背景换成森林
/grok生图 3:2 转换为油画风格
/grok生图 4 添加下雪效果
```

说明：
- 自动读取原图分辨率；grok2api 未显式指定尺寸时使用原图尺寸，xAI/OpenAI 会按后端支持范围映射
- 支持数量参数，最多 10 张
- 显式输入比例/尺寸时会覆盖自动匹配结果
- 最多读取 3 张图片；多张图片会以官方 `images` 字段一起作为参考输入

### 生视频（文生/图生）

可直接发文字，或发送图片/引用图片后附带命令：
```
/grok视频 让画面动起来
/grok视频 10 夜晚海边的慢镜头
/grok视频 3:2 夜晚海边的慢镜头
/grok视频 16:9 6 720p 让人物眨眼微笑
/grok视频 null 8 720p 抽象粒子缓慢旋转
/grok视频 1280x720 12 添加飘落的樱花
```

说明：
- 文生视频/参考图视频默认比例：读取 UI 配置 `grok_video_aspect_ratio`，默认 `16:9`
- `aspect_ratio` 对齐官方 `null | string`：配置或命令输入 `null` / `none` / `auto` / `不传` 时发送 JSON `null`
- 文生视频/单图图生视频默认时长：读取 UI 配置 `grok_video_duration`，默认 `8` 秒；可输入 `1`-`15` 秒覆盖
- 比例支持：`16:9` / `9:16` / `1:1` / `4:3` / `3:4` / `3:2` / `2:3`
- 支持比例格式（如 `3:2`、`16:9`）或可换算为官方比例的像素格式（如 `1280x720`）
- 分辨率支持：`480p` / `720p` / `1080p`，默认读取 UI 配置 `grok_video_resolution`
- xAI 单图图生视频使用官方 I2V 模式：图片作为视频首帧，默认不传 `aspect_ratio` 保持原图比例；显式输入比例/尺寸时覆盖，显式输入 `null` 时发送 JSON `null`
- 单图图生视频的提示词可省略；文生视频与参考图视频仍需要提示词
- 多图使用官方 R2V `reference_images` 模式：图片作为主体/风格/场景参考，不作为首帧；最多 7 张，时长最多 10 秒；单张图片可加 `参考图` 强制使用 R2V
- grok2api 生视频优先走 `/v1/videos` 的 `input_reference[]` 字段；上游会按参考图视频语义处理上传图片，不等同于 xAI 的官方首帧 `image` 字段；如果专用接口返回参数类错误，会回退到 `/v1/chat/completions`
- OpenAI 生视频走 SDK `client.videos.create(...)`，使用 `seconds` / `size`，最多传 1 个 `input_reference`
- 图片输入支持消息附件、`file_id:xxx`，或 `image_url:https://...`
- xAI/OpenAI 使用异步任务：先创建任务，再轮询状态并下载视频内容
- 轮询默认每 5 秒一次，最长等待 300 秒；可通过 `grok_video_poll_interval_seconds` / `grok_video_timeout_seconds` 调整
- 自动启用增强策略（高细节、低噪点、时序稳定）

### 视频编辑 / 扩展

```
/grok视频编辑 给人物添加银色项链 +视频
/grok视频扩展 6 镜头继续向前推进 +视频
```

说明：
- 视频编辑：提交原视频和提示词，接口为 `/v1/videos/edits`
- 视频扩展：提交原视频、提示词和扩展时长，接口为 `/v1/videos/extensions`
- 使用 `grok2api` 视频后端时，当前仅支持生视频；视频编辑和视频扩展会提示切换到 `xAI` 或 `OpenAI` 后端
- 使用 `OpenAI` 视频后端时，编辑/扩展走 SDK `client.videos.edit(...)` / `client.videos.extend(...)`
- 视频输入支持消息附件、mp4 直链、可下载的视频 URL，或 `file_id:xxx`
- 可选配置 `grok_video_output_upload_url` 会作为官方 `output.upload_url` 发送
- 生视频/视频编辑会自动把发送者 ID 作为官方 `user` 字段发送；视频扩展接口官方文档未列出 `user`
- 两者都走异步任务，先返回 `request_id`，再轮询结果

### 智能对话

```
/grok 你好，介绍一下你自己
/grok 帮我写一首关于春天的诗
/grok 今天的新闻有哪些（自动联网）
/grok 最新的 AI 技术进展（自动联网）
/grok 帮我总结这段语音内容（附语音）
/grok 请提炼这个文件的重点（附文件）
```

说明：
- 默认 `auto` 模式：模型自动判断是否需要联网
- 普通问题直接回答，时效性问题自动联网搜索
- 支持图片、语音、文件的多模态理解
- 可通过配置切换为始终联网或纯对话模式
- 支持作为 LLM Tool 被其他插件调用

## API 接口对照

图片和视频请求按 UI 中选择的后端分发到不同模块；API URL 和 Key 支持图片/视频分别配置，留空时继承全局配置。

### xAI 官方接口

| 功能 | 接口路径 | 请求格式 |
|------|----------|----------|
| 文生图 | `POST /v1/images/generations` | JSON，发送 `aspect_ratio`、`resolution`、`response_format` |
| 图生图 | `POST /v1/images/edits` | JSON，发送 `image` / `images`、`resolution`、`response_format`，仅在显式输入比例时发送 `aspect_ratio` |
| 生视频 | `POST /v1/videos/generations` + `GET /v1/videos/{request_id}` | JSON |
| 视频编辑 | `POST /v1/videos/edits` + `GET /v1/videos/{request_id}` | JSON |
| 视频扩展 | `POST /v1/videos/extensions` + `GET /v1/videos/{request_id}` | JSON |
| 对话/搜索 | `POST /v1/chat/completions` | JSON |

### grok2api 兼容接口

| 功能 | 接口路径 | 请求格式 |
|------|----------|----------|
| 文生图 | `POST /v1/images/generations`，参数错误时回退 `POST /v1/chat/completions` | 专用接口 JSON 发送 `size`、`response_format`；回退接口发送 `messages`、`image_config` |
| 图生图 | `POST /v1/images/edits`，参数错误时回退 `POST /v1/chat/completions` | 专用接口 multipart 按 grok2api 源码字段 `image[]` 发送；`size` 优先使用命令尺寸/比例，未指定时使用原图尺寸；回退接口发送 `messages`、`image_config` |
| 生视频 | `POST /v1/videos` + `GET /v1/videos/{video_id}` + `GET /v1/videos/{video_id}/content`，参数错误时回退 `POST /v1/chat/completions` | 专用接口 multipart 发送 `seconds`、`size`、`resolution_name`、`preset`、可选 `input_reference[]`；回退接口发送 `messages`、`video_config` |
| 视频编辑 | 不支持 | grok2api 文档未提供视频编辑接口 |
| 视频扩展 | 不支持 | grok2api 文档未提供视频扩展接口 |
| 对话/搜索 | `POST /v1/chat/completions` | JSON，不受图片/视频后端选择影响 |

### OpenAI SDK 后端

| 功能 | SDK 调用 | 请求格式 |
|------|----------|----------|
| 文生图 | `client.images.generate(...)` | 发送 `model`、`prompt`、`n`、`size`、`response_format` |
| 图生图 | `client.images.edit(...)` | 发送 `model`、`prompt`、`image`、`n`、`size`、`response_format` |
| 生视频 | `client.videos.create(...)` + `retrieve(...)` + `download_content(...)` | 发送 `model`、`prompt`、`seconds`、`size`、可选单个 `input_reference` |
| 视频编辑 | `client.videos.edit(...)` + `retrieve(...)` + `download_content(...)` | 发送 `prompt`、`video` |
| 视频扩展 | `client.videos.extend(...)` + `retrieve(...)` + `download_content(...)` | 发送 `prompt`、`seconds`、`video` |

OpenAI SDK 图片尺寸会映射到 SDK 支持的 `1024x1024` / `1536x1024` / `1024x1536` 等尺寸；视频时长会映射到 SDK 支持的 `4` / `8` / `12` 秒，扩展时长会映射到 `4` / `8` / `12` / `16` / `20` 秒。

### 官方视频字段对照

| 官方字段 | `/v1/videos/generations` 插件支持情况 |
|----------|----------------------------------------|
| `model` | 由 `grok_video_model` 配置，默认 `grok-imagine-video` |
| `prompt` | 文生视频/参考图视频必填；单图图生视频可省略 |
| `image` | I2V 单图首帧模式使用，支持消息图片、`file_id:xxx`、`image_url:`；默认按官方 object 结构发送，遇到别名层要求 string 时自动压平成 URL/data URL 重试 |
| `reference_images` | R2V 参考图模式使用，支持 1-7 个 `file_id` / URL / data URL；默认按官方 object 数组发送，遇到别名层要求 string 数组时自动压平重试 |
| `aspect_ratio` | 支持官方 `null` / `1:1` / `16:9` / `9:16` / `4:3` / `3:4` / `3:2` / `2:3`；单图未显式输入时不传 |
| `duration` | 由 `grok_video_duration` 配置或命令覆盖；T2V/I2V 支持 `1`-`15` 秒，R2V 支持 `1`-`10` 秒；插件使用官方主字段 `duration`，不使用兼容别名 `seconds` |
| `resolution` | 由 `grok_video_resolution` 配置或命令覆盖，支持 `480p` / `720p` / `1080p` |
| `output.upload_url` | 由 `grok_video_output_upload_url` 配置，生成/编辑/扩展都会发送 |
| `user` | 生视频/视频编辑发送当前用户 ID；视频扩展官方未列出 `user`，插件不发送 |

视频编辑接口发送 `model`、`prompt`、`video`、可选 `output.upload_url`、可选 `user`。视频扩展接口发送 `model`、`prompt`、`video`、`duration`、可选 `output.upload_url`。

图片响应格式由 `grok_image_response_format` 控制，三种图片后端都会生效：xAI 会在 JSON 请求中发送 `response_format`，grok2api 会在专用接口和 `/v1/chat/completions` 回退的 `image_config` 中发送，OpenAI 后端会传给 SDK `client.images.generate/edit(...)`。默认 `b64_json`，避免依赖图片 URL/CDN；选择 `auto` 会先请求 `b64_json`，如果后端明确报 `response_format` 不兼容再尝试 `url`，选择 `url` 则只请求 URL。该配置不影响视频。视频完成后插件会下载内容并作为本地视频文件发送；如果视频 URL 所在域名无法访问，需要使用可访问的代理/API 地址或配置官方 `output.upload_url`。

grok2api 视频接口的 `preset` 由 `grok2api_video_preset` 配置，默认 `custom`，可选 `fun`、`normal`、`spicy`。

## 注意事项

1. **API 兼容性**：本插件支持 xAI 官方后端、grok2api 源码兼容后端，以及 OpenAI SDK 后端
2. **模型名称**：不同 API 提供商支持的模型可能不同，请根据实际情况配置
3. **图片格式**：生图/图生图支持 PNG、JPG、WEBP、GIF、BMP；视频生成的 `image` / `reference_images` 按官方要求应使用 JPEG、PNG 或 WEBP
4. **图片比例**：用户可输入像素格式 `1024x1024`、`1024x1792`、`1280x720`、`1792x1024`、`720x1280`，也可直接输入官方比例 `1:1`、`16:9`、`9:16`、`4:3`、`3:4`、`3:2`、`2:3`、`2:1`、`1:2`、`19.5:9`、`9:19.5`、`20:9`、`9:20`、`auto`
5. **超时设置**：图片生成默认 150 秒，视频生成/编辑/扩展默认 300 秒，对话/搜索默认 60 秒
6. **文件保存**：开启 `save_media` 后，文件保存在插件数据目录的 `images/` 和 `videos/` 子目录
7. **LLM Tool**：对话/搜索功能可作为 LLM Tool 被其他插件或 Agent 调用
8. **Skill 模式**：开启后禁用 LLM Tool，改为通过 Skill 钩子响应
9. **搜索模式**：`auto` 模式下模型会自动判断是否需要联网，无需手动切换
10. **模型容错**：插件会自动探测可用模型，当前配置模型不可用时自动回退
