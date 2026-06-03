import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from astrbot.api import logger


VIDEO_GENERATION_PATH = "/v1/videos/generations"
VIDEO_EDIT_PATH = "/v1/videos/edits"
VIDEO_EXTENSION_PATH = "/v1/videos/extensions"
API_SCOPE = "video"


def _video_source_object_to_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if not isinstance(value, dict):
        return None
    for key in ("url", "image_url", "file_id"):
        source = value.get(key)
        if isinstance(source, str) and source:
            return source
    return None


def _build_alias_compat_payload(
    payload: Dict[str, Any],
    error_text: str,
) -> Optional[Dict[str, Any]]:
    if ".Alias.image of type string" not in error_text and ".Alias.reference_images" not in error_text:
        return None

    compat_payload = dict(payload)
    changed = False

    if ".Alias.image of type string" in error_text:
        image_source = _video_source_object_to_string(payload.get("image"))
        if image_source:
            compat_payload["image"] = image_source
            changed = True

    if ".Alias.reference_images" in error_text:
        reference_sources = []
        for item in payload.get("reference_images") or []:
            source = _video_source_object_to_string(item)
            if not source:
                return None
            reference_sources.append(source)
        if reference_sources:
            compat_payload["reference_images"] = reference_sources
            changed = True

    return compat_payload if changed else None


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


def _extract_video_result_duration(data: Dict[str, Any]) -> Optional[int]:
    if not isinstance(data, dict):
        return None

    video_obj = data.get("video")
    if isinstance(video_obj, dict):
        value = video_obj.get("duration")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())

    value = data.get("duration")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _log_video_result_metadata(
    scene: str,
    request_id: str,
    data: Dict[str, Any],
    *,
    expected_duration: Optional[int] = None,
) -> None:
    actual_duration = _extract_video_result_duration(data)
    if actual_duration is None:
        return

    if expected_duration is not None and actual_duration != expected_duration:
        logger.warning(
            f"[{scene}][xAI] 任务 {request_id} 返回视频时长 {actual_duration} 秒，"
            f"请求 duration={expected_duration} 秒"
        )
        return

    logger.info(f"[{scene}][xAI] 任务 {request_id} 返回视频时长 {actual_duration} 秒")


def _add_optional_fields(plugin: Any, payload: Dict[str, Any], *, user: Optional[str] = None) -> None:
    output = plugin._get_configured_video_output()
    if output:
        payload["output"] = output

    user_value = str(user or "").strip()
    if user_value:
        payload["user"] = user_value


