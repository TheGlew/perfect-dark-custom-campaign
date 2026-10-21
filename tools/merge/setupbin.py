"""The N64 setup file as the engine reads it (port/src/preprocess/filesetup.c):

  header: 8 u32 file offsets: waypoints, waygroups, cover, intro, props, paths, ailists, padfiledata
  props:  u32 words; each command's type is byte 3 of its first word; setupGetCmdLength gives sizes
  intro:  s32 words, terminated by INTROCMD_END (0x0c)
  paths:  { u32 ptr_pads; u8 id; u8 flags; u16 len } until ptr 0; each pad list is s32 until -1
  ailists:{ u32 ptr_list; s32 id } until ptr 0; each list is AI bytecode ending in the end command

This module parses such a file into plain Python structures, and writes one back, so an
assembled setup can be compared with the ROM's and merged setups can be emitted."""
import struct

INTROCMD_END = 0x0C
AICMD_END = 0x0004
# intro command -> length in words (src/game/playerreset.c)
INTRO_LENGTHS = {0x00: 3, 0x01: 4, 0x02: 4, 0x03: 8, 0x04: 2, 0x05: 2, 0x06: 10, 0x07: 3, 0x08: 2,
                 0x09: 3, 0x0A: 3, 0x0B: 2, 0x0C: 1}

# Length in u32 words of every prop command type, from src/game/setuputils.c (setupGetCmdLength).
# Filled lazily from the game source so it never drifts: see load_cmd_lengths().
_cmd_lengths = None


def load_cmd_lengths(src_root):
    """Read setupGetCmdLength's switch from the game source: a map objtype -> words."""
    global _cmd_lengths
    import re
    import os
    text = open(os.path.join(src_root, "src", "game", "setuputils.c"), encoding="utf-8", errors="replace").read()
    types = {}
    ctext = open(os.path.join(src_root, "src", "include", "constants.h"), encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"#define\s+(OBJTYPE_[A-Z0-9_]+)\s+(0x[0-9a-fA-F]+|\d+)", ctext):
        types[m.group(1)] = int(m.group(2), 0)
    sizes = {}
    # struct sizes in words: parse "case OBJTYPE_X: return sizeof(struct y) / sizeof(u32);"
    # The struct sizes come from the port's N64 layouts (filesetup.c objSizeN64): take them from there.
    ptext = open(os.path.join(src_root, "port", "src", "preprocess", "filesetup.c"), encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"case\s+(OBJTYPE_[A-Z0-9_]+):\s*return\s+sizeof\(struct\s+([a-z0-9_]+)\)\s*/\s*sizeof\(u32\);", ptext):
        sizes[types[m.group(1)]] = m.group(2)
    _cmd_lengths = (sizes, src_root)
    return sizes


class Setup:
    def __init__(self):
        self.props = []      # list of int words
        self.intro = []      # list of int words
        self.paths = []      # list of (id, flags, len, [pads])
        self.ailists = []    # list of (id, bytes)

    @classmethod
    def parse(cls, data: bytes) -> "Setup":
        s = cls()
        hdr = struct.unpack(">8I", data[:32])
        p_intro, p_props, p_paths, p_ail = hdr[3], hdr[4], hdr[5], hdr[6]
        # props run from p_props to the next section start; find the end by the endprops command (0x34)
        words = []
        off = p_props
        while True:
            w = struct.unpack(">I", data[off:off + 4])[0]
            words.append(w)
            off += 4
            if (w & 0xFF) == 0x34 and (w >> 8) == 0:
                break
        s.props = words
        # intro: walk by command length (playerreset.c), since a parameter may equal the end value
        off = p_intro
        intro = []
        while True:
            w = struct.unpack(">i", data[off:off + 4])[0]
            n = INTRO_LENGTHS.get(w, 1)
            intro += list(struct.unpack(">%di" % n, data[off:off + 4 * n]))
            off += 4 * n
            if w == INTROCMD_END:
                break
        s.intro = intro
        # paths
        off = p_paths
        while True:
            ptr, pid, flags, ln = struct.unpack(">IBBH", data[off:off + 8])
            if ptr == 0:
                break
            pads = []
            o = ptr
            while True:
                v = struct.unpack(">i", data[o:o + 4])[0]
                o += 4
                if v == -1:
                    break
                pads.append(v)
            s.paths.append((pid, flags, ln, pads))
            off += 8
        # ailists
        off = p_ail
        while True:
            ptr, lid = struct.unpack(">Ii", data[off:off + 8])
            if ptr == 0:
                break
            s.ailists.append((lid, ailist_bytes(data, ptr)))
            off += 8
        return s

    def write(self) -> bytes:
        """Serialise in the ROM's order: header, props, intro, paths table, pad lists, lists, ailists table."""
        out = bytearray(32)
        p_props = len(out)
        out += b"".join(struct.pack(">I", w & 0xFFFFFFFF) for w in self.props)
        p_intro = len(out)
        out += b"".join(struct.pack(">i", w if w < 0x80000000 else w - 0x100000000) for w in self.intro)
        # pad lists first so the paths table can point at them
        padlist_offsets = []
        padlists = bytearray()
        base = len(out) + 8 * (len(self.paths) + 1)
        for pid, flags, ln, pads in self.paths:
            padlist_offsets.append(base + len(padlists))
            padlists += b"".join(struct.pack(">i", p) for p in pads) + struct.pack(">i", -1)
        p_paths = len(out)
        for (pid, flags, ln, pads), o in zip(self.paths, padlist_offsets):
            out += struct.pack(">IBBH", o, pid, flags, ln)
        out += struct.pack(">IBBH", 0, 0, 0, 0)
        out += padlists
        while len(out) % 4:
            out += b"\0"
        # lists: identical byte strings share one copy
        list_offsets = {}
        entries = []
        for lid, body in self.ailists:
            key = bytes(body)
            if key not in list_offsets:
                list_offsets[key] = len(out)
                out += key
                while len(out) % 4:
                    out += b"\0"
            entries.append((list_offsets[key], lid))
        p_ail = len(out)
        for o, lid in entries:
            out += struct.pack(">Ii", o, lid)
        out += struct.pack(">Ii", 0, 0)
        while len(out) % 16:
            out += b"\0"
        out[:32] = struct.pack(">8I", 0, 0, 0, p_intro, p_props, p_paths, p_ail, 0)
        return bytes(out)


def ailist_bytes(data: bytes, ptr: int, lengths=None) -> bytes:
    """Walk AI bytecode from ptr to its end command using the command length table."""
    from . import aicmds
    off = ptr
    while True:
        t = (data[off] << 8) | data[off + 1]
        n = aicmds.command_length(data, off)
        off += n
        if t == AICMD_END:
            break
    return data[ptr:off]
