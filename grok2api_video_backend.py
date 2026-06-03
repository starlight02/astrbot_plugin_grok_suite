import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
import aiohttp
from astrbot.api import logger


VIDEO_GENERATION_PATH = "/v1/videos"
VIDEO_MODEL = "grok-imagine-video"
API_SCOPE = "video"
SUPPORTED_SECONDS = (6, 10, 12, 16, 20)
SUPPORTED_RESOLUTIONS = ("480p", "720p")
SUPPORTED_PRESETS = ("custom", "fun", "normal", "spicy")
SUPPORTED_SIZES = (
    "720x1280",
    "1280x720",
    "1024x1024",
    "1024x1792",
    "1792x1024",
)


def _configured_model(plugin: Any, config_key: str, default_model: str) -> str:
    configured = str(plugin.conf.get(config_key, "") or "").strip()
    xai_defaults = {
        getattr(plugin, "DEFAULT_VIDEO_MODEL", ""),
        getattr(plugin, "DEFAULT_VIDEO_EDIT_MODEL", ""),
        getattr(plugin, "DEFAULT_VIDEO_EXTENSION_MODEL", ""),
    }
    if not configured or configured in xai_defaults:
        return default_model
    return configured


def _nearest_supported_seconds(value: int) -> int:
    return min(SUPPORTED_SECONDS, key=lambda item: (abs(item - value), -item))


def _normalize_seconds(plugin: Any, value: Optional[int]) -> int:
    if value is None:
        value = plugin._get_configured_video_duration()
    seconds = _nearest_supported_seconds(int(value))
    if seconds != value:
        logger.warning(
            f"[生视频][grok2api] 后端仅支持时长 {SUPPORTED_SECONDS}，"
            f"已将 {value} 秒归一化为 {seconds} 秒"
        )
    return seconds


def _normalize_resolution(plugin: Any, value: Optional[str]) -> str:
    resolution = plugin._normalize_video_resolution(value) or plugin._get_configured_video_resolution()
    if resolution in SUPPORTED_RESOLUTIONS:
        return resolution
    logger.warning(
        f"[生视频][grok2api] 后端仅支持分辨率 {SUPPORTED_RESOLUTIONS}，"
        f"已将 {resolution} 归一化为 720p"
    )
    return "720p"


def _normalize_preset(plugin: Any) -> str:
    preset = str(plugin.conf.get("grok2api_video_preset", "custom") or "custom").strip().lower()
    if preset in SUPPORTED_PRESETS:
        return preset
    logger.warning(
        f"[生视频][grok2api] 后端仅支持 preset {SUPPORTED_PRESETS}，"
        f"已将 {preset} 归一化为 custom"
    )
    return "custom"


def _normalize_size(plugin: Any, target_size: Optional[str], image_payloads: List[bytes]) -> str:
    if target_size in SUPPORTED_SIZES:
        return target_size
    if target_size in plugin.ASPECT_RATIO_TO_SIZE:
        mapped = plugin.ASPECT_RATIO_TO_SIZE[target_size]
        if mapped in SUPPORTED_SIZES:
            return mapped

    if image_payloads:
        image_size = plugin._get_image_resolution(image_payloads[0])
        if image_size:
            matched = plugin._get_closest_supported_size(*image_size)
            if matched in SUPPORTED_SIZES:
                return matched

    if target_size:
        logger.warning(
            f"[生视频][grok2api] 后端不支持视频尺寸/比例 {target_size}，"
            f"已回退为 {plugin.DEFAULT_VIDEO_SIZE}"
        )
    return plugin.DEFAULT_VIDEO_SIZE


def _data_url_to_bytes(value: str) -> Optional[Tuple[bytes, str]]:
    if not value.startswith("data:") or "," not in value:
        return None
    header, encoded = value.split(",", 1)
    mime_type = "image/png"
    if header.startswith("data:"):
        mime_type = header[5:].split(";", 1)[0] or mime_type
    try:
        return base64.b64decode(encoded), mime_type
    except Exception:
        return None


