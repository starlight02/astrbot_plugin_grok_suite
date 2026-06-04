import asyncio
import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from astrbot.api import logger

ImageResult = Tuple[Optional[str], Optional[bytes]]

IMAGE_GENERATION_PATH = "/v1/images/generations"
IMAGE_EDIT_PATH = "/v1/images/edits"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
IMAGE_MODEL = "grok-imagine-image"
IMAGE_MODEL_PRO = "grok-imagine-image-pro"
IMAGE_MODEL_LITE = "grok-imagine-image-lite"
IMAGE_EDIT_MODEL = "grok-imagine-image-edit"
IMAGE_MODEL_FALLBACKS = [IMAGE_MODEL, IMAGE_MODEL_PRO, IMAGE_MODEL_LITE]
EDIT_IMAGE_MODEL_FALLBACKS = [IMAGE_EDIT_MODEL]
EDIT_SIZE = "1024x1024"
API_SCOPE = "image"
CHAT_ENTRYPOINT_MODES = {"chat", "chat_first", "chat-completions", "chat_completions"}


def _configured_model(plugin: Any, config_key: str, default_model: str) -> str:
    configured = str(plugin.conf.get(config_key, "") or "").strip()
    xai_defaults = {
        getattr(plugin, "DEFAULT_IMAGE_MODEL", ""),
        getattr(plugin, "DEFAULT_LEGACY_IMAGE_MODEL", ""),
        getattr(plugin, "DEFAULT_LEGACY_EDIT_MODEL", ""),
    }
    if not configured or configured in xai_defaults:
        return default_model
    return configured


def _configured_generation_model(plugin: Any) -> str:
    configured = str(plugin.conf.get("grok_image_model", "") or "").strip()
    xai_defaults = {
        getattr(plugin, "DEFAULT_IMAGE_MODEL", ""),
        getattr(plugin, "DEFAULT_LEGACY_IMAGE_MODEL", ""),
        getattr(plugin, "DEFAULT_LEGACY_EDIT_MODEL", ""),
    }
    if not configured or configured in xai_defaults:
        if plugin._get_configured_image_resolution() == "2k":
            return IMAGE_MODEL_PRO
        return IMAGE_MODEL
    return configured


def _get_image_entrypoint(plugin: Any) -> str:
    mode = str(plugin.conf.get("grok2api_image_entrypoint", "dedicated_first") or "").strip().lower()
    if mode in CHAT_ENTRYPOINT_MODES:
        return "chat"
    if mode in {"", "dedicated", "dedicated_first", "images", "images_first", "auto"}:
        return "dedicated_first"
    logger.warning(f"[grok2api] 图片入口配置无效: {mode}, 已回退为 dedicated_first")
    return "dedicated_first"


def _is_parameter_error(status: int, detail: str, response_text: str = "") -> bool:
    if status not in (400, 422):
        return False
    text = f"{detail}\n{response_text}".lower()
    if any(token in text for token in ("api key", "unauthorized", "forbidden", "quota", "rate limit")):
        return False
    return any(
        token in text
        for token in (
            "invalid_request_error",
            "invalid_request",
            "invalid_json",
            "invalid_value",
            "field required",
            "missing",
            "required",
            "model name not specified",
            "model name cannot be empty",
            "model cannot be empty",
            "cannot unmarshal",
            "unmarshal",
            "validation",
            "param=",
            "must be",
            "unsupported",
            "not supported",
            "image[]",
            "response_format",
            "size",
            "n must",
        )
    )


def _is_non_retryable_upstream_rejection(
    status: int,
    detail: str,
    response_text: str = "",
) -> bool:
    if status not in (400, 403, 422, 451, 500, 502):
        return False

    text = f"{detail}\n{response_text}".lower()
    if any(
        token in text
        for token in (
            "content_policy",
            "content policy",
            "moderation",
            "safety",
            "unsafe",
            "policy_violation",
            "policy violation",
            "blocked",
            "rejected",
            "not allowed",
            "bad_response_status_code",
            "openai_error",
        )
    ):
        return True
    return False


