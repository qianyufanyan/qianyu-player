# -*- coding: utf-8 -*-
"""
酷狗桌面歌词放大器

酷狗自带的桌面歌词放大倍率有限，这个程序用更大的自由度的方式显示歌词：
无边框、可置顶、字号随便放多大，还可以整屏显示；跟随酷狗当前播放的歌曲自动切换。

歌词来源：酷狗官方歌词接口（按“歌名 歌手”匹配，优先用歌曲 hash 精确定位），
失败时回退网易云音乐与 LRCLIB。

运行环境：Windows + Python 3.10+ + PySide6 + requests
"""

from __future__ import annotations

import base64
import bisect
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from PySide6.QtCore import (QAbstractNativeEventFilter, QPoint, QPointF, QRectF, Qt,
                            QTimer, Signal, QObject)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QIcon,
                           QPainter, QPainterPath, QPen, QPixmap, QPalette)
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QFontComboBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSlider, QSpinBox, QSystemTrayIcon, QVBoxLayout,
                               QWidget)

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent          # exe 所在目录，配置和缓存放这
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))     # 打包进 exe 的资源目录
else:
    APP_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = APP_DIR

CONFIG_PATH = APP_DIR / "config.json"
CACHE_DIR = APP_DIR / "歌词缓存"
BRIDGE_SCRIPT = BUNDLE_DIR / "tools" / "smtc_bridge.ps1"

APP_TITLE = "酷狗歌词放大器"
SINGLE_INSTANCE_MUTEX = "Global\\KugouLyricDisplay_Mutex_v1"

_DEBUG = bool(os.environ.get("LYRIC_DEBUG"))


def _debug(msg: str) -> None:
    if not _DEBUG:
        return
    try:
        with open(APP_DIR / "debug.log", "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


DEFAULT_CONFIG = {
    # 外观
    "font_size": 58,
    "fullscreen_font_size": 116,
    "font_family": "Microsoft YaHei UI",
    "side_ratio": 1.0,
    "width_ratio": 0.88,
    "bg_alpha": 0.0,
    "text_color": "#FFFFFF",
    "active_color": "#FFD34E",
    "dim_color": "#B7C4D2",
    "dim_alpha": 0.46,
    "stroke_alpha": 0.86,
    "theme": "经典白字",
    # 行为
    "x": None,
    "y": None,
    "locked": False,
    "fullscreen": False,
    "visible": True,
    "offset_ms": 0,
    "display_mode": "triple",
    "filter_meta": True,
    "show_progress": True,
    "auto_click_through_fullscreen": True,
}

THEMES = {
    "经典白字": ("#FFFFFF", "#FFD34E", "#B7C4D2"),
    "暖橙": ("#FFF6EE", "#F08B5F", "#D7B49A"),
    "霓虹青": ("#EAFDFF", "#3FE0D0", "#8FB9C4"),
    "粉紫": ("#FFF0F8", "#FF8FC8", "#C3A6C8"),
    "薄荷绿": ("#F0FFF6", "#66E29B", "#A3C9B4"),
}

# Claude 风格配色
CANVAS = "#FAF9F5"
CARD = "#FFFFFF"
BORDER = "#E8E4DA"
INK = "#1F1E1D"
INK_SOFT = "#73706A"
ACCENT = "#C96442"
ACCENT_HOVER = "#B55636"
HOVER_BG = "#F5F3EC"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class Config:
    def __init__(self) -> None:
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in DEFAULT_CONFIG:
                        self.data[k] = v
                if "display_mode" not in raw and not raw.get("show_context", True):
                    self.data["display_mode"] = "single"
                if abs(float(self.data.get("side_ratio", 1.0) or 1.0) - 0.52) < 1e-6:
                    self.data["side_ratio"] = 1.0
                    self.save()
        except Exception:
            pass

    def save(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULT_CONFIG.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value


# --------------------------------------------------------------------------- #
# 读取酷狗当前播放的歌曲
# --------------------------------------------------------------------------- #

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
_kernel32_le = ctypes.WinDLL("kernel32", use_last_error=True)
_gdi32 = ctypes.windll.gdi32
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wt.BOOL

HWND_TOPMOST = wt.HWND(-1)
HWND_NOTOPMOST = wt.HWND(-2)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER, SWP_NOZORDER = (
    0x0001, 0x0002, 0x0010, 0x0200, 0x0004)

TITLE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*酷狗音乐\s*$")
SONG_SPLIT_RE = re.compile(r"\s*[-–—]\s*")


class KuGouTitleWatcher:
    """轮询酷狗窗口标题，得到 (歌名, 歌手)。只用窗口类名 + 标题后缀识别，不碰子进程。"""

    def __init__(self) -> None:
        self._cache: tuple[str, str] | None = None
        self._cache_raw = ""

    def raw_title(self) -> str:
        found: list[str] = []
        buf = ctypes.create_unicode_buffer(513)
        cls = ctypes.create_unicode_buffer(64)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _lparam):
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "kugou_ui":
                return True
            user32.GetWindowTextW(hwnd, buf, 512)
            text = buf.value
            if text and " - 酷狗音乐" in text:
                found.append(text)
            return True

        try:
            user32.EnumWindows(_cb, 0)
        except Exception:
            pass
        if not found:
            return ""
        found.sort(key=len, reverse=True)
        return found[0].strip()

    def now_playing(self) -> tuple[str, str] | None:
        raw = self.raw_title()
        if raw == self._cache_raw:
            return self._cache
        self._cache_raw = raw
        if not raw:
            self._cache = None
            return None
        body = TITLE_SUFFIX_RE.sub("", raw).strip()
        if not body:
            self._cache = None
            return None
        parts = SONG_SPLIT_RE.split(body, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            self._cache = (parts[0].strip(), parts[1].strip())
        else:
            self._cache = (body.strip(), "")
        return self._cache


class SmtcBridge:
    """常驻 PowerShell 进程，读取 Windows 媒体会话里酷狗的播放状态。"""

    def __init__(self) -> None:
        self.status = "unknown"
        self.updated_at = 0.0
        self.position = 0.0
        self.duration = 0.0
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = False
        self._restarts = 0

    def start(self) -> None:
        if not BRIDGE_SCRIPT.exists():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop and self._restarts < 20:
            try:
                self._proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(BRIDGE_SCRIPT)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000)
            except Exception:
                time.sleep(5)
                self._restarts += 1
                continue
            proc = self._proc
            assert proc.stdout is not None
            while not self._stop:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.decode("utf-8", "replace").strip())
                except Exception:
                    continue
                with self._lock:
                    self.status = str(data.get("status") or "none")
                    self.updated_at = time.monotonic()
                    try:
                        self.position = float(data.get("pos") or 0)
                        self.duration = float(data.get("dur") or 0)
                    except (TypeError, ValueError):
                        self.position, self.duration = 0.0, 0.0
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            if self._stop:
                break
            self._restarts += 1
            time.sleep(3)

    def timeline(self) -> tuple[float, float]:
        with self._lock:
            return self.position, self.duration

    def playing(self) -> bool | None:
        with self._lock:
            status, ts = self.status, self.updated_at
        if time.monotonic() - ts > 4:
            return None
        if status == "Playing":
            return True
        if status in ("Paused", "Stopped", "none", "None"):
            return False
        return None


