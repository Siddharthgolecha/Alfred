"""Small, dependency-free terminal UI for Alfred's installer."""
from __future__ import annotations

import os
import shutil
import sys
import textwrap
import time


class Terminal:
    """Accessible inline prompts with safe fallbacks for redirected terminals."""

    def __init__(self, stream=None, input_stream=None, plain=False):
        self.out = stream or sys.stdout
        self.input = input_stream or sys.stdin
        self.plain = plain
        self.tty = bool(getattr(self.out, "isatty", lambda: False)()
                        and getattr(self.input, "isatty", lambda: False)())
        term = os.environ.get("TERM", "").lower()
        ansi = ((os.name != "nt" and term != "dumb")
                or any(os.environ.get(name) for name in
                       ("WT_SESSION", "ANSICON", "ConEmuANSI", "TERM_PROGRAM")))
        self.arrows = self.tty and not plain and ansi
        self.color = self.arrows and "NO_COLOR" not in os.environ
        encoding = str(getattr(self.out, "encoding", "") or "").lower()
        self.unicode = self.arrows and ("utf" in encoding or "utf" in os.environ.get("LC_ALL", "").lower())

    def _paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def header(self, subtitle="guided setup"):
        mark = "▲" if self.unicode else "*"
        print("", file=self.out)
        print(f"  {self._paint(mark, '36')}  {self._paint('Alfred', '1')}  {subtitle}",
              file=self.out)
        print("  " + ("─" if self.unicode else "-") * 50, file=self.out)
        print("", file=self.out)

    def info(self, message):
        print(f"  {self._paint('·' if self.unicode else '-', '2')} {message}", file=self.out)

    def ok(self, message):
        icon = "✓" if self.unicode else "OK"
        print(f"  {self._paint(icon, '32')} {message}", file=self.out, flush=True)

    def warn(self, message):
        icon = "!" if not self.unicode else "⚠"
        print(f"  {self._paint(icon, '33')} {message}", file=self.out, flush=True)

    def error(self, message):
        icon = "x" if not self.unicode else "✖"
        print(f"  {self._paint(icon, '31')} {message}", file=self.out, flush=True)

    def section(self, title, detail=None):
        pointer = "›" if self.unicode else ">"
        print(f"\n  {self._paint(pointer, '36')} {self._paint(title, '1')}", file=self.out)
        if detail:
            self.info(detail)

    def _key(self):
        if os.name == "nt":
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch = msvcrt.getwch()
                return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch, "")
            return {"\r": "enter", "\x03": "interrupt", "\x04": "interrupt",
                    "\x08": "backspace"}.get(ch, ch)
        import termios
        import tty
        fd = self.input.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = self.input.read(1)
            if not ch:
                raise KeyboardInterrupt
            if ch == "\x1b":
                seq = self.input.read(2)
                return {"\x1b[A": "up", "\x1b[B": "down",
                        "\x1b[D": "left", "\x1b[C": "right"}.get(ch + seq, "")
            return {"\r": "enter", "\n": "enter", "\x03": "interrupt", "\x04": "interrupt",
                    "\x7f": "backspace"}.get(ch, ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    def choose(self, prompt, options, default=0, back=False):
        """Choose (stable_value, label, help) with arrows or numbered input."""
        if not options:
            raise ValueError("A selection needs at least one option")
        current = max(0, min(default, len(options) - 1))
        if not self.arrows:
            print(f"\n  {prompt}", file=self.out)
            for number, (_, label, help_text) in enumerate(options, 1):
                suffix = " (recommended)" if number - 1 == current else ""
                print(f"    {number}. {label}{suffix}", file=self.out)
                if help_text:
                    self.info(help_text)
            while True:
                answer = self._line(f"  Choose [{current + 1}]: ").strip()
                if not answer:
                    return options[current][0]
                if answer.lower() in ("b", "back") and back:
                    return "__back__"
                if answer.isdigit() and 1 <= int(answer) <= len(options):
                    return options[int(answer) - 1][0]
                self.warn("Enter a listed number" + (", or b to go back." if back else "."))

        print(f"  {self._paint('›' if self.unicode else '>', '36')} {prompt}", file=self.out)
        rows_drawn = 0
        while True:
            for _ in range(rows_drawn):
                self.out.write("\033[1A\033[2K")
            height = max(3, shutil.get_terminal_size((80, 24)).lines - 6)
            start = max(0, min(current - height + 1, len(options) - height))
            visible = options[start:start + height]
            rows = 0
            if start:
                print("     " + ("↑ more" if self.unicode else "^ more"), file=self.out)
                rows += 1
            for local_idx, (_, label, help_text) in enumerate(visible):
                idx = start + local_idx
                pointer = ("❯" if self.unicode else ">") if idx == current else " "
                width = max(12, shutil.get_terminal_size((80, 24)).columns - 10)
                label_rows = textwrap.wrap(label, width=width) or [""]
                label = label_rows[0]
                rendered = (self._paint(f" {pointer} {label}", "36;1") if idx == current else f"   {label}")
                print(f"  {rendered}", file=self.out)
                rows += 1
                for continuation in label_rows[1:]:
                    print(f"     {continuation}", file=self.out)
                    rows += 1
                if help_text and idx == current:
                    width = max(12, shutil.get_terminal_size((80, 24)).columns - 8)
                    wrapped = [help_text[i:i + width] for i in range(0, len(help_text), width)]
                    for line in wrapped:
                        print(f"     {line}", file=self.out)
                        rows += 1
            if start + len(visible) < len(options):
                print("     " + ("↓ more" if self.unicode else "v more"), file=self.out)
                rows += 1
            rows_drawn = rows
            self.out.flush()
            key = self._key()
            if key == "interrupt":
                raise KeyboardInterrupt
            if key in ("up", "left"):
                current = (current - 1) % len(options)
            elif key in ("down", "right"):
                current = (current + 1) % len(options)
            elif key == "enter":
                print(file=self.out)
                return options[current][0]
            elif key in ("b", "B") and back:
                print(file=self.out)
                return "__back__"
            elif key.isdigit() and 1 <= int(key) <= len(options):
                current = int(key) - 1

    def _line(self, prompt):
        self.out.write(prompt)
        self.out.flush()
        answer = self.input.readline()
        if answer == "":
            raise RuntimeError("Input ended before setup choices were complete; rerun in an interactive terminal or use --auto")
        return answer.rstrip("\r\n")

    def text(self, prompt, default=""):
        answer = self._line(f"  {prompt} [{default}]: ").strip()
        return answer or default

    def confirm(self, prompt, default=False):
        choice = self.choose(prompt, [("yes", "Yes", ""), ("no", "No", "")],
                             default=0 if default else 1)
        return choice == "yes"

    def progress(self, message):
        """Context manager compatible with a truthful, low-rate activity line."""
        return Progress(self, message)

    def download_progress(self, name, received, total=None):
        amount = received / (1024 * 1024)
        size = f" / {total / (1024 * 1024):.0f} MiB" if total else ""
        line = f"  Downloading {name}: {amount:.1f} MiB{size}"
        if self.arrows:
            self.out.write("\r\033[2K" + line)
            self.out.flush()

    def finish_download(self):
        if self.arrows:
            self.out.write("\n")
            self.out.flush()


class Progress:
    def __init__(self, ui, message):
        self.ui, self.message = ui, message
        self.started = 0

    def __enter__(self):
        self.started = time.monotonic()
        dots = "…" if self.ui.unicode else "..."
        print(f"  {self.ui._paint(dots, '36')} {self.message}", file=self.ui.out, flush=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.monotonic() - self.started
        if exc_type is None:
            self.ui.ok(f"{self.message} ({elapsed:.0f}s)")
        else:
            self.ui.error(f"{self.message} failed after {elapsed:.0f}s")
        return False
