# -*- mode: python ; coding: utf-8 -*-
"""YuanRunner PyInstaller spec：--onedir --windowed 打包。

构建：pip install -e ".[runner]" pyinstaller
      pyinstaller runner/packaging/YuanRunner.spec
产物：dist/YuanRunner/YuanRunner.exe（双击后台运行，无控制台窗口）
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent.parent

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "runner" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # keyring Windows 后端（PyInstaller 有时探测不到动态导入）
        "keyring.backends.Windows",
        "keyring.backends.chainer",
        # yaml C 加速器缺失时纯 Python 回退，但显式声明避免告警
        "yaml",
        # 命令沙箱：psutil 为 C 扩展，显式声明保证杀进程树能力可用
        "psutil",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除大型无关库，缩小体积
        "tkinter",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YuanRunner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无窗口：后台运行
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="YuanRunner",
)