async def _image_input_to_file(
    plugin: Any,
    image_input: Dict[str, str],
    index: int,
) -> Tuple[Optional[Tuple[bytes, str, str]], Optional[str]]:
    if not isinstance(image_input, dict):
        return None, "grok2api 视频参考图输入格式无效"

    if image_input.get("file_id"):
        return None, "grok2api 视频接口不支持 file_id 参考图，请使用图片附件或 image_url"

    url = str(image_input.get("url") or image_input.get("image_url") or "").strip()
    if not url:
        return None, "grok2api 视频参考图缺少 URL 或图片数据"

    if url.startswith("data:image/"):
        decoded = _data_url_to_bytes(url)
        if not decoded:
            return None, "grok2api 视频参考图 base64 解码失败"
        data, mime_type = decoded
    elif url.startswith(("http://", "https://")):
        data = await plugin._download_media(url)
        if not data:
            return None, f"grok2api 视频参考图下载失败: {url[:120]}"
        mime_type = plugin._detect_mime_type(data)
    else:
        return None, "grok2api 视频参考图仅支持图片附件、data URL 或 HTTP(S) 图片 URL"

    if not plugin._is_supported_edit_image_mime(mime_type):
        return None, "grok2api 视频参考图仅支持 JPEG、PNG、WebP 格式"

    ext = "jpg" if mime_type == "image/jpeg" else mime_type.rsplit("/", 1)[-1]
    return (data, f"reference_{index}.{ext}", mime_type), None


async def _collect_reference_files(
    plugin: Any,
    image_input: Optional[Dict[str, str]],
    reference_images: Optional[List[Dict[str, str]]],
) -> Tuple[Optional[List[Tuple[bytes, str, str]]], Optional[str]]:
    inputs: List[Dict[str, str]] = []
    if image_input:
        inputs.append(image_input)
    inputs.extend(item for item in (reference_images or []) if item)
    if len(inputs) > plugin.MAX_VIDEO_REFERENCE_IMAGES:
        return None, f"grok2api 图生视频最多支持 {plugin.MAX_VIDEO_REFERENCE_IMAGES} 张参考图"

    files: List[Tuple[bytes, str, str]] = []
    for index, item in enumerate(inputs, start=1):
        payload, error = await _image_input_to_file(plugin, item, index)
        if error:
            return None, error
        if payload:
            files.append(payload)
    return files, None


