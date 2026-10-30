"""ZSCII text decoding/encoding and dictionary handling (Z-Machine Standard 1.1, sections 3 and 13)."""

A0 = "abcdefghijklmnopqrstuvwxyz"
A1 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Index 0 of A2 is z-char 6 (the ZSCII escape) and never printed directly.
A2_V1 = " 0123456789.,!?_#'\"/\\<-:()"
A2 = " \n0123456789.,!?_#'\"/\\-:()"

# ZSCII 155..223 default extra characters (Standard 3.8.5.3).
DEFAULT_UNICODE = "äöüÄÖÜß»«ëïÿËÏáéíóúýÁÉÍÓÚÝàèìòùÀÈÌÒÙâêîôûÂÊÎÔÛåÅøØãñõÃÑÕæÆçÇþðÞÐ£œŒ¡¿"


class ZText:
    def __init__(self, machine):
        self.m = machine
        mem = machine.mem
        v = self.version = machine.version
        self.abbrev_table = machine.word(0x18)

        self.unicode_table = DEFAULT_UNICODE
        ext = machine.word(0x36) if v >= 5 else 0
        if ext and machine.word(ext) >= 3:
            ut = machine.word(ext + 6)
            if ut:
                n = mem[ut]
                self.unicode_table = "".join(chr(machine.word(ut + 1 + 2 * i)) for i in range(n))

        alpha_addr = machine.word(0x34) if v >= 5 else 0
        if alpha_addr:
            rows = []
            for r in range(3):
                rows.append("".join(self.zscii_to_unicode(mem[alpha_addr + 26 * r + i]) for i in range(26)))
            a2 = list(rows[2])
            a2[0], a2[1] = " ", "\n"
            self.alphabets = [rows[0], rows[1], "".join(a2)]
        else:
            self.alphabets = [A0, A1, A2_V1 if v == 1 else A2]

        # Reverse map for encoding: unicode char -> (alphabet, z-char)
        self.reverse = {}
        for a, row in enumerate(self.alphabets):
            for i, ch in enumerate(row):
                if a == 2 and (i == 0 or (i == 1 and v >= 2)):
                    continue
                self.reverse.setdefault(ch, (a, i + 6))
        self._unicode_reverse = {ch: 155 + i for i, ch in enumerate(self.unicode_table)}

    # --- ZSCII <-> unicode ---------------------------------------------------------------
    def zscii_to_unicode(self, z):
        if 32 <= z <= 126:
            return chr(z)
        if z == 13 or z == 10:
            return "\n"
        if 155 <= z <= 251:
            i = z - 155
            return self.unicode_table[i] if i < len(self.unicode_table) else "?"
        if z == 9:
            return " "
        return ""

    def unicode_to_zscii(self, ch):
        o = ord(ch)
        if 32 <= o <= 126:
            return o
        if ch == "\n":
            return 13
        return self._unicode_reverse.get(ch, ord("?"))

    # --- decoding ------------------------------------------------------------------------
    def zchars_at(self, addr):
        mem = self.m.mem
        end = len(mem) - 1
        zs = []
        while addr < end:
            w = (mem[addr] << 8) | mem[addr + 1]
            addr += 2
            zs.append((w >> 10) & 31)
            zs.append((w >> 5) & 31)
            zs.append(w & 31)
            if w & 0x8000:
                break
        return zs, addr

    def decode(self, addr):
        """Decode the string at addr. Returns (text, address after the string)."""
        zs, nxt = self.zchars_at(addr)
        return self.decode_zchars(zs), nxt

    def decode_zchars(self, zs, allow_abbrev=True):
        v = self.version
        alphabets = self.alphabets
        out = []
        alpha = lock = 0
        i, n = 0, len(zs)
        while i < n:
            c = zs[i]
            i += 1
            if c == 0:
                out.append(" ")
                alpha = lock
                continue
            if v >= 3:
                if c <= 3:
                    if i < n and allow_abbrev:
                        out.append(self.abbreviation(32 * (c - 1) + zs[i]))
                    i += 1
                    alpha = 0
                    continue
                if c == 4:
                    alpha = 1
                    continue
                if c == 5:
                    alpha = 2
                    continue
            else:
                if c == 1:
                    if v == 1:
                        out.append("\n")
                    else:
                        if i < n and allow_abbrev:
                            out.append(self.abbreviation(zs[i]))
                        i += 1
                    alpha = lock
                    continue
                if c in (2, 3):
                    alpha = (lock + c - 1) % 3
                    continue
                if c in (4, 5):
                    lock = (lock + c - 3) % 3
                    alpha = lock
                    continue
            if alpha == 2 and c == 6:
                if i + 1 < n:
                    out.append(self.zscii_to_unicode((zs[i] << 5) | zs[i + 1]))
                i += 2
                alpha = lock
                continue
            out.append(alphabets[alpha][c - 6])
            alpha = lock
        return "".join(out)

    def abbreviation(self, index):
        m = self.m
        addr = m.word(self.abbrev_table + 2 * index) * 2
        zs, _ = self.zchars_at(addr)
        return self.decode_zchars(zs, allow_abbrev=False)

    # --- encoding ------------------------------------------------------------------------
    def encode_zscii(self, codes):
        """Encode a sequence of ZSCII codes as a dictionary word (6 or 9 z-chars)."""
        v = self.version
        limit = 6 if v <= 3 else 9
        shift = (None, 2, 3) if v <= 2 else (None, 4, 5)
        zs = []
        for z in codes:
            if len(zs) >= limit:
                break
            ch = self.zscii_to_unicode(z)
            hit = self.reverse.get(ch)
            if hit:
                a, c = hit
                if a:
                    zs.append(shift[a])
                zs.append(c)
            else:
                zs.extend((shift[2], 6, (z >> 5) & 31, z & 31))
        zs = zs[:limit]
        zs.extend([5] * (limit - len(zs)))
        out = bytearray()
        for k in range(0, limit, 3):
            w = (zs[k] << 10) | (zs[k + 1] << 5) | zs[k + 2]
            if k + 3 == limit:
                w |= 0x8000
            out += bytes(((w >> 8) & 0xFF, w & 0xFF))
        return bytes(out)

    def encode_word(self, word):
        return self.encode_zscii([self.unicode_to_zscii(ch) for ch in word.lower()])


