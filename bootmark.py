#!/usr/bin/env python3
"""BootMark — minimal BIOS backup / validation / flash assistant for Windows."""

from __future__ import annotations

import ctypes
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

LogLineKind = Literal["normal", "progress", "command"]

from PyQt6.QtCore import QProcess, QThread, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

BOOTMARK_VERSION = "1.0.3"
APP_USER_MODEL_ID = "com.bootmark.app"
ICON_FILE_NAME = "logo.ico"
DEFAULT_WINDOW_WIDTH = 780
DEFAULT_WINDOW_HEIGHT = 720
MIN_WINDOW_WIDTH = 520
MIN_WINDOW_HEIGHT = 640
FPT_SUCCESS_MARKER = "FPT Operation Successful"
REWRITE_IDENTICAL_MARKER = "RESULT: The data is identical."

BIOS_BACKUP_NAME = "bios_region_original.bin"
BIOS_EDIT_COPY_NAME = "bios_region_edit_me.bin"
FULL_SPI_BACKUP_NAME = "full_spi_original.bin"
MODIFIED_DEFAULT_NAME = "logo_modified.bin"

LOG_COLORS = {
    "normal": "#d4d4d4",
    "meta": "#9e9e9e",
    "command": "#4fc3f7",
    "cwd": "#80cbc4",
    "success": "#81c784",
    "warning": "#ffb74d",
    "error": "#ef5350",
    "exit_ok": "#a5d6a7",
    "exit_fail": "#ef9a9a",
    "section": "#616161",
}

APP_STYLESHEET = """
QMainWindow, QWidget#centralRoot {
    background-color: #252526;
    color: #e0e0e0;
}
QLabel#statusLabel {
    color: #9cdcfe;
    padding: 4px 8px;
    background-color: #2d2d30;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    font-size: 12px;
}
QLabel#footerLabel {
    color: #808080;
    font-size: 11px;
    padding: 2px 0 0 0;
}
QLabel#footerLabel a {
    color: #4fc3f7;
    text-decoration: none;
}
QLabel#footerLabel a:hover {
    color: #9cdcfe;
    text-decoration: underline;
}
QGroupBox {
    font-weight: bold;
    font-size: 12px;
    border: 1px solid #3c3c3c;
    border-radius: 5px;
    margin-top: 10px;
    padding: 8px 6px 6px 6px;
    color: #cccccc;
}
QGroupBox#utilityGroup {
    margin-top: 10px;
    padding: 16px 6px 6px 6px;
}
QGroupBox#logGroup {
    margin-top: 6px;
    padding: 6px 6px 4px 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 6px;
}
QFrame#opsDivider {
    background-color: #3c3c3c;
    max-width: 1px;
}
QPushButton[buttonRole="workflow"] {
    text-align: left;
    padding: 5px 8px;
    border: 1px solid #404040;
    border-radius: 4px;
    background-color: #2d2d30;
    color: #e8e8e8;
    font-size: 11px;
}
QPushButton[buttonRole="workflow"]:hover:enabled {
    background-color: #094771;
    border-color: #3794ff;
    color: #ffffff;
}
QPushButton[buttonRole="workflow"]:pressed:enabled {
    background-color: #062d4a;
}
QPushButton[buttonRole="utility"] {
    text-align: left;
    padding: 5px 8px;
    border: 1px solid #3a4a3a;
    border-radius: 4px;
    background-color: #2a2f2a;
    color: #d8e8d8;
    font-size: 11px;
}
QPushButton[buttonRole="utility"]:hover:enabled {
    background-color: #2d4a2d;
    border-color: #6abf69;
    color: #ffffff;
}
QPushButton[buttonRole="utility"]:pressed:enabled {
    background-color: #1e3a1e;
}
QPushButton[buttonRole="danger"] {
    text-align: left;
    padding: 5px 8px;
    border: 1px solid #5a3a2a;
    border-radius: 4px;
    background-color: #3a2a22;
    color: #ffccbc;
    font-size: 11px;
    font-weight: 600;
}
QPushButton[buttonRole="danger"]:hover:enabled {
    background-color: #8b4513;
    border-color: #ff8a50;
    color: #ffffff;
}
QPushButton[buttonRole="danger"]:pressed:enabled {
    background-color: #5c2e0a;
}
QPushButton[buttonRole="restart"] {
    text-align: left;
    padding: 5px 8px;
    border: 1px solid #3a3a5a;
    border-radius: 4px;
    background-color: #2a2a3a;
    color: #c5cae9;
    font-size: 11px;
}
QPushButton[buttonRole="restart"]:hover:enabled {
    background-color: #3949ab;
    border-color: #7986cb;
    color: #ffffff;
}
QPushButton:disabled {
    background-color: #1e1e1e;
    color: #5a5a5a;
    border-color: #333333;
}
"""


def extract_percent(line: str) -> int | None:
    match = re.search(r"(\d+)\s*%", line)
    if match:
        return int(match.group(1))
    return None


def extract_block_progress(line: str) -> tuple[int, int] | None:
    """FPT prints e.g. 'Processed memory blocks 192 from 3071'."""
    match = re.search(r"(\d+)\s+from\s+(\d+)", line, re.IGNORECASE)
    if not match:
        return None
    current = int(match.group(1))
    total = int(match.group(2))
    if total <= 0 or current > total:
        return None
    return current, total


def extract_progress_percent(line: str) -> int | None:
    percent = extract_percent(line)
    if percent is not None:
        return percent
    blocks = extract_block_progress(line)
    if blocks:
        current, total = blocks
        return current * 100 // total
    return None


def is_progress_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if extract_block_progress(stripped):
        return True
    if extract_percent(stripped) is not None:
        return True
    if re.search(
        r"(?i)(reading|writing|programming|verifying|erasing|read|write|flash)",
        stripped,
    ) and re.search(r"\d", stripped):
        return True
    return False


def format_fpt_progress_display(raw_line: str, timestamp: str) -> str:
    blocks = extract_block_progress(raw_line)
    percent = extract_progress_percent(raw_line)
    if blocks:
        current, total = blocks
        pct = percent if percent is not None else 0
        return f"[{timestamp}] FPT progress: block {current}/{total} ({pct}%)"
    if percent is not None:
        return f"[{timestamp}] FPT progress: {percent}%"
    return f"[{timestamp}] {raw_line}"


def split_process_output(chunk: str) -> list[str]:
    normalized = chunk.replace("\r\n", "\n")
    lines: list[str] = []
    for piece in re.split(r"[\r\n]+", normalized):
        text = piece.strip()
        if text:
            lines.append(text)
    return lines


def classify_log_line(message: str) -> str:
    if "COMMAND:" in message:
        return "command"
    if "WORKING DIRECTORY:" in message:
        return "cwd"
    if "EXIT CODE:" in message:
        match = re.search(r"EXIT CODE:\s*(\d+)", message)
        return "exit_ok" if match and match.group(1) == "0" else "exit_fail"
    if FPT_SUCCESS_MARKER in message or REWRITE_IDENTICAL_MARKER in message:
        return "success"
    if re.search(r"\bError\s+\d+:", message, re.IGNORECASE):
        return "error"
    if any(
        token in message
        for token in (
            "Unsupported hardware platform",
            "failed",
            "Refusing to overwrite",
            "Do not flash",
            "not found",
            "markers were not found",
        )
    ):
        return "error" if "failed" in message.lower() else "warning"
    if any(
        token in message
        for token in (
            "complete",
            "successful",
            "passed",
            "Session created",
            "saved to",
            "Copied modified",
        )
    ):
        return "success"
    return "normal"


