# -*- coding: utf-8 -*-
"""
千羽播放器 · 安装程序

把打包好的程序复制到本机、创建快捷方式、登记卸载信息。
不带参数运行是安装界面；带 --uninstall 是卸载。

这个脚本本身会被 PyInstaller 打包成安装包，payload.zip 里
放的是 dist/千羽播放器 的完整内容。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
import winreg
from pathlib import Path

from PySide6.QtCore import QTimer, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

APP_NAME = "千羽播放器"
APP_SHORT = "QianyuPlayer"
REG_PATH = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_SHORT}"
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
EXE_PLAYER = "千羽播放器.exe"

_BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_PATH = _BUNDLE / "assets" / "qianyu.ico"

CANVAS = "#FAF9F5"
CARD = "#FFFFFF"
BORDER = "#E8E4DA"
INK = "#1F1E1D"
INK_SOFT = "#73706A"
ACCENT = "#C96442"
ACCENT_HOVER = "#B55636"

QSS = f"""
QWidget#Root {{ background: {CANVAS}; }}
QLabel {{ color: {INK}; font-size: 13px; }}
QLabel#Title {{ font-size: 24px; font-weight: 600; }}
QLabel#Sub {{ color: {INK_SOFT}; font-size: 13px; }}
QLabel#Step {{ color: {INK_SOFT}; font-size: 12px; }}
QLineEdit {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 8px 12px; color: {INK}; font-size: 13px;
}}
QPushButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 8px 16px; color: {INK}; font-size: 13px;
}}
QPushButton:hover {{ background: #F5F3EC; }}
QPushButton#Primary {{ background: {ACCENT}; color: #FFFFFF; border: none; padding: 10px 22px; }}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Primary:disabled {{ background: #D9C9C2; }}
QProgressBar {{
    border: 1px solid {BORDER}; border-radius: 6px; background: {CARD};
    height: 10px; text-align: center; color: {INK_SOFT}; font-size: 11px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QFrame#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
"""


def payload_source() -> Path:
    """安装源：正式安装包里是 payload.zip，开发时也可以是打包好的目录。"""
    for name in ("payload.zip", "payload", "dist/千羽播放器"):
        p = _BUNDLE / name
        if p.exists():
            return p
    fallback = Path(__file__).resolve().parent / "dist" / "千羽播放器"
    return fallback if fallback.exists() else _BUNDLE


def make_shortcut(lnk: Path, target: Path, workdir: Path) -> None:
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{lnk}'); "
        f"$sc.TargetPath = '{target}'; "
        f"$sc.WorkingDirectory = '{workdir}'; "
        "$sc.IconLocation = '%s,0'; "
        "$sc.Save()" % target
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                   capture_output=True, creationflags=0x08000000)


def write_uninstaller(install_dir: Path) -> Path:
    bat = install_dir / "卸载.bat"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "echo 正在卸载...",
        f'taskkill /f /im "{EXE_PLAYER}" >nul 2>nul',
        f'reg delete "HKCU\\{REG_PATH}" /f >nul 2>nul',
        f'del "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\{APP_NAME}.lnk" >nul 2>nul',
        f'del "%USERPROFILE%\\Desktop\\{APP_NAME}.lnk" >nul 2>nul',
        "echo 完成。按任意键关闭。",
        'start "" cmd /c "timeout /t 2 >nul & rmdir /s /q \\"%~dp0\\""',
        "pause >nul",
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="gbk", errors="replace")
    return bat


def register_uninstall(install_dir: Path) -> None:
    uninst = install_dir / "卸载.bat"
    exe = install_dir / EXE_PLAYER
    size_kb = 0
    try:
        size_kb = sum(f.stat().st_size for f in install_dir.rglob("*") if f.is_file()) // 1024
    except Exception:
        pass
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, "1.0.0")
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "本地")
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'"{uninst}"')
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


class InstallWorker(QThread):
    progress = Signal(int, str)
    done = Signal(bool, str)

    def __init__(self, src: Path, dst: Path, desktop: bool, startmenu: bool) -> None:
        super().__init__()
        self.src, self.dst = src, dst
        self.desktop, self.startmenu = desktop, startmenu

    def run(self) -> None:
        try:
            self.dst.mkdir(parents=True, exist_ok=True)
            keep = {"qianyu.json", "qianyu_sources.json", "config.json"}

            if self.src.is_file() and self.src.suffix.lower() == ".zip":
                with zipfile.ZipFile(self.src) as z:
                    names = [n for n in z.namelist() if not n.endswith("/")]
                    total = max(1, len(names))
                    for i, name in enumerate(names, 1):
                        leaf = Path(name).name
                        if leaf in keep or name.startswith("歌词缓存"):
                            continue
                        z.extract(name, self.dst)
                        if i % 40 == 0 or i == total:
                            self.progress.emit(int(i * 92 / total), leaf)
            else:
                files = [f for f in self.src.rglob("*") if f.is_file()]
                total = max(1, len(files))
                for i, f in enumerate(files, 1):
                    rel = f.relative_to(self.src)
                    if rel.name in keep or (rel.parts and rel.parts[0] == "歌词缓存"):
                        continue
                    target = self.dst / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
                    if i % 40 == 0 or i == total:
                        self.progress.emit(int(i * 92 / total), rel.name)

            self.progress.emit(94, "创建卸载程序")
            write_uninstaller(self.dst)

            self.progress.emit(96, "创建快捷方式")
            target = self.dst / EXE_PLAYER
            if target.exists():
                if self.desktop:
                    make_shortcut(Path.home() / "Desktop" / f"{APP_NAME}.lnk", target, self.dst)
                if self.startmenu:
                    menu = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
                            "Start Menu" / "Programs")
                    menu.mkdir(parents=True, exist_ok=True)
                    make_shortcut(menu / f"{APP_NAME}.lnk", target, self.dst)

            self.progress.emit(98, "登记卸载信息")
            register_uninstall(self.dst)
            self.progress.emit(100, "完成")
            self.done.emit(True, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


class InstallerWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle(f"{APP_NAME} 安装程序")
        self.setStyleSheet(QSS)
        self.setFixedSize(560, 420)
        self.src = payload_source()
        self.worker: InstallWorker | None = None
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        root.setSpacing(14)

        title = QLabel(f"安装 {APP_NAME}")
        title.setObjectName("Title")
        sub = QLabel("本地音乐 + 在线搜索下载 + 大字号歌词，装好后从桌面或开始菜单启动")
        sub.setObjectName("Sub")
        sub.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(sub)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 18)
        cl.setSpacing(10)

        lbl = QLabel("安装位置")
        lbl.setObjectName("Step")
        cl.addWidget(lbl)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.path_edit = QLineEdit(str(DEFAULT_DIR))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        cl.addLayout(row)

        self.chk_desktop = QCheckBox("创建桌面快捷方式")
        self.chk_desktop.setChecked(True)
        self.chk_menu = QCheckBox("添加到开始菜单")
        self.chk_menu.setChecked(True)
        cl.addWidget(self.chk_desktop)
        cl.addWidget(self.chk_menu)
        root.addWidget(card)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        self.status = QLabel("准备就绪")
        self.status.setObjectName("Step")
        root.addWidget(self.status)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.close)
        self.btn_install = QPushButton("开始安装")
        self.btn_install.setObjectName("Primary")
        self.btn_install.clicked.connect(self._install)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_install)
        root.addLayout(btns)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择安装位置", self.path_edit.text())
        if d:
            self.path_edit.setText(d)

    def _install(self) -> None:
        dst = Path(self.path_edit.text().strip() or DEFAULT_DIR)
        src_ok = ((self.src.is_file() and self.src.suffix.lower() == ".zip")
                  or (self.src / EXE_PLAYER).exists())
        if not src_ok:
            self.status.setText(f"安装包内容缺失：{self.src}")
            return
        self.btn_install.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.bar.setValue(0)
        self.status.setText("正在安装…")
        self.worker = InstallWorker(self.src, dst, self.chk_desktop.isChecked(),
                                    self.chk_menu.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_progress(self, pct: int, what: str) -> None:
        self.bar.setValue(pct)
        self.status.setText(f"{pct}% · {what}")

    def _on_done(self, ok: bool, err: str) -> None:
        if ok:
            self.bar.setValue(100)
            self.status.setText("安装完成，正在启动…")
            QTimer.singleShot(700, self._launch)
        else:
            self.status.setText(f"安装失败：{err}")
            self.btn_install.setEnabled(True)
            self.btn_cancel.setEnabled(True)

    def _launch(self) -> None:
        dst = Path(self.path_edit.text().strip() or DEFAULT_DIR)
        exe = dst / EXE_PLAYER
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(dst))
        self.close()


def uninstall() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as k:
            path = Path(winreg.QueryValueEx(k, "InstallLocation")[0])
    except Exception:
        path = DEFAULT_DIR
    for exe in (EXE_PLAYER, "本地播放器.exe", "酷狗歌词.exe"):
        subprocess.run(["taskkill", "/f", "/im", exe],
                       capture_output=True, creationflags=0x08000000)
    for lnk in (Path.home() / "Desktop" / f"{APP_NAME}.lnk",
                Path.home() / "Desktop" / "本地播放器.lnk",
                Path.home() / "Desktop" / "酷狗歌词.lnk"):
        try:
            lnk.unlink(missing_ok=True)
        except Exception:
            pass
    menus = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
             "Start Menu" / "Programs")
    shutil.rmtree(menus / APP_NAME, ignore_errors=True)
    shutil.rmtree(menus / "歌词显示", ignore_errors=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)
    return 0


def main() -> int:
    if "--uninstall" in sys.argv:
        return uninstall()
    app = QApplication(sys.argv)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    w = InstallerWindow()
    if ICON_PATH.exists():
        w.setWindowIcon(QIcon(str(ICON_PATH)))
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
