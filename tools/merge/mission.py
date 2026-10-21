"""Build a merged mission's geometry: one background, one collision file and one pads file
holding every leg's world, each world placed by its own offset.

A mission spec names its worlds in order; the first world keeps its coordinates and later
ones are shifted so nothing overlaps. Room numbers of a later world shift by the earlier
worlds' room counts; `room_offsets()` reports them so setups can be rewritten to match."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import bgfile, assets  # noqa: E402

ROOT = assets.ROOT
BGDATA = os.path.join(ROOT, "src", "assets", "ntsc-final", "files", "bgdata")

# world name -> (bg name, pads name for the nav graph, offset)
MISSIONS = {
    # Mission 1: dataDyne Central (Defection and Extraction share it) and dataDyne Research
    "dd": {
        "worlds": [
            {"bg": "ame", "pads": ["ame", "ark"], "graph": "ame", "offset": (0, 0, 0)},
            {"bg": "ear", "pads": ["ear"], "graph": "ear", "offset": (20000, 0, 0)},
        ],
    },
    # Mission 3: Chicago's streets and the G5 Building
    "chi": {
        "worlds": [
            {"bg": "pete", "pads": ["pete"], "graph": "pete", "offset": (0, 0, 0)},
            {"bg": "depo", "pads": ["depo"], "graph": "depo", "offset": (20000, 0, 0)},
        ],
    },
    # Mission 4: Area 51, one world, three setups over identical pads
    "a51": {
        "worlds": [
            {"bg": "lue", "pads": ["lue"], "graph": "lue", "offset": (0, 0, 0)},
        ],
    },
}


def load_bg(name):
    return bgfile.BgFile.parse(open(os.path.join(BGDATA, "bg_%s.seg" % name), "rb").read())


def room_offsets(spec):
    """Room number offsets per world: a later world's room r becomes r + offset, where the
    offset is the number of real rooms (room 0 excluded) in the worlds before it."""
    offs = []
    total = 0
    for w in spec["worlds"]:
        offs.append(total)
        total += len(assets.load_tiles(w["bg"])["rooms"]) - 1
    return offs


def build_geometry(key, outdir):
    """Write bg_<key>.seg, bg_<key>_tilesZ and bg_<key>_padsZ into outdir/bgdata."""
    spec = MISSIONS[key]
    worlds = spec["worlds"]
    merged = load_bg(worlds[0]["bg"])
    for w in worlds[1:]:
        merged = bgfile.merge(merged, load_bg(w["bg"]), w["offset"])
    tiles = assets.merge_tiles([{"json": assets.load_tiles(w["bg"]), "offset": w["offset"]} for w in worlds])
    padparts = []
    for w in worlds:
        for p in w["pads"]:
            padparts.append({"json": assets.load_pads(p), "offset": w["offset"], "waypoints": p == w["graph"]})
    pads = assets.merge_pads(padparts)
    os.makedirs(os.path.join(outdir, "bgdata"), exist_ok=True)
    open(os.path.join(outdir, "bgdata", "bg_%s.seg" % key), "wb").write(merged.write())
    open(os.path.join(outdir, "bgdata", "bg_%s_tilesZ" % key), "wb").write(assets.build_tiles(tiles, key))
    open(os.path.join(outdir, "bgdata", "bg_%s_padsZ" % key), "wb").write(assets.build_pads(pads, key))
    return {
        "rooms": merged.numrooms,
        "room_offsets": room_offsets(spec),
        "pads": len(pads["pads"]),
        "waypoints": len(pads["waypoints"]),
        "portals": len(merged.portals),
        "lights": len(merged.lights),
        "pad_positions": {p["id"]: tuple(p["pos"]) for p in pads["pads"]},
    }


if __name__ == "__main__":
    key = sys.argv[1]
    outdir = sys.argv[2]
    print(build_geometry(key, outdir))
