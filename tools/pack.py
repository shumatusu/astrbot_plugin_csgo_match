#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把插件打包成可发布（release）的 zip。

用法（在插件目录或仓库根目录都行）：
    python tools/pack.py

产物：与插件目录同级的 astrbot_plugin_csgo_match.zip

包内结构与你手动压缩的一致：**文件直接位于 zip 根目录**，不套
astrbot_plugin_wmpvp_csgo/ 前缀（解压到 data/plugins/<插件名>/ 即可）。

发布包只保留运行插件所需内容，以下开发产物一律排除：
    __pycache__ / data / *.pyc / .gitignore
    tests/   （冒烟测试与 astrbot 桩）
    tools/   （打包脚本、接口探测脚本）
    zip 自身
"""

import sys
import zipfile
from pathlib import Path

PLUGIN_DIR_NAME = "astrbot_plugin_wmpvp_csgo"   # 本地插件目录名
OUTPUT_NAME = "astrbot_plugin_csgo_match"          # 发布包 / 仓库名
SKIP_DIRS = {
    "__pycache__", "data", ".git", ".idea", ".vscode",
    "tests", "tools",          # 开发产物，不进发布包
}
SKIP_FILES = {f"{PLUGIN_DIR_NAME}.zip", f"{OUTPUT_NAME}.zip",
              ".DS_Store", ".gitignore"}


def main() -> int:
    here = Path(__file__).resolve().parent
    root = here.parent if here.name == "tools" else here
    if root.name != PLUGIN_DIR_NAME:
        # 允许从仓库根目录调用
        candidate = root / PLUGIN_DIR_NAME
        if candidate.is_dir():
            root = candidate
        else:
            print(f"找不到插件目录 {PLUGIN_DIR_NAME}/")
            return 1

    out = root.parent / f"{OUTPUT_NAME}.zip"
    count = 0
    total = 0
    dirs = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name in SKIP_FILES or path.suffix == ".pyc":
                continue
            if path.is_file():
                rel = path.relative_to(root)
                # 补目录条目，Explorer 里展开时才有文件夹（与手动压缩一致）
                for parent in rel.parents:
                    if parent.as_posix() != ".":
                        dirs.add(parent.as_posix())
                # 相对插件目录本身，保持根目录平铺（与手动压缩的结果一致）
                zf.write(path, rel.as_posix())
                count += 1
                total += path.stat().st_size
        for d in sorted(dirs):
            zf.writestr(zipfile.ZipInfo(f"{d}/"), b"")

    print(f"已打包 {count} 个文件 → {out}")
    print(f"源文件 {total / 1024:.0f} KB，压缩后 {out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
