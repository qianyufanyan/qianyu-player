# -*- coding: utf-8 -*-
"""
本地音乐播放器 + 大字号歌词

自己播歌就能拿到精确的播放位置，歌词进度和声音永远同步，
不需要像跟随酷狗那样靠计时估算。

音乐库默认扫描「音乐」文件夹，文件名按「歌名-歌手」解析（酷狗下载的就是这个格式），
歌词优先找同名 .lrc 文件，没有就走 lyric_display 里的网络歌词接口。

运行环境：Windows + Python 3.10+ + PySide6 + requests
"""

from __future__ import annotations

import ctypes
import os
import random
import re
import sys
import time
from pathlib import Path

import ctypes.wintypes as wt

from PySide6.QtCore import (QAbstractNativeEventFilter, QEvent, QSize, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QShortcut, QKeySequence
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices
from pypinyin import Style, lazy_pinyin
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QPushButton,
                               QSlider, QStyle, QVBoxLayout, QWidget, QMenu,
                               QSystemTrayIcon)

import lyric_display as L
import qianyu_online as O

APP_TITLE = "千羽播放器 · 歌词"
APP_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
BUNDLE_DIR = (Path(getattr(sys, "_MEIPASS", APP_DIR)) if getattr(sys, "frozen", False)
              else APP_DIR)
PLAYER_CONFIG = APP_DIR / "qianyu.json"
ICON_PATH = BUNDLE_DIR / "assets" / "qianyu.ico"

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".ape"}

CANVAS = L.CANVAS
CARD = L.CARD
BORDER = L.BORDER
INK = L.INK
INK_SOFT = L.INK_SOFT
ACCENT = L.ACCENT
ACCENT_HOVER = L.ACCENT_HOVER
HOVER_BG = L.HOVER_BG

PLAYER_QSS = f"""
QMainWindow, QWidget#Root {{ background: {CANVAS}; }}
QLabel {{ color: {INK}; font-size: 13px; }}
QLabel#Title {{ font-size: 19px; font-weight: 600; }}
QLabel#Sub {{ color: {INK_SOFT}; font-size: 12px; }}
QLabel#NowTitle {{ font-size: 15px; font-weight: 600; }}
QLabel#Time {{ color: {INK_SOFT}; font-size: 12px; }}
QLineEdit {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 7px 12px; color: {INK}; font-size: 13px;
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QListWidget {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px;
    padding: 6px; outline: none; font-size: 13px;
}}
QListWidget::item {{
    padding: 7px 10px; border-radius: 8px; color: {INK};
}}
QListWidget::item:hover {{ background: {HOVER_BG}; }}
QListWidget::item:selected {{ background: {ACCENT}; color: #FFFFFF; }}
QPushButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 7px 14px; color: {INK}; font-size: 13px;
}}
QPushButton:hover {{ background: {HOVER_BG}; }}
QPushButton#Primary {{ background: {ACCENT}; color: #FFFFFF; border: none; }}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Round {{
    border-radius: 21px; min-width: 42px; max-width: 42px;
    min-height: 42px; max-height: 42px; padding: 0; font-size: 15px;
}}
QPushButton#Accent {{ background: {ACCENT}; color: #FFFFFF; border: none; }}
QPushButton#Accent:hover {{ background: {ACCENT_HOVER}; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {CARD}; border: 2px solid {ACCENT}; width: 13px; height: 13px;
    margin: -6px 0; border-radius: 7px;
}}
QFrame#Panel {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
"""


# --------------------------------------------------------------------------- #
# 音乐库
# --------------------------------------------------------------------------- #

NAME_SPLIT = re.compile(r"\s*[-–—]\s*")


def pinyin_key(text: str) -> tuple[str, str]:
    """(首字母串, 完整拼音) 作为排序键，中文按拼音、英文按字母。"""
    text = (text or "").strip()
    if not text:
        return ("￿", "")
    try:
        first = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER)).upper()
        full = "".join(lazy_pinyin(text)).lower()
    except Exception:
        first, full = text.upper(), text.lower()
    return (first, full)


SORT_MODES = {
    "name": ("按歌名首字母", lambda t: (t.song_key, t.song.lower())),
    "artist": ("按歌手首字母", lambda t: (t.artist_key, t.song_key)),
    "file": ("按文件名", lambda t: t.path.stem.lower()),
}