class Dictionary:
    """A parsed Z-machine dictionary table."""

    def __init__(self, machine, addr):
        self.m = machine
        mem = machine.mem
        self.addr = addr
        nsep = mem[addr]
        self.separators = set(mem[addr + 1:addr + 1 + nsep])
        p = addr + 1 + nsep
        self.entry_length = mem[p]
        count = machine.word(p + 1)
        if count & 0x8000:
            count = 0x10000 - count
        self.count = count
        self.entries_start = p + 3
        self.key_length = 4 if machine.version <= 3 else 6
        self.index = {}
        for i in range(count):
            e = self.entries_start + i * self.entry_length
            self.index.setdefault(bytes(mem[e:e + self.key_length]), e)

    def lookup_zscii(self, codes):
        return self.index.get(self.m.text.encode_zscii(codes), 0)

    def lookup(self, word):
        return self.index.get(self.m.text.encode_word(word), 0)

    def entries(self):
        """Yield (address, decoded word, data bytes) for every entry."""
        mem = self.m.mem
        text = self.m.text
        for i in range(self.count):
            e = self.entries_start + i * self.entry_length
            zs, _ = text.zchars_at(e)
            word = text.decode_zchars(zs[: (6 if self.m.version <= 3 else 9)], allow_abbrev=False)
            yield e, word, bytes(mem[e + self.key_length:e + self.entry_length])


def split_words(codes, separators):
    """Split ZSCII codes into (start, length) word spans the way the Z-machine tokeniser does."""
    spans = []
    i, n = 0, len(codes)
    while i < n:
        c = codes[i]
        if c == 32:
            i += 1
        elif c in separators:
            spans.append((i, 1))
            i += 1
        else:
            j = i
            while j < n and codes[j] != 32 and codes[j] not in separators:
                j += 1
            spans.append((i, j - i))
            i = j
    return spans