class PlayClock:
    """没有精确时间轴可用时，用“从本曲开始计时 + 暂停扣时”逼近播放位置。"""

    def __init__(self) -> None:
        self.key: str = ""
        self._start: float | None = None
        self._paused_total = 0.0
        self._pause_at: float | None = None
        self.waiting_first_play = False
        self._ext_ok = False
        self._ext_pos = 0.0
        self._ext_ts = 0.0

    def want_song(self, key: str, playing: bool | None) -> bool:
        if key == self.key:
            return False
        self.key = key
        now = time.monotonic()
        self._start = None
        self._paused_total = 0.0
        self._pause_at = None
        self.waiting_first_play = True
        self._ext_ok = False
        self._ext_pos = 0.0
        if playing is True:
            self._start = now
            self.waiting_first_play = False
        return True

    def on_playing(self, playing: bool | None) -> None:
        now = time.monotonic()
        if playing is True:
            if self._start is None:
                self._start = now
                self.waiting_first_play = False
            if self._pause_at is not None:
                self._paused_total += now - self._pause_at
                self._pause_at = None
        elif playing is False:
            if self._pause_at is None:
                self._pause_at = now

    def feed_external(self, pos_s: float, dur_s: float) -> None:
        """酷狗若通过系统媒体会话提供时间轴，就用它校正本地计时。"""
        if dur_s > 5.0 and 0.0 <= pos_s <= dur_s:
            self._ext_ok = True
            self._ext_pos = pos_s
            self._ext_ts = time.monotonic()
        else:
            self._ext_ok = False

    def position_ms(self, offset_ms: int = 0) -> int | None:
        if self._ext_ok:
            base = self._ext_pos
            if self._pause_at is None and self._start is not None:
                base += time.monotonic() - self._ext_ts
            return max(0, int(base * 1000) + offset_ms)
        if self._start is None:
            return None
        now = time.monotonic()
        paused = self._paused_total
        if self._pause_at is not None:
            paused += now - self._pause_at
        return max(0, int((now - self._start - paused) * 1000) + offset_ms)


# --------------------------------------------------------------------------- #
# 歌词：解析与获取
# --------------------------------------------------------------------------- #


@dataclass
class LyricLine:
    t: int
    text: str


@dataclass
class Lyrics:
    lines: list[LyricLine]
    duration: int = 0
    source: str = ""
    matched: str = ""


LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
META_LINE_RE = re.compile(
    r"^\s*(作词|作曲|编曲|制作人|制作|监制|出品|发行|混音|录音|母带|吉他|贝斯|鼓|和声|"
    r"统筹|企划|策划|词|曲|OP|SP|原唱|翻唱|演唱|歌手|专辑|"
    r"歌词|资料来源|本歌词|编辑|版权|发行公司|唱片公司|制作公司|录音室)\s*[:：]",
    re.IGNORECASE)

META_LINE_EN_RE = re.compile(
    r"^\s*(written|composed|lyrics?|produced|arranged|mixed|recorded|mastered|"
    r"music|words|vocals?|performed|artist|title|album|label|publishing|"
    r"additional|engineers?|producer|songwriter|copyright|制作|出品)\s*(by)?\s*[:：]",
    re.IGNORECASE)


def parse_lrc(text: str) -> Lyrics:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    offset = 0
    m = re.search(r"\[offset:\s*([+-]?\d+)\]", text, re.IGNORECASE)
    if m:
        try:
            offset = int(m.group(1))
        except ValueError:
            offset = 0

    entries: list[tuple[int, str]] = []
    for raw in text.split("\n"):
        stamps = list(LRC_TIME_RE.finditer(raw))
        if not stamps:
            continue
        body = raw[stamps[-1].end():].strip()
        if not body:
            continue
        for st in stamps:
            minute = int(st.group(1))
            second = int(st.group(2))
            frac = st.group(3) or "0"
            frac_ms = int((frac + "00")[:3]) if len(frac) < 3 else int(frac[:3])
            ms = minute * 60000 + second * 1000 + frac_ms
            entries.append((max(0, ms + offset), body))

    entries.sort(key=lambda x: x[0])
    lines: list[LyricLine] = []
    for ms, body in entries:
        if lines and lines[-1].t == ms:
            if body not in lines[-1].text:
                lines[-1].text += " " + body
            continue
        lines.append(LyricLine(ms, body))
    return Lyrics(lines=lines)


