"""字幕优化模型 provider：在子序列约束下对 clip 窗口文本做语义删词与断句。

复用评审模型的 Chat Completions 形态与凭据配置（见 ADR 0011），但：
- 输出契约是「纯文本分块」（每块一行），不走 JSON（flash 类模型返回不合规 JSON 是已知痛点）；
- 子序列约束下只能删字、断句，不可增改字；解析得到的显示块再用 subtitle_align 两指针校验并对齐回逐字时间；
- 校验失败（非子序列）、HTTP/超时、无 API Key 时一律「失败」（返回 None / is_available False），交由导出侧规则兜底，不抛异常。
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from video_auto_editor.config import CONFIG
from video_auto_editor.subtitle_align import build_window, validate_and_align


PROMPT_VERSION = "stepfun_chat_subtitle_optimization_v2"
SUBTITLE_OPTIMIZATION_CACHE_SCHEMA_VERSION = "subtitle_optimization_cache_v1"

_SYSTEM_PROMPT = (
    "你的任务：从下面给出的口语转写原文里【删除】一些字符，得到适合短视频画面的字幕。\n"
    "你只做删除，不做任何别的事。把它想成用橡皮擦擦掉原文里的某些字，剩下的字保持原样。\n"
    "\n"
    "绝对规则（违反任何一条都算彻底失败）：\n"
    "1. 输出里出现的每一个字符，都必须原样、按原顺序来自原文。保留的字要逐字照抄，"
    "不能改写、不能换同义词、不能调整语序、不能补全。\n"
    "2. 禁止新增任何原文中没有的字符——包括汉字、标点、数字，尤其【禁止空格】。"
    "原文没有空格，你的输出里也不能有空格。\n"
    "3. 你能做的只有两件事：(a) 删掉语气词（嗯啊呃哦等）、口吃重复、无意义口头禅和填充词；"
    "(b) 通过【换行】把保留下来的字切成一个个显示块。保留所有承载语义的字，不要删成残句。\n"
    "\n"
    "输出格式：每个显示块占一行，用换行分隔。块内不得有空格。"
    "只输出这些显示块，不要编号、不要解释、不要引号、不要 Markdown。\n"
    "\n"
    "示例：\n"
    "输入：嗯，那个我们呢，今天主要是讲一下这个，这个消毒的流程啊。\n"
    "输出：\n"
    "我们今天主要是讲\n"
    "这个消毒的流程\n"
)


class StepFunChatSubtitleOptimizer:
    """以 OpenAI-compatible Chat Completions 形态调用字幕优化模型。"""

    def __init__(self, config=None, request_func=None):
        self.config = config or CONFIG
        self.api_key = _resolve_api_key(self.config)
        self.base_url = _resolve_base_url(self.config)
        # model 留空则继承主题评审模型。
        self.model = (
            str(_config_get(self.config, "subtitle_optimization_model", "")).strip()
            or str(_config_get(self.config, "topic_review_model", "step-2-mini")).strip()
        )
        self.timeout = int(_config_get(self.config, "subtitle_optimization_timeout", 180))
        self.retry_attempts = _positive_int_config(self.config, "subtitle_optimization_retry_attempts", 1)
        self.retry_backoff_seconds = _non_negative_float_config(
            self.config, "subtitle_optimization_retry_backoff_seconds", 0.0
        )
        self.temperature = float(_config_get(self.config, "subtitle_optimization_temperature", 0.2))
        self.reasoning_effort = str(_config_get(self.config, "subtitle_optimization_reasoning_effort", "")).strip()
        self.provider_name = str(_config_get(self.config, "subtitle_optimization_provider", "stepfun_chat"))
        self.cache_dir = str(_config_get(self.config, "subtitle_optimization_cache_dir", "") or "")
        self.max_chars_per_line = int(_config_get(self.config, "subtitle_max_chars_per_line", 15))
        self.max_lines = int(_config_get(self.config, "subtitle_max_lines", 1))
        self.window_max_chars = _positive_int_config(self.config, "subtitle_optimization_window_max_chars", 100)
        self.request_func = request_func or urllib.request.urlopen

    def is_available(self):
        """只检查 API Key，不发起网络请求。"""
        return bool(self.api_key)

    def optimize_window(self, window_chunks):
        """对一条 clip 窗口的 chunk 做字幕优化，返回对齐后的显示块或 None。

        窗口按字符预算（`window_max_chars`）贪心分组、逐组请求：文本越短，模型重组语序、
        造词、塞空格的空间越小，子序列通过率越高（见 ADR 0012）。各子窗口按文本签名独立
        缓存与对齐，跨组时间天然连续，拼接后即整条 clip 的显示块。

        成功：返回 TranscriptChunk 列表（每块经子序列校验并对齐回逐字时间）。
        失败（无 key / 无文本 / 任一组 HTTP / 超时 / 非子序列）：返回 None，调用方走规则兜底。
        """
        if not self.api_key:
            return None
        full_text, _ = build_window(window_chunks)
        if not full_text:
            return None
        all_blocks = []
        for group in _split_window(window_chunks, self.window_max_chars):
            text, char_times = build_window(group)
            if not text:
                continue
            block_lines = self._block_lines_cached(text)
            if block_lines is None:
                return None
            aligned = validate_and_align(text, char_times, block_lines, self.max_chars_per_line, self.max_lines)
            if aligned is None:
                return None
            all_blocks.extend(aligned)
        return all_blocks

    def _block_lines_cached(self, text):
        """读缓存命中即返回；否则请求模型、成功后落盘。失败返回 None。

        只缓存块字符串；时间在每次对齐时按当前逐字时间重算，不入缓存。
        """
        block_lines = self._read_cache(text)
        if block_lines is None:
            block_lines = self._resolve_block_lines(text)
            if block_lines is None:
                return None
            self._write_cache(text, block_lines)
        return block_lines

    def _resolve_block_lines(self, text):
        """请求字幕优化模型并解析为显示块文本行；失败返回 None。"""
        for attempt in range(1, self.retry_attempts + 1):
            block_lines = self._request_once(text)
            if block_lines is not None:
                return block_lines
            if attempt < self.retry_attempts:
                self._sleep_before_retry(attempt)
        return None

    def _request_once(self, text):
        try:
            request = self._build_request(text)
            with self.request_func(request, timeout=self.timeout) as response:
                raw_response = response.read().decode("utf-8")
        except (ValueError, urllib.error.HTTPError, TimeoutError, urllib.error.URLError, OSError):
            return None
        except Exception:
            return None
        return _parse_block_lines(raw_response)

    def _sleep_before_retry(self, failed_attempt):
        delay = self.retry_backoff_seconds * failed_attempt
        if delay > 0:
            time.sleep(delay)

    def _build_request(self, text):
        _validate_https_base_url(self.base_url)
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

    def _cache_signature(self, text):
        """按字幕输入签名：窗口文本 + 模型 + base_url + 提示版本（含删词尺度）+ 切分参数。"""
        payload = {
            "schema_version": SUBTITLE_OPTIMIZATION_CACHE_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "provider": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "max_chars_per_line": self.max_chars_per_line,
            "max_lines": self.max_lines,
            "text": text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cache_path(self, text):
        if not self.cache_dir:
            return ""
        return os.path.join(self.cache_dir, f"{self._cache_signature(text)}.json")

    def _read_cache(self, text):
        cache_path = self._cache_path(text)
        if not cache_path or not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if payload.get("schema_version") != SUBTITLE_OPTIMIZATION_CACHE_SCHEMA_VERSION:
            return None
        if payload.get("signature") != self._cache_signature(text):
            return None
        blocks = payload.get("blocks")
        if not isinstance(blocks, list) or not all(isinstance(block, str) for block in blocks):
            return None
        return blocks

    def _write_cache(self, text, block_lines):
        cache_path = self._cache_path(text)
        if not cache_path:
            return
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            payload = {
                "schema_version": SUBTITLE_OPTIMIZATION_CACHE_SCHEMA_VERSION,
                "signature": self._cache_signature(text),
                "blocks": list(block_lines),
            }
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(payload, cache_file, ensure_ascii=False, indent=2, sort_keys=True)
                cache_file.write("\n")
        except OSError:
            return


def create_subtitle_optimizer(config=None, request_func=None):
    """按配置创建字幕优化 provider。"""
    config = config or CONFIG
    provider = str(_config_get(config, "subtitle_optimization_provider", "stepfun_chat")).strip().lower()
    if provider in {"stepfun_chat", "openai_compatible"}:
        return StepFunChatSubtitleOptimizer(config, request_func=request_func)
    raise ValueError(f"Unknown subtitle optimization provider: {provider}")


def _split_window(window_chunks, max_chars):
    """把窗口 chunk 按字符预算贪心分组：累计文本长度超额则起新组。

    单个 chunk 文本本身超过预算时独占一组（不再拆 chunk，保留 chunk 内连续性）。
    `max_chars` 非正数时视为不限，全部归一组。
    """
    if max_chars <= 0:
        return [list(window_chunks)]
    groups = []
    current = []
    current_len = 0
    for chunk in window_chunks:
        chunk_len = len(str(chunk.text))
        if current and current_len + chunk_len > max_chars:
            groups.append(current)
            current = []
            current_len = 0
        current.append(chunk)
        current_len += chunk_len
    if current:
        groups.append(current)
    return groups


def _parse_block_lines(raw_response):
    """从 Chat Completions 响应里提取显示块文本行；无内容返回 None。"""
    try:
        payload = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        return None
    content = _extract_chat_content(payload)
    if content is None:
        return None
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 容忍模型偶发包裹的 Markdown 代码块围栏。
        if line.startswith("```"):
            continue
        lines.append(line)
    if not lines:
        return None
    return lines


def _extract_chat_content(payload):
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _resolve_api_key(config):
    api_key = _config_get(config, "subtitle_optimization_api_key", None)
    if api_key:
        return str(api_key)
    env_name = _config_get(config, "subtitle_optimization_api_key_env", "STEPFUN_API_KEY")
    return os.environ.get(str(env_name), "")


def _resolve_base_url(config):
    base_url = _config_get(config, "subtitle_optimization_base_url", None)
    if base_url:
        return str(base_url)
    return "https://api.stepfun.com/v1"


def _validate_https_base_url(base_url):
    parsed = urllib.parse.urlparse(str(base_url))
    if parsed.scheme.lower() != "https":
        raise ValueError("Subtitle optimization base_url must use HTTPS for credential safety")


def _positive_int_config(config, key, default):
    raw_value = _config_get(config, key, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return int(default)
    return max(1, value)


def _non_negative_float_config(config, key, default):
    raw_value = _config_get(config, key, default)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, value)


def _config_get(config, key, default=None):
    if config is not None and key in config:
        return config[key]
    if key in CONFIG:
        return CONFIG[key]
    return default
