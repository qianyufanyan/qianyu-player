# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[],
    datas=[('dist/payload.zip', '.'), ('assets/qianyu.ico', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.Qt3DCore', 'PySide6.QtCharts', 'PySide6.QtMultimedia', 'PySide6.QtNetwork', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtOpenGL', 'pypinyin', 'requests', 'numpy', 'PIL'],
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
    name='qianyu-player-v1.0.1-setup',
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
    icon=['assets/qianyu.ico'],
)
