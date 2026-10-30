"""Terminal frontend."""

import os
import shutil
import sys

from .zmachine import Host

try:
    import readline  # noqa: F401  (line editing and history for input())
except ImportError:
    pass


class TerminalUI:
    def __init__(self, width=None, color=None, show_status=True):
        self.interactive = sys.stdin.isatty()
        self.color = sys.stdout.isatty() if color is None else color
        self.width = width or max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        self.show_status = show_status
        self.partial = ""
        self.prompt = ""
        self.last_upper = None

    def _ansi(self, code, text):
        return "\x1b[%sm%s\x1b[0m" % (code, text) if self.color else text

    def _wrap(self, line):
        out = []
        width = self.width
        while len(line) > width:
            cut = line.rfind(" ", 0, width + 1)
            if cut <= 0:
                out.append(line[:width])
                line = line[width:]
            else:
                out.append(line[:cut])
                line = line[cut + 1:]
        out.append(line)
        return out

    def show(self, text):
        lines = (self.partial + text).split("\n")
        self.partial = lines.pop()
        for line in lines:
            sys.stdout.write("\n".join(self._wrap(line)) + "\n")
        sys.stdout.flush()

    def flush(self):
        if self.partial:
            sys.stdout.write(self.partial + "\n")
            self.partial = ""
        sys.stdout.flush()

    def notice(self, text):
        """Print an interpreter message. A pending prompt is kept for the next read."""
        for line in text.split("\n"):
            sys.stdout.write(self._ansi("2;36", line) + "\n")
        sys.stdout.flush()
        if not self.partial:
            self.partial = self.prompt

    def debug(self, text):
        self.notice(text)

    def narrate(self, text):
        """Show a companion answer, word-wrapped. A pending prompt is kept for the next read."""
        for para in text.strip().split("\n"):
            for line in self._wrap(para if self.color else "[frobai] " + para):
                sys.stdout.write(self._ansi("3;33", line) + "\n")
        sys.stdout.write("\n")
        sys.stdout.flush()
        if not self.partial:
            self.partial = self.prompt

    def command_echo(self, cmd):
        """Show a command the AI parser substituted, on the prompt line if one is pending."""
        sys.stdout.write(self.partial + self._ansi("2;36", "» " + cmd) + "\n")
        self.partial = ""
        sys.stdout.flush()

    def thinking(self, on):
        if not (self.color and sys.stdout.isatty()):
            return
        sys.stdout.write(self._ansi("2", "  …thinking") if on else "\r\x1b[K")
        sys.stdout.flush()

    def show_upper(self, machine):
        if not self.show_status:
            return
        scr = machine.screen
        if machine.version <= 3:
            if not scr.status:
                return
            left, right = scr.status
            gap = max(1, self.width - len(left) - len(right) - 2)
            rows = [" " + left + " " * gap + right + " "]
        else:
            rows = scr.upper_text()
        if rows == self.last_upper:
            return
        self.last_upper = rows
        if not rows:
            return
        lead = self.partial
        self.partial = ""
        for r in rows:
            sys.stdout.write(self._ansi("7", r[:self.width].ljust(self.width)) + "\n")
        self.partial = lead
        sys.stdout.flush()

    def read_line(self):
        self.prompt = self.partial
        self.partial = ""
        try:
            line = input(self.prompt)
        except EOFError:
            return None
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            return None
        if not self.interactive:
            sys.stdout.write(line + "\n")
        return line

    def read_key(self):
        self.flush()
        if not self.interactive:
            line = sys.stdin.readline()
            if not line:
                return None
            return ord(line[0]) if line.strip() else 13
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ch = os.read(fd, 8).decode(errors="ignore")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        arrows = {"\x1b[A": 129, "\x1b[B": 130, "\x1b[D": 131, "\x1b[C": 132}
        if ch in arrows:
            return arrows[ch]
        if not ch or ch in "\r\n":
            return 13
        if ch == "\x7f":
            return 8
        if ch == "\x1b":
            return 27
        return ord(ch[0]) if ord(ch[0]) < 256 else ord("?")


class TerminalHost(Host):
    """Save/restore via files named at a prompt."""

    def __init__(self, ui, story_path):
        self.ui = ui
        self.default = os.path.splitext(os.path.basename(story_path))[0] + ".qzl"

    def _ask(self, verb):
        self.ui.flush()
        try:
            name = input("%s file name [%s]: " % (verb, self.default)).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        name = name or self.default
        self.default = name
        return name

    def save_game(self, data):
        name = self._ask("Save")
        if not name:
            return False
        with open(name, "wb") as f:
            f.write(data)
        return True

    def restore_game(self):
        name = self._ask("Restore")
        if not name or not os.path.exists(name):
            return None
        with open(name, "rb") as f:
            return f.read()