def clean_lyrics(ly: Lyrics, song: str, artist: str) -> Lyrics:
    if not ly.lines:
        return ly
    song_n = re.sub(r"\s+", "", song).lower()
    artist_n = re.sub(r"\s+", "", artist).lower()

    def is_junk(text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        if META_LINE_RE.match(t) or META_LINE_EN_RE.match(t):
            return True
        if re.match(r"^\s*(written|composed|produced|arranged|mixed|recorded|mastered)\s+by",
                    t, re.IGNORECASE):
            return True
        flat = re.sub(r"[\s\-–—_·・]+", "", t).lower()
        if song_n and artist_n:
            if flat == f"{song_n}{artist_n}" or flat == f"{artist_n}{song_n}":
                return True
        if song_n and flat == song_n and len(t) <= len(song) + 2:
            return True
        if re.fullmatch(r"[\s\-–—_.·・]+", t):
            return True
        return False

    lines = [ln for ln in ly.lines if not is_junk(ln.text)]
    return Lyrics(lines=lines, duration=ly.duration, source=ly.source, matched=ly.matched)


class LyricFetcher(QObject):
    """按“歌名 + 歌手”取歌词，内存 + 磁盘缓存。"""

    loaded = Signal(str, str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mem: dict[str, Lyrics | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA})
        CACHE_DIR.mkdir(exist_ok=True)

    @staticmethod
    def _key(song: str, artist: str) -> str:
        raw = f"{song.strip().lower()}|{artist.strip().lower()}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def _cache_path(self, song: str, artist: str) -> Path:
        return CACHE_DIR / f"{self._key(song, artist)}.json"

    def _load_cache(self, song: str, artist: str) -> Lyrics | None:
        p = self._cache_path(song, artist)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            lines = [LyricLine(int(l["t"]), str(l["text"])) for l in raw["lines"]]
            if not lines:
                return None
            return Lyrics(lines=lines, duration=int(raw.get("duration") or 0),
                          source=str(raw.get("source") or ""),
                          matched=str(raw.get("matched") or ""))
        except Exception:
            return None

    def _save_cache(self, song: str, artist: str, ly: Lyrics) -> None:
        try:
            self._cache_path(song, artist).write_text(json.dumps({
                "song": song, "artist": artist, "source": ly.source,
                "matched": ly.matched, "duration": ly.duration,
                "lines": [{"t": l.t, "text": l.text} for l in ly.lines],
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def request(self, song: str, artist: str) -> None:
        key = f"{song}|{artist}"
        if key in self._mem:
            self.loaded.emit(song, artist, self._mem[key])
            return
        cached = self._load_cache(song, artist)
        if cached:
            cached = clean_lyrics(cached, song, artist)
        if cached and cached.lines:
            self._mem[key] = cached
            self.loaded.emit(song, artist, cached)
            return

        def work() -> None:
            ly: Lyrics | None = None
            try:
                ly = self._fetch_all(song, artist)
            except Exception:
                ly = None
            if ly is not None and ly.lines:
                ly = clean_lyrics(ly, song, artist)
                if ly.lines:
                    self._save_cache(song, artist, ly)
                else:
                    ly = None
            self._mem[key] = ly
            self.loaded.emit(song, artist, ly)

        threading.Thread(target=work, daemon=True).start()

    def forget(self, song: str, artist: str) -> None:
        self._mem.pop(f"{song}|{artist}", None)
        try:
            self._cache_path(song, artist).unlink(missing_ok=True)
        except Exception:
            pass

    def _fetch_all(self, song: str, artist: str) -> Lyrics | None:
        for fn in (self._from_kugou, self._from_kugou_keyword,
                   self._from_netease, self._from_lrclib):
            try:
                ly = fn(song, artist)
            except Exception:
                ly = None
            if ly and ly.lines:
                return ly
        return None

    # ---------------- 酷狗 ----------------
    def _kugou_song_search(self, song: str, artist: str) -> list[dict]:
        r = self._session.get(
            "https://songsearch.kugou.com/song_search_v2",
            params={"keyword": f"{song} {artist}".strip(), "page": 1, "pagesize": 10,
                    "platform": "WebFilter", "userid": "-1", "clientver": "",
                    "iscorrection": 1, "privilege_filter": 0, "filter": 10,
                    "token": "", "appid": 1018, "signature": "", "area_code": 1},
            timeout=8, headers={"Referer": "https://www.kugou.com/"})
        return ((r.json().get("data") or {}).get("lists") or [])

    @staticmethod
    def _pick_song(cands: list[dict], song: str, artist: str) -> dict | None:
        song_n = re.sub(r"[\s\-–—_]+", "", song).lower()
        artist_n = re.sub(r"[\s\-–—_]+", "", artist).lower()

        def score(item: dict) -> float:
            name = re.sub(r"[\s\-–—_]+", "", str(item.get("SongName", ""))).lower()
            singer = re.sub(r"[\s\-–—_]+", "", str(item.get("SingerName", ""))).lower()
            s = 0.0
            if song_n and name == song_n:
                s += 6
            elif song_n and (song_n in name or name in song_n):
                s += 3
            if artist_n and artist_n in singer:
                s += 5
            elif artist_n and any(a and a in singer for a in artist_n.split("、")):
                s += 3
            return s

        best = max(cands, key=score, default=None)
        return best if best and score(best) > 0 else None

    def _kugou_lyric_search(self, keyword: str, duration_ms: str = "",
                            song_hash: str = "") -> list[dict]:
        r = self._session.get(
            "https://krcs.kugou.com/search",
            params={"ver": "1", "man": "yes", "client": "mobi",
                    "keyword": keyword, "duration": duration_ms, "hash": song_hash},
            timeout=8, headers={"Referer": "https://www.kugou.com/"})
        return (r.json().get("candidates") or [])

    def _kugou_download(self, cand: dict) -> str:
        r = self._session.get(
            "https://lyrics.kugou.com/download",
            params={"ver": "1", "client": "pc", "id": cand.get("id"),
                    "accesskey": cand.get("accesskey"), "fmt": "lrc", "charset": "utf8"},
            timeout=10, headers={"Referer": "https://www.kugou.com/"})
        raw = base64.b64decode(r.json().get("content") or b"")
        return raw.decode("utf-8", "replace")

    def _kugou_pick_lyric(self, cands: list[dict], song: str, artist: str) -> dict | None:
        song_n = re.sub(r"[\s\-–—_]+", "", song).lower()
        artist_n = re.sub(r"[\s\-–—_]+", "", artist).lower()

        def score(c: dict) -> float:
            s = 0.0
            name = re.sub(r"[\s\-–—_]+", "", str(c.get("song", ""))).lower()
            singer = re.sub(r"[\s\-–—_]+", "", str(c.get("singer", ""))).lower()
            if song_n and (name == song_n or song_n in name or name in song_n):
                s += 5
            if artist_n and (artist_n in singer or singer in artist_n):
                s += 4
            dur = int(c.get("duration") or 0)
            if 60_000 < dur < 900_000:
                s += 1
            s += (int(c.get("score") or 0)) / 100.0
            return s

        return max(cands, key=score, default=None)

    def _from_kugou(self, song: str, artist: str) -> Lyrics | None:
        songs = self._kugou_song_search(song, artist)
        picked = self._pick_song(songs, song, artist)
        if not picked:
            return None
        duration_ms = str(int(picked.get("Duration") or 0) * 1000)
        cands = self._kugou_lyric_search(
            str(picked.get("SongName") or song), duration_ms,
            str(picked.get("FileHash") or ""))
        if not cands:
            return None
        cand = self._kugou_pick_lyric(cands, song, artist)
        if not cand:
            return None
        ly = parse_lrc(self._kugou_download(cand))
        ly.source = "kugou"
        ly.matched = f"{cand.get('song')} - {cand.get('singer')}"
        ly.duration = int(cand.get("duration") or 0)
        return ly

    def _from_kugou_keyword(self, song: str, artist: str) -> Lyrics | None:
        for kw in (f"{song} {artist}".strip(), f"{song} - {artist}".strip(), song):
            cands = self._kugou_lyric_search(kw)
            if not cands:
                continue
            cand = self._kugou_pick_lyric(cands, song, artist)
            if not cand:
                continue
            ly = parse_lrc(self._kugou_download(cand))
            if ly.lines:
                ly.source = "kugou"
                ly.matched = f"{cand.get('song')} - {cand.get('singer')}"
                ly.duration = int(cand.get("duration") or 0)
                return ly
        return None

    # ---------------- 网易云 ----------------
    def _from_netease(self, song: str, artist: str) -> Lyrics | None:
        payload = self._session.post(
            "https://music.163.com/api/search/get/web",
            data={"s": f"{song} {artist}".strip(), "type": 1, "offset": 0,
                  "total": "true", "limit": 8},
            timeout=8, headers={"Referer": "https://music.163.com/"}).json()
        songs = ((payload.get("result") or {}).get("songs") or [])
        if not songs:
            return None
        song_n = re.sub(r"[\s\-–—_]+", "", song).lower()
        artist_n = re.sub(r"[\s\-–—_]+", "", artist).lower()
        best, best_score = None, 0.0
        for s in songs:
            name = re.sub(r"[\s\-–—_]+", "", str(s.get("name", ""))).lower()
            singers = re.sub(r"[\s\-–—_]+", "",
                             ",".join(a.get("name", "") for a in s.get("artists", []))).lower()
            sc = 0.0
            if song_n and (name == song_n or song_n in name):
                sc += 4
            if artist_n and (artist_n in singers or singers in artist_n):
                sc += 4
            if sc > best_score:
                best, best_score = s, sc
        if not best or best_score <= 0:
            return None
        ly = self._session.get(
            "https://music.163.com/api/song/lyric",
            params={"id": best["id"], "lv": 1, "kv": 1, "tv": -1},
            timeout=8, headers={"Referer": "https://music.163.com/"}).json()
        text = ((ly.get("lrc") or {}).get("lyric") or "")
        if not text.strip():
            return None
        out = parse_lrc(text)
        out.source = "netease"
        out.matched = f"{best.get('name')} - {','.join(a.get('name','') for a in best.get('artists', []))}"
        out.duration = int(best.get("duration") or 0)
        return out

    # ---------------- LRCLIB ----------------
    def _from_lrclib(self, song: str, artist: str) -> Lyrics | None:
        r = self._session.get(
            "https://lrclib.net/api/search",
            params={"track_name": song, "artist_name": artist},
            timeout=10, headers={"User-Agent": "KugouLyricDisplay/1.0"})
        items = r.json()
        if not isinstance(items, list) or not items:
            return None
        for item in items:
            text = item.get("syncedLyrics") or ""
            if not text:
                continue
            out = parse_lrc(text)
            if out.lines:
                out.source = "lrclib"
                out.matched = f"{item.get('trackName')} - {item.get('artistName')}"
                out.duration = int((item.get("duration") or 0) * 1000)
                return out
        return None


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# --------------------------------------------------------------------------- #
# 全局热键
# --------------------------------------------------------------------------- #

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT = 0x26, 0x28, 0x25, 0x27
VK_F, VK_H, VK_L, VK_R, VK_0 = 0x46, 0x48, 0x4C, 0x52, 0x30

HOTKEYS = {
    1: ("font_up", MOD_CONTROL | MOD_ALT, VK_UP),
    2: ("font_down", MOD_CONTROL | MOD_ALT, VK_DOWN),
    3: ("sync_back", MOD_CONTROL | MOD_ALT, VK_LEFT),
    4: ("sync_forward", MOD_CONTROL | MOD_ALT, VK_RIGHT),
    5: ("toggle_lock", MOD_CONTROL | MOD_ALT, VK_L),
    6: ("toggle_visible", MOD_CONTROL | MOD_ALT, VK_H),
    7: ("sync_reset", MOD_CONTROL | MOD_ALT, VK_0),
    8: ("toggle_fullscreen", MOD_CONTROL | MOD_ALT, VK_F),
    9: ("line_prev", MOD_CONTROL | MOD_ALT | MOD_SHIFT, VK_LEFT),
    10: ("line_next", MOD_CONTROL | MOD_ALT | MOD_SHIFT, VK_RIGHT),
}


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = wt.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and int(msg.wParam) in HOTKEYS:
                    self._on_hotkey(int(msg.wParam))
                    return True, 0
        except Exception:
            pass
        return False, 0


# --------------------------------------------------------------------------- #
# 歌词窗口
# --------------------------------------------------------------------------- #


class LyricWindow(QWidget):
    def __init__(self, config: Config) -> None:
        super().__init__(None)
        self.config = config
        self.lines: list[LyricLine] = []
        self.times: list[int] = []
        self.position_ms: int | None = None
        self.status_text = "正在寻找酷狗…"
        self.hint_text = ""
        self._path_cache: dict = {}
        self._pix_cache: dict = {}
        self._cur_px = 0.0
        self._click_through = False
        self._fps_n = 0
        self._paint_ms = 0.0
        self._fps_t = time.monotonic()
        self._drag_origin: QPoint | None = None
        self._win_origin: QPoint | None = None
        self.font_callback = None
        self.fullscreen_changed = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle(APP_TITLE)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_geometry()

    # ---------------- 几何 ----------------
    def _screen(self):
        scr = self.screen() or QGuiApplication.primaryScreen()
        return scr.availableGeometry() if scr else QRectF(0, 0, 1920, 1080).toRect()

    def is_fullscreen(self) -> bool:
        return bool(self.config["fullscreen"])

    def _stroke_pad(self, px: float) -> float:
        """位图四周留的描边余量，布局和渲染必须用同一个值。"""
        if float(self.config["stroke_alpha"]) <= 0.02:
            return 2.0
        return max(3.0, px * 0.26) + 2.0

    def _content_layout(self):
        """按歌词内容算出真正可用的主字号和需要的窗口宽高。

        先把字号缩到最长一行放得下，再缩到最高的布局放得下，
        保证绘制时用的字号和窗口尺寸是同一套数字，不会出现被裁掉的情况。
        """
        base = self.current_font_px()
        geo = self._screen()
        mode = self.display_mode()
        ratio = max(0.4, min(1.0, float(self.config["side_ratio"])))
        max_w = geo.width() - 8
        max_h = geo.height() * 0.94

        if not self.lines:
            factor = {"single": 2.1, "double": 3.2, "triple": 4.0}[mode]
            return base, int(geo.width() * float(self.config["width_ratio"])), max(110, int(base * factor))

        px = base
        widest = need_w = need_h = 0.0
        for _ in range(6):
            pad = self._stroke_pad(px)
            fm = QFontMetricsF(self._font(px))
            widths = sorted(fm.horizontalAdvance(l.text) for l in self.lines)
            widest = widths[min(len(widths) - 1, int(len(widths) * 0.92))]
            need_w = widest + pad * 2 + 12
            main_h = fm.height() + pad * 2

            if mode == "double":
                side_px_local = px * ratio
                fsm = QFontMetricsF(self._font(side_px_local, bold=False))
                side_h = fsm.height() + self._stroke_pad(side_px_local) * 2
                need_h = main_h + side_h + px * 0.42 + 8
            elif mode == "triple":
                side_px_local = px * ratio
                fsm = QFontMetricsF(self._font(side_px_local, bold=False))
                side_h = fsm.height() + self._stroke_pad(side_px_local) * 2
                need_h = main_h + 2 * side_h + 2 * px * 0.42 + 8
            else:
                need_h = main_h + 8

            if need_w > max_w:
                px = max(12.0, px * max_w / need_w)
                continue
            if need_h > max_h:
                px = max(12.0, px * max_h / need_h)
                continue
            break

        return px, int(min(max_w, need_w)), int(min(max_h, need_h))

    def _apply_geometry(self) -> None:
        geo = self._screen()
        px, width, height = self._content_layout()
        self._cur_px = px
        if self.is_fullscreen():
            y = geo.y() + (geo.height() - height) // 2
            self.setGeometry(geo.center().x() - width // 2, y, width, height)
            return
        x, y = self.config["x"], self.config["y"]
        if x is None or y is None:
            x = geo.center().x() - width // 2
            y = geo.bottom() - height - 24
        x = max(geo.left(), min(int(x), geo.right() - 200))
        y = max(geo.top(), min(int(y), geo.bottom() - 80))
        self.setGeometry(int(x), int(y), width, height)

    def enter_fullscreen(self) -> None:
        self.config["fullscreen"] = True
        self.config.save()
        self._apply_geometry()
        if self.config["auto_click_through_fullscreen"]:
            self._apply_click_through(True)
        self.update()
        if callable(self.fullscreen_changed):
            self.fullscreen_changed(True)

    def exit_fullscreen(self) -> None:
        self.config["fullscreen"] = False
        self.config.save()
        self._apply_geometry()
        self._apply_click_through(bool(self.config["locked"]))
        self.update()
        if callable(self.fullscreen_changed):
            self.fullscreen_changed(False)

    def toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def _apply_click_through(self, on: bool) -> None:
        if self._click_through == on:
            return
        self._click_through = on
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x20
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)

    # ---------------- 数据 ----------------
    def set_lyrics(self, lines: list[LyricLine]) -> None:
        self.lines = lines
        self.times = [l.t for l in lines]
        self._path_cache.clear()
        self._pix_cache.clear()
        self._apply_geometry()
        self.update()

    def set_status(self, text: str) -> None:
        if text != self.status_text:
            self.status_text = text
            self.update()

    def set_hint(self, text: str) -> None:
        if text != self.hint_text:
            self.hint_text = text
            self.update()

    def set_position(self, ms: int | None) -> None:
        if ms == self.position_ms:
            return
        self.position_ms = ms
        self.update()

    def current_index(self) -> int:
        if not self.times:
            return -1
        if self.position_ms is None:
            return 0
        idx = bisect.bisect_right(self.times, self.position_ms) - 1
        return max(0, min(idx, len(self.lines) - 1))

    def display_mode(self) -> str:
        mode = str(self.config["display_mode"])
        return mode if mode in ("single", "double", "triple") else "triple"

    def _build_entries(self, idx: int, main_px: float, side_px: float):
        """按显示模式排出要画的几行：单行 / 双排（当前 + 下一句）/ 三行。"""
        mode = self.display_mode()
        dim_alpha = max(0.0, min(1.0, float(self.config["dim_alpha"])))
        dim = QColor(self.config["dim_color"])
        text = QColor(self.config["text_color"])
        entries: list[tuple[int, float, QColor, float]] = []
        if mode == "triple" and idx - 1 >= 0:
            entries.append((idx - 1, side_px, dim, dim_alpha))
        entries.append((idx, main_px, text, 1.0))
        if idx + 1 < len(self.lines):
            if mode == "double":
                entries.append((idx + 1, side_px, dim,
                                min(1.0, dim_alpha + 0.22)))
            elif mode == "triple":
                entries.append((idx + 1, side_px, dim, dim_alpha * 0.78))
        return entries

    def current_font_px(self) -> float:
        key = "fullscreen_font_size" if self.is_fullscreen() else "font_size"
        return float(self.config[key])

    # ---------------- 绘制 ----------------
    def _font(self, size: float, bold: bool = True) -> QFont:
        f = QFont()
        f.setFamily(str(self.config["font_family"]))
        f.setPixelSize(max(8, int(size)))
        f.setBold(bold)
        f.setStyleStrategy(QFont.PreferAntialias)
        return f

    def _fit(self, text: str, px: float, max_w: float, bold=True) -> QFont:
        size = float(px)
        f = self._font(size, bold)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text)
        if w > max_w and w > 0:
            f = self._font(max(9.0, px * max_w / w), bold)
        return f

    def _text_path(self, text: str, font: QFont) -> QPainterPath:
        key = (text, font.pixelSize(), font.bold(), font.family())
        path = self._path_cache.get(key)
        if path is None:
            path = QPainterPath()
            path.addText(0.0, 0.0, font, text)
            br = path.boundingRect()
            path.translate(-br.left(), -br.top())
            if len(self._path_cache) > 60:
                self._path_cache.clear()
            self._path_cache[key] = path
        return path

    def paintEvent(self, event) -> None:
        _t0 = time.perf_counter()
        if _DEBUG:
            self._fps_n += 1
            now = time.monotonic()
            if now - self._fps_t >= 3.0:
                _debug("fps=%.1f win=%dx%d px=%.1f paint=%.1fms lines=%d mode=%s"
                       % (self._fps_n / (now - self._fps_t), self.width(), self.height(),
                          getattr(self, "_cur_px", 0), self._paint_ms / max(1, self._fps_n),
                          len(self.lines), self.display_mode()))
                self._fps_n = 0
                self._fps_t = now
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)

        rect = QRectF(self.rect())
        if _DEBUG and os.environ.get("LYRIC_REDBG"):
            painter.fillRect(rect, QColor(255, 0, 255))
        bg_alpha = float(self.config["bg_alpha"])
        if bg_alpha > 0.01:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(255 * min(bg_alpha, 1.0))))
            radius = min(28.0, rect.height() / 3)
            painter.drawRoundedRect(rect, radius, radius)

        if not self.lines:
            self._paint_status(painter, rect)
            self._paint_hint(painter, rect)
            painter.end()
            return

        idx = self.current_index()
        main_px = float(getattr(self, "_cur_px", 0) or self.current_font_px())
        side_px = main_px * float(self.config["side_ratio"])
        gap = main_px * 0.42

        entries = self._build_entries(idx, main_px, side_px)

        items = []
        for i, px_want, color, alpha in entries:
            text = self.lines[i].text
            font = self._font(px_want, bold=(i == idx))
            c = QColor(color)
            c.setAlphaF(alpha)
            pm, w, h = self._line_pixmap(text, font, c)
            limit = rect.width() - 6
            if w > limit > 0:
                font = self._font(px_want * limit / w, bold=(i == idx))
                pm, w, h = self._line_pixmap(text, font, c)
            items.append((i, text, font, c, pm, w, h))

        total_h = sum(it[6] for it in items) + gap * (len(items) - 1)
        if total_h > rect.height() > 0 and items:
            factor = rect.height() / total_h
            scaled = []
            for i, text, font, c, pm, w, h in items:
                nf = self._font(max(8, font.pixelSize() * factor * 0.97), bold=font.bold())
                npm, nw, nh = self._line_pixmap(text, nf, c)
                scaled.append((i, text, nf, c, npm, nw, nh))
            items = scaled
            total_h = sum(it[6] for it in items) + gap * (len(items) - 1)
        y = (rect.height() - total_h) / 2.0
        if y < 0:
            y = 0.0
        cx = rect.center().x()

        for i, text, font, color, pm, w, h in items:
            x = cx - w / 2.0
            if i == idx:
                painter.drawPixmap(QPointF(x, y), pm)
                self._draw_progress(painter, text, font, idx, x, y, w, h)
            else:
                painter.drawPixmap(QPointF(x, y), pm)
            y += h + gap

        self._paint_hint(painter, rect)
        painter.end()
        if _DEBUG:
            self._paint_ms += (time.perf_counter() - _t0) * 1000.0

    def _paint_hint(self, painter: QPainter, rect: QRectF) -> None:
        if not self.hint_text:
            return
        px = max(13.0, self.current_font_px() * 0.24)
        font = self._fit(self.hint_text, px, rect.width() - 40, bold=False)
        c = QColor(self.config["active_color"])
        c.setAlphaF(0.92)
        h = QFontMetricsF(font).height()
        self._draw_sub_line(painter, self.hint_text, font, c,
                            QRectF(20, 6, rect.width() - 40, h), stroke_scale=0.8)

    def _paint_status(self, painter: QPainter, rect: QRectF) -> None:
        if not self.status_text:
            return
        px = max(16.0, self.current_font_px() * 0.52)
        font = self._fit(self.status_text, px, rect.width() - 40, bold=False)
        c = QColor(self.config["dim_color"])
        c.setAlphaF(0.92)
        self._draw_sub_line(painter, self.status_text, font, c, rect)

    def _place(self, painter: QPainter, path: QPainterPath, rect: QRectF):
        br = path.boundingRect()
        left = rect.center().x() - br.width() / 2.0
        top = rect.center().y() - br.height() / 2.0
        painter.translate(left, top)
        return br, left, top

    def _line_pixmap(self, text: str, font: QFont, color: QColor,
                     stroke_scale: float = 1.0):
        """把带描边的一行歌词渲染成位图缓存，避免每帧重算字形轮廓。"""
        key = (text, font.pixelSize(), font.bold(), font.family(), color.name(),
               color.alpha(), int(float(self.config["stroke_alpha"]) * 100),
               round(stroke_scale, 2))
        hit = self._pix_cache.get(key)
        if hit is not None:
            return hit

        path = self._text_path(text, font)
        br = path.boundingRect()
        px = font.pixelSize()
        alpha = float(self.config["stroke_alpha"]) * color.alphaF()
        if alpha > 0.02:
            inner = max(1.8, px * 0.12 * stroke_scale)
            outer = max(3.0, px * 0.26 * stroke_scale)
            pad = outer + 2.0
        else:
            inner = outer = 0.0
            pad = 2.0
        w = br.width() + pad * 2
        h = br.height() + pad * 2

        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.translate(pad - br.left(), pad - br.top())
        if alpha > 0.02:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, int(255 * alpha * 0.42)), outer,
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(QColor(0, 0, 0, int(255 * alpha)), inner,
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)
        p.end()

        if len(self._pix_cache) > 12:
            self._pix_cache.clear()
        entry = (pm, w, h)
        self._pix_cache[key] = entry
        return entry

    def _blit(self, painter: QPainter, text: str, font: QFont, color: QColor,
              rect: QRectF, stroke_scale: float = 1.0):
        pm, w, h = self._line_pixmap(text, font, color, stroke_scale)
        x = rect.center().x() - w / 2.0
        y = rect.center().y() - h / 2.0
        painter.drawPixmap(QPointF(x, y), pm)
        return x, y, w, h

    def _draw_sub_line(self, painter: QPainter, text: str, font: QFont, color: QColor,
                       rect: QRectF, stroke_scale: float = 1.0) -> None:
        if not text or color.alpha() == 0:
            return
        painter.save()
        self._blit(painter, text, font, color, rect, stroke_scale)
        painter.restore()

    def _draw_progress(self, painter: QPainter, text: str, font: QFont, idx: int,
                       x: float, y: float, w: float, h: float) -> None:
        if not self.config["show_progress"] or self.position_ms is None or w <= 0:
            return
        start = self.times[idx]
        end = self.times[idx + 1] if idx + 1 < len(self.times) else start + 4200
        progress = (self.position_ms - start) / max(300, end - start)
        progress = min(1.0, max(0.0, progress))
        if progress <= 0.012:
            return
        active, _w, _h = self._line_pixmap(text, font,
                                           QColor(self.config["active_color"]))
        painter.save()
        painter.setClipRect(QRectF(x, y, w * progress, h))
        painter.drawPixmap(QPointF(x, y), active)
        painter.restore()

    def _draw_main_line(self, painter: QPainter, text: str, font: QFont, color: QColor,
                        rect: QRectF, idx: int) -> None:
        if not text:
            return
        painter.save()
        x, y, w, h = self._blit(painter, text, font, color, rect)

        if not self.config["show_progress"] or self.position_ms is None:
            painter.restore()
            return
        start = self.times[idx]
        end = self.times[idx + 1] if idx + 1 < len(self.times) else start + 4200
        progress = (self.position_ms - start) / max(300, end - start)
        progress = min(1.0, max(0.0, progress))
        if progress > 0.012:
            painter.setClipRect(QRectF(x, y, w * progress, h))
            active, _w, _h = self._line_pixmap(text, font,
                                               QColor(self.config["active_color"]))
            painter.drawPixmap(QPointF(x, y), active)
            painter.setClipping(False)
        painter.restore()

    # ---------------- 交互 ----------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
            self._win_origin = self.pos()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and self._win_origin is not None:
            self.move(self._win_origin + (event.globalPosition().toPoint() - self._drag_origin))
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_origin is not None:
            self._drag_origin = None
            if not self.is_fullscreen():
                g = self.geometry()
                self.config["x"] = g.x()
                self.config["y"] = g.y()
                self.config.save()
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.is_fullscreen():
            self.exit_fullscreen()
            event.accept()

    def wheelEvent(self, event) -> None:
        if self.font_callback is None:
            return
        self.font_callback(4 if event.angleDelta().y() > 0 else -4)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        if callable(getattr(self, "menu_callback", None)):
            self.menu_callback(event.globalPos())
            event.accept()


