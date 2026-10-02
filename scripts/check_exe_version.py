"""校验打包产物里真正带的版本号，免得「改了代码忘了升版本号」就把包发出去。

为什么不能在 exe 里直接搜字符串：Python 字节码是压缩存放的，在二进制里搜到的 "1.3.2"
其实是 zlib 自己的版本号（inflate 1.3.2），会骗过检查。所以走 PyInstaller 的归档读取器，
把 kuaitou.storage 的代码对象取出来，再看它里面的常量。

用法：

    .build\\venv\\Scripts\\python.exe scripts\\check_exe_version.py                  # 校验默认产物
    .build\\venv\\Scripts\\python.exe scripts\\check_exe_version.py dist\\快投.exe
    .build\\venv\\Scripts\\python.exe scripts\\check_exe_version.py --expect 9.9.9   # 走不一致分支

退出码：0=一致，1=不一致，2=打不开产物或缺 PyInstaller。

注意：这里用的是 PyInstaller 的内部 Reader API（不是公开接口），升级 PyInstaller 大版本后
要回来跑一遍确认没变。requirements.txt 已把版本钉死。
"""

import argparse
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kuaitou.storage import APP_VERSION  # noqa: E402

DEFAULT_EXE = os.path.join("dist", "快投.exe")

# CI 的 Windows runner 上 stdout 是管道，编码会取系统 ANSI 代码页，直接 print 中文会抛
# UnicodeEncodeError（本地是控制台反而看不出来）。这里统一钉成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def walk_versions(code):
    """在代码对象常量里递归找形如 x.y.z 的字符串。"""
    hits = set()
    if not isinstance(code, types.CodeType):
        return hits
    for const in code.co_consts:
        if isinstance(const, str):
            parts = const.split(".")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                hits.add(const)
        elif isinstance(const, types.CodeType):
            hits |= walk_versions(const)
    return hits


def read_versions(exe):
    """取出 exe 里 kuaitou.storage 的版本号常量集合。"""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    arch = CArchiveReader(exe)
    with tempfile.TemporaryDirectory() as tmp:
        pyz = os.path.join(tmp, "PYZ.pyz")
        with open(pyz, "wb") as f:
            f.write(arch.extract("PYZ.pyz"))
        code = ZlibArchiveReader(pyz).extract("kuaitou.storage")   # 已经是反序列化的代码对象
    return walk_versions(code)


def main():
    parser = argparse.ArgumentParser(description="校验 exe 内的版本号与源码一致")
    parser.add_argument("exe", nargs="?", default=DEFAULT_EXE,
                        help="打包产物路径（默认 %s）" % DEFAULT_EXE)
    parser.add_argument("--expect", default=None,
                        help="期望的版本号，默认取 kuaitou.storage.APP_VERSION")
    args = parser.parse_args()

    expect = args.expect or APP_VERSION
    if not os.path.exists(args.exe):
        print("打不开打包产物：%s（先跑一遍 PyInstaller）" % args.exe, file=sys.stderr)
        return 2
    try:
        hits = read_versions(args.exe)
    except ImportError:
        print("读不了产物：需要 PyInstaller（pip install pyinstaller==6.22.3）", file=sys.stderr)
        return 2
    except Exception as e:
        print("读不了产物：%r" % (e,), file=sys.stderr)
        return 2

    print("产物: %s" % args.exe)
    print("期望版本: %s" % expect)
    print("产物里的版本号: %s" % (", ".join(sorted(hits)) or "（没找到）"))
    if expect not in hits:
        print("版本号对不上：源码是 %s，产物里没有这个串。"
              "请把 kuaitou/storage.py 的 APP_VERSION 改对再重新打包。" % expect, file=sys.stderr)
        return 1
    print("版本号一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