def _extract_video_result_url(plugin: Any, data: Dict[str, Any]) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    video_obj = data.get("video")
    if isinstance(video_obj, dict):
        for key in ("url", "video_url", "media_url", "file_url"):
            value = video_obj.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

    for key in ("url", "video_url", "media_url", "file_url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    url, _, _ = plugin._parse_json_response(data)
    return url


def _extract_video_error(data: Dict[str, Any]) -> str:
    error_obj = data.get("error") if isinstance(data, dict) else None
    if isinstance(error_obj, dict):
        message = str(error_obj.get("message", "")).strip()
        if message:
            return message
    if isinstance(error_obj, str) and error_obj.strip():
        return error_obj.strip()
    message = data.get("message") if isinstance(data, dict) else None
    return str(message or "").strip()


async def _download_video_content(
    plugin: Any,
    video_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    api_url = plugin._build_api_url(f"/v1/videos/{video_id}/content", API_SCOPE)
    try:
        session = await plugin._ensure_session()
        headers = plugin._get_auth_headers(API_SCOPE)
        plugin._debug_log_request(
            "视频内容下载[grok2api]",
            "GET",
            api_url,
            headers=headers,
        )
        async with session.get(
            api_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=plugin.VIDEO_TIMEOUT),
        ) as resp:
            content = await resp.read()
            if resp.status != 200:
                text = content.decode("utf-8", errors="ignore")
                plugin._log_error_response("视频内容下载[grok2api]", resp.status, text)
                detail = plugin._extract_api_error_message(text)
                return None, plugin._translate_error(detail or f"状态码: {resp.status}")

            save_path = (Path(plugin.temp_dir) / f"grok2api_video_{video_id}_{uuid.uuid4().hex[:8]}.mp4").resolve()
            async with aiofiles.open(save_path, "wb") as f:
                await f.write(content)
            logger.info(f"[生视频][grok2api] 视频内容已下载: {save_path}")
            return str(save_path), None
    except (asyncio.TimeoutError, aiohttp.ClientError):
        return None, "视频下载超时，请重试"
    except Exception as e:
        logger.error(f"[生视频][grok2api] 视频内容下载异常: {e}")
        return None, plugin._translate_error(str(e))


async def _poll_video_task(
    plugin: Any,
    video_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    api_url = plugin._build_api_url(f"/v1/videos/{video_id}", API_SCOPE)
    started_at = time.monotonic()
    last_status = ""

    while time.monotonic() - started_at < plugin.VIDEO_TIMEOUT:
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_auth_headers(API_SCOPE)
            plugin._debug_log_request(
                "生视频轮询[grok2api]",
                "GET",
                api_url,
                headers=headers,
            )
            async with session.get(
                api_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    plugin._log_error_response("生视频轮询[grok2api]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    if plugin._is_retryable_status(resp.status):
                        await asyncio.sleep(plugin.VIDEO_POLL_INTERVAL_SECONDS)
                        continue
                    return None, plugin._translate_error(detail or f"状态码: {resp.status}")

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"[生视频][grok2api] 轮询响应 JSON 解析失败: {text}")
                    return None, "API响应格式异常"

                status = str(data.get("status", "")).strip().lower()
                if status and status != last_status:
                    logger.info(f"[生视频][grok2api] 任务 {video_id} 状态: {status}")
                    last_status = status

                if status in {"done", "succeeded", "completed", "success"}:
                    video_url = _extract_video_result_url(plugin, data)
                    if video_url:
                        return video_url, None
                    return await _download_video_content(plugin, video_id)

                if not status:
                    video_url = _extract_video_result_url(plugin, data)
                    if video_url:
                        return video_url, None

                if status in {"failed", "expired", "cancelled", "canceled", "error"}:
                    detail = _extract_video_error(data)
                    return None, plugin._translate_error(detail or f"视频任务状态: {status}")

                await asyncio.sleep(plugin.VIDEO_POLL_INTERVAL_SECONDS)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            await asyncio.sleep(plugin.VIDEO_POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.error(f"[生视频][grok2api] 轮询异常: {e}")
            return None, plugin._translate_error(str(e))

    return None, "视频生成超时，请稍后重试"


async def generate_video(
    plugin: Any,
    prompt: str,
    image_input: Optional[Dict[str, str]] = None,
    target_size: Optional[str] = None,
    video_length: Optional[int] = None,
    *,
    reference_images: Optional[List[Dict[str, str]]] = None,
    resolution: Optional[str] = None,
    aspect_ratio_explicit: bool = False,
    user: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    del aspect_ratio_explicit, user

    reference_files, error = await _collect_reference_files(plugin, image_input, reference_images)
    if error:
        return None, error
    reference_files = reference_files or []

    if not prompt.strip() and not reference_files:
        return None, "文生视频需要提示词"

    # grok2api /v1/videos maps input_reference[] to upstream reference-to-video.
    # It is not xAI's first-frame image-to-video `image` field.
    mode = "reference-to-video" if reference_files else "text-to-video"
    enhanced_prompt = plugin._build_video_prompt(prompt, mode) if prompt.strip() else prompt
    seconds = _normalize_seconds(plugin, video_length)
    resolution_name = _normalize_resolution(plugin, resolution)
    preset = _normalize_preset(plugin)
    size = _normalize_size(
        plugin,
        target_size,
        [payload for payload, _, _ in reference_files],
    )

    configured_model = _configured_model(plugin, "grok_video_model", VIDEO_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=[VIDEO_MODEL],
        scene="生视频",
        scope=API_SCOPE,
    )

    def build_form() -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("prompt", enhanced_prompt)
        form.add_field("seconds", str(seconds))
        form.add_field("size", size)
        form.add_field("resolution_name", resolution_name)
        form.add_field("preset", preset)
        for payload, filename, mime_type in reference_files:
            form.add_field(
                "input_reference[]",
                payload,
                filename=filename,
                content_type=mime_type,
            )
        return form

    form_debug: List[Dict[str, Any]] = [
        {"name": "model", "value": model},
        {"name": "prompt", "value": enhanced_prompt},
        {"name": "seconds", "value": str(seconds)},
        {"name": "size", "value": size},
        {"name": "resolution_name", "value": resolution_name},
        {"name": "preset", "value": preset},
    ]
    for payload, filename, mime_type in reference_files:
        form_debug.append(
            plugin._build_form_file_debug_field(
                "input_reference[]",
                payload,
                filename=filename,
                content_type=mime_type,
            )
        )

    logger.info(
        f"[生视频][grok2api] 请求参数: model={model}, mode={mode}, seconds={seconds}, "
        f"size={size}, resolution_name={resolution_name}, preset={preset}, "
        f"references={len(reference_files)}"
    )

    api_url = plugin._build_api_url(VIDEO_GENERATION_PATH, API_SCOPE)
    last_error: Optional[str] = None
    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        attempt_started_at = time.monotonic()
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_auth_headers(API_SCOPE)
            plugin._debug_log_request(
                "生视频[grok2api]",
                "POST",
                api_url,
                headers=headers,
                form_body=form_debug,
                attempt=attempt + 1,
            )
            async with session.post(
                api_url,
                headers=headers,
                data=build_form(),
                timeout=aiohttp.ClientTimeout(total=plugin.VIDEO_TIMEOUT),
            ) as resp:
                text = await resp.text()
                if resp.status not in (200, 201, 202):
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    last_error = translated_error
                    plugin._log_error_response("生视频[grok2api]", resp.status, text)
                    if (
                        plugin._is_retryable_status(resp.status)
                        and attempt < plugin.MAX_REQUEST_RETRIES - 1
                    ):
                        await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                        continue
                    return None, translated_error

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"[生视频][grok2api] 启动响应 JSON 解析失败: {text}")
                    return None, "API响应格式异常"

                direct_url = _extract_video_result_url(plugin, data)
                if direct_url:
                    return direct_url, None

                video_id = str(
                    data.get("video_id") or data.get("request_id") or data.get("id") or ""
                ).strip()
                if not video_id:
                    logger.error(f"[生视频][grok2api] 启动响应缺少 video_id: {text}")
                    return None, "API响应中未包含 video_id"

                return await _poll_video_task(plugin, video_id)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            elapsed = time.monotonic() - attempt_started_at
            logger.warning(
                f"[生视频][grok2api] 启动请求第 {attempt + 1}/"
                f"{plugin.MAX_REQUEST_RETRIES} 次失败，耗时 {elapsed:.1f}s: "
                f"{e.__class__.__name__}: {e}"
            )
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            last_error = (
                "视频任务启动请求超时或连接中断，未拿到 video_id；"
                "请检查后端或中间代理是否在创建任务阶段提前断开连接"
            )
        except Exception as e:
            elapsed = time.monotonic() - attempt_started_at
            logger.warning(
                f"[生视频][grok2api] 启动请求第 {attempt + 1}/"
                f"{plugin.MAX_REQUEST_RETRIES} 次异常，耗时 {elapsed:.1f}s: "
                f"{e.__class__.__name__}: {e}"
            )
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[生视频][grok2api] 请求异常: {e}")
            last_error = plugin._translate_error(str(e))

    return None, last_error or "视频请求失败"


async def edit_video(
    plugin: Any,
    prompt: str,
    video_input: Dict[str, str],
    *,
    source_label: Optional[str] = None,
    user: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    del plugin, prompt, video_input, source_label, user
    return None, "grok2api 后端未提供视频编辑接口，请将视频后端类型切换为 xAI"


async def extend_video(
    plugin: Any,
    prompt: str,
    video_input: Dict[str, str],
    *,
    duration: Optional[int] = None,
    source_label: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    del plugin, prompt, video_input, duration, source_label
    return None, "grok2api 后端未提供视频扩展接口，请将视频后端类型切换为 xAI"