# --------------------------------------------------------------------------- #
# Claude 风格的设置面板
# --------------------------------------------------------------------------- #

DIALOG_QSS = f"""
QDialog {{ background: {CANVAS}; }}
QLabel {{ color: {INK}; font-size: 13px; }}
QLabel#Title {{ font-size: 22px; font-weight: 600; color: {INK}; }}
QLabel#Subtitle {{ font-size: 13px; color: {INK_SOFT}; }}
QLabel#SectionTitle {{ font-size: 12px; font-weight: 600; color: {INK_SOFT};
                       letter-spacing: 1px; }}
QLabel#Value {{ color: {INK_SOFT}; font-size: 12px; }}
QFrame#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QComboBox, QFontComboBox, QSpinBox {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 10px; color: {INK}; min-height: 20px;
}}
QComboBox:hover, QFontComboBox:hover, QSpinBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {HOVER_BG};
    selection-color: {INK}; outline: none; padding: 4px;
}}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {CARD}; border: 2px solid {ACCENT}; width: 14px; height: 14px;
    margin: -7px 0; border-radius: 8px;
}}
QCheckBox {{ color: {INK}; font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid #D8D3C6; background: {CARD};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QPushButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 8px 16px; color: {INK}; font-size: 13px;
}}
QPushButton:hover {{ background: {HOVER_BG}; }}
QPushButton#Primary {{ background: {ACCENT}; color: #FFFFFF; border: none; }}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Swatch {{ border-radius: 8px; padding: 0px; min-width: 26px; max-width: 26px;
                      min-height: 26px; max-height: 26px; }}
"""


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 14, 18, 16)
    lay.setSpacing(10)
    lbl = QLabel(title.upper())
    lbl.setObjectName("SectionTitle")
    lay.addWidget(lbl)
    return frame, lay