def parse_track_name(stem: str) -> tuple[str, str]:
    """从「歌名-歌手」文件名里拆出歌名和歌手。"""
    stem = stem.strip()
    parts = NAME_SPLIT.split(stem, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        song, artist = parts[0].strip(), parts[1].strip()
        # 歌手字段明显过长时，多半是歌名里本身带连字符，反过来拆
        if len(artist) > 60 and "-" in artist:
            song, artist = stem.rsplit("-", 1)
            song, artist = song.strip(), artist.strip()
        return song, artist
    return stem, ""


def scan_music(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            if p.suffix.lower() in AUDIO_EXTS:
                files.append(p)
    files.sort(key=lambda p: p.stem.lower())
    return files


def read_text_any(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return path.read_bytes().decode("utf-8", "replace")


class Track:
    __slots__ = ("path", "song", "artist", "label", "song_key", "artist_key")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.song, self.artist = parse_track_name(path.stem)
        self.label = f"{self.song}  ·  {self.artist}" if self.artist else self.song
        self.song_key = pinyin_key(self.song)
        self.artist_key = pinyin_key(self.artist)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #


class PlayerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = L.Config()
        self.settings = None
        self.online = None
        self.sources = O.SourceStore()
        self._really_quit = False
        self._tray_hint_shown = False
        self._auto_output = True
        self.tracks: list[Track] = []
        self.filtered: list[Track] = []
        self.current_index = -1
        state = self._load_player_state()
        self.music_root = Path(state.get("music_root") or (Path.home() / "Music"))
        self._saved_volume = float(state.get("volume", 0.85))
        self.play_mode = str(state.get("play_mode") or "order")
        if self.play_mode not in ("order", "shuffle", "single"):
            self.play_mode = "order"
        self._history: list[Path] = []
        self.sort_mode = str(state.get("sort_mode") or "name")
        if self.sort_mode not in SORT_MODES:
            self.sort_mode = "name"
        self._seeking = False
        self._lyric_source = ""

        self.setWindowTitle(APP_TITLE)
        self.setStyleSheet(PLAYER_QSS)
        self.resize(940, 660)
        self.setMinimumSize(760, 520)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(max(0.0, min(1.0, self._saved_volume)))

        # 蓝牙耳机连上/断开时系统默认输出会变，跟着切，否则播放器还留在旧设备上没声音
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioOutputsChanged.connect(self._on_outputs_changed)
        self._auto_output = True
        self._audio_timer = QTimer(self)
        self._audio_timer.timeout.connect(self._check_default_output)
        self._audio_timer.start(2000)

        self.fetcher = L.LyricFetcher()
        self.fetcher.loaded.connect(self._on_lyrics)

        self.lyric_win = L.LyricWindow(self.config)
        self.lyric_win.setWindowTitle("千羽 · 歌词")
        self.lyric_win.font_callback = self._adjust_lyric_font
        self.lyric_win.menu_callback = self._show_lyric_menu
        self.lyric_win.show()

        self._build_ui()
        self._wire_player()
        self._install_shortcuts()
        self._refresh_mode_button()
        self._load_library()
        self._refresh_outputs()
        self._setup_tray()
        self._hotkey_filter = PlayerHotkeyFilter(self._on_global_hotkey)
        QApplication.instance().installNativeEventFilter(self._hotkey_filter)
        self._register_hotkeys()
        QTimer.singleShot(400, self._restore_last)

    # ---------------- 播放器状态 ----------------
    def _load_player_state(self) -> dict:
        try:
            import json
            return json.loads(PLAYER_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_player_state(self) -> None:
        try:
            import json
            PLAYER_CONFIG.write_text(json.dumps({
                "music_root": str(self.music_root),
                "volume": self.audio.volume(),
                "last": str(self.tracks[self.current_index].path) if self.current_index >= 0 else "",
                "play_mode": self.play_mode,
                "sort_mode": self.sort_mode,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("千羽播放器")
        title.setObjectName("Title")
        sub = QLabel("本地音乐与在线下载，歌词窗口独立浮在桌面上")
        sub.setObjectName("Sub")
        head_col = QVBoxLayout()
        head_col.setSpacing(2)
        head_col.addWidget(title)
        head_col.addWidget(sub)
        header.addLayout(head_col)
        header.addStretch(1)
        online_btn = QPushButton("在线搜索")
        online_btn.clicked.connect(self._open_online)
        header.addWidget(online_btn)
        pick_btn = QPushButton("选择音乐文件夹")
        pick_btn.clicked.connect(self._choose_folder)
        header.addWidget(pick_btn)
        outer.addLayout(header)

        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌名或歌手…")
        self.search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search, 1)
        self.sort_combo = QComboBox()
        for key, (label, _fn) in SORT_MODES.items():
            self.sort_combo.addItem(label, key)
        idx = self.sort_combo.findData(self.sort_mode)
        self.sort_combo.setCurrentIndex(max(0, idx))
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        search_row.addWidget(self.sort_combo)
        outer.addLayout(search_row)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_item_activated)
        outer.addWidget(self.list, 1)

        panel = QFrame()
        panel.setObjectName("Panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(16, 12, 16, 14)
        pl.setSpacing(10)

        now_row = QHBoxLayout()
        self.now_title = QLabel("还没有选歌")
        self.now_title.setObjectName("NowTitle")
        self.now_sub = QLabel("")
        self.now_sub.setObjectName("Sub")
        now_col = QVBoxLayout()
        now_col.setSpacing(1)
        now_col.addWidget(self.now_title)
        now_col.addWidget(self.now_sub)
        now_row.addLayout(now_col)
        now_row.addStretch(1)
        self.lyric_btn = QPushButton("隐藏歌词")
        self.lyric_btn.clicked.connect(self._toggle_lyrics)
        now_row.addWidget(self.lyric_btn)
        settings_btn = QPushButton("歌词设置")
        settings_btn.clicked.connect(self._open_settings)
        now_row.addWidget(settings_btn)
        pl.addLayout(now_row)

        seek_row = QHBoxLayout()
        self.pos_label = QLabel("00:00")
        self.pos_label.setObjectName("Time")
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(self._on_seek_press)
        self.seek.sliderReleased.connect(self._on_seek_release)
        self.seek.sliderMoved.connect(self._on_seek_moved)
        self.dur_label = QLabel("00:00")
        self.dur_label.setObjectName("Time")
        seek_row.addWidget(self.pos_label)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.dur_label)
        pl.addLayout(seek_row)

        btn_row = QHBoxLayout()
        self.btn_prev = QPushButton("上一首")
        self.btn_prev.clicked.connect(lambda: self.play_relative(-1))
        self.btn_play = QPushButton("播放")
        self.btn_play.setObjectName("Accent")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next = QPushButton("下一首")
        self.btn_next.clicked.connect(lambda: self.play_relative(+1))
        self.btn_mode = QPushButton()
        self.btn_mode.setToolTip("切换播放顺序（Ctrl+M）")
        self.btn_mode.clicked.connect(self.cycle_mode)
        btn_row.addWidget(self.btn_prev)
        btn_row.addWidget(self.btn_play)
        btn_row.addWidget(self.btn_next)
        btn_row.addWidget(self.btn_mode)
        btn_row.addStretch(1)
        self.out_combo = QComboBox()
        self.out_combo.setMinimumWidth(150)
        self.out_combo.setMaximumWidth(190)
        self.out_combo.setToolTip("音频输出设备")
        self.out_combo.currentIndexChanged.connect(self._on_output_picked)
        btn_row.addWidget(self.out_combo)
        vol_label = QLabel("音量")
        vol_label.setObjectName("Sub")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(self.audio.volume() * 100))
        self.vol.setFixedWidth(110)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100.0))
        btn_row.addWidget(vol_label)
        btn_row.addWidget(self.vol)
        pl.addLayout(btn_row)
        outer.addWidget(panel)

        self.status = QLabel("")
        self.status.setObjectName("Sub")
        outer.addWidget(self.status)

    def _wire_player(self) -> None:
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(
            lambda _e, msg: self._set_status(f"播放出错：{msg}"))

    def _install_shortcuts(self) -> None:
        bindings = (
            ("Space", self.toggle_play),
            ("Ctrl+Right", lambda: self._nudge(+5000)),
            ("Ctrl+Left", lambda: self._nudge(-5000)),
            ("Ctrl+Up", lambda: self.play_relative(-1)),
            ("Ctrl+Down", lambda: self.play_relative(+1)),
            ("F11", self.lyric_win.toggle_fullscreen),
            ("Ctrl+M", self.cycle_mode),
        )
        for key, fn in bindings:
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(fn)

        # 方向键直接控歌：上下切歌、左右调进度
        self._arrow_shortcuts = []
        arrow_bindings = (
            ("Up", lambda: self.play_relative(-1)),
            ("Down", lambda: self.play_relative(+1)),
            ("Left", lambda: self._nudge(-5000)),
            ("Right", lambda: self._nudge(+5000)),
        )
        for key, fn in arrow_bindings:
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fn)
            self._arrow_shortcuts.append(sc)
        # 在搜索框里打字时，方向键要留给文本框移动光标
        self.search.installEventFilter(self)

    def _register_hotkeys(self) -> None:
        u = ctypes.windll.user32
        for hid, (mods, vk) in PLAYER_HOTKEYS.items():
            u.RegisterHotKey(None, hid, mods, vk)

    def _unregister_hotkeys(self) -> None:
        u = ctypes.windll.user32
        for hid in PLAYER_HOTKEYS:
            try:
                u.UnregisterHotKey(None, hid)
            except Exception:
                pass

    def _on_global_hotkey(self, hid: int) -> None:
        action = {
            101: lambda: self.play_relative(-1),
            102: lambda: self.play_relative(+1),
            103: lambda: self._nudge(-5000),
            104: lambda: self._nudge(+5000),
        }.get(hid)
        if action:
            action()

    # ---------------- 音频输出 ----------------
    AUTO_OUTPUT = "__auto__"

    def _refresh_outputs(self, *_args) -> None:
        if not hasattr(self, "out_combo"):
            return
        cur = self.audio.device()
        cur_id = cur.id() if cur and not cur.isNull() else ""
        self.out_combo.blockSignals(True)
        self.out_combo.clear()
        self.out_combo.addItem("跟随系统默认", self.AUTO_OUTPUT)
        pick = 0
        for dev in QMediaDevices.audioOutputs():
            self.out_combo.addItem(dev.description(), dev.id())
            if not self._auto_output and dev.id() == cur_id:
                pick = self.out_combo.count() - 1
        self.out_combo.setCurrentIndex(pick)
        self.out_combo.blockSignals(False)

    def _on_outputs_changed(self) -> None:
        self._refresh_outputs()
        self._check_default_output()

    def _check_default_output(self) -> None:
        """蓝牙耳机连上或断开时系统默认输出会变，跟着切过去。"""
        if not self._auto_output:
            return
        dev = QMediaDevices.defaultAudioOutput()
        if dev is None or dev.isNull():
            return
        cur = self.audio.device()
        if cur and not cur.isNull() and cur.id() == dev.id():
            return
        self._switch_output(dev, auto=True)

    def _on_output_picked(self, index: int) -> None:
        if index < 0:
            return
        want = self.out_combo.itemData(index)
        if want == self.AUTO_OUTPUT:
            self._auto_output = True
            dev = QMediaDevices.defaultAudioOutput()
            if dev and not dev.isNull():
                self._switch_output(dev, auto=True)
            return
        self._auto_output = False
        for dev in QMediaDevices.audioOutputs():
            if dev.id() == want:
                self._switch_output(dev)
                break

    def _switch_output(self, device, auto: bool = False) -> None:
        was_playing = self.player.playbackState() == QMediaPlayer.PlayingState
        position = self.player.position()
        self.audio.setDevice(device)
        if was_playing:
            self.player.setPosition(position)
            self.player.play()
        self._refresh_outputs()
        if auto:
            self._set_status(f"音频输出已切换到 {device.description()}")

    # ---------------- 托盘 ----------------
    def _setup_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else             self.style().standardIcon(QStyle.SP_MediaPlay)
        self.tray.setIcon(icon)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu()
        menu.addAction("显示主界面", self._restore_window)
        menu.addAction("显示 / 隐藏歌词", self._toggle_lyrics)
        menu.addSeparator()
        menu.addAction("播放 / 暂停", self.toggle_play)
        menu.addAction("上一首", lambda: self.play_relative(-1))
        menu.addAction("下一首", lambda: self.play_relative(+1))
        menu.addSeparator()
        menu.addAction("退出", self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._restore_window()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        self._really_quit = True
        self.close()
        QApplication.quit()

    # ---------------- 音乐库 ----------------
    def _load_library(self) -> None:
        self.tracks = [Track(p) for p in scan_music(self.music_root)]
        self._resort()
        self._set_status(f"{self.music_root} · 找到 {len(self.tracks)} 首")

    def _resort(self) -> None:
        key = SORT_MODES[self.sort_mode][1]
        try:
            self.tracks.sort(key=key)
        except Exception:
            self.tracks.sort(key=lambda t: t.path.stem.lower())
        self._apply_filter(self.search.text())

    def _apply_filter(self, text: str) -> None:
        kw = text.strip().lower()
        self.filtered = [t for t in self.tracks
                         if not kw or kw in t.label.lower()]
        self.list.clear()
        for t in self.filtered:
            item = QListWidgetItem(t.label)
            item.setData(Qt.UserRole, str(t.path))
            self.list.addItem(item)
        if self.filtered:
            self._set_status(f"{len(self.filtered)} / {len(self.tracks)} 首")

    def _on_sort_changed(self, _index: int) -> None:
        mode = self.sort_combo.currentData()
        if mode and mode != self.sort_mode:
            self.sort_mode = str(mode)
            self._resort()
            self._save_player_state()
            self._set_status(f"已按{SORT_MODES[self.sort_mode][0]}排序")

    def _open_online(self) -> None:
        if self.online is None:
            self.online = O.OnlineDialog(self.sources, self.music_root, self)
            self.online.downloaded.connect(self._on_downloaded)
        self.online.music_root = self.music_root
        self.online.show()
        self.online.raise_()
        self.online.activateWindow()

    def _on_downloaded(self, path: str) -> None:
        self._load_library()
        p = Path(path)
        row = next((i for i, t in enumerate(self.filtered) if t.path == p), -1)
        if row >= 0:
            self.list.setCurrentRow(row)
        self._set_status(f"已下载并加入列表：{p.stem}")

    def _choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择音乐文件夹", str(self.music_root))
        if d:
            self.music_root = Path(d)
            self._load_library()
            self._save_player_state()

    # ---------------- 播放 ----------------
    def _on_item_activated(self, item: QListWidgetItem) -> None:
        path = Path(item.data(Qt.UserRole))
        try:
            self.current_index = next(i for i, t in enumerate(self.tracks) if t.path == path)
        except StopIteration:
            self.current_index = -1
        self.play_track(path)

    def play_track(self, path: Path) -> None:
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        track = Track(path)
        self.now_title.setText(track.song)
        self.now_sub.setText(track.artist)
        self.setWindowTitle(f"{track.song} - {APP_TITLE}")
        self._load_lyrics_for(path, track)
        self._save_player_state()

    def toggle_play(self) -> None:
        state = self.player.playbackState()
        if state == QMediaPlayer.PlayingState:
            self.player.pause()
        elif self.player.source().isEmpty() and self.filtered:
            self._on_item_activated(self.list.item(0))
        else:
            self.player.play()

    MODE_LABEL = {"order": "顺序 ▶", "shuffle": "随机 ⇄", "single": "单曲 ↻"}

    def _refresh_mode_button(self) -> None:
        self.btn_mode.setText(self.MODE_LABEL.get(self.play_mode, "顺序 ▶"))
        self.btn_mode.setObjectName("Accent" if self.play_mode != "order" else "")
        self.btn_mode.setStyleSheet("")

    def cycle_mode(self) -> None:
        order = ["order", "shuffle", "single"]
        self.play_mode = order[(order.index(self.play_mode) + 1) % len(order)]
        self._refresh_mode_button()
        self._save_player_state()

    def play_relative(self, delta: int) -> None:
        if not self.tracks:
            return
        order = self.filtered or self.tracks
        cur = self.tracks[self.current_index] if self.current_index >= 0 else None

        if delta < 0 and self._history:
            target = self._history.pop()
            nxt = next((t for t in order if t.path == target), None) or order[0]
        elif self.play_mode == "shuffle" and len(order) > 1:
            pool = [t for t in order if t.path != (cur.path if cur else None)]
            nxt = random.choice(pool)
        else:
            try:
                base = order.index(cur) if cur else 0
            except ValueError:
                base = 0
            nxt = order[(base + delta) % len(order)]

        if cur is not None and nxt.path != cur.path:
            self._history.append(cur.path)
            del self._history[:-50]
        try:
            self.current_index = self.tracks.index(nxt)
        except ValueError:
            self.current_index = -1
        self.play_track(nxt.path)
        row = self._row_for(nxt)
        if row >= 0:
            self.list.setCurrentRow(row)

    def _row_for(self, track: Track) -> int:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == str(track.path):
                return i
        return -1

    def _nudge(self, delta_ms: int) -> None:
        pos = self.player.position() + delta_ms
        self.player.setPosition(max(0, min(pos, self.player.duration())))

    def _on_position(self, pos: int) -> None:
        dur = self.player.duration()
        if not self._seeking:
            self.seek.blockSignals(True)
            self.seek.setValue(int(pos * 1000 / dur) if dur > 0 else 0)
            self.seek.blockSignals(False)
            self.pos_label.setText(_fmt(pos))
        self.lyric_win.set_position(pos)

    def _on_duration(self, dur: int) -> None:
        self.dur_label.setText(_fmt(dur))

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setText("暂停" if playing else "播放")
        self.lyric_win.set_status("" if playing or self.player.source().isEmpty()
                                  else "已暂停")

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.EndOfMedia:
            if self.play_mode == "single":
                self.player.setPosition(0)
                self.player.play()
            else:
                self.play_relative(+1)

    def _on_seek_press(self) -> None:
        self._seeking = True

    def _on_seek_moved(self, value: int) -> None:
        dur = self.player.duration()
        if dur > 0:
            self.pos_label.setText(_fmt(int(dur * value / 1000)))

    def _on_seek_release(self) -> None:
        self._seeking = False
        dur = self.player.duration()
        if dur > 0:
            self.player.setPosition(int(dur * self.seek.value() / 1000))

    # ---------------- 歌词 ----------------
    def _load_lyrics_for(self, path: Path, track: Track) -> None:
        lrc = path.with_suffix(".lrc")
        if lrc.exists():
            ly = L.parse_lrc(read_text_any(lrc))
            ly = L.clean_lyrics(ly, track.song, track.artist)
            if ly.lines:
                self.lyric_win.set_lyrics(ly.lines)
                self._lyric_source = "本地 .lrc"
                self._set_status(f"歌词来自本地文件：{lrc.name}")
                return
        self.lyric_win.set_lyrics([])
        self.lyric_win.set_status(f"正在获取歌词：{track.song}")
        self._lyric_source = "网络"
        self.fetcher.request(track.song, track.artist)

    def _on_lyrics(self, song: str, artist: str, lyrics) -> None:
        if not self.tracks:
            return
        track = self.tracks[self.current_index] if self.current_index >= 0 else None
        if track and track.song != song:
            return
        if not lyrics or not lyrics.lines:
            self.lyric_win.set_lyrics([])
            self.lyric_win.set_status(f"没找到歌词：{song}")
            self._set_status("没找到歌词，可以放一个同名 .lrc 文件到歌曲旁边")
            return
        self.lyric_win.set_lyrics(lyrics.lines)
        self.lyric_win.set_position(self.player.position())
        self._set_status(f"歌词来源：{lyrics.source or '网络'} · {lyrics.matched or song}")

    # ---------------- 歌词窗口 ----------------
    def _adjust_lyric_font(self, delta: int) -> None:
        key = "fullscreen_font_size" if self.lyric_win.is_fullscreen() else "font_size"
        lo, hi = (40, 320) if key == "fullscreen_font_size" else (18, 240)
        self.config[key] = max(lo, min(hi, int(self.config[key]) + delta))
        self.config.save()
        self.lyric_win._path_cache.clear()
        self.lyric_win._pix_cache.clear()
        self.lyric_win._apply_geometry()
        self.lyric_win.update()

    def _toggle_lyrics(self) -> None:
        if self.lyric_win.isVisible():
            self.lyric_win.hide()
            self.lyric_btn.setText("显示歌词")
        else:
            self.lyric_win.show()
            self.lyric_win._apply_geometry()
            self.lyric_win.update()
            self.lyric_btn.setText("隐藏歌词")

    def _show_lyric_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("歌词设置…", self._open_settings)
        menu.addAction("全屏 / 窗口  Ctrl+Alt+F" if False else "全屏歌词", self.lyric_win.toggle_fullscreen)
        menu.addSeparator()
        menu.addAction("放大字号", lambda: self._adjust_lyric_font(+4))
        menu.addAction("缩小字号", lambda: self._adjust_lyric_font(-4))
        menu.addSeparator()
        menu.addAction("隐藏歌词窗口", self._toggle_lyrics)
        menu.popup(pos)

    def _open_settings(self) -> None:
        if self.settings is None:
            self.settings = L.SettingsDialog(self.config, self)
            self.settings.changed.connect(self._on_lyric_setting)
            self.settings.align_requested.connect(lambda _d: None)
        self.settings._sync_from_config()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def _on_lyric_setting(self, key: str, value) -> None:
        self.lyric_win._path_cache.clear()
        self.lyric_win._pix_cache.clear()
        if key == "fullscreen":
            if value:
                self.lyric_win.enter_fullscreen()
            else:
                self.lyric_win.exit_fullscreen()
        else:
            self.lyric_win._apply_geometry()
        self.lyric_win.update()

    # ---------------- 杂项 ----------------
    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _restore_last(self) -> None:
        last = self._load_player_state().get("last") or ""
        if not last:
            return
        p = Path(last)
        if not p.exists():
            return
        for i, t in enumerate(self.tracks):
            if t.path == p:
                self.current_index = i
                row = self._row_for(t)
                if row >= 0:
                    self.list.setCurrentRow(row)
                self.now_title.setText(t.song)
                self.now_sub.setText(t.artist)
                break

    def eventFilter(self, obj, event):
        if obj is getattr(self, "search", None):
            if event.type() == QEvent.FocusIn:
                for sc in getattr(self, "_arrow_shortcuts", []):
                    sc.setEnabled(False)
            elif event.type() == QEvent.FocusOut:
                for sc in getattr(self, "_arrow_shortcuts", []):
                    sc.setEnabled(True)
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        if not self._really_quit:
            tray_ok = bool(getattr(self, "tray", None)) and self.tray.isVisible()
            if tray_ok:
                event.ignore()
                self.hide()
                if not self._tray_hint_shown:
                    self._tray_hint_shown = True
                    self.tray.showMessage(APP_TITLE, "已缩到状态栏，右键图标可以退出",
                                          QSystemTrayIcon.Information, 3000)
                return
            # 系统托盘不可用（某些精简系统），那就老老实实退出，别留个看不见的进程
            self._really_quit = True
        self._unregister_hotkeys()
        self._save_player_state()
        self.player.stop()
        try:
            self.lyric_win.close()
        except Exception:
            pass
        event.accept()


WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT = 0x26, 0x28, 0x25, 0x27

# 焦点不在播放器时也要能切歌，所以走系统级热键，ID 避开 lyric_display 用的 1~10
PLAYER_HOTKEYS = {
    101: (MOD_CONTROL, VK_UP),
    102: (MOD_CONTROL, VK_DOWN),
    103: (MOD_CONTROL, VK_LEFT),
    104: (MOD_CONTROL, VK_RIGHT),
}


class PlayerHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = wt.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and int(msg.wParam) in PLAYER_HOTKEYS:
                    self._on_hotkey(int(msg.wParam))
                    return True, 0
        except Exception:
            pass
        return False, 0


PLAYER_MUTEX = r"Global\QianyuPlayer_Mutex_v1"


def _single_instance_ok() -> bool:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW(None, False, PLAYER_MUTEX)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _fmt(ms: int) -> str:
    ms = max(0, int(ms))
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}"


def _wake_existing_instance() -> None:
    """程序缩在托盘里时再次双击图标，把已有窗口叫到前台。"""
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    me = str(Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])).lower()
    targets: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = k.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                if buf.value.lower() == me:
                    targets.append(hwnd)
        finally:
            k.CloseHandle(handle)
        return True

    u.EnumWindows(cb, 0)
    if targets:
        u.ShowWindow(targets[0], 9)  # SW_RESTORE
        u.SetForegroundWindow(targets[0])


def main() -> int:
    if not _single_instance_ok():
        _wake_existing_instance()
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyleSheet(PLAYER_QSS)
    app.setQuitOnLastWindowClosed(False)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = PlayerWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
