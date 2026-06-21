"""课程上下文 JSON 解析与安全摘要。"""

import json
import os
from copy import deepcopy
from dataclasses import InitVar, dataclass, field


STRING_FIELDS = {
    "course_title",
    "instructor",
    "brand",
    "audience",
    "notes",
}

STRING_LIST_FIELDS = {
    "priority_topics",
    "excluded_topics",
    "forbidden_terms",
}

KNOWN_FIELDS = STRING_FIELDS | STRING_LIST_FIELDS


@dataclass(frozen=True)
class CourseContext:
    """课程上下文，保留未知字段以支持后续扩展。"""

    data: InitVar[dict | None] = None
    _data: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self, data):
        object.__setattr__(self, "_data", deepcopy(data or {}))

    @property
    def data(self):
        """返回上下文副本，避免外部修改绕过加载时校验。"""
        return deepcopy(self._data)

    def summary(self):
        """返回可打印摘要，不泄露完整上下文内容。"""
        known_present = sorted(field for field in KNOWN_FIELDS if field in self._data)
        list_counts = {
            field: len(self._data[field])
            for field in sorted(STRING_LIST_FIELDS)
            if field in self._data
        }
        string_fields = sorted(field for field in STRING_FIELDS if field in self._data)
        unknown_fields = sorted(field for field in self._data if field not in KNOWN_FIELDS)
        return {
            "known_fields": known_present,
            "string_fields": string_fields,
            "list_counts": list_counts,
            "unknown_fields": unknown_fields,
        }


def load_course_context(path):
    """加载并校验课程上下文 JSON 文件。"""
    try:
        with open(path, "r", encoding="utf-8") as context_file:
            payload = json.load(context_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"课程上下文必须是合法 JSON：{exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取课程上下文文件：{exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("课程上下文必须是 JSON object")

    _validate_known_fields(payload)
    return CourseContext(payload)


@dataclass(frozen=True)
class CourseContextBuild:
    """课程上下文采集结果。

    ``payload`` 为可写出的合法上下文（仅含已知字段，且经过类型规整）。
    ``unknown_fields`` 为被排除在交付 JSON 之外的未知字段名，交由调用方决定
    是否提示用户。
    """

    payload: dict
    unknown_fields: list


def build_course_context(raw):
    """把用户提供的课程信息整理为合法上下文 payload。

    - 字符串字段：去空白，空串则丢弃，类型非法时报错。
    - 字符串数组字段：去空白、丢弃空项、保持顺序，类型非法时报错。
    - 未知字段不进入交付 JSON，而是收集到 ``unknown_fields``。
    """
    if not isinstance(raw, dict):
        raise ValueError("课程信息必须是 object/字典")

    payload = {}
    for field_name in sorted(STRING_FIELDS):
        if field_name not in raw:
            continue
        value = raw[field_name]
        if not isinstance(value, str):
            raise ValueError(f"课程上下文字段 {field_name} 必须是字符串")
        cleaned = value.strip()
        if cleaned:
            payload[field_name] = cleaned

    for field_name in sorted(STRING_LIST_FIELDS):
        if field_name not in raw:
            continue
        value = raw[field_name]
        if not isinstance(value, list):
            raise ValueError(f"课程上下文字段 {field_name} 必须是字符串数组")
        cleaned_items = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"课程上下文字段 {field_name}[{index}] 必须是字符串")
            cleaned = item.strip()
            if cleaned:
                cleaned_items.append(cleaned)
        if cleaned_items:
            payload[field_name] = cleaned_items

    unknown_fields = sorted(name for name in raw if name not in KNOWN_FIELDS)
    return CourseContextBuild(payload=payload, unknown_fields=unknown_fields)


def write_course_context(raw, path):
    """采集课程信息并写出合法上下文 JSON，返回采集结果。"""
    build = build_course_context(raw)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as context_file:
        json.dump(build.payload, context_file, ensure_ascii=False, indent=2)
        context_file.write("\n")
    return build


def _validate_known_fields(payload):
    for field_name in sorted(STRING_FIELDS):
        if field_name in payload and not isinstance(payload[field_name], str):
            raise ValueError(f"课程上下文字段 {field_name} 必须是字符串")

    for field_name in sorted(STRING_LIST_FIELDS):
        if field_name not in payload:
            continue
        if not isinstance(payload[field_name], list):
            raise ValueError(f"课程上下文字段 {field_name} 必须是字符串数组")
        for index, item in enumerate(payload[field_name]):
            if not isinstance(item, str):
                raise ValueError(f"课程上下文字段 {field_name}[{index}] 必须是字符串")