def _row(label: str, widget: QWidget, value_label: QLabel | None = None) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lab = QLabel(label)
    lay.addWidget(lab)
    lay.addStretch(1)
    if value_label is not None:
        lay.addWidget(value_label)
    lay.addWidget(widget)
    return row


class SettingsDialog(QDialog):
    """Claude 风格的设置面板。"""

    changed = Signal(str, object)
    align_requested = Signal(int)

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("歌词显示设置")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(DIALOG_QSS)
        self.setMinimumWidth(460)
        self._build()
        self._sync_from_config()

    # ---------------- 构建 ----------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(14)

        title = QLabel("歌词显示")
        title.setObjectName("Title")
        sub = QLabel("调整字体、样式与布局，改动会立刻生效并记住")
        sub.setObjectName("Subtitle")
        root.addWidget(title)
        root.addWidget(sub)

        # 外观
        card, lay = _card("外观")
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont(str(self.config["font_family"])))
        self.font_combo.currentFontChanged.connect(
            lambda f: self._emit("font_family", f.family()))
        lay.addWidget(_row("字体", self.font_combo))

        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(24, 200)
        self.size_value = QLabel()
        self.size_value.setObjectName("Value")
        self.size_slider.valueChanged.connect(self._on_size)
        lay.addWidget(_row("字号（窗口模式）", self.size_slider, self.size_value))

        self.fs_slider = QSlider(Qt.Horizontal)
        self.fs_slider.setRange(40, 320)
        self.fs_value = QLabel()
        self.fs_value.setObjectName("Value")
        self.fs_slider.valueChanged.connect(self._on_fs_size)
        lay.addWidget(_row("字号（全屏模式）", self.fs_slider, self.fs_value))

        theme_row = QWidget()
        tl = QHBoxLayout(theme_row)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        tl.addWidget(QLabel("配色"))
        tl.addStretch(1)
        for name in THEMES:
            btn = QPushButton(name)
            btn.clicked.connect(lambda _=False, n=name: self._apply_theme(n))
            tl.addWidget(btn)
        lay.addWidget(theme_row)

        self.side_slider = QSlider(Qt.Horizontal)
        self.side_slider.setRange(40, 100)
        self.side_value = QLabel()
        self.side_value.setObjectName("Value")
        self.side_slider.valueChanged.connect(self._on_side_ratio)
        lay.addWidget(_row("上下句字号（相对当前句）", self.side_slider, self.side_value))

        self.dim_slider = QSlider(Qt.Horizontal)
        self.dim_slider.setRange(0, 100)
        self.dim_value = QLabel()
        self.dim_value.setObjectName("Value")
        self.dim_slider.valueChanged.connect(self._on_dim)
        lay.addWidget(_row("上下句透明度", self.dim_slider, self.dim_value))

        color_row = QWidget()
        cl = QHBoxLayout(color_row)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        cl.addWidget(QLabel("自定义颜色"))
        cl.addStretch(1)
        self.sw_text = self._swatch("text_color")
        self.sw_active = self._swatch("active_color")
        self.sw_dim = self._swatch("dim_color")
        for sw, name in ((self.sw_text, "文字"), (self.sw_active, "高亮"), (self.sw_dim, "上下句")):
            lab = QLabel(name)
            lab.setObjectName("Value")
            cl.addWidget(lab)
            cl.addWidget(sw)
        lay.addWidget(color_row)
        root.addWidget(card)

        # 背景
        card2, lay2 = _card("背景")
        self.bg_slider = QSlider(Qt.Horizontal)
        self.bg_slider.setRange(0, 100)
        self.bg_value = QLabel()
        self.bg_value.setObjectName("Value")
        self.bg_slider.valueChanged.connect(self._on_bg)
        lay2.addWidget(_row("背景深浅", self.bg_slider, self.bg_value))
        self.stroke_slider = QSlider(Qt.Horizontal)
        self.stroke_slider.setRange(0, 100)
        self.stroke_value = QLabel()
        self.stroke_value.setObjectName("Value")
        self.stroke_slider.valueChanged.connect(self._on_stroke)
        lay2.addWidget(_row("文字描边", self.stroke_slider, self.stroke_value))
        root.addWidget(card2)

        # 布局
        card3, lay3 = _card("布局与行为")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("三行（上一句 · 当前 · 下一句）", "triple")
        self.mode_combo.addItem("双排（当前 · 下一句）", "double")
        self.mode_combo.addItem("单行（只显示当前句）", "single")
        self.mode_combo.currentIndexChanged.connect(self._on_mode)
        lay3.addWidget(_row("显示方式", self.mode_combo))
        self.cb_progress = QCheckBox("当前行按进度高亮")
        self.cb_progress.toggled.connect(lambda v: self._emit("show_progress", bool(v)))
        lay3.addWidget(self.cb_progress)
        self.cb_fullscreen = QCheckBox("全屏显示（Ctrl+Alt+F 切换）")
        self.cb_fullscreen.toggled.connect(self._on_fullscreen)
        lay3.addWidget(self.cb_fullscreen)
        root.addWidget(card3)

        # 同步
        card4, lay4 = _card("同步")
        self.offset_slider = QSlider(Qt.Horizontal)
        self.offset_slider.setRange(-5000, 5000)
        self.offset_slider.setSingleStep(100)
        self.offset_value = QLabel()
        self.offset_value.setObjectName("Value")
        self.offset_slider.valueChanged.connect(self._on_offset)
        lay4.addWidget(_row("时间偏移", self.offset_slider, self.offset_value))
        align_row = QWidget()
        al = QHBoxLayout(align_row)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        al.addWidget(QLabel("按句对齐"))
        al.addStretch(1)
        prev_btn = QPushButton("上一句")
        prev_btn.clicked.connect(lambda: self.align_requested.emit(-1))
        next_btn = QPushButton("下一句")
        next_btn.clicked.connect(lambda: self.align_requested.emit(+1))
        al.addWidget(prev_btn)
        al.addWidget(next_btn)
        lay4.addWidget(align_row)
        tip = QLabel("看着屏幕唱到哪句，按「下一句」追上去即可；也可以按 Ctrl+Alt+Shift+← / →。"
                     "Ctrl+Alt+← / → 是 0.5 秒级的微调。")
        tip.setWordWrap(True)
        tip.setObjectName("Subtitle")
        lay4.addWidget(tip)
        root.addWidget(card4)

        # 底部按钮
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        reset = QPushButton("恢复默认")
        reset.clicked.connect(self._reset)
        done = QPushButton("完成")
        done.setObjectName("Primary")
        done.clicked.connect(self.accept)
        bottom.addWidget(reset)
        bottom.addStretch(1)
        bottom.addWidget(done)
        root.addLayout(bottom)

    def _swatch(self, key: str) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("Swatch")
        btn.setToolTip("点击选择颜色")
        btn.clicked.connect(lambda: self._pick_color(key, btn))
        return btn

    def _sync_from_config(self) -> None:
        c = self.config
        widgets = (self.size_slider, self.fs_slider, self.side_slider, self.dim_slider,
                   self.bg_slider, self.stroke_slider, self.offset_slider,
                   self.mode_combo, self.cb_progress, self.cb_fullscreen)
        for w in widgets:
            w.blockSignals(True)
        self.size_slider.setValue(int(c["font_size"]))
        self.fs_slider.setValue(int(c["fullscreen_font_size"]))
        self.side_slider.setValue(int(float(c["side_ratio"]) * 100))
        self.dim_slider.setValue(int(float(c["dim_alpha"]) * 100))
        self.bg_slider.setValue(int(float(c["bg_alpha"]) * 100))
        self.stroke_slider.setValue(int(float(c["stroke_alpha"]) * 100))
        idx = self.mode_combo.findData(str(c["display_mode"]))
        self.mode_combo.setCurrentIndex(max(0, idx))
        self.cb_progress.setChecked(bool(c["show_progress"]))
        self.cb_fullscreen.setChecked(bool(c["fullscreen"]))
        self.offset_slider.setValue(max(-5000, min(5000, int(c["offset_ms"]))))
        for w in widgets:
            w.blockSignals(False)
        self._refresh_slider_labels()
        for key, btn in (("text_color", self.sw_text),
                         ("active_color", self.sw_active),
                         ("dim_color", self.sw_dim)):
            self._paint_swatch(btn, str(c[key]))

    def _refresh_slider_labels(self) -> None:
        c = self.config
        self.size_value.setText(f"{int(c['font_size'])} px")
        self.fs_value.setText(f"{int(c['fullscreen_font_size'])} px")
        self.side_value.setText(f"{int(float(c['side_ratio']) * 100)}%")
        self.dim_value.setText("隐藏" if float(c["dim_alpha"]) <= 0.001
                               else f"{int(float(c['dim_alpha']) * 100)}%")
        self.bg_value.setText("透明" if float(c["bg_alpha"]) <= 0.001
                              else f"{int(float(c['bg_alpha']) * 100)}%")
        self.stroke_value.setText(f"{int(float(c['stroke_alpha']) * 100)}%")
        self.offset_value.setText(f"{int(c['offset_ms']) / 1000:+.1f} s")

    def _paint_swatch(self, btn: QPushButton, color: str) -> None:
        btn.setStyleSheet(
            f"QPushButton#Swatch {{ background: {color}; border: 1px solid {BORDER}; }}")

    # ---------------- 回调 ----------------
    def _emit(self, key: str, value) -> None:
        self.config[key] = value
        self.config.save()
        self.changed.emit(key, value)

    def _on_size(self, v: int) -> None:
        self.size_value.setText(f"{v} px")
        self._emit("font_size", v)

    def _on_fs_size(self, v: int) -> None:
        self.fs_value.setText(f"{v} px")
        self._emit("fullscreen_font_size", v)

    def _on_mode(self, _index: int) -> None:
        mode = self.mode_combo.currentData()
        if mode:
            self._emit("display_mode", str(mode))

    def _on_side_ratio(self, v: int) -> None:
        self.side_value.setText(f"{v}%")
        self._emit("side_ratio", v / 100.0)

    def _on_dim(self, v: int) -> None:
        self.dim_value.setText("隐藏" if v == 0 else f"{v}%")
        self._emit("dim_alpha", v / 100.0)

    def _on_bg(self, v: int) -> None:
        self.bg_value.setText("透明" if v == 0 else f"{v}%")
        self._emit("bg_alpha", v / 100.0)

    def _on_stroke(self, v: int) -> None:
        self.stroke_value.setText(f"{v}%")
        self._emit("stroke_alpha", v / 100.0)

    def _on_offset(self, v: int) -> None:
        self.offset_value.setText(f"{v / 1000:+.1f} s")
        self._emit("offset_ms", v)

    def _on_fullscreen(self, on: bool) -> None:
        self._emit("fullscreen", bool(on))

    def _apply_theme(self, name: str) -> None:
        text, active, dim = THEMES[name]
        self.config["theme"] = name
        for key, value in (("text_color", text), ("active_color", active), ("dim_color", dim)):
            self._emit(key, value)
        for key, btn in (("text_color", self.sw_text),
                         ("active_color", self.sw_active),
                         ("dim_color", self.sw_dim)):
            self._paint_swatch(btn, str(self.config[key]))

    def _pick_color(self, key: str, btn: QPushButton) -> None:
        col = QColorDialog.getColor(QColor(str(self.config[key])), self, "选择颜色")
        if col.isValid():
            self._paint_swatch(btn, col.name())
            self._emit(key, col.name())

    def _reset(self) -> None:
        for key, value in DEFAULT_CONFIG.items():
            if key in ("x", "y", "visible"):
                continue
            self._emit(key, value)
        self.font_combo.setCurrentFont(QFont(str(DEFAULT_CONFIG["font_family"])))
        self._sync_from_config()


