"""校验生产源码与发布 wheel 是否遵守已批准的模块边界。"""

from __future__ import annotations

import argparse
import ast
import importlib.util
from collections.abc import Iterable, Mapping, Sequence
from configparser import ConfigParser
from configparser import Error as ConfigError
from email.parser import Parser
from email.policy import default as email_policy
from pathlib import Path
from zipfile import BadZipFile, ZipFile

APPROVED_PACKAGE_FILES = frozenset(
    {
        "video_auto_editor/__init__.py",
        "video_auto_editor/application/__init__.py",
        "video_auto_editor/application/_readiness_model.py",
        "video_auto_editor/application/cache_maintenance.py",
        "video_auto_editor/application/live.py",
        "video_auto_editor/application/readiness.py",
        "video_auto_editor/application/run_interpretation.py",
        "video_auto_editor/cache/__init__.py",
        "video_auto_editor/cache/_envelope.py",
        "video_auto_editor/cache/_failure.py",
        "video_auto_editor/cache/_filesystem.py",
        "video_auto_editor/cache/_model.py",
        "video_auto_editor/cache/_repository.py",
        "video_auto_editor/cache/filesystem.py",
        "video_auto_editor/cli.py",
        "video_auto_editor/clip_planning/__init__.py",
        "video_auto_editor/clip_planning/_model.py",
        "video_auto_editor/clip_planning/_planning.py",
        "video_auto_editor/composition.py",
        "video_auto_editor/configuration/__init__.py",
        "video_auto_editor/configuration/_diagnostics.py",
        "video_auto_editor/configuration/_failure.py",
        "video_auto_editor/configuration/_loader.py",
        "video_auto_editor/configuration/_model.py",
        "video_auto_editor/configuration/_schema.py",
        "video_auto_editor/delivery/__init__.py",
        "video_auto_editor/delivery/_media.py",
        "video_auto_editor/delivery/_model.py",
        "video_auto_editor/delivery/build.py",
        "video_auto_editor/delivery/capability.py",
        "video_auto_editor/delivery/publication.py",
        "video_auto_editor/delivery/schema/__init__.py",
        "video_auto_editor/delivery/verification.py",
        "video_auto_editor/diagnostics/__init__.py",
        "video_auto_editor/diagnostics/_facts.py",
        "video_auto_editor/diagnostics/_failure.py",
        "video_auto_editor/diagnostics/_model.py",
        "video_auto_editor/diagnostics/_reader.py",
        "video_auto_editor/diagnostics/_session.py",
        "video_auto_editor/diagnostics/_store.py",
        "video_auto_editor/diagnostics/collecting.py",
        "video_auto_editor/diagnostics/persistent.py",
        "video_auto_editor/runtime/__init__.py",
        "video_auto_editor/runtime/cancellation.py",
        "video_auto_editor/runtime/errors.py",
        "video_auto_editor/runtime/identity.py",
        "video_auto_editor/source_analysis/__init__.py",
        "video_auto_editor/source_analysis/_analysis.py",
        "video_auto_editor/source_analysis/_failure.py",
        "video_auto_editor/source_analysis/_model.py",
        "video_auto_editor/subtitle_optimization/__init__.py",
        "video_auto_editor/subtitle_optimization/_model.py",
        "video_auto_editor/subtitle_optimization/_optimization.py",
        "video_auto_editor/text_model/__init__.py",
        "video_auto_editor/text_model/_stepfun_https.py",
        "video_auto_editor/text_model/deterministic.py",
        "video_auto_editor/text_model/interface.py",
        "video_auto_editor/text_model/stepfun.py",
        "video_auto_editor/topic_review/__init__.py",
        "video_auto_editor/topic_review/_model.py",
        "video_auto_editor/topic_review/_review.py",
        "video_auto_editor/transcription/__init__.py",
        "video_auto_editor/transcription/_normalized_audio.py",
        "video_auto_editor/transcription/_stepaudio_audio.py",
        "video_auto_editor/transcription/_stepaudio_https.py",
        "video_auto_editor/transcription/deterministic.py",
        "video_auto_editor/transcription/interface.py",
        "video_auto_editor/transcription/reconciliation.py",
        "video_auto_editor/transcription/stepaudio.py",
        "video_auto_editor/workspace/__init__.py",
        "video_auto_editor/workspace/_capability.py",
        "video_auto_editor/workspace/_failure.py",
        "video_auto_editor/workspace/_workspace.py",
    }
)

