# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本：只产出「千羽播放器」，歌词窗口是它的一部分。"""

BLOCK = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.QtQuick3D",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "tkinter", "unittest", "pydoc", "pytest", "numpy", "PIL", "matplotlib",
    "selenium", "docx", "openpyxl", "lxml", "yaml", "pytz", "pandas",
]

COMMON = dict(
    pathex=["."],
    excludes=BLOCK,
    noarchive=False,
)

a_player = Analysis(
    ["player.py"],
    datas=[
        ("tools/smtc_bridge.ps1", "tools"),
        ("assets/qianyu.ico", "assets"),
    ],
    hiddenimports=["pypinyin", "requests", "PySide6.QtMultimedia"],
    **COMMON,
)

pyz_player = PYZ(a_player.pure, a_player.zipped_data)

exe_player = EXE(
    pyz_player, a_player.scripts, a_player.binaries, a_player.datas, [],
    name="千羽播放器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/qianyu.ico",
)

coll = COLLECT(
    exe_player, a_player.binaries, a_player.datas,
    name="千羽播放器",
    strip=False,
    upx=False,
    upx_exclude=[],
)