# --------------------------------------------------------------------------- #
# 主程序
# --------------------------------------------------------------------------- #


class LyricApp(QObject):
    def __init__(self, qapp: QApplication) -> None:
        super().__init__()
        self.qapp = qapp
        self.config = Config()
        self.window = LyricWindow(self.config)
        self.window.font_callback = self.adjust_font
        self.window.menu_callback = self.show_menu
        self.title_watcher = KuGouTitleWatcher()
        self.smtc = SmtcBridge()
        self.clock = PlayClock()
        self.fetcher = LyricFetcher()
        self.current_song = ""
        self.current_artist = ""
        self._hint_shown = False
        self.settings: SettingsDialog | None = None

        self.fetcher.loaded.connect(self._on_lyrics)

        self.window.show()
        self._apply_lock_state(notify=False)
        self.window.set_status("正在寻找酷狗…")

        self.smtc.start()
        self.hotkeys = HotkeyFilter(self._on_hotkey)
        qapp.installNativeEventFilter(self.hotkeys)
        self._register_hotkeys()

        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self._poll_track)
        self.track_timer.start(280)

        self.paint_timer = QTimer(self)
        self.paint_timer.setTimerType(Qt.PreciseTimer)
        self.paint_timer.timeout.connect(self._tick)
        self.paint_timer.start(self._paint_interval())
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

        self._make_tray()

    # ---------------- 托盘与菜单 ----------------
    def _make_tray(self) -> None:
        pix = QPixmap(64, 64)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(ACCENT))
        p.setPen(Qt.NoPen)
        p.drawEllipse(1, 1, 62, 62)
        f = QFont()
        f.setFamily(str(self.config["font_family"]))
        f.setPixelSize(38)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(pix.rect(), Qt.AlignCenter, "词")
        p.end()

        self.tray = QSystemTrayIcon(QIcon(pix), self)
        self.tray.setToolTip(APP_TITLE)
        self.tray.setContextMenu(self._build_menu())
        self.tray.activated.connect(
            lambda reason: self.toggle_visible() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        menu.addAction("设置…", self.open_settings)
        menu.addSeparator()
        menu.addAction("放大字号  Ctrl+Alt+↑", lambda: self.adjust_font(+4))
        menu.addAction("缩小字号  Ctrl+Alt+↓", lambda: self.adjust_font(-4))
        menu.addAction("全屏显示  Ctrl+Alt+F", self.toggle_fullscreen)
        menu.addSeparator()
        menu.addAction("歌词提前 0.5 秒  Ctrl+Alt+←", lambda: self.shift_offset(+500))
        menu.addAction("歌词延后 0.5 秒  Ctrl+Alt+→", lambda: self.shift_offset(-500))
        menu.addAction("对齐上一句  Ctrl+Alt+Shift+←", lambda: self.skip_line(-1))
        menu.addAction("对齐下一句  Ctrl+Alt+Shift+→", lambda: self.skip_line(+1))
        menu.addAction("重置时间偏移  Ctrl+Alt+0", self.reset_offset)
        menu.addSeparator()
        self.act_lock = menu.addAction("锁定（鼠标穿透）  Ctrl+Alt+L", self.toggle_lock)
        self.act_lock.setCheckable(True)
        self.act_lock.setChecked(bool(self.config["locked"]))
        menu.addAction("隐藏  Ctrl+Alt+H", self.toggle_visible)
        menu.addAction("重新搜索歌词", self.refresh_lyrics)
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self._menu = menu
        return menu

    def show_menu(self, pos) -> None:
        self._menu.popup(pos)

    def _set_topmost(self, on: bool) -> None:
        try:
            hwnd = int(self.window.winId())
        except Exception:
            return
        user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                            0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)

    def open_settings(self) -> None:
        if self.settings is None:
            self.settings = SettingsDialog(self.config)
            self.settings.changed.connect(self._on_setting_changed)
            self.settings.align_requested.connect(self.skip_line)
            self.settings.finished.connect(lambda _: self._set_topmost(True))
        self._set_topmost(False)
        self.settings._sync_from_config()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def _on_setting_changed(self, key: str, value) -> None:
        if key in ("font_size", "fullscreen_font_size", "show_context", "display_mode",
                   "side_ratio"):
            self._resize_for_font()
        elif key in ("dim_alpha", "bg_alpha", "stroke_alpha",
                     "text_color", "active_color", "dim_color"):
            self.window._pix_cache.clear()
        elif key == "fullscreen":
            if value:
                self.window.enter_fullscreen()
            else:
                self.window.exit_fullscreen()
        self.window._path_cache.clear()
        self.window.update()

    # ---------------- 尺寸 / 外观 ----------------
    def adjust_font(self, delta: int) -> None:
        key = "fullscreen_font_size" if self.window.is_fullscreen() else "font_size"
        lo, hi = (40, 320) if key == "fullscreen_font_size" else (18, 240)
        size = max(lo, min(hi, int(self.config[key]) + delta))
        self.config[key] = size
        self.config.save()
        self._resize_for_font()
        self.window.update()
        if self.settings is not None and self.settings.isVisible():
            self.settings._sync_from_config()

    def _resize_for_font(self) -> None:
        self.window._path_cache.clear()
        self.window._apply_geometry()

    def toggle_fullscreen(self) -> None:
        self.window.toggle_fullscreen()
        self._set_topmost(True)
        if self.settings is not None:
            self.settings._sync_from_config()

    def shift_offset(self, delta: int) -> None:
        self.config["offset_ms"] = max(-3600000, min(3600000,
                                                     int(self.config["offset_ms"]) + delta))
        self.config.save()
        self._flash_notice(f"偏移 {self.config['offset_ms'] / 1000:+.1f}s")
        if self.settings is not None and self.settings.isVisible():
            self.settings._sync_from_config()

    def skip_line(self, delta: int) -> None:
        """把显示对齐到上/下一句：看着屏幕唱到哪句，按几下就追上了。"""
        lines = self.window.lines
        if not lines:
            return
        idx = self.window.current_index()
        target = max(0, min(len(lines) - 1, idx + delta))
        if target == idx:
            return
        base = self.clock.position_ms(0)
        if base is None:
            base = 0
        self.config["offset_ms"] = int(lines[target].t - base)
        self.config.save()
        self.window.set_position(self.clock.position_ms(int(self.config["offset_ms"])))
        self._flash_notice(f"已对齐到第 {target + 1} 句")
        if self.settings is not None and self.settings.isVisible():
            self.settings._sync_from_config()

    def reset_offset(self) -> None:
        self.config["offset_ms"] = 0
        self.config.save()
        self._flash_notice("偏移已归零")
        if self.settings is not None and self.settings.isVisible():
            self.settings._sync_from_config()

    def _flash_notice(self, text: str) -> None:
        self.window.set_hint(text)
        QTimer.singleShot(1400, lambda: self.window.set_hint(""))

    # ---------------- 锁定 / 显示 ----------------
    def toggle_lock(self) -> None:
        self.config["locked"] = not bool(self.config["locked"])
        self.config.save()
        self._apply_lock_state()

    def _apply_lock_state(self, notify: bool = True) -> None:
        locked = bool(self.config["locked"])
        if not (self.window.is_fullscreen()
                and self.config["auto_click_through_fullscreen"]):
            self.window._apply_click_through(locked)
        if hasattr(self, "act_lock"):
            self.act_lock.setChecked(locked)
        if notify:
            self._flash_notice("已锁定（鼠标穿透）" if locked else "已解锁，可以拖动")

    def toggle_visible(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.show()
            self.window._apply_geometry()
            self._apply_lock_state(notify=False)
            self.window.update()

    # ---------------- 热键 ----------------
    def _register_hotkeys(self) -> None:
        for hid, (name, mods, vk) in HOTKEYS.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                _debug(f"hotkey {name} register failed")

    def _unregister_hotkeys(self) -> None:
        for hid in HOTKEYS:
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass

    def _on_hotkey(self, hid: int) -> None:
        name = HOTKEYS.get(hid, ("", 0, 0))[0]
        action = {
            "font_up": lambda: self.adjust_font(+4),
            "font_down": lambda: self.adjust_font(-4),
            "sync_back": lambda: self.shift_offset(+500),
            "sync_forward": lambda: self.shift_offset(-500),
            "sync_reset": self.reset_offset,
            "toggle_lock": self.toggle_lock,
            "toggle_visible": self.toggle_visible,
            "toggle_fullscreen": self.toggle_fullscreen,
            "line_prev": lambda: self.skip_line(-1),
            "line_next": lambda: self.skip_line(+1),
        }.get(name)
        if action:
            action()

    # ---------------- 轮询 ----------------
    def _poll_track(self) -> None:
        playing = self.smtc.playing()
        self.clock.feed_external(*self.smtc.timeline())
        now = self.title_watcher.now_playing()
        if not now:
            self.clock.on_playing(playing)
            if not self.current_song:
                self.window.set_status("等待酷狗开始播放…")
            return
        song, artist = now
        if self.clock.want_song(f"{song}|{artist}", playing):
            self.current_song, self.current_artist = song, artist
            self.window.set_lyrics([])
            self.window.set_status(f"正在获取歌词：{song}")
            if song and song != "酷狗音乐":
                self.fetcher.request(song, artist)
        self.clock.on_playing(playing)

    def _paint_interval(self) -> int:
        area = self.window.width() * self.window.height()
        return 33 if area <= 2_600_000 else 40

    def _sync_paint_rate(self) -> None:
        want = self._paint_interval()
        if self.paint_timer.interval() != want:
            self.paint_timer.setInterval(want)

    def _tick(self) -> None:
        self._sync_paint_rate()
        if not self.window.lines:
            return
        self.window.set_position(self.clock.position_ms(int(self.config["offset_ms"])))

    # ---------------- 歌词回调 ----------------
    def _on_lyrics(self, song: str, artist: str, lyrics) -> None:
        if song != self.current_song:
            return
        if not lyrics or not lyrics.lines:
            self.window.set_lyrics([])
            self.window.set_status(f"没找到歌词：{song}")
            return
        self.window.set_lyrics(lyrics.lines)
        self.window.set_status("")
        if not self._hint_shown:
            self._hint_shown = True
            self.window.set_hint("歌词若与声音不同步，按 Ctrl+Alt+← / → 微调")
            QTimer.singleShot(7000, lambda: self.window.set_hint(""))

    def refresh_lyrics(self) -> None:
        if not self.current_song:
            return
        self.fetcher.forget(self.current_song, self.current_artist)
        self.window.set_status("重新搜索歌词…")
        self.fetcher.request(self.current_song, self.current_artist)

    # ---------------- 退出 ----------------
    def quit(self) -> None:
        self._unregister_hotkeys()
        self.smtc.stop()
        try:
            self.tray.hide()
        except Exception:
            pass
        self.qapp.quit()


def single_instance_ok() -> bool:
    _kernel32_le.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    return ctypes.get_last_error() != 183


def main() -> int:
    if not single_instance_ok():
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)
    lyric_app = LyricApp(app)
    app._lyric_app = lyric_app
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
