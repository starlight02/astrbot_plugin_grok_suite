import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiofiles
from astrbot.api import logger


API_SCOPE = "video"
OPENAI_VIDEO_SECONDS = (4, 8, 12)
OPENAI_VIDEO_EXTENSION_SECONDS = (4, 8, 12, 16, 20)
OPENAI_VIDEO_SIZES = ("720x1280", "1280x720", "1024x1792", "1792x1024")

try:
    from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
except ImportError:  # pragma: no cover - handled at runtime by callers
    APIConnectionError = None
    APIStatusError = None
    APITimeoutError = None
    AsyncOpenAI = None


def _ensure_available() -> Optional[str]:
    if AsyncOpenAI is None:
        return "未安装 openai SDK，请安装 requirements.txt 中的 openai 依赖"
    return None


def _sdk_base_url(plugin: Any) -> str:
    return f"{plugin._get_base_url(API_SCOPE)}/v1"


def _status_error_classes() -> Tuple[Any, ...]:
    return (APIStatusError,) if APIStatusError is not None else ()


def _transport_error_classes() -> Tuple[Any, ...]:
    classes = []
    if APIConnectionError is not None:
        classes.append(APIConnectionError)
    if APITimeoutError is not None:
        classes.append(APITimeoutError)
    return tuple(classes)


def _response_to_dict(response: Any) -> Dict[str, Any]:
    if hasattr(response, "model_dump"):
        data = response.model_dump()
        return data if isinstance(data, dict) else {}
    if hasattr(response, "to_dict_recursive"):
        data = response.to_dict_recursive()
        return data if isinstance(data, dict) else {}
    if isinstance(response, dict):
        return response
    try:
        return json.loads(str(response))
    except Exception:
        return {}


def _status_error_text(error: Any) -> str:
    response = getattr(error, "response", None)
    if response is not None:
        try:
            return str(response.text)
        except Exception:
            pass
    return str(error)


def _translate_status_error(plugin: Any, scene: str, status: int, error_text: str) -> str:
    plugin._log_error_response(f"{scene}[OpenAI]", status, error_text)
    detail = plugin._extract_api_error_message(error_text)
    return plugin._translate_error(detail or f"状态码: {status}")


def _configured_video_model(plugin: Any, config_key: str) -> str:
    configured = str(plugin.conf.get(config_key, "") or "").strip()
    if configured:
        return configured
    default_attr = {
        "grok_video_edit_model": "DEFAULT_VIDEO_EDIT_MODEL",
        "grok_video_extension_model": "DEFAULT_VIDEO_EXTENSION_MODEL",
    }.get(config_key, "DEFAULT_VIDEO_MODEL")
    return str(getattr(plugin, default_attr, "") or "").strip()


def _nearest(value: int, candidates: Sequence[int]) -> int:
    return min(candidates, key=lambda item: (abs(item - value), -item))


def _normalize_video_seconds(value: Optional[int]) -> int:
    requested = int(value or OPENAI_VIDEO_SECONDS[0])
    return _nearest(requested, OPENAI_VIDEO_SECONDS)


def _normalize_extension_seconds(value: Optional[int]) -> int:
    requested = int(value or OPENAI_VIDEO_EXTENSION_SECONDS[1])
    return _nearest(requested, OPENAI_VIDEO_EXTENSION_SECONDS)


def _normalize_openai_video_size(plugin: Any, target_size: Optional[str]) -> str:
    if target_size in OPENAI_VIDEO_SIZES:
        return str(target_size)
    if target_size in plugin.ASPECT_RATIO_TO_SIZE:
        mapped = plugin.ASPECT_RATIO_TO_SIZE[target_size]
        if mapped in OPENAI_VIDEO_SIZES:
            return mapped
    return "1280x720"


def _file_tuple(plugin: Any, item: bytes, index: int, *, prefix: str = "image") -> Tuple[str, bytes, str]:
    mime_type = plugin._detect_mime_type(item)
    ext = "jpg" if mime_type == "image/jpeg" else mime_type.rsplit("/", 1)[-1]
    return f"{prefix}_{index}.{ext}", item, mime_type


def _video_file_tuple(plugin: Any, video_bytes: bytes, filename: str = "video.mp4") -> Tuple[str, bytes, str]:
    return filename, video_bytes, plugin._detect_video_mime_type(video_bytes)


def _data_url_to_bytes(value: str) -> Optional[Tuple[bytes, str]]:
    if not value.startswith("data:") or "," not in value:
        return None
    import base64

    header, encoded = value.split(",", 1)
    mime_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    try:
        return base64.b64decode(encoded), mime_type
    except Exception:
        return None


