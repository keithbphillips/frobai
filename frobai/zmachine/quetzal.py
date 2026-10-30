"""Quetzal (IFZS) save file format, as used by Frotz and most other interpreters."""

import struct


class QuetzalError(Exception):
    pass


def _chunk(cid, data):
    out = cid + struct.pack(">I", len(data)) + data
    return out + b"\0" if len(data) & 1 else out


def save(machine, pc):
    """Serialise the machine; pc is the address of the save instruction's branch/store byte."""
    mem = machine.mem
    story = machine.story
    ifhd = bytes(mem[0x02:0x04]) + bytes(mem[0x12:0x18]) + bytes(mem[0x1C:0x1E]) + pc.to_bytes(3, "big")

    # CMem: XOR with the original story, zero runs compressed as 0, count-1.
    dyn = machine.static_base
    cmem = bytearray()
    zeros = 0
    for i in range(dyn):
        b = mem[i] ^ story[i]
        if b == 0:
            zeros += 1
            continue
        while zeros:
            n = min(zeros, 256)
            cmem += bytes((0, n - 1))
            zeros -= n
        cmem.append(b)

    stks = bytearray()
    for i, f in enumerate(machine.frames):
        if i == 0:
            flags, var, args = 0, 0, 0
        else:
            flags = len(f.locals) | (0x10 if f.store is None else 0)
            var = 0 if f.store is None else f.store
            args = (1 << f.argc) - 1
        stks += f.return_pc.to_bytes(3, "big") + bytes((flags, var, args & 0x7F))
        stks += struct.pack(">H", len(f.stack))
        for x in f.locals:
            stks += struct.pack(">H", x)
        for x in f.stack:
            stks += struct.pack(">H", x)

    body = b"IFZS" + _chunk(b"IFhd", ifhd) + _chunk(b"CMem", bytes(cmem)) + _chunk(b"Stks", bytes(stks))
    return b"FORM" + struct.pack(">I", len(body)) + body


def restore(machine, data):
    """Load a Quetzal file into the machine. Returns the saved PC. Raises QuetzalError."""
    from .machine import Frame

    if len(data) < 12 or data[:4] != b"FORM" or data[8:12] != b"IFZS":
        raise QuetzalError("not a Quetzal save file")
    chunks = {}
    p = 12
    end = min(len(data), 8 + struct.unpack(">I", data[4:8])[0])
    while p + 8 <= end:
        cid = data[p:p + 4]
        n = struct.unpack(">I", data[p + 4:p + 8])[0]
        chunks.setdefault(cid, data[p + 8:p + 8 + n])
        p += 8 + n + (n & 1)

    ifhd = chunks.get(b"IFhd")
    if not ifhd or len(ifhd) < 13:
        raise QuetzalError("missing IFhd chunk")
    mem = machine.mem
    story = machine.story
    if ifhd[0:2] != story[0x02:0x04] or ifhd[2:8] != story[0x12:0x18] or ifhd[8:10] != story[0x1C:0x1E]:
        raise QuetzalError("save file is for a different game")
    pc = int.from_bytes(ifhd[10:13], "big")

    dyn = machine.static_base
    if b"CMem" in chunks:
        newmem = bytearray(story[:dyn])
        i = 0
        src = chunks[b"CMem"]
        k = 0
        while k < len(src) and i < dyn:
            b = src[k]
            k += 1
            if b == 0:
                if k >= len(src):
                    raise QuetzalError("corrupt CMem chunk")
                i += src[k] + 1
                k += 1
            else:
                newmem[i] ^= b
                i += 1
    elif b"UMem" in chunks and len(chunks[b"UMem"]) == dyn:
        newmem = bytearray(chunks[b"UMem"])
    else:
        raise QuetzalError("missing memory chunk")

    stks = chunks.get(b"Stks")
    if stks is None:
        raise QuetzalError("missing Stks chunk")
    frames = []
    q = 0
    while q + 8 <= len(stks):
        ret = int.from_bytes(stks[q:q + 3], "big")
        flags, var, args = stks[q + 3], stks[q + 4], stks[q + 5]
        nstack = struct.unpack(">H", stks[q + 6:q + 8])[0]
        q += 8
        nloc = flags & 0x0F
        words = struct.unpack(">%dH" % (nloc + nstack), stks[q:q + 2 * (nloc + nstack)])
        q += 2 * (nloc + nstack)
        argc = 0
        while args & (1 << argc):
            argc += 1
        store = None if (flags & 0x10 or not frames) else var
        frames.append(Frame(ret, list(words[:nloc]), list(words[nloc:]), store, argc))
    if not frames:
        raise QuetzalError("empty stack")

    mem[:dyn] = newmem
    machine.frames = frames
    return pc
