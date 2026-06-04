import asyncio
import base64
import io
import json
import math
import mimetypes
import time
import re
import uuid
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import urlparse, unquote
Image = None
try:
    from PIL import Image
except ImportError:
    pass

import aiohttp
import aiofiles
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.platform.astr_message_event import AstrMessageEvent

try:
    from . import (
        grok2api_image_backend,
        grok2api_video_backend,
        openai_image_backend,
        openai_video_backend,
        xai_image_backend,
        xai_video_backend,
    )
except ImportError:
    import grok2api_image_backend
    import grok2api_video_backend
    import openai_image_backend
    import openai_video_backend
    import xai_image_backend
    import xai_video_backend

try:
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.provider.func_tool_manager import FunctionToolManager
except ImportError:
    ProviderRequest = Any
    FunctionToolManager = None


class GrokPlugin(Star):
    """Grok 多媒体与联网搜索插件 - 支持生图、生视频、视频编辑/扩展、联网搜索"""

    DEFAULT_TEXT_IMAGE_SIZE = "720x1280"  # 9:16 竖屏
    DEFAULT_IMAGE_RESOLUTION = "2k"  # 默认 2k 提升质量
    DEFAULT_IMAGE_MODEL = "grok-imagine-image-quality"
    DEFAULT_LEGACY_IMAGE_MODEL = "grok-imagine-1.0"
    DEFAULT_LEGACY_EDIT_MODEL = "grok-imagine-1.0-edit"
    SUPPORTED_IMAGE_RESOLUTIONS = ("1k", "2k")
    DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
    DEFAULT_VIDEO_SIZE = "1280x720"      # 16:9 横构图
    DEFAULT_VIDEO_LENGTH_SECONDS = 8
    MIN_VIDEO_LENGTH_SECONDS = 1
    MAX_VIDEO_LENGTH_SECONDS = 15
    MAX_REFERENCE_VIDEO_LENGTH_SECONDS = 10
    DEFAULT_VIDEO_MODEL = "grok-imagine-video"
    DEFAULT_VIDEO_EDIT_MODEL = "grok-imagine-video"
    DEFAULT_VIDEO_EXTENSION_MODEL = "grok-imagine-video"
    DEFAULT_VIDEO_RESOLUTION = "720p"
    DEFAULT_VIDEO_EXTENSION_DURATION_SECONDS = 6
    MIN_VIDEO_EXTENSION_SECONDS = 1
    MAX_VIDEO_EXTENSION_SECONDS = 10
    SUPPORTED_VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
    VIDEO_POLL_INTERVAL_SECONDS = 5.0
    SUPPORTED_IMAGE_ASPECT_RATIOS = (
        "1:1",
        "16:9",
        "9:16",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
        "2:1",
        "1:2",
        "19.5:9",
        "9:19.5",
        "20:9",
        "9:20",
        "auto",
    )
    SUPPORTED_IMAGE_SIZES = (
        "1024x1024",
        "1024x1792",
        "1280x720",
        "1792x1024",
        "720x1280",
    )
    SIZE_TO_ASPECT_RATIO = {
        "1280x720": "16:9",
        "720x1280": "9:16",
        "1792x1024": "3:2",
        "1024x1792": "2:3",
        "1024x1024": "1:1",
    }
    # 反向映射：比例 → 像素尺寸（用于用户输入比例格式时的转换）
    ASPECT_RATIO_TO_SIZE = {
        "16:9": "1280x720",
        "9:16": "720x1280",
        "3:2": "1792x1024",
        "2:3": "1024x1792",
        "1:1": "1024x1024",
    }
    SUPPORTED_VIDEO_ASPECT_RATIOS = (
        "16:9",
        "9:16",
        "1:1",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
    )
    IMAGE_MODEL_FALLBACKS = [
        DEFAULT_IMAGE_MODEL,
        "grok-imagine-image",
        DEFAULT_LEGACY_IMAGE_MODEL,
    ]
    EDIT_IMAGE_MODEL_FALLBACKS = [
        DEFAULT_IMAGE_MODEL,
        "grok-imagine-image",
        DEFAULT_LEGACY_EDIT_MODEL,
        DEFAULT_LEGACY_IMAGE_MODEL,
    ]
    DEFAULT_SEARCH_MODEL = "grok-4-fast"
    DEFAULT_SEARCH_TIMEOUT = 60.0
    DEFAULT_SEARCH_THINKING_BUDGET = 32000

    MAX_IMAGE_COUNT = 10
    MAX_EDIT_REFERENCE_IMAGES = 3
    MAX_VIDEO_REFERENCE_IMAGES = 7
    MAX_STREAM_LINES = 10000
    MAX_RESPONSE_BYTES = 50 * 1024 * 1024
    MIN_BASE64_LENGTH = 100
    IMAGE_TIMEOUT = 150
    VIDEO_TIMEOUT = 300
    MAX_PROMPT_LENGTH = 4000
    MAX_REQUEST_RETRIES = 3
    RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
    MODEL_CACHE_TTL_SECONDS = 300
    MODEL_PROBE_TIMEOUT = 15
    IMAGE_RESPONSE_FORMAT_CANDIDATES = ("b64_json", "url", None)
    DEBUG_LOG_STRING_LIMIT = 1000

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._models_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._models_cache_lock = asyncio.Lock()
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_grok_suite")
        self.temp_dir = Path(self.plugin_data_dir) / "temp"
        self.image_dir = Path(self.plugin_data_dir) / "images"
        self.video_dir = Path(self.plugin_data_dir) / "videos"
        self.temp_dir.mkdir(exist_ok=True, parents=True)
        self.image_dir.mkdir(exist_ok=True, parents=True)
        self.video_dir.mkdir(exist_ok=True, parents=True)

    async def initialize(self):
        if Image is None:
            logger.warning("Pillow 未安装，部分功能受限")
        async with self._session_lock:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
        logger.info("Grok 多媒体与联网搜索插件初始化完成")

    async def terminate(self):
        async with self._session_lock:
            if self._session and not self._session.closed:
                try:
                    await self._session.close()
                except Exception as e:
                    logger.warning(f"关闭 session 时出错: {e}")
            self._session = None
        logger.info("Grok 多媒体与联网搜索插件已终止")

    # ==================== 工具方法 ====================

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """移除文本中的 Markdown 格式，但保留排版结构（换行、段落、列表）"""
        if not text:
            return ""

        # 移除代码块标记，但保留内容和内部换行
        text = re.sub(r'```(?:\w+)?\n?([\s\S]*?)```', r'\1', text)

        # 移除行内代码标记
        text = re.sub(r'`([^`]+)`', r'\1', text)

        # 移除粗体标记
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'__([^_]+)__', r'\1', text)

        # 移除斜体标记
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'_([^_]+)_', r'\1', text)

        # 移除标题符号，保留标题文本和换行
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

        # 转换链接格式：[文本](url) → 文本: url
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)

        # 移除图片标记，保留 alt 文本
        text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)

        # 移除水平线（整行删除）
        text = re.sub(r'^[-*_]{3,}\s*$\n?', '', text, flags=re.MULTILINE)

        # 移除引用符号，保留引用内容
        text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)

        # 移除多余的连续空行（保留最多一个空行）
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    ERROR_TRANSLATIONS = {
        "Session is closed": "会话已关闭，请重试",
        "Connection reset by peer": "连接被重置，请重试",
        "Connection refused": "连接被拒绝，请检查API地址",
        "Timeout": "请求超时，请重试",
        "TimeoutError": "请求超时，请重试",
        "Name or service not known": "无法解析API地址，请检查网络",
        "No route to host": "无法连接到服务器，请检查网络",
        "Network is unreachable": "网络不可达，请检查网络连接",
        "SSL": "SSL证书错误，请检查API地址",
        "Certificate": "证书验证失败",
        "Unauthorized": "API密钥无效或已过期",
        "Forbidden": "访问被拒绝，请检查权限",
        "Not Found": "API接口不存在，请检查配置",
        "Too Many Requests": "请求过于频繁，请稍后重试",
        "Rate limit": "已达到速率限制，请稍后重试",
        "Internal Server Error": "服务器内部错误，请稍后重试",
        "Bad Gateway": "网关错误，请稍后重试",
        "Service Unavailable": "服务暂时不可用，请稍后重试",
        "Gateway Timeout": "网关超时，请稍后重试",
        "Invalid API Key": "API密钥无效",
        "Insufficient quota": "API额度不足",
        "Model not found": "模型不存在，请检查配置",
        "Content policy": "内容违反使用政策",
        "Safety system": "触发安全系统限制",
    }

    def _translate_error(self, error: str) -> str:
        """将英文错误消息翻译为中文"""
        if not error:
            return "未知错误"

        raw_error = str(error).strip()
        if not raw_error:
            return "未知错误"

        # 已经是中文，直接透传，避免二次翻译后信息丢失
        if any("\u4e00" <= c <= "\u9fff" for c in raw_error):
            return raw_error

        error_lower = raw_error.lower()

        if "fail_to_fetch_task" in error_lower and "incorrect api key" in error_lower:
            return "视频任务认证失败：API密钥无效，或代理服务端上游视频 API Key 配置错误"
        if "incorrect api key provided" in error_lower:
            return "API密钥无效或与当前 API 地址不匹配"

        # 检查是否匹配已知错误模式
        for en_pattern, zh_msg in self.ERROR_TRANSLATIONS.items():
            if en_pattern.lower() in error_lower:
                return zh_msg

        if "invalid_size" in error_lower or "size must be" in error_lower:
            return f"尺寸参数不合法: {raw_error}"

        if "invalid_resolution" in error_lower or "resolution_name" in error_lower:
            return f"视频分辨率参数不合法: {raw_error}"

        # 处理 HTTP 状态码
        if "状态码: 401" in raw_error or "status: 401" in error_lower:
            return "API密钥无效或已过期"
        if "状态码: 403" in raw_error or "status: 403" in error_lower:
            return "访问被拒绝"
        if "状态码: 404" in raw_error or "status: 404" in error_lower:
            return "API接口不存在"
        if "状态码: 429" in raw_error or "status: 429" in error_lower:
            return "请求过于频繁，请稍后重试"
        if "状态码: 5" in raw_error or "status: 5" in error_lower:
            return "服务器错误，请稍后重试"

        # 处理 Errno 错误
        if "errno" in error_lower:
            if "104" in raw_error:
                return "连接被重置，请重试"
            if "111" in raw_error:
                return "连接被拒绝，请检查API地址"
            if "110" in raw_error:
                return "连接超时，请重试"
            if "113" in raw_error:
                return "无法连接到服务器"

        # 提取末尾更有价值的片段
        if ":" in raw_error:
            parts = raw_error.split(":")
            for part in reversed(parts):
                part = part.strip()
                if part and not part.startswith("[") and len(part) > 3:
                    return part[:200]

        return raw_error[:200]

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """确保 session 有效（线程安全）"""
        async with self._session_lock:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    def _parse_image_api_response(self, data: dict) -> List[Tuple[Optional[str], Optional[bytes]]]:
        """解析图片生成 API 响应，返回 [(url, bytes), ...]"""
        results = []
        # 标准 OpenAI 格式: {"data": [{"url": "..."} or {"b64_json": "..."}]}
        if "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if isinstance(item, dict):
                    if item.get("url"):
                        results.append((item["url"], None))
                    elif item.get("b64_json"):
                        try:
                            img_bytes = base64.b64decode(item["b64_json"])
                            results.append((None, img_bytes))
                        except Exception as e:
                            logger.warning(f"Base64 解码失败: {e}")

        # 其他格式: 尝试提取 URL 或 Base64
        if not results:
            url, b64, _ = self._parse_json_response(data)
            if url:
                results.append((url, None))
            elif b64:
                try:
                    img_bytes = base64.b64decode(b64)
                    results.append((None, img_bytes))
                except Exception as e:
                    logger.warning(f"Base64 解码失败: {e}")

        return results

    @staticmethod
    def _extract_api_error_message(raw_text: str) -> str:
        """从 API 错误响应中提取可读信息"""
        if not raw_text:
            return ""

        text = raw_text.strip()
        if not text:
            return ""

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text[:500]

        if isinstance(data, dict):
            value = data.get("message")
            if isinstance(value, str) and value.strip():
                return value.strip()

            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                message = str(error_obj.get("message", "")).strip()
                code = str(error_obj.get("code", "")).strip()
                param = str(error_obj.get("param", "")).strip()
                parts = []
                if message:
                    parts.append(message)
                if code and code not in message:
                    parts.append(f"code={code}")
                if param and param not in message:
                    parts.append(f"param={param}")
                if parts:
                    return " | ".join(parts)
            elif isinstance(error_obj, str) and error_obj.strip():
                return error_obj.strip()

            for key in ("detail", "error_description"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return text[:500]

    def _summarize_request_debug_value(self, value: Any) -> Any:
        if isinstance(value, bytes):
            return {"type": "bytes", "length": len(value)}
        if isinstance(value, str):
            if value.startswith("data:") and "," in value:
                header, encoded = value.split(",", 1)
                return {
                    "type": "data_url",
                    "media_type": header[5:].split(";", 1)[0] if header.startswith("data:") else "",
                    "length": len(value),
                    "encoded_length": len(encoded),
                    "preview": f"{header},{encoded[:48]}...",
                }
            if len(value) > self.DEBUG_LOG_STRING_LIMIT:
                return {
                    "type": "string",
                    "length": len(value),
                    "preview": f"{value[:self.DEBUG_LOG_STRING_LIMIT]}...",
                }
            return value
        if isinstance(value, dict):
            return {
                str(key): self._summarize_request_debug_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._summarize_request_debug_value(item) for item in value]
        return value

    def _summarize_headers_for_debug(self, headers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        summarized: Dict[str, Any] = {}
        for key, value in (headers or {}).items():
            key_text = str(key)
            if key_text.lower() == "authorization":
                summarized[key_text] = "Bearer ***"
            else:
                summarized[key_text] = self._summarize_request_debug_value(value)
        return summarized

    def _debug_log_request(
        self,
        scene: str,
        method: str,
        api_url: str,
        *,
        headers: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        form_body: Optional[Any] = None,
        attempt: Optional[int] = None,
    ) -> None:
        body = json_body if json_body is not None else form_body
        body_type = "json" if json_body is not None else "multipart" if form_body is not None else "none"
        debug_payload = {
            "method": method.upper(),
            "url": api_url,
            "attempt": attempt,
            "headers": self._summarize_headers_for_debug(headers),
            "body_type": body_type,
            "body": self._summarize_request_debug_value(body),
        }
        logger.debug(
            f"[{scene}] 即将发送 API 请求: "
            f"{json.dumps(debug_payload, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _log_error_response(scene: str, status: int, response_text: str) -> None:
        logger.error(f"[{scene}] API 错误响应 (状态码: {status}): {response_text}")

    def _build_form_file_debug_field(
        self,
        name: str,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Dict[str, Any]:
        return {
            "name": name,
            "kind": "file",
            "filename": filename,
            "content_type": content_type,
            "bytes": len(data),
        }

    @staticmethod
    def _is_size_related_error(error_message: str) -> bool:
        """判断是否是尺寸参数相关错误"""
        if not error_message:
            return False
        err = error_message.lower()
        if "invalid_size" in err or "size must be" in err:
            return True
        return "size" in err and (
            "invalid" in err
            or "unsupported" in err
            or "unknown" in err
            or "must be" in err
        )

    @staticmethod
    def _is_resolution_related_error(error_message: str) -> bool:
        """判断是否是视频分辨率参数相关错误"""
        if not error_message:
            return False
        err = error_message.lower()
        if "invalid_resolution" in err:
            return True
        if "resolution_name" in err:
            return True
        return "resolution" in err and (
            "invalid" in err
            or "unsupported" in err
            or "must be" in err
        )

    @staticmethod
    def _is_response_format_related_error(error_message: str) -> bool:
        """判断是否是媒体格式参数相关错误"""
        if not error_message:
            return False
        err = error_message.lower()
        if "response_format" in err:
            return True
        return "format" in err and (
            "invalid" in err
            or "unsupported" in err
            or "must be" in err
        )

    def _get_image_response_format_candidates(self) -> Tuple[Optional[str], ...]:
        """获取图片响应格式候选顺序。默认强制 base64，避免依赖 xAI 图片 CDN。"""
        mode = str(self.conf.get("grok_image_response_format", "b64_json") or "b64_json").strip().lower()
        if mode in {"auto", "fallback"}:
            return self.IMAGE_RESPONSE_FORMAT_CANDIDATES
        if mode == "url":
            return ("url",)
        if mode in {"none", "default", "provider"}:
            return (None,)
        if mode not in {"b64_json", "base64"}:
            logger.warning(f"图片响应格式配置无效: {mode}, 已回退为 b64_json")
        return ("b64_json",)

    @classmethod
    def _is_retryable_status(cls, status_code: int) -> bool:
        """判断状态码是否适合自动重试"""
        return status_code in cls.RETRYABLE_HTTP_STATUS_CODES

    @staticmethod
    def _retry_delay_seconds(attempt_index: int) -> float:
        """退避重试等待时长"""
        return min(1.5 * (2 ** attempt_index), 4.0)

    @classmethod
    def _parse_video_length_token(cls, token: str) -> Optional[int]:
        """解析视频时长参数，支持 1-15 或 1s-15s"""
        if not token:
            return None
        cleaned = token.strip().lower()
        if cleaned.endswith("s"):
            cleaned = cleaned[:-1]
        if not cleaned.isdigit():
            return None
        value = int(cleaned)
        if cls.MIN_VIDEO_LENGTH_SECONDS <= value <= cls.MAX_VIDEO_LENGTH_SECONDS:
            return value
        return None

    @classmethod
    def _parse_video_extension_duration_token(cls, token: str) -> Optional[int]:
        """解析视频扩展时长参数，支持 1-10 或 1s-10s"""
        value = cls._parse_video_length_token(token)
        if value is None:
            return None
        if cls.MIN_VIDEO_EXTENSION_SECONDS <= value <= cls.MAX_VIDEO_EXTENSION_SECONDS:
            return value
        return None

    @classmethod
    def _is_video_length_like_token(cls, token: str) -> bool:
        if not token:
            return False
        cleaned = token.strip().lower()
        if cleaned.endswith("s"):
            cleaned = cleaned[:-1]
        return cleaned.isdigit()

    @classmethod
    def _normalize_video_resolution(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        normalized = value.strip().lower()
        if normalized in cls.SUPPORTED_VIDEO_RESOLUTIONS:
            return normalized
        return None

    @staticmethod
    def _is_video_resolution_like_token(token: str) -> bool:
        return bool(token and re.fullmatch(r"\d+p", token.strip().lower()))

    @staticmethod
    def _is_video_aspect_ratio_like_token(token: str) -> bool:
        return bool(token and re.fullmatch(r"\d+(?:\.\d+)?:\d+(?:\.\d+)?", token.strip()))

    @staticmethod
    def _is_video_aspect_ratio_null_token(token: str) -> bool:
        return bool(token and token.strip().lower() in {"null", "none", "auto", "不传"})

    @staticmethod
    def _extract_first_url(text: str) -> Tuple[Optional[str], str]:
        if not text:
            return None, ""
        match = re.search(r"https?://\S+", text)
        if not match:
            return None, text
        url = match.group(0).rstrip("，。,.")
        remaining = (text[:match.start()] + text[match.end():]).strip()
        return url, re.sub(r"\s+", " ", remaining).strip()

    @staticmethod
    def _extract_inline_file_id(text: str) -> Tuple[Optional[str], str]:
        file_ids, remaining = GrokPlugin._extract_inline_file_ids(text)
        return (file_ids[0] if file_ids else None), remaining

    @staticmethod
    def _extract_inline_file_ids(text: str) -> Tuple[List[str], str]:
        if not text:
            return [], ""

        file_ids: List[str] = []

        def _collect(match: re.Match) -> str:
            file_ids.append(match.group(1).strip())
            return " "

        remaining = re.sub(
            r"(?:^|\s)file_id\s*[:=]\s*([A-Za-z0-9_-]+)",
            _collect,
            text,
            flags=re.IGNORECASE,
        )
        return file_ids, re.sub(r"\s+", " ", remaining).strip()

    @staticmethod
    def _extract_inline_image_urls(text: str) -> Tuple[List[str], str]:
        if not text:
            return [], ""

        image_urls: List[str] = []

        def _collect(match: re.Match) -> str:
            image_urls.append(match.group(1).rstrip("，。,.;"))
            return " "

        remaining = re.sub(
            r"(?:^|\s)(?:image_url|image-url|图片url|图片URL)\s*[:=]\s*(data:image/\S+|https?://\S+)",
            _collect,
            text,
            flags=re.IGNORECASE,
        )
        return image_urls, re.sub(r"\s+", " ", remaining).strip()

    @staticmethod
    def _extract_video_reference_mode(text: str) -> Tuple[bool, str]:
        if not text:
            return False, ""

        mode_tokens = {"参考图", "参考图视频", "reference", "r2v", "--reference", "--ref"}
        kept_parts: List[str] = []
        found = False
        for part in text.split():
            if part.lower() in mode_tokens:
                found = True
            else:
                kept_parts.append(part)
        return found, " ".join(kept_parts).strip()

    @staticmethod
    def _is_mp4_url(url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        try:
            return urlparse(url).path.lower().endswith(".mp4")
        except Exception:
            return False

    @staticmethod
    def _segment_type_name(seg: Any) -> str:
        if not seg:
            return ""
        return seg.__class__.__name__.lower()

    def _is_segment_type(self, seg: Any, type_name: str) -> bool:
        """兼容不同平台实现的消息段类型判断"""
        cls = getattr(Comp, type_name, None)
        if cls is not None:
            try:
                if isinstance(seg, cls):
                    return True
            except Exception:
                pass
        return self._segment_type_name(seg) == type_name.lower()

    @staticmethod
    def _extract_segment_sources(seg: Any) -> List[str]:
        sources: List[str] = []
        for key in ("file", "url", "path", "src"):
            value = getattr(seg, key, None)
            if isinstance(value, str) and value.strip():
                sources.append(value.strip())
        return list(dict.fromkeys(sources))

    @staticmethod
    def _guess_filename_from_source(source: Optional[str], fallback: str) -> str:
        if not source:
            return fallback
        try:
            if source.startswith("http"):
                parsed = urlparse(source)
                candidate = unquote(Path(parsed.path).name)
            else:
                candidate = Path(source).name
            if candidate:
                return candidate
        except Exception:
            pass
        return fallback

    @staticmethod
    def _guess_mime_type_from_source(source: Optional[str], default: str) -> str:
        if source:
            guess, _ = mimetypes.guess_type(source)
            if guess:
                return guess
        return default

    @classmethod
    def _guess_audio_format_from_source(cls, source: Optional[str]) -> str:
        if not source:
            return "mp3"
        name = source.split("?", 1)[0].lower()
        if name.endswith(".wav"):
            return "wav"
        if name.endswith(".flac"):
            return "flac"
        if name.endswith(".ogg"):
            return "ogg"
        if name.endswith(".m4a"):
            return "m4a"
        if name.endswith(".aac"):
            return "aac"
        if name.endswith(".opus"):
            return "opus"
        if name.endswith(".mp3"):
            return "mp3"
        return "mp3"

    @staticmethod
    def _parse_size_string(size: str) -> Optional[Tuple[int, int]]:
        """解析 WxH 字符串"""
        if not size or "x" not in size.lower():
            return None
        try:
            width_str, height_str = size.lower().split("x", 1)
            width = int(width_str.strip())
            height = int(height_str.strip())
            if width <= 0 or height <= 0:
                return None
            return width, height
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _format_size(width: int, height: int) -> str:
        """格式化尺寸字符串"""
        return f"{width}x{height}"

    def _normalize_supported_size(self, size: str) -> Optional[str]:
        """归一化并校验是否为受支持尺寸

        支持两种输入格式：
        1. 像素格式：如 "1280x720"
        2. 比例格式：如 "16:9"

        Returns:
            标准化的像素格式字符串，如 "1280x720"；无效输入返回 None
        """
        # 先尝试比例格式（如 "16:9"）
        if ":" in size:
            parts = size.split(":")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                # 有效的比例格式，查找对应的像素尺寸
                if size in self.ASPECT_RATIO_TO_SIZE:
                    return self.ASPECT_RATIO_TO_SIZE[size]
                return None

        # 尝试像素格式（如 "1280x720"）
        parsed = self._parse_size_string(size)
        if not parsed:
            return None
        normalized = self._format_size(parsed[0], parsed[1])
        if normalized in self.SUPPORTED_IMAGE_SIZES:
            return normalized
        return None

    def _normalize_image_size_or_ratio(self, value: str) -> Optional[str]:
        """归一化图片命令参数，允许官方 aspect_ratio 直接输入。"""
        normalized = self._normalize_supported_size(value)
        if normalized:
            return normalized
        if value in self.SUPPORTED_IMAGE_ASPECT_RATIOS:
            return value
        return None

    @classmethod
    def _normalize_image_aspect_ratio(cls, value: Optional[str]) -> str:
        """转换为 xAI 图片接口支持的 aspect_ratio。"""
        if not value:
            return cls._size_to_aspect_ratio(cls.DEFAULT_TEXT_IMAGE_SIZE)
        if value in cls.SUPPORTED_IMAGE_ASPECT_RATIOS:
            return value
        return cls._size_to_aspect_ratio(value)

    def _get_image_resolution(self, image_bytes: bytes) -> Optional[Tuple[int, int]]:
        """读取图片分辨率"""
        if not Image:
            return None
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                width, height = img.size
            if width <= 0 or height <= 0:
                return None
            return width, height
        except Exception as e:
            logger.warning(f"读取图片分辨率失败: {e}")
            return None

    def _get_closest_supported_size(self, width: int, height: int) -> Optional[str]:
        """按分辨率距离匹配最接近的合法尺寸"""
        if width <= 0 or height <= 0:
            return None

        candidates: List[Tuple[str, int, int]] = []
        for size_str in self.SUPPORTED_IMAGE_SIZES:
            parsed = self._parse_size_string(size_str)
            if parsed:
                candidates.append((size_str, parsed[0], parsed[1]))

        if not candidates:
            return None

        target_ratio = width / height
        target_area = width * height

        def distance(item: Tuple[str, int, int]) -> Tuple[float, float, float]:
            _, cand_w, cand_h = item
            dim_distance = (
                abs(cand_w - width) / max(width, 1)
                + abs(cand_h - height) / max(height, 1)
            )
            ratio_distance = abs((cand_w / cand_h) - target_ratio)
            area_distance = abs((cand_w * cand_h) - target_area) / max(target_area, 1)
            # 比例优先：先保证构图比例，再考虑像素接近度
            return ratio_distance, area_distance, dim_distance

        best = min(candidates, key=distance)
        return best[0]

    @classmethod
    def _size_to_aspect_ratio(cls, size: str) -> str:
        """将像素尺寸转换为宽高比

        Args:
            size: 像素尺寸字符串（如 "1280x720"）或比例字符串（如 "16:9"）

        Returns:
            宽高比字符串，如 "16:9"
        """
        # 白名单：Grok 支持的标准比例
        ALLOWED_RATIOS = {"1:1", "2:3", "3:2", "9:16", "16:9"}

        if size in cls.SIZE_TO_ASPECT_RATIO:
            return cls.SIZE_TO_ASPECT_RATIO[size]
        if ":" in size:
            parts = size.split(":")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                # 只接受白名单中的比例
                if size in ALLOWED_RATIOS:
                    return size
        # 回退值与 grok2api 保持一致
        return "2:3"

    @classmethod
    def _normalize_video_aspect_ratio(cls, value: Optional[str]) -> Optional[str]:
        """归一化视频宽高比参数，允许官方比例或可约分像素尺寸。"""
        if not value:
            return None

        candidate = value.strip()
        if candidate in cls.SUPPORTED_VIDEO_ASPECT_RATIOS:
            return candidate

        mapped = cls.SIZE_TO_ASPECT_RATIO.get(candidate)
        if mapped in cls.SUPPORTED_VIDEO_ASPECT_RATIOS:
            return mapped

        parsed = cls._parse_size_string(candidate)
        if not parsed:
            return None

        width, height = parsed
        divisor = math.gcd(width, height)
        if divisor <= 0:
            return None
        ratio = f"{width // divisor}:{height // divisor}"
        if ratio in cls.SUPPORTED_VIDEO_ASPECT_RATIOS:
            return ratio
        return None

    @classmethod
    def _get_aspect_ratio_display(cls, size: str) -> str:
        """获取尺寸的比例显示（用于用户提示）

        Args:
            size: 像素尺寸字符串（如 "1280x720"）

        Returns:
            比例字符串，如 "16:9"，如果无法识别则返回原始尺寸
        """
        if size in cls.SIZE_TO_ASPECT_RATIO:
            return cls.SIZE_TO_ASPECT_RATIO[size]
        return size

    def _build_video_prompt(self, prompt: str, mode: str) -> str:
        """构建视频增强提示词，默认开启细节与稳定性增强"""
        prompt = prompt.strip()
        if not prompt:
            return ""

        enhancement_hint = (
            "画面要求：高细节、清晰边缘、低噪点、运动稳定、时序一致。"
            "输出风格自然，不要过度锐化。"
        )
        if mode == "reference-to-video":
            consistency_hint = "保持参考图主体身份、构图和色调风格一致。"
        elif mode == "image-to-video":
            consistency_hint = "保持首帧主体、构图和色调一致，让画面自然动起来。"
        else:
            consistency_hint = "主体动作连贯，镜头转场平滑。"
        return f"{prompt}\n\n{enhancement_hint}{consistency_hint}"

    @staticmethod
    def _normalize_api_scope(scope: Optional[str]) -> str:
        normalized = str(scope or "").strip().lower()
        return normalized if normalized in {"image", "video"} else "global"

    def _get_api_key(self, scope: Optional[str] = None) -> str:
        normalized_scope = self._normalize_api_scope(scope)
        scoped_key = ""
        if normalized_scope == "image":
            scoped_key = str(self.conf.get("grok_image_api_key", "") or "").strip()
        elif normalized_scope == "video":
            scoped_key = str(self.conf.get("grok_video_api_key", "") or "").strip()
        if scoped_key:
            return scoped_key
        return str(self.conf.get("grok_api_key", "") or "").strip()

    @staticmethod
    def _normalize_api_base_url(url: str) -> str:
        url = str(url or "https://api.x.ai").rstrip("/")
        suffixes = [
            "/v1/chat/completions", "/v1/images/generations", "/v1/images/edits",
            "/v1/video/generations", "/v1/videos/generations", "/v1/videos/edits",
            "/v1/videos/extensions", "/v1/videos", "/chat/completions",
            "/images/generations", "/images/edits", "/video/generations",
            "/videos/generations", "/videos/edits", "/videos/extensions",
            "/videos", "/v1"
        ]
        for suffix in suffixes:
            if url.endswith(suffix):
                url = url[:-len(suffix)]
        return url.rstrip("/")

    async def _fetch_available_models(self, scope: Optional[str] = None) -> Optional[set]:
        """探测当前可用模型列表，带短时缓存"""
        now = time.time()
        normalized_scope = self._normalize_api_scope(scope)
        base_url = self._get_base_url(normalized_scope)
        api_key = self._get_api_key(normalized_scope)
        if not api_key:
            return None

        cache_key = (normalized_scope, base_url, api_key)
        async with self._models_cache_lock:
            cached_entry = self._models_cache.get(cache_key, {})
            cached_models = set(cached_entry.get("models", set()))
            expires_at = float(cached_entry.get("expires_at", 0.0))
            if cached_models and now < expires_at:
                return cached_models

        api_url = f"{base_url}/v1/models"
        try:
            session = await self._ensure_session()
            headers = {"Authorization": f"Bearer {api_key}"}
            self._debug_log_request(
                "模型探测",
                "GET",
                api_url,
                headers=headers,
            )
            async with session.get(
                api_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.MODEL_PROBE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    self._log_error_response("模型探测", resp.status, text)
                    return None
                raw_text = await resp.text()
            data = json.loads(raw_text)
            model_ids: set = set()
            for item in data.get("data", []) if isinstance(data, dict) else []:
                if isinstance(item, dict):
                    model_id = str(item.get("id", "")).strip()
                    if model_id:
                        model_ids.add(model_id)
            if not model_ids:
                return None
            async with self._models_cache_lock:
                self._models_cache[cache_key] = {
                    "models": model_ids,
                    "expires_at": time.time() + self.MODEL_CACHE_TTL_SECONDS,
                }
            return set(model_ids)
        except Exception:
            return None

    async def _resolve_model(
        self,
        configured_model: str,
        fallback_models: List[str],
        scene: str,
        scope: Optional[str] = None,
    ) -> str:
        """根据 /v1/models 自动选择可用模型，不可用时按候选回退"""
        preferred_model = str(configured_model or "").strip()
        if not preferred_model and fallback_models:
            preferred_model = fallback_models[0]

        candidates: List[str] = []
        for model_name in [preferred_model, *fallback_models]:
            model_name = str(model_name or "").strip()
            if model_name and model_name not in candidates:
                candidates.append(model_name)

        if not candidates:
            return preferred_model

        available_models = await self._fetch_available_models(scope=scope)
        if not available_models:
            return candidates[0]

        for candidate in candidates:
            if candidate in available_models:
                if candidate != candidates[0]:
                    logger.warning(
                        f"[{scene}] 配置模型不可用，自动回退为可用模型: {candidate}"
                    )
                return candidate

        logger.warning(f"[{scene}] 未命中可用候选模型，继续使用: {candidates[0]}")
        return candidates[0]

    # ==================== API 调用 ====================

    @staticmethod
    def _detect_mime_type(data: bytes) -> str:
        """检测图片 MIME 类型"""
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return "image/png"
        if data.startswith(b'\xff\xd8\xff'):
            return "image/jpeg"
        if data.startswith((b'GIF87a', b'GIF89a')):
            return "image/gif"
        if data.startswith(b'RIFF') and len(data) > 12 and data[8:12] == b'WEBP':
            return "image/webp"
        if data.startswith(b'BM'):
            return "image/bmp"
        return "image/png"

    def _build_image_data_url(self, image_bytes: bytes) -> str:
        mime_type = self._detect_mime_type(image_bytes)
        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{b64_data}"

    def _build_image_input_object(self, image_bytes: bytes) -> Dict[str, str]:
        return {
            "type": "image_url",
            "url": self._build_image_data_url(image_bytes),
        }

    def _build_video_image_object(self, image_bytes: bytes) -> Dict[str, str]:
        return {"url": self._build_image_data_url(image_bytes)}

    @staticmethod
    def _detect_video_mime_type(data: bytes) -> str:
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return "video/mp4"
        return "video/mp4"

    def _build_video_data_url(self, video_bytes: bytes) -> str:
        mime_type = self._detect_video_mime_type(video_bytes)
        b64_data = base64.b64encode(video_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{b64_data}"

    def _build_video_input_object(
        self,
        *,
        url: Optional[str] = None,
        video_bytes: Optional[bytes] = None,
    ) -> Optional[Dict[str, str]]:
        if url:
            return {"url": url}
        if video_bytes:
            return {"url": self._build_video_data_url(video_bytes)}
        return None

    async def _resolve_inline_video_source(
        self,
        url: str,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        if self._is_mp4_url(url):
            return {"url": url}, url
        video_bytes = await self._download_media(url)
        if video_bytes:
            return {"url": self._build_video_data_url(video_bytes)}, url
        return None, url

    @staticmethod
    def _is_supported_edit_image_mime(mime_type: str) -> bool:
        return mime_type in {"image/jpeg", "image/png", "image/webp"}

    def _get_configured_image_resolution(self) -> str:
        resolution = str(
            self.conf.get("grok_image_resolution", self.DEFAULT_IMAGE_RESOLUTION)
        ).strip().lower()
        if resolution in self.SUPPORTED_IMAGE_RESOLUTIONS:
            return resolution
        logger.warning(
            f"图片分辨率配置无效: {resolution}, 已回退为 {self.DEFAULT_IMAGE_RESOLUTION}"
        )
        return self.DEFAULT_IMAGE_RESOLUTION

    def _get_configured_video_resolution(self) -> str:
        resolution = str(
            self.conf.get("grok_video_resolution", self.DEFAULT_VIDEO_RESOLUTION)
        ).strip().lower()
        if resolution in self.SUPPORTED_VIDEO_RESOLUTIONS:
            return resolution
        logger.warning(
            f"视频分辨率配置无效: {resolution}, 已回退为 {self.DEFAULT_VIDEO_RESOLUTION}"
        )
        return self.DEFAULT_VIDEO_RESOLUTION

    def _get_configured_image_timeout_seconds(self) -> float:
        try:
            timeout = float(self.conf.get("grok_image_timeout_seconds", self.IMAGE_TIMEOUT))
        except (TypeError, ValueError):
            timeout = float(self.IMAGE_TIMEOUT)
        if 30 <= timeout <= 600:
            return timeout
        logger.warning(f"图片请求超时配置无效: {timeout}, 已回退为 {self.IMAGE_TIMEOUT}")
        return float(self.IMAGE_TIMEOUT)

    def _get_configured_video_aspect_ratio(self) -> Optional[str]:
        aspect_ratio = str(
            self.conf.get("grok_video_aspect_ratio", self.DEFAULT_VIDEO_ASPECT_RATIO)
        ).strip()
        if self._is_video_aspect_ratio_null_token(aspect_ratio):
            return None
        normalized = self._normalize_video_aspect_ratio(aspect_ratio)
        if normalized:
            return normalized
        logger.warning(
            f"视频默认比例配置无效: {aspect_ratio}, 已回退为 {self.DEFAULT_VIDEO_ASPECT_RATIO}"
        )
        return self.DEFAULT_VIDEO_ASPECT_RATIO

    def _get_configured_video_duration(self) -> int:
        try:
            duration = int(
                self.conf.get(
                    "grok_video_duration",
                    self.DEFAULT_VIDEO_LENGTH_SECONDS,
                )
            )
        except (TypeError, ValueError):
            duration = self.DEFAULT_VIDEO_LENGTH_SECONDS
        if self.MIN_VIDEO_LENGTH_SECONDS <= duration <= self.MAX_VIDEO_LENGTH_SECONDS:
            return duration
        logger.warning(
            f"视频默认时长配置无效: {duration}, "
            f"已回退为 {self.DEFAULT_VIDEO_LENGTH_SECONDS}"
        )
        return self.DEFAULT_VIDEO_LENGTH_SECONDS

    def _get_configured_video_extension_duration(self) -> int:
        try:
            duration = int(
                self.conf.get(
                    "grok_video_extension_duration",
                    self.DEFAULT_VIDEO_EXTENSION_DURATION_SECONDS,
                )
            )
        except (TypeError, ValueError):
            duration = self.DEFAULT_VIDEO_EXTENSION_DURATION_SECONDS
        if self.MIN_VIDEO_EXTENSION_SECONDS <= duration <= self.MAX_VIDEO_EXTENSION_SECONDS:
            return duration
        logger.warning(
            f"视频扩展时长配置无效: {duration}, "
            f"已回退为 {self.DEFAULT_VIDEO_EXTENSION_DURATION_SECONDS}"
        )
        return self.DEFAULT_VIDEO_EXTENSION_DURATION_SECONDS

    def _get_configured_video_timeout_seconds(self) -> float:
        try:
            timeout = float(self.conf.get("grok_video_timeout_seconds", self.VIDEO_TIMEOUT))
        except (TypeError, ValueError):
            timeout = float(self.VIDEO_TIMEOUT)
        if 60 <= timeout <= 3600:
            return timeout
        logger.warning(f"视频轮询超时配置无效: {timeout}, 已回退为 {self.VIDEO_TIMEOUT}")
        return float(self.VIDEO_TIMEOUT)

    def _get_configured_video_poll_interval_seconds(self) -> float:
        try:
            interval = float(
                self.conf.get(
                    "grok_video_poll_interval_seconds",
                    self.VIDEO_POLL_INTERVAL_SECONDS,
                )
            )
        except (TypeError, ValueError):
            interval = float(self.VIDEO_POLL_INTERVAL_SECONDS)
        if 2 <= interval <= 10:
            return interval
        logger.warning(
            f"视频轮询间隔配置无效: {interval}, "
            f"已回退为 {self.VIDEO_POLL_INTERVAL_SECONDS}"
        )
        return float(self.VIDEO_POLL_INTERVAL_SECONDS)

    def _get_configured_video_output(self) -> Optional[Dict[str, str]]:
        upload_url = str(self.conf.get("grok_video_output_upload_url", "")).strip()
        if upload_url:
            return {"upload_url": upload_url}
        return None

    def _is_save_media_enabled(self) -> bool:
        value = self.conf.get("save_media", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _normalize_backend_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized == "grok2api":
            return "grok2api"
        if normalized in {"openai", "openai-sdk"}:
            return "openai"
        return "xai"

    def _get_configured_image_backend_type(self) -> str:
        return self._normalize_backend_type(self.conf.get("grok_image_backend_type", "xAI"))

    def _get_configured_video_backend_type(self) -> str:
        return self._normalize_backend_type(self.conf.get("grok_video_backend_type", "xAI"))

    def _get_headers(self, scope: Optional[str] = None) -> dict:
        api_key = self._get_api_key(scope)
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def _get_auth_headers(self, scope: Optional[str] = None) -> dict:
        api_key = self._get_api_key(scope)
        return {"Authorization": f"Bearer {api_key}"}

    def _get_base_url(self, scope: Optional[str] = None) -> str:
        """获取 API 基础 URL，自动处理常见的 URL 格式问题

        用户只需填写基础 URL（如 https://api.x.ai），
        会自动移除多余的路径后缀，返回纯净的基础 URL
        """
        normalized_scope = self._normalize_api_scope(scope)
        scoped_url = ""
        if normalized_scope == "image":
            scoped_url = str(self.conf.get("grok_image_api_url", "") or "").strip()
        elif normalized_scope == "video":
            scoped_url = str(self.conf.get("grok_video_api_url", "") or "").strip()
        url = scoped_url or str(self.conf.get("grok_api_url", "https://api.x.ai") or "").strip()
        return self._normalize_api_base_url(url)

    def _build_api_url(self, endpoint_path: str, scope: Optional[str] = None) -> str:
        if endpoint_path.startswith(("http://", "https://")):
            return endpoint_path
        return f"{self._get_base_url(scope)}{endpoint_path}"

    async def _generate_image(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        reference_images: Optional[List[bytes]] = None,
        n: int = 1,
        target_size: Optional[str] = None,
    ) -> Tuple[List[Tuple[Optional[str], Optional[bytes]]], Optional[str]]:
        """按 UI 选择的图片后端分发到独立请求模块。"""
        if image_bytes:
            return await self._edit_image(
                prompt,
                image_bytes,
                n=n,
                target_size=target_size,
                reference_images=reference_images,
            )

        backend = self._get_configured_image_backend_type()
        if backend == "openai":
            return await openai_image_backend.generate_image(
                self,
                prompt,
                n=n,
                target_size=target_size,
            )
        if backend == "grok2api":
            return await grok2api_image_backend.generate_image(
                self,
                prompt,
                n=n,
                target_size=target_size,
            )
        return await xai_image_backend.generate_image(
            self,
            prompt,
            n=n,
            target_size=target_size,
        )

    async def _edit_image(
        self,
        prompt: str,
        image_bytes: bytes,
        n: int = 1,
        target_size: Optional[str] = None,
        reference_images: Optional[List[bytes]] = None,
    ) -> Tuple[List[Tuple[Optional[str], Optional[bytes]]], Optional[str]]:
        """按 UI 选择的图片后端分发到独立编辑模块。"""
        backend = self._get_configured_image_backend_type()
        if backend == "openai":
            return await openai_image_backend.edit_image(
                self,
                prompt,
                image_bytes,
                n=n,
                target_size=target_size,
                reference_images=reference_images,
            )
        if backend == "grok2api":
            return await grok2api_image_backend.edit_image(
                self,
                prompt,
                image_bytes,
                n=n,
                target_size=target_size,
                reference_images=reference_images,
            )
        return await xai_image_backend.edit_image(
            self,
            prompt,
            image_bytes,
            n=n,
            target_size=target_size,
            reference_images=reference_images,
        )

    async def _generate_video(
        self,
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
        """按 UI 选择的视频后端分发到独立请求模块。"""
        backend = self._get_configured_video_backend_type()
        if backend == "openai":
            video_backend = openai_video_backend
        elif backend == "grok2api":
            video_backend = grok2api_video_backend
        else:
            video_backend = xai_video_backend
        return await video_backend.generate_video(
            self,
            prompt,
            image_input,
            target_size,
            video_length,
            reference_images=reference_images,
            resolution=resolution,
            aspect_ratio_explicit=aspect_ratio_explicit,
            user=user,
        )

    async def _generate_video_edit(
        self,
        prompt: str,
        video_input: Dict[str, str],
        *,
        source_label: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """按 UI 选择的视频后端分发到独立编辑模块。"""
        backend = self._get_configured_video_backend_type()
        if backend == "openai":
            video_backend = openai_video_backend
        elif backend == "grok2api":
            video_backend = grok2api_video_backend
        else:
            video_backend = xai_video_backend
        return await video_backend.edit_video(
            self,
            prompt,
            video_input,
            source_label=source_label,
            user=user,
        )

    async def _generate_video_extension(
        self,
        prompt: str,
        video_input: Dict[str, str],
        *,
        duration: Optional[int] = None,
        source_label: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """按 UI 选择的视频后端分发到独立扩展模块。"""
        backend = self._get_configured_video_backend_type()
        if backend == "openai":
            video_backend = openai_video_backend
        elif backend == "grok2api":
            video_backend = grok2api_video_backend
        else:
            video_backend = xai_video_backend
        return await video_backend.extend_video(
            self,
            prompt,
            video_input,
            duration=duration,
            source_label=source_label,
        )

    # ==================== 响应解析 ====================

    async def _parse_media_response(self, resp, media_type: str = "image") -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
        """
        统一解析媒体响应，支持流式/非流式、URL/Base64
        返回: (media_bytes, media_url, error_msg)
        """
        accumulated_text = []
        is_streaming = False
        raw_content = b""
        extracted_url = None
        extracted_base64 = None
        line_count = 0

        async for line in resp.content:
            line_count += 1
            if line_count > self.MAX_STREAM_LINES:
                return None, None, "响应行数超限"
            if len(raw_content) > self.MAX_RESPONSE_BYTES:
                return None, None, "响应数据过大"
            raw_content += line
            if not line or not line.strip():
                continue

            try:
                line_str = line.decode('utf-8').strip()
            except UnicodeDecodeError:
                continue

            # SSE 流式解析
            payload_str = None
            if line_str.startswith('data: '):
                payload_str = line_str[6:]
            elif line_str.startswith('data:'):
                payload_str = line_str[5:]

            if payload_str is not None:
                is_streaming = True
                payload_str = payload_str.strip()
                if payload_str in ('[DONE]', 'done', ''):
                    continue

                try:
                    chunk = json.loads(payload_str)
                    # 提取文本内容
                    content = self._extract_text_content(chunk)
                    if content:
                        accumulated_text.append(content)
                    # 提取媒体数据
                    url, b64 = self._extract_media_from_chunk(chunk)
                    if url:
                        extracted_url = url
                    if b64:
                        extracted_base64 = b64
                except json.JSONDecodeError:
                    if payload_str.startswith(("http://", "https://")):
                        extracted_url = payload_str.split()[0]
                    elif self._is_base64(payload_str):
                        extracted_base64 = payload_str

        # 非流式响应处理
        if not is_streaming and raw_content:
            try:
                data = json.loads(raw_content.decode('utf-8'))
                # 处理各种 API 响应格式
                url, b64, text = self._parse_json_response(data)
                if url:
                    extracted_url = url
                if b64:
                    extracted_base64 = b64
                if text:
                    accumulated_text.append(text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        # 从累积文本中提取媒体
        full_text = "".join(accumulated_text)
        if not extracted_url and not extracted_base64 and full_text:
            extracted_url = self._extract_url_from_text(full_text)
            if not extracted_url:
                extracted_base64 = self._extract_base64_from_text(full_text)

        # 返回结果
        if extracted_base64:
            try:
                media_bytes = base64.b64decode(extracted_base64)
                return media_bytes, None, None
            except Exception as e:
                return None, None, f"Base64 解码失败: {e}"

        if extracted_url:
            return None, extracted_url, None

        return None, None, "未能从响应中提取媒体内容"

    def _parse_json_response(self, data: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """解析 JSON 响应，返回 (url, base64, text)"""
        url = None
        b64 = None
        text = None

        # OpenAI 图像生成格式: {"data": [{"url": "..."} or {"b64_json": "..."}]}
        if "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if isinstance(item, dict):
                    if item.get("url"):
                        url = item["url"]
                    if item.get("b64_json"):
                        b64 = item["b64_json"]
                    if item.get("revised_prompt"):
                        text = item["revised_prompt"]

        # Chat Completions 格式
        if "choices" in data:
            for choice in data.get("choices", []):
                msg = choice.get("message") or choice.get("delta") or {}
                content = msg.get("content")
                if content:
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "image_url":
                                    img_url = part.get("image_url", {}).get("url", "")
                                    if img_url.startswith("data:"):
                                        b64 = self._extract_base64_from_data_uri(img_url)
                                    else:
                                        url = img_url
                                elif part.get("type") == "text":
                                    text = part.get("text", "")

        # 直接字段
        for key in ("url", "image_url", "video_url", "media_url", "file_url"):
            if data.get(key):
                url = data[key]
                break

        for key in ("b64_json", "base64", "image_base64", "data"):
            val = data.get(key)
            if val and isinstance(val, str) and self._is_base64(val):
                b64 = val
                break

        for key in ("content", "text", "result", "output", "message"):
            val = data.get(key)
            if val and isinstance(val, str):
                text = val
                break

        return url, b64, text

    def _extract_media_from_chunk(self, chunk: dict) -> Tuple[Optional[str], Optional[str]]:
        """从流式块中提取媒体 URL 或 Base64"""
        url = None
        b64 = None

        # 递归搜索
        def search(obj):
            nonlocal url, b64
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("url", "image_url", "video_url") and isinstance(v, str):
                        if v.startswith("data:"):
                            b64 = self._extract_base64_from_data_uri(v)
                        elif v.startswith("http"):
                            url = v
                    elif k in ("b64_json", "base64") and isinstance(v, str):
                        b64 = v
                    else:
                        search(v)
            elif isinstance(obj, list):
                for item in obj:
                    search(item)

        search(chunk)
        return url, b64

    @staticmethod
    def _extract_text_content(chunk: dict) -> Optional[str]:
        """从响应块中提取文本内容"""
        if chunk.get("choices"):
            choice = chunk["choices"][0]
            delta = choice.get("delta") or choice.get("message") or {}
            content = delta.get("content", "")
            if isinstance(content, list):
                return "".join(
                    str(c.get("text", "")) if isinstance(c, dict) else str(c)
                    for c in content
                )
            return str(content) if content else ""
        for key in ("content", "text", "result", "output"):
            val = chunk.get(key)
            if val:
                if isinstance(val, str):
                    return val
                if isinstance(val, list):
                    return "".join(
                        str(c.get("text", "")) if isinstance(c, dict) else str(c)
                        for c in val
                    )
        return None

    @staticmethod
    def _extract_url_from_text(text: str) -> Optional[str]:
        """从文本中提取媒体 URL"""
        if not text:
            return None
        text = text.strip()

        if text.startswith(("http://", "https://")):
            return text.split()[0].rstrip('.,;!?)\'\"')

        patterns = [
            r'<(?:video|source|img)[^>]*src=["\']([^"\']+)["\']',
            r'(https?://[^\s<>"\')\]\\]+\.(?:mp4|webm|mov|avi|mkv|png|jpg|jpeg|gif|webp|bmp)(?:[?#][^\s<>"\')\]\\]*)?)',
            r'!\[[^\]]*\]\(([^)]+)\)',
            r'"url"\s*:\s*"(https?://[^"]+)"',
            r'(https?://[^\s<>"\')\]\\]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                url = match.group(1)
                if url.startswith(("http://", "https://")):
                    return url
        return None

    @staticmethod
    def _extract_base64_from_text(text: str) -> Optional[str]:
        """从文本中提取 Base64 数据"""
        if not text:
            return None

        # data URI 格式
        match = re.search(r'data:[^;]+;base64,([A-Za-z0-9+/=]+)', text)
        if match:
            return match.group(1)

        # 纯 base64 字符串（至少100字符，避免误判）
        match = re.search(r'([A-Za-z0-9+/]{100,}={0,2})', text)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_base64_from_data_uri(data_uri: str) -> Optional[str]:
        """从 data URI 中提取 Base64"""
        if "base64," in data_uri:
            return data_uri.split("base64,", 1)[1]
        return None

    @staticmethod
    def _is_base64(s: str) -> bool:
        """检查字符串是否为有效的 Base64"""
        if not s or len(s) < 100:
            return False
        try:
            if re.match(r'^[A-Za-z0-9+/]+={0,2}$', s):
                base64.b64decode(s[:100])
                return True
        except Exception:
            pass
        return False

    # ==================== 联网搜索辅助方法 ====================

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        """将配置值转换为布尔值"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return default

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        """将配置值转换为整数"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        """将配置值转换为浮点数"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _parse_json_config_dict(self, key: str) -> Dict[str, Any]:
        """解析 JSON 格式的配置项"""
        value = self.conf.get(key, {})
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return {}
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                logger.warning(f"配置项 {key} JSON 解析失败: {exc}")
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _extract_urls_from_text(text: str) -> List[str]:
        """从文本中提取 URL 列表"""
        urls = re.findall(r"https?://[^\s)\]}>\"']+", text or "")
        seen = set()
        result: List[str] = []
        for url in urls:
            cleaned = url.rstrip(".,;:!?'\"")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        return result

    @staticmethod
    def _coerce_search_json(text: str) -> Optional[Dict[str, Any]]:
        """尝试将搜索响应文本解析为 JSON 对象"""
        if not text:
            return None
        cleaned = text.strip()
        # 移除 Markdown 代码块包装
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        if not (cleaned.startswith("{") and cleaned.endswith("}")):
            return None
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _parse_search_message(self, message_content: Any) -> Tuple[str, List[Dict[str, str]], str]:
        """解析搜索响应消息，返回 (content, sources, raw)"""
        if isinstance(message_content, str):
            message_text = message_content.strip()
        elif isinstance(message_content, list):
            parts: List[str] = []
            for item in message_content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            message_text = "".join(parts).strip()
        else:
            message_text = str(message_content or "").strip()

        parsed = self._coerce_search_json(message_text)
        sources: List[Dict[str, str]] = []
        raw = ""

        if parsed is None:
            content = message_text
            raw = message_text
        else:
            content = str(parsed.get("content") or "").strip()
            source_list = parsed.get("sources")
            if isinstance(source_list, list):
                for item in source_list:
                    if isinstance(item, dict) and item.get("url"):
                        sources.append({
                            "url": str(item.get("url")),
                            "title": str(item.get("title") or ""),
                            "snippet": str(item.get("snippet") or ""),
                        })
            if not content:
                content = message_text

        if not sources:
            for url in self._extract_urls_from_text(content):
                sources.append({"url": url, "title": "", "snippet": ""})

        return content, sources, raw

    def _search_show_sources(self) -> bool:
        """是否显示搜索来源"""
        return self._to_bool(self.conf.get("grok_search_show_sources", False), False)

    def _search_max_sources(self) -> int:
        """最大显示来源数量"""
        value = self._to_int(self.conf.get("grok_search_max_sources", 5), 5)
        return 5 if value < 0 else value

    def _search_skill_enabled(self) -> bool:
        """是否启用 Skill 模式"""
        return self._to_bool(self.conf.get("grok_search_enable_skill", False), False)

    async def _perform_web_search(
        self,
        query: str,
        multimodal_inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """执行联网搜索/对话，支持图像、音频、文件理解"""
        started = time.time()
        query = (query or "").strip()
        multimodal_inputs = multimodal_inputs or {}
        image_bytes: Optional[bytes] = multimodal_inputs.get("image_bytes")
        audio_inputs: List[Dict[str, str]] = multimodal_inputs.get("audio_inputs", []) or []
        file_inputs: List[Dict[str, str]] = multimodal_inputs.get("file_inputs", []) or []

        if not query and not image_bytes and not audio_inputs and not file_inputs:
            return {
                "ok": False,
                "error": "请输入问题内容",
                "content": "",
                "sources": [],
                "raw": "",
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        if query and len(query) > self.MAX_PROMPT_LENGTH:
            return {
                "ok": False,
                "error": f"输入内容过长，最大支持 {self.MAX_PROMPT_LENGTH} 字符",
                "content": "",
                "sources": [],
                "raw": "",
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        api_key = str(self.conf.get("grok_api_key", "")).strip()
        if not api_key:
            return {
                "ok": False,
                "error": "未配置 API 密钥",
                "content": "",
                "sources": [],
                "raw": "",
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        configured_model = str(
            self.conf.get("grok_search_model", self.DEFAULT_SEARCH_MODEL)
        ).strip() or self.DEFAULT_SEARCH_MODEL
        model = await self._resolve_model(
            configured_model=configured_model,
            fallback_models=[self.DEFAULT_SEARCH_MODEL, "grok-4", "grok-3"],
            scene="对话/搜索",
        )
        timeout = self._to_float(self.conf.get("grok_search_timeout_seconds", self.DEFAULT_SEARCH_TIMEOUT), self.DEFAULT_SEARCH_TIMEOUT)
        if timeout <= 0:
            timeout = self.DEFAULT_SEARCH_TIMEOUT

        enable_thinking = self._to_bool(self.conf.get("grok_search_enable_thinking", True), True)
        thinking_budget = self._to_int(self.conf.get("grok_search_thinking_budget", self.DEFAULT_SEARCH_THINKING_BUDGET), self.DEFAULT_SEARCH_THINKING_BUDGET)
        if thinking_budget < 0:
            thinking_budget = self.DEFAULT_SEARCH_THINKING_BUDGET

        # 搜索模式: auto/on/off
        search_mode = str(self.conf.get("grok_search_mode", "auto")).strip().lower()
        if search_mode not in {"auto", "on", "off"}:
            search_mode = "auto"

        # 根据搜索模式设置 system prompt 和 search_parameters
        if search_mode == "off":
            # 纯对话模式，不联网
            system_prompt = (
                "You are a helpful assistant. "
                "IMPORTANT: Do NOT use Markdown formatting - respond in plain text only."
            )
            search_parameters = None
        elif search_mode == "on":
            # 始终联网搜索
            system_prompt = (
                "You are a web research assistant. Use live web search/browsing when answering. "
                "Return ONLY a single JSON object with keys: "
                "content (string), sources (array of objects with url/title/snippet when possible). "
                "Keep content concise and evidence-backed. "
                "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
            )
            search_parameters = {"mode": "on"}
        else:
            # auto 模式，由模型自动判断
            system_prompt = (
                "You are a helpful assistant with web search capabilities. "
                "If the user's question requires up-to-date information, current events, or facts you're unsure about, "
                "use web search to find accurate information. "
                "When you do search, return a JSON object with keys: "
                "content (string), sources (array of objects with url/title/snippet when possible). "
                "For general questions that don't need web search, respond normally. "
                "IMPORTANT: Do NOT use Markdown formatting - respond in plain text only."
            )
            search_parameters = {"mode": "auto"}

        has_multimodal = bool(image_bytes or audio_inputs or file_inputs)
        if has_multimodal:
            user_content: List[Dict[str, Any]] = [
                {"type": "text", "text": query or "请分析我发送的内容"}
            ]
        else:
            user_content = []

        # 构建用户消息（支持多模态）
        if image_bytes and has_multimodal:
            mime_type = self._detect_mime_type(image_bytes)
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            user_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
            )

        if has_multimodal:
            for audio in audio_inputs:
                audio_data = str(audio.get("data", "")).strip()
                audio_format = str(audio.get("format", "mp3")).strip() or "mp3"
                if not audio_data:
                    continue
                user_content.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": audio_data, "format": audio_format},
                    }
                )

            for file_item in file_inputs:
                file_payload: Dict[str, str] = {}
                file_url = str(file_item.get("url", "")).strip()
                file_data = str(file_item.get("data", "")).strip()
                if file_url:
                    file_payload["file_url"] = file_url
                elif file_data:
                    file_payload["file_data"] = file_data
                else:
                    continue

                filename = str(file_item.get("filename", "")).strip()
                if filename:
                    file_payload["filename"] = filename
                user_content.append({"type": "file", "file": file_payload})

        if not has_multimodal:
            user_content = query

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "stream": False,
        }

        # 添加 search_parameters（如果需要联网）
        if search_parameters:
            payload["search_parameters"] = search_parameters

        if enable_thinking:
            payload["reasoning_effort"] = "high"
            if thinking_budget > 0:
                payload["reasoning_budget_tokens"] = thinking_budget

        extra_body = self._parse_json_config_dict("grok_search_extra_body")
        for key, value in extra_body.items():
            if key not in {"model", "messages", "stream"}:
                payload[key] = value

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        extra_headers = self._parse_json_config_dict("grok_search_extra_headers")
        for key, value in extra_headers.items():
            if str(key).lower() not in {"authorization", "content-type"}:
                headers[str(key)] = str(value)

        api_url = f"{self._get_base_url()}/v1/chat/completions"
        raw_text = ""

        for attempt in range(self.MAX_REQUEST_RETRIES):
            try:
                session = await self._ensure_session()
                self._debug_log_request(
                    "对话/搜索",
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
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    raw_text = await resp.text()
                    if resp.status != 200:
                        self._log_error_response("对话/搜索", resp.status, raw_text)
                        if (
                            self._is_retryable_status(resp.status)
                            and attempt < self.MAX_REQUEST_RETRIES - 1
                        ):
                            await asyncio.sleep(self._retry_delay_seconds(attempt))
                            continue
                        return {
                            "ok": False,
                            "error": self._translate_error(f"状态码: {resp.status}"),
                            "content": "",
                            "sources": [],
                            "raw": raw_text[:2000] if raw_text else "",
                            "elapsed_ms": int((time.time() - started) * 1000),
                        }

                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    logger.error(f"[对话/搜索] 响应解析失败，完整响应: {raw_text}")
                    return {
                        "ok": False,
                        "error": "响应解析失败，API 返回了非 JSON 格式的数据",
                        "content": "",
                        "sources": [],
                        "raw": raw_text[:2000] if raw_text else "",
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                if "error" in data and isinstance(data.get("error"), (dict, str)):
                    error_info = data["error"]
                    error_msg = (
                        error_info.get("message", str(error_info))
                        if isinstance(error_info, dict)
                        else str(error_info)
                    )
                    logger.error(f"[对话/搜索] API 返回 error 字段，完整响应: {raw_text}")
                    return {
                        "ok": False,
                        "error": self._translate_error(error_msg),
                        "content": "",
                        "sources": [],
                        "raw": raw_text[:2000] if raw_text else "",
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                choices = data.get("choices")
                if not choices or not isinstance(choices, list):
                    logger.error(f"[对话/搜索] 响应缺少 choices 字段，完整响应: {raw_text}")
                    return {
                        "ok": False,
                        "error": "响应缺少 choices 字段",
                        "content": "",
                        "sources": [],
                        "raw": raw_text[:2000] if raw_text else "",
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                choice = choices[0] if isinstance(choices[0], dict) else {}
                message = choice.get("message") if isinstance(choice, dict) else {}
                content, sources, raw = self._parse_search_message((message or {}).get("content"))

                if not content:
                    logger.error(f"[对话/搜索] API 返回空响应，完整响应: {raw_text}")
                    return {
                        "ok": False,
                        "error": "API 返回了空响应",
                        "content": "",
                        "sources": [],
                        "raw": raw_text[:2000] if raw_text else "",
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                return {
                    "ok": True,
                    "content": content,
                    "sources": sources,
                    "raw": raw,
                    "model": data.get("model") or model,
                    "usage": data.get("usage") or {},
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                if attempt < self.MAX_REQUEST_RETRIES - 1:
                    await asyncio.sleep(self._retry_delay_seconds(attempt))
                    continue
                return {
                    "ok": False,
                    "error": self._translate_error(str(exc) or "请求超时，请稍后重试"),
                    "content": "",
                    "sources": [],
                    "raw": "",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            except Exception as exc:
                if attempt < self.MAX_REQUEST_RETRIES - 1:
                    await asyncio.sleep(self._retry_delay_seconds(attempt))
                    continue
                logger.error(f"[联网搜索] 请求异常: {exc}")
                return {
                    "ok": False,
                    "error": self._translate_error(str(exc)),
                    "content": "",
                    "sources": [],
                    "raw": "",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

        return {
            "ok": False,
            "error": "请求失败，请稍后重试",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    def _format_search_result(self, result: Dict[str, Any]) -> str:
        """格式化搜索结果为用户友好的消息（纯文本，无 Markdown）"""
        if not result.get("ok"):
            error = result.get("error", "未知错误")
            raw = result.get("raw", "")
            # 尝试从 raw 中提取更具体的错误信息
            if raw:
                try:
                    error_data = json.loads(raw)
                    if isinstance(error_data.get("error"), dict):
                        error = error_data["error"].get("message", error)
                    elif isinstance(error_data.get("error"), str):
                        error = error_data["error"]
                except (json.JSONDecodeError, KeyError):
                    pass
            return f"❌ 请求失败: {error}"

        content = self._strip_markdown(str(result.get("content", "")))
        sources = result.get("sources", [])
        if not isinstance(sources, list):
            sources = []

        lines = [content]
        if self._search_show_sources() and sources:
            max_sources = self._search_max_sources()
            selected = sources[:max_sources] if max_sources > 0 else sources
            lines.append("\n来源:")
            for i, src in enumerate(selected, 1):
                url = src.get("url", "")
                title = src.get("title", "")
                if title:
                    lines.append(f"  {i}. {title}\n     {url}")
                else:
                    lines.append(f"  {i}. {url}")

        return "\n".join(lines)

    def _format_search_result_for_llm(self, result: Dict[str, Any]) -> str:
        """格式化搜索结果供 LLM 使用"""
        if not result.get("ok"):
            error = result.get("error", "未知错误")
            raw = result.get("raw", "")
            return f"搜索失败: {error}\n{raw}" if raw else f"搜索失败: {error}"

        content = str(result.get("content", ""))
        sources = result.get("sources", [])
        if not isinstance(sources, list):
            sources = []
        lines = [f"搜索结果:\n{content}"]

        if self._search_show_sources() and sources:
            max_sources = self._search_max_sources()
            selected = sources[:max_sources] if max_sources > 0 else sources
            lines.append("\n参考来源:")
            for i, src in enumerate(selected, 1):
                url = src.get("url", "")
                title = src.get("title", "")
                snippet = src.get("snippet", "")
                if title:
                    lines.append(f"  {i}. {title}")
                    lines.append(f"     {url}")
                else:
                    lines.append(f"  {i}. {url}")
                if snippet:
                    lines.append(f"     {snippet}")

        return "\n".join(lines)

    # ==================== 媒体处理 ====================

    async def _download_media(self, url: str) -> Optional[bytes]:
        try:
            session = await self._ensure_session()
            should_log_request = url.startswith(("http://", "https://"))
            if should_log_request:
                self._debug_log_request("媒体下载", "GET", url)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status < 200 or resp.status >= 300:
                    text = await resp.text()
                    self._log_error_response("媒体下载", resp.status, text)
                    return None
                return await resp.read()
        except Exception as e:
            logger.error(f"媒体下载失败: {e}")
            return None

    async def _load_bytes(self, src: str) -> Optional[bytes]:
        if Path(src).is_file():
            try:
                async with aiofiles.open(src, 'rb') as f:
                    return await f.read()
            except Exception as e:
                logger.debug(f"读取本地文件失败 ({src[:50]}): {e}")
                return None
        elif src.startswith("http"):
            return await self._download_media(src)
        elif src.startswith("base64://"):
            try:
                return base64.b64decode(src[9:])
            except Exception as e:
                logger.debug(f"Base64解码失败: {e}")
                return None
        return None

    def _iter_event_segments(self, event: AstrMessageEvent) -> List[Any]:
        """展开消息链与引用链，返回统一的消息段列表"""
        message_list = getattr(getattr(event, "message_obj", None), "message", None) or []
        segments: List[Any] = []
        for seg in message_list:
            if self._is_segment_type(seg, "Reply") and getattr(seg, "chain", None):
                for inner in seg.chain:
                    segments.append(inner)
            else:
                segments.append(seg)
        return segments

    async def _load_segment_payload(self, seg: Any) -> Tuple[Optional[bytes], Optional[str]]:
        """从消息段中读取媒体数据，返回 (bytes, source)"""
        direct_data = getattr(seg, "data", None)
        if isinstance(direct_data, (bytes, bytearray)) and direct_data:
            return bytes(direct_data), None

        for src in self._extract_segment_sources(seg):
            payload = await self._load_bytes(src)
            if payload:
                return payload, src
        return None, None

    async def _get_images_from_event(
        self,
        event: AstrMessageEvent,
        max_count: Optional[int] = 1,
    ) -> List[bytes]:
        images: List[bytes] = []
        if max_count is not None and max_count <= 0:
            return images

        for seg in self._iter_event_segments(event):
            if not self._is_segment_type(seg, "Image"):
                continue
            payload, _ = await self._load_segment_payload(seg)
            if payload:
                images.append(payload)
                if max_count is not None and len(images) >= max_count:
                    break
        return images

    async def _get_image_from_event(self, event: AstrMessageEvent) -> Optional[bytes]:
        images = await self._get_images_from_event(event, max_count=1)
        if images:
            return images[0]
        return None

    async def _load_video_segment_payload(self, seg: Any) -> Tuple[Optional[bytes], Optional[str]]:
        """从消息段中读取视频数据，返回 (bytes, source)"""
        direct_data = getattr(seg, "data", None)
        if isinstance(direct_data, (bytes, bytearray)) and direct_data:
            return bytes(direct_data), None

        for src in self._extract_segment_sources(seg):
            if self._is_mp4_url(src):
                return None, src

            payload = await self._load_bytes(src)
            if payload:
                return payload, src
        return None, None

    async def _get_video_from_event(self, event: AstrMessageEvent) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """从事件中提取一个视频输入对象与来源说明。"""
        for seg in self._iter_event_segments(event):
            if not (self._is_segment_type(seg, "Video") or self._is_segment_type(seg, "File")):
                continue
            payload, source = await self._load_video_segment_payload(seg)
            if source and self._is_mp4_url(source):
                return {"url": source}, source
            if payload:
                return {"url": self._build_video_data_url(payload)}, source
        return None, None

    async def _collect_multimodal_inputs(self, event: AstrMessageEvent) -> Dict[str, Any]:
        """收集对话命令中的多模态输入（图像/音频/文件）"""
        images = await self._get_images_from_event(event, max_count=1)
        image_bytes = images[0] if images else None

        audio_inputs: List[Dict[str, str]] = []
        file_inputs: List[Dict[str, str]] = []

        for seg in self._iter_event_segments(event):
            is_audio = (
                self._is_segment_type(seg, "Record")
                or self._is_segment_type(seg, "Audio")
                or self._is_segment_type(seg, "Voice")
            )
            is_file = self._is_segment_type(seg, "File")

            if not is_audio and not is_file:
                continue

            sources = self._extract_segment_sources(seg)
            payload, source = await self._load_segment_payload(seg)
            if not source and sources:
                source = sources[0]
            if not payload:
                if is_file:
                    file_url = next((s for s in sources if s.startswith("http")), "")
                    if file_url:
                        file_inputs.append(
                            {
                                "filename": self._guess_filename_from_source(file_url, "upload.bin"),
                                "url": file_url,
                            }
                        )
                continue

            if is_audio:
                audio_inputs.append(
                    {
                        "format": self._guess_audio_format_from_source(source),
                        "data": base64.b64encode(payload).decode("utf-8"),
                    }
                )
                continue

            filename = self._guess_filename_from_source(source, "upload.bin")
            mime_type = self._guess_mime_type_from_source(source, "application/octet-stream")
            file_inputs.append(
                {
                    "filename": filename,
                    "data": f"data:{mime_type};base64,{base64.b64encode(payload).decode('utf-8')}",
                }
            )

        return {
            "image_bytes": image_bytes,
            "audio_inputs": audio_inputs,
            "file_inputs": file_inputs,
        }

    async def _save_and_send_media(self, event: AstrMessageEvent, url: str,
                                    media_bytes: bytes, media_type: str = "image"):
        save_media = self._is_save_media_enabled()
        if media_type == "video":
            ext = "mp4"
        else:
            mime_type = self._detect_mime_type(media_bytes)
            ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}
            ext = ext_map.get(mime_type, "png")
            image_size = self._get_image_resolution(media_bytes)
            if image_size:
                logger.info(f"[{media_type}] 保存前分辨率: {image_size[0]}x{image_size[1]}, mime={mime_type}")
        filename = f"grok_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"

        save_dir = self.video_dir if save_media and media_type == "video" else (
            self.image_dir if save_media else self.temp_dir
        )

        file_path = (save_dir / filename).resolve()

        try:
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(media_bytes)
            logger.info(f"[{media_type}] 媒体已写入: {file_path}")

            if media_type == "video":
                component = Comp.Video.fromFileSystem(path=str(file_path), name=filename)
            else:
                component = Comp.Image.fromFileSystem(path=str(file_path))

            yield event.chain_result([component])

        except Exception as e:
            logger.error(f"媒体处理失败: {e}")
            yield event.plain_result("❌ 发送失败，请到后台查看")
        finally:
            if not save_media:
                try:
                    await aiofiles.os.remove(file_path)
                except Exception:
                    pass

    async def _send_video_result(self, event: AstrMessageEvent, video_result: str):
        save_media = self._is_save_media_enabled()

        if Path(video_result).is_file():
            try:
                if save_media:
                    source_path = Path(video_result).resolve()
                    save_path = (self.video_dir / source_path.name).resolve()
                    if source_path.parent == self.video_dir.resolve():
                        save_path = source_path
                    elif not save_path.exists() or not save_path.samefile(source_path):
                        async with aiofiles.open(source_path, 'rb') as src:
                            content = await src.read()
                        async with aiofiles.open(save_path, 'wb') as dst:
                            await dst.write(content)
                        if source_path.exists() and source_path != save_path:
                            await aiofiles.os.remove(source_path)
                    filename = save_path.name
                    logger.info(f"[video] 媒体已保存: {save_path}")
                    component = Comp.Video.fromFileSystem(path=str(save_path), name=filename)
                else:
                    component = Comp.Video.fromFileSystem(
                        path=video_result,
                        name=Path(video_result).name,
                    )
                yield event.chain_result([component])
            except Exception as e:
                logger.error(f"视频发送失败: {e}")
                yield event.plain_result(f"❌ 视频发送失败: {self._translate_error(str(e))}")
            finally:
                if not save_media:
                    try:
                        await aiofiles.os.remove(video_result)
                    except Exception:
                        pass
            return

        video_bytes = await self._download_media(video_result)
        if video_bytes:
            async for result in self._save_and_send_media(event, video_result, video_bytes, "video"):
                yield result
        else:
            yield event.plain_result("❌ 视频下载失败，请到后台查看")

    async def _send_images_forward(self, event: AstrMessageEvent,
                                    images_data: List[Tuple[str, bytes]], failed_count: int = 0):
        """使用合并转发发送多张图片"""
        saved_files = []
        nodes = []
        save_media = self._is_save_media_enabled()
        save_dir = self.image_dir if save_media else self.temp_dir

        try:
            self_id = event.get_self_id()
            try:
                self_id_int = int(self_id)
            except (ValueError, TypeError):
                self_id_int = 10000

            for i, (url, img_bytes) in enumerate(images_data):
                mime_type = self._detect_mime_type(img_bytes)
                ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}
                ext = ext_map.get(mime_type, "png")
                image_size = self._get_image_resolution(img_bytes)
                if image_size:
                    logger.info(f"[image] 合并转发前分辨率: {image_size[0]}x{image_size[1]}, mime={mime_type}")
                filename = f"grok_{int(time.time())}_{uuid.uuid4().hex[:8]}_{i}.{ext}"
                file_path = (save_dir / filename).resolve()

                async with aiofiles.open(file_path, 'wb') as f:
                    await f.write(img_bytes)
                saved_files.append((file_path, save_media))

                nodes.append(
                    Comp.Node(
                        name="Grok",
                        uin=self_id_int,
                        content=[Comp.Image.fromFileSystem(path=str(file_path))]
                    )
                )

            # 如果有失败的图片，添加提示节点
            if failed_count > 0:
                nodes.append(
                    Comp.Node(
                        name="Grok",
                        uin=self_id_int,
                        content=[Comp.Plain(f"⚠️ {failed_count}张图片下载失败，请到后台查看")]
                    )
                )

            yield event.chain_result([Comp.Nodes(nodes)])

        except Exception as e:
            logger.error(f"合并转发发送失败: {e}")
            for i, (url, img_bytes) in enumerate(images_data):
                async for result in self._save_and_send_media(event, url, img_bytes, "image"):
                    yield result
        finally:
            for file_path, should_keep in saved_files:
                if not should_keep:
                    try:
                        await aiofiles.os.remove(file_path)
                    except Exception:
                        pass

    # ==================== 权限检查 ====================

    async def _check_permissions(self, event: AstrMessageEvent) -> Tuple[bool, Optional[str]]:
        group_blacklist = self.conf.get("group_blacklist", [])
        if hasattr(event, 'get_group_id') and group_blacklist:
            try:
                group_id = event.get_group_id()
                if group_id and group_id in group_blacklist:
                    return False, None
            except Exception as e:
                logger.debug(f"获取群组ID失败: {e}")

        group_whitelist = self.conf.get("group_whitelist", [])
        if hasattr(event, 'get_group_id') and group_whitelist:
            try:
                group_id = event.get_group_id()
                if group_id and group_id not in group_whitelist:
                    return False, None
            except Exception as e:
                logger.debug(f"获取群组ID失败: {e}")

        user_blacklist = self.conf.get("user_blacklist", [])
        if event.get_sender_id() in user_blacklist:
            return False, None

        user_whitelist = self.conf.get("user_whitelist", [])
        if user_whitelist and event.get_sender_id() not in user_whitelist:
            return False, None

        return True, None

    # ==================== 参数解析 ====================

    def _parse_image_params(self, text: str, strict_size: bool = True) -> Tuple[str, Dict[str, Any]]:
        """解析生图参数: [数量] [尺寸] 提示词（顺序任意）

        规则：
        - 只识别开头连续的独立参数词
        - 数量: 1-10 的独立数字
        - 尺寸: WxH（必须是 SUPPORTED_IMAGE_SIZES 中的合法尺寸）
        - 遇到非参数词立即停止，后续全部作为提示词
        - 每种参数最多识别一次
        """
        params = {
            "n": 1,
            "size": self.DEFAULT_TEXT_IMAGE_SIZE,
            "size_explicit": False,
            "invalid_size": None,
        }
        parts = text.split()
        if not parts:
            return "", params

        prompt_start = 0
        found_n = False
        found_size = False

        # 最多检查前2个词（数量+尺寸，顺序任意）
        for i in range(min(2, len(parts))):
            p = parts[i]

            # 检查是否为数量(1-10的独立数字)
            if not found_n and p.isdigit() and 1 <= int(p) <= self.MAX_IMAGE_COUNT:
                params["n"] = int(p)
                prompt_start = i + 1
                found_n = True
            # 检查是否为尺寸
            elif not found_size:
                normalized = self._normalize_image_size_or_ratio(p)
                if normalized:
                    params["size"] = normalized
                    params["size_explicit"] = True
                    prompt_start = i + 1
                    found_size = True
                    continue

                parsed_size = self._parse_size_string(p)
                if parsed_size:
                    parsed_value = self._format_size(parsed_size[0], parsed_size[1])
                    if strict_size:
                        params["invalid_size"] = parsed_value
                    else:
                        params["size"] = parsed_value
                    params["size_explicit"] = True
                    prompt_start = i + 1
                    found_size = True
                    continue
                break
            else:
                # 遇到非参数词，停止解析
                break

        prompt = " ".join(parts[prompt_start:]).strip()
        return prompt, params

    def _parse_video_params(self, text: str, strict_size: bool = True) -> Tuple[str, Dict[str, Any]]:
        """解析生视频参数: [比例/尺寸] [时长] [分辨率] 提示词（顺序任意）"""
        params = {
            "size": self._get_configured_video_aspect_ratio(),
            "size_explicit": False,
            "invalid_size": None,
            "duration_seconds": self._get_configured_video_duration(),
            "invalid_duration": None,
            "resolution": None,
            "invalid_resolution": None,
        }
        parts = text.split()
        if not parts:
            return "", params

        prompt_start = 0
        found_size = False
        found_duration = False
        found_resolution = False

        # 最多识别前3个词（比例/尺寸+时长+分辨率，顺序任意）
        for i in range(min(3, len(parts))):
            p = parts[i]

            if not found_size:
                if self._is_video_aspect_ratio_null_token(p):
                    params["size"] = None
                    params["size_explicit"] = True
                    prompt_start = i + 1
                    found_size = True
                    continue
                normalized = self._normalize_video_aspect_ratio(p)
                if normalized:
                    params["size"] = normalized
                    params["size_explicit"] = True
                    prompt_start = i + 1
                    found_size = True
                    continue
                if self._is_video_aspect_ratio_like_token(p):
                    params["invalid_size"] = p
                    params["size_explicit"] = True
                    prompt_start = i + 1
                    found_size = True
                    continue

            if not found_duration:
                parsed_duration = self._parse_video_length_token(p)
                if parsed_duration:
                    params["duration_seconds"] = parsed_duration
                    prompt_start = i + 1
                    found_duration = True
                    continue
                if self._is_video_length_like_token(p):
                    params["invalid_duration"] = p
                    prompt_start = i + 1
                    found_duration = True
                    continue

            if not found_resolution:
                normalized_resolution = self._normalize_video_resolution(p)
                if normalized_resolution:
                    params["resolution"] = normalized_resolution
                    prompt_start = i + 1
                    found_resolution = True
                    continue
                if self._is_video_resolution_like_token(p):
                    params["invalid_resolution"] = p
                    prompt_start = i + 1
                    found_resolution = True
                    continue

            if not found_size:
                parsed_size = self._parse_size_string(p)
                if parsed_size and strict_size:
                    params["invalid_size"] = self._format_size(parsed_size[0], parsed_size[1])
                    params["size_explicit"] = True
                    prompt_start = i + 1
                    found_size = True
                    continue

            break

        prompt = " ".join(parts[prompt_start:]).strip()
        return prompt, params

    def _parse_video_extension_params(self, text: str) -> Tuple[str, Dict[str, Any]]:
        """解析视频扩展参数: [时长] 提示词"""
        params = {
            "duration_seconds": self._get_configured_video_extension_duration(),
            "invalid_duration": None,
        }
        parts = text.split()
        if not parts:
            return "", params

        prompt_start = 0
        first = parts[0]
        parsed_duration = self._parse_video_extension_duration_token(first)
        if parsed_duration is not None:
            params["duration_seconds"] = parsed_duration
            prompt_start = 1
        elif self._is_video_length_like_token(first):
            params["invalid_duration"] = first
            prompt_start = 1

        prompt = " ".join(parts[prompt_start:]).strip()
        return prompt, params

    # ==================== 命令 ====================

    @filter.command("grok生图", prefix_optional=True)
    async def on_image_request(self, event: AstrMessageEvent):
        """Grok 生图: /grok生图 [数量] [尺寸] <提示词> [+图片可选]"""
        api_key = self._get_api_key("image")
        if not api_key:
            yield event.plain_result("❌ 未配置生图 API 密钥")
            return

        # 从 message_str 中移除命令前缀
        raw_input = event.message_str.strip()
        cmd = "grok生图"
        if raw_input.startswith(cmd):
            user_input = raw_input[len(cmd):].strip()
        else:
            user_input = raw_input

        if not user_input:
            yield event.plain_result("❌ 请输入提示词\n示例: /grok生图 一只可爱的猫咪")
            return

        can_proceed, _ = await self._check_permissions(event)
        if not can_proceed:
            yield event.plain_result("❌ 当前会话无权限使用此功能")
            return

        image_inputs = await self._get_images_from_event(
            event,
            max_count=self.MAX_EDIT_REFERENCE_IMAGES,
        )
        image_bytes = image_inputs[0] if image_inputs else None
        reference_images = image_inputs[1:] if len(image_inputs) > 1 else []
        mode = "图生图" if image_bytes else "文生图"

        prompt_text, params = self._parse_image_params(user_input, strict_size=not image_bytes)
        if not prompt_text:
            yield event.plain_result("❌ 请输入提示词")
            return

        if len(prompt_text) > self.MAX_PROMPT_LENGTH:
            yield event.plain_result(f"❌ 提示词过长，最大支持 {self.MAX_PROMPT_LENGTH} 字符")
            return

        n = params["n"]
        requested_size = params["size"]
        size_explicit = bool(params.get("size_explicit"))
        invalid_size = params.get("invalid_size")

        if not image_bytes and invalid_size:
            supported_ratios = "、".join(self.SUPPORTED_IMAGE_ASPECT_RATIOS)
            yield event.plain_result(
                f"❌ 不支持的尺寸: {invalid_size}\n支持比例: {supported_ratios}"
            )
            return

        target_size = None
        if image_bytes:
            if size_explicit:
                target_size = requested_size
            else:
                if self._get_configured_image_backend_type() == "grok2api":
                    source_resolution = self._get_image_resolution(image_bytes)
                    if source_resolution:
                        target_size = self._get_closest_supported_size(*source_resolution)
                        logger.info(
                            "[图生图][grok2api] 未显式指定尺寸，"
                            f"按当前图片比例映射尺寸 "
                            f"{self._format_size(*source_resolution)} -> {target_size}"
                        )
                else:
                    source_resolution = self._get_image_resolution(image_bytes)
                    if source_resolution:
                        target_size = self._get_closest_supported_size(*source_resolution)
        else:
            target_size = requested_size

        if not target_size:
            target_size = self.DEFAULT_TEXT_IMAGE_SIZE

        aspect_ratio_display = self._get_aspect_ratio_display(target_size)
        yield event.plain_result(f"🎨 正在进行 [{mode}] · {n}张 · {aspect_ratio_display} ...")

        results, error = await self._generate_image(
            prompt_text,
            image_bytes,
            reference_images=reference_images,
            n=n,
            target_size=target_size,
        )

        if error:
            yield event.plain_result(f"❌ [{mode}] 生成失败: {self._translate_error(error)}")
            return

        if not results:
            yield event.plain_result("❌ 未获取到图片")
            return

        # 处理所有图片（URL 需下载，bytes 直接使用）
        images_data = []
        failed_count = 0
        for i, (url_or_path, img_bytes) in enumerate(results):
            if img_bytes:
                images_data.append((url_or_path or f"image_{i}", img_bytes))
            elif url_or_path:
                downloaded = await self._download_media(url_or_path)
                if downloaded:
                    images_data.append((url_or_path, downloaded))
                else:
                    failed_count += 1

        if not images_data:
            yield event.plain_result("❌ 图片下载失败，请到后台查看")
            return

        # 单张图片直接发送，多张使用合并转发
        if len(images_data) == 1:
            async for result in self._save_and_send_media(event, images_data[0][0], images_data[0][1], "image"):
                yield result
            # 单张图片时，如果有失败的，单独提示
            if failed_count > 0:
                yield event.plain_result(f"⚠️ {failed_count}张图片下载失败，请到后台查看")
        else:
            async for result in self._send_images_forward(event, images_data, failed_count):
                yield result

    @filter.command("grok视频", prefix_optional=True)
    async def on_video_request(self, event: AstrMessageEvent):
        """Grok 生视频: /grok视频 [比例/尺寸] [时长] [分辨率] <提示词> [+图片可选]"""
        api_key = self._get_api_key("video")
        if not api_key:
            yield event.plain_result("❌ 未配置视频 API 密钥")
            return

        # 从 message_str 中移除命令前缀
        raw_input = event.message_str.strip()
        normalized_input = raw_input.lstrip("/")
        if normalized_input.startswith(("grok视频编辑", "grok视频扩展")):
            return
        cmd = "grok视频"
        if normalized_input.startswith(cmd):
            user_input = normalized_input[len(cmd):].strip()
        else:
            user_input = raw_input

        can_proceed, _ = await self._check_permissions(event)
        if not can_proceed:
            yield event.plain_result("❌ 当前会话无权限使用此功能")
            return

        reference_mode_requested, user_input = self._extract_video_reference_mode(user_input)
        inline_file_ids, user_input = self._extract_inline_file_ids(user_input)
        inline_image_urls, user_input = self._extract_inline_image_urls(user_input)

        video_images = await self._get_images_from_event(event, max_count=None)
        image_inputs: List[Dict[str, str]] = [
            {"file_id": file_id} for file_id in inline_file_ids
        ]
        image_inputs.extend({"url": image_url} for image_url in inline_image_urls)
        image_inputs.extend(self._build_video_image_object(image_bytes) for image_bytes in video_images)

        if reference_mode_requested:
            if not image_inputs:
                yield event.plain_result("❌ 参考图模式需要至少一张图片")
                return
            mode = "参考图生视频"
            image_input = None
            reference_images = image_inputs
        elif len(image_inputs) > 1:
            mode = "参考图生视频"
            image_input = None
            reference_images = image_inputs
        elif image_inputs:
            mode = "图生视频"
            image_input = image_inputs[0]
            reference_images = []
        else:
            mode = "文生视频"
            image_input = None
            reference_images = []

        if reference_images and len(reference_images) > self.MAX_VIDEO_REFERENCE_IMAGES:
            yield event.plain_result(
                f"❌ 参考图生视频最多支持 {self.MAX_VIDEO_REFERENCE_IMAGES} 张图片"
            )
            return

        prompt_text, params = self._parse_video_params(user_input, strict_size=True)

        if params.get("invalid_size"):
            supported_ratios = "、".join(self.SUPPORTED_VIDEO_ASPECT_RATIOS)
            yield event.plain_result(
                f"❌ 不支持的尺寸: {params['invalid_size']}\n支持比例: {supported_ratios}"
            )
            return
        if params.get("invalid_duration"):
            yield event.plain_result(
                f"❌ 不支持的视频时长: {params['invalid_duration']}\n"
                f"支持范围: {self.MIN_VIDEO_LENGTH_SECONDS}-{self.MAX_VIDEO_LENGTH_SECONDS} 秒"
            )
            return
        if params.get("invalid_resolution"):
            supported_resolutions = "、".join(self.SUPPORTED_VIDEO_RESOLUTIONS)
            yield event.plain_result(
                f"❌ 不支持的视频分辨率: {params['invalid_resolution']}\n"
                f"支持分辨率: {supported_resolutions}"
            )
            return

        if not prompt_text and not image_input and not reference_images:
            yield event.plain_result("❌ 请输入提示词\n示例: /grok视频 让画面动起来")
            return

        if not prompt_text and reference_images:
            yield event.plain_result("❌ 参考图生视频需要提示词")
            return

        if not prompt_text and image_input and not reference_images:
            prompt_text = ""
        elif not prompt_text:
            yield event.plain_result("❌ 请输入提示词")
            return

        if prompt_text and len(prompt_text) > self.MAX_PROMPT_LENGTH:
            yield event.plain_result(f"❌ 提示词过长，最大支持 {self.MAX_PROMPT_LENGTH} 字符")
            return

        video_length_seconds = int(params.get("duration_seconds", self._get_configured_video_duration()))
        if reference_images and video_length_seconds > self.MAX_REFERENCE_VIDEO_LENGTH_SECONDS:
            yield event.plain_result(
                f"❌ 参考图生视频时长仅支持 "
                f"{self.MIN_VIDEO_LENGTH_SECONDS}-{self.MAX_REFERENCE_VIDEO_LENGTH_SECONDS} 秒"
            )
            return

        size_explicit = bool(params.get("size_explicit"))
        if image_input and not reference_images and not size_explicit:
            video_target_size = None
        elif size_explicit:
            video_target_size = params["size"]
        else:
            video_target_size = params["size"]

        video_resolution = params.get("resolution") or self._get_configured_video_resolution()
        if video_target_size:
            aspect_ratio_display = self._get_aspect_ratio_display(video_target_size)
        elif size_explicit:
            aspect_ratio_display = "null"
        elif image_input and not reference_images:
            aspect_ratio_display = "原图比例"
        else:
            aspect_ratio_display = "不指定比例"
        yield event.plain_result(
            f"🎬 正在进行 [{mode}] · {video_length_seconds}秒 · {aspect_ratio_display} · {video_resolution} ..."
        )

        video_result, error = await self._generate_video(
            prompt_text,
            image_input,
            video_target_size,
            video_length=video_length_seconds,
            reference_images=reference_images,
            resolution=video_resolution,
            aspect_ratio_explicit=size_explicit,
            user=str(event.get_sender_id() or "").strip(),
        )

        if error:
            yield event.plain_result(f"❌ [{mode}] 生成失败: {self._translate_error(error)}")
            return

        if not video_result:
            yield event.plain_result("❌ 未获取到视频")
            return

        async for result in self._send_video_result(event, video_result):
            yield result

    @filter.command("grok视频编辑", prefix_optional=True)
    async def on_video_edit_request(self, event: AstrMessageEvent):
        """Grok 视频编辑: /grok视频编辑 [视频URL] <提示词> [+视频可选]"""
        api_key = self._get_api_key("video")
        if not api_key:
            yield event.plain_result("❌ 未配置视频 API 密钥")
            return

        raw_input = event.message_str.strip()
        cmd = "grok视频编辑"
        normalized_input = raw_input.lstrip("/")
        user_input = normalized_input[len(cmd):].strip() if normalized_input.startswith(cmd) else raw_input

        if not user_input:
            yield event.plain_result("❌ 请输入编辑提示词\n示例: /grok视频编辑 给人物添加银色项链 +视频")
            return

        can_proceed, _ = await self._check_permissions(event)
        if not can_proceed:
            yield event.plain_result("❌ 当前会话无权限使用此功能")
            return

        inline_file_id, remaining_input = self._extract_inline_file_id(user_input)
        inline_url, prompt_text = self._extract_first_url(remaining_input)
        if inline_file_id:
            video_input, source_label = {"file_id": inline_file_id}, inline_file_id
        elif inline_url:
            video_input, source_label = await self._resolve_inline_video_source(inline_url)
        else:
            video_input, source_label = await self._get_video_from_event(event)

        if not video_input:
            yield event.plain_result("❌ 请附带一个视频，或在命令中提供 mp4 视频 URL")
            return

        if not prompt_text:
            yield event.plain_result("❌ 请输入编辑提示词")
            return

        if len(prompt_text) > self.MAX_PROMPT_LENGTH:
            yield event.plain_result(f"❌ 提示词过长，最大支持 {self.MAX_PROMPT_LENGTH} 字符")
            return

        yield event.plain_result("🎬 正在进行 [视频编辑] ...")
        video_result, error = await self._generate_video_edit(
            prompt_text,
            video_input,
            source_label=source_label,
            user=str(event.get_sender_id() or "").strip(),
        )

        if error:
            yield event.plain_result(f"❌ [视频编辑] 失败: {self._translate_error(error)}")
            return

        if not video_result:
            yield event.plain_result("❌ 未获取到视频")
            return

        async for result in self._send_video_result(event, video_result):
            yield result

    @filter.command("grok视频扩展", prefix_optional=True)
    async def on_video_extension_request(self, event: AstrMessageEvent):
        """Grok 视频扩展: /grok视频扩展 [时长] [视频URL] <提示词> [+视频可选]"""
        api_key = self._get_api_key("video")
        if not api_key:
            yield event.plain_result("❌ 未配置视频 API 密钥")
            return

        raw_input = event.message_str.strip()
        cmd = "grok视频扩展"
        normalized_input = raw_input.lstrip("/")
        user_input = normalized_input[len(cmd):].strip() if normalized_input.startswith(cmd) else raw_input

        if not user_input:
            yield event.plain_result("❌ 请输入扩展提示词\n示例: /grok视频扩展 6 镜头继续向前推进 +视频")
            return

        can_proceed, _ = await self._check_permissions(event)
        if not can_proceed:
            yield event.plain_result("❌ 当前会话无权限使用此功能")
            return

        inline_file_id, remaining_input = self._extract_inline_file_id(user_input)
        inline_url, remaining_input = self._extract_first_url(remaining_input)
        prompt_text, params = self._parse_video_extension_params(remaining_input)

        if params.get("invalid_duration"):
            yield event.plain_result(
                f"❌ 不支持的视频扩展时长: {params['invalid_duration']}\n"
                f"支持范围: {self.MIN_VIDEO_EXTENSION_SECONDS}-{self.MAX_VIDEO_EXTENSION_SECONDS} 秒"
            )
            return

        if inline_file_id:
            video_input, source_label = {"file_id": inline_file_id}, inline_file_id
        elif inline_url:
            video_input, source_label = await self._resolve_inline_video_source(inline_url)
        else:
            video_input, source_label = await self._get_video_from_event(event)

        if not video_input:
            yield event.plain_result("❌ 请附带一个视频，或在命令中提供 mp4 视频 URL")
            return

        if not prompt_text:
            yield event.plain_result("❌ 请输入扩展提示词")
            return

        if len(prompt_text) > self.MAX_PROMPT_LENGTH:
            yield event.plain_result(f"❌ 提示词过长，最大支持 {self.MAX_PROMPT_LENGTH} 字符")
            return

        duration_seconds = int(params.get("duration_seconds", self.DEFAULT_VIDEO_EXTENSION_DURATION_SECONDS))
        yield event.plain_result(f"🎬 正在进行 [视频扩展] · {duration_seconds}秒 ...")
        video_result, error = await self._generate_video_extension(
            prompt_text,
            video_input,
            duration=duration_seconds,
            source_label=source_label,
        )

        if error:
            yield event.plain_result(f"❌ [视频扩展] 失败: {self._translate_error(error)}")
            return

        if not video_result:
            yield event.plain_result("❌ 未获取到视频")
            return

        async for result in self._send_video_result(event, video_result):
            yield result

    @filter.command("grok帮助", prefix_optional=True)
    async def on_help(self, event: AstrMessageEvent):
        help_text = (
            "【Grok AI 助手】\n\n"
            "🎨 生图命令:\n"
            "/grok生图 [数量] [比例] 提示词\n"
            "• 数量: 1-10 (默认1)\n"
            "• 比例: 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 等\n"
            "• 不传比例时默认 9:16 竖屏\n"
            "• 可附带图片进行图生图，默认按当前图片比例映射到后端支持尺寸\n"
            "• 最多读取3张图，多图会作为官方 images 参考输入\n\n"
            "示例:\n"
            "• /grok生图 一只猫\n"
            "• /grok生图 4 3:2 日落海滩\n"
            "• /grok生图 把背景换成森林 +图片\n\n"
            "━━━━━━━━━━━━━━\n"
            "🎬 视频命令:\n"
            "/grok视频 [比例|null] [时长] [分辨率] 提示词 [+图片可选]\n"
            "• 比例: 16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 3:2 / 2:3；输入 null 表示发送 JSON null\n"
            "• 文生视频/参考图视频默认读取 UI 配置；单图图生视频默认保持原图比例\n"
            "• 时长支持 1-15 秒，默认 8 秒，可在插件配置调整\n"
            "• 分辨率支持 480p / 720p / 1080p，默认读取插件配置\n"
            "• xAI 后端单张图片默认作为首帧图生视频，提示词可省略\n"
            "• grok2api 后端上传图片会走 input_reference[] 参考图语义\n"
            "• OpenAI 后端走 OpenAI SDK，生视频最多使用 1 个 input_reference\n"
            "• 多张图片会使用参考图视频模式；最多7张，最长10秒，提示词必填\n"
            "• 单张图可加“参考图”强制使用 reference_images 参考图模式\n"
            "• 图片输入支持附件、file_id:xxx 或 image_url:https://...\n\n"
            "示例:\n"
            "• /grok视频 让画面动起来\n"
            "• /grok视频 10 夜晚海边慢镜头\n"
            "• /grok视频 16:9 6 720p 让城市霓虹缓慢流动\n"
            "• /grok视频 让人物眨眼微笑 +图片\n\n"
            "🎞️ 视频编辑/扩展:\n"
            "/grok视频编辑 [视频URL] 提示词 [+视频可选]\n"
            "/grok视频扩展 [时长] [视频URL] 提示词 [+视频可选]\n"
            "• 编辑: 根据提示修改输入视频\n"
            "• 扩展: 生成后续片段，时长支持 1-10 秒\n"
            "• 视频输入支持 mp4 URL、file_id 或消息中的视频附件\n\n"
            "• 视频后端可在插件 UI 选择 xAI、grok2api 或 OpenAI；grok2api 当前仅支持生视频\n\n"
            "示例:\n"
            "• /grok视频编辑 给人物添加银色项链 +视频\n"
            "• /grok视频扩展 6 镜头继续向前推进 +视频\n\n"
            "━━━━━━━━━━━━━━\n"
            "💬 对话命令:\n"
            "/grok <内容> [+图片/语音/文件可选]\n"
            "• 智能对话，可自动联网获取最新信息\n"
            "• 支持附带图片、语音、文件进行多模态理解\n\n"
            "示例:\n"
            "• /grok 你好，介绍一下你自己\n"
            "• /grok 今天有什么新闻\n"
            "• /grok 这张图片里有什么 +图片\n"
            "• /grok 帮我总结这个语音和文件 +语音/+文件"
        )
        yield event.plain_result(help_text)

    @filter.command("grok", prefix_optional=True)
    async def on_web_search(self, event: AstrMessageEvent):
        """Grok 对话/搜索: /grok <内容> [+图片/语音/文件可选]"""
        raw_input = event.message_str.strip()
        normalized_input = raw_input.lstrip("/")
        # 避免与其他 grok 命令冲突
        if normalized_input.startswith(("grok生图", "grok视频", "grok帮助")):
            return

        can_proceed, _ = await self._check_permissions(event)
        if not can_proceed:
            yield event.plain_result("❌ 当前会话无权限使用此功能")
            return

        cmd = "grok"
        query = normalized_input[len(cmd):].strip() if normalized_input.startswith(cmd) else normalized_input

        multimodal_inputs = await self._collect_multimodal_inputs(event)
        has_multimodal = bool(
            multimodal_inputs.get("image_bytes")
            or multimodal_inputs.get("audio_inputs")
            or multimodal_inputs.get("file_inputs")
        )

        if not query and not has_multimodal:
            yield event.plain_result(
                "使用方法: /grok <问题内容> [+图片/语音/文件可选]\n输入 /grok帮助 查看完整说明"
            )
            return

        if query.lower() == "help" or query == "帮助":
            yield event.plain_result(
                "使用方法: /grok <问题内容> [+图片/语音/文件可选]\n输入 /grok帮助 查看完整说明"
            )
            return

        api_key = self.conf.get("grok_api_key", "").strip()
        if not api_key:
            yield event.plain_result("❌ 未配置 API 密钥")
            return

        result = await self._perform_web_search(query, multimodal_inputs)
        yield event.plain_result(self._format_search_result(result))

    @filter.llm_tool(name="grok_web_search")
    async def grok_web_search_tool(self, event: AstrMessageEvent, query: str) -> str:
        """通过 Grok 进行实时联网搜索，获取最新信息和来源

        当需要搜索实时信息、最新新闻、API 版本、错误解决方案或验证过时/不确定信息时使用。

        Args:
            query(string): 搜索查询内容，应该是清晰具体的问题或关键词
        """
        query = (query or "").strip()
        if not query:
            return "搜索失败: 查询不能为空"

        can_proceed, _ = await self._check_permissions(event)
        if not can_proceed:
            return "搜索失败: 当前会话没有权限使用该工具"

        result = await self._perform_web_search(query)
        return self._format_search_result_for_llm(result)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求时，如果启用了 Skill 则移除 grok_web_search 工具"""
        if not self._search_skill_enabled():
            return

        tool_set = getattr(req, "func_tool", None)
        if FunctionToolManager is not None and isinstance(tool_set, FunctionToolManager):
            req.func_tool = tool_set.get_full_tool_set()
            tool_set = req.func_tool

        if not tool_set:
            return
        if hasattr(tool_set, "remove_tool"):
            tool_set.remove_tool("grok_web_search")
            return
        if isinstance(tool_set, dict):
            tool_set.pop("grok_web_search", None)
