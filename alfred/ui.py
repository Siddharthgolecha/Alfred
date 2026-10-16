"""Terminal conversation, bounded request history, and live debug display."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import json
import os
import sys
import threading
import time

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .console import clean
from .platform_utils import sample_resources
from .trace import TraceStore, elapsed_ms

LOG = logging.getLogger(__name__)


@dataclass
class Message:
    speaker: str
    text: str


def _safe_text(value: object) -> str:
    return "\n".join(clean(line) for line in str(value or "").splitlines()).strip()


def _stat_value(value, unit=""):
    if value is None:
        return "unavailable"
    return f"{value:g}{unit}" if isinstance(value, (int, float)) else str(value)


class TerminalUI:
    """One output owner for chat, transient activity, and optional debug trace."""

    def __init__(self, *, debug=False, stdout=None, stderr=None, stdin=None,
                 sampler=sample_resources, clock=time.monotonic):
        self.debug = bool(debug)
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.stdin = sys.stdin if stdin is None else stdin
        self.clock = clock
        self.started = clock()
        self.trace = TraceStore(enabled=self.debug, clock=clock)
        same_terminal = all(
            getattr(stream, "isatty", lambda: False)()
            for stream in (self.stdout, self.stderr)
        )
        self.interactive = same_terminal and getattr(self.stdin, "isatty", lambda: False)()
        output = self.stderr if self.interactive else self.stdout
        self._owned_streams = []
        output = self._duplicate(output)
        diagnostic = output if self.interactive else self._duplicate(self.stderr)
        self.console = Console(file=output, markup=False, highlight=False,
                               color_system="auto" if self.interactive else None)
        self.diagnostic_console = (
            self.console if self.interactive else
            Console(file=diagnostic, markup=False, highlight=False, color_system=None)
        )
        self.messages = deque(maxlen=200)
        self.logs = deque(maxlen=100)
        self.activity_text = "Starting Alfred…"
        self.preview_text = ""
        self.stats = {"available_mib": None, "swap_used_mib": None,
                      "cpu_load_per_core": None}
        self._sampler = sampler
        self._selected = None
        self._selected_number = None
        self._detail_page = 0
        self._help = False
        self._finished = False
        self.request_state = "complete"
        self._source = "text"
        self._exit_browser = threading.Event()
        self._stop = threading.Event()
        self._live = None
        self._status = None
        self._threads = []
        self._terminal_state = None
        self._input_fd = None

    def _duplicate(self, stream):
        try:
            stream.flush()
            copy = os.fdopen(os.dup(stream.fileno()), "w", encoding="utf-8",
                             errors="replace", buffering=1)
            self._owned_streams.append(copy)
            return copy
        except (AttributeError, OSError, ValueError):
            return stream

    def start(self):
        if self.interactive:
            self._live = Live(self._render(), console=self.console,
                              refresh_per_second=4, transient=False,
                              redirect_stdout=False, redirect_stderr=False,
                              vertical_overflow="ellipsis")
            self._live.start()
            if self.debug:
                self._start_terminal_input()
            if self.debug:
                self._start_stats_thread()
        elif self.debug:
            self._start_stats_thread()
        return self

    def _start_terminal_input(self):
        try:
            fd = self.stdin.fileno()
            if os.name == "nt":
                import msvcrt
                self._input_fd = ("windows", msvcrt)
            else:
                import termios
                import tty
                self._input_fd = ("posix", fd, termios.tcgetattr(fd), termios, tty)
                tty.setcbreak(fd)
            thread = threading.Thread(target=self._read_keys, daemon=True,
                                      name="alfred-debug-keys")
            thread.start()
            self._threads.append(thread)
        except (OSError, AttributeError, ValueError):
            self._input_fd = None

    def _read_keys(self):
        if self._input_fd and self._input_fd[0] == "windows":
            msvcrt = self._input_fd[1]
            while not self._stop.is_set():
                try:
                    if msvcrt.kbhit():
                        key = msvcrt.getwch()
                        if key in ("\x00", "\xe0"):
                            key = "\x1b[" + msvcrt.getwch()
                        self._key(key)
                    else:
                        self._stop.wait(0.04)
                except (OSError, ValueError):
                    return
            return
        try:
            import select
            fd = self._input_fd[1]
            pending = ""
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    continue
                char = os.read(fd, 1).decode("utf-8", errors="ignore")
                pending += char
                if pending == "\x1b":
                    continue
                if pending.startswith("\x1b"):
                    if pending.endswith("~") or pending in ("\x1b[A", "\x1b[B", "\x1b[H", "\x1b[F"):
                        self._key(pending)
                        pending = ""
                    elif len(pending) > 8:
                        pending = ""
                    continue
                self._key(pending)
                pending = ""
        except (OSError, ValueError):
            return

    def _key(self, key):
        requests, _ = self.trace.snapshot()
        if key in ("\x1b[A", "k") and requests:
            current = len(requests) - 1 if self._selected_number is None else next(
                (index for index, item in enumerate(requests)
                 if item.number == self._selected_number), len(requests) - 1)
            self._selected = max(0, current - 1)
            self._selected_number = requests[self._selected].number
            self._detail_page = 0
        elif key in ("\x1b[B", "j") and requests:
            current = len(requests) - 1 if self._selected_number is None else next(
                (index for index, item in enumerate(requests)
                 if item.number == self._selected_number), len(requests) - 1)
            self._selected = min(len(requests) - 1, current + 1)
            self._selected_number = requests[self._selected].number
            self._detail_page = 0
        elif key in ("\x1b[H", "\x1b[1~") and requests:
            self._selected = 0
            self._selected_number = requests[0].number
            self._detail_page = 0
        elif key in ("\x1b[F", "\x1b[4~"):
            self._selected = None
            self._selected_number = None
            self._detail_page = 0
        elif key in ("\x1b[5~", "[", "-"):
            self._detail_page = max(0, self._detail_page - 1)
        elif key in ("\x1b[6~", "]", "+"):
            self._detail_page += 1
        elif key == "?":
            self._help = not self._help
        elif key.lower() == "q":
            if self._finished:
                self._exit_browser.set()
            else:
                self._selected = None
                self._selected_number = None
                self._detail_page = 0
                self._refresh()

    def refresh(self):
        self._refresh()

    def _start_stats_thread(self):
        interval = 1.0 if self.interactive else 5.0

        def sample_loop():
            while not self._stop.is_set():
                try:
                    values = self._sampler()
                    if self._stop.is_set():
                        return
                    if isinstance(values, dict):
                        self.stats = {key: values.get(key) for key in (
                            "available_mib", "swap_used_mib", "cpu_load_per_core")}
                        if not self.interactive:
                            uptime = int(max(0, self.clock() - self.started))
                            self.diagnostic_console.print(
                                "[debug stats] " + self._stats_text(uptime))
                except Exception:
                    self.stats = {"available_mib": None, "swap_used_mib": None,
                                  "cpu_load_per_core": None}
                    LOG.debug("Resource sample unavailable", exc_info=True)
                    if not self.interactive:
                        uptime = int(max(0, self.clock() - self.started))
                        self.diagnostic_console.print(
                            "[debug stats] " + self._stats_text(uptime))
                if self._stop.wait(interval):
                    return

        thread = threading.Thread(target=sample_loop, daemon=True,
                                  name="alfred-debug-stats")
        thread.start()
        self._threads.append(thread)

    def _stats_text(self, uptime=None):
        values = self.stats
        cpu = values.get("cpu_load_per_core")
        cpu_text = _stat_value(round(cpu, 2) if cpu is not None else None, "/core")
        ram = values.get("available_mib")
        swap = values.get("swap_used_mib")
        up = int(max(0, self.clock() - self.started)) if uptime is None else uptime
        return (f"CPU load {cpu_text} · RAM available {_stat_value(ram, ' MiB')} · "
                f"swap used {_stat_value(swap, ' MiB')} · Alfred uptime {up}s")

    def activity(self, text):
        self.activity_text = _safe_text(text)[:100]
        if self._status is not None:
            try:
                self._status.update(self.activity_text)
            except Exception:
                self._status = None
        if not self.debug and self.interactive and self._live is None and self._status is None:
            try:
                self._status = self.console.status(self.activity_text, spinner="dots")
                self._status.__enter__()
            except Exception:
                self._status = None
        self._refresh()

    def begin_request(self, source):
        self.request_state = "complete"
        self._source = source
        try:
            if self.trace.snapshot()[1] is None:
                self.trace.start(source)
            self.activity("Thinking…")
        except Exception:
            return

    @property
    def trace_active(self):
        return self.trace.snapshot()[1] is not None

    def record(self, stage, status, detail="", elapsed=None):
        try:
            self.trace.record(stage, status, detail, elapsed)
            if self.debug and not self.interactive:
                suffix = f" ({elapsed} ms)" if elapsed is not None else ""
                note = f" — {detail}" if detail else ""
                self.diagnostic_console.print(f"[trace] {stage}: {status}{note}{suffix}")
            self._refresh()
        except Exception:
            return

    def finish_request(self, state="complete"):
        self.request_state = state
        try:
            self.trace.finish(state)
            self.activity("Listening for wake word…" if self._source == "voice" else "Ready")
            self._refresh()
        except Exception:
            return

    def add_message(self, speaker, text):
        text = _safe_text(text)
        if not text:
            return
        if not self.interactive:
            if self._status is not None:
                try:
                    self._status.__exit__(None, None, None)
                except Exception:
                    pass
                self._status = None
            try:
                self.console.print(f"{speaker}: {text}")
            except Exception:
                return
        else:
            self.messages.append(Message(speaker, text))
            self._refresh()

    def user(self, text):
        self.add_message("You", text)

    def reply(self, text):
        self.add_message("Alfred", text)

    @staticmethod
    def _result_lines(value):
        if isinstance(value, list):
            lines = []
            for item in value:
                if isinstance(item, dict):
                    lines.extend(TerminalUI._result_lines(item))
                else:
                    lines.append(_safe_text(item)[:120])
            return lines
        if not isinstance(value, dict):
            return [_safe_text(value)[:160]]
        if value.get("skipped") is True:
            return ["Step skipped because its condition was not met."]
        labels = {
            "gpu_temp_c": ("GPU temperature", " °C"),
            "uptime_seconds": ("System uptime", " s"),
            "root_free_gib": ("Free disk space", " GiB"),
            "available_mib": ("Available memory", " MiB"),
            "swap_used_mib": ("Swap used", " MiB"),
            "cpu_load_per_core": ("CPU load per core", ""),
            "service_count": ("Services", ""),
            "id": ("ID", ""),
            "run_id": ("Run", ""),
            "name": ("Name", ""),
            "state": ("State", ""),
            "status": ("Status", ""),
            "message": ("Result", ""),
            "available": ("Available", ""),
        }
        lines = []
        for key, item in value.items():
            if key in ("skipped", "raw", "stdout", "stderr"):
                continue
            if key == "items" and isinstance(item, list):
                lines.extend(TerminalUI._result_lines(item))
                continue
            label, unit = labels.get(key, (key.replace("_", " ").capitalize(), ""))
            if isinstance(item, (dict, list)):
                nested = TerminalUI._result_lines(item)
                lines.extend(f"{label}: {line}" for line in nested)
            elif isinstance(item, bool):
                lines.append(f"{label}: {'yes' if item else 'no'}")
            elif item is not None:
                lines.append(f"{label}: {_safe_text(item)}{unit}")
        return lines or ["Completed."]

    def present_reply(self, message):
        """Render structured model/tool output as concise conversation text."""
        display = _safe_text(message)
        if display.startswith("Could not complete command:"):
            display = "I couldn't safely complete that request. Please try again."
        spoken = display
        try:
            value = json.loads(message)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, list):
            lines = self._result_lines(value)
            display = "\n".join(lines)
            spoken = " ".join(lines)
            self.reply(display)
            if self.debug:
                self.record("Response", "complete", "Reply displayed")
            return spoken
        if isinstance(value, dict) and (
                "results" in value or "message" in value or "approval" in value
                or "status" in value):
            lines = []
            summary = value.get("message")
            if isinstance(summary, str) and summary.strip():
                lines.append(_safe_text(summary))
            elif value.get("status") == "incomplete":
                lines.append("I couldn't complete that request.")
            results = value.get("results")
            if isinstance(results, list):
                for line in self._result_lines(results):
                    if line not in lines:
                        lines.append(line)
            outcomes = value.get("tool_outcomes")
            if isinstance(outcomes, list):
                for item in outcomes:
                    if isinstance(item, dict):
                        outcome = _safe_text(item.get("outcome", ""))
                        if outcome in {"blocked", "failed", "skipped", "staged", "partial"}:
                            tool = _safe_text(item.get("tool", "Step"))
                            lines.append(f"{tool}: {outcome}.")
            if value.get("partial") is True and not any("partial" in line.lower() for line in lines):
                lines.insert(0, "Partial result:")
            display = "\n".join(lines) or "I couldn't complete that request."
            spoken = " ".join(lines)
            if value.get("status") == "incomplete":
                self.request_state = "partial" if value.get("partial") else "failed"
            elif "approval" in value:
                self.request_state = "awaiting approval"
            else:
                self.request_state = "complete"
        self.reply(display)
        if self.debug:
            self.record("Response", "complete", "Reply displayed")
        return spoken

    def preview(self, text):
        safe = _safe_text(text)
        if self.interactive and self.debug:
            if self.messages and self.messages[-1].speaker == "You":
                self.messages[-1].text = safe
            else:
                self.messages.append(Message("You", safe))
            self._refresh()
        elif self.interactive:
            self.preview_text = safe
            self.activity_text = "Listening…"
            self._refresh()
        elif not self.interactive and not self.debug and self._status is not None:
            try:
                self._status.update(f"You: {safe}")
            except Exception:
                pass

    def clear_preview(self):
        self.preview_text = ""
        if self.interactive and self.debug and self.messages and self.messages[-1].speaker == "You":
            self.messages.pop()
            self._refresh()

    def wait_browser(self):
        self.browser()

    def log(self, record):
        try:
            formatted = logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ).format(record)
            self.logs.append(_safe_text(formatted))
            if not self.interactive:
                self.diagnostic_console.print(self.logs[-1])
            self._refresh()
        except Exception:
            # The UI is observational; an output failure cannot affect Alfred.
            return

    def _render_trace(self):
        requests, active = self.trace.snapshot()
        if not requests:
            return Panel("Waiting for an authorized request…", title="Request flow",
                         border_style="cyan")
        selected = len(requests) - 1 if self._selected_number is None else next(
            (index for index, item in enumerate(requests)
             if item.number == self._selected_number), len(requests) - 1)
        self._selected = selected
        self._selected_number = requests[selected].number
        request = requests[selected]
        history = Table(box=box.SIMPLE, expand=True, show_header=False, padding=(0, 1))
        history.add_column("Request", ratio=1)
        for index, item in enumerate(requests):
            marker = "▶ " if index == selected else "  "
            age = max(0, int(self.clock() - item.started))
            label = f"{marker}#{item.number} {item.source} · {item.state} · {age}s"
            history.add_row(Text(label[:52], style="bold cyan" if index == selected else ""))
        events = request.events
        page_size = max(4, min(12, self.console.height - 18))
        pages = max(1, (len(events) + page_size - 1) // page_size)
        page = min(self._detail_page, pages - 1)
        selected_events = events[page * page_size:(page + 1) * page_size]
        flow = Table(box=box.SIMPLE, expand=True, show_header=True, padding=(0, 1))
        flow.add_column("Status", width=3)
        flow.add_column("Stage", ratio=2)
        flow.add_column("Details", ratio=3)
        flow.add_column("Time", justify="right", width=9)
        for event in selected_events:
            marker = {"complete": "✓", "running": "→", "skipped": "–",
                      "blocked": "!", "awaiting approval": "?", "failed": "×",
                      "partial": "~", "interrupted": "×"}.get(event.status, "·")
            style = "green" if marker == "✓" else "yellow" if marker in "→?~" else "red" if marker in "!×" else "dim"
            flow.add_row(Text(marker, style=style), event.stage,
                         event.detail or event.status,
                         f"{event.elapsed_ms} ms" if event.elapsed_ms is not None else "")
        if request.dropped:
            flow.add_row("!", "Trace limit", f"{request.dropped} events omitted", "")
        content = Table.grid(expand=True)
        content.add_row(flow)
        content.add_row(Text(f"Page {page + 1}/{pages} · ↑/↓ request · PgUp/PgDn details · End live · ? help", style="dim"))
        history_panel = Panel(history, title="Recent requests", border_style="blue")
        detail_panel = Panel(content,
                             title=f"Request #{request.number} · {request.source} · {request.state}",
                             border_style="cyan")
        layout = Layout()
        if self.console.width >= 110:
            layout.split_row(Layout(history_panel, ratio=1), Layout(detail_panel, ratio=3))
        else:
            layout.split_column(Layout(history_panel, ratio=1), Layout(detail_panel, ratio=2))
        if self._help:
            return Group(layout, Panel("↑/↓ select request · PgUp/PgDn scroll trace · End follow live request · q close completed browser · Ctrl+C stop Alfred", title="Debug controls"))
        return layout

    def _render(self):
        if not self.debug:
            content = Table.grid(padding=(0, 1), expand=True)
            content.add_column(style="bold", width=8)
            content.add_column(ratio=1)
            for item in list(self.messages)[-12:]:
                content.add_row(item.speaker + ":", item.text)
            return Panel(Group(content, Text(self.activity_text, style="cyan")),
                         title="Alfred", border_style="cyan", padding=(0, 1))
        root = Layout()
        root.split_column(Layout(name="conversation", ratio=3), Layout(name="debug", ratio=2),
                          Layout(name="stats", size=3))
        convo = Table.grid(padding=(0, 1), expand=True)
        convo.add_column(style="bold", width=8)
        convo.add_column(ratio=1)
        for item in list(self.messages)[-12:]:
            convo.add_row(item.speaker + ":", item.text)
        if not self.messages:
            convo.add_row("Alfred:", "Ready when you are.")
        log_text = Text("\n".join(list(self.logs)[-5:]) or "No diagnostic events yet", style="dim")
        root["conversation"].update(Panel(Group(convo, log_text), title="Alfred · Debug", border_style="blue"))
        root["debug"].update(self._render_trace())
        root["stats"].update(Panel(
            f"{self.activity_text}  ·  {self._stats_text()}",
            title="Live system stats", border_style="green", padding=(0, 1)))
        return root

    def _refresh(self):
        if self._live is not None:
            try:
                self._live.update(self._render(), refresh=True)
            except Exception:
                LOG.debug("Terminal render failed", exc_info=True)

    def browser(self):
        if not self.debug or not self.interactive:
            return
        self._finished = True
        self.activity("Request complete · press q to close")
        self._exit_browser.wait()

    def close(self):
        self._stop.set()
        if self._input_fd and self._input_fd[0] == "posix":
            _, fd, state, termios, _tty = self._input_fd
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, state)
            except (OSError, ValueError):
                pass
        if self._status is not None:
            try:
                self._status.__exit__(None, None, None)
            except Exception:
                pass
            self._status = None
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        for stream in self._owned_streams:
            try:
                stream.close()
            except OSError:
                pass
        self._owned_streams.clear()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._threads.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()