def fpt_platform_mismatch_hint(output: str) -> str | None:
    if "Unsupported hardware platform" not in output and "Error 621" not in output:
        return None
    hw_match = re.search(r"HW:\s*([^.]+)", output)
    supported_match = re.search(r"Supported HW:\s*([^.]+)", output)
    hw = hw_match.group(1).strip() if hw_match else "your chipset"
    supported = supported_match.group(1).strip() if supported_match else "a different platform"
    return (
        f"FPT platform mismatch: this FPT build targets {supported}, but the machine "
        f"reports {hw}. Replace tools\\fpt\\WIN64 with Intel CSME System Tools whose "
        f"Flash Programming Tool matches your chipset generation (not Comet Lake v14 "
        f"for a Sunrise Point / H110 system)."
    )


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def sessions_directory() -> Path:
    path = application_root() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_session_folders() -> list[Path]:
    return sorted(
        folder for folder in sessions_directory().iterdir() if folder.is_dir()
    )


def resource_path(name: str) -> Path:
    """Resolve bundled assets (PyInstaller onefile extracts to sys._MEIPASS)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / name
            if bundled.is_file():
                return bundled
        beside_exe = application_root() / name
        if beside_exe.is_file():
            return beside_exe
    return Path(__file__).resolve().parent / name


def application_icon() -> QIcon:
    path = resource_path(ICON_FILE_NAME)
    if path.is_file():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return QIcon()


def configure_windows_integration() -> None:
    """Ensure custom icon appears on the taskbar (not the generic Python icon)."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except OSError:
        pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def restart_as_admin() -> None:
    params = " ".join(f'"{arg}"' if " " in arg else arg for arg in sys.argv[1:])
    executable = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        params or None,
        str(application_root()),
        1,
    )