def _image_to_data_url(plugin: Any, image_bytes: bytes) -> str:
    mime_type = plugin._detect_mime_type(image_bytes)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _normalize_media_url(plugin: Any, url: str) -> str:
    value = str(url or "").strip()
    if value.startswith(("http://", "https://", "data:")):
        return value
    if value.startswith("/"):
        return f"{plugin._get_base_url(API_SCOPE)}{value}"
    return value


def _extract_chat_media_url(plugin: Any, text: str) -> Optional[str]:
    patterns = (
        r'!\[[^\]]*\]\(([^)]+)\)',
        r'<(?:img|video|source)[^>]*src=["\']([^"\']+)["\']',
        r'((?:/v1)?/files/image\?id=[^\s<>"\')\]]+)',
        r'(data:image/[^\s<>"\')\]]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        url = match.group(1).strip()
        if url:
            return _normalize_media_url(plugin, url)
    return None


def _parse_image_results(plugin: Any, data: Dict[str, Any]) -> List[ImageResult]:
    results = plugin._parse_image_api_response(data)
    if results:
        normalized_results: List[ImageResult] = []
        for url, image_bytes in results:
            if image_bytes:
                normalized_results.append((url, image_bytes))
                continue
            if not url:
                normalized_results.append((url, image_bytes))
                continue
            normalized_url = _normalize_media_url(plugin, url)
            if normalized_url.startswith("data:"):
                b64_data = plugin._extract_base64_from_data_uri(normalized_url)
                if b64_data:
                    try:
                        normalized_results.append((None, base64.b64decode(b64_data)))
                    except Exception as e:
                        logger.warning(f"[grok2api] 图片 data URI 解码失败: {e}")
                continue
            normalized_results.append((normalized_url, None))
        return normalized_results

    _, _, text = plugin._parse_json_response(data)
    if not text:
        return []

    url = plugin._extract_url_from_text(text) or _extract_chat_media_url(plugin, text)
    if url:
        if url.startswith("data:"):
            b64_data = plugin._extract_base64_from_data_uri(url)
            if b64_data:
                try:
                    return [(None, base64.b64decode(b64_data))]
                except Exception as e:
                    logger.warning(f"[grok2api] Chat 回退图片 data URI 解码失败: {e}")
            return []
        return [(url, None)]

    b64 = plugin._extract_base64_from_text(text)
    if b64:
        try:
            return [(None, base64.b64decode(b64))]
        except Exception as e:
            logger.warning(f"[grok2api] Chat 回退图片 Base64 解码失败: {e}")
    return []


def _resolve_edit_size(plugin: Any, image_bytes: bytes, target_size: Optional[str]) -> str:
    if target_size:
        return target_size
    source_resolution = plugin._get_image_resolution(image_bytes)
    if source_resolution:
        mapped_size = plugin._get_closest_supported_size(*source_resolution)
        if mapped_size:
            return mapped_size
    return EDIT_SIZE


def _build_chat_image_payload(
    *,
    model: str,
    prompt: str,
    n: int,
    size: str,
    response_format: Optional[str],
) -> Dict[str, Any]:
    image_config: Dict[str, Any] = {
        "n": max(1, min(n, 10)),
        "size": size,
    }
    if response_format:
        image_config["response_format"] = response_format
    return {
        "model": model,
        "stream": False,
        "reasoning_effort": "none",
        "messages": [{"role": "user", "content": prompt}],
        "image_config": image_config,
    }


def _build_chat_image_edit_payload(
    plugin: Any,
    *,
    model: str,
    prompt: str,
    image_items: List[bytes],
    n: int,
    size: str,
    response_format: Optional[str],
) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in image_items:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _image_to_data_url(plugin, item)},
            }
        )
    image_config: Dict[str, Any] = {
        "n": max(1, min(n, 2)),
        "size": size,
    }
    if response_format:
        image_config["response_format"] = response_format
    return {
        "model": model,
        "stream": False,
        "reasoning_effort": "none",
        "messages": [{"role": "user", "content": content}],
        "image_config": image_config,
    }


