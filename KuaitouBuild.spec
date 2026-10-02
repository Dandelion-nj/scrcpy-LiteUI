# -*- mode: python ; coding: utf-8 -*-
"""快投的 PyInstaller 打包配置（唯一的一份）。

用法：
    .build\\venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean KuaitouBuild.spec

别再从命令行手工拼一遍打包参数：那样跑完 PyInstaller 会在当前目录另生成一份自动命名的
spec（按 --name 取名），于是同一份配置变成两份，早晚会各改各的漂移。

spec 是被 exec 执行的，里面没有 __file__：仓库根目录取自 PyInstaller 注入到 spec 命名空间
里的 SPECPATH（取不到时退回当前工作目录）。
"""

import os
import sys

ROOT = os.path.abspath(globals().get("SPECPATH") or os.getcwd())


def _datas(pairs):
    """挑出真实存在的打包源，缺的跳过并给一行警告。

    icons/ 不入库（图标版权归各 App 所有者，见 THIRD-PARTY-NOTICES.md），所以从干净克隆
    打包时它不存在；而 PyInstaller 碰到缺失的 datas 源会直接报错退出，连包都打不出来。
    这里选择跳过：少的是内置图标素材库，运行时仍会在线抓图、并从手机取真实图标，界面上的
    图标只是先走降级显示，功能不受影响。
    """
    out = []
    for src, dst in pairs:
        if os.path.exists(os.path.join(ROOT, src)):
            out.append((src, dst))
        else:
            print("警告: 打包源不存在，已跳过: %s" % src, file=sys.stderr)
    return out


a = Analysis(
    ['launcher_server.py'],
    pathex=[],
    binaries=[],
    datas=_datas([('index.html', '.'), ('config.json', '.'), ('appicon.ico', '.'), ('icons', 'icons'),
                  ('scrcpy', 'scrcpy'), ('android/icondump.dex', 'android')]),
    hiddenimports=['pystray._win32', 'qrcode', 'qrcode.image.svg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='快投',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['appicon.ico'],
)