APPROVED_MODULE_NAMES = frozenset(
    package_file.removesuffix(".py")
    .replace("/", ".")
    .removesuffix(".__init__")
    for package_file in APPROVED_PACKAGE_FILES
)


APPROVED_DEPENDENCIES = {
    "__init__": frozenset(),
    "runtime": frozenset(),
    "configuration": frozenset({"runtime"}),
    "workspace": frozenset({"runtime"}),
    "cache": frozenset({"runtime", "workspace"}),
    "source_analysis": frozenset({"runtime", "workspace"}),
    "clip_planning": frozenset(
        {"configuration", "runtime", "source_analysis"}
    ),
    "text_model": frozenset({"runtime"}),
    "transcription": frozenset(
        {"cache", "runtime", "source_analysis", "workspace"}
    ),
    "topic_review": frozenset(
        {"cache", "clip_planning", "runtime", "text_model"}
    ),
    "subtitle_optimization": frozenset(
        {
            "cache",
            "clip_planning",
            "runtime",
            "text_model",
            "transcription",
        }
    ),
    "delivery": frozenset(
        {
            "clip_planning",
            "configuration",
            "runtime",
            "source_analysis",
            "subtitle_optimization",
            "transcription",
            "workspace",
        }
    ),
    "diagnostics": frozenset(
        {
            "cache",
            "clip_planning",
            "configuration",
            "delivery",
            "runtime",
            "workspace",
        }
    ),
    "application": frozenset(
        {
            "clip_planning",
            "configuration",
            "delivery",
            "diagnostics",
            "runtime",
            "source_analysis",
            "text_model",
            "transcription",
            "workspace",
        }
    ),
    "composition": frozenset(
        {
            "application",
            "cache",
            "clip_planning",
            "configuration",
            "delivery",
            "diagnostics",
            "runtime",
            "source_analysis",
            "subtitle_optimization",
            "text_model",
            "topic_review",
            "transcription",
            "workspace",
        }
    ),
    "cli": frozenset(
        {"application", "composition", "runtime", "workspace"}
    ),
}


APPROVED_ADAPTER_IMPORTERS = {
    "video_auto_editor.cache.filesystem": frozenset(
        {"video_auto_editor.composition"}
    ),
    "video_auto_editor.diagnostics.collecting": frozenset(),
    "video_auto_editor.diagnostics.persistent": frozenset(
        {"video_auto_editor.composition"}
    ),
    "video_auto_editor.text_model.deterministic": frozenset(),
    "video_auto_editor.text_model._stepfun_https": frozenset(
        {"video_auto_editor.composition"}
    ),
    "video_auto_editor.text_model.stepfun": frozenset(
        {
            "video_auto_editor.composition",
            "video_auto_editor.text_model._stepfun_https",
        }
    ),
    "video_auto_editor.transcription.deterministic": frozenset(),
    "video_auto_editor.transcription._stepaudio_audio": frozenset(
        {"video_auto_editor.composition"}
    ),
    "video_auto_editor.transcription._stepaudio_https": frozenset(
        {"video_auto_editor.composition"}
    ),
    "video_auto_editor.transcription.stepaudio": frozenset(
        {
            "video_auto_editor.composition",
            "video_auto_editor.transcription._stepaudio_https",
        }
    ),
}


