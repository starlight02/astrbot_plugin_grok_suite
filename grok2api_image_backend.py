import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from astrbot.api import logger


ImageResult = Tuple[Optional[str], Optional[bytes]]

IMAGE_GENERATION_PATH = "/v1/images/generations"
IMAGE_EDIT_PATH = "/v1/images/edits"
IMAGE_MODEL = "grok-imagine-image"
IMAGE_EDIT_MODEL = "grok-imagine-image-edit"
EDIT_SIZE = "1024x1024"
API_SCOPE = "image"


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


async def _post_json_with_retries(
    plugin: Any,
    *,
    api_url: str,
    payload: Dict[str, Any],
    scene: str,
    response_format: Optional[str],
) -> Tuple[List[ImageResult], Optional[str], bool]:
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
                timeout=aiohttp.ClientTimeout(total=plugin.IMAGE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    plugin._log_error_response(f"{scene}[grok2api]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    if response_format and plugin._is_response_format_related_error(detail):
                        logger.warning(
                            f"[{scene}][grok2api] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                        )
                        return [], translated_error, True
                    if (
                        plugin._is_retryable_status(resp.status)
                        and attempt < plugin.MAX_REQUEST_RETRIES - 1
                    ):
                        await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                        continue
                    return [], translated_error, False

                raw_content = await resp.read()
                try:
                    data = json.loads(raw_content.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response_text = raw_content.decode("utf-8", errors="replace")
                    logger.error(f"[{scene}][grok2api] JSON解析失败，完整响应: {response_text}")
                    return [], "API响应格式异常", False

                results = plugin._parse_image_api_response(data)
                if results:
                    return results, None, False
                return [], "未能从响应中提取图片", False

        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return [], "请求超时，请重试", False
        except Exception as e:
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[{scene}][grok2api] 请求异常: {e}")
            return [], plugin._translate_error(str(e)), False

    return [], f"{scene}请求失败", False


async def _post_form_with_retries(
    plugin: Any,
    *,
    api_url: str,
    build_form: Any,
    form_debug: List[Dict[str, Any]],
    scene: str,
    response_format: Optional[str],
) -> Tuple[List[ImageResult], Optional[str], bool]:
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
                timeout=aiohttp.ClientTimeout(total=plugin.IMAGE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    plugin._log_error_response(f"{scene}[grok2api]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    if response_format and plugin._is_response_format_related_error(detail):
                        logger.warning(
                            f"[{scene}][grok2api] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                        )
                        return [], translated_error, True
                    if (
                        plugin._is_retryable_status(resp.status)
                        and attempt < plugin.MAX_REQUEST_RETRIES - 1
                    ):
                        await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                        continue
                    return [], translated_error, False

                raw_content = await resp.read()
                try:
                    data = json.loads(raw_content.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response_text = raw_content.decode("utf-8", errors="replace")
                    logger.error(f"[{scene}][grok2api] JSON解析失败，完整响应: {response_text}")
                    return [], "API响应格式异常", False

                results = plugin._parse_image_api_response(data)
                if results:
                    return results, None, False
                return [], "未能从响应中提取图片", False

        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            return [], "请求超时，请重试", False
        except Exception as e:
            if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                continue
            logger.error(f"[{scene}][grok2api] 请求异常: {e}")
            return [], plugin._translate_error(str(e)), False

    return [], f"{scene}请求失败", False


async def generate_image(
    plugin: Any,
    prompt: str,
    *,
    n: int = 1,
    target_size: Optional[str] = None,
) -> Tuple[List[ImageResult], Optional[str]]:
    api_url = plugin._build_api_url(IMAGE_GENERATION_PATH, API_SCOPE)
    configured_model = _configured_model(plugin, "grok_image_model", IMAGE_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=[IMAGE_MODEL, "grok-imagine-image-pro", "grok-imagine-image-lite"],
        scene="文生图",
        scope=API_SCOPE,
    )
    image_size = target_size or plugin.DEFAULT_TEXT_IMAGE_SIZE
    last_error: Optional[str] = None

    for response_format in plugin._get_image_response_format_candidates():
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": max(1, min(n, plugin.MAX_IMAGE_COUNT)),
            "size": image_size,
        }
        if response_format:
            payload["response_format"] = response_format

        logger.info(f"[文生图][grok2api] 完整请求参数: {payload}")
        results, error, switch_format = await _post_json_with_retries(
            plugin,
            api_url=api_url,
            payload=payload,
            scene="文生图",
            response_format=response_format,
        )
        if results or not switch_format:
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
        fallback_models=[IMAGE_EDIT_MODEL, IMAGE_MODEL],
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

    if target_size and target_size != EDIT_SIZE:
        logger.warning(
            f"[图生图][grok2api] 当前后端编辑接口仅支持 {EDIT_SIZE}，"
            f"已忽略请求尺寸: {target_size}"
        )

    last_error: Optional[str] = None
    for response_format in plugin._get_image_response_format_candidates():
        def build_form() -> aiohttp.FormData:
            form = aiohttp.FormData()
            form.add_field("model", model)
            form.add_field("prompt", prompt)
            form.add_field("n", str(max(1, min(n, 2))))
            form.add_field("size", EDIT_SIZE)
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
            {"name": "size", "value": EDIT_SIZE},
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
            f"size={EDIT_SIZE}"
        )
        results, error, switch_format = await _post_form_with_retries(
            plugin,
            api_url=api_url,
            build_form=build_form,
            form_debug=form_debug,
            scene="图生图",
            response_format=response_format,
        )
        if results or not switch_format:
            return results, error
        last_error = error

    return [], last_error or "图生图请求失败"
