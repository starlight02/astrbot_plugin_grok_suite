import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from astrbot.api import logger


ImageResult = Tuple[Optional[str], Optional[bytes]]

IMAGE_GENERATION_PATH = "/v1/images/generations"
IMAGE_EDIT_PATH = "/v1/images/edits"
API_SCOPE = "image"


async def _post_json_with_retries(
    plugin: Any,
    *,
    api_url: str,
    payload: Dict[str, Any],
    scene: str,
    response_format: Optional[str],
) -> Tuple[List[ImageResult], Optional[str], bool]:
    image_timeout = plugin._get_configured_image_timeout_seconds()
    for attempt in range(plugin.MAX_REQUEST_RETRIES):
        try:
            session = await plugin._ensure_session()
            headers = plugin._get_headers(API_SCOPE)
            plugin._debug_log_request(
                f"{scene}[xAI]",
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
                    plugin._log_error_response(f"{scene}[xAI]", resp.status, text)
                    detail = plugin._extract_api_error_message(text)
                    translated_error = plugin._translate_error(
                        detail or f"状态码: {resp.status}"
                    )
                    if response_format and plugin._is_response_format_related_error(detail):
                        logger.warning(
                            f"[{scene}][xAI] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
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
                    logger.error(f"[{scene}][xAI] JSON解析失败，完整响应: {response_text}")
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
            logger.error(f"[{scene}][xAI] 请求异常: {e}")
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
    configured_model = plugin.conf.get("grok_image_model", plugin.DEFAULT_IMAGE_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=plugin.IMAGE_MODEL_FALLBACKS,
        scene="文生图",
        scope=API_SCOPE,
    )

    aspect_ratio = plugin._normalize_image_aspect_ratio(target_size)
    image_resolution = plugin._get_configured_image_resolution()
    last_error: Optional[str] = None

    for response_format in plugin._get_image_response_format_candidates():
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": max(1, min(n, plugin.MAX_IMAGE_COUNT)),
            "aspect_ratio": aspect_ratio,
            "resolution": image_resolution,
        }
        if response_format:
            payload["response_format"] = response_format

        logger.info(f"[文生图][xAI] 完整请求参数: {payload}")
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
    configured_model = plugin.conf.get("grok_edit_model", plugin.DEFAULT_IMAGE_MODEL)
    model = await plugin._resolve_model(
        configured_model=configured_model,
        fallback_models=plugin.EDIT_IMAGE_MODEL_FALLBACKS,
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
            return [], "图生图参考图仅支持 JPEG、PNG、WebP 格式"

    image_inputs = [plugin._build_image_input_object(image_bytes)]
    for ref_image in all_image_bytes[1:]:
        image_inputs.append(plugin._build_image_input_object(ref_image))

    image_resolution = plugin._get_configured_image_resolution()
    last_error: Optional[str] = None
    for response_format in plugin._get_image_response_format_candidates():
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": max(1, min(n, plugin.MAX_IMAGE_COUNT)),
            "resolution": image_resolution,
        }
        if target_size:
            payload["aspect_ratio"] = plugin._normalize_image_aspect_ratio(target_size)
        if len(image_inputs) == 1:
            payload["image"] = image_inputs[0]
        else:
            payload["images"] = image_inputs
        if response_format:
            payload["response_format"] = response_format

        logger.info(
            f"[图生图][xAI] 请求参数: model={model}, references={len(image_inputs)}, "
            f"aspect_ratio={payload.get('aspect_ratio', 'source')}, "
            f"resolution={image_resolution}"
        )
        results, error, switch_format = await _post_json_with_retries(
            plugin,
            api_url=api_url,
            payload=payload,
            scene="图生图",
            response_format=response_format,
        )
        if results or not switch_format:
            return results, error
        last_error = error

    return [], last_error or "图生图请求失败"
