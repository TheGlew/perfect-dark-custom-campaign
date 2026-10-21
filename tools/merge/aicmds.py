"""AI bytecode command lengths, read from the game's own table (src/game/chrai.c,
g_CommandLengths) so the tools never carry a copy that can drift."""
import os
import re

CMD_PRINT = 0x00B5  # dprint: variable length, NUL-terminated after the opcode (constants.h CMD_PRINT)
_lengths = None


def _src_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def lengths():
    global _lengths
    if _lengths is None:
        text = open(os.path.join(_src_root(), "src", "game", "chrai.c"), encoding="utf-8", errors="replace").read()
        start = text.index("g_CommandLengths[] = {")
        end = text.index("};", start)
        body = text[start:end]
        table = {}
        for m in re.finditer(r"/\*0x([0-9a-f]{4})\*/\s*([^,]+),", body):
            op = int(m.group(1), 16)
            expr = m.group(2).strip()
            if "?" in expr:
                # VERSION >= VERSION_NTSC_1_0 ? a : b -> the final build takes a
                expr = expr.split("?")[1].split(":")[0]
            table[op] = int(expr.strip())
        _lengths = table
    return _lengths


def command_length(data, off):
    t = (data[off] << 8) | data[off + 1]
    if t == CMD_PRINT:
        o = off + 2
        while data[o] != 0:
            o += 1
        return o - off + 1
    return lengths()[t]


def max_opcode():
    return max(lengths())
