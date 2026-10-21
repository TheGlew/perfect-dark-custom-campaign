"""Assemble stock setups from source and compare them, structure by structure, with the
files extracted from the ROM."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import setupasm, setupbin  # noqa: E402

ROOT = setupasm.ROOT


def compare(name):
    src = os.path.join(ROOT, "src", "setups", "setup%s.c" % name)
    rom = os.path.join(ROOT, "extracted", "ntsc-final", "files", "setup", "setup%s.bin" % name)
    # the level's own pads header, not every header in the folder: a merged mission's
    # (dd.h) names the same pads at other indices
    symbols = [os.path.join(setupasm.GENERATED, "lang", f) for f in os.listdir(os.path.join(setupasm.GENERATED, "lang")) if f.endswith(".h")]
    symbols += [p for p in (os.path.join(setupasm.GENERATED, "pads", name + ".h"),
                            os.path.join(ROOT, "src", "generated", "ntsc-final", "pads", name + ".h")) if os.path.exists(p)]
    a = setupasm.assemble_file(src, symbol_paths=symbols)
    b = setupbin.Setup.parse(open(rom, "rb").read())
    problems = []
    if a.props != b.props:
        n = next((i for i, (x, y) in enumerate(zip(a.props, b.props)) if x != y), min(len(a.props), len(b.props)))
        problems.append("props differ at word %d (%d vs %d words): %s vs %s" % (n, len(a.props), len(b.props), hex(a.props[n]) if n < len(a.props) else "-", hex(b.props[n]) if n < len(b.props) else "-"))
    if a.intro != b.intro:
        problems.append("intro differs (%d vs %d words)" % (len(a.intro), len(b.intro)))
    if a.paths != b.paths:
        problems.append("paths differ (%d vs %d)" % (len(a.paths), len(b.paths)))
    da = dict(a.ailists)
    db = dict(b.ailists)
    if set(da) != set(db):
        problems.append("ailist ids differ: only in source %s, only in rom %s" % (sorted(map(hex, set(da) - set(db)))[:5], sorted(map(hex, set(db) - set(da)))[:5]))
    else:
        bad = [hex(k) for k in da if da[k] != db[k]]
        if bad:
            problems.append("ailist bodies differ: %s" % bad[:8])
    # the written file parses back to the same thing
    c = setupbin.Setup.parse(a.write())
    if (c.props, c.intro, c.paths, dict(c.ailists)) != (a.props, a.intro, a.paths, da):
        problems.append("write/parse round trip differs")
    return problems


if __name__ == "__main__":
    names = sys.argv[1:] or ["lue", "lip", "tra", "ame", "ark", "ear", "pete", "depo"]
    bad = 0
    for n in names:
        try:
            problems = compare(n)
        except Exception as e:  # noqa: BLE001
            problems = ["exception: %r" % (e,)]
        bad += bool(problems)
        print(("OK " if not problems else "BAD"), n, "; ".join(problems))
    sys.exit(1 if bad else 0)