async def _poll_video_generation(
    plugin: Any,
    request_id: str,
    *,
    expected_duration: Optional[int] = None,
    scene: str = "生视频",
) -> Tuple[Optional[str], Optional[str]]:
    api_url = plugin._build_api_url(f"/v1/videos/{request_id}", API_SCOPE)
    started_at = time.monotonic()
    last_status = ""

    while time.monotonic() - started_at < plugin.VIDEO_TIMEOUT:
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_headers(API_SCOPE)
            plugin._debug_log_request(
                "生视频轮询[xAI]",
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
                    plugin._log_error_response("生视频轮询[xAI]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    if plugin._is_retryable_status(resp.status):
                        await asyncio.sleep(plugin.VIDEO_POLL_INTERVAL_SECONDS)
                        continue
                    return None, plugin._translate_error(detail or f"状态码: {resp.status}")

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"[生视频][xAI] 轮询响应 JSON 解析失败: {text}")
                    return None, "API响应格式异常"

                status = str(data.get("status", "")).strip().lower()
                if status and status != last_status:
                    logger.info(f"[生视频][xAI] 任务 {request_id} 状态: {status}")
                    last_status = status

                if status in {"done", "succeeded", "completed"}:
                    video_url = _extract_video_result_url(plugin, data)
                    if video_url:
                        _log_video_result_metadata(
                            scene,
                            request_id,
                            data,
                            expected_duration=expected_duration,
                        )
                        return video_url, None
                    return None, "任务完成但响应中未包含视频 URL"

                if not status:
                    video_url = _extract_video_result_url(plugin, data)
                    if video_url:
                        _log_video_result_metadata(
                            scene,
                            request_id,
                            data,
                            expected_duration=expected_duration,
                        )
                        return video_url, None

                if status in {"failed", "expired", "cancelled", "canceled"}:
                    detail = _extract_video_error(data)
                    return None, plugin._translate_error(detail or f"视频任务状态: {status}")

                await asyncio.sleep(plugin.VIDEO_POLL_INTERVAL_SECONDS)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            await asyncio.sleep(plugin.VIDEO_POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.error(f"[生视频][xAI] 轮询异常: {e}")
            return None, plugin._translate_error(str(e))

    return None, "视频生成超时，请稍后重试"


async def _start_video_task(
    plugin: Any,
    *,
    endpoint_path: str,
    payload: Dict[str, Any],
    scene: str,
) -> Tuple[Optional[str], Optional[str]]:
    api_url = plugin._build_api_url(endpoint_path, API_SCOPE)
    last_error: Optional[str] = None
    request_payload = payload
    alias_compat_tried = False

    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        attempt_started_at = time.monotonic()
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_headers(API_SCOPE)
            plugin._debug_log_request(
                f"{scene}[xAI]",
                "POST",
                api_url,
                headers=headers,
                json_body=request_payload,
                attempt=attempt + 1,
            )
            async with session.post(
                api_url,
                headers=headers,
                json=request_payload,
                timeout=aiohttp.ClientTimeout(total=plugin.VIDEO_TIMEOUT),
            ) as resp:
                text = await resp.text()
                if resp.status not in (200, 201, 202):
                    plugin._log_error_response(f"{scene}[xAI]", resp.status, text)
                    if not alias_compat_tried:
                        compat_payload = _build_alias_compat_payload(request_payload, text)
                        if compat_payload:
                            changed_fields = [
                                key for key in ("image", "reference_images")
                                if request_payload.get(key) != compat_payload.get(key)
                            ]
                            alias_compat_tried = True
                            request_payload = compat_payload
                            logger.warning(
                                f"[{scene}][xAI] 后端要求 {', '.join(changed_fields)} "
                                "使用字符串格式，已切换兼容 payload 重试"
                            )
                            continue

                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    last_error = translated_error

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
                    logger.error(f"[{scene}][xAI] 启动响应 JSON 解析失败: {text}")
                    return None, "API响应格式异常"

                direct_url = _extract_video_result_url(plugin, data)
                if direct_url:
                    return direct_url, None

                request_id = str(data.get("request_id") or data.get("id") or "").strip()
                if not request_id:
                    logger.error(f"[{scene}][xAI] 启动响应缺少 request_id: {text}")
                    return None, "API响应中未包含 request_id"

                expected_duration = payload.get("duration")
                if not isinstance(expected_duration, int):
                    expected_duration = None
                return await _poll_video_generation(
                    plugin,
                    request_id,
                    expected_duration=expected_duration,
                    scene=scene,
                )

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            elapsed = time.monotonic() - attempt_started_at
            logger.warning(
                f"[{scene}][xAI] 启动请求第 {attempt + 1}/"
                f"{plugin.MAX_REQUEST_RETRIES} 次失败，耗时 {elapsed:.1f}s: "
                f"{e.__class__.__name__}: {e}"
            )
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            last_error = (
                "视频任务启动请求超时或连接中断，未拿到 request_id；"
                "请检查后端或中间代理是否在创建任务阶段提前断开连接"
            )
        except Exception as e:
            elapsed = time.monotonic() - attempt_started_at
            logger.warning(
                f"[{scene}][xAI] 启动请求第 {attempt + 1}/"
                f"{plugin.MAX_REQUEST_RETRIES} 次异常，耗时 {elapsed:.1f}s: "
                f"{e.__class__.__name__}: {e}"
            )
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[{scene}][xAI] 请求异常: {e}")
            last_error = plugin._translate_error(str(e))

    return None, last_error or "视频请求失败"


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
    configured_model = plugin.conf.get("grok_video_model", plugin.DEFAULT_VIDEO_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=[plugin.DEFAULT_VIDEO_MODEL, "grok-imagine-1.0-video"],
        scene="生视频",
        scope=API_SCOPE,
    )

    if video_length is None:
        video_length = plugin._get_configured_video_duration()
    if not (plugin.MIN_VIDEO_LENGTH_SECONDS <= video_length <= plugin.MAX_VIDEO_LENGTH_SECONDS):
        return None, f"视频时长仅支持 {plugin.MIN_VIDEO_LENGTH_SECONDS}-{plugin.MAX_VIDEO_LENGTH_SECONDS} 秒"

    video_resolution = plugin._normalize_video_resolution(resolution) or plugin._get_configured_video_resolution()
    target_aspect_ratio = plugin._normalize_video_aspect_ratio(target_size)
    configured_aspect_ratio = plugin._get_configured_video_aspect_ratio()

    reference_mode_images = [item for item in (reference_images or []) if item]
    if reference_mode_images:
        mode = "reference-to-video"
    elif image_input:
        mode = "image-to-video"
    else:
        mode = "text-to-video"

    if mode == "text-to-video" and not prompt.strip():
        return None, "文生视频需要提示词"
    if mode == "reference-to-video" and not prompt.strip():
        return None, "参考图生视频需要提示词"
    if mode == "reference-to-video":
        if len(reference_mode_images) > plugin.MAX_VIDEO_REFERENCE_IMAGES:
            return None, f"参考图视频最多支持 {plugin.MAX_VIDEO_REFERENCE_IMAGES} 张图片"
        if video_length > plugin.MAX_REFERENCE_VIDEO_LENGTH_SECONDS:
            return None, (
                f"参考图视频时长仅支持 {plugin.MIN_VIDEO_LENGTH_SECONDS}-"
                f"{plugin.MAX_REFERENCE_VIDEO_LENGTH_SECONDS} 秒"
            )

    enhanced_prompt = plugin._build_video_prompt(prompt, mode)
    payload: Dict[str, Any] = {
        "model": model,
        "duration": video_length,
        "resolution": video_resolution,
    }
    if enhanced_prompt or mode != "image-to-video":
        payload["prompt"] = enhanced_prompt

    _add_optional_fields(plugin, payload, user=user)

    if mode == "image-to-video":
        payload["image"] = image_input
        if aspect_ratio_explicit:
            payload["aspect_ratio"] = target_aspect_ratio
    elif mode == "reference-to-video":
        payload["reference_images"] = reference_mode_images
        if aspect_ratio_explicit:
            payload["aspect_ratio"] = target_aspect_ratio
        elif configured_aspect_ratio is not None:
            payload["aspect_ratio"] = configured_aspect_ratio
        else:
            payload["aspect_ratio"] = None
    else:
        if aspect_ratio_explicit:
            payload["aspect_ratio"] = target_aspect_ratio
        elif configured_aspect_ratio is not None:
            payload["aspect_ratio"] = configured_aspect_ratio
        else:
            payload["aspect_ratio"] = None

    if "aspect_ratio" in payload:
        aspect_ratio_log = "null" if payload["aspect_ratio"] is None else payload["aspect_ratio"]
    else:
        aspect_ratio_log = "原图比例" if mode == "image-to-video" else "不指定"
    logger.info(
        f"[生视频][xAI] 请求参数: model={model}, mode={mode}, duration={video_length}, "
        f"aspect_ratio={aspect_ratio_log}, resolution={video_resolution}, "
        f"references={len(reference_mode_images)}"
    )

    return await _start_video_task(
        plugin,
        endpoint_path=VIDEO_GENERATION_PATH,
        payload=payload,
        scene="生视频",
    )


async def edit_video(
    plugin: Any,
    prompt: str,
    video_input: Dict[str, str],
    *,
    source_label: Optional[str] = None,
    user: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    configured_model = plugin.conf.get("grok_video_edit_model", plugin.DEFAULT_VIDEO_EDIT_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=[plugin.DEFAULT_VIDEO_EDIT_MODEL, plugin.DEFAULT_VIDEO_MODEL],
        scene="视频编辑",
        scope=API_SCOPE,
    )

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "video": video_input,
    }
    _add_optional_fields(plugin, payload, user=user)

    logger.info(
        f"[视频编辑][xAI] 请求参数: model={model}, "
        f"source={source_label or video_input.get('url', '')[:80]}"
    )
    return await _start_video_task(
        plugin,
        endpoint_path=VIDEO_EDIT_PATH,
        payload=payload,
        scene="视频编辑",
    )


async def extend_video(
    plugin: Any,
    prompt: str,
    video_input: Dict[str, str],
    *,
    duration: Optional[int] = None,
    source_label: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    configured_model = plugin.conf.get(
        "grok_video_extension_model",
        plugin.DEFAULT_VIDEO_EXTENSION_MODEL,
    )
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=[plugin.DEFAULT_VIDEO_EXTENSION_MODEL, plugin.DEFAULT_VIDEO_MODEL],
        scene="视频扩展",
        scope=API_SCOPE,
    )

    if duration is None:
        duration = plugin._get_configured_video_extension_duration()
    if not (plugin.MIN_VIDEO_EXTENSION_SECONDS <= duration <= plugin.MAX_VIDEO_EXTENSION_SECONDS):
        return None, (
            f"视频扩展时长仅支持 {plugin.MIN_VIDEO_EXTENSION_SECONDS}-"
            f"{plugin.MAX_VIDEO_EXTENSION_SECONDS} 秒"
        )

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "video": video_input,
        "duration": duration,
    }
    _add_optional_fields(plugin, payload)

    logger.info(
        f"[视频扩展][xAI] 请求参数: model={model}, duration={duration}, "
        f"source={source_label or video_input.get('url', '')[:80]}"
    )
    return await _start_video_task(
        plugin,
        endpoint_path=VIDEO_EXTENSION_PATH,
        payload=payload,
        scene="视频扩展",
    )