APPROVED_CROSS_PACKAGE_PRIVATE_IMPORTS = frozenset(
    {
        (
            "video_auto_editor.composition",
            "video_auto_editor.application.live",
            "_DeliveryBuildWork",
        ),
        (
            "video_auto_editor.composition",
            "video_auto_editor.application.live",
            "_RunAssembly",
        ),
        (
            "video_auto_editor.composition",
            "video_auto_editor.application.live",
            "_StageWork",
        ),
        (
            "video_auto_editor.composition",
            "video_auto_editor.text_model._stepfun_https",
            "StdlibStepFunTransport",
        ),
        (
            "video_auto_editor.composition",
            "video_auto_editor.transcription._stepaudio_audio",
            "FFmpegNormalizedPcmPreparer",
        ),
        (
            "video_auto_editor.composition",
            "video_auto_editor.transcription._stepaudio_https",
            "StdlibStepAudioTransport",
        ),
    }
)


class ArchitectureViolation(RuntimeError):
    """生产结构偏离已批准布局。"""


class _CaseSensitiveConfigParser(ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def validate_source_tree(project_root: Path) -> None:
    """校验项目根目录下生产包的精确 Python 文件集合。"""

    package_root = project_root / "video_auto_editor"
    actual_files = frozenset(
        path.relative_to(project_root).as_posix()
        for path in package_root.rglob("*.py")
    )
    missing = sorted(APPROVED_PACKAGE_FILES - actual_files)
    unexpected = sorted(actual_files - APPROVED_PACKAGE_FILES)
    details: list[str] = []
    if missing:
        details.append("缺少已批准模块: " + ", ".join(missing))
    if unexpected:
        details.append("发现未批准模块: " + ", ".join(unexpected))
    if not details:
        sources = {
            relative_path: (project_root / relative_path).read_text(
                encoding="utf-8"
            )
            for relative_path in actual_files
        }
        details.extend(_dependency_violations(sources))
    if details:
        raise ArchitectureViolation("；".join(details))


def validate_wheel(
    wheel_path: Path,
    *,
    source_root: Path | None = None,
) -> None:
    """校验 wheel 内生产模块、元数据和公共入口。"""

    try:
        with ZipFile(wheel_path) as archive:
            members = frozenset(
                name
                for name in archive.namelist()
                if name and not name.endswith("/")
            )
            package_files = frozenset(
                name
                for name in members
                if name.startswith("video_auto_editor/")
            )

            member_details = _wheel_member_violations(members, package_files)
            if member_details:
                raise ArchitectureViolation("；".join(member_details))
            package_contents = {
                relative_path: archive.read(relative_path)
                for relative_path in package_files
            }
            if source_root is not None:
                content_details = _wheel_source_content_violations(
                    package_contents,
                    source_root,
                )
                if content_details:
                    raise ArchitectureViolation("；".join(content_details))
            package_sources = {
                relative_path: contents.decode("utf-8")
                for relative_path, contents in package_contents.items()
            }
            dependency_details = _dependency_violations(package_sources)
            if dependency_details:
                raise ArchitectureViolation("；".join(dependency_details))

            dist_info = next(
                name.split("/", 1)[0]
                for name in members
                if ".dist-info/" in name
            )
            metadata = Parser(policy=email_policy).parsestr(
                archive.read(f"{dist_info}/METADATA").decode("utf-8")
            )
            wheel_metadata = Parser(policy=email_policy).parsestr(
                archive.read(f"{dist_info}/WHEEL").decode("utf-8")
            )
            entry_points_text = archive.read(
                f"{dist_info}/entry_points.txt"
            ).decode("utf-8")
    except (BadZipFile, OSError) as error:
        raise ArchitectureViolation(
            f"无法读取 wheel {wheel_path}: {error}"
        ) from error
    except (KeyError, StopIteration, UnicodeError) as error:
        raise ArchitectureViolation(
            f"wheel 缺少可读的发布元数据或 console script: {error}"
        ) from error

    metadata_details: list[str] = []
    if metadata.get("Name") != "video-auto-editor":
        metadata_details.append("wheel 项目名称必须为 video-auto-editor")
    if metadata.get("Version") != "4.7.0":
        metadata_details.append("wheel 项目版本必须为 4.7.0")
    if metadata.get_all("Requires-Dist", []):
        metadata_details.append("wheel 不得声明运行时依赖")
    if wheel_metadata.get("Root-Is-Purelib", "").casefold() != "true":
        metadata_details.append("wheel 必须是纯 Python 产物")
    if wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
        metadata_details.append("wheel 标签必须为 py3-none-any")
    metadata_details.extend(_entry_point_violations(entry_points_text))
    if metadata_details:
        raise ArchitectureViolation("；".join(metadata_details))


def _wheel_member_violations(
    members: frozenset[str],
    package_files: frozenset[str],
) -> list[str]:
    details: list[str] = []
    missing = sorted(APPROVED_PACKAGE_FILES - package_files)
    unexpected = sorted(package_files - APPROVED_PACKAGE_FILES)
    if missing:
        details.append("wheel 缺少已批准模块: " + ", ".join(missing))
    if unexpected:
        details.append("wheel 包含未批准模块: " + ", ".join(unexpected))

    dist_info_directories = {
        name.split("/", 1)[0]
        for name in members
        if ".dist-info/" in name
    }
    expected_dist_info = "video_auto_editor-4.7.0.dist-info"
    if dist_info_directories != {expected_dist_info}:
        details.append(
            "wheel 必须只包含 video_auto_editor-4.7.0.dist-info"
        )
        return details

    required_metadata = {
        f"{expected_dist_info}/METADATA",
        f"{expected_dist_info}/WHEEL",
        f"{expected_dist_info}/entry_points.txt",
        f"{expected_dist_info}/RECORD",
    }
    missing_metadata = sorted(required_metadata - members)
    if missing_metadata:
        details.append(
            "wheel 缺少发布元数据或 console script: "
            + ", ".join(missing_metadata)
        )

    unexpected_roots = sorted(
        name
        for name in members
        if not name.startswith("video_auto_editor/")
        and not name.startswith(f"{expected_dist_info}/")
    )
    if unexpected_roots:
        details.append(
            "wheel 包含未批准的顶层成员: " + ", ".join(unexpected_roots)
        )
    return details


def _wheel_source_content_violations(
    package_contents: Mapping[str, bytes],
    source_root: Path,
) -> list[str]:
    details: list[str] = []
    for relative_path, built_contents in sorted(package_contents.items()):
        source_path = source_root / relative_path
        try:
            source_contents = source_path.read_bytes()
        except OSError as error:
            details.append(f"无法读取当前源码 {relative_path}: {error}")
            continue
        if built_contents != source_contents:
            details.append(
                "wheel 模块内容与当前源码不一致: " + relative_path
            )
    return details


def _entry_point_violations(entry_points_text: str) -> list[str]:
    parser = _CaseSensitiveConfigParser(interpolation=None)
    try:
        parser.read_string(entry_points_text)
    except ConfigError as error:
        return [f"wheel console script 元数据无法解析: {error}"]

    expected = {"video-auto-editor": "video_auto_editor.cli:main"}
    if parser.sections() != ["console_scripts"]:
        return ["wheel 必须只声明 console_scripts 入口组"]
    if dict(parser["console_scripts"]) != expected:
        return [
            (
                "wheel 必须只声明 console script "
                "video-auto-editor = video_auto_editor.cli:main"
            )
        ]
    return []


def _dependency_violations(
    package_sources: Mapping[str, str],
) -> list[str]:
    violations: list[str] = []
    for relative_path in sorted(package_sources):
        source_owner = _owner_for_path(relative_path)
        module_name = _module_name_for_path(relative_path)
        try:
            tree = ast.parse(
                package_sources[relative_path],
                filename=relative_path,
            )
        except SyntaxError as error:
            violations.append(f"无法解析 {relative_path}: {error.msg}")
            continue

        for node, imported_module in _internal_imports(
            tree,
            module_name,
            is_package=Path(relative_path).name == "__init__.py",
        ):
            approved_importers = APPROVED_ADAPTER_IMPORTERS.get(
                imported_module
            )
            if (
                approved_importers is not None
                and module_name not in approved_importers
            ):
                violations.append(
                    "具体 Adapter 导入违规: "
                    f"{source_owner} 导入 {imported_module} "
                    f"({relative_path}:{node.lineno})"
                )
            imported_owner = _owner_for_module(imported_module)
            if imported_owner in {None, source_owner}:
                continue
            for imported_name in _private_imported_names(
                node,
                imported_module,
            ):
                approved_private_import = (
                    module_name,
                    imported_module,
                    imported_name,
                )
                if (
                    approved_private_import
                    not in APPROVED_CROSS_PACKAGE_PRIVATE_IMPORTS
                ):
                    violations.append(
                        "跨包私有导入违规: "
                        f"{source_owner} -> {imported_owner} "
                        f"({imported_module}:{imported_name or '*'}) "
                        f"({relative_path}:{node.lineno})"
                    )
            allowed = APPROVED_DEPENDENCIES[source_owner]
            if imported_owner not in allowed:
                violations.append(
                    "依赖方向违规: "
                    f"{source_owner} -> {imported_owner} "
                    f"({relative_path}:{node.lineno})"
                )
    return violations


def _owner_for_path(relative_path: str) -> str:
    parts = Path(relative_path).parts
    if len(parts) == 2:
        return Path(parts[-1]).stem
    return parts[1]


def _module_name_for_path(relative_path: str) -> str:
    path = Path(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _owner_for_module(module_name: str) -> str | None:
    parts = module_name.split(".")
    if not parts or parts[0] != "video_auto_editor":
        return None
    if len(parts) == 1:
        return "__init__"
    return parts[1]


def _private_imported_names(
    node: ast.Import | ast.ImportFrom,
    imported_module: str,
) -> tuple[str, ...]:
    module_is_private = any(
        part.startswith("_")
        for part in imported_module.split(".")[2:]
    )
    if isinstance(node, ast.Import):
        return ("",) if module_is_private else ()

    private_names = tuple(
        alias.name for alias in node.names if alias.name.startswith("_")
    )
    if module_is_private:
        return tuple(alias.name for alias in node.names)
    return private_names


def _internal_imports(
    tree: ast.AST,
    module_name: str,
    *,
    is_package: bool,
) -> Iterable[tuple[ast.Import | ast.ImportFrom, str]]:
    package_name = (
        module_name if is_package else module_name.rpartition(".")[0]
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("video_auto_editor"):
                    yield node, alias.name
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        imported_module = node.module or ""
        if node.level:
            imported_module = importlib.util.resolve_name(
                "." * node.level + imported_module,
                package_name,
            )
        if not imported_module.startswith("video_auto_editor"):
            continue
        if imported_module == "video_auto_editor":
            for alias in node.names:
                if alias.name != "*":
                    yield node, f"video_auto_editor.{alias.name}"
            continue
        yield node, imported_module
        for alias in node.names:
            imported_submodule = f"{imported_module}.{alias.name}"
            if imported_submodule in APPROVED_MODULE_NAMES:
                yield node, imported_submodule


def main(argv: Sequence[str] | None = None) -> int:
    """运行源码门禁，并可额外校验一个或多个 wheel。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="项目根目录",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        action="append",
        default=[],
        help="需要校验的 wheel，可重复指定",
    )
    arguments = parser.parse_args(argv)

    validate_source_tree(arguments.root)
    for wheel_path in arguments.wheel:
        validate_wheel(wheel_path, source_root=arguments.root)
    print("生产结构校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