def find_fpt_executable() -> Path | None:
    root = application_root()
    candidates = (
        root / "tools" / "fpt" / "WIN64" / "FPTW64.exe",
        root / "tools" / "FPTW64.exe",
        root / "FPTW64.exe",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def fpt_working_directory(fpt_path: Path) -> Path:
    return fpt_path.parent


def build_fpt_argument_list(extra: list[str]) -> list[str]:
    """FPT args with -y (no prompts). Resolve filesystem paths for Windows."""
    resolved: list[str] = ["-y"]
    for arg in extra:
        if arg.startswith("-"):
            resolved.append(arg)
            continue
        candidate = Path(arg)
        if candidate.is_absolute() or (len(arg) > 1 and arg[1] == ":"):
            resolved.append(str(candidate.resolve()))
        else:
            resolved.append(arg)
    return resolved


def build_windows_command_line(program: str, arguments: list[str]) -> str:
    return subprocess.list2cmdline([program, *arguments])


def fpt_invalid_args_hint(output: str) -> str | None:
    if "Error 37" not in output and "Invalid command line option" not in output:
        return None
    return (
        "FPT rejected the command line (Error 37). This often happens when the path "
        "contains spaces (for example Desktop\\BootMark v1.0.1). Move BootMark to a "
        "folder without spaces, such as C:\\BootMark, and try again."
    )


def sanitize_path_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", value or "Unknown")
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned or "Unknown"


def parse_format_list(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


def wmi_date_to_iso(value: str) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 8:
        return value
    try:
        dt = datetime.strptime(digits[:8], "%Y%m%d")
        return dt.date().isoformat()
    except ValueError:
        return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class DeviceInfo:
    computer_system: dict[str, str] = field(default_factory=dict)
    bios: dict[str, str] = field(default_factory=dict)
    baseboard: dict[str, str] = field(default_factory=dict)

    @property
    def computer_manufacturer(self) -> str:
        return self.computer_system.get("Manufacturer", "Unknown")

    @property
    def computer_model(self) -> str:
        return self.computer_system.get("Model", "Unknown")

    @property
    def bios_version(self) -> str:
        return self.bios.get("SMBIOSBIOSVersion") or self.bios.get("Version", "")

    @property
    def bios_vendor(self) -> str:
        return self.bios.get("Manufacturer", "")

    @property
    def baseboard_product(self) -> str:
        return self.baseboard.get("Product", "")

    def summary_dict(self, session_created_at: str) -> dict[str, str]:
        return {
            "computer_manufacturer": self.computer_manufacturer,
            "computer_model": self.computer_model,
            "bios_manufacturer": self.bios_vendor,
            "bios_version": self.bios_version,
            "bios_release_date": wmi_date_to_iso(self.bios.get("ReleaseDate", "")),
            "bios_serial_number": self.bios.get("SerialNumber", ""),
            "baseboard_manufacturer": self.baseboard.get("Manufacturer", ""),
            "baseboard_product": self.baseboard_product,
            "session_created_at": session_created_at,
            "bootmark_version": BOOTMARK_VERSION,
        }

    def format_summary_text(self, session_folder_name: str, created_at: str) -> str:
        """Single human-readable device summary for device_info\\device_summary.txt."""
        sep = "-" * 60
        lines = [
            "BootMark Device Summary",
            f"Session folder : {session_folder_name}",
            f"Created (UTC)  : {created_at}",
            f"BootMark         : {BOOTMARK_VERSION}",
            "",
            sep,
            "QUICK REFERENCE",
            sep,
            f"Manufacturer      : {self.computer_manufacturer}",
            f"Model             : {self.computer_model}",
            f"BaseBoard/Product : {self.baseboard_product}",
            f"BIOS Vendor       : {self.bios_vendor}",
            f"BIOS Version      : {self.bios_version}",
            "",
            sep,
            "DETAILS",
            sep,
            f"BaseBoard manufacturer : {self.baseboard.get('Manufacturer', '')}",
            f"BaseBoard version      : {self.baseboard.get('Version', '')}",
            f"BaseBoard serial       : {self.baseboard.get('SerialNumber', '')}",
            f"BIOS release date      : {wmi_date_to_iso(self.bios.get('ReleaseDate', ''))}",
            f"BIOS serial number     : {self.bios.get('SerialNumber', '')}",
            f"BIOS SMBIOS version    : {self.bios.get('SMBIOSBIOSVersion', '')}",
            "",
            sep,
            "RAW — Win32_ComputerSystem",
            sep,
        ]
        lines.extend(self._format_summary_lines(self.computer_system))
        lines.extend(
            [
                "",
                sep,
                "RAW — Win32_BIOS",
                sep,
            ]
        )
        lines.extend(self._format_summary_lines(self.bios))
        lines.extend(
            [
                "",
                sep,
                "RAW — Win32_BaseBoard",
                sep,
            ]
        )
        lines.extend(self._format_summary_lines(self.baseboard))
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_summary_lines(data: dict[str, str]) -> list[str]:
        if not data:
            return ["(no data)"]
        return [f"{key}: {value}" for key, value in data.items()]


@dataclass
class QueuedCommand:
    program: str
    arguments: list[str]
    working_directory: str
    operation: str
    success_markers: list[str] = field(default_factory=lambda: [FPT_SUCCESS_MARKER])
    command_line: str | None = None


class DeleteFoldersThread(QThread):
    """Delete session folders off the UI thread (large SPI backups can take a while)."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(int, int, list)  # deleted_count, total_count, failed_names

    def __init__(self, folders: list[Path]) -> None:
        super().__init__()
        self._folders = folders

    def run(self) -> None:
        deleted = 0
        failed: list[str] = []
        total = len(self._folders)
        for folder in self._folders:
            self.progress.emit(f"Deleting {folder.name}…")
            try:
                shutil.rmtree(folder)
                deleted += 1
            except OSError:
                failed.append(folder.name)
        self.finished.emit(deleted, total, failed)


class SerialCommandRunner:
    """Runs external commands one at a time via QProcess."""

    _FLUSH_MS = 250

    def __init__(self, line_callback: Callable[[str, LogLineKind], None]) -> None:
        self._line_callback = line_callback
        self._queue: list[QueuedCommand] = []
        self._process = QProcess()
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.finished.connect(self._on_finished)
        self._flush_timer = QTimer()
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush_stdout_buffer)
        self._busy = False
        self._current: QueuedCommand | None = None
        self._output_buffer = ""
        self._stdout_pending = ""
        self._suppress_fpt_help = False
        self._on_complete: Callable[[bool, str, str], None] | None = None
        self._on_idle: Callable[[], None] | None = None
        self._on_section_end: Callable[[], None] | None = None
        self._on_command_start: Callable[[], None] | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    def set_completion_handler(
        self, handler: Callable[[bool, str, str], None] | None
    ) -> None:
        self._on_complete = handler

    def set_idle_handler(self, handler: Callable[[], None] | None) -> None:
        self._on_idle = handler

    def set_section_handler(self, handler: Callable[[], None] | None) -> None:
        self._on_section_end = handler

    def set_command_start_handler(self, handler: Callable[[], None] | None) -> None:
        self._on_command_start = handler

    def enqueue(self, command: QueuedCommand) -> None:
        self._queue.append(command)
        if not self._busy:
            self._start_next()

    def clear_queue(self) -> None:
        self._queue.clear()

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _schedule_stdout_flush(self) -> None:
        if not self._flush_timer.isActive():
            self._flush_timer.start(self._FLUSH_MS)

    def _emit_process_line(self, line: str) -> None:
        if self._suppress_fpt_help and re.match(r"^-\w", line.strip()):
            return
        if "Invalid command line option" in line or "Error 37:" in line:
            self._suppress_fpt_help = True
        kind: LogLineKind = "progress" if is_progress_line(line) else "normal"
        self._line_callback(line, kind)

    def _flush_stdout_buffer(self) -> None:
        if not self._stdout_pending:
            return
        data = self._stdout_pending
        self._stdout_pending = ""
        self._output_buffer += data
        lines = split_process_output(data)
        pending_progress: str | None = None
        for line in lines:
            if is_progress_line(line):
                pending_progress = line
                continue
            if pending_progress is not None:
                self._emit_process_line(pending_progress)
                pending_progress = None
            self._emit_process_line(line)
        if pending_progress is not None:
            self._emit_process_line(pending_progress)

    def _on_stdout(self) -> None:
        chunk = bytes(self._process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if chunk:
            self._stdout_pending += chunk
            self._schedule_stdout_flush()

    def _start_next(self) -> None:
        if not self._queue:
            self._busy = False
            self._current = None
            return
        self._busy = True
        self._current = self._queue.pop(0)
        assert self._current is not None
        self._output_buffer = ""
        self._stdout_pending = ""
        self._suppress_fpt_help = False
        if self._on_command_start is not None:
            self._on_command_start()
        cmd = self._current
        display = cmd.command_line or f"{cmd.program} {' '.join(cmd.arguments)}"
        ts = self._timestamp()
        self._line_callback(f"[{ts}] COMMAND: {display}", "command")
        self._line_callback(
            f"[{ts}] WORKING DIRECTORY: {cmd.working_directory}",
            "command",
        )
        self._process.setWorkingDirectory(cmd.working_directory)
        if cmd.command_line:
            self._process.startCommand(cmd.command_line)
        else:
            self._process.start(cmd.program, cmd.arguments)

    def _on_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        self._flush_timer.stop()
        trailing = bytes(self._process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if trailing:
            self._stdout_pending += trailing
        self._flush_stdout_buffer()

        ts = self._timestamp()
        self._line_callback(f"[{ts}] EXIT CODE: {exit_code}", "command")
        success = False
        operation = ""
        combined = self._output_buffer
        if self._current is not None:
            operation = self._current.operation
            markers = self._current.success_markers
            success = exit_code == 0 and all(
                marker in combined for marker in markers
            )
            if not success and exit_code == 0:
                self._line_callback(
                    f"[{ts}] Operation finished but required output markers "
                    "were not found.",
                    "normal",
                )
        if self._on_complete:
            self._on_complete(success, operation, combined)
        self._current = None
        self._start_next()
        if self._on_section_end is not None:
            self._on_section_end()
        if not self._busy and self._on_idle is not None:
            self._on_idle()


class BootMarkWindow(QMainWindow):
    def __init__(self, icon: QIcon | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"BootMark {BOOTMARK_VERSION}")
        if icon is not None and not icon.isNull():
            self.setWindowIcon(icon)

        self._device_info = DeviceInfo()
        self._session_path: Path | None = None
        self._session_log_path: Path | None = None
        self._fpt_path: Path | None = find_fpt_executable()
        self._modified_file: Path | None = None
        self._rewrite_test_passed = False
        self._validation_passed = False
        self._flash_succeeded = False
        self._flash_attempted = False
        self._pending_log_lines: list[str] = []
        self._cim_pending: list[tuple[str, str]] = []
        self._deleting_sessions = False
        self._delete_thread: DeleteFoldersThread | None = None
        self._progress_anchor: int | None = None
        self._progress_end: int | None = None
        self._last_progress_percent: int | None = None
        self._last_status_percent: int | None = None
        self._last_progress_ui_time = 0.0

        self._runner = SerialCommandRunner(self._on_runner_line)
        self._runner.set_completion_handler(self._on_command_finished)
        self._runner.set_idle_handler(self._update_button_states)
        self._runner.set_section_handler(self._append_log_section)
        self._runner.set_command_start_handler(self._finalize_progress_line)

        self._build_ui()
        self._apply_initial_geometry()
        self._update_button_states()
        self._log_startup_status()

    def _apply_initial_geometry(self) -> None:
        """Tall narrow default so the window fits typical laptop screens."""
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
            return
        available = screen.availableGeometry()
        width = min(DEFAULT_WINDOW_WIDTH, int(available.width() * 0.92))
        height = min(DEFAULT_WINDOW_HEIGHT, int(available.height() * 0.92))
        width = max(width, MIN_WINDOW_WIDTH)
        height = max(height, MIN_WINDOW_HEIGHT)
        self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    # ------------------------------------------------------------------ UI
    def _make_button(
        self,
        attr: str,
        text: str,
        role: str,
        tooltip: str,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("buttonRole", role)
        button.setMinimumHeight(28)
        button.setMaximumHeight(30)
        button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.style().unpolish(button)
        button.style().polish(button)
        setattr(self, attr, button)
        return button

    def _add_buttons_to_column(
        self,
        column: QVBoxLayout,
        specs: list[tuple[str, str, str, str]],
    ) -> None:
        for attr, text, role, tooltip in specs:
            button = self._make_button(attr, text, role, tooltip)
            self._buttons.append(button)
            column.addWidget(button)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        self.setStyleSheet(APP_STYLESHEET)
        layout = QVBoxLayout(central)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        self._status_label = QLabel("")
        self._status_label.setObjectName("statusLabel")
        self._status_label.setWordWrap(True)
        self._status_label.setMaximumHeight(44)
        layout.addWidget(self._status_label)

        ops_row = QHBoxLayout()
        ops_row.setSpacing(8)
        ops_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        workflow_box = QGroupBox("Workflow — follow in order")
        workflow_box.setMinimumWidth(300)
        workflow_layout = QVBoxLayout(workflow_box)
        workflow_layout.setSpacing(3)
        workflow_layout.setContentsMargins(4, 2, 4, 4)
        workflow_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        utility_box = QGroupBox("Folders & system")
        utility_box.setObjectName("utilityGroup")
        utility_box.setMinimumWidth(155)
        utility_layout = QVBoxLayout(utility_box)
        utility_layout.setSpacing(2)
        utility_layout.setContentsMargins(4, 4, 4, 4)
        utility_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        divider = QFrame()
        divider.setObjectName("opsDivider")
        divider.setFrameShape(QFrame.Shape.VLine)

        self._buttons: list[QPushButton] = []

        workflow_specs = [
            (
                "btn_admin_device",
                "1. Check Admin / Device Info",
                "workflow",
                "Verify administrator rights and read system manufacturer, model, and BIOS info.",
            ),
            (
                "btn_create_session",
                "2. Create Session",
                "workflow",
                "Create a timestamped folder under sessions\\ and save device_info files.",
            ),
            (
                "btn_backup_bios",
                "3. Backup BIOS Region",
                "workflow",
                "FPT read into backups\\bios_region_original.bin and creates "
                "backups\\bios_region_edit_me.bin for H2OEZE.",
            ),
            (
                "btn_backup_spi",
                "4. Backup Full SPI",
                "workflow",
                "FPT full SPI dump into backups\\full_spi_original.bin.",
            ),
            (
                "btn_hash",
                "5. Hash Backups",
                "workflow",
                "Compute SHA256 hashes and write hashes\\hashes.txt.",
            ),
            (
                "btn_rewrite_test",
                "6. Test Rewrite Original BIOS Region",
                "workflow",
                "Prove FPT can rewrite the original BIOS-region backup safely.",
            ),
            (
                "btn_select_modified",
                "7. Select Modified BIOS File",
                "workflow",
                "Choose your edited BIOS region; copies to modified\\logo_modified.bin.",
            ),
            (
                "btn_validate",
                "8. Validate Modified File",
                "workflow",
                "Check size and hash vs. original backup before flashing.",
            ),
            (
                "btn_flash",
                "9. Flash Modified BIOS Region",
                "danger",
                "Write modified\\logo_modified.bin to the BIOS region. Irreversible without backup.",
            ),
        ]
        utility_specs = [
            (
                "btn_open_modified",
                "Open Modified Folder",
                "utility",
                "Open sessions\\…\\modified\\ in Explorer for logo_modified.bin after editing.",
            ),
            (
                "btn_open_session",
                "Open Session Folder",
                "utility",
                "Open the active session directory (backups, logs, device_info).",
            ),
            (
                "btn_restart",
                "Restart Now",
                "restart",
                "Reboot after a successful flash to see the new logo.",
            ),
            (
                "btn_restore",
                "Emergency: Restore Original",
                "danger",
                "Rollback only — flashes bios_region_original.bin if step 9 failed or PC "
                "will not boot. Skip if flash worked and the system is fine.",
            ),
            (
                "btn_clear_session",
                "Clear Active Session",
                "danger",
                "Delete the current session folder (backups, logs, device_info).",
            ),
            (
                "btn_clear_all_sessions",
                "Clear All Sessions",
                "danger",
                "Delete every folder under sessions\\ (all backups and logs).",
            ),
        ]

        self._add_buttons_to_column(workflow_layout, workflow_specs)
        self._add_buttons_to_column(utility_layout, utility_specs)
        utility_layout.addStretch(1)

        ops_row.addWidget(workflow_box, stretch=4)
        ops_row.addWidget(divider)
        ops_row.addWidget(utility_box, stretch=3)

        ops_panel = QWidget()
        ops_panel.setLayout(ops_row)
        ops_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        layout.addWidget(ops_panel)

        log_box = QGroupBox("Log")
        log_box.setObjectName("logGroup")
        log_layout = QVBoxLayout(log_box)
        log_layout.setSpacing(0)
        log_layout.setContentsMargins(4, 4, 4, 4)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFont(QFont("Consolas", 10))
        self._log_view.setMinimumHeight(200)
        self._log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log_view.setStyleSheet(
            "QTextEdit { background-color: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #3c3c3c; padding: 4px; }"
        )
        log_layout.addWidget(self._log_view)
        layout.addWidget(log_box, stretch=4)

        footer_year = datetime.now().year
        footer = QLabel(
            f'Copyright &copy; {footer_year} '
            f'<a href="https://github.com/SSujitX">Sujit Biswas</a>'
        )
        footer.setObjectName("footerLabel")
        footer.setTextFormat(Qt.TextFormat.RichText)
        footer.setOpenExternalLinks(True)
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        layout.addWidget(footer)

        self.btn_admin_device.clicked.connect(self._on_check_admin_device_info)
        self.btn_create_session.clicked.connect(self._on_create_session)
        self.btn_backup_bios.clicked.connect(self._on_backup_bios)
        self.btn_backup_spi.clicked.connect(self._on_backup_spi)
        self.btn_hash.clicked.connect(self._on_hash_backups)
        self.btn_rewrite_test.clicked.connect(self._on_rewrite_test)
        self.btn_open_modified.clicked.connect(self._on_open_modified_folder)
        self.btn_select_modified.clicked.connect(self._on_select_modified)
        self.btn_validate.clicked.connect(self._on_validate_modified)
        self.btn_flash.clicked.connect(self._on_flash_modified)
        self.btn_restore.clicked.connect(self._on_restore_original)
        self.btn_restart.clicked.connect(self._on_restart_now)
        self.btn_open_session.clicked.connect(self._on_open_session_folder)
        self.btn_clear_session.clicked.connect(self._on_clear_active_session)
        self.btn_clear_all_sessions.clicked.connect(self._on_clear_all_sessions)

        if not is_admin():
            self.btn_admin_device.setText("Restart as Administrator")

    # ---------------------------------------------------------------- logging
    def _write_log_file(self, message: str) -> None:
        if self._session_log_path:
            try:
                with self._session_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except OSError as exc:
                self._append_log(f"[log file error] {exc}", level="error")
        else:
            self._pending_log_lines.append(message)

    def _scroll_log_to_end(self) -> None:
        self._log_view.verticalScrollBar().setValue(
            self._log_view.verticalScrollBar().maximum()
        )

    def _finalize_progress_line(self) -> None:
        if self._progress_anchor is not None:
            cursor = self._log_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertHtml("<br>")
            self._log_view.setTextCursor(cursor)
        self._progress_anchor = None
        self._progress_end = None
        self._last_progress_percent = None
        self._last_status_percent = None
        self._last_progress_ui_time = 0.0

    def _update_progress_line(self, message: str, raw_line: str) -> None:
        percent = extract_progress_percent(raw_line)
        now = time.monotonic()
        first_line = self._progress_anchor is None
        if (
            not first_line
            and percent is not None
            and percent == self._last_progress_percent
            and (now - self._last_progress_ui_time) < 0.2
        ):
            return

        color = LOG_COLORS["meta"]
        safe = html.escape(message)
        cursor = self._log_view.textCursor()
        if first_line:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._progress_anchor = cursor.position()
            cursor.insertHtml(f'<span style="color:{color}">{safe}</span>')
            self._progress_end = cursor.position()
        else:
            cursor.setPosition(self._progress_anchor)
            cursor.setPosition(
                self._progress_end,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.removeSelectedText()
            cursor.insertHtml(f'<span style="color:{color}">{safe}</span>')
            self._progress_end = cursor.position()
        self._log_view.setTextCursor(cursor)
        self._last_progress_ui_time = now

        if percent is not None and percent != self._last_progress_percent:
            self._write_log_file(message)
            self._last_progress_percent = percent
            self._scroll_log_to_end()
        if percent is not None and percent != self._last_status_percent:
            self._last_status_percent = percent
            blocks = extract_block_progress(raw_line)
            if blocks:
                current, total = blocks
                self._set_status(f"FPT progress: block {current}/{total} ({percent}%)")
            else:
                self._set_status(f"FPT progress: {percent}%")

    def _on_runner_line(self, line: str, kind: LogLineKind) -> None:
        if not line:
            return
        if kind == "command":
            self._finalize_progress_line()
            self._append_log(line, level=classify_log_line(line))
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if kind == "progress" or is_progress_line(line):
            display = format_fpt_progress_display(line, ts)
            self._update_progress_line(display, line)
            return
        display = line if line.startswith("[") else f"[{ts}] {line}"
        self._finalize_progress_line()
        self._append_log(display, level=classify_log_line(line))

    def _append_log(self, message: str, *, level: str | None = None) -> None:
        if not message:
            return
        self._finalize_progress_line()
        log_level = level or classify_log_line(message)
        color = LOG_COLORS.get(log_level, LOG_COLORS["normal"])
        safe = html.escape(message)
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(f'<span style="color:{color}">{safe}</span><br>')
        self._log_view.setTextCursor(cursor)
        self._scroll_log_to_end()
        self._write_log_file(message)

    def _append_log_section(self) -> None:
        self._finalize_progress_line()
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(
            '<hr style="border:0;border-top:1px solid #444;margin:12px 0;">'
        )
        self._log_view.setTextCursor(cursor)
        if self._session_log_path:
            try:
                with self._session_log_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n" + ("-" * 72) + "\n\n")
            except OSError:
                pass

    def _flush_pending_logs(self) -> None:
        if not self._session_log_path:
            return
        for line in self._pending_log_lines:
            try:
                with self._session_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
        self._pending_log_lines.clear()

    def _log_startup_status(self) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append_log(f"[{ts}] BootMark {BOOTMARK_VERSION} started.")
        self._append_log(f"[{ts}] Application root: {application_root()}")
        if self._fpt_path:
            self._append_log(f"[{ts}] FPT found: {self._fpt_path}")
        else:
            self._append_log(f"[{ts}] FPT not found. Place FPTW64.exe under tools\\fpt\\WIN64.")
        self._append_log(
            f"[{ts}] Administrator: {'yes' if is_admin() else 'no — elevation required'}"
        )

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    # -------------------------------------------------------------- paths/state
    def _bios_backup_path(self) -> Path | None:
        if not self._session_path:
            return None
        return self._session_path / "backups" / BIOS_BACKUP_NAME

    def _bios_edit_copy_path(self) -> Path | None:
        if not self._session_path:
            return None
        return self._session_path / "backups" / BIOS_EDIT_COPY_NAME

    def _create_bios_edit_copy(self) -> Path | None:
        """Working copy in backups\\ for H2OEZE — original stays read-only."""
        original = self._bios_backup_path()
        if not original or not original.is_file():
            return None
        edit_copy = self._bios_edit_copy_path()
        assert edit_copy is not None
        if edit_copy.exists():
            return edit_copy
        shutil.copy2(original, edit_copy)
        return edit_copy

    def _full_spi_backup_path(self) -> Path | None:
        if not self._session_path:
            return None
        return self._session_path / "backups" / FULL_SPI_BACKUP_NAME

    def _modified_default_path(self) -> Path | None:
        if not self._session_path:
            return None
        return self._session_path / "modified" / MODIFIED_DEFAULT_NAME

    def _effective_modified_path(self) -> Path | None:
        if self._modified_file and self._modified_file.is_file():
            return self._modified_file
        default = self._modified_default_path()
        if default and default.is_file():
            return default
        return None

    def _bios_backup_exists(self) -> bool:
        path = self._bios_backup_path()
        return bool(path and path.is_file())

    def _is_busy(self) -> bool:
        return self._runner.busy or self._deleting_sessions

    def _ensure_not_busy(self) -> bool:
        if self._deleting_sessions:
            self._warn(
                "Busy",
                "Session folders are being deleted. Please wait.",
            )
            return False
        if self._runner.busy:
            self._warn(
                "Busy",
                "A firmware command is already running. Wait for it to finish.",
            )
            return False
        return True

    def _apply_dialog_icon(self, box: QMessageBox) -> None:
        icon = self.windowIcon()
        if not icon.isNull():
            box.setWindowIcon(icon)

    def _warn(self, title: str, text: str) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            title,
            text,
            QMessageBox.StandardButton.Ok,
            self,
        )
        self._apply_dialog_icon(box)
        box.exec()

    def _info(self, title: str, text: str) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Information,
            title,
            text,
            QMessageBox.StandardButton.Ok,
            self,
        )
        self._apply_dialog_icon(box)
        box.exec()

    def _question(
        self,
        title: str,
        text: str,
        *,
        warning: bool = False,
    ) -> QMessageBox.StandardButton:
        icon = QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Question
        box = QMessageBox(
            icon,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        self._apply_dialog_icon(box)
        return QMessageBox.StandardButton(box.exec())

    def _update_button_states(self) -> None:
        admin_ok = is_admin()
        fpt_ok = self._fpt_path is not None
        session_ok = self._session_path is not None
        bios_backup = self._bios_backup_exists()
        modified_selected = self._effective_modified_path() is not None
        busy = self._is_busy()

        if not is_admin():
            self.btn_admin_device.setText("Restart as Administrator")
            self.btn_admin_device.setEnabled(not self._deleting_sessions)
        else:
            self.btn_admin_device.setText("1. Check Admin / Device Info")
            self.btn_admin_device.setEnabled(not busy)

        self.btn_create_session.setEnabled(
            admin_ok and not busy and bool(self._device_info.computer_system)
        )
        self.btn_backup_bios.setEnabled(
            admin_ok and fpt_ok and session_ok and not busy
        )
        self.btn_backup_spi.setEnabled(
            admin_ok and fpt_ok and session_ok and not busy
        )
        self.btn_hash.setEnabled(session_ok and bios_backup and not busy)
        self.btn_rewrite_test.setEnabled(
            admin_ok
            and fpt_ok
            and session_ok
            and bios_backup
            and not busy
        )
        self.btn_open_modified.setEnabled(session_ok and not self._deleting_sessions)
        self.btn_select_modified.setEnabled(session_ok and not busy)
        self.btn_validate.setEnabled(
            session_ok and modified_selected and bios_backup and not busy
        )

        flash_ready = (
            admin_ok
            and fpt_ok
            and session_ok
            and bios_backup
            and self._rewrite_test_passed
            and self._validation_passed
            and not busy
        )
        self.btn_flash.setEnabled(flash_ready)

        self.btn_restore.setEnabled(
            admin_ok
            and fpt_ok
            and session_ok
            and bios_backup
            and self._flash_attempted
            and not busy
        )
        if self._flash_succeeded:
            self.btn_restore.setToolTip(
                "Optional rollback — only if the PC will not boot or the image was wrong. "
                "Not needed when flash succeeded and the laptop works."
            )
        elif self._flash_attempted:
            self.btn_restore.setToolTip(
                "Flash was attempted. Use this only if boot fails or you need the old BIOS back."
            )
        self.btn_restart.setEnabled(self._flash_succeeded and not busy)
        self.btn_open_session.setEnabled(session_ok and not self._deleting_sessions)
        self.btn_clear_session.setEnabled(session_ok and not busy)
        self.btn_clear_all_sessions.setEnabled(
            bool(list_session_folders()) and not busy
        )

    def _reset_session_state(self) -> None:
        self._session_path = None
        self._session_log_path = None
        self._modified_file = None
        self._rewrite_test_passed = False
        self._validation_passed = False
        self._flash_succeeded = False
        self._flash_attempted = False
        self._set_status("No active session. Run Create Session to start again.")

    def _start_background_delete(self, folders: list[Path], mode: str) -> None:
        if self._delete_thread is not None and self._delete_thread.isRunning():
            self._warn("Busy", "A delete operation is already in progress.")
            return

        self._deleting_sessions = True
        self._update_button_states()

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if mode == "active":
            self._append_log(f"[{ts}] Deleting active session folder…")
        else:
            self._append_log(
                f"[{ts}] Deleting {len(folders)} session folder(s) in background…"
            )
        self._set_status("Deleting session folders… please wait.")

        self._delete_thread = DeleteFoldersThread(folders)
        self._delete_thread.progress.connect(self._on_delete_progress)
        self._delete_thread.finished.connect(
            lambda deleted, total, failed: self._on_delete_finished(
                deleted, total, failed, mode
            )
        )
        self._delete_thread.start()

    @pyqtSlot(str)
    def _on_delete_progress(self, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append_log(f"[{ts}] {message}")
        self._set_status(message)

    def _offer_create_session(self) -> None:
        if not self._device_info.computer_system:
            return
        reply = self._question(
            "Create new session?",
            "Create a new session now? (Device info from step 1 will be reused.)",
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._on_create_session()

    def _on_delete_finished(
        self,
        deleted: int,
        total: int,
        failed_names: list,
        mode: str,
    ) -> None:
        self._deleting_sessions = False
        self._delete_thread = None
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if failed_names:
            self._append_log(
                f"[{ts}] Could not delete: {', '.join(failed_names)}",
                level="error",
            )
            self._warn(
                "Partial delete",
                f"Deleted {deleted} of {total} session folder(s).\n\n"
                f"Failed: {', '.join(failed_names)}\n\n"
                "Close Explorer windows or files inside those folders and try again.",
            )
            self._set_status(f"Delete incomplete ({deleted}/{total}).")
        elif mode == "active":
            self._append_log(f"[{ts}] Active session deleted.", level="success")
            self._info("Session cleared", "Active session folder was deleted.")
            self._set_status("No active session.")
            self._offer_create_session()
        else:
            self._append_log(
                f"[{ts}] Cleared all sessions ({deleted} folder(s)).",
                level="success",
            )
            self._info("Sessions cleared", f"Deleted {deleted} session folder(s).")
            self._set_status("No active session.")
            self._offer_create_session()

        self._update_button_states()

    # ------------------------------------------------------------- button handlers
    @pyqtSlot()
    def _on_check_admin_device_info(self) -> None:
        if not is_admin():
            restart_as_admin()
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(500, app.quit)
            return
        if not self._ensure_not_busy():
            return
        self._collect_device_info()

    def _collect_device_info(self) -> None:
        self._append_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Collecting device info...")
        specs = [
            (
                "computer_system",
                'Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model | Format-List',
            ),
            (
                "bios",
                'Get-CimInstance Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion, Version, ReleaseDate, SerialNumber | Format-List',
            ),
            (
                "baseboard",
                'Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product, Version, SerialNumber | Format-List',
            ),
        ]
        self._device_info = DeviceInfo()
        self._cim_pending = list(specs)
        self._update_button_states()
        self._run_next_cim_query()

    def _run_next_cim_query(self) -> None:
        if not self._cim_pending:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._append_log(f"[{ts}] Device info collection complete.")
            self._append_log(
                f"[{ts}] {self._device_info.computer_manufacturer} "
                f"{self._device_info.computer_model}"
            )
            self._set_status(
                f"Device: {self._device_info.computer_manufacturer} "
                f"{self._device_info.computer_model}"
            )
            self._append_log(
                f"[{ts}] Device info is in memory only. Click '2. Create Session' "
                f"to save device_info\\device_summary.txt under sessions\\."
            )
            return
        target, command = self._cim_pending.pop(0)
        args = [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
        self._cim_target = target
        self._runner.enqueue(
            QueuedCommand(
                program="powershell.exe",
                arguments=args,
                working_directory=str(application_root()),
                operation=f"device_info_{target}",
                success_markers=[],
            )
        )

    @pyqtSlot()
    def _on_create_session(self) -> None:
        if not is_admin():
            self._warn("Administrator required", "Run BootMark as Administrator.")
            return
        if not self._device_info.computer_system:
            self._warn("Device info missing", "Run Check Admin / Device Info first.")
            return
        if self._session_path:
            reply = self._question(
                "Session exists",
                "A session is already active. Create a new session folder?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        manufacturer = sanitize_path_component(self._device_info.computer_manufacturer)
        model = sanitize_path_component(self._device_info.computer_model)
        folder_name = f"{stamp}_{manufacturer}_{model}"
        session_path = application_root() / "sessions" / folder_name
        created_at = datetime.now(timezone.utc).isoformat()

        subdirs = (
            "device_info",
            "backups",
            "modified",
            "hashes",
            "logs",
        )
        for name in subdirs:
            (session_path / name).mkdir(parents=True, exist_ok=True)

        (session_path / "device_info" / "computer_system.txt").write_text(
            self._format_dict_block(self._device_info.computer_system),
            encoding="utf-8",
        )
        (session_path / "device_info" / "bios.txt").write_text(
            self._format_dict_block(self._device_info.bios),
            encoding="utf-8",
        )
        (session_path / "device_info" / "baseboard.txt").write_text(
            self._format_dict_block(self._device_info.baseboard),
            encoding="utf-8",
        )
        summary = self._device_info.summary_dict(created_at)
        (session_path / "device_info" / "summary.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )
        summary_path = session_path / "device_info" / "device_summary.txt"
        summary_path.write_text(
            self._device_info.format_summary_text(folder_name, created_at),
            encoding="utf-8",
        )

        self._session_path = session_path
        self._session_log_path = session_path / "logs" / "bootmark_session.log"
        self._flush_pending_logs()
        self._rewrite_test_passed = False
        self._validation_passed = False
        self._flash_succeeded = False
        self._flash_attempted = False
        self._modified_file = None

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append_log(f"[{ts}] Session created: {session_path}")
        self._append_log(f"[{ts}] Device summary saved: {summary_path}", level="success")
        self._set_status(f"Session: {session_path.name}")

        if not self._ensure_not_busy():
            return
        nfo_path = session_path / "device_info" / "msinfo32.nfo"
        self._runner.enqueue(
            QueuedCommand(
                program="msinfo32.exe",
                arguments=["/nfo", str(nfo_path)],
                working_directory=str(application_root()),
                operation="msinfo32",
                success_markers=[],
            )
        )
        self._update_button_states()

    @staticmethod
    def _format_dict_block(data: dict[str, str]) -> str:
        lines = [f"{key:28}: {value}" for key, value in data.items()]
        return "\n".join(lines) + ("\n" if lines else "")

    def _run_fpt(
        self,
        operation: str,
        arguments: list[str],
        success_markers: list[str] | None = None,
    ) -> bool:
        if not self._ensure_not_busy():
            return False
        if not is_admin():
            self._warn("Administrator required", "Run BootMark as Administrator.")
            return False
        if not self._fpt_path:
            self._warn("FPT missing", "FPTW64.exe was not found.")
            return False
        if not self._session_path:
            self._warn("No session", "Create a session first.")
            return False
        markers = success_markers if success_markers is not None else [FPT_SUCCESS_MARKER]
        program = str(self._fpt_path.resolve())
        args = build_fpt_argument_list(arguments)
        command_line = (
            build_windows_command_line(program, args) if sys.platform == "win32" else None
        )
        self._runner.enqueue(
            QueuedCommand(
                program=program,
                arguments=args,
                working_directory=str(fpt_working_directory(self._fpt_path)),
                operation=operation,
                success_markers=markers,
                command_line=command_line,
            )
        )
        self._update_button_states()
        return True

    @pyqtSlot()
    def _on_backup_bios(self) -> None:
        path = self._bios_backup_path()
        assert path is not None
        if path.exists():
            self._append_log(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Refusing to overwrite existing backup: {path}"
            )
            self._warn(
                "Backup exists",
                "Original BIOS-region backup already exists and will not be overwritten.",
            )
            return
        self._rewrite_test_passed = False
        self._validation_passed = False
        self._run_fpt(
            "backup_bios",
            ["-bios", "-d", str(path)],
        )

    @pyqtSlot()
    def _on_backup_spi(self) -> None:
        path = self._full_spi_backup_path()
        assert path is not None
        if path.exists():
            self._append_log(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Refusing to overwrite existing backup: {path}"
            )
            self._warn(
                "Backup exists",
                "Original full SPI backup already exists and will not be overwritten.",
            )
            return
        self._run_fpt(
            "backup_spi",
            ["-d", str(path)],
        )

    @pyqtSlot()
    def _on_hash_backups(self) -> None:
        if not self._session_path:
            return
        lines: list[str] = []
        targets = [
            self._bios_backup_path(),
            self._bios_edit_copy_path(),
            self._full_spi_backup_path(),
            self._modified_default_path(),
        ]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for path in targets:
            if path and path.is_file():
                digest = sha256_file(path)
                rel = path.relative_to(self._session_path)
                line = f"{rel} SHA256: {digest}"
                lines.append(line)
                self._append_log(f"[{ts}] {line}")
            elif path:
                self._append_log(f"[{ts}] Skipped (not present): {path.name}")
        if not lines:
            self._info("Hash", "No files to hash.")
            return
        hash_path = self._session_path / "hashes" / "hashes.txt"
        hash_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._append_log(f"[{ts}] Hashes saved to {hash_path}")
        self._update_button_states()

    @pyqtSlot()
    def _on_rewrite_test(self) -> None:
        backup = self._bios_backup_path()
        if not backup or not backup.is_file():
            self._warn("Backup missing", "Create a BIOS-region backup first.")
            return
        self._rewrite_test_passed = False
        self._validation_passed = False
        self._run_fpt(
            "rewrite_test",
            ["-bios", "-rewrite", "-f", str(backup)],
            success_markers=[FPT_SUCCESS_MARKER, REWRITE_IDENTICAL_MARKER],
        )

    @pyqtSlot()
    def _on_open_modified_folder(self) -> None:
        if not self._session_path:
            return
        folder = self._session_path / "modified"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    @pyqtSlot()
    def _on_select_modified(self) -> None:
        if not self._session_path:
            return
        start_dir = str(self._session_path / "modified")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Modified BIOS Region File",
            start_dir,
            "BIOS images (*.bin);;All files (*.*)",
        )
        if not file_path:
            return
        selected = Path(file_path)
        target = self._modified_default_path()
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        if selected.resolve() != target.resolve():
            shutil.copy2(selected, target)
            self._append_log(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Copied modified file to {target}"
            )
        self._modified_file = target
        self._validation_passed = False
        self._append_log(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Modified file selected: {target}"
        )
        self._update_button_states()

    @pyqtSlot()
    def _on_validate_modified(self) -> None:
        self._validation_passed = False
        original = self._bios_backup_path()
        modified = self._effective_modified_path()
        if not original or not original.is_file():
            self._set_status("BIOS-region backup missing.")
            self._update_button_states()
            return
        if not modified or not modified.is_file():
            self._set_status("Modified file missing. Place logo_modified.bin in modified\\.")
            self._update_button_states()
            return

        orig_size = original.stat().st_size
        mod_size = modified.stat().st_size
        if mod_size != orig_size:
            msg = "Do not flash. Modified file size does not match original BIOS-region backup."
            self._set_status(msg)
            self._append_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")
            self._update_button_states()
            return

        orig_hash = sha256_file(original)
        mod_hash = sha256_file(modified)
        if orig_hash == mod_hash:
            msg = "Modified file is identical to original. Nothing changed."
            self._set_status(msg)
            self._append_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")
            self._update_button_states()
            return

        self._validation_passed = True
        msg = "Modified file validation passed."
        self._set_status(msg)
        self._append_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")
        self._append_log(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Original SHA256: {orig_hash}"
        )
        self._append_log(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Modified SHA256: {mod_hash}"
        )
        self._on_hash_backups()
        self._update_button_states()

    @pyqtSlot()
    def _on_flash_modified(self) -> None:
        if not self._rewrite_test_passed or not self._validation_passed:
            self._warn(
                "Not ready",
                "Complete rewrite test and modified-file validation before flashing.",
            )
            return
        modified = self._modified_default_path()
        if not modified or not modified.is_file():
            self._warn(
                "Modified file missing",
                f"Place the edited BIOS region at:\n{modified}",
            )
            return
        reply = self._question(
            "Confirm flash",
            "You are about to flash the BIOS region. Continue only if this laptop is "
            "the intended target and you have backups.",
            warning=True,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._flash_succeeded = False
        self._flash_attempted = True
        self._update_button_states()
        self._run_fpt(
            "flash_modified",
            ["-bios", "-f", str(modified)],
        )

    @pyqtSlot()
    def _on_restore_original(self) -> None:
        backup = self._bios_backup_path()
        if not backup or not backup.is_file():
            self._warn("Backup missing", "No BIOS-region backup to restore.")
            return
        reply = self._question(
            "Emergency restore",
            "Put the ORIGINAL BIOS region back from backup?\n\n"
            "Use this only if flash failed, the PC will not boot, or the image was wrong.\n"
            "If step 9 worked and the laptop is fine, choose No and use Restart instead.",
            warning=True,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._run_fpt(
            "restore_original",
            ["-bios", "-f", str(backup)],
        )

    @pyqtSlot()
    def _on_restart_now(self) -> None:
        if not self._ensure_not_busy():
            return
        self._runner.enqueue(
            QueuedCommand(
                program="shutdown.exe",
                arguments=["/r", "/t", "0"],
                working_directory=str(application_root()),
                operation="restart",
                success_markers=[],
            )
        )

    @pyqtSlot()
    def _on_open_session_folder(self) -> None:
        if self._session_path:
            os.startfile(self._session_path)

    @pyqtSlot()
    def _on_clear_active_session(self) -> None:
        if not self._session_path:
            self._warn("No session", "There is no active session to clear.")
            return
        if not self._ensure_not_busy():
            return
        folder = self._session_path
        reply = self._question(
            "Clear active session",
            f"Delete this session folder and all of its contents?\n\n{folder}\n\n"
            "This cannot be undone.",
            warning=True,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Release log file handle before Windows can delete the folder.
        self._reset_session_state()
        self._start_background_delete([folder], "active")

    @pyqtSlot()
    def _on_clear_all_sessions(self) -> None:
        if not self._ensure_not_busy():
            return
        folders = list_session_folders()
        if not folders:
            self._info("No sessions", "The sessions folder is already empty.")
            return
        count = len(folders)
        reply = self._question(
            "Clear all sessions",
            f"Delete ALL {count} session folder(s) under:\n\n{sessions_directory()}\n\n"
            "Every backup, log, and device_info file will be removed. "
            "This cannot be undone.",
            warning=True,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._reset_session_state()
        self._start_background_delete(folders, "all")

    # ---------------------------------------------------------- command completion
    def _on_command_finished(self, success: bool, operation: str, output: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if operation.startswith("device_info_"):
            target = operation.replace("device_info_", "", 1)
            parsed = parse_format_list(output)
            if target == "computer_system":
                self._device_info.computer_system = parsed
            elif target == "bios":
                self._device_info.bios = parsed
            elif target == "baseboard":
                self._device_info.baseboard = parsed
            self._run_next_cim_query()
            return

        if operation == "msinfo32":
            if success:
                self._append_log(f"[{ts}] msinfo32 export completed.", level="success")
            else:
                self._append_log(
                    f"[{ts}] msinfo32 export finished with warnings or errors.",
                    level="warning",
                )
            return

        mismatch = fpt_platform_mismatch_hint(output)
        if mismatch and operation in {
            "backup_bios",
            "backup_spi",
            "rewrite_test",
            "flash_modified",
            "restore_original",
        }:
            self._append_log(f"[{ts}] {mismatch}", level="error")
            self._set_status("FPT wrong platform — replace tools\\fpt\\WIN64 with matching CSME kit.")
            self._warn("FPT platform mismatch", mismatch)

        invalid_args = fpt_invalid_args_hint(output)
        if invalid_args and operation in {
            "backup_bios",
            "backup_spi",
            "rewrite_test",
            "flash_modified",
            "restore_original",
        }:
            self._append_log(f"[{ts}] {invalid_args}", level="error")
            self._warn("FPT command line error", invalid_args)

        if operation == "backup_bios" and success:
            self._append_log(f"[{ts}] BIOS-region backup successful.", level="success")
            self._append_log(
                f"[{ts}] Original (do not edit): backups\\{BIOS_BACKUP_NAME}",
                level="success",
            )
            edit_copy = self._create_bios_edit_copy()
            if edit_copy:
                self._append_log(
                    f"[{ts}] --- Edit this file in H2OEZE ---",
                    level="success",
                )
                self._append_log(
                    f"[{ts}] backups\\{BIOS_EDIT_COPY_NAME}",
                    level="success",
                )
                self._append_log(
                    f"[{ts}] Open this in H2OEZE — working copy "
                    "(auto-created from the original).",
                    level="success",
                )
                self._append_log(
                    f"[{ts}] After editing, save your flash image as: "
                    f"modified\\{MODIFIED_DEFAULT_NAME}",
                    level="success",
                )
            else:
                self._append_log(
                    f"[{ts}] Could not create backups\\{BIOS_EDIT_COPY_NAME}.",
                    level="warning",
                )
        elif operation == "backup_spi" and success:
            self._append_log(f"[{ts}] Full SPI backup successful.", level="success")
            self._append_log(
                f"[{ts}] Saved: backups\\{FULL_SPI_BACKUP_NAME} — safety archive only; "
                f"for logo edits use the BIOS-region backup file above.",
                level="success",
            )
        elif operation == "rewrite_test":
            self._rewrite_test_passed = success
            if success:
                self._append_log(f"[{ts}] Rewrite test passed.")
            else:
                self._append_log(f"[{ts}] Rewrite test failed.")
                self._validation_passed = False
        elif operation == "flash_modified":
            if success:
                self._flash_succeeded = True
                self._append_log(f"[{ts}] Flash successful.")
                self._append_log(
                    f"[{ts}] If the PC boots normally, use Restart — "
                    "Emergency Restore is not required.",
                    level="success",
                )
                self._info(
                    "Flash successful",
                    "Flash successful. Restart to view the new boot logo.\n\n"
                    "You do not need Emergency Restore if the laptop boots normally.",
                )
                self._update_button_states()
            else:
                self._append_log(f"[{ts}] Flash failed.")
                self._append_log(
                    f"[{ts}] If the PC will not boot, use Emergency: Restore Original "
                    "on the right.",
                    level="warning",
                )
                self._update_button_states()
        elif operation == "restore_original":
            if success:
                self._append_log(f"[{ts}] Restore successful.")
            else:
                self._append_log(f"[{ts}] Restore failed.")
        elif operation == "restart":
            self._append_log(f"[{ts}] Restart command issued.")


def main() -> int:
    configure_windows_integration()
    app = QApplication(sys.argv)
    app.setApplicationName("BootMark")
    app.setApplicationDisplayName("BootMark")

    icon = application_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    window = BootMarkWindow(icon if not icon.isNull() else None)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
