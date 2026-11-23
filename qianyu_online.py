# -*- coding: utf-8 -*-
"""
千羽播放器 · 在线搜索与下载

内置「网易云」源（走 GD Studio 的公开接口），也可以自己填地址接入 Meting 协议的
公共或自建服务。搜到的歌可以直接下到本地音乐库，歌词一起下，下完自动出现在列表里。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout)

import lyric_display as L

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SOURCES_PATH = L.APP_DIR / "qianyu_sources.json"

KIND_LABEL = {"gdstudio": "GD Studio 协议", "meting": "Meting 协议"}

BUILTIN_SOURCES = [
    {"id": "netease", "name": "网易云", "kind": "gdstudio",
     "base": "https://music-api.gdstudio.xyz/api.php", "param": "netease"},
]

STYLE = f"""
QDialog {{ background: {L.CANVAS}; }}
QLabel {{ color: {L.INK}; font-size: 13px; }}
QLabel#Hint {{ color: {L.INK_SOFT}; font-size: 12px; }}
QLabel#Title {{ font-size: 19px; font-weight: 600; }}
QLineEdit, QComboBox {{
    background: #FFFFFF; border: 1px solid {L.BORDER}; border-radius: 8px;
    padding: 7px 10px; color: {L.INK}; font-size: 13px;
}}
QComboBox QAbstractItemView {{
    background: #FFFFFF; border: 1px solid {L.BORDER};
    selection-background-color: {L.HOVER_BG}; color: {L.INK};
}}
QPushButton {{
    background: #FFFFFF; border: 1px solid {L.BORDER}; border-radius: 8px;
    padding: 7px 14px; color: {L.INK}; font-size: 13px;
}}
QPushButton:hover {{ background: {L.HOVER_BG}; }}
QPushButton#Primary {{ background: {L.ACCENT}; color: #FFFFFF; border: none; }}
QPushButton#Primary:hover {{ background: {L.ACCENT_HOVER}; }}
QPushButton:disabled {{ color: #B9B5AC; }}
QListWidget {{
    background: #FFFFFF; border: 1px solid {L.BORDER}; border-radius: 10px;
    padding: 4px; color: {L.INK}; font-size: 13px; outline: none;
}}
QListWidget::item {{ padding: 7px 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {L.HOVER_BG}; color: {L.INK}; }}
QProgressBar {{
    background: #FFFFFF; border: 1px solid {L.BORDER}; border-radius: 5px;
    max-height: 8px; min-height: 8px; color: transparent;
}}
QProgressBar::chunk {{ background: {L.ACCENT}; border-radius: 4px; }}
"""


def http_bytes(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://music.gdstudio.xyz/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(url: str, timeout: float = 20.0):
    raw = http_bytes(url, timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def safe_name(text: str, limit: int = 90) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    return (cleaned or "未命名")[:limit]


def search(source: dict, keyword: str) -> list[dict]:
    """统一返回 [{title, artist, album, ...源私有字段}]"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    base = (source.get("base") or "").strip()
    if not base:
        raise RuntimeError("这个源还没填地址")
    param = source.get("param") or "netease"
    kind = source.get("kind") or "gdstudio"

    if kind == "gdstudio":
        query = urllib.parse.urlencode(
            {"types": "search", "source": param, "name": kw, "count": 30})
        data = http_json(f"{base}?{query}")
        out = []
        for it in data or []:
            artists = it.get("artist") or []
            if isinstance(artists, str):
                artists = [artists]
            out.append({
                "title": it.get("name") or "",
                "artist": "/".join(a for a in artists if a),
                "album": it.get("album") or "",
                "kind": "gdstudio",
                "source": param,
                "base": base,
                "url_id": it.get("url_id") or it.get("id"),
                "lyric_id": it.get("lyric_id") or it.get("id"),
            })
        return out

    query = urllib.parse.urlencode({"server": param, "type": "search", "id": kw})
    data = http_json(f"{base}?{query}")
    out = []
    for it in data or []:
        out.append({
            "title": it.get("title") or it.get("name") or "",
            "artist": it.get("author") or it.get("artist") or "",
            "album": it.get("album") or "",
            "kind": "meting",
            "source": param,
            "base": base,
            "play_url": it.get("url") or "",
            "lrc_url": it.get("lrc") or "",
        })
    return out


def resolve_play_url(item: dict) -> str:
    if item.get("play_url"):
        return item["play_url"]
    if item.get("kind") != "gdstudio":
        return ""
    base = item["base"]
    for br in ("320", "128"):
        query = urllib.parse.urlencode(
            {"types": "url", "source": item["source"], "id": item["url_id"], "br": br})
        data = http_json(f"{base}?{query}")
        url = (data or {}).get("url") or ""
        if url:
            return url
    return ""


def fetch_lrc(item: dict) -> str:
    if item.get("lrc_url"):
        try:
            return http_bytes(item["lrc_url"], 12).decode("utf-8", "replace")
        except Exception:
            return ""
    if item.get("kind") != "gdstudio":
        return ""
    query = urllib.parse.urlencode(
        {"types": "lyric", "source": item["source"], "id": item["lyric_id"]})
    try:
        data = http_json(f"{item['base']}?{query}", 12)
    except Exception:
        return ""
    lyric = (data or {}).get("lyric") if isinstance(data, dict) else ""
    return lyric or ""


def download_stream(url: str, dest: Path, on_tick=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if on_tick:
                    on_tick(got, total)
    tmp.replace(dest)


class SourceStore:
    """网络源配置，存在 exe 旁边的 qianyu_sources.json"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SOURCES_PATH
        self.data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data.get("sources"), list) or not data["sources"]:
            data["sources"] = [dict(s) for s in BUILTIN_SOURCES]
        if not data.get("active"):
            data["active"] = data["sources"][0].get("id") or ""
        return data

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except Exception:
            pass

    @property
    def sources(self) -> list[dict]:
        return self.data["sources"]

    def by_id(self, sid: str) -> dict | None:
        for s in self.sources:
            if s.get("id") == sid:
                return s
        return None

    def active_source(self) -> dict:
        return self.by_id(self.data.get("active") or "") or self.sources[0]

    def set_active(self, sid: str) -> None:
        self.data["active"] = sid
        self.save()

    @property
    def download_dir(self) -> str:
        return self.data.get("download_dir") or ""

    @download_dir.setter
    def download_dir(self, value: str) -> None:
        self.data["download_dir"] = value
        self.save()

    def restore_builtin(self) -> None:
        ids = {s.get("id") for s in self.sources}
        for builtin in BUILTIN_SOURCES:
            if builtin["id"] not in ids:
                self.sources.append(dict(builtin))


class SearchWorker(QThread):
    ok = Signal(list)
    fail = Signal(str)

    def __init__(self, source: dict, keyword: str, parent=None) -> None:
        super().__init__(parent)
        self.source = source
        self.keyword = keyword

    def run(self) -> None:
        try:
            self.ok.emit(search(self.source, self.keyword))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class DownloadWorker(QThread):
    tick = Signal(int, int)
    ok = Signal(str)
    fail = Signal(str)

    def __init__(self, item: dict, folder: Path, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self.folder = folder

    def run(self) -> None:
        try:
            url = resolve_play_url(self.item)
            if not url:
                raise RuntimeError("这个源没给出播放地址，可能这首歌下架或要会员")
            self.folder.mkdir(parents=True, exist_ok=True)
            stem = safe_name(f"{self.item.get('title', '')}-{self.item.get('artist', '')}")
            dest = self.folder / f"{stem}.mp3"
            download_stream(url, dest, lambda got, total: self.tick.emit(got, total))
            lyric = fetch_lrc(self.item)
            if lyric:
                (self.folder / f"{stem}.lrc").write_text(lyric, encoding="utf-8")
            self.ok.emit(str(dest))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class SourceDialog(QDialog):
    """网络源配置：自己填地址接 Meting，或者用内置的网易云源"""

    def __init__(self, store: SourceStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.current: dict | None = None
        self.setWindowTitle("网络源")
        self.setStyleSheet(STYLE)
        self.resize(720, 520)
        self._build()
        self._reload_list()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(12)

        title = QLabel("网络源")
        title.setObjectName("Title")
        outer.addWidget(title)
        hint = QLabel("搜索用的接口地址。内置源开箱可用；也可以填自己的 Meting 服务，"
                      "例如 https://你的域名/api")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.list = QListWidget()
        self.list.setFixedWidth(230)
        self.list.currentRowChanged.connect(self._on_pick)
        row.addWidget(self.list)

        form = QVBoxLayout()
        form.setSpacing(9)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("源名称，例如 我的网易云")
        self.kind_combo = QComboBox()
        for key, label in KIND_LABEL.items():
            self.kind_combo.addItem(label, key)
        self.base_edit = QLineEdit()
        self.base_edit.setPlaceholderText("https://music-api.gdstudio.xyz/api.php")
        self.param_edit = QLineEdit()
        self.param_edit.setPlaceholderText("netease")

        for label, widget in (("名称", self.name_edit), ("协议", self.kind_combo),
                              ("地址", self.base_edit), ("源参数", self.param_edit)):
            line = QHBoxLayout()
            tag = QLabel(label)
            tag.setFixedWidth(58)
            line.addWidget(tag)
            line.addWidget(widget, 1)
            form.addLayout(line)

        tip = QLabel("GD Studio 协议的源参数是 netease / tencent 之类；"
                     "Meting 协议的源参数是 netease / kugou / 自己服务支持的平台名。")
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        form.addWidget(tip)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        add_btn = QPushButton("新增")
        add_btn.clicked.connect(self._add)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._remove)
        restore_btn = QPushButton("恢复内置源")
        restore_btn.clicked.connect(self._restore)
        apply_btn = QPushButton("应用修改")
        apply_btn.clicked.connect(self._apply_current)
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addWidget(restore_btn)
        btns.addStretch(1)
        btns.addWidget(apply_btn)
        form.addLayout(btns)
        form.addStretch(1)

        row.addLayout(form, 1)
        outer.addLayout(row, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("Primary")
        close_btn.clicked.connect(self.accept)
        foot.addWidget(close_btn)
        outer.addLayout(foot)

    def _reload_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for src in self.store.sources:
            item = QListWidgetItem(f"{src.get('name', '未命名')} · {KIND_LABEL.get(src.get('kind'), '')}")
            self.list.addItem(item)
        self.list.blockSignals(False)
        idx = next((i for i, s in enumerate(self.store.sources)
                    if s.get("id") == self.store.data.get("active")), 0)
        self.list.setCurrentRow(min(idx, max(0, self.list.count() - 1)))

    def _on_pick(self, row: int) -> None:
        if not (0 <= row < len(self.store.sources)):
            self.current = None
            return
        self.current = self.store.sources[row]
        self.name_edit.setText(self.current.get("name", ""))
        self.kind_combo.setCurrentIndex(
            max(0, self.kind_combo.findData(self.current.get("kind", "gdstudio"))))
        self.base_edit.setText(self.current.get("base", ""))
        self.param_edit.setText(self.current.get("param", "netease"))
        self.store.set_active(self.current.get("id", ""))

    def _apply_current(self) -> None:
        if self.current is None:
            return
        name = self.name_edit.text().strip() or "未命名源"
        base = self.base_edit.text().strip()
        if not base.startswith("http"):
            QMessageBox.warning(self, "地址不对", "地址要以 http:// 或 https:// 开头")
            return
        self.current.update({
            "name": name,
            "kind": self.kind_combo.currentData(),
            "base": base,
            "param": self.param_edit.text().strip() or "netease",
        })
        self.store.save()
        row = self.list.currentRow()
        self._reload_list()
        self.list.setCurrentRow(row)

    def _add(self) -> None:
        new = {"id": f"custom-{len(self.store.sources) + 1}",
               "name": "新的源", "kind": "meting",
               "base": "https://", "param": "netease"}
        self.store.sources.append(new)
        self.store.save()
        self._reload_list()
        self.list.setCurrentRow(self.list.count() - 1)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self.store.sources)):
            return
        if len(self.store.sources) == 1:
            QMessageBox.information(self, "至少留一个", "删光了就搜不了歌了")
            return
        target = self.store.sources[row]
        if QMessageBox.question(self, "删除源", f"确定删掉「{target.get('name')}」？") != QMessageBox.Yes:
            return
        self.store.sources.pop(row)
        if self.store.data.get("active") == target.get("id"):
            self.store.data["active"] = self.store.sources[0].get("id", "")
        self.store.save()
        self._reload_list()

    def _restore(self) -> None:
        self.store.restore_builtin()
        self.store.save()
        self._reload_list()


class OnlineDialog(QDialog):
    """在线搜索歌曲并下载到本地"""

    downloaded = Signal(str)

    def __init__(self, store: SourceStore, music_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.music_root = music_root
        self.results: list[dict] = []
        self.search_worker: SearchWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.setWindowTitle("在线搜索")
        self.setStyleSheet(STYLE)
        self.resize(820, 600)
        self._build()
        self._sync_sources()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("在线搜索")
        title.setObjectName("Title")
        top.addWidget(title)
        top.addStretch(1)
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(150)
        top.addWidget(self.source_combo)
        cfg_btn = QPushButton("源设置")
        cfg_btn.clicked.connect(self._open_sources)
        top.addWidget(cfg_btn)
        outer.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("输入歌名或歌手，回车搜索")
        self.keyword.returnPressed.connect(self._start_search)
        row.addWidget(self.keyword, 1)
        self.search_btn = QPushButton("搜索")
        self.search_btn.setObjectName("Primary")
        self.search_btn.clicked.connect(self._start_search)
        row.addWidget(self.search_btn)
        outer.addLayout(row)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._start_download())
        outer.addWidget(self.list, 1)

        self.status = QLabel("搜到歌之后双击就能下载")
        self.status.setObjectName("Hint")
        outer.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        outer.addWidget(self.bar)

        foot = QHBoxLayout()
        self.dir_label = QLabel()
        self.dir_label.setObjectName("Hint")
        foot.addWidget(self.dir_label)
        pick_btn = QPushButton("改下载目录")
        pick_btn.clicked.connect(self._pick_dir)
        foot.addWidget(pick_btn)
        foot.addStretch(1)
        self.dl_btn = QPushButton("下载选中")
        self.dl_btn.setObjectName("Primary")
        self.dl_btn.clicked.connect(self._start_download)
        foot.addWidget(self.dl_btn)
        outer.addLayout(foot)
        self._sync_dir()

    def _sync_sources(self) -> None:
        self.source_combo.clear()
        for src in self.store.sources:
            self.source_combo.addItem(src.get("name", "未命名"), src.get("id"))
        active = self.store.data.get("active")
        idx = self.source_combo.findData(active)
        self.source_combo.setCurrentIndex(max(0, idx))

    def _open_sources(self) -> None:
        dlg = SourceDialog(self.store, self)
        dlg.exec()
        self._sync_sources()

    def _download_folder(self) -> Path:
        saved = self.store.download_dir
        if saved:
            return Path(saved)
        return self.music_root / "千羽下载"

    def _sync_dir(self) -> None:
        self.dir_label.setText(f"下载到：{self._download_folder()}")

    def _pick_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择下载目录", str(self._download_folder()))
        if folder:
            self.store.download_dir = folder
            self._sync_dir()

    def _start_search(self) -> None:
        kw = self.keyword.text().strip()
        if not kw:
            return
        if self.search_worker and self.search_worker.isRunning():
            return
        source = self.store.by_id(self.source_combo.currentData() or "")
        if not source:
            QMessageBox.warning(self, "没有源", "先到「源设置」里配一个源")
            return
        self.list.clear()
        self.results = []
        self.search_btn.setEnabled(False)
        self.search_btn.setText("搜索中…")
        self.status.setText("正在搜索…")
        self.search_worker = SearchWorker(source, kw, self)
        self.search_worker.ok.connect(self._on_search_ok)
        self.search_worker.fail.connect(self._on_search_fail)
        self.search_worker.finished.connect(self._reset_search_btn)
        self.search_worker.start()

    def _reset_search_btn(self) -> None:
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def _on_search_ok(self, results: list) -> None:
        self.results = results
        for it in results:
            title = it.get("title") or "未知歌曲"
            artist = it.get("artist") or "未知歌手"
            album = it.get("album") or ""
            text = f"{title} — {artist}" + (f"    《{album}》" if album else "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, self.list.count())
            self.list.addItem(item)
        self.status.setText(f"搜到 {len(results)} 首，双击下载" if results else "没搜到，换个关键词试试")

    def _on_search_fail(self, msg: str) -> None:
        self.status.setText(f"搜索失败：{msg}")

    def _start_download(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            QMessageBox.information(self, "慢慢来", "上一首还在下")
            return
        row = self.list.currentRow()
        if not (0 <= row < len(self.results)):
            self.status.setText("先选一首歌")
            return
        item = self.results[row]
        self.dl_btn.setEnabled(False)
        self.bar.setValue(0)
        self.status.setText(f"正在下载 {item.get('title', '')}…")
        self.download_worker = DownloadWorker(item, self._download_folder(), self)
        self.download_worker.tick.connect(self._on_tick)
        self.download_worker.ok.connect(self._on_download_ok)
        self.download_worker.fail.connect(self._on_download_fail)
        self.download_worker.finished.connect(lambda: self.dl_btn.setEnabled(True))
        self.download_worker.start()

    def _on_tick(self, got: int, total: int) -> None:
        if total > 0:
            self.bar.setValue(int(got * 100 / total))
            self.status.setText(f"下载中 {got // 1024} / {total // 1024} KB")
        else:
            self.bar.setRange(0, 0)
            self.status.setText(f"下载中 {got // 1024} KB")

    def _on_download_ok(self, path: str) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.status.setText(f"已保存：{Path(path).name}")
        self.downloaded.emit(path)

    def _on_download_fail(self, msg: str) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText(f"下载失败：{msg}")
