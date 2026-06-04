import asyncio
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger


ImageResult = Tuple[Optional[str], Optional[bytes]]
API_SCOPE = "image"
OPENAI_IMAGE_MODEL = "gpt-image-1"
OPENAI_EDIT_SIZE = "1024x1024"
OPENAI_IMAGE_SIZES = ("1024x1024", "1536x1024", "1024x1536", "1792x1024", "1024x1792")
OPENAI_EDIT_SIZES = ("1024x1024", "1536x1024", "1024x1536")

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


def _configured_image_model(plugin: Any, config_key: str, default_model: str) -> str:
    configured = str(plugin.conf.get(config_key, "") or "").strip()
    xai_defaults = {
        getattr(plugin, "DEFAULT_IMAGE_MODEL", ""),
        getattr(plugin, "DEFAULT_LEGACY_IMAGE_MODEL", ""),
        getattr(plugin, "DEFAULT_LEGACY_EDIT_MODEL", ""),
        "grok-imagine-image",
        "grok-imagine-image-quality",
        "grok-imagine-image-edit",
    }
    if not configured or configured in xai_defaults:
        return default_model
    return configured


def _parse_size(size: Optional[str]) -> Optional[Tuple[int, int]]:
    if not size or "x" not in size:
        return None
    width_text, height_text = size.lower().split("x", 1)
    if not width_text.isdigit() or not height_text.isdigit():
        return None
    width = int(width_text)
    height = int(height_text)
    return (width, height) if width > 0 and height > 0 else None


def _closest_openai_image_size(size: Optional[str], candidates: Sequence[str]) -> str:
    if size in candidates:
        return str(size)

    requested = _parse_size(size)
    if not requested:
        return OPENAI_EDIT_SIZE

    target_width, target_height = requested
    target_ratio = target_width / target_height
    target_area = target_width * target_height

    def distance(candidate: str) -> Tuple[float, float, float]:
        parsed = _parse_size(candidate)
        if not parsed:
            return (float("inf"), float("inf"), float("inf"))
        width, height = parsed
        ratio_distance = abs((width / height) - target_ratio)
        area_distance = abs((width * height) - target_area) / max(target_area, 1)
        dim_distance = (
            abs(width - target_width) / max(target_width, 1)
            + abs(height - target_height) / max(target_height, 1)
        )
        return ratio_distance, area_distance, dim_distance

    return min(candidates, key=distance)


def _file_tuple(plugin: Any, item: bytes, index: int, *, prefix: str = "image") -> Tuple[str, bytes, str]:
    mime_type = plugin._detect_mime_type(item)
    ext = "jpg" if mime_type == "image/jpeg" else mime_type.rsplit("/", 1)[-1]
    return f"{prefix}_{index}.{ext}", item, mime_type


