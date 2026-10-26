"""AI Memory Manager desktop application (PyQt6)."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QPoint, QProcess, QSettings, QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QIcon, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QScrollArea, QLayout,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ai_memory_manager.connectors.gpt.export_import import archive_export  # noqa: E402
from ai_memory_manager.connectors.local_sessions import (  # noqa: E402
    archive_local_session, detect_local_connectors, discover_local_sessions, transcript_messages,
)
from ai_memory_manager.memory import lifecycle as memory_api  # noqa: E402
from ai_memory_manager.personas import global_persona as global_persona_api  # noqa: E402
from ai_memory_manager.summaries import summaries as summary_api  # noqa: E402

ARCHIVE = ROOT / "src/ai_memory_manager/archive/Session_Lib"
PERSONAS = ROOT / "src/ai_memory_manager/personas"
PERSONA_SESSION_LIBRARIES = PERSONAS / ".session_libraries.json"
PERSONA_USAGE = ROOT / "src/ai_memory_manager/sync/persona_usage.json"
PERSONA_SENDER = ROOT / "src/ai_memory_manager/connectors/gpt/persona_send.mjs"
MEMORY_EXTRACTOR = ROOT / "src/ai_memory_manager/memory/archive_extraction.py"
def asset_path(name: str, fallback: str) -> Path:
    """优先使用本机可选素材，缺失时回退到仓库内置素材。"""
    preferred = ROOT / "ui/assets" / name
    return preferred if preferred.exists() else ROOT / "ui/assets" / fallback


APP_ICON = asset_path("memory-muse.png", "app-icon.svg")
SUMMARY_PAGE_CHARS = 850
CODEX_ICON = asset_path("codex-gpt-dragon.png", "codex-official.png")
DEEPSEEK_ICON = asset_path("deepseek-dragon.png", "deepseek-official.svg")
OFFICIAL_AI_ICONS = {
    "Codex": ROOT / "ui/assets/codex-official.png",
    "ChatGPT": ROOT / "ui/assets/openai-desktop-official.png",
    "DeepSeek": ROOT / "ui/assets/deepseek-official.svg",
}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def paginate_summary_text(
    text: str, page_chars: int = SUMMARY_PAGE_CHARS,
    fits: Callable[[str], bool] | None = None,
) -> list[str]:
    """Fill each page up to its character and visible-height limits."""

    remaining = text.strip() or "当天没有概要内容。"
    pages = []
    while remaining:
        limit = min(len(remaining), page_chars)
        if fits is not None:
            low, high = 1, limit
            while low < high:
                middle = (low + high + 1) // 2
                if fits(remaining[:middle]):
                    low = middle
                else:
                    high = middle - 1
            limit = low
        if len(remaining) <= limit:
            pages.append(remaining)
            break
        near_end = max(1, limit * 9 // 10)
        boundary = remaining.rfind("\n\n", near_end, limit + 1)
        if boundary >= 0:
            boundary += 2
        else:
            boundary = remaining.rfind("\n", near_end, limit + 1)
            if boundary >= 0:
                boundary += 1
            else:
                boundary = remaining.rfind(" ", near_end, limit + 1)
                if boundary >= 0:
                    boundary += 1
        if boundary <= 0:
            boundary = limit
        pages.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return pages


def session_records() -> list[dict[str, Any]]:
    records = []
    if not ARCHIVE.is_dir():
        return records
    folders = set(ARCHIVE.glob("*/*/*"))
    folders.update(path.parent for path in ARCHIVE.rglob("session.meta.json"))
    for folder in sorted(folders, reverse=True):
        if not folder.is_dir() or folder.name == "summaries":
            continue
        meta = read_json(folder / "session.meta.json") or {}
        if not isinstance(meta, dict):
            meta = {}
        files = [p for p in folder.iterdir() if p.is_file() and p.name != "session.meta.json"]
        if not files:
            continue
        relative = folder.relative_to(ARCHIVE)
        nested_source = relative.parts[1] if len(relative.parts) > 1 and relative.parts[0] == "OpenAI" else relative.parts[0]
        date_index = 2 if relative.parts and relative.parts[0] == "OpenAI" else 1
        title = meta.get("title") or (folder.name.rsplit("--", 1)[0] if "--" in folder.name else folder.name)
        records.append({
            "title": str(title),
            "provider": str(meta.get("provider") or nested_source),
            "date": str(meta.get("date") or (relative.parts[date_index] if len(relative.parts) > date_index else "")),
            "id": str(meta.get("conversation_id") or folder.name.split("--")[-1]),
            "persona": str(meta.get("persona_id") or "未关联人设"),
            "source_size": meta.get("source_size"),
            "source_mtime_ns": meta.get("source_mtime_ns"),
            "folder": folder,
            "files": files,
        })
    return records


def library_group(provider: str) -> str:
    """Treat ChatGPT and Codex as client sources within one OpenAI library."""
    return "OpenAI" if provider in {"ChatGPT", "Codex"} else provider


def session_text(record: dict[str, Any]) -> str:
    parts = []
    connector_id = {"Codex": "codex", "Claude Code": "claude-code"}.get(record["provider"])
    original = next((path for path in record["files"] if path.name == "session.jsonl"), None)
    original_messages = []
    if connector_id and original:
        try:
            original_messages = transcript_messages(original, connector_id)
        except OSError:
            pass
    parts.extend(f"{'你' if role == 'user' else 'AI'}：{content}" for role, content in original_messages)
    for path in record["files"]:
        try:
            if original_messages and path.name in {"session.jsonl", "messages.jsonl"}:
                continue
            if path.name == "session.jsonl" and connector_id:
                parts.extend(
                    f"{'你' if role == 'user' else 'AI'}：{content}"
                    for role, content in transcript_messages(path, connector_id)
                )
                continue
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                value = json.loads(text)
                def collect(node: Any) -> None:
                    if isinstance(node, dict):
                        for key, val in node.items():
                            if key in {"text", "content"} and isinstance(val, str):
                                parts.append(val)
                            else:
                                collect(val)
                    elif isinstance(node, list):
                        for val in node:
                            collect(val)
                collect(value)
            else:
                parts.append(text)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return "\n".join(parts)


def persona_records() -> list[dict[str, Any]]:
    records = []
    if not PERSONAS.is_dir():
        return records
    for path in sorted(PERSONAS.glob("*.json"), key=lambda p: p.name.casefold()):
        value = read_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("system_prompt"), str):
            continue
        records.append({
            "name": str(value.get("name") or path.stem),
            "id": str(value.get("id") or path.stem),
            "provider": str(value.get("provider") or "Codex"),
            "description": str(value.get("description") or ""),
            "system_prompt": value["system_prompt"],
            "path": path,
        })
    return records


class ThemedSelector(QPushButton):
    """A button-backed selector with an in-window menu and a centered arrow."""

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("themedSelector")
        self._items = []
        self._index = -1
        self._popup = None
        self._arrow = QLabel(self)
        self._arrow.setFixedSize(18, 18)
        self._arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._set_arrow(False)
        self.clicked.connect(self.showPopup)

    def addItem(self, label, data=None):
        self._items.append((label, data))
        if self._index < 0:
            self.setCurrentIndex(0)

    def clear(self):
        had_selection = self._index >= 0
        self._items.clear()
        self._index = -1
        self.setText("")
        if had_selection:
            self.currentIndexChanged.emit(-1)

    def count(self):
        return len(self._items)

    def itemData(self, index):
        return self._items[index][1] if 0 <= index < len(self._items) else None

    def currentData(self):
        return self.itemData(self._index)

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self._items) or index == self._index:
            return
        self._index = index
        self.setText(self._items[index][0])
        self.currentIndexChanged.emit(index)

    def _set_arrow(self, expanded):
        arrow = QIcon(str(ROOT / "ui/assets/chevron-down.svg")).pixmap(QSize(10, 6))
        if expanded:
            arrow = arrow.transformed(QTransform().rotate(180), Qt.TransformationMode.SmoothTransformation)
        self._arrow.setPixmap(arrow)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrow.move(self.width() - 28, (self.height() - self._arrow.height()) // 2)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Down:
            self.showPopup()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape and self._popup:
            self.hidePopup()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)

    def showPopup(self):
        if not self.count():
            return
        if self._popup:
            self.hidePopup()
            return
        popup = QFrame(self.window())
        popup.setObjectName("themedComboPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        options = QVBoxLayout(popup)
        options.setContentsMargins(4, 4, 4, 4)
        options.setSpacing(2)
        for index in range(self.count()):
            item = QPushButton(self._items[index][0])
            item.setObjectName("themedComboOptionSelected" if index == self._index else "themedComboOption")
            item.clicked.connect(lambda _checked=False, selected=index: self._choose_option(selected))
            options.addWidget(item)
        popup.adjustSize()
        height = popup.sizeHint().height()
        host = popup.parentWidget()
        position = self.mapTo(host, QPoint(0, self.height() + 4))
        if position.y() + height > host.height() - 8:
            position = self.mapTo(host, QPoint(0, -height - 4))
        popup.setGeometry(position.x(), position.y(), max(self.width(), popup.sizeHint().width()), height)
        self._popup = popup
        popup.show()
        popup.raise_()
        self._set_arrow(True)
        QApplication.instance().installEventFilter(self)

    def _choose_option(self, index):
        self.hidePopup()
        self.setCurrentIndex(index)

    def hidePopup(self):
        QApplication.instance().removeEventFilter(self)
        if self._popup:
            self._popup.hide()
            self._popup.deleteLater()
            self._popup = None
        self._set_arrow(False)

    def eventFilter(self, watched, event):
        if self._popup and event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
            if (watched is self or self.isAncestorOf(watched)
                    or watched is self._popup or self._popup.isAncestorOf(watched)):
                return False
            self.hidePopup()
        return super().eventFilter(watched, event)


class MemoryApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("让你的AI更加立体")
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.resize(1240, 800)
        self.setMinimumSize(920, 620)
        self.dark = QApplication.instance().property("dark_mode") is True
        self.records: list[dict[str, Any]] = []
        self.selected_provider: str | None = None
        self.selected_persona_id: str | None = None
        self.expanded_persona_ids: set[str] = set()
        self.active_persona_conversations: list[dict[str, str]] | None = None
        self.persona_status_process: QProcess | None = None
        self.persona_send_process: QProcess | None = None
        self.persona_clear_process: QProcess | None = None
        self.memory_extract_process: QProcess | None = None
        self.memory_search_process: QProcess | None = None
        self.auto_memory_process: QProcess | None = None
        self.auto_capture_process: QProcess | None = None
        self._closing = False
        self._memory_search_cache = None
        self._memory_search_pending = None
        self.memory_search_timer = QTimer(self)
        self.memory_search_timer.setSingleShot(True)
        self.memory_search_timer.setInterval(350)
        self.memory_search_timer.timeout.connect(self._run_memory_search)
        self.current_page = "总览"
        self.persona_watcher = QFileSystemWatcher(self)
        self.persona_watcher.directoryChanged.connect(self._schedule_persona_refresh)
        self.persona_watcher.fileChanged.connect(self._schedule_persona_refresh)
        self.persona_refresh_timer = QTimer(self)
        self.persona_refresh_timer.setSingleShot(True)
        self.persona_refresh_timer.setInterval(250)
        self.persona_refresh_timer.timeout.connect(self._refresh_persona_files)
        self._build()
        self.refresh()
        self._sync_persona_watchers()
        self.apply_theme()
        self.persona_usage_timer = QTimer(self)
        self.persona_usage_timer.setInterval(5000)
        self.persona_usage_timer.timeout.connect(self._poll_persona_usage)
        self.persona_usage_timer.start()
        self._poll_persona_usage()
        self.auto_memory_status_timer = QTimer(self)
        self.auto_memory_status_timer.setInterval(5000)
        self.auto_memory_status_timer.timeout.connect(self._refresh_automatic_memory_status)
        self.auto_memory_status_timer.start()
        QTimer.singleShot(0, self._start_automatic_memory)

    def _start_automatic_memory(self):
        if self._closing:
            return
        if self.auto_memory_process and self.auto_memory_process.state() != QProcess.ProcessState.NotRunning:
            return
        worker = QProcess(self)
        self.auto_memory_process = worker
        worker.readyReadStandardOutput.connect(lambda: (worker.readAllStandardOutput(), self._refresh_automatic_memory_status()))
        worker.readyReadStandardError.connect(lambda: worker.readAllStandardError())
        worker.finished.connect(lambda code, _status: QTimer.singleShot(5000, self._start_automatic_memory)
                                if code and not self._closing else None)
        worker.start(sys.executable, [str(ROOT / "src/ai_memory_manager/memory/worker.py"),
            "--archive", str(ARCHIVE), "--parent-pid", str(os.getpid()), "--capture"])
        self._refresh_automatic_memory_status()

    def _refresh_automatic_memory_status(self):
        path = ARCHIVE / ".memory_jobs.sqlite3"
        if not path.is_file():
            self.memory_auto_status.setText("自动跟进：等待归档会话。")
            return
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                counts = dict(db.execute("SELECT status,count(*) FROM jobs GROUP BY status"))
                errors = [row[0] for row in db.execute("SELECT error FROM jobs WHERE error IS NOT NULL LIMIT 3")]
            finally:
                db.close()
        except sqlite3.Error:
            return
        pending = counts.get("pending", 0) + counts.get("running", 0)
        self.memory_auto_status.setText(f"自动跟进：待处理 {pending} · 已完成 {counts.get('done', 0)} · "
            f"待重试 {counts.get('retry', 0)} · 待分配人设 {counts.get('needs_persona', 0)}")
        self.memory_auto_status.setToolTip("\n".join(errors))
        if self.current_page == "记忆更新":
            self.refresh_memories()

    def closeEvent(self, event):
        self._closing = True
        self.auto_memory_status_timer.stop()
        for process in (self.auto_memory_process, self.auto_capture_process,
                        self.memory_search_process, self.memory_extract_process):
            if process and process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(1200):
                    process.kill()
                    process.waitForFinished(500)
        super().closeEvent(event)

    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer_layout = QVBoxLayout(root)
        outer_layout.setContentsMargins(6, 6, 6, 6)
        outer_layout.setSpacing(0)
        self.outer_layout = outer_layout
        root.setMouseTracking(True)
        root.installEventFilter(self)

        self.title_bar = QFrame()
        self.title_bar.setObjectName("titleBar")
        self.title_bar.setFixedHeight(48)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(12, 0, 4, 0)
        title_layout.setSpacing(0)
        self.window_title_label = QLabel("让你的 AI 助手更懂你\nAn AI assistant that understands you better")
        self.window_title_label.setObjectName("windowTitle")
        self.window_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.window_title_label.setContentsMargins(120, 0, 0, 0)
        title_layout.addStretch(1)
        title_layout.addWidget(self.window_title_label, 2)
        title_layout.addStretch(1)
        controls = QHBoxLayout()
        controls.setSpacing(2)
        self.minimize_button = QPushButton("−")
        self.minimize_button.setObjectName("windowMinimize")
        self.minimize_button.setToolTip("最小化")
        self.minimize_button.clicked.connect(self.showMinimized)
        self.maximize_button = QPushButton("□")
        self.maximize_button.setObjectName("windowMaximize")
        self.maximize_button.setToolTip("最大化")
        self.maximize_button.clicked.connect(self.toggle_maximize)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("windowClose")
        self.close_button.setToolTip("关闭")
        self.close_button.clicked.connect(self.close)
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            button.setFixedSize(38, 30)
            controls.addWidget(button)
        title_layout.addLayout(controls)
        self.title_bar.mousePressEvent = self._start_window_move
        self.title_bar.mouseDoubleClickEvent = self._title_double_click
        self.window_title_label.mousePressEvent = self._start_window_move
        self.window_title_label.mouseDoubleClickEvent = self._title_double_click
        outer_layout.addWidget(self.title_bar)

        content = QWidget()
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(228)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(18, 24, 18, 18)
        side.setSpacing(8)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setObjectName("logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(74, 74)
        logo.setPixmap(QIcon(str(APP_ICON)).pixmap(QSize(72, 72)))
        titlebox = QVBoxLayout()
        name = QLabel("知忆")
        name.setObjectName("brand")
        sub = QLabel("你的 AI 记忆数据库\nAI Memory Manager")
        sub.setObjectName("caption")
        titlebox.addWidget(name)
        titlebox.addWidget(sub)
        brand.addWidget(logo)
        brand.addSpacing(10)
        brand.addLayout(titlebox)
        brand.addStretch()
        side.addLayout(brand)
        side.addSpacing(30)
        nav_label = QLabel("工作空间")
        nav_label.setObjectName("sectionLabel")
        side.addWidget(nav_label)
        self.nav_buttons = {}
        for page, icon in [("总览", "⌂"), ("会话库", "◷"), ("每日概要", "▤"), ("人格档案", "◉"), ("记忆更新", "✧")]:
            button = QPushButton(f"  {icon}    {page}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, p=page: self.navigate(p))
            self.nav_buttons[page] = button
            side.addWidget(button)
        side.addStretch()
        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        status_layout = QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(14, 14, 14, 14)
        status_head = QHBoxLayout()
        status_dot = QLabel("●")
        status_dot.setObjectName("greenDot")
        status_head.addWidget(status_dot)
        status_head.addWidget(QLabel("本地存储"))
        status_head.addStretch()
        status_layout.addLayout(status_head)
        self.path_label = QLabel(str(ARCHIVE))
        self.path_label.setObjectName("mutedSmall")
        self.path_label.setWordWrap(True)
        status_layout.addWidget(self.path_label)
        side.addWidget(self.status_card)
        layout.addWidget(self.sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(32, 24, 32, 24)
        main_layout.setSpacing(22)
        header = QHBoxLayout()
        self.page_title = QLabel("总览")
        self.page_title.setObjectName("pageTitle")
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕   搜索会话…")
        self.search.setObjectName("searchBox")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.textChanged.connect(self.on_search_changed)
        self.import_button = QPushButton("＋  导入会话")
        self.import_button.setObjectName("primaryButton")
        import_menu = QMenu(self.import_button)
        import_menu.addAction("ChatGPT 官方导出文件…", self.import_archive)
        import_menu.addAction("本机编码助手会话…", self.import_local_sessions)
        self.import_button.setMenu(import_menu)
        self.theme_button = QPushButton("☾")
        self.theme_button.setObjectName("iconButton")
        self.theme_button.setFixedSize(40, 40)
        self.theme_button.clicked.connect(self.toggle_theme)
        header.addWidget(self.page_title)
        header.addStretch()
        header.addWidget(self.search)
        header.addWidget(self.theme_button)
        header.addWidget(self.import_button)
        main_layout.addLayout(header)

        self.pages = QStackedWidget()
        self.pages.setObjectName("pages")
        self.overview_page = self._overview()
        self.sessions_page = self._sessions()
        self.summaries_page = self._summaries()
        self.personas_page = self._personas()
        self.memory_page = self._memory_workspace()
        for page in (self.overview_page, self.sessions_page, self.summaries_page, self.personas_page, self.memory_page):
            self.pages.addWidget(page)
        main_layout.addWidget(self.pages, 1)
        layout.addWidget(main, 1)
        outer_layout.addWidget(content, 1)

        refresh_action = QAction("刷新", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.refresh)
        self.addAction(refresh_action)

    def _overview(self):
        page = QWidget()
        page.setObjectName("page")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(18)
        intro = QLabel("你的 AI 对话，都在这里好好保存。")
        intro.setObjectName("intro")
        outer.addWidget(intro)
        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.stat_values = {}
        for key, label, icon in [("sessions", "已归档会话", "◷"), ("sources", "接入来源", "⌘"), ("days", "记录天数", "▦")]:
            card = QFrame()
            card.setObjectName("statCard")
            c = QVBoxLayout(card)
            c.setContentsMargins(18, 16, 18, 16)
            top = QHBoxLayout()
            small = QLabel(label)
            small.setObjectName("muted")
            glyph = QLabel(icon)
            glyph.setObjectName("statIcon")
            top.addWidget(small)
            top.addStretch()
            top.addWidget(glyph)
            value = QLabel("0")
            value.setObjectName("statValue")
            self.stat_values[key] = value
            c.addLayout(top)
            c.addWidget(value)
            cards.addWidget(card)
        outer.addLayout(cards)
        section = QHBoxLayout()
        recent_label = QLabel("最近会话")
        recent_label.setObjectName("sectionTitle")
        self.all_sessions = QPushButton("查看全部 →")
        self.all_sessions.setObjectName("linkButton")
        self.all_sessions.clicked.connect(lambda: self.navigate("会话库"))
        section.addWidget(recent_label)
        section.addStretch()
        section.addWidget(self.all_sessions)
        outer.addLayout(section)
        self.recent_list = QListWidget()
        self.recent_list.setObjectName("sessionList")
        self.recent_list.itemClicked.connect(self.open_session_item)
        outer.addWidget(self.recent_list, 1)
        return page

    def _sessions(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.sessions_back = QPushButton("←  所有来源")
        self.sessions_back.setObjectName("linkButton")
        self.sessions_back.clicked.connect(self.show_sources)
        self.sessions_hint = QLabel("OpenAI 共用一个会话库，具体会话会标明来自 ChatGPT 或 Codex。")
        self.sessions_hint.setObjectName("muted")
        top.addWidget(self.sessions_back)
        top.addWidget(self.sessions_hint)
        top.addStretch()
        layout.addLayout(top)
        self.session_list = QListWidget()
        self.session_list.setObjectName("sessionList")
        self.session_list.setIconSize(QSize(52, 52))
        self.session_list.itemClicked.connect(self.activate_session_item)
        layout.addWidget(self.session_list, 1)
        return page

    def _summaries(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        intro = QLabel("每日概要从本地会话中提取重点，可随时回到原始记录。")
        intro.setObjectName("muted")
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.generate_button = QPushButton("生成 / 更新概要")
        self.generate_button.setObjectName("primaryButton")
        self.generate_button.clicked.connect(self.generate_summaries)
        row.addWidget(self.generate_button)
        row.addStretch()
        layout.addLayout(row)
        self.summary_list = QListWidget()
        self.summary_list.setObjectName("sessionList")
        self.summary_list.itemClicked.connect(self.show_summary)
        layout.addWidget(self.summary_list, 1)
        return page

    def _personas(self):
        page = QScrollArea()
        page.setObjectName("personaPageScroll")
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("personaPageContent")
        layout = QVBoxLayout(content)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("独立管理不同 AI 身份的人设资料。")
        title.setObjectName("muted")
        layout.addWidget(title)
        card = QFrame()
        card.setObjectName("emptyCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 16)
        card_layout.setSpacing(10)
        card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        card_header = QHBoxLayout()
        card_header.addStretch()
        self.persona_provider = "Codex"
        self.persona_target_selector = QPushButton()
        self.persona_target_selector.setObjectName("personaTargetSelector")
        selector_layout = QHBoxLayout(self.persona_target_selector)
        selector_layout.setContentsMargins(10, 0, 10, 0)
        selector_layout.setSpacing(6)
        self.persona_selector_icon = QLabel()
        self.persona_selector_icon.setPixmap(QIcon(str(OFFICIAL_AI_ICONS["Codex"])).pixmap(QSize(18, 18)))
        self.persona_selector_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        selector_layout.addWidget(self.persona_selector_icon)
        self.persona_selector_name = QLabel("Codex")
        self.persona_selector_name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        selector_layout.addWidget(self.persona_selector_name)
        selector_layout.addStretch(1)
        self.persona_selector_arrow = QLabel()
        self.persona_selector_arrow.setObjectName("personaSelectorArrow")
        self.persona_selector_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.persona_selector_arrow.setFixedSize(18, 18)
        self.persona_selector_arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        selector_layout.addWidget(self.persona_selector_arrow)
        self._set_persona_selector_arrow(False)
        self.persona_target_selector.clicked.connect(self._toggle_persona_provider_menu)
        self.persona_target_selector.setFixedWidth(160)
        card_header.addWidget(self.persona_target_selector)
        self.persona_send_button = QPushButton("导入到当前 AI 对话")
        self.persona_send_button.setObjectName("linkButton")
        self.persona_send_button.setToolTip("向所选 AI 的当前对话发送这份人设。若输入框中已有草稿，将停止发送。")
        self.persona_send_button.setEnabled(False)
        self.persona_send_button.clicked.connect(self.send_persona_to_ai)
        card_header.addWidget(self.persona_send_button)
        self.persona_clear_button = QPushButton("清除当前 AI 人设")
        self.persona_clear_button.setObjectName("linkButton")
        self.persona_clear_button.setToolTip("向所选 AI 的当前对话发送停止使用所选人设的消息，确认发送后移除本地绑定。")
        self.persona_clear_button.setEnabled(False)
        self.persona_clear_button.clicked.connect(self.clear_persona_from_ai)
        card_header.addWidget(self.persona_clear_button)
        self.global_persona_button = QPushButton("设为全局人设")
        self.global_persona_button.setObjectName("primaryButton")
        self.global_persona_button.setEnabled(False)
        self.global_persona_button.setText("设为全局人设")
        self.global_persona_button.setToolTip("先选择下方的人设；全局设置目前支持 Codex")
        self.global_persona_button.clicked.connect(self.toggle_global_persona)
        self.persona_edit_button = QPushButton("编辑人设")
        self.persona_edit_button.setObjectName("linkButton")
        self.persona_edit_button.setEnabled(False)
        self.persona_edit_button.clicked.connect(self.edit_persona)
        card_header.addWidget(self.persona_edit_button)
        self.persona_import_button = QPushButton("＋  导入人设 JSON")
        self.persona_import_button.setObjectName("primaryButton")
        self.persona_import_button.clicked.connect(self.import_persona)
        card_header.addWidget(self.persona_import_button)
        card_layout.addLayout(card_header)
        card_layout.addWidget(self.global_persona_button, 0, Qt.AlignmentFlag.AlignLeft)
        # Keep the menu above the card and its rows, including where it extends
        # beyond the card's bottom edge.
        self.persona_provider_menu = QFrame(content)
        self.persona_provider_menu.setObjectName("personaTargetPopup")
        self.persona_provider_menu.setFixedWidth(160)
        menu_layout = QVBoxLayout(self.persona_provider_menu)
        menu_layout.setContentsMargins(4, 4, 4, 4)
        menu_layout.setSpacing(2)
        self.persona_provider_options = {}
        for provider in ("Codex", "DeepSeek"):
            option = QPushButton(provider)
            option.setIcon(QIcon(str(OFFICIAL_AI_ICONS[provider])))
            option.setIconSize(QSize(18, 18))
            option.setObjectName("personaProviderOptionSelected" if provider == self.persona_provider else "personaProviderOption")
            option.clicked.connect(lambda _checked=False, value=provider: self._select_persona_provider(value))
            menu_layout.addWidget(option)
            self.persona_provider_options[provider] = option
        self.persona_provider_menu.hide()
        self.persona_rows_widget = QWidget()
        self.persona_rows_layout = QVBoxLayout(self.persona_rows_widget)
        self.persona_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.persona_rows_layout.setSpacing(8)
        card_layout.addWidget(self.persona_rows_widget)
        self.persona_usage_row = QWidget()
        self.persona_usage_layout = QHBoxLayout(self.persona_usage_row)
        self.persona_usage_layout.setContentsMargins(0, 0, 0, 0)
        self.persona_usage_layout.setSpacing(6)
        self.persona_usage_label = QLabel()
        self.persona_usage_label.setObjectName("muted")
        self.persona_usage_layout.addWidget(self.persona_usage_label)
        self.persona_usage_layout.addStretch()
        self.persona_usage_row.hide()
        card_layout.addWidget(self.persona_usage_row)
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)
        page.setWidget(content)
        self.refresh_personas()
        return page

    def _memory_workspace(self):
        page = QWidget()
        page.setObjectName("memoryPage")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        intro = QLabel("可从所选归档会话提取长期记忆；当前对话中的 AI 也能按需要检索和更新。你可以删除不想保留的记忆。")
        intro.setObjectName("intro")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        controls = QFrame()
        controls.setObjectName("emptyCard")
        row = QHBoxLayout(controls)
        row.setContentsMargins(14, 10, 14, 10)
        row.addWidget(QLabel("目标人设"))
        self.memory_persona_selector = ThemedSelector()
        self.memory_persona_selector.setMinimumWidth(180)
        self.memory_persona_selector.currentIndexChanged.connect(self.refresh_memories)
        row.addWidget(self.memory_persona_selector)
        self.memory_search = QLineEdit()
        self.memory_search.setPlaceholderText("搜索当前人设的记忆…")
        self.memory_search.setClearButtonEnabled(True)
        self.memory_search.setFixedWidth(220)
        self.memory_search.textChanged.connect(self.refresh_memories)
        row.addWidget(self.memory_search)
        row.addStretch()
        self.memory_status_selector = ThemedSelector()
        self.memory_status_selector.addItem("当前有效", "active")
        self.memory_status_selector.addItem("已替代", "superseded")
        self.memory_status_selector.addItem("已过期", "expired")
        self.memory_status_selector.currentIndexChanged.connect(self.refresh_memories)
        row.addWidget(self.memory_status_selector)
        self.memory_extract_button = QPushButton("从归档提取")
        self.memory_extract_button.setObjectName("linkButton")
        self.memory_extract_button.setToolTip("选择归档会话，使用本机模型提取到当前人设")
        self.memory_extract_button.setEnabled(False)
        self.memory_extract_button.clicked.connect(self.extract_archived_memories)
        row.addWidget(self.memory_extract_button)
        self.memory_delete_button = QPushButton("删除选中记忆")
        self.memory_delete_button.setObjectName("linkButton")
        self.memory_delete_button.setEnabled(False)
        self.memory_delete_button.clicked.connect(self.delete_selected_memory)
        row.addWidget(self.memory_delete_button)
        outer.addWidget(controls)
        self.memory_extract_status = QLabel("选择要提取的归档会话；提取在本机运行。")
        self.memory_extract_status.setObjectName("muted")
        outer.addWidget(self.memory_extract_status)
        self.memory_auto_status = QLabel("自动跟进：正在启动…")
        self.memory_auto_status.setObjectName("muted")
        self.memory_auto_status.setWordWrap(True)
        outer.addWidget(self.memory_auto_status)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        active_card = QFrame()
        active_card.setObjectName("emptyCard")
        active_layout = QVBoxLayout(active_card)
        active_layout.setContentsMargins(14, 14, 14, 14)
        active_head = QHBoxLayout()
        active_head.addWidget(QLabel("记忆档案"))
        active_head.addStretch()
        self.memory_count_label = QLabel("0 条")
        self.memory_count_label.setObjectName("muted")
        active_head.addWidget(self.memory_count_label)
        active_layout.addLayout(active_head)
        self.memory_list = QListWidget()
        self.memory_list.setObjectName("sessionList")
        self.memory_list.currentItemChanged.connect(self._memory_selected)
        self.memory_list.itemSelectionChanged.connect(self._memory_selection_changed)
        active_layout.addWidget(self.memory_list, 1)
        columns.addWidget(active_card, 3)

        pending_card = QFrame()
        pending_card.setObjectName("emptyCard")
        pending_layout = QVBoxLayout(pending_card)
        pending_layout.setContentsMargins(14, 14, 14, 14)
        pending_layout.addWidget(QLabel("最近自动写入"))
        self.memory_updates_list = QListWidget()
        self.memory_updates_list.setObjectName("sessionList")
        self.memory_updates_list.currentItemChanged.connect(self._memory_update_selected)
        pending_layout.addWidget(self.memory_updates_list, 1)
        self.memory_update_detail = QLabel("模型调用记忆工具后，写入记录会显示在这里。")
        self.memory_update_detail.setObjectName("muted")
        self.memory_update_detail.setWordWrap(True)
        pending_layout.addWidget(self.memory_update_detail)
        columns.addWidget(pending_card, 2)
        outer.addLayout(columns, 1)
        self.refresh_memory_personas()
        return page

    def _memory_db_path(self, persona):
        # Use the selected local persona filename, never an ID read from JSON as a path.
        return PERSONAS / ".memory" / f"{Path(persona['path']).stem}.sqlite3"

    def _memory_search_stamp(self, db_path):
        stamp = []
        for path in (Path(db_path), Path(str(db_path) + "-wal")):
            try:
                info = path.stat()
                stamp.append((info.st_mtime_ns, info.st_size))
            except FileNotFoundError:
                stamp.append(None)
        return tuple(stamp)

    def _run_memory_search(self):
        key = self._memory_search_pending
        if not key:
            return
        process = QProcess(self)
        self.memory_search_process = process
        def finished(code, _status):
            if self._memory_search_pending == key:
                try:
                    rows = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8")) if code == 0 else []
                except (ValueError, UnicodeError):
                    rows = []
                self._memory_search_cache = (key, rows, self._memory_search_stamp(key[0]))
                self.refresh_memories()
            if self.memory_search_process is process:
                self.memory_search_process = None
            process.deleteLater()
        process.finished.connect(finished)
        process.start(sys.executable, [str(ROOT / "src/ai_memory_manager/memory/semantic.py"), *key])

    def extract_archived_memories(self):
        persona = self.memory_persona_selector.currentData()
        if not persona or self.memory_extract_process:
            return
        records = [record for record in session_records()
                   if (record["folder"] / "session.meta.json").is_file()]
        if not records:
            self.show_message(QMessageBox.Icon.Information, self, "暂无可提取会话", "请先导入会话归档。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"提取到「{persona['name']}」")
        dialog.resize(660, 480)
        layout = QVBoxLayout(dialog)
        hint = QLabel("勾选包含这份人设相关信息的会话。提取结果会写入当前人设的记忆档案。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        sessions = QListWidget()
        for record in records:
            item = QListWidgetItem(f"{record['title']} · {record['provider']} · {record['date']}")
            item.setData(Qt.ItemDataRole.UserRole, str(record["folder"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if record["persona"] == persona["id"]
                               else Qt.CheckState.Unchecked)
            sessions.addItem(item)
        layout.addWidget(sessions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始提取")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.prepare_dialog(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = [sessions.item(index).data(Qt.ItemDataRole.UserRole) for index in range(sessions.count())
                    if sessions.item(index).checkState() == Qt.CheckState.Checked]
        if not selected:
            return
        self.memory_extract_button.setEnabled(False)
        self.memory_extract_status.setText("正在用本机模型提取，较长的会话可能需要几分钟…")
        process = QProcess(self)
        self.memory_extract_process = process
        process.finished.connect(self._memory_extract_finished)
        process.errorOccurred.connect(self._memory_extract_error)
        arguments = [str(MEMORY_EXTRACTOR), str(persona["path"])]
        for path in selected:
            arguments.extend(["--session", path])
        process.start(sys.executable, arguments)

    def _memory_extract_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        process = self.memory_extract_process
        if not process:
            return
        self.memory_extract_process = None
        self.memory_extract_button.setEnabled(bool(self.memory_persona_selector.currentData()))
        self.memory_extract_status.setText("提取未完成。")
        process.deleteLater()
        self.show_message(QMessageBox.Icon.Warning, self, "提取失败", "无法启动本机提取程序。")

    def _memory_extract_finished(self, code, _status):
        process = self.memory_extract_process
        if not process:
            return
        self.memory_extract_process = None
        self.memory_extract_button.setEnabled(bool(self.memory_persona_selector.currentData()))
        output = bytes(process.readAllStandardOutput()).decode("utf-8", "replace").strip()
        error = bytes(process.readAllStandardError()).decode("utf-8", "replace").strip()
        process.deleteLater()
        if code != 0:
            self.memory_extract_status.setText("提取未完成，已处理的结果已保留，可重试继续。")
            self._memory_rendered_snapshot = None
            self.refresh_memories()
            self.show_message(QMessageBox.Icon.Warning, self, "提取失败", error or "本机模型未返回结果。")
            return
        try:
            counts = json.loads(output)
        except ValueError:
            self.memory_extract_status.setText("提取未完成。")
            self.show_message(QMessageBox.Icon.Warning, self, "提取失败", "无法读取提取结果。")
            return
        self.memory_extract_status.setText(
            f"已扫描 {counts['sessions']} 个关联会话，处理 {counts['processed']} 条用户消息，"
            f"新增 {counts['created']} 条记忆。")
        self._memory_rendered_snapshot = None
        self.refresh_memories()

    def refresh_memory_personas(self):
        if not hasattr(self, "memory_persona_selector"):
            return
        current = self.memory_persona_selector.currentData()
        current_id = current.get("id") if isinstance(current, dict) else None
        self.memory_persona_selector.blockSignals(True)
        self.memory_persona_selector.clear()
        for persona in persona_records():
            self.memory_persona_selector.addItem(persona["name"], persona)
        index = next((i for i in range(self.memory_persona_selector.count())
                      if (self.memory_persona_selector.itemData(i) or {}).get("id") == current_id), 0)
        if self.memory_persona_selector.count():
            self.memory_persona_selector.setCurrentIndex(index)
        self.memory_persona_selector.blockSignals(False)
        self.memory_extract_button.setEnabled(bool(self.memory_persona_selector.currentData())
                                              and self.memory_extract_process is None)
        self.refresh_memories()

    def refresh_memories(self, *_args):
        if not hasattr(self, "memory_list"):
            return
        persona = self.memory_persona_selector.currentData()
        memory_rows = []
        update_rows = []
        searching = False
        if persona:
            db_path = self._memory_db_path(persona)
            memory_api.expire_due(db_path)
            status = self.memory_status_selector.currentData()
            query = self.memory_search.text().strip()
            if query and status == "active":
                key = (str(db_path), query)
                cache = self._memory_search_cache
                valid_cache = cache and cache[0] == key and cache[2] == self._memory_search_stamp(db_path)
                memories = cache[1] if valid_cache else []
                searching = not valid_cache
                if cache and cache[0] == key and not valid_cache and not self.memory_search_process:
                    self._memory_search_pending = None
                if not valid_cache and self._memory_search_pending != key:
                    self._memory_search_pending = key
                    if self.memory_search_process:
                        self.memory_search_process.kill()
                    self.memory_search_timer.start()
            else:
                self._memory_search_pending = None
                self.memory_search_timer.stop()
                memories = memory_api.list_memories(db_path, status=status)
            if query and status != "active":
                memories = [item for item in memories if query.casefold() in
                            (item["subject"] + " " + item["content"]).casefold()]
            for memory in memories:
                valid_until = f"  ·  有效至 {memory['valid_until'][:10]}" if memory.get("valid_until") else ""
                memory_rows.append((
                    f"{memory['subject']}\n{memory['content']}\n{memory['memory_type']}  ·  来源：{memory['source']}{valid_until}",
                    memory["id"],
                ))
            for candidate in memory_api.list_candidates(db_path, status="accepted"):
                payload = candidate["payload"]
                if query and query.casefold() not in (payload["subject"] + " " + payload["content"]).casefold():
                    continue
                update_rows.append((
                    f"{payload['subject']}\n{payload['content']}\n{payload['memory_type']}  ·  {payload['source']}",
                    candidate["id"], candidate,
                ))
        else:
            memories = []
        snapshot = (persona["id"] if persona else None, self.memory_status_selector.currentData(),
                    self.memory_search.text().strip(), searching,
                    memory_rows, update_rows)
        selected_memory = self.memory_list.currentItem()
        selected_memory_id = selected_memory.data(Qt.ItemDataRole.UserRole) if selected_memory else None
        if snapshot == getattr(self, "_memory_rendered_snapshot", None):
            self.memory_delete_button.setEnabled(bool(selected_memory_id))
            return
        self._memory_rendered_snapshot = snapshot
        selected = self.memory_updates_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.memory_list.setUpdatesEnabled(False)
        self.memory_updates_list.setUpdatesEnabled(False)
        self.memory_updates_list.blockSignals(True)
        try:
            self.memory_list.clear()
            self.memory_updates_list.clear()
            self.memory_count_label.setText("检索中…" if searching else f"{len(memories)} 条")
            if memory_rows:
                for label, memory_id in memory_rows:
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, memory_id)
                    self.memory_list.addItem(item)
                    if memory_id == selected_memory_id:
                        self.memory_list.setCurrentItem(item)
            else:
                self.memory_list.addItem("正在检索本地记忆…" if searching
                                         else "没有找到匹配的记忆。" if persona and self.memory_search.text().strip()
                                         else "还没有记忆。模型判定有值得保留的信息后会自动写入。" if persona
                                         else "先在人格档案中导入或创建一个人设。")
            for label, candidate_id, candidate in update_rows:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, candidate_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, candidate)
                self.memory_updates_list.addItem(item)
                if candidate_id == selected_id:
                    self.memory_updates_list.setCurrentItem(item)
            if not update_rows:
                self.memory_updates_list.addItem("暂无自动写入记录。")
        finally:
            self.memory_updates_list.blockSignals(False)
            self.memory_list.setUpdatesEnabled(True)
            self.memory_updates_list.setUpdatesEnabled(True)
        self.memory_delete_button.setEnabled(bool(self.memory_list.currentItem()
                                                  and self.memory_list.currentItem().data(Qt.ItemDataRole.UserRole)))
        self._memory_update_selected(self.memory_updates_list.currentItem(), None)

    def _memory_selected(self, item, _previous):
        self.memory_delete_button.setEnabled(bool(item and item.data(Qt.ItemDataRole.UserRole)))

    def _memory_selection_changed(self):
        selected = self.memory_list.selectedItems()
        self.memory_delete_button.setEnabled(bool(
            selected and selected[0].data(Qt.ItemDataRole.UserRole)
        ))

    def _memory_update_selected(self, item, _previous):
        candidate = item.data(Qt.ItemDataRole.UserRole + 1) if item else None
        if not candidate:
            self.memory_update_detail.setText("模型调用记忆工具后，写入记录会显示在这里。")
            return
        payload = candidate["payload"]
        self.memory_update_detail.setText(
            f"来源：{payload['source']}\n会话：{payload.get('conversation_id') or '—'}\n"
            f"消息：{payload.get('source_message_id') or '—'}\n置信度：{payload['confidence']:.0%}"
        )

    def delete_selected_memory(self):
        persona = self.memory_persona_selector.currentData()
        selected = self.memory_list.currentItem()
        memory_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        if not persona or not memory_id:
            return
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("删除记忆？")
        message.setText("这会从本地记忆档案中永久删除该条记忆及其修订记录。此操作无法撤销。")
        delete_button = message.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        message.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        self.prepare_message_box(message)
        message.exec()
        if message.clickedButton() is not delete_button:
            return
        try:
            memory_api.delete_memory(self._memory_db_path(persona), memory_id, actor="user")
        except (KeyError, ValueError, OSError, sqlite3.Error, PermissionError) as error:
            self.show_message(QMessageBox.Icon.Warning, self, "删除失败", str(error))
            return
        self._memory_rendered_snapshot = None
        self.refresh_memories()

    def refresh_personas(self):
        if not hasattr(self, "persona_rows_layout"):
            return
        provider = self.persona_provider
        records = [persona for persona in persona_records() if persona["provider"] == provider]
        snapshot = (provider, records)
        if getattr(self, "_personas_rendered_snapshot", None) == snapshot:
            return
        self._personas_rendered_snapshot = snapshot
        while self.persona_rows_layout.count():
            child = self.persona_rows_layout.takeAt(0)
            if child.widget():
                child.widget().hide()
                child.widget().deleteLater()
        self.persona_rows = {}
        self.persona_prompt_labels = {}
        self.persona_expand_buttons = {}
        self.personas = records
        for persona in self.personas:
            identity = f"{persona['name']}  ·  {persona['id']}"
            description = persona["description"].strip()
            row = QFrame(self.persona_rows_widget)
            row.setObjectName("personaRowSelected" if persona["id"] == self.selected_persona_id else "personaRow")
            row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 8, 8)
            row_layout.setSpacing(8)
            text_column = QVBoxLayout()
            text_column.setContentsMargins(0, 0, 0, 0)
            text_column.setSpacing(2)
            name_button = QPushButton(identity)
            name_button.setObjectName("linkButton")
            name_button.setProperty("personaIdentity", True)
            name_button.setStyleSheet("text-align:left; padding:1px 4px;")
            name_button.clicked.connect(lambda _checked=False, persona_id=persona["id"]: self.show_persona(persona_id))
            text_column.addWidget(name_button)
            if description:
                description_label = QLabel(description)
                description_label.setObjectName("muted")
                description_label.setProperty("personaDescription", True)
                description_label.setWordWrap(True)
                description_label.setToolTip(description)
                text_column.addWidget(description_label)
            full_prompt = QLabel(persona["system_prompt"])
            full_prompt.setObjectName("muted")
            full_prompt.setProperty("personaFullPrompt", True)
            full_prompt.setWordWrap(True)
            full_prompt.setTextFormat(Qt.TextFormat.PlainText)
            full_prompt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            full_prompt.setVisible(persona["id"] in self.expanded_persona_ids)
            text_column.addWidget(full_prompt)
            row_layout.addLayout(text_column, 1)
            actions = QVBoxLayout()
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setSpacing(4)
            manage_button = QPushButton("相关会话管理")
            manage_button.setObjectName("personaManageButton")
            manage_button.setProperty("personaSessionManager", True)
            manage_button.clicked.connect(lambda _checked=False, current=persona: self.manage_persona_sessions(current))
            actions.addWidget(manage_button)
            expand_button = QPushButton("收起全文" if persona["id"] in self.expanded_persona_ids else "展开全文")
            expand_button.setObjectName("linkButton")
            expand_button.setProperty("personaExpandButton", True)
            expand_button.setVisible(bool(persona["system_prompt"].strip()))
            expand_button.clicked.connect(lambda _checked=False, persona_id=persona["id"]:
                                          self.toggle_persona_detail(persona_id))
            actions.addWidget(expand_button, 0, Qt.AlignmentFlag.AlignRight)
            actions.addStretch(1)
            row_layout.addLayout(actions)
            self.persona_rows_layout.addWidget(row)
            self.persona_rows[persona["id"]] = row
            self.persona_prompt_labels[persona["id"]] = full_prompt
            self.persona_expand_buttons[persona["id"]] = expand_button
        if not self.personas:
            empty = QLabel("暂无")
            empty.setObjectName("muted")
            self.persona_rows_layout.addWidget(empty)
            self.persona_edit_button.setEnabled(False)
            self.persona_send_button.setEnabled(False)
            self.persona_clear_button.setEnabled(False)
            self.global_persona_button.setEnabled(False)
            self.global_persona_button.setText("设为全局人设")
            self.persona_usage_row.hide()
        elif self.selected_persona_id:
            current = next((p for p in self.personas if p["id"] == self.selected_persona_id), None)
            if current:
                self.persona_edit_button.setEnabled(True)
                self.persona_send_button.setEnabled(True)
                self._show_persona_record(current)
            else:
                self.selected_persona_id = None
                self.persona_edit_button.setEnabled(False)
                self.persona_send_button.setEnabled(False)
                self.persona_clear_button.setEnabled(False)
                self.global_persona_button.setEnabled(False)
                self.global_persona_button.setText("设为全局人设")
                self.persona_usage_row.hide()
        else:
            self.persona_edit_button.setEnabled(False)
            self.persona_send_button.setEnabled(False)
            self.persona_clear_button.setEnabled(False)
            self.global_persona_button.setEnabled(False)
            self.global_persona_button.setText("设为全局人设")
            self.persona_usage_row.hide()
        self._sync_persona_watchers()

    def _set_persona_selector_arrow(self, expanded):
        arrow = QIcon(str(ROOT / "ui/assets/chevron-down.svg")).pixmap(QSize(10, 6))
        if expanded:
            arrow = arrow.transformed(QTransform().rotate(180), Qt.TransformationMode.SmoothTransformation)
        self.persona_selector_arrow.setPixmap(arrow)

    def _toggle_persona_provider_menu(self):
        menu = self.persona_provider_menu
        if menu.isVisible():
            menu.hide()
            self._set_persona_selector_arrow(False)
            return
        menu.adjustSize()
        position = self.persona_target_selector.mapTo(menu.parentWidget(),
                                                       self.persona_target_selector.rect().bottomLeft())
        menu.move(position.x(), position.y() + 4)
        menu.show()
        menu.raise_()
        self._set_persona_selector_arrow(True)

    def _select_persona_provider(self, provider):
        self.persona_provider_menu.hide()
        self._set_persona_selector_arrow(False)
        if provider == self.persona_provider:
            return
        self.persona_provider = provider
        self.persona_selector_name.setText(provider)
        self.persona_selector_icon.setPixmap(QIcon(str(OFFICIAL_AI_ICONS[provider])).pixmap(QSize(18, 18)))
        for name, option in self.persona_provider_options.items():
            option.setObjectName("personaProviderOptionSelected" if name == provider else "personaProviderOption")
            option.style().unpolish(option)
            option.style().polish(option)
        self._persona_provider_changed()

    def _persona_provider_changed(self):
        self.selected_persona_id = None
        self.persona_edit_button.setEnabled(False)
        self.persona_send_button.setEnabled(False)
        self.persona_clear_button.setEnabled(False)
        self.persona_send_button.setVisible(True)
        self.persona_clear_button.setVisible(True)
        self.global_persona_button.setEnabled(False)
        self.global_persona_button.setText("设为全局人设")
        self.persona_usage_row.hide()
        self.refresh_personas()

    def manage_persona_sessions(self, persona):
        sessions = session_records()
        library_data = read_json(PERSONA_SESSION_LIBRARIES)
        libraries = library_data.get("personas", {}) if isinstance(library_data, dict) else {}
        saved = libraries.get(persona["id"], []) if isinstance(libraries, dict) else []
        saved_keys = {(str(entry.get("provider")), str(entry.get("conversation_id")))
                      for entry in saved if isinstance(entry, dict)}
        usage = self._read_persona_usage()
        match_terms = set()
        if len(persona["id"].strip()) >= 3:
            match_terms.add(persona["id"].strip().casefold())
        if len(persona["name"].strip()) >= 3:
            match_terms.add(persona["name"].strip().casefold())

        dialog = QDialog(self)
        dialog.setWindowTitle(f"{persona['name']} · 相关会话管理")
        dialog.resize(680, 560)
        layout = QVBoxLayout(dialog)
        hint = QLabel("以下仅显示已关联或自动检索到的人格相关会话；每条记录对应完整会话，双击可查看。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        results = QListWidget()
        results.setObjectName("sessionList")
        results.itemDoubleClicked.connect(self.open_session_item)
        layout.addWidget(results, 1)

        matched_records = []
        for record in sessions:
            key = (record["provider"], record["id"])
            content = (record["title"] + " " + session_text(record)).casefold()
            usage_match = any(entry.get("persona_id") == persona["id"]
                              and entry.get("provider") == record["provider"]
                              and str(entry.get("conversation_id")) == record["id"] for entry in usage)
            metadata_match = record["persona"] == persona["id"]
            text_match = any(term and term in content for term in match_terms)
            matched = key in saved_keys or metadata_match or usage_match or text_match
            if not matched:
                continue
            matched_records.append(record)
            label = f"{record['title']}  ·  {record['provider']}  ·  {record['date']}"
            if metadata_match:
                label += "  ·  已关联"
            elif text_match or usage_match:
                label += "  ·  自动匹配"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, {"record": record})
            results.addItem(item)
        if not matched_records:
            results.addItem(QListWidgetItem("暂无关联会话"))

        latest = read_json(PERSONA_SESSION_LIBRARIES)
        all_libraries = latest.get("personas", {}) if isinstance(latest, dict) else {}
        if not isinstance(all_libraries, dict):
            all_libraries = {}
        all_libraries[persona["id"]] = [
            {"provider": record["provider"], "conversation_id": record["id"]}
            for record in matched_records
        ]
        try:
            PERSONA_SESSION_LIBRARIES.parent.mkdir(parents=True, exist_ok=True)
            temporary = PERSONA_SESSION_LIBRARIES.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({"personas": all_libraries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(PERSONA_SESSION_LIBRARIES)
        except OSError as error:
            self.show_message(QMessageBox.Icon.Critical, self, "会话库更新失败", f"无法保存人格会话库：{error}")
            return

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.prepare_dialog(dialog)
        dialog.exec()

    def prepare_dialog(self, dialog):
        """Apply the current app theme and a custom titlebar to every app dialog."""
        dialog.setObjectName("themedDialog")
        dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.FramelessWindowHint)
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        titlebar = QFrame(dialog)
        titlebar.setObjectName("dialogTitleBar")
        titlebar.setFixedHeight(42)
        bar = QHBoxLayout(titlebar)
        bar.setContentsMargins(12, 0, 6, 0)
        bar.setSpacing(4)
        title = QLabel(dialog.windowTitle())
        title.setObjectName("dialogTitle")
        bar.addWidget(title, 1)
        for symbol, object_name, tip, action in (
            ("−", "dialogMinimize", "最小化", dialog.showMinimized),
            ("□", "dialogMaximize", "最大化", lambda: dialog.showNormal() if dialog.isMaximized() else dialog.showMaximized()),
            ("×", "dialogClose", "关闭", dialog.reject),
        ):
            control = QPushButton(symbol)
            control.setObjectName(object_name)
            control.setToolTip(tip)
            control.setFixedSize(34, 28)
            control.clicked.connect(action)
            bar.addWidget(control)
        def move_dialog(event):
            if event.button() == Qt.MouseButton.LeftButton and dialog.windowHandle():
                dialog.windowHandle().startSystemMove()
            event.accept()
        titlebar.mousePressEvent = move_dialog
        title.mousePressEvent = move_dialog
        titlebar.mouseDoubleClickEvent = lambda event: (dialog.showNormal() if dialog.isMaximized() else dialog.showMaximized())
        layout = dialog.layout()
        if isinstance(layout, QFormLayout):
            layout.insertRow(0, titlebar)
        elif isinstance(layout, QVBoxLayout):
            layout.insertWidget(0, titlebar)
        else:
            layout.addWidget(titlebar)
        dialog.setMinimumSize(max(dialog.minimumWidth(), 360), max(dialog.minimumHeight(), 220))

    def prepare_message_box(self, message):
        """Give short alerts the same themed frame as the rest of the app."""
        message.setObjectName("themedDialog")
        message.setWindowFlags(message.windowFlags() | Qt.WindowType.FramelessWindowHint)
        message.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        message.setWindowIcon(QIcon(str(APP_ICON)))
        layout = message.layout()
        if not isinstance(layout, QGridLayout):
            return
        existing = []
        while layout.count():
            row, column, row_span, column_span = layout.getItemPosition(0)
            existing.append((layout.takeAt(0), row, column, row_span, column_span))
        titlebar = QFrame(message)
        titlebar.setObjectName("dialogTitleBar")
        titlebar.setFixedHeight(38)
        bar = QHBoxLayout(titlebar)
        bar.setContentsMargins(12, 0, 5, 0)
        title = QLabel(message.windowTitle())
        title.setObjectName("dialogTitle")
        bar.addWidget(title, 1)
        close_button = QPushButton("×")
        close_button.setObjectName("dialogClose")
        close_button.setFixedSize(30, 26)
        close_button.clicked.connect(message.reject)
        bar.addWidget(close_button)
        def move_message(event):
            if event.button() == Qt.MouseButton.LeftButton and message.windowHandle():
                message.windowHandle().startSystemMove()
            event.accept()
        titlebar.mousePressEvent = move_message
        title.mousePressEvent = move_message
        columns = max((column + column_span for _, _, column, _, column_span in existing), default=1)
        layout.addWidget(titlebar, 0, 0, 1, columns)
        for item, row, column, row_span, column_span in existing:
            layout.addItem(item, row + 1, column, row_span, column_span)
        message.setMinimumWidth(340)

    def show_message(self, icon, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                     default_button=QMessageBox.StandardButton.No):
        message = QMessageBox(parent)
        message.setIcon(icon)
        message.setWindowTitle(title)
        message.setText(text)
        message.setStandardButtons(buttons)
        if default_button & buttons:
            message.setDefaultButton(default_button)
        self.prepare_message_box(message)
        return QMessageBox.StandardButton(message.exec())

    def _sync_persona_watchers(self):
        PERSONAS.mkdir(parents=True, exist_ok=True)
        wanted_dirs = {str(PERSONAS)}
        wanted_files = {str(record["path"]) for record in persona_records()}
        for path in self.persona_watcher.directories():
            if path not in wanted_dirs:
                self.persona_watcher.removePath(path)
        for path in self.persona_watcher.files():
            if path not in wanted_files:
                self.persona_watcher.removePath(path)
        add_dirs = wanted_dirs.difference(self.persona_watcher.directories())
        add_files = wanted_files.difference(self.persona_watcher.files())
        if add_dirs:
            self.persona_watcher.addPaths(sorted(add_dirs))
        if add_files:
            self.persona_watcher.addPaths(sorted(add_files))

    def _schedule_persona_refresh(self, _path=None):
        self.persona_refresh_timer.start()

    def _refresh_persona_files(self):
        self.refresh_personas()
        self.refresh_memory_personas()

    def import_persona(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入人设 JSON", str(Path.home()), "人设 JSON (*.json)"
        )
        if not path:
            return
        value = read_json(Path(path))
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not isinstance(value.get("system_prompt"), str):
            self.show_message(QMessageBox.Icon.Critical, self, "导入失败", "文件不是有效的人设 JSON：需要包含 name 和 system_prompt 字段。")
            return
        value["provider"] = self.persona_provider
        import re
        persona_id = str(value.get("id") or value["name"]).strip()
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", persona_id).strip(".-_").lower() or "persona"
        destination = PERSONAS / f"{filename}.json"
        if destination.exists():
            answer = self.show_message(QMessageBox.Icon.Question,
                self, "覆盖人格档案？", f"“{value['name']}”档案已存在。要用导入文件覆盖吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            PERSONAS.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            self.show_message(QMessageBox.Icon.Critical, self, "导入失败", str(error))
            return
        self.refresh_personas()
        self.refresh_memory_personas()
        self.show_message(QMessageBox.Icon.Information, self, "导入完成", f"已导入人设“{value['name']}”。\n档案：{destination}")

    def show_persona(self, persona_id):
        persona = next((entry for entry in getattr(self, "personas", []) if entry["id"] == persona_id), None)
        if not persona:
            return
        previous = self.persona_rows.get(self.selected_persona_id)
        if previous:
            previous.setObjectName("personaRow")
            previous.style().unpolish(previous)
            previous.style().polish(previous)
        self.selected_persona_id = persona["id"]
        row = self.persona_rows.get(persona_id)
        if row:
            row.setObjectName("personaRowSelected")
            row.style().unpolish(row)
            row.style().polish(row)
        self.persona_edit_button.setEnabled(True)
        self.persona_send_button.setVisible(True)
        self.persona_clear_button.setVisible(True)
        self.persona_send_button.setEnabled(True)
        self._show_persona_record(persona)

    def edit_persona(self):
        persona = next(
            (entry for entry in getattr(self, "personas", []) if entry["id"] == self.selected_persona_id),
            None,
        )
        if not persona:
            return
        original = read_json(persona["path"])
        if not isinstance(original, dict):
            self.show_message(QMessageBox.Icon.Critical, self, "编辑失败", "无法读取这份人格档案。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"编辑人设：{persona['name']}")
        dialog.resize(680, 620)
        form = QFormLayout(dialog)
        form.setContentsMargins(20, 20, 20, 14)
        name_input = QLineEdit(str(original.get("name") or persona["name"]))
        id_input = QLineEdit(str(original.get("id") or persona["id"]))
        id_input.setReadOnly(True)
        description_input = QPlainTextEdit(str(original.get("description") or ""))
        description_input.setMaximumHeight(100)
        prompt_input = QPlainTextEdit(str(original.get("system_prompt") or ""))
        prompt_input.setMinimumHeight(280)
        form.addRow("名称", name_input)
        form.addRow("档案 ID（用于关联会话）", id_input)
        form.addRow("简介", description_input)
        form.addRow("系统提示词", prompt_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        def accept_if_valid():
            if not name_input.text().strip():
                self.show_message(QMessageBox.Icon.Warning, dialog, "无法保存", "人设名称不能为空。")
                return
            dialog.accept()

        buttons.accepted.connect(accept_if_valid)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        self.prepare_dialog(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name = name_input.text().strip()
        updated = dict(original)
        updated["name"] = name
        updated["description"] = description_input.toPlainText().strip()
        updated["system_prompt"] = prompt_input.toPlainText()
        temporary = persona["path"].with_name(persona["path"].name + ".tmp")
        try:
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(persona["path"])
        except OSError as error:
            temporary.unlink(missing_ok=True)
            self.show_message(QMessageBox.Icon.Critical, self, "保存失败", str(error))
            return
        self.expanded_persona_ids.discard(persona["id"])
        self.refresh_personas()
        self.show_message(QMessageBox.Icon.Information, self, "保存完成", f"人设“{name}”已更新。")

    def toggle_persona_detail(self, persona_id):
        if persona_id in self.expanded_persona_ids:
            self.expanded_persona_ids.remove(persona_id)
            expanded = False
        else:
            self.expanded_persona_ids.add(persona_id)
            expanded = True
        prompt = self.persona_prompt_labels.get(persona_id)
        button = self.persona_expand_buttons.get(persona_id)
        if prompt:
            prompt.setVisible(expanded)
        if button:
            button.setText("收起全文" if expanded else "展开全文")
        self.persona_rows_widget.updateGeometry()
        self.personas_page.widget().updateGeometry()

    def _show_persona_record(self, persona):
        self._render_persona_usage(persona)

    def _read_persona_usage(self) -> list[dict[str, str]]:
        value = read_json(PERSONA_USAGE)
        return value.get("bindings", []) if isinstance(value, dict) and isinstance(value.get("bindings"), list) else []

    def _write_persona_usage(self, bindings: list[dict[str, str]]):
        PERSONA_USAGE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PERSONA_USAGE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"bindings": bindings}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(PERSONA_USAGE)

    def _current_persona_binding(self, persona):
        target = self.persona_provider
        for conversation in self.active_persona_conversations or []:
            binding = next((entry for entry in self._read_persona_usage()
                if entry.get("provider") == target
                and entry.get("provider") == conversation.get("provider")
                and entry.get("conversation_id") == conversation.get("conversation_id")
                and entry.get("persona_id") == persona["id"]), None)
            if binding:
                return binding
        return None

    def _render_persona_usage(self, persona):
        shown_providers = []
        if self.active_persona_conversations is None:
            status_text = "知忆导入记录：正在检查当前对话…"
            tooltip = ""
        else:
            current = []
            outdated = []
            digest = hashlib.sha256(persona["system_prompt"].encode("utf-8")).hexdigest()
            for conversation in self.active_persona_conversations:
                binding = next((entry for entry in self._read_persona_usage()
                    if entry.get("provider") == conversation.get("provider")
                    and entry.get("conversation_id") == conversation.get("conversation_id")
                    and entry.get("persona_id") == persona["id"]), None)
                if binding:
                    (current if binding.get("prompt_sha256") == digest else outdated).append(conversation["provider"])
            if current:
                status_text = "知忆导入记录：当前对话已导入"
                shown_providers = sorted(set(current))
            elif outdated:
                status_text = "知忆导入记录：发现旧版人设（请重新导入）"
                shown_providers = sorted(set(outdated))
            else:
                status_text = "知忆导入记录：当前对话未检测到"
            tooltip = "这里只统计通过知忆导入且当前仍打开的对话；不会自动确认其他客户端的人设状态。"
        global_id = global_persona_api.current_persona_id()
        if self.persona_provider == "Codex":
            if global_id == persona["id"]:
                global_text = f"Codex 全局人设：已检测到「{persona['name']}」"
            elif global_id:
                global_text = "Codex 全局人设：已检测到其他人设"
            else:
                global_text = "Codex 全局人设：未检测到"
            status_text = f"{status_text}\n{global_text}"
            tooltip += " 全局人设状态表示 ~/.codex/AGENTS.md 中的设置已检测到；当前打开的对话是否已经加载，需新建对话后才能确定。"
        display = (persona["id"], status_text, tuple(shown_providers), tooltip)
        if getattr(self, "_persona_usage_display", None) != display:
            self._persona_usage_display = display
            self.persona_usage_label.setText(status_text)
            self.persona_usage_label.setToolTip(tooltip)
            while self.persona_usage_layout.count() > 2:
                item = self.persona_usage_layout.takeAt(1)
                if item.widget():
                    item.widget().hide()
                    item.widget().deleteLater()
            for provider in shown_providers:
                icon_path = OFFICIAL_AI_ICONS.get(provider)
                if icon_path and icon_path.is_file():
                    icon = QLabel()
                    icon.setPixmap(QPixmap(str(icon_path)).scaled(
                        18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                    ))
                    icon.setToolTip(f"{provider} 官方桌面应用图标")
                    self.persona_usage_layout.insertWidget(self.persona_usage_layout.count() - 1, icon)
                name = QLabel(provider)
                name.setObjectName("muted")
                self.persona_usage_layout.insertWidget(self.persona_usage_layout.count() - 1, name)
        self.persona_clear_button.setEnabled(
            self._current_persona_binding(persona) is not None
            and self.persona_send_process is None
            and self.persona_clear_process is None
        )
        self._render_global_persona_button(persona)
        self.persona_usage_row.show()

    def _render_global_persona_button(self, persona):
        active_id = global_persona_api.current_persona_id()
        supported = self.persona_provider == "Codex"
        if not supported:
            self.global_persona_button.setText("Codex 全局人设")
            self.global_persona_button.setToolTip("全局指令目前只支持 Codex")
        elif active_id == persona["id"]:
            self.global_persona_button.setText("关闭全局人设")
            self.global_persona_button.setToolTip("从 Codex 全局指令中移除当前人设")
        elif active_id:
            self.global_persona_button.setText("切换全局人设")
            self.global_persona_button.setToolTip("将 Codex 全局默认人设切换为当前所选人设")
        else:
            self.global_persona_button.setText("设为全局人设")
            self.global_persona_button.setToolTip("让后续 Codex 对话持续参考所选人设")
        self.global_persona_button.setEnabled(supported)
        self.global_persona_button.setVisible(True)

    def toggle_global_persona(self):
        persona = next((item for item in getattr(self, "personas", [])
                        if item["id"] == self.selected_persona_id), None)
        if not persona or self.persona_provider != "Codex":
            return
        try:
            active_id = global_persona_api.current_persona_id()
            if active_id == persona["id"]:
                global_persona_api.clear_global_persona()
                self.show_message(QMessageBox.Icon.Information, self, "全局人设已关闭",
                                  "已从 Codex 全局指令中移除该人设。")
            else:
                settings = QSettings()
                if not settings.value("persona/global_consent_granted", False, type=bool):
                    message = QMessageBox(self)
                    message.setIcon(QMessageBox.Icon.Warning)
                    message.setWindowTitle("启用 Codex 全局人设")
                    message.setText(
                        f"同意将「{persona['name']}」设为 Codex 全局默认人设吗？\n\n"
                        "启用后，知忆会把人设写入 ~/.codex/AGENTS.md，供之后的新 Codex 对话持续参考。"
                        "这会影响其他 Codex 项目与对话；已经打开的对话不会自动刷新。"
                        "更高优先级的系统与开发者指令仍然优先。"
                    )
                    agree = message.addButton("同意并启用", QMessageBox.ButtonRole.AcceptRole)
                    message.addButton("暂不启用", QMessageBox.ButtonRole.RejectRole)
                    self.prepare_message_box(message)
                    message.exec()
                    if message.clickedButton() is not agree:
                        return
                global_persona_api.set_global_persona(
                    persona["id"], persona["name"], persona["system_prompt"]
                )
                settings.setValue("persona/global_consent_granted", True)
                self.show_message(QMessageBox.Icon.Information, self, "全局人设已启用",
                                  f"「{persona['name']}」已写入 Codex 全局指令。新建的 Codex 对话会读取它；"
                                  "当前已打开的对话需要重新开始才会加载。")
        except (OSError, ValueError) as error:
            self.show_message(QMessageBox.Icon.Warning, self, "全局人设设置失败", str(error))
        current = next((item for item in getattr(self, "personas", [])
                        if item["id"] == self.selected_persona_id), None)
        if current:
            self._render_global_persona_button(current)

    def _poll_persona_usage(self):
        if self.persona_status_process and self.persona_status_process.state() != QProcess.ProcessState.NotRunning:
            return
        process = QProcess(self)
        self.persona_status_process = process
        process.finished.connect(self._persona_status_finished)
        process.errorOccurred.connect(self._persona_status_error)
        process.start("node", [str(PERSONA_SENDER), "status"])

    def _persona_status_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        process = self.persona_status_process
        if not process or process.state() != QProcess.ProcessState.NotRunning:
            return
        self.active_persona_conversations = None
        persona = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if persona:
            self._render_persona_usage(persona)
        process.deleteLater()
        self.persona_status_process = None

    def _persona_status_finished(self, _code, _status):
        process = self.persona_status_process
        if not process:
            return
        try:
            result = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8").strip())
            self.active_persona_conversations = result["conversations"] if result.get("ok") else None
        except (ValueError, KeyError, UnicodeError):
            self.active_persona_conversations = None
        persona = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if persona:
            self._render_persona_usage(persona)
        process.deleteLater()
        self.persona_status_process = None

    def send_persona_to_ai(self):
        if self.persona_provider != "Codex":
            self.show_message(QMessageBox.Icon.Warning, self, "暂不可用", "所选 AI 暂未接入人设导入。")
            return
        persona = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if not persona or self.persona_send_process or self.persona_clear_process:
            return
        self.persona_send_button.setEnabled(False)
        self.persona_clear_button.setEnabled(False)
        self.persona_send_button.setText("正在导入…")
        process = QProcess(self)
        self.persona_send_process = process
        process.finished.connect(lambda code, status: self._persona_send_finished(persona, code, status))
        process.errorOccurred.connect(self._persona_send_error)
        process.start("node", [str(PERSONA_SENDER), "send", str(persona["path"])])

    def _persona_send_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        process = self.persona_send_process
        if not process or process.state() != QProcess.ProcessState.NotRunning:
            return
        self.persona_send_process = None
        self.persona_send_button.setText("导入到当前 AI 对话")
        self.persona_send_button.setEnabled(self.selected_persona_id is not None)
        current = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if current:
            self._render_persona_usage(current)
        process.deleteLater()
        self.show_message(QMessageBox.Icon.Warning, self, "导入未完成", "无法启动 Node.js 连接器，请检查 Node.js 是否已安装。")

    def _persona_send_finished(self, persona, _code, _status):
        process = self.persona_send_process
        if not process:
            return
        self.persona_send_process = None
        self.persona_send_button.setText("导入到当前 AI 对话")
        self.persona_send_button.setEnabled(self.selected_persona_id is not None)
        current = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if current:
            self._render_persona_usage(current)
        try:
            result = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8").strip())
        except (ValueError, UnicodeError):
            result = {"ok": False, "error": "无法读取连接器结果"}
        process.deleteLater()
        if not result.get("ok"):
            self.show_message(QMessageBox.Icon.Warning, self, "导入未完成", str(result.get("error") or "请检查 Codex 是否已打开"))
            return
        bindings = [entry for entry in self._read_persona_usage()
            if not (entry.get("provider") == result["provider"]
                and entry.get("conversation_id") == result["conversation_id"])]
        bindings.append({
            "provider": result["provider"],
            "conversation_id": result["conversation_id"],
            "title": result.get("title", ""),
            "persona_id": persona["id"],
            "prompt_sha256": result["prompt_sha256"],
        })
        try:
            self._write_persona_usage(bindings)
        except OSError as error:
            self.show_message(QMessageBox.Icon.Warning, self, "人设已发送", f"人设已发送到当前对话，但使用状态未能保存：{error}")
            return
        self._poll_persona_usage()
        self.show_message(QMessageBox.Icon.Information, self, "导入完成", f"已将“{persona['name']}”发送到当前 Codex 对话。")

    def clear_persona_from_ai(self):
        if self.persona_provider != "Codex":
            self.show_message(QMessageBox.Icon.Warning, self, "暂不可用", "所选 AI 暂未接入人设清除。")
            return
        persona = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if not persona or self.persona_send_process or self.persona_clear_process:
            return
        binding = self._current_persona_binding(persona)
        if not binding:
            self._render_persona_usage(persona)
            return
        self.persona_send_button.setEnabled(False)
        self.persona_clear_button.setEnabled(False)
        self.persona_clear_button.setText("正在清除…")
        process = QProcess(self)
        self.persona_clear_process = process
        process.finished.connect(lambda code, status: self._persona_clear_finished(persona, binding, code, status))
        process.errorOccurred.connect(self._persona_clear_error)
        process.start("node", [str(PERSONA_SENDER), "clear", str(persona["path"]), binding["conversation_id"]])

    def _persona_clear_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        process = self.persona_clear_process
        if not process or process.state() != QProcess.ProcessState.NotRunning:
            return
        self.persona_clear_process = None
        self.persona_clear_button.setText("清除当前 AI 人设")
        self.persona_send_button.setEnabled(self.selected_persona_id is not None)
        current = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if current:
            self._render_persona_usage(current)
        process.deleteLater()
        self.show_message(QMessageBox.Icon.Warning, self, "清除未完成", "无法启动 Node.js 连接器，请检查 Node.js 是否已安装。")

    def _persona_clear_finished(self, persona, binding, _code, _status):
        process = self.persona_clear_process
        if not process:
            return
        self.persona_clear_process = None
        self.persona_clear_button.setText("清除当前 AI 人设")
        self.persona_send_button.setEnabled(self.selected_persona_id is not None)
        try:
            result = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8").strip())
        except (ValueError, UnicodeError):
            result = {"ok": False, "error": "无法读取连接器结果"}
        process.deleteLater()
        if not result.get("ok") or result.get("action") != "clear" or result.get("conversation_id") != binding["conversation_id"]:
            self.show_message(QMessageBox.Icon.Warning, self, "清除未完成", str(result.get("error") or "未能确认当前对话已收到清除消息"))
        else:
            bindings = [entry for entry in self._read_persona_usage()
                if not (entry.get("provider") == binding.get("provider")
                    and entry.get("conversation_id") == binding.get("conversation_id")
                    and entry.get("persona_id") == persona["id"])]
            bindings.append({"provider": binding.get("provider"),
                             "conversation_id": binding.get("conversation_id"), "disabled": True})
            try:
                self._write_persona_usage(bindings)
            except OSError as error:
                self.show_message(QMessageBox.Icon.Warning, self, "人设已停止", f"当前对话已收到清除消息，但本地状态未能保存：{error}")
            else:
                self.show_message(QMessageBox.Icon.Information, self, "清除完成", f"已通知当前 Codex 对话停止使用“{persona['name']}”人设。")
        current = next((item for item in getattr(self, "personas", [])
            if item["id"] == self.selected_persona_id), None)
        if current:
            self._render_persona_usage(current)
        self._poll_persona_usage()

    def refresh(self):
        self.records = session_records()
        sources = {library_group(r["provider"]) for r in self.records}
        self.stat_values["sessions"].setText(str(len(self.records)))
        self.stat_values["sources"].setText(str(len(sources)))
        self.stat_values["days"].setText(str(len({r['date'] for r in self.records if r['date']})))
        self.recent_list.clear()
        for record in self.records:
            if self.recent_list.count() < 6:
                self.recent_list.addItem(self._record_item(record))
        self.render_sessions()
        self.summary_list.clear()
        dates = summary_api.列出概要(会话库=ARCHIVE)
        for day in reversed(dates):
            doc = summary_api.读取概要(day, 会话库=ARCHIVE) or {}
            self.summary_list.addItem(QListWidgetItem(f"▤    {day}     ·     {doc.get('session_count', 0)} 段会话"))
        self.refresh_personas()
        self.refresh_memory_personas()
        if not self.records:
            self.recent_list.addItem(QListWidgetItem("还没有会话记录。点击右上角「导入会话」开始添加。"))

    def _record_item(self, record):
        title = record["title"] or "未命名会话"
        item = QListWidgetItem(f"◷    {title}\n       {record['provider']}   ·   {record['date']}   ·   {record['persona']}")
        item.setData(Qt.ItemDataRole.UserRole, {"type": "session", "record": record})
        return item

    def navigate(self, page):
        self.current_page = page
        if page == "人格档案":
            self.refresh_personas()
        elif page == "记忆更新":
            self.refresh_memory_personas()
        index = {"总览": 0, "会话库": 1, "每日概要": 2, "人格档案": 3, "记忆更新": 4}[page]
        self.pages.setCurrentIndex(index)
        self.page_title.setText(page)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == page)

    def render_sessions(self):
        if not hasattr(self, "session_list"):
            return
        self.session_list.clear()
        query = self.search.text().strip().casefold()
        self.sessions_back.setVisible(bool(query) or self.selected_provider is not None)
        if query:
            self.sessions_hint.setText("搜索结果")
            visible_records = [
                record for record in self.records
                if query in (record["title"] + record["provider"] + library_group(record["provider"]) + record["date"] + session_text(record)).casefold()
            ]
            for record in visible_records:
                self.session_list.addItem(self._record_item(record))
            if not visible_records:
                self.session_list.addItem(QListWidgetItem("没有找到匹配的会话。"))
        elif self.selected_provider is None:
            self.sessions_hint.setText("OpenAI 共用一个会话库，具体会话会标明来自 ChatGPT 或 Codex。")
            grouped: dict[str, list[dict[str, Any]]] = {}
            for record in self.records:
                grouped.setdefault(library_group(record["provider"]), []).append(record)
            if ARCHIVE.is_dir():
                for source_dir in ARCHIVE.iterdir():
                    if source_dir.is_dir() and source_dir.name != "summaries":
                        if source_dir.name == "OpenAI":
                            grouped.setdefault("OpenAI", [])
                            for client_dir in source_dir.iterdir():
                                if client_dir.is_dir():
                                    grouped.setdefault("OpenAI", [])
                        else:
                            grouped.setdefault(library_group(source_dir.name), [])
            for provider, records in sorted(grouped.items()):
                latest = max((record["date"] for record in records), default="")
                if provider == "OpenAI":
                    by_client = {name: sum(record["provider"] == name for record in records) for name in ("ChatGPT", "Codex")}
                    detail = f"ChatGPT {by_client['ChatGPT']} · Codex {by_client['Codex']}"
                    if latest:
                        detail += f"   ·   最近记录 {latest}"
                else:
                    detail = f"{len(records)} 个会话   ·   最近记录 {latest}" if latest else f"{len(records)} 个会话   ·   暂无记录"
                is_codex = provider.casefold().startswith("codex")
                is_deepseek = provider.casefold().startswith("deepseek")
                is_openai = provider == "OpenAI"
                icon_label = "" if is_codex or is_deepseek or is_openai else "◈    "
                item = QListWidgetItem(f"{icon_label}{provider}\n       {detail}")
                if is_codex or is_openai:
                    item.setIcon(QIcon(str(CODEX_ICON)))
                elif is_deepseek:
                    item.setIcon(QIcon(str(DEEPSEEK_ICON)))
                item.setData(Qt.ItemDataRole.UserRole, {"type": "source", "provider": provider})
                self.session_list.addItem(item)
            if not grouped:
                self.session_list.addItem(QListWidgetItem("会话库为空。支持导入 ChatGPT 官方导出 ZIP 或 conversations.json。"))
        else:
            visible_records = [record for record in self.records if library_group(record["provider"]) == self.selected_provider]
            if self.selected_provider == "OpenAI":
                clients = sorted({record["provider"] for record in visible_records})
                source_detail = " / ".join(clients) if clients else "ChatGPT / Codex"
                self.sessions_hint.setText(f"OpenAI  ·  {source_detail}  ·  {len(visible_records)} 段会话")
            else:
                self.sessions_hint.setText(f"{self.selected_provider}  ·  {len(visible_records)} 段会话")
            for record in visible_records:
                self.session_list.addItem(self._record_item(record))
            if not visible_records:
                self.session_list.addItem(QListWidgetItem(f"还没有 {self.selected_provider} 会话，导入或接入后会显示在这里。"))

    def show_sources(self):
        self.search.clear()
        self.selected_provider = None
        self.render_sessions()

    def on_search_changed(self, text):
        if text.strip() and self.current_page != "会话库":
            self.navigate("会话库")
        if text.strip():
            self.selected_provider = None
        self.render_sessions()

    def activate_session_item(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("type") == "source":
            self.selected_provider = data["provider"]
            self.render_sessions()
            return
        self.open_session_item(item)

    def open_session_item(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        record = data.get("record")
        if record is None:
            title = item.text().split("\n")[0].strip("◷ ")
            record = next((r for r in self.records if r["title"] == title), None)
        if not record:
            return
        body = session_text(record).strip()[:12000]
        message = QMessageBox(self)
        message.setWindowTitle(record["title"])
        message.setText(f"{record['provider']}  ·  {record['date']}  ·  {record['id']}")
        message.setInformativeText(body[:1800] if body else "原始会话文件已归档，可从下方打开所在目录。")
        open_action = message.addButton("打开归档目录", QMessageBox.ButtonRole.ActionRole)
        message.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        self.prepare_message_box(message)
        message.exec()
        if message.clickedButton() is open_action:
            import subprocess
            subprocess.Popen(["xdg-open", str(record["folder"])])

    def import_archive(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 ChatGPT 会话导出", str(Path.home()), "ChatGPT 导出 (*.zip *.json)")
        if not path:
            return
        try:
            target = self.memory_persona_selector.currentData() or {}
            imported = archive_export(path, ARCHIVE, persona_id=target.get("id"))
        except (OSError, ValueError) as error:
            self.show_message(QMessageBox.Icon.Critical, self, "导入失败", str(error))
            return
        self.refresh()
        self.show_message(QMessageBox.Icon.Information, self, "导入完成", f"已归档 {len(imported)} 个会话。")

    def import_local_sessions(self):
        connectors = [item for item in detect_local_connectors() if item.can_discover]
        if not connectors:
            self.show_message(QMessageBox.Icon.Information, self, "未发现本机会话", "没有找到可读取的 Codex 或 Claude Code 会话目录。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("从本机导入会话")
        dialog.resize(690, 520)
        layout = QVBoxLayout(dialog)
        hint = QLabel("选择需要归档的会话；原始转录会保存在本地会话库。")
        layout.addWidget(hint)
        source = QComboBox(dialog)
        source.addItem("全部来源", None)
        for connector in connectors:
            source.addItem(connector.label, connector.connector_id)
        layout.addWidget(source)
        sessions = QListWidget(dialog)
        sessions.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(sessions, 1)
        status = QLabel(dialog)
        layout.addWidget(status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("归档所选会话")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        archived = {(record["provider"], record["id"]): record for record in session_records()}

        def populate():
            sessions.clear()
            try:
                previews = discover_local_sessions(source.currentData())
            except (OSError, ValueError) as error:
                status.setText(f"读取会话失败：{error}")
                buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
                return
            for preview in previews:
                previous = archived.get((preview.provider, preview.session_id))
                suffix = ""
                if previous:
                    changed = (
                        previous["source_size"] is not None
                        and previous["source_mtime_ns"] is not None
                        and (previous["source_size"], previous["source_mtime_ns"])
                        != (preview.size_bytes, preview.mtime_ns)
                    )
                    suffix = "  ·  来源有更新" if changed else "  ·  已归档"
                item = QListWidgetItem(
                    f"{preview.title}\n{preview.provider}  ·  {preview.updated_at:%Y-%m-%d %H:%M}{suffix}"
                )
                item.setToolTip(str(preview.source_path))
                item.setData(Qt.ItemDataRole.UserRole, preview)
                sessions.addItem(item)
            status.setText(f"找到 {len(previews)} 个本机会话；可按 Ctrl 或 Shift 多选。")
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(previews))

        source.currentIndexChanged.connect(populate)
        populate()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = [item.data(Qt.ItemDataRole.UserRole) for item in sessions.selectedItems()]
        if not selected:
            return
        imported = []
        errors = []
        for preview in selected:
            try:
                target = self.memory_persona_selector.currentData() or {}
                imported.append(archive_local_session(preview, ARCHIVE, persona_id=target.get("id")))
            except (OSError, ValueError) as error:
                errors.append(f"{preview.title}：{error}")
        self.refresh()
        detail = f"已归档 {len(imported)} 个会话。"
        if errors:
            detail += "\n\n未完成：\n" + "\n".join(errors[:5])
        self.show_message(
            QMessageBox.Icon.Warning if errors else QMessageBox.Icon.Information,
            self, "本机会话导入结果", detail,
        )

    def generate_summaries(self):
        try:
            files = summary_api.生成概要(会话库=ARCHIVE)
            self.refresh()
            self.show_message(QMessageBox.Icon.Information, self, "概要已更新", f"已生成 {len(files)} 份每日概要。")
        except (OSError, ValueError) as error:
            self.show_message(QMessageBox.Icon.Critical, self, "生成失败", str(error))

    def show_summary(self, item):
        day = item.text().split("·")[0].replace("▤", "").strip()
        doc = summary_api.读取概要(day, 会话库=ARCHIVE) or {}
        lines = []
        for session in doc.get("sessions", []):
            lines.append(f"{session.get('title', '未命名')}  ·  {session.get('provider', '')}")
            lines.extend(f"你：{text}" for text in session.get("user_highlights", []))
            lines.extend(f"AI：{text}" for text in session.get("assistant_highlights", []))
            lines.append("")
        summary_text = "\n".join(lines)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{day} 每日概要")
        dialog.setFixedSize(760, 540)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)

        body = QPlainTextEdit(dialog)
        body.setReadOnly(True)
        body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(body, 1)

        controls = QHBoxLayout()
        previous = QPushButton("← 上一页")
        next_page = QPushButton("下一页 →")
        previous.setEnabled(False)
        next_page.setEnabled(False)
        page_label = QLabel("正在排版…")
        page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        controls.addWidget(previous)
        controls.addStretch()
        controls.addWidget(page_label)
        controls.addStretch()
        controls.addWidget(next_page)
        controls.addSpacing(12)
        controls.addWidget(close)
        layout.addLayout(controls)

        pages = []
        current_page = 0

        def show_page():
            body.setPlainText(pages[current_page])
            body.verticalScrollBar().setValue(0)
            page_label.setText(f"第 {current_page + 1} / {len(pages)} 页  ·  每页最多 {SUMMARY_PAGE_CHARS} 字")
            previous.setEnabled(current_page > 0)
            next_page.setEnabled(current_page < len(pages) - 1)

        def turn_page(step):
            nonlocal current_page
            current_page = max(0, min(current_page + step, len(pages) - 1))
            show_page()

        previous.clicked.connect(lambda: turn_page(-1))
        next_page.clicked.connect(lambda: turn_page(1))
        self.prepare_dialog(dialog)
        maximize_button = dialog.findChild(QPushButton, "dialogMaximize")
        if maximize_button:
            maximize_button.hide()

        def prepare_pages():
            body.setUpdatesEnabled(False)

            def fits(candidate):
                body.setPlainText(candidate)
                QApplication.processEvents()
                return body.verticalScrollBar().maximum() == 0

            pages.extend(paginate_summary_text(summary_text, fits=fits))
            body.setUpdatesEnabled(True)
            show_page()

        QTimer.singleShot(0, prepare_pages)
        dialog.exec()

    def toggle_theme(self):
        self.dark = not self.dark
        QSettings().setValue("appearance/dark_mode", self.dark)
        QApplication.instance().setProperty("dark_mode", self.dark)
        self.apply_theme()

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.maximize_button.setText("❐" if self.isMaximized() else "□")

    def _start_window_move(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle:
                handle.startSystemMove()
        event.accept()

    def _title_double_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()
        event.accept()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "maximize_button"):
            self.maximize_button.setText("❐" if self.isMaximized() else "□")
            margin = 0 if self.isMaximized() else 6
            self.outer_layout.setContentsMargins(margin, margin, margin, margin)

    def eventFilter(self, watched, event):
        if watched is self.centralWidget() and event.type() in (
            QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
        ) and not self.isMaximized():
            point = event.position().toPoint()
            rect = watched.rect()
            edges = Qt.Edge(0)
            if point.x() <= 8:
                edges |= Qt.Edge.LeftEdge
            elif point.x() >= rect.width() - 8:
                edges |= Qt.Edge.RightEdge
            if point.y() <= 8:
                edges |= Qt.Edge.TopEdge
            elif point.y() >= rect.height() - 8:
                edges |= Qt.Edge.BottomEdge
            if event.type() == QEvent.Type.MouseButtonPress and edges and event.button() == Qt.MouseButton.LeftButton:
                handle = self.windowHandle()
                if handle:
                    handle.startSystemResize(edges)
                    return True
            cursor = {
                Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
                Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
                Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
                Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
                Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
                Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
                Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
                Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
            }.get(edges)
            watched.setCursor(cursor or Qt.CursorShape.ArrowCursor)
        return super().eventFilter(watched, event)

    def apply_theme(self):
        self.theme_button.setText("☀" if self.dark else "☾")
        stylesheet = DARK_QSS if self.dark else LIGHT_QSS
        QApplication.instance().setStyleSheet(stylesheet)


LIGHT_QSS = """
QWidget { color:#263044; font-family:'Noto Sans CJK SC','Inter',sans-serif; font-size:14px; }
QDialog#themedDialog { background:#fff; color:#263044; border:1px solid #dfe3ea; border-radius:12px; }
QMessageBox#themedDialog { background:#fff; color:#263044; border:1px solid #dfe3ea; border-radius:12px; }
QFrame#dialogTitleBar { background:#f1f3f7; border:0; border-bottom:1px solid #dfe3ea; border-top-left-radius:11px; border-top-right-radius:11px; }
QLabel#dialogTitle { color:#354158; font-size:13px; font-weight:600; }
#dialogMinimize, #dialogMaximize, #dialogClose { background:transparent; color:#768092; border:0; border-radius:5px; padding:0; font-size:17px; }
#dialogMinimize:hover, #dialogMaximize:hover { background:#e2e6ed; }
#dialogClose:hover { background:#e55353; color:white; }
QPushButton { background:#fff; color:#354158; border:1px solid #dfe3ea; border-radius:8px; padding:7px 12px; }
QPushButton:hover { background:#f4f7fc; border-color:#b8c9e8; }
QPushButton:pressed { background:#eaf0fa; }
QPushButton:disabled { background:#f4f5f7; color:#a3aab5; border-color:#e6e9ee; }
QLineEdit, QPlainTextEdit { background:#fff; color:#354158; border:1px solid #dfe3ea; border-radius:8px; padding:8px; selection-background-color:#d7e3ff; }
QLineEdit:focus, QPlainTextEdit:focus { border-color:#93b2f2; }
#root { background:#f7f8fb; border:1px solid #cfd5df; }
#memoryPage QPushButton, QDialogButtonBox QPushButton { border-radius:9px; }
#personaPageScroll { background:transparent; border:0; }
#personaPageContent { background:#f7f8fb; }
#personaRow { background:#fff; border:1px solid #e9edf3; border-radius:10px; }
#personaRowSelected { background:#fff; border:2px solid #5f8fe8; border-radius:10px; }
#titleBar { background:#f1f3f7; border-bottom:1px solid #dfe3ea; }
#windowTitle { color:#6e7786; font-size:12px; }
#windowMinimize, #windowMaximize, #windowClose { background:transparent; color:#768092; border:0; border-radius:5px; font-size:17px; }
#windowMinimize:hover, #windowMaximize:hover { background:#e2e6ed; }
#windowClose:hover { background:#e55353; color:white; }
#sidebar { background:#fff; border-right:1px solid #e9edf3; }
#logo { background:transparent; border:0; }
#brand { font-family:'Noto Serif CJK SC'; font-size:20px; font-weight:900; letter-spacing:2px; color:#202b3c; }
#caption { font-size:10px; color:#9ba5b5; letter-spacing:0px; }
#sectionLabel { color:#a0aaba; font-size:11px; padding-left:9px; padding-bottom:5px; }
#navButton { text-align:left; color:#778195; border:1px solid transparent; border-radius:9px; padding:11px; background:transparent; }
#navButton:hover { background:#f4f6fa; color:#39475f; }
#navButton:checked { background:#edf3ff; color:#4778e6; font-weight:600; }
#statusCard { background:#f7f8fb; border:1px solid #edf0f5; border-radius:11px; }
#greenDot { color:#4abb83; font-size:10px; }
#mutedSmall { color:#9aa4b4; font-size:10px; }
#pageTitle { font-size:24px; font-weight:700; color:#202a3b; }
#intro { color:#8993a4; font-size:14px; padding-bottom:4px; }
#searchBox, QComboBox { background:#fff; border:1px solid #e7ebf1; border-radius:9px; padding:10px 12px; color:#354158; }
#searchBox:focus, QComboBox:focus { border-color:#93b2f2; }
QPushButton#themedSelector { background:#fff; color:#354158; border:1px solid #e7ebf1; border-radius:9px; padding:7px 31px 7px 10px; text-align:left; }
QPushButton#themedSelector:hover { background:#f4f7fc; border-color:#b8c9e8; }
#themedComboPopup { background:#fff; border:1px solid #dfe3ea; border-radius:12px; }
#themedComboOption, #themedComboOptionSelected { text-align:left; border:0; border-radius:8px; padding:5px 8px; }
#themedComboOption { background:transparent; }
#themedComboOption:hover { background:#edf3ff; }
#themedComboOptionSelected { background:#d7e3ff; color:#315db5; }
#personaTargetSelector { padding:6px 10px; min-height:32px; border-radius:9px; text-align:left; }
#personaSelectorArrow { color:#8290a5; font-size:17px; }
#personaTargetPopup { background:#fff; border:1px solid #e7ebf1; border-radius:12px; }
#personaProviderOption, #personaProviderOptionSelected { text-align:left; border:0; border-radius:7px; padding:5px 8px; }
#personaProviderOption { background:transparent; }
#personaProviderOption:hover { background:#edf3ff; }
#personaProviderOptionSelected { background:#d7e3ff; color:#315db5; }
#primaryButton { background:#4d7fe8; color:white; font-weight:600; border:1px solid #4d7fe8; border-radius:9px; padding:11px 16px; }
#primaryButton:hover { background:#3f70d5; }
#iconButton { background:#fff; border:1px solid #e7ebf1; border-radius:10px; color:#64718a; font-size:18px; }
#iconButton:hover { background:#f0f4fc; }
#statCard, #emptyCard { background:#fff; border:1px solid #e9edf3; border-radius:13px; }
#muted { color:#8994a5; }
#statIcon { color:#7093da; font-size:17px; }
#statValue { font-size:28px; font-weight:700; color:#26354d; padding-top:8px; }
#sectionTitle { font-size:16px; font-weight:650; color:#303c51; }
#linkButton { background:transparent; border:1px solid transparent; color:#527fe0; border-radius:8px; padding:6px 9px; }
#linkButton:hover { background:#f3f6fc; border-color:#d6e1f6; }
#personaManageButton { background:transparent; color:#527fe0; border:1px solid #b8c9e8; border-radius:9px; padding:3px 8px; min-height:22px; max-height:26px; }
#personaManageButton:hover { background:#f3f6fc; border-color:#7fa1df; }
#sessionList { background:#fff; border:1px solid #e9edf3; border-radius:13px; padding:8px; outline:0; }
#sessionList::item { color:#334056; padding:14px 12px; border-bottom:1px solid #f0f2f6; border-radius:8px; }
#sessionList::item:hover { background:#f6f8fc; }
#sessionList::item:selected { background:#edf3ff; color:#315db5; }
#emptyGlyph { color:#a7bce7; font-size:36px; padding:14px; }
QScrollBar:vertical { width:8px; background:transparent; margin:0; }
QScrollBar::handle:vertical { background:#d9dee8; border-radius:4px; min-height:30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QMessageBox { background:#fff; }
"""

DARK_QSS = """
QWidget { color:#e0e6f1; font-family:'Noto Sans CJK SC','Inter',sans-serif; font-size:14px; }
QDialog#themedDialog { background:#202632; color:#e0e6f1; border:1px solid #353e4d; border-radius:12px; }
QMessageBox#themedDialog { background:#202632; color:#e0e6f1; border:1px solid #353e4d; border-radius:12px; }
QFrame#dialogTitleBar { background:#202631; border:0; border-bottom:1px solid #303947; border-top-left-radius:11px; border-top-right-radius:11px; }
QLabel#dialogTitle { color:#e0e6f1; font-size:13px; font-weight:600; }
#dialogMinimize, #dialogMaximize, #dialogClose { background:transparent; color:#aab5c7; border:0; border-radius:5px; padding:0; font-size:17px; }
#dialogMinimize:hover, #dialogMaximize:hover { background:#303a49; }
#dialogClose:hover { background:#c94c55; color:white; }
QPushButton { background:#202632; color:#e0e6f1; border:1px solid #353e4d; border-radius:8px; padding:7px 12px; }
QPushButton:hover { background:#293241; border-color:#566b91; }
QPushButton:pressed { background:#303d53; }
QPushButton:disabled { background:#202632; color:#758095; border-color:#303947; }
QLineEdit, QPlainTextEdit { background:#171c25; color:#e0e6f1; border:1px solid #353e4d; border-radius:8px; padding:8px; selection-background-color:#3b5277; }
QLineEdit:focus, QPlainTextEdit:focus { border-color:#718fcf; }
#root { background:#171b24; border:1px solid #353e4d; }
#memoryPage QPushButton, QDialogButtonBox QPushButton { border-radius:9px; }
#personaPageScroll { background:transparent; border:0; }
#personaPageContent { background:#171b24; }
#personaRow { background:#202632; border:1px solid #303949; border-radius:10px; }
#personaRowSelected { background:#202632; border:2px solid #7ea4ff; border-radius:10px; }
#titleBar { background:#202631; border-bottom:1px solid #303947; }
#windowTitle { color:#a4afc1; font-size:12px; }
#windowMinimize, #windowMaximize, #windowClose { background:transparent; color:#aab5c7; border:0; border-radius:5px; font-size:17px; }
#windowMinimize:hover, #windowMaximize:hover { background:#303a49; }
#windowClose:hover { background:#c94c55; color:white; }
#sidebar { background:#1c212c; border-right:1px solid #2c3340; }
#logo { background:transparent; border:0; }
#brand { font-family:'Noto Serif CJK SC'; font-size:20px; font-weight:900; letter-spacing:2px; color:#f0f3fa; }
#caption { font-size:10px; color:#818b9d; letter-spacing:0px; }
#sectionLabel { color:#8490a3; font-size:11px; padding-left:9px; padding-bottom:5px; }
#navButton { text-align:left; color:#a2adbf; border:1px solid transparent; border-radius:9px; padding:11px; background:transparent; }
#navButton:hover { background:#252d3a; color:#e1e8f4; }
#navButton:checked { background:#263652; color:#9ab7ff; font-weight:600; }
#statusCard { background:#222936; border:1px solid #303949; border-radius:11px; }
#greenDot { color:#58c38c; font-size:10px; }
#mutedSmall { color:#8c97a8; font-size:10px; }
#pageTitle { font-size:24px; font-weight:700; color:#f0f3fa; }
#intro { color:#9aa5b7; font-size:14px; padding-bottom:4px; }
#searchBox, QComboBox { background:#202632; border:1px solid #333c4c; border-radius:9px; padding:10px 12px; color:#e0e6f1; }
#searchBox:focus, QComboBox:focus { border-color:#718fcf; }
QPushButton#themedSelector { background:#202632; color:#e0e6f1; border:1px solid #333c4c; border-radius:9px; padding:7px 31px 7px 10px; text-align:left; }
QPushButton#themedSelector:hover { background:#293241; border-color:#566b91; }
#themedComboPopup { background:#202632; border:1px solid #353e4d; border-radius:12px; }
#themedComboOption, #themedComboOptionSelected { text-align:left; border:0; border-radius:8px; padding:5px 8px; }
#themedComboOption { background:transparent; }
#themedComboOption:hover { background:#29364b; }
#themedComboOptionSelected { background:#344d73; color:#fff; }
#personaTargetSelector { padding:6px 10px; min-height:32px; border-radius:9px; text-align:left; }
#personaSelectorArrow { color:#aab5c7; font-size:17px; }
#personaTargetPopup { background:#202632; border:1px solid #333c4c; border-radius:12px; }
#personaProviderOption, #personaProviderOptionSelected { text-align:left; border:0; border-radius:7px; padding:5px 8px; }
#personaProviderOption { background:transparent; }
#personaProviderOption:hover { background:#29364b; }
#personaProviderOptionSelected { background:#344d73; color:#fff; }
#primaryButton { background:#527fe0; color:white; font-weight:600; border:1px solid #527fe0; border-radius:9px; padding:11px 16px; }
#primaryButton:hover { background:#648eea; }
#iconButton { background:#202632; border:1px solid #333c4c; border-radius:10px; color:#c5d0e1; font-size:18px; }
#iconButton:hover { background:#2a3342; }
#statCard, #emptyCard { background:#202632; border:1px solid #303949; border-radius:13px; }
#muted { color:#9aa5b7; }
#statIcon { color:#8ba9ec; font-size:17px; }
#statValue { font-size:28px; font-weight:700; color:#e7ecf6; padding-top:8px; }
#sectionTitle { font-size:16px; font-weight:650; color:#e1e7f1; }
#linkButton { background:transparent; border:1px solid transparent; color:#91adf0; border-radius:8px; padding:6px 9px; }
#linkButton:hover { background:#252e3e; border-color:#3b4c6a; }
#personaManageButton { background:transparent; color:#a9c1ff; border:1px solid #536b96; border-radius:9px; padding:3px 8px; min-height:22px; max-height:26px; }
#personaManageButton:hover { background:#29364b; border-color:#7e9ddd; }
#sessionList { background:#202632; border:1px solid #303949; border-radius:13px; padding:8px; outline:0; }
#sessionList::item { color:#dbe2ee; padding:14px 12px; border-bottom:1px solid #2b3340; border-radius:8px; }
#sessionList::item:hover { background:#293241; }
#sessionList::item:selected { background:#293b5e; color:#c5d5ff; }
#emptyGlyph { color:#879dcc; font-size:36px; padding:14px; }
QScrollBar:vertical { width:8px; background:transparent; margin:0; }
QScrollBar::handle:vertical { background:#404b5d; border-radius:4px; min-height:30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QMessageBox { background:#202632; }
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("知忆")
    app.setOrganizationName("AI Memory Manager")
    app.setProperty("dark_mode", QSettings().value("appearance/dark_mode", False, type=bool))
    app.setFont(QFont("Noto Sans CJK SC", 10))
    window = MemoryApp()
    if "--memory" in sys.argv:
        window.navigate("记忆更新")
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