async def _image_input_to_reference(plugin: Any, image_input: Dict[str, str], index: int) -> Tuple[Optional[Any], Optional[str]]:
    if not isinstance(image_input, dict):
        return None, "OpenAI 视频参考图输入格式无效"
    if image_input.get("file_id"):
        return {"file_id": image_input["file_id"]}, None

    url = str(image_input.get("url") or image_input.get("image_url") or "").strip()
    if not url:
        return None, "OpenAI 视频参考图缺少 URL 或图片数据"
    if url.startswith("data:image/"):
        decoded = _data_url_to_bytes(url)
        if not decoded:
            return None, "OpenAI 视频参考图 base64 解码失败"
        data, mime_type = decoded
    elif url.startswith(("http://", "https://")):
        data = await plugin._download_media(url)
        if not data:
            return None, f"OpenAI 视频参考图下载失败: {url[:120]}"
        mime_type = plugin._detect_mime_type(data)
    else:
        return None, "OpenAI 视频参考图仅支持图片附件、data URL、HTTP(S) 图片 URL 或 file_id"

    if not plugin._is_supported_edit_image_mime(mime_type):
        return None, "OpenAI 视频参考图仅支持 JPEG、PNG、WebP 格式"
    return _file_tuple(plugin, data, index, prefix="reference"), None


async def _video_input_to_reference(plugin: Any, video_input: Dict[str, str]) -> Tuple[Optional[Any], Optional[str]]:
    if not isinstance(video_input, dict):
        return None, "OpenAI 视频输入格式无效"
    if video_input.get("file_id"):
        return {"id": video_input["file_id"]}, None

    url = str(video_input.get("url") or "").strip()
    if not url:
        return None, "OpenAI 视频输入缺少 URL 或 file_id"
    if url.startswith("data:video/"):
        decoded = _data_url_to_bytes(url)
        if not decoded:
            return None, "OpenAI 视频 base64 解码失败"
        data, _ = decoded
    elif url.startswith(("http://", "https://")):
        data = await plugin._download_media(url)
        if not data:
            return None, f"OpenAI 视频下载失败: {url[:120]}"
    else:
        return None, "OpenAI 视频输入仅支持视频附件、data URL、HTTP(S) URL 或 file_id"

    return _video_file_tuple(plugin, data), None


