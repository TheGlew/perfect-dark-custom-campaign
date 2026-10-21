"""Pads and tiles: merge the JSON sources of several levels into one and build the binaries
with the repository's own tools (tools/assetmgr/mkpads and mktiles).

A pads file holds the pads (positions and boxes everything is placed at), the waypoint graph
chrs navigate, the waygroups and the cover points. A tiles file holds each room's collision.
Both are per level in the sources; a merged level needs one of each covering every room."""
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ASSETS = os.path.join(ROOT, "src", "assets", "ntsc-final")
ASSETMGR = os.path.join(ROOT, "tools", "assetmgr")


def load_pads(name):
    return json.load(open(os.path.join(ASSETS, "pads", "%s.json" % name)))


def load_tiles(name):
    return json.load(open(os.path.join(ASSETS, "tiles", "%s.json" % name)))


def _shift(pos, offset):
    return [pos[0] + offset[0], pos[1] + offset[1], pos[2] + offset[2]]


def merge_pads(parts):
    """parts: list of dicts {json, offset, waypoints (bool), prefix}. Pads keep their ids (the
    level prefix keeps them unique); positions of a part are shifted by its offset. The
    waypoint graph and waygroups are taken only from parts flagged `waypoints` (one graph per
    geometry region); cover points come from every part."""
    out = {"pads": [], "waypoints": [], "waygroups": [], "cover": []}
    seen = set()
    for part in parts:
        j = part["json"]
        off = part.get("offset", (0, 0, 0))
        for p in j["pads"]:
            q = dict(p)
            if q["id"] in seen:
                raise ValueError("duplicate pad id " + q["id"])
            seen.add(q["id"])
            q["pos"] = _shift(p["pos"], off)
            out["pads"].append(q)
        if part.get("waypoints", True):
            out["waypoints"] += [dict(w) for w in j["waypoints"]]
            out["waygroups"] += [dict(g) for g in j["waygroups"]]
        for c in j["cover"]:
            q = dict(c)
            q["pos"] = _shift(c["pos"], off)
            out["cover"].append(q)
    return out


def merge_tiles(parts):
    """parts: list of dicts {json, offset}. Rooms are concatenated in order (room numbers of a
    later part shift by the earlier parts' room counts; the caller renumbers room references
    elsewhere with the same offsets). Vertices shift by the part's offset."""
    rooms = {}
    for idx, part in enumerate(parts):
        off = part.get("offset", (0, 0, 0))
        items = list(part["json"]["rooms"].items())
        if idx > 0:
            items = items[1:]  # every level's room 0 is a dummy; the merged file keeps only the first
        for name, tiles in items:
            if name in rooms:
                raise ValueError("duplicate room " + name)
            newtiles = []
            for t in tiles:
                q = dict(t)
                q["vertices"] = [{"x": v["x"] + off[0], "y": v["y"] + off[1], "z": v["z"] + off[2]} for v in t["vertices"]]
                newtiles.append(q)
            rooms[name] = newtiles
    return {"rooms": rooms}


def room_counts(parts):
    return [len(p["json"]["rooms"]) for p in parts]


def _run_tool(tool, jsonpath, headers_only=False):
    env = dict(os.environ)
    env["ROMID"] = "ntsc-final"
    env["PYTHONPATH"] = ASSETMGR
    args = [sys.executable, os.path.join(ASSETMGR, tool), jsonpath]
    if headers_only:
        args.append("--headers-only")
    res = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("%s failed: %s" % (tool, res.stderr[-2000:]))


def build_pads(padsjson, name):
    """Write <name>.json under src/assets and build; returns the zipped pads file bytes and
    writes the PAD_<NAME>_* enum header the setup assembler resolves names with."""
    path = os.path.join(ASSETS, "pads", "%s.json" % name)
    # a stock file is left alone when the content is unchanged, so a round trip never
    # reformats what upstream committed
    if not (os.path.exists(path) and json.load(open(path)) == padsjson):
        json.dump(padsjson, open(path, "w"), indent=4)
    _run_tool("mkpads", path)
    out = os.path.join(ROOT, "build", "ntsc-final", "assets", "files", "bgdata", "bg_%s_padsZ" % name)
    return open(out, "rb").read()


def build_tiles(tilesjson, name):
    path = os.path.join(ASSETS, "tiles", "%s.json" % name)
    # a stock file is left alone when the content is unchanged, so a round trip never
    # reformats what upstream committed
    if not (os.path.exists(path) and json.load(open(path)) == tilesjson):
        json.dump(tilesjson, open(path, "w"), indent=4)
    _run_tool("mktiles", path)
    out = os.path.join(ROOT, "build", "ntsc-final", "assets", "files", "bgdata", "bg_%s_tilesZ" % name)
    return open(out, "rb").read()