async def _post_json_with_retries(
    plugin: Any,
    *,
    api_url: str,
    payload: Dict[str, Any],
    scene: str,
    response_format: Optional[str],
) -> Tuple[List[ImageResult], Optional[str], bool, bool]:
    image_timeout = plugin._get_configured_image_timeout_seconds()
    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_headers(API_SCOPE)
            plugin._debug_log_request(
                f"{scene}[grok2api]",
                "POST",
                api_url,
                headers=headers,
                json_body=payload,
                attempt=attempt + 1,
            )
            async with session.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=image_timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    plugin._log_error_response(f"{scene}[grok2api]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    parameter_error = _is_parameter_error(resp.status, detail, text)
                    if response_format and plugin._is_response_format_related_error(detail):
                        logger.warning(
                            f"[{scene}][grok2api] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                        )
                        return [], translated_error, True, False
                    if _is_non_retryable_upstream_rejection(resp.status, detail, text):
                        logger.warning(
                            f"[{scene}][grok2api] 上游拒绝或审核拦截，停止重试: {detail[:120]}"
                        )
                        return [], translated_error, False, False
                    if (
                        plugin._is_retryable_status(resp.status)
                        and attempt < plugin.MAX_REQUEST_RETRIES - 1
                    ):
                        await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                        continue
                    return [], translated_error, False, parameter_error

                raw_content = await resp.read()
                try:
                    data = json.loads(raw_content.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response_text = raw_content.decode("utf-8", errors="replace")
                    logger.error(f"[{scene}][grok2api] JSON解析失败，完整响应: {response_text}")
                    return [], "API响应格式异常", False, False

                results = _parse_image_results(plugin, data)
                if results:
                    return results, None, False, False
                return [], "未能从响应中提取图片", False, False

        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return [], "请求超时，请重试", False, False
        except Exception as e:
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[{scene}][grok2api] 请求异常: {e}")
            return [], plugin._translate_error(str(e)), False, False

    return [], f"{scene}请求失败", False, False


async def _post_form_with_retries(
    plugin: Any,
    *,
    api_url: str,
    build_form: Any,
    form_debug: List[Dict[str, Any]],
    scene: str,
    response_format: Optional[str],
) -> Tuple[List[ImageResult], Optional[str], bool, bool]:
    image_timeout = plugin._get_configured_image_timeout_seconds()
    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_auth_headers(API_SCOPE)
            plugin._debug_log_request(
                f"{scene}[grok2api]",
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
                timeout=aiohttp.ClientTimeout(total=image_timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    plugin._log_error_response(f"{scene}[grok2api]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    parameter_error = _is_parameter_error(resp.status, detail, text)
                    if response_format and plugin._is_response_format_related_error(detail):
                        logger.warning(
                            f"[{scene}][grok2api] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                        )
                        return [], translated_error, True, False
                    if _is_non_retryable_upstream_rejection(resp.status, detail, text):
                        logger.warning(
                            f"[{scene}][grok2api] 上游拒绝或审核拦截，停止重试: {detail[:120]}"
                        )
                        return [], translated_error, False, False
                    if (
                        plugin._is_retryable_status(resp.status)
                        and attempt < plugin.MAX_REQUEST_RETRIES - 1
                    ):
                        await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                        continue
                    return [], translated_error, False, parameter_error

                raw_content = await resp.read()
                try:
                    data = json.loads(raw_content.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response_text = raw_content.decode("utf-8", errors="replace")
                    logger.error(f"[{scene}][grok2api] JSON解析失败，完整响应: {response_text}")
                    return [], "API响应格式异常", False, False

                results = _parse_image_results(plugin, data)
                if results:
                    return results, None, False, False
                return [], "未能从响应中提取图片", False, False

        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return [], "请求超时，请重试", False, False
        except Exception as e:
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[{scene}][grok2api] 请求异常: {e}")
            return [], plugin._translate_error(str(e)), False, False

    return [], f"{scene}请求失败", False, False


async def _post_chat_fallback(
    plugin: Any,
    *,
    payload: Dict[str, Any],
    scene: str,
    response_format: Optional[str],
    fallback: bool = True,
) -> Tuple[List[ImageResult], Optional[str], bool]:
    api_url = plugin._build_api_url(CHAT_COMPLETIONS_PATH, API_SCOPE)
    image_timeout = plugin._get_configured_image_timeout_seconds()
    request_scene = f"{scene}Chat回退[grok2api]" if fallback else f"{scene}Chat[grok2api]"
    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_headers(API_SCOPE)
            plugin._debug_log_request(
                request_scene,
                "POST",
                api_url,
                headers=headers,
                json_body=payload,
                attempt=attempt + 1,
            )
            async with session.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=image_timeout),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    plugin._log_error_response(request_scene, resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(detail or f"状态码: {resp.status}")
                    if response_format and plugin._is_response_format_related_error(detail):
                        logger.warning(
                            f"[{scene}][grok2api] Chat 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                        )
                        return [], translated_error, True
                    if _is_non_retryable_upstream_rejection(resp.status, detail, text):
                        logger.warning(
                            f"[{scene}][grok2api] Chat 上游拒绝或审核拦截，停止重试: {detail[:120]}"
                        )
                        return [], translated_error, False
                    if (
                        plugin._is_retryable_status(resp.status)
                        and attempt < plugin.MAX_REQUEST_RETRIES - 1
                    ):
                        await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                        continue
                    return [], translated_error, False

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"[{scene}][grok2api] Chat 回退响应 JSON 解析失败: {text}")
                    return [], "API响应格式异常", False

                results = _parse_image_results(plugin, data)
                if results:
                    return results, None, False
                return [], "未能从 Chat 回退响应中提取图片", False
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return [], "请求超时，请重试", False
        except Exception as e:
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[{request_scene}] 请求异常: {e}")
            return [], plugin._translate_error(str(e)), False

    return [], f"{scene}Chat请求失败", False


async def generate_image(
    plugin: Any,
    prompt: str,
    *,
    n: int = 1,
    target_size: Optional[str] = None,
) -> Tuple[List[ImageResult], Optional[str]]:
    api_url = plugin._build_api_url(IMAGE_GENERATION_PATH, API_SCOPE)
    configured_model = _configured_generation_model(plugin)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=IMAGE_MODEL_FALLBACKS,
        scene="文生图",
        scope=API_SCOPE,
    )
    image_size = target_size or plugin.DEFAULT_TEXT_IMAGE_SIZE
    entrypoint = _get_image_entrypoint(plugin)
    last_error: Optional[str] = None

    for response_format in plugin._get_image_response_format_candidates():
        if entrypoint == "chat":
            results, error, switch_format = await _post_chat_fallback(
                plugin,
                payload=_build_chat_image_payload(
                    model=model,
                    prompt=prompt,
                    n=n,
                    size=image_size,
                    response_format=response_format,
                ),
                scene="文生图",
                response_format=response_format,
                fallback=False,
            )
            if results:
                return results, None
            if switch_format:
                last_error = error
                continue
            return results, error

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": max(1, min(n, plugin.MAX_IMAGE_COUNT)),
            "size": image_size,
        }
        if response_format:
            payload["response_format"] = response_format

        logger.info(f"[文生图][grok2api] 完整请求参数: {payload}")
        results, error, switch_format, parameter_error = await _post_json_with_retries(
            plugin,
            api_url=api_url,
            payload=payload,
            scene="文生图",
            response_format=response_format,
        )
        if results:
            return results, None
        if parameter_error:
            logger.warning("[文生图][grok2api] 专用接口参数错误，切换 /v1/chat/completions 回退")
            fallback_results, fallback_error, fallback_switch_format = await _post_chat_fallback(
                plugin,
                payload=_build_chat_image_payload(
                    model=model,
                    prompt=prompt,
                    n=n,
                    size=image_size,
                    response_format=response_format,
                ),
                scene="文生图",
                response_format=response_format,
            )
            if fallback_results:
                return fallback_results, None
            if fallback_switch_format:
                last_error = fallback_error
                continue
            return fallback_results, fallback_error
        if not switch_format:
            return results, error
        last_error = error

    return [], last_error or "文生图请求失败"


async def edit_image(
    plugin: Any,
    prompt: str,
    image_bytes: bytes,
    *,
    n: int = 1,
    target_size: Optional[str] = None,
    reference_images: Optional[List[bytes]] = None,
) -> Tuple[List[ImageResult], Optional[str]]:
    api_url = plugin._build_api_url(IMAGE_EDIT_PATH, API_SCOPE)
    configured_model = _configured_model(plugin, "grok_edit_model", IMAGE_EDIT_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=EDIT_IMAGE_MODEL_FALLBACKS,
        scene="图生图",
        scope=API_SCOPE,
    )

    all_image_bytes = [image_bytes]
    for ref_image in reference_images or []:
        if ref_image:
            all_image_bytes.append(ref_image)
        if len(all_image_bytes) >= plugin.MAX_EDIT_REFERENCE_IMAGES:
            break

    for item in all_image_bytes:
        mime_type = plugin._detect_mime_type(item)
        if not plugin._is_supported_edit_image_mime(mime_type):
            return [], "grok2api 图像编辑参考图仅支持 JPEG、PNG、WebP 格式"

    edit_size = _resolve_edit_size(plugin, image_bytes, target_size)
    entrypoint = _get_image_entrypoint(plugin)

    last_error: Optional[str] = None
    for response_format in plugin._get_image_response_format_candidates():
        if entrypoint == "chat":
            results, error, switch_format = await _post_chat_fallback(
                plugin,
                payload=_build_chat_image_edit_payload(
                    plugin,
                    model=model,
                    prompt=prompt,
                    image_items=all_image_bytes,
                    n=n,
                    size=edit_size,
                    response_format=response_format,
                ),
                scene="图生图",
                response_format=response_format,
                fallback=False,
            )
            if results:
                return results, None
            if switch_format:
                last_error = error
                continue
            return results, error

        def build_form() -> aiohttp.FormData:
            form = aiohttp.FormData()
            form.add_field("model", model)
            form.add_field("prompt", prompt)
            form.add_field("n", str(max(1, min(n, 2))))
            form.add_field("size", edit_size)
            if response_format:
                form.add_field("response_format", response_format)
            for index, item in enumerate(all_image_bytes, start=1):
                mime_type = plugin._detect_mime_type(item)
                ext = "jpg" if mime_type == "image/jpeg" else mime_type.rsplit("/", 1)[-1]
                form.add_field(
                    "image[]",
                    item,
                    filename=f"image_{index}.{ext}",
                    content_type=mime_type,
                )
            return form

        form_debug: List[Dict[str, Any]] = [
            {"name": "model", "value": model},
            {"name": "prompt", "value": prompt},
            {"name": "n", "value": str(max(1, min(n, 2)))},
            {"name": "size", "value": edit_size},
        ]
        if response_format:
            form_debug.append({"name": "response_format", "value": response_format})
        for index, item in enumerate(all_image_bytes, start=1):
            mime_type = plugin._detect_mime_type(item)
            ext = "jpg" if mime_type == "image/jpeg" else mime_type.rsplit("/", 1)[-1]
            form_debug.append(
                plugin._build_form_file_debug_field(
                    "image[]",
                    item,
                    filename=f"image_{index}.{ext}",
                    content_type=mime_type,
                )
            )

        logger.info(
            f"[图生图][grok2api] 请求参数: model={model}, references={len(all_image_bytes)}, "
            f"size={edit_size}, image_field=image[]"
        )
        results, error, switch_format, parameter_error = await _post_form_with_retries(
            plugin,
            api_url=api_url,
            build_form=build_form,
            form_debug=form_debug,
            scene="图生图",
            response_format=response_format,
        )
        if results:
            return results, None
        if parameter_error:
            logger.warning("[图生图][grok2api] 专用接口参数错误，切换 /v1/chat/completions 回退")
            fallback_results, fallback_error, fallback_switch_format = await _post_chat_fallback(
                plugin,
                payload=_build_chat_image_edit_payload(
                    plugin,
                    model=model,
                    prompt=prompt,
                    image_items=all_image_bytes,
                    n=n,
                    size=edit_size,
                    response_format=response_format,
                ),
                scene="图生图",
                response_format=response_format,
            )
            if fallback_results:
                return fallback_results, None
            if fallback_switch_format:
                last_error = fallback_error
                continue
            return fallback_results, fallback_error
        if not switch_format:
            return results, error
        last_error = error

    return [], last_error or "图生图请求失败"
