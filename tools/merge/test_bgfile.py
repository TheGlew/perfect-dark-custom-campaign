"""Round-trip every background file: parse, write, parse again, and compare everything that
matters, with room data compared after normalising pointers to a zero base."""
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import bgfile, rzip  # noqa: E402


def normalised_room(bg, i):
    data = bytearray(bg.room_inflated(i))
    bgfile.relocate_room(data, -bg.rooms[i].unk00, bg.rooms[i].unk00)
    return bytes(data)


def check(path):
    raw = open(path, "rb").read()
    a = bgfile.BgFile.parse(raw)
    out = a.write()
    b = bgfile.BgFile.parse(out)
    problems = []
    if a.numrooms != b.numrooms:
        problems.append("rooms %d != %d" % (a.numrooms, b.numrooms))
    for i in range(1, min(a.numrooms, b.numrooms) + 1):
        ra, rb = a.rooms[i], b.rooms[i]
        if ra.pos != rb.pos or ra.light_min != rb.light_min or ra.light_max != rb.light_max:
            problems.append("room %d header differs" % i)
        if normalised_room(a, i) != normalised_room(b, i):
            problems.append("room %d data differs" % i)
            if len(problems) > 5:
                break
    if [(p.room1, p.room2, p.flags, p.vertices) for p in a.portals] != [(p.room1, p.room2, p.flags, p.vertices) for p in b.portals]:
        problems.append("portals differ")
    if [(L.roomnum, L.colour, L.brightness, L.bits, L.brightnessmult, L.dir, L.bbox) for L in a.lights] != \
       [(L.roomnum, L.colour, L.brightness, L.bits, L.brightnessmult, L.dir, L.bbox) for L in b.lights]:
        problems.append("lights differ (%d vs %d)" % (len(a.lights), len(b.lights)))
    if [(c.type, c.len, c.param) for c in a.bgcmds] != [(c.type, c.len, c.param) for c in b.bgcmds]:
        problems.append("bgcmds differ")
    if a.textures != b.textures:
        problems.append("textures differ")
    if a.bboxes != b.bboxes or a.gfxdatalens != b.gfxdatalens or a.lightcounts != b.lightcounts:
        problems.append("section 3 differs")
    # the original's first room address against the formula the writer relies on
    prim_inf = struct.unpack(">I", raw[:4])[0]
    note = "unk00(1) %s BASE+prim_inf" % ("==" if a.rooms[1].unk00 == bgfile.BASE + prim_inf else "!=")
    return problems, note, len(raw), len(out), a.numrooms, len(a.portals), len(a.lights), len(a.bgcmds)


if __name__ == "__main__":
    names = sys.argv[1:] or ["ame", "ear", "ark", "pete", "depo", "lue", "dish", "sho"]
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src", "assets", "ntsc-final", "files", "bgdata")
    bad = 0
    for n in names:
        p = os.path.join(root, "bg_%s.seg" % n)
        if not os.path.exists(p):
            print(n, "missing"); continue
        problems, note, lin, lout, nr, npo, nl, nc = check(p)
        status = "OK " if not problems else "BAD"
        bad += bool(problems)
        print("%s %-5s rooms %3d portals %3d lights %4d cmds %4d  in %7d out %7d  %s %s" % (status, n, nr, npo, nl, nc, lin, lout, note, "; ".join(problems)))
    sys.exit(1 if bad else 0)
