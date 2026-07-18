#!/usr/bin/env python3
"""从私密知识库生成去隐私、可校验的公开原始版本。"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "发布包"
STATIC_EXCLUDES = {
    ".git",
    ".releaseignore",
    ".release-private-patterns",
    ".DS_Store",
    "发布包",
    "__pycache__",
}
PRIVATE_BLOCK = re.compile(
    r"^[ \t]*<!--\s*private:start\s*-->[ \t]*\n.*?^[ \t]*<!--\s*private:end\s*-->[ \t]*(?:\n|$)",
    re.DOTALL | re.MULTILINE,
)
RELEASE_LINE = re.compile(r"^[ \t]*<!--\s*release:\s?(.*?)\s*-->[ \t]*$", re.MULTILINE)
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip().replace("\\", "/")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def is_under(rel: str, candidate: str) -> bool:
    candidate = candidate.rstrip("/")
    return rel == candidate or rel.startswith(candidate + "/")


def is_private_markdown(source: Path, rel: str) -> bool:
    if source.suffix.lower() != ".md" or rel.startswith("模板/"):
        return False
    try:
        head = source.read_text(encoding="utf-8")[:8192]
    except UnicodeDecodeError:
        return False
    if not head.startswith("---\n"):
        return False
    end = head.find("\n---", 4)
    frontmatter = head[4:end] if end != -1 else head
    return re.search(r"(?m)^sensitivity:\s*private\s*$", frontmatter) is not None


def should_skip(source: Path, rel: str, excludes: list[str]) -> bool:
    if any(part in STATIC_EXCLUDES for part in Path(rel).parts):
        return True
    if any(is_under(rel, item) for item in excludes):
        return True
    return source.is_file() and is_private_markdown(source, rel)


def copy_public_tree(stage: Path, excludes: list[str]) -> None:
    for source in sorted(ROOT.rglob("*")):
        rel = source.relative_to(ROOT).as_posix()
        if should_skip(source, rel, excludes):
            continue
        target = stage / rel
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def sanitize_shared_markdown(stage: Path) -> None:
    for path in stage.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        text = PRIVATE_BLOCK.sub("", text)
        text = RELEASE_LINE.sub(lambda match: match.group(1).rstrip(), text)
        text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
        path.write_text(text, encoding="utf-8")


def install_blank_memory(stage: Path, build_date: str) -> None:
    template = (ROOT / "模板" / "长期记忆.md").read_text(encoding="utf-8")
    template = template.replace("YYYY-MM-DD", build_date)
    (stage / "MEMORY.md").write_text(template, encoding="utf-8")


def reset_seed_maps(stage: Path) -> None:
    for rel in ("60-地图/哲学地图.md", "60-地图/文学地图.md", "60-地图/跨领域地图.md"):
        path = stage / rel
        if path.exists():
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("status: growing", "status: seed", 1), encoding="utf-8")


def write_release_note(stage: Path, version: str, build_date: str) -> None:
    note = f"""# 原始版本说明

- 版本：{version}
- 生成日期：{build_date}
- 定位：可公开使用的空白个人记忆库骨架

本版本保留分类、模板、规则、助手入口、长期记忆空白模板和公开构建工具；不包含生成者的私人原始材料、思考卡片、分析、长期记忆、使用审计或 Git 历史。

从[首页](首页.md)或[助手使用指南](助手使用指南.md)开始即可。
"""
    (stage / "版本说明.md").write_text(note, encoding="utf-8")


def verify_no_private_paths(stage: Path, excludes: list[str]) -> None:
    leaked = [item for item in excludes if item != "MEMORY.md" and (stage / item).exists()]
    if leaked:
        raise RuntimeError("公开副本仍含排除路径：" + "、".join(leaked))


def verify_blank_memory(stage: Path, build_date: str) -> None:
    expected = (ROOT / "模板" / "长期记忆.md").read_text(encoding="utf-8")
    expected = expected.replace("YYYY-MM-DD", build_date)
    actual = (stage / "MEMORY.md").read_text(encoding="utf-8")
    if actual != expected:
        raise RuntimeError("公开副本的 MEMORY.md 不是空白长期记忆模板")


def verify_no_private_patterns(stage: Path, patterns: list[str]) -> None:
    hits: list[str] = []
    text_suffixes = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".py", ".sh"}
    folded = [(pattern, pattern.casefold()) for pattern in patterns]
    for path in stage.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        try:
            content = path.read_text(encoding="utf-8").casefold()
        except UnicodeDecodeError:
            continue
        for original, pattern in folded:
            if pattern in content:
                hits.append(f"{path.relative_to(stage)}: {original}")
    if hits:
        raise RuntimeError("敏感词检查未通过：\n" + "\n".join(hits))


def verify_markers_removed(stage: Path) -> None:
    hits: list[str] = []
    marker = re.compile(r"(?m)^[ \t]*<!--\s*(?:private:(?:start|end)|release:)")
    for path in stage.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if marker.search(text):
            hits.append(str(path.relative_to(stage)))
    if hits:
        raise RuntimeError("公开副本仍含发布标记：" + "、".join(hits))


def verify_markdown_links(stage: Path) -> None:
    broken: list[str] = []
    for path in stage.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for raw in MARKDOWN_LINK.findall(text):
            target = raw.strip().strip("<>")
            if (
                not target
                or target.startswith(("http://", "https://", "mailto:", "#"))
                or "{{" in target
            ):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(stage.resolve())
            except ValueError:
                broken.append(f"{path.relative_to(stage)} -> {raw}")
                continue
            if not resolved.exists():
                broken.append(f"{path.relative_to(stage)} -> {raw}")
    if broken:
        raise RuntimeError("公开副本存在断链：\n" + "\n".join(broken))


def create_archive(stage: Path, folder_name: str, version: str, build_date: str) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT_DIR / f"个人记忆库-原始版本-{version}-{build_date.replace('-', '')}.zip"
    temporary_archive = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                bundle.write(path, (Path(folder_name) / path.relative_to(stage)).as_posix())
    temporary_archive.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(".sha256")
    temporary_checksum = checksum.with_suffix(".sha256.tmp")
    temporary_checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    temporary_checksum.replace(checksum)
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1.0.0", help="公开版本号")
    parser.add_argument("--date", default=date.today().isoformat(), help="生成日期 YYYY-MM-DD")
    args = parser.parse_args()

    excludes = read_list(ROOT / ".releaseignore")
    patterns = read_list(ROOT / ".release-private-patterns")
    folder_name = f"个人记忆库-原始版本-{args.version}"

    with tempfile.TemporaryDirectory(prefix="memory-vault-release-") as temp:
        stage = Path(temp) / folder_name
        stage.mkdir()
        copy_public_tree(stage, excludes)
        sanitize_shared_markdown(stage)
        install_blank_memory(stage, args.date)
        reset_seed_maps(stage)
        write_release_note(stage, args.version, args.date)
        verify_no_private_paths(stage, excludes)
        verify_blank_memory(stage, args.date)
        verify_no_private_patterns(stage, patterns)
        verify_markers_removed(stage)
        verify_markdown_links(stage)
        archive, checksum = create_archive(stage, folder_name, args.version, args.date)

    print(f"发布包：{archive}")
    print(f"校验文件：{checksum}")


if __name__ == "__main__":
    main()
