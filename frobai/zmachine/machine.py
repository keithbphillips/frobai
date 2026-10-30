"""The Z-machine processor (versions 1-5, 7 and 8)."""

import random

from . import quetzal
from .screen import Screen
from .text import Dictionary, ZText, split_words

RUNNING, WAIT_LINE, WAIT_CHAR, HALTED = "running", "wait_line", "wait_char", "halted"


class ZMachineError(Exception):
    pass


class Frame:
    __slots__ = ("return_pc", "locals", "stack", "store", "argc")

    def __init__(self, return_pc, locals_, stack, store, argc):
        self.return_pc = return_pc
        self.locals = locals_
        self.stack = stack
        self.store = store        # variable number, or None to discard the result
        self.argc = argc

    def copy(self):
        return Frame(self.return_pc, list(self.locals), list(self.stack), self.store, self.argc)


class Host:
    """Callbacks the machine needs from its environment. Override in a frontend."""

    def save_game(self, data):
        return False

    def restore_game(self):
        return None


def s16(x):
    return x - 0x10000 if x & 0x8000 else x


class ZMachine:
    def __init__(self, story, host=None, width=80, seed=None):
        story = bytes(story)
        if len(story) < 64 or story[0] not in (1, 2, 3, 4, 5, 7, 8):
            raise ZMachineError("unsupported story file (version %d)" % (story[0] if story else 0))
        self.story = story
        self.version = story[0]
        self.host = host or Host()
        self.screen = Screen(width)
        self.rng = random.Random(seed)
        self.undo_stack = []
        self._build_dispatch()
        self.reset()

    # ================================================================== setup
    def reset(self, keep_flags2=None):
        self.mem = bytearray(self.story)
        if keep_flags2 is not None:
            self.mem[0x11] = (self.mem[0x11] & ~0x03) | (keep_flags2 & 0x03)
        v = self.version
        self.static_base = self.word(0x0E)
        self.globals = self.word(0x0C)
        self.obj_table = self.word(0x0A)
        self.text = ZText(self)
        self.dictionary = Dictionary(self, self.word(0x08))
        self._dict_cache = {}
        self.init_header()
        self.frames = [Frame(0, [], [], None, 0)]
        self.pc = self.word(0x06)
        self.state = RUNNING
        self.pending = None
        self.streams3 = []                 # stack of [table address, count]
        self.stream1 = True
        self.screen.reset()
        if v <= 3:
            self.obj_entry_size, self.obj_prop_off, self.obj_defaults = 9, 7, 31
        else:
            self.obj_entry_size, self.obj_prop_off, self.obj_defaults = 14, 12, 63

    def init_header(self):
        m = self.mem
        v = self.version
        if v <= 3:
            m[1] = (m[1] & ~0x50) | 0x20     # status line available, screen split available, fixed font
        else:
            m[1] = 0x1C                      # bold, italic, fixed-space; no colours/pictures/sound/timer
            m[0x1E] = 6
            m[0x1F] = ord("F")
            m[0x20] = 255
            m[0x21] = min(self.screen.width, 255)
        if v >= 5:
            self.set_word(0x22, self.screen.width)
            self.set_word(0x24, 255)
            m[0x26] = 1
            m[0x27] = 1
            m[0x2C] = 2
            m[0x2D] = 9
        m[0x11] &= ~(0x08 | 0x20 | 0x40 | 0x80)  # no pictures, mouse, colours, sound
        m[0x10] &= ~0x01                         # no menus
        m[0x32] = 1
        m[0x33] = 1

    # ================================================================== memory
    def word(self, a):
        m = self.mem
        return (m[a] << 8) | m[a + 1]

    def set_word(self, a, val):
        self.mem[a] = (val >> 8) & 0xFF
        self.mem[a + 1] = val & 0xFF

    def unpack_routine(self, p):
        v = self.version
        if v <= 3:
            return 2 * p
        if v <= 5:
            return 4 * p
        if v == 7:
            return 4 * p + 8 * self.word(0x28)
        return 8 * p

    def unpack_string(self, p):
        v = self.version
        if v <= 3:
            return 2 * p
        if v <= 5:
            return 4 * p
        if v == 7:
            return 4 * p + 8 * self.word(0x2A)
        return 8 * p

    # ================================================================== variables
    def read_var(self, n):
        if n == 0:
            st = self.frames[-1].stack
            if not st:
                raise ZMachineError("stack underflow")
            return st.pop()
        if n < 16:
            return self.frames[-1].locals[n - 1]
        a = self.globals + 2 * (n - 16)
        m = self.mem
        return (m[a] << 8) | m[a + 1]

    def write_var(self, n, val):
        val &= 0xFFFF
        if n == 0:
            self.frames[-1].stack.append(val)
        elif n < 16:
            self.frames[-1].locals[n - 1] = val
        else:
            a = self.globals + 2 * (n - 16)
            self.mem[a] = val >> 8
            self.mem[a + 1] = val & 0xFF

    def peek_var(self, n):
        """Indirect variable read: variable 0 reads the top of stack without popping."""
        if n == 0:
            st = self.frames[-1].stack
            if not st:
                raise ZMachineError("stack underflow")
            return st[-1]
        return self.read_var(n)

    def poke_var(self, n, val):
        """Indirect variable write: variable 0 replaces the top of stack."""
        if n == 0:
            st = self.frames[-1].stack
            if not st:
                raise ZMachineError("stack underflow")
            st[-1] = val & 0xFFFF
        else:
            self.write_var(n, val)

    def store(self, val):
        var = self.mem[self.pc]
        self.pc += 1
        self.write_var(var, val)

    def branch(self, cond):
        m = self.mem
        pc = self.pc
        b = m[pc]
        pc += 1
        if b & 0x40:
            off = b & 0x3F
        else:
            off = ((b & 0x3F) << 8) | m[pc]
            pc += 1
            if off & 0x2000:
                off -= 0x4000
        self.pc = pc
        if bool(cond) == bool(b & 0x80):
            if off == 0 or off == 1:
                self.ret(off)
            else:
                self.pc = pc + off - 2

    # ================================================================== routines
    def call(self, packed, args, store):
        if packed == 0:
            if store is not None:
                self.write_var(store, 0)
            return
        addr = self.unpack_routine(packed)
        m = self.mem
        n = m[addr]
        addr += 1
        if n > 15:
            raise ZMachineError("bad routine header at %05x" % (addr - 1))
        if self.version <= 4:
            locals_ = [(m[addr + 2 * i] << 8) | m[addr + 2 * i + 1] for i in range(n)]
            addr += 2 * n
        else:
            locals_ = [0] * n
        for i in range(min(n, len(args))):
            locals_[i] = args[i]
        self.frames.append(Frame(self.pc, locals_, [], store, len(args)))
        self.pc = addr

    def ret(self, val):
        if len(self.frames) <= 1:
            raise ZMachineError("return from main routine")
        f = self.frames.pop()
        self.pc = f.return_pc
        if f.store is not None:
            self.write_var(f.store, val)

    def read_store_var(self):
        var = self.mem[self.pc]
        self.pc += 1
        return var

    # ================================================================== objects
    def obj_addr(self, n):
        return self.obj_table + 2 * self.obj_defaults + (n - 1) * self.obj_entry_size

    def parent(self, o):
        a = self.obj_addr(o)
        return self.mem[a + 4] if self.version <= 3 else self.word(a + 6)

    def sibling(self, o):
        a = self.obj_addr(o)
        return self.mem[a + 5] if self.version <= 3 else self.word(a + 8)

    def child(self, o):
        a = self.obj_addr(o)
        return self.mem[a + 6] if self.version <= 3 else self.word(a + 10)

    def _set_rel(self, o, which, val):
        a = self.obj_addr(o)
        if self.version <= 3:
            self.mem[a + 4 + which] = val
        else:
            self.set_word(a + 6 + 2 * which, val)

    def set_parent(self, o, v):
        self._set_rel(o, 0, v)

    def set_sibling(self, o, v):
        self._set_rel(o, 1, v)

    def set_child(self, o, v):
        self._set_rel(o, 2, v)

    def remove_obj(self, o):
        p = self.parent(o)
        if p == 0:
            return
        if self.child(p) == o:
            self.set_child(p, self.sibling(o))
        else:
            c = self.child(p)
            guard = 0
            while c and guard < 65536:
                s = self.sibling(c)
                if s == o:
                    self.set_sibling(c, self.sibling(o))
                    break
                c = s
                guard += 1
        self.set_parent(o, 0)
        self.set_sibling(o, 0)

    def insert_obj(self, o, dest):
        self.remove_obj(o)
        self.set_sibling(o, self.child(dest))
        self.set_child(dest, o)
        self.set_parent(o, dest)

    def prop_table(self, o):
        return self.word(self.obj_addr(o) + self.obj_prop_off)

    def obj_name(self, o):
        if o == 0:
            return ""
        p = self.prop_table(o)
        if self.mem[p] == 0:
            return ""
        return self.text.decode(p + 1)[0]

    def prop_header(self, p):
        """Return (number, size, data address) of the property whose size byte is at p."""
        m = self.mem
        b = m[p]
        if self.version <= 3:
            return b & 31, (b >> 5) + 1, p + 1
        num = b & 63
        if b & 0x80:
            s = m[p + 1] & 63
            return num, s or 64, p + 2
        return num, 2 if b & 0x40 else 1, p + 1

    def first_prop(self, o):
        p = self.prop_table(o)
        return p + 1 + 2 * self.mem[p]

    def find_prop(self, o, prop):
        p = self.first_prop(o)
        while True:
            num, size, data = self.prop_header(p)
            if num == 0:
                return 0, 0
            if num == prop:
                return data, size
            p = data + size

    def prop_len(self, data):
        if data == 0:
            return 0
        b = self.mem[data - 1]
        if self.version <= 3:
            return (b >> 5) + 1
        if b & 0x80:
            return (b & 63) or 64
        return 2 if b & 0x40 else 1

    def test_attr(self, o, attr):
        return self.mem[self.obj_addr(o) + attr // 8] & (0x80 >> (attr % 8))

    # ================================================================== output
    def output(self, s):
        if not s:
            return
        if self.streams3:
            t = self.streams3[-1]
            m = self.mem
            for ch in s:
                m[t[0] + 2 + t[1]] = self.text.unicode_to_zscii(ch)
                t[1] += 1
            return
        if self.stream1:
            self.screen.print(s)

    # ================================================================== execution
    def run(self, max_steps=None):
        """Execute until the game waits for input or halts. Returns the new state."""
        if self.state in (WAIT_LINE, WAIT_CHAR, HALTED):
            return self.state
        self.state = RUNNING
        step = self.step
        try:
            if max_steps is None:
                while self.state == RUNNING:
                    step()
            else:
                for _ in range(max_steps):
                    if self.state != RUNNING:
                        break
                    step()
        except ZMachineError as e:
            raise ZMachineError("%s (at pc %05x)" % (e, self.op_pc)) from None
        except IndexError:
            raise ZMachineError("memory access out of range (at pc %05x)" % self.op_pc) from None
        return self.state

    def step(self):
        m = self.mem
        pc = self.op_pc = self.pc
        b = m[pc]
        pc += 1
        if b < 0x80:
            x = m[pc]
            y = m[pc + 1]
            self.pc = pc + 2
            if b & 0x40:
                x = self.read_var(x)
            if b & 0x20:
                y = self.read_var(y)
            self.op2[b & 0x1F](x, y)
            return
        if b < 0xC0 and not (b == 0xBE and self.version >= 5):
            t = (b >> 4) & 3
            if t == 3:
                self.pc = pc
                self.op0[b & 0x0F]()
                return
            if t == 0:
                x = (m[pc] << 8) | m[pc + 1]
                pc += 2
            elif t == 1:
                x = m[pc]
                pc += 1
            else:
                x = self.read_var(m[pc])
                pc += 1
            self.pc = pc
            self.op1[b & 0x0F](x)
            return
        if b == 0xBE and self.version >= 5:
            table = self.opext
            opnum = m[pc]
            pc += 1
        elif b < 0xE0:
            table = self.op2
            opnum = b & 0x1F
        else:
            table = self.opvar
            opnum = b & 0x1F
        types = m[pc]
        pc += 1
        if b == 0xEC or b == 0xFA:
            types = (types << 8) | m[pc]
            pc += 1
            shifts = (14, 12, 10, 8, 6, 4, 2, 0)
        else:
            shifts = (6, 4, 2, 0)
        args = []
        for sh in shifts:
            t = (types >> sh) & 3
            if t == 3:
                break
            if t == 0:
                args.append((m[pc] << 8) | m[pc + 1])
                pc += 2
            elif t == 1:
                args.append(m[pc])
                pc += 1
            else:
                args.append(self.read_var(m[pc]))
                pc += 1
        self.pc = pc
        handler = table.get(opnum) if isinstance(table, dict) else table[opnum]
        if handler is None:
            raise ZMachineError("illegal opcode %02x/%d" % (b, opnum))
        handler(*args)

    # ================================================================== input
    def provide_line(self, line):
        """Complete a pending line read with the given text."""
        if self.state != WAIT_LINE:
            raise ZMachineError("machine is not waiting for a line")
        text_addr, parse_addr, store_var = self.pending
        m = self.mem
        v = self.version
        maxlen = m[text_addr] - (1 if v <= 4 else 0)
        codes = [self.text.unicode_to_zscii(ch) for ch in line.lower() if ch != "\n"]
        codes = [c for c in codes if 32 <= c <= 126 or 155 <= c <= 251][:max(0, maxlen)]
        if v >= 5:
            m[text_addr + 1] = len(codes)
            m[text_addr + 2:text_addr + 2 + len(codes)] = bytes(codes)
        else:
            m[text_addr + 1:text_addr + 1 + len(codes)] = bytes(codes)
            m[text_addr + 1 + len(codes)] = 0
        if parse_addr:
            self.tokenise(text_addr, parse_addr)
        self.pending = None
        self.state = RUNNING
        if v >= 5:
            self.write_var(store_var, 13)

    def provide_char(self, zscii):
        if self.state != WAIT_CHAR:
            raise ZMachineError("machine is not waiting for a key")
        store_var = self.pending
        self.pending = None
        self.state = RUNNING
        self.write_var(store_var, zscii)

    def get_dictionary(self, addr):
        if addr == 0 or addr == self.dictionary.addr:
            return self.dictionary
        return Dictionary(self, addr)

    def tokenise(self, text_addr, parse_addr, dict_addr=0, skip_unknown=False):
        m = self.mem
        if self.version >= 5:
            n = m[text_addr + 1]
            start = text_addr + 2
        else:
            start = text_addr + 1
            n = 0
            while start + n < len(m) and m[start + n] != 0:
                n += 1
        codes = list(m[start:start + n])
        d = self.get_dictionary(dict_addr)
        spans = split_words(codes, d.separators)[:m[parse_addr]]
        m[parse_addr + 1] = len(spans)
        for k, (pos, length) in enumerate(spans):
            addr = d.lookup_zscii(codes[pos:pos + length])
            if skip_unknown and addr == 0:
                continue
            e = parse_addr + 2 + 4 * k
            self.set_word(e, addr)
            m[e + 2] = length
            m[e + 3] = pos + (start - text_addr)

    def lookup_words(self, line):
        """Tokenise a line against the main dictionary: list of (word, dictionary address)."""
        codes = [self.text.unicode_to_zscii(ch) for ch in line.lower()]
        out = []
        for pos, length in split_words(codes, self.dictionary.separators):
            w = codes[pos:pos + length]
            out.append(("".join(map(chr, w)), self.dictionary.lookup_zscii(w)))
        return out

    # ================================================================== snapshots
    def snapshot(self):
        return (bytes(self.mem[:self.static_base]), [f.copy() for f in self.frames], self.pc,
                self.state, self.pending, self.screen.snapshot(), [list(t) for t in self.streams3],
                self.stream1, self.rng.getstate())

    def restore_snapshot(self, snap):
        (dyn, frames, self.pc, self.state, self.pending, scr, streams3, self.stream1, rng) = snap
        self.mem[:len(dyn)] = dyn
        self.frames = [f.copy() for f in frames]
        self.screen.restore(scr)
        self.streams3 = [list(t) for t in streams3]
        self.rng.setstate(rng)

    # ================================================================== status line
    def update_status(self):
        if self.version > 3:
            return
        loc = self.obj_name(self.word(self.globals))
        a = s16(self.word(self.globals + 2))
        b = self.word(self.globals + 4)
        if self.version == 3 and self.mem[1] & 0x02:
            right = "Time: %d:%02d %s" % ((a % 12) or 12, b, "AM" if a < 12 else "PM")
        else:
            right = "Score: %d  Moves: %d" % (a, b)
        self.screen.status = (loc, right)

    # ================================================================== opcodes
    def _build_dispatch(self):
        v = self.version
        self.op2 = [None] * 32
        for i, name in enumerate(
                [None, "je", "jl", "jg", "dec_chk", "inc_chk", "jin", "test", "or", "and", "test_attr",
                 "set_attr", "clear_attr", "store", "insert_obj", "loadw", "loadb", "get_prop",
                 "get_prop_addr", "get_next_prop", "add", "sub", "mul", "div", "mod", "call_2s",
                 "call_2n", "set_colour", "throw"]):
            if name:
                self.op2[i] = getattr(self, "op_" + name)
        self.op1 = [getattr(self, "op_" + n) for n in (
            "jz", "get_sibling", "get_child", "get_parent", "get_prop_len", "inc", "dec", "print_addr",
            "call_1s", "remove_obj", "print_obj", "ret", "jump", "print_paddr", "load",
            "not" if v <= 4 else "call_1n")]
        self.op0 = [getattr(self, "op_" + n) for n in (
            "rtrue", "rfalse", "print", "print_ret", "nop", "save", "restore", "restart", "ret_popped",
            "pop" if v <= 4 else "catch", "quit", "new_line", "show_status", "verify", "nop", "piracy")]
        self.opvar = [None] * 32
        for i, name in enumerate(
                ["call_vs", "storew", "storeb", "put_prop", "read", "print_char", "print_num", "random",
                 "push", "pull", "split_window", "set_window", "call_vs2", "erase_window", "erase_line",
                 "set_cursor", "get_cursor", "set_text_style", "buffer_mode", "output_stream",
                 "input_stream", "sound_effect", "read_char", "scan_table", "not", "call_vn", "call_vn2",
                 "tokenise", "encode_text", "copy_table", "print_table", "check_arg_count"]):
            self.opvar[i] = getattr(self, "op_" + name)
        self.opext = {0: self.op_ext_save, 1: self.op_ext_restore, 2: self.op_log_shift,
                      3: self.op_art_shift, 4: self.op_set_font, 9: self.op_save_undo,
                      10: self.op_restore_undo, 11: self.op_print_unicode, 12: self.op_check_unicode,
                      13: self.op_nop_args}

    # --- 2OP
    def op_je(self, a, *rest):
        self.branch(a in rest)

    def op_jl(self, a, b):
        self.branch(s16(a) < s16(b))

    def op_jg(self, a, b):
        self.branch(s16(a) > s16(b))

    def op_dec_chk(self, var, val):
        x = s16((self.peek_var(var) - 1) & 0xFFFF)
        self.poke_var(var, x)
        self.branch(x < s16(val))

    def op_inc_chk(self, var, val):
        x = (s16(self.peek_var(var)) + 1)
        if x > 32767:
            x -= 65536
        self.poke_var(var, x)
        self.branch(x > s16(val))

    def op_jin(self, a, b):
        self.branch(a != 0 and self.parent(a) == b)

    def op_test(self, bitmap, flags):
        self.branch(bitmap & flags == flags)

    def op_or(self, a, b):
        self.store(a | b)

    def op_and(self, a, b):
        self.store(a & b)

    def op_test_attr(self, o, attr):
        self.branch(o != 0 and self.test_attr(o, attr))

    def op_set_attr(self, o, attr):
        if o:
            self.mem[self.obj_addr(o) + attr // 8] |= 0x80 >> (attr % 8)

    def op_clear_attr(self, o, attr):
        if o:
            self.mem[self.obj_addr(o) + attr // 8] &= ~(0x80 >> (attr % 8)) & 0xFF

    def op_store(self, var, val):
        self.poke_var(var, val)

    def op_insert_obj(self, o, dest):
        if o and dest:
            self.insert_obj(o, dest)

    def op_loadw(self, arr, idx):
        self.store(self.word((arr + 2 * idx) & 0xFFFF))

    def op_loadb(self, arr, idx):
        self.store(self.mem[(arr + idx) & 0xFFFF])

    def op_get_prop(self, o, prop):
        if o == 0:
            self.store(0)
            return
        data, size = self.find_prop(o, prop)
        if data == 0:
            self.store(self.word(self.obj_table + 2 * (prop - 1)))
        elif size == 1:
            self.store(self.mem[data])
        else:
            self.store(self.word(data))

    def op_get_prop_addr(self, o, prop):
        self.store(self.find_prop(o, prop)[0] if o else 0)

    def op_get_next_prop(self, o, prop):
        if o == 0:
            self.store(0)
            return
        if prop == 0:
            p = self.first_prop(o)
        else:
            data, size = self.find_prop(o, prop)
            if data == 0:
                raise ZMachineError("get_next_prop: object %d has no property %d" % (o, prop))
            p = data + size
        self.store(self.prop_header(p)[0])

    def op_add(self, a, b):
        self.store(a + b)

    def op_sub(self, a, b):
        self.store(a - b)

    def op_mul(self, a, b):
        self.store(a * b)

    def op_div(self, a, b):
        a, b = s16(a), s16(b)
        if b == 0:
            raise ZMachineError("division by zero")
        q = abs(a) // abs(b)
        self.store(-q if (a < 0) != (b < 0) else q)

    def op_mod(self, a, b):
        a, b = s16(a), s16(b)
        if b == 0:
            raise ZMachineError("modulo by zero")
        q = abs(a) // abs(b)
        q = -q if (a < 0) != (b < 0) else q
        self.store(a - b * q)

    def op_call_2s(self, r, a):
        self.call(r, (a,), self.read_store_var())

    def op_call_2n(self, r, a):
        self.call(r, (a,), None)

    def op_set_colour(self, *args):
        pass

    def op_throw(self, val, frame):
        del self.frames[frame:]
        self.ret(val)

    # --- 1OP
    def op_jz(self, a):
        self.branch(a == 0)

    def op_get_sibling(self, o):
        x = self.sibling(o) if o else 0
        self.store(x)
        self.branch(x)

    def op_get_child(self, o):
        x = self.child(o) if o else 0
        self.store(x)
        self.branch(x)

    def op_get_parent(self, o):
        self.store(self.parent(o) if o else 0)

    def op_get_prop_len(self, data):
        self.store(self.prop_len(data))

    def op_inc(self, var):
        self.poke_var(var, self.peek_var(var) + 1)

    def op_dec(self, var):
        self.poke_var(var, self.peek_var(var) - 1)

    def op_print_addr(self, addr):
        self.output(self.text.decode(addr)[0])

    def op_call_1s(self, r):
        self.call(r, (), self.read_store_var())

    def op_call_1n(self, r):
        self.call(r, (), None)

    def op_remove_obj(self, o):
        if o:
            self.remove_obj(o)

    def op_print_obj(self, o):
        self.output(self.obj_name(o))

    def op_ret(self, val):
        self.ret(val)

    def op_jump(self, off):
        self.pc += s16(off) - 2

    def op_print_paddr(self, p):
        self.output(self.text.decode(self.unpack_string(p))[0])

    def op_load(self, var):
        self.store(self.peek_var(var))

    def op_not(self, a):
        self.store(~a & 0xFFFF)

    # --- 0OP
    def op_rtrue(self):
        self.ret(1)

    def op_rfalse(self):
        self.ret(0)

    def op_print(self):
        s, self.pc = self.text.decode(self.pc)
        self.output(s)

    def op_print_ret(self):
        s, self.pc = self.text.decode(self.pc)
        self.output(s + "\n")
        self.ret(1)

    def op_nop(self):
        pass

    def op_nop_args(self, *args):
        pass

    def _save(self):
        data = quetzal.save(self, self.pc)
        try:
            return bool(self.host.save_game(data))
        except Exception:
            return False

    def _restore(self):
        """Attempt a restore. On success the pc points at the save's branch/store byte."""
        try:
            data = self.host.restore_game()
        except Exception:
            data = None
        if not data:
            return False
        flags2 = self.mem[0x11]
        backup = self.snapshot()
        try:
            self.pc = quetzal.restore(self, data)
        except quetzal.QuetzalError:
            self.restore_snapshot(backup)
            return False
        self.mem[0x11] = (self.mem[0x11] & ~0x03) | (flags2 & 0x03)
        self.init_header()
        return True

    def op_save(self):
        ok = self._save()
        if self.version <= 3:
            self.branch(ok)
        else:
            self.store(1 if ok else 0)

    def op_restore(self):
        if self._restore():
            if self.version <= 3:
                self.branch(True)
            else:
                self.store(2)
        elif self.version <= 3:
            self.branch(False)
        else:
            self.store(0)

    def op_ext_save(self, *args):
        if args:
            self.store(0)
        else:
            self.store(1 if self._save() else 0)

    def op_ext_restore(self, *args):
        if not args and self._restore():
            self.store(2)
        else:
            self.store(0)

    def op_restart(self):
        flags2 = self.mem[0x11]
        width = self.screen.width
        self.reset(keep_flags2=flags2)
        self.screen.width = width

    def op_ret_popped(self):
        self.ret(self.read_var(0))

    def op_pop(self):
        self.read_var(0)

    def op_catch(self):
        self.store(len(self.frames))

    def op_quit(self):
        self.state = HALTED

    def op_new_line(self):
        self.output("\n")

    def op_show_status(self):
        self.update_status()

    def op_verify(self):
        v = self.version
        length = self.word(0x1A) * (2 if v <= 3 else 4 if v <= 5 else 8)
        length = min(length or len(self.story), len(self.story))
        self.branch(sum(self.story[0x40:length]) & 0xFFFF == self.word(0x1C))

    def op_piracy(self):
        self.branch(True)

    # --- VAR
    def op_call_vs(self, r, *args):
        self.call(r, args, self.read_store_var())

    op_call_vs2 = op_call_vs

    def op_call_vn(self, r, *args):
        self.call(r, args, None)

    op_call_vn2 = op_call_vn

    def op_storew(self, arr, idx, val):
        self.set_word((arr + 2 * idx) & 0xFFFF, val)

    def op_storeb(self, arr, idx, val):
        self.mem[(arr + idx) & 0xFFFF] = val & 0xFF

    def op_put_prop(self, o, prop, val):
        data, size = self.find_prop(o, prop)
        if data == 0:
            raise ZMachineError("put_prop: object %d has no property %d" % (o, prop))
        if size == 1:
            self.mem[data] = val & 0xFF
        else:
            self.set_word(data, val)

    def op_read(self, text, parse=0, *_timed):
        if self.version <= 3:
            self.update_status()
        store_var = self.read_store_var() if self.version >= 5 else None
        self.pending = (text, parse, store_var)
        self.state = WAIT_LINE

    def op_print_char(self, z):
        self.output(self.text.zscii_to_unicode(z))

    def op_print_num(self, n):
        self.output(str(s16(n)))

    def op_random(self, r):
        r = s16(r)
        if r > 0:
            self.store(self.rng.randint(1, r))
        else:
            self.rng.seed(-r if r < 0 else None)
            self.store(0)

    def op_push(self, val):
        self.write_var(0, val)

    def op_pull(self, var):
        self.poke_var(var, self.read_var(0))

    def op_split_window(self, lines):
        self.screen.split(lines, self.version)

    def op_set_window(self, w):
        self.screen.set_window(w, self.version)

    def op_erase_window(self, w):
        self.screen.erase_window(s16(w), self.version)

    def op_erase_line(self, val=1):
        if val == 1:
            self.screen.erase_line()

    def op_set_cursor(self, row, col=1, *_):
        self.screen.set_cursor(s16(row), s16(col))

    def op_get_cursor(self, arr):
        self.set_word(arr, self.screen.row)
        self.set_word(arr + 2, self.screen.col)

    def op_set_text_style(self, style):
        self.screen.style = style

    def op_buffer_mode(self, flag):
        pass

    def op_output_stream(self, n, table=0, *_):
        n = s16(n)
        if n == 1:
            self.stream1 = True
        elif n == -1:
            self.stream1 = False
        elif n == 2:
            self.mem[0x11] |= 0x01
        elif n == -2:
            self.mem[0x11] &= ~0x01
        elif n == 3:
            if len(self.streams3) >= 16:
                raise ZMachineError("output stream 3 nested too deeply")
            self.streams3.append([table, 0])
        elif n == -3:
            if self.streams3:
                t = self.streams3.pop()
                self.set_word(t[0], t[1])

    def op_input_stream(self, n):
        pass

    def op_sound_effect(self, *args):
        pass

    def op_read_char(self, *args):
        self.pending = self.read_store_var()
        self.state = WAIT_CHAR

    def op_scan_table(self, x, table, length, form=0x82):
        size = form & 0x7F
        words = form & 0x80
        m = self.mem
        for i in range(length):
            a = table + i * size
            val = ((m[a] << 8) | m[a + 1]) if words else m[a]
            if val == x:
                self.store(a)
                self.branch(True)
                return
        self.store(0)
        self.branch(False)

    def op_tokenise(self, text, parse, dictionary=0, flag=0):
        self.tokenise(text, parse, dictionary, bool(flag))

    def op_encode_text(self, zscii, length, start, coded):
        data = self.text.encode_zscii(list(self.mem[zscii + start:zscii + start + length]))
        self.mem[coded:coded + len(data)] = data

    def op_copy_table(self, first, second, size):
        m = self.mem
        size = s16(size)
        if second == 0:
            m[first:first + abs(size)] = bytes(abs(size))
        elif size < 0:
            for i in range(-size):
                m[second + i] = m[first + i]
        else:
            m[second:second + size] = bytes(m[first:first + size])

    def op_print_table(self, table, width, height=1, skip=0):
        scr = self.screen
        col = scr.col
        for r in range(height):
            if r:
                if scr.window == 1:
                    scr.set_cursor(scr.row + 1, col)
                else:
                    self.output("\n")
            base = table + r * (width + skip)
            self.output("".join(self.text.zscii_to_unicode(c) for c in self.mem[base:base + width]))

    def op_check_arg_count(self, n):
        self.branch(n <= self.frames[-1].argc)

    # --- EXT
    def op_log_shift(self, n, places):
        p = s16(places)
        self.store((n << p) if p >= 0 else (n >> -p))

    def op_art_shift(self, n, places):
        p = s16(places)
        self.store((s16(n) << p) if p >= 0 else (s16(n) >> -p))

    def op_set_font(self, font, *_):
        scr = self.screen
        if font == 0:
            self.store(scr.font)
        elif font in (1, 4):
            self.store(scr.font)
            scr.font = font
        else:
            self.store(0)

    def op_save_undo(self, *args):
        var = self.read_store_var()
        self.undo_stack.append((self.snapshot(), var))
        del self.undo_stack[:-20]
        self.write_var(var, 1)

    def op_restore_undo(self, *args):
        var = self.read_store_var()
        if not self.undo_stack:
            self.write_var(var, 0)
            return
        snap, saved_var = self.undo_stack.pop()
        self.restore_snapshot(snap)
        self.write_var(saved_var, 2)

    def op_print_unicode(self, c):
        self.output(chr(c))

    def op_check_unicode(self, c):
        self.store(3 if 32 <= c <= 0xFFFF else 0)
