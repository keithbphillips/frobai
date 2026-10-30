"""A frontend-neutral screen model: a scrolling lower window plus a character-grid upper window."""


class Screen:
    def __init__(self, width=80):
        self.width = width
        self.reset()

    def reset(self):
        self.lower = []          # pending lower-window text not yet consumed by the frontend
        self.upper = []          # rows of characters
        self.window = 0
        self.row = 1
        self.col = 1
        self.status = None       # v1-3 status line: (location, right-hand text)
        self.style = 0
        self.font = 1

    # --- output ---------------------------------------------------------------------------
    def print(self, text):
        if self.window == 0:
            self.lower.append(text)
            return
        for ch in text:
            if ch == "\n":
                self.row += 1
                self.col = 1
                continue
            if 1 <= self.row <= len(self.upper) and 1 <= self.col <= self.width:
                self.upper[self.row - 1][self.col - 1] = ch
            self.col += 1

    def take_output(self):
        text = "".join(self.lower)
        self.lower = []
        return text

    def upper_text(self):
        """Non-blank rows of the upper window, trailing blanks stripped."""
        rows = ["".join(r).rstrip() for r in self.upper]
        while rows and not rows[-1]:
            rows.pop()
        return rows if any(rows) else []

    # --- window management ---------------------------------------------------------------
    def split(self, lines, version):
        lines = max(0, lines)
        if version == 3:
            self.upper = [[" "] * self.width for _ in range(lines)]
        else:
            self.upper = self.upper[:lines] + [[" "] * self.width for _ in range(lines - len(self.upper))]
        if self.row > lines:
            self.row, self.col = 1, 1

    def set_window(self, w, version):
        self.window = 1 if w == 1 else 0
        if self.window == 1 and version >= 4:
            self.row, self.col = 1, 1

    def erase_window(self, w, version):
        if w == -1:
            self.upper = []
            self.window = 0
        elif w == -2 or w == 1:
            self.upper = [[" "] * self.width for _ in self.upper]
        if w in (-1, -2, 1):
            self.row, self.col = 1, 1

    def erase_line(self):
        if self.window == 1 and 1 <= self.row <= len(self.upper):
            r = self.upper[self.row - 1]
            for c in range(self.col - 1, self.width):
                r[c] = " "

    def set_cursor(self, row, col):
        if self.window == 1:
            self.row, self.col = row, col

    # --- snapshots for speculative execution / undo ------------------------------------------
    def snapshot(self):
        return (list(self.lower), [list(r) for r in self.upper], self.window, self.row, self.col,
                self.status, self.style, self.font)

    def restore(self, snap):
        lower, upper, self.window, self.row, self.col, self.status, self.style, self.font = snap
        self.lower = list(lower)
        self.upper = [list(r) for r in upper]