async def generate_image(
    plugin: Any,
    prompt: str,
    *,
    n: int = 1,
    target_size: Optional[str] = None,
) -> Tuple[List[ImageResult], Optional[str]]:
    error = _ensure_available()
    if error:
        return [], error

    model = _configured_image_model(plugin, "grok_image_model", OPENAI_IMAGE_MODEL)
    image_size = _closest_openai_image_size(
        target_size or plugin.DEFAULT_TEXT_IMAGE_SIZE,
        OPENAI_IMAGE_SIZES,
    )
    image_timeout = plugin._get_configured_image_timeout_seconds()
    last_error: Optional[str] = None

    for response_format in plugin._get_image_response_format_candidates():
        params: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": max(1, min(n, plugin.MAX_IMAGE_COUNT)),
            "size": image_size,
        }
        if response_format:
            params["response_format"] = response_format

        for attempt in range(plugin.MAX_REQUEST_RETRIES):
            try:
                plugin._debug_log_request(
                    "文生图[OpenAI]",
                    "POST",
                    f"{_sdk_base_url(plugin)}/images/generations",
                    headers=plugin._get_auth_headers(API_SCOPE),
                    json_body=params,
                    attempt=attempt + 1,
                )
                async with AsyncOpenAI(
                    api_key=plugin._get_api_key(API_SCOPE),
                    base_url=_sdk_base_url(plugin),
                    timeout=image_timeout,
                    max_retries=0,
                ) as client:
                    response = await client.images.generate(**params)
                results = plugin._parse_image_api_response(_response_to_dict(response))
                if results:
                    return results, None
                return [], "未能从响应中提取图片"
            except _status_error_classes() as e:
                status = int(getattr(e, "status_code", 0) or 0)
                detail = _translate_status_error(plugin, "文生图", status, _status_error_text(e))
                last_error = detail
                if response_format and plugin._is_response_format_related_error(detail):
                    logger.warning(
                        f"[文生图][OpenAI] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                    )
                    break
                if plugin._is_retryable_status(status) and attempt < plugin.MAX_REQUEST_RETRIES - 1:
                    await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                    continue
                return [], detail
            except _transport_error_classes():
                if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                    await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                    continue
                return [], "请求超时，请重试"
            except Exception as e:
                logger.error(f"[文生图][OpenAI] 请求异常: {e}")
                return [], plugin._translate_error(str(e))

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
    error = _ensure_available()
    if error:
        return [], error

    model = _configured_image_model(plugin, "grok_edit_model", OPENAI_IMAGE_MODEL)
    all_image_bytes = [image_bytes]
    for ref_image in reference_images or []:
        if ref_image:
            all_image_bytes.append(ref_image)
        if len(all_image_bytes) >= plugin.MAX_EDIT_REFERENCE_IMAGES:
            break

    for item in all_image_bytes:
        mime_type = plugin._detect_mime_type(item)
        if not plugin._is_supported_edit_image_mime(mime_type):
            return [], "OpenAI 图像编辑参考图仅支持 JPEG、PNG、WebP 格式"

    image_files = [
        _file_tuple(plugin, item, index)
        for index, item in enumerate(all_image_bytes, start=1)
    ]
    image_param: Any = image_files[0] if len(image_files) == 1 else image_files
    image_size = _closest_openai_image_size(target_size, OPENAI_EDIT_SIZES)
    image_timeout = plugin._get_configured_image_timeout_seconds()
    last_error: Optional[str] = None

    for response_format in plugin._get_image_response_format_candidates():
        params: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "image": image_param,
            "n": max(1, min(n, 2)),
            "size": image_size,
        }
        if response_format:
            params["response_format"] = response_format

        form_debug: List[Dict[str, Any]] = [
            {"name": "model", "value": model},
            {"name": "prompt", "value": prompt},
            {"name": "n", "value": str(max(1, min(n, 2)))},
            {"name": "size", "value": image_size},
        ]
        if response_format:
            form_debug.append({"name": "response_format", "value": response_format})
        for index, item in enumerate(all_image_bytes, start=1):
            filename, _, mime_type = _file_tuple(plugin, item, index)
            form_debug.append(
                plugin._build_form_file_debug_field(
                    "image" if len(all_image_bytes) == 1 else "image[]",
                    item,
                    filename=filename,
                    content_type=mime_type,
                )
            )

        for attempt in range(plugin.MAX_REQUEST_RETRIES):
            try:
                plugin._debug_log_request(
                    "图生图[OpenAI]",
                    "POST",
                    f"{_sdk_base_url(plugin)}/images/edits",
                    headers=plugin._get_auth_headers(API_SCOPE),
                    form_body=form_debug,
                    attempt=attempt + 1,
                )
                async with AsyncOpenAI(
                    api_key=plugin._get_api_key(API_SCOPE),
                    base_url=_sdk_base_url(plugin),
                    timeout=image_timeout,
                    max_retries=0,
                ) as client:
                    response = await client.images.edit(**params)
                results = plugin._parse_image_api_response(_response_to_dict(response))
                if results:
                    return results, None
                return [], "未能从响应中提取图片"
            except _status_error_classes() as e:
                status = int(getattr(e, "status_code", 0) or 0)
                detail = _translate_status_error(plugin, "图生图", status, _status_error_text(e))
                last_error = detail
                if response_format and plugin._is_response_format_related_error(detail):
                    logger.warning(
                        f"[图生图][OpenAI] 返回格式不兼容，自动切换模式重试: {detail[:120]}"
                    )
                    break
                if plugin._is_retryable_status(status) and attempt < plugin.MAX_REQUEST_RETRIES - 1:
                    await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                    continue
                return [], detail
            except _transport_error_classes():
                if attempt < plugin.MAX_REQUEST_RETRIES - 1:
                    await asyncio.sleep(plugin._retry_delay_seconds(attempt))
                    continue
                return [], "请求超时，请重试"
            except Exception as e:
                logger.error(f"[图生图][OpenAI] 请求异常: {e}")
                return [], plugin._translate_error(str(e))

    return [], last_error or "图生图请求失败"