async def _download_video_content(plugin: Any, client: Any, video_id: str, scene: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        plugin._debug_log_request(
            f"{scene}内容下载[OpenAI]",
            "GET",
            f"{_sdk_base_url(plugin)}/videos/{video_id}/content",
            headers=plugin._get_auth_headers(API_SCOPE),
        )
        content = await client.videos.download_content(video_id)
        video_bytes = await content.aread()
        save_path = (Path(plugin.temp_dir) / f"openai_video_{video_id}_{uuid.uuid4().hex[:8]}.mp4").resolve()
        async with aiofiles.open(save_path, "wb") as f:
            await f.write(video_bytes)
        logger.info(f"[{scene}][OpenAI] 视频内容已下载: {save_path}")
        return str(save_path), None
    except _status_error_classes() as e:
        status = int(getattr(e, "status_code", 0) or 0)
        return None, _translate_status_error(plugin, f"{scene}内容下载", status, _status_error_text(e))
    except _transport_error_classes():
        return None, "视频下载超时，请重试"
    except Exception as e:
        logger.error(f"[{scene}][OpenAI] 视频内容下载异常: {e}")
        return None, plugin._translate_error(str(e))


def _extract_video_id(data: Dict[str, Any]) -> str:
    for key in ("id", "video_id", "request_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _poll_video_task(plugin: Any, client: Any, video_id: str, scene: str) -> Tuple[Optional[str], Optional[str]]:
    started_at = time.monotonic()
    last_status = ""
    poll_timeout = plugin._get_configured_video_timeout_seconds()
    poll_interval = plugin._get_configured_video_poll_interval_seconds()
    logger.info(
        f"[{scene}][OpenAI] 开始轮询任务 {video_id}: "
        f"interval={poll_interval:.1f}s, timeout={poll_timeout:.0f}s"
    )

    while time.monotonic() - started_at < poll_timeout:
        try:
            plugin._debug_log_request(
                f"{scene}轮询[OpenAI]",
                "GET",
                f"{_sdk_base_url(plugin)}/videos/{video_id}",
                headers=plugin._get_auth_headers(API_SCOPE),
            )
            response = await client.videos.retrieve(video_id, timeout=30)
            data = _response_to_dict(response)
            status = str(data.get("status", "")).strip().lower()
            if status and status != last_status:
                logger.info(f"[{scene}][OpenAI] 任务 {video_id} 状态: {status}")
                last_status = status

            if status in {"completed", "succeeded", "done"}:
                return await _download_video_content(plugin, client, video_id, scene)
            if status in {"failed", "cancelled", "canceled", "expired"}:
                detail = plugin._extract_api_error_message(json.dumps(data, ensure_ascii=False))
                return None, plugin._translate_error(detail or f"视频任务状态: {status}")
            await asyncio.sleep(poll_interval)
        except _status_error_classes() as e:
            status = int(getattr(e, "status_code", 0) or 0)
            error_text = _status_error_text(e)
            if plugin._is_retryable_status(status):
                await asyncio.sleep(poll_interval)
                continue
            return None, _translate_status_error(plugin, f"{scene}轮询", status, error_text)
        except _transport_error_classes():
            await asyncio.sleep(poll_interval)
        except Exception as e:
            logger.error(f"[{scene}][OpenAI] 轮询异常: {e}")
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
    del resolution, aspect_ratio_explicit, user
    error = _ensure_available()
    if error:
        return None, error

    model = _configured_video_model(plugin, "grok_video_model")
    requested_seconds = int(video_length or plugin._get_configured_video_duration())
    seconds = _normalize_video_seconds(requested_seconds)
    if seconds != requested_seconds:
        logger.warning(
            f"[生视频][OpenAI] SDK 后端仅支持时长 {OPENAI_VIDEO_SECONDS}，"
            f"已将 {requested_seconds} 秒归一化为 {seconds} 秒"
        )
    size = _normalize_openai_video_size(plugin, target_size)

    reference_inputs = [item for item in (reference_images or []) if item]
    if image_input and not reference_inputs:
        reference_inputs = [image_input]
    input_reference = None
    if reference_inputs:
        if len(reference_inputs) > 1:
            logger.warning(
                "[生视频][OpenAI] SDK videos.create 仅支持单个 input_reference，"
                f"已使用第 1 个，忽略其余 {len(reference_inputs) - 1} 个"
            )
        input_reference, error = await _image_input_to_reference(plugin, reference_inputs[0], 1)
        if error:
            return None, error

    if not prompt.strip():
        return None, "OpenAI 生视频需要提示词"

    params: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "seconds": str(seconds),
        "size": size,
    }
    if input_reference is not None:
        params["input_reference"] = input_reference

    debug_body: List[Dict[str, Any]] = [
        {"name": "model", "value": model},
        {"name": "prompt", "value": prompt},
        {"name": "seconds", "value": str(seconds)},
        {"name": "size", "value": size},
    ]
    if isinstance(input_reference, tuple):
        filename, data, content_type = input_reference[:3]
        debug_body.append(
            plugin._build_form_file_debug_field(
                "input_reference",
                data,
                filename=filename or "reference_1.png",
                content_type=content_type or "application/octet-stream",
            )
        )
    elif isinstance(input_reference, dict):
        debug_body.append({"name": "input_reference", "value": input_reference})

    video_timeout = plugin._get_configured_video_timeout_seconds()
    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        try:
            plugin._debug_log_request(
                "生视频[OpenAI]",
                "POST",
                f"{_sdk_base_url(plugin)}/videos",
                headers=plugin._get_auth_headers(API_SCOPE),
                form_body=debug_body,
                attempt=attempt + 1,
            )
            async with AsyncOpenAI(
                api_key=plugin._get_api_key(API_SCOPE),
                base_url=_sdk_base_url(plugin),
                timeout=video_timeout,
                max_retries=0,
            ) as client:
                response = await client.videos.create(**params)
                video_id = _extract_video_id(_response_to_dict(response))
                if not video_id:
                    logger.error(f"[生视频][OpenAI] 启动响应缺少 video id: {_response_to_dict(response)}")
                    return None, "API响应中未包含 video id"
                return await _poll_video_task(plugin, client, video_id, "生视频")
        except _status_error_classes() as e:
            status = int(getattr(e, "status_code", 0) or 0)
            detail = _translate_status_error(plugin, "生视频", status, _status_error_text(e))
            if plugin._is_retryable_status(status) and attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return None, detail
        except _transport_error_classes():
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return None, "请求超时，请重试"
        except Exception as e:
            logger.error(f"[生视频][OpenAI] 请求异常: {e}")
            return None, plugin._translate_error(str(e))

    return None, "视频请求失败"


async def edit_video(
    plugin: Any,
    prompt: str,
    video_input: Dict[str, str],
    *,
    source_label: Optional[str] = None,
    user: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    del source_label, user
    error = _ensure_available()
    if error:
        return None, error

    video_reference, error = await _video_input_to_reference(plugin, video_input)
    if error:
        return None, error
    if not prompt.strip():
        return None, "OpenAI 视频编辑需要提示词"

    model = _configured_video_model(plugin, "grok_video_edit_model")
    params = {"model": model, "prompt": prompt, "video": video_reference}
    debug_body = [{"name": "model", "value": model}, {"name": "prompt", "value": prompt}]
    if isinstance(video_reference, tuple):
        filename, data, content_type = video_reference[:3]
        debug_body.append(
            plugin._build_form_file_debug_field(
                "video",
                data,
                filename=filename or "video.mp4",
                content_type=content_type or "video/mp4",
            )
        )
    elif isinstance(video_reference, dict):
        debug_body.append({"name": "video", "value": video_reference})

    video_timeout = plugin._get_configured_video_timeout_seconds()
    try:
        plugin._debug_log_request(
            "视频编辑[OpenAI]",
            "POST",
            f"{_sdk_base_url(plugin)}/videos/edits",
            headers=plugin._get_auth_headers(API_SCOPE),
            form_body=debug_body,
        )
        async with AsyncOpenAI(
            api_key=plugin._get_api_key(API_SCOPE),
            base_url=_sdk_base_url(plugin),
            timeout=video_timeout,
            max_retries=0,
        ) as client:
            response = await client.videos.edit(**params)
            video_id = _extract_video_id(_response_to_dict(response))
            if not video_id:
                logger.error(f"[视频编辑][OpenAI] 启动响应缺少 video id: {_response_to_dict(response)}")
                return None, "API响应中未包含 video id"
            return await _poll_video_task(plugin, client, video_id, "视频编辑")
    except _status_error_classes() as e:
        status = int(getattr(e, "status_code", 0) or 0)
        return None, _translate_status_error(plugin, "视频编辑", status, _status_error_text(e))
    except _transport_error_classes():
        return None, "请求超时，请重试"
    except Exception as e:
        logger.error(f"[视频编辑][OpenAI] 请求异常: {e}")
        return None, plugin._translate_error(str(e))


async def extend_video(
    plugin: Any,
    prompt: str,
    video_input: Dict[str, str],
    *,
    duration: Optional[int] = None,
    source_label: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    del source_label
    error = _ensure_available()
    if error:
        return None, error

    video_reference, error = await _video_input_to_reference(plugin, video_input)
    if error:
        return None, error
    if not prompt.strip():
        return None, "OpenAI 视频扩展需要提示词"

    requested_seconds = int(duration or plugin._get_configured_video_extension_duration())
    seconds = _normalize_extension_seconds(requested_seconds)
    if seconds != requested_seconds:
        logger.warning(
            f"[视频扩展][OpenAI] SDK 后端仅支持扩展时长 {OPENAI_VIDEO_EXTENSION_SECONDS}，"
            f"已将 {requested_seconds} 秒归一化为 {seconds} 秒"
        )

    model = _configured_video_model(plugin, "grok_video_extension_model")
    params = {"model": model, "prompt": prompt, "seconds": str(seconds), "video": video_reference}
    debug_body = [
        {"name": "model", "value": model},
        {"name": "prompt", "value": prompt},
        {"name": "seconds", "value": str(seconds)},
    ]
    if isinstance(video_reference, tuple):
        filename, data, content_type = video_reference[:3]
        debug_body.append(
            plugin._build_form_file_debug_field(
                "video",
                data,
                filename=filename or "video.mp4",
                content_type=content_type or "video/mp4",
            )
        )
    elif isinstance(video_reference, dict):
        debug_body.append({"name": "video", "value": video_reference})

    video_timeout = plugin._get_configured_video_timeout_seconds()
    try:
        plugin._debug_log_request(
            "视频扩展[OpenAI]",
            "POST",
            f"{_sdk_base_url(plugin)}/videos/extensions",
            headers=plugin._get_auth_headers(API_SCOPE),
            form_body=debug_body,
        )
        async with AsyncOpenAI(
            api_key=plugin._get_api_key(API_SCOPE),
            base_url=_sdk_base_url(plugin),
            timeout=video_timeout,
            max_retries=0,
        ) as client:
            response = await client.videos.extend(**params)
            video_id = _extract_video_id(_response_to_dict(response))
            if not video_id:
                logger.error(f"[视频扩展][OpenAI] 启动响应缺少 video id: {_response_to_dict(response)}")
                return None, "API响应中未包含 video id"
            return await _poll_video_task(plugin, client, video_id, "视频扩展")
    except _status_error_classes() as e:
        status = int(getattr(e, "status_code", 0) or 0)
        return None, _translate_status_error(plugin, "视频扩展", status, _status_error_text(e))
    except _transport_error_classes():
        return None, "请求超时，请重试"
    except Exception as e:
        logger.error(f"[视频扩展][OpenAI] 请求异常: {e}")
        return None, plugin._translate_error(str(e))
