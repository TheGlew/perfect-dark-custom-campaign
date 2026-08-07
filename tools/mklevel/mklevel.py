#!/usr/bin/env python3
"""
mklevel - one level description in, three outputs.

This is decision A of the fork plan: geometry is built by C at runtime while collision
and navigation are compile-time JSON, so "author them from the same source" is only
meaningful if that source sits OFFLINE of both. One description file per level is
transpiled into:

  1. src/generated/<name>_geom.inc.c  - static geometry DATA (never display lists;
                                        those are built at runtime by bgprocedural.c)
  2. src/assets/<romid>/tiles/<short>.json - collision, compiled by mktiles
  3. src/assets/<romid>/pads/<short>.json  - navigation, compiled by mkpads

THE COORDINATE ASYMMETRY, which is the whole reason this tool exists:

  * GEOMETRY vertices are ROOM-RELATIVE, offset by g_BgRooms[r].pos (verified: bg.c:1984
    adds the room pos to the section-3 bbox, and a real room's verts are ~(0,-192,-454)
    while its world bbox is (783,583,424)..(1584,1300,1382)).
  * TILE vertices are WORLD coordinates (verified empirically: ame room 2's tiles span
    x -783..912 y -600..1100 z -1100..1100, matching that room's world bbox rather than
    being centred on its origin (71,3003,0)).

Authoring in one place and emitting both forms is what keeps them from drifting.

Usage:
    python mklevel.py <level.json> [--repo <path-to-fork>] [--dry-run]

Schemas for the two JSON outputs are documented in docs/w2-asset-schemas.md.
"""

import argparse
import json
import os
import sys

# Every tile flag key mktiles requires. It does `if room[name]` with no default, so a
# missing key is a KeyError rather than a false. Order is the bit order.
TILE_FLAGS = [
    'flag0001', 'flag0002', 'flag0004', 'flag0008',
    'flag0010', 'flag0020', 'ladder', 'flag0080',
    'flag0100', 'underwater', 'flag0400', 'aibotcrouch',
    'aibotduck', 'flag2000', 'die', 'climbableledge',
]

FLOORTYPES = ['default', 'wood', 'stone', 'carpet', 'metal', 'mud', 'water', 'dirt', 'snow']


def tile(vertices, floortype='default', floorcolour=4095, **flags):
    """One collision surface. vertices are WORLD coords, wound consistently."""
    if floortype not in FLOORTYPES:
        raise ValueError('unknown floortype %r (expected one of %s)' % (floortype, FLOORTYPES))

    t = {name: bool(flags.get(name, False)) for name in TILE_FLAGS}
    # flag0001/0002/0008/0010 are set on ordinary walkable surfaces in the stock data
    # (sampled from uff); carrying them keeps authored floors behaving like real ones.
    for name in ('flag0001', 'flag0002', 'flag0008', 'flag0010'):
        t.setdefault(name, True)
        if name not in flags:
            t[name] = True
    t['floortype'] = floortype
    t['floorcolour'] = floorcolour
    t['vertices'] = [{'x': int(round(v[0])), 'y': int(round(v[1])), 'z': int(round(v[2]))}
                     for v in vertices]
    return t


class Box:
    """
    An axis-aligned box, authored in WORLD coordinates.

    This is the plan's decision B: primitives are vocabulary of the AUTHORING FORMAT,
    expanded here into plain vertex arrays. The engine stays dumb data population and
    never grows a runtime layout API.
    """

    def __init__(self, spec):
        self.min = [float(v) for v in spec['min']]
        self.max = [float(v) for v in spec['max']]
        self.floortype = spec.get('floortype', 'default')
        self.walls = spec.get('walls', True)
        self.ceiling = spec.get('ceiling', True)

    def corners(self):
        """8 world-space corners, ordered 0-3 bottom then 4-7 top (matches bgprocedural)."""
        x0, y0, z0 = self.min
        x1, y1, z1 = self.max
        return [
            (x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1),
            (x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1),
        ]

    def geometry_vertices(self, origin):
        """Room-RELATIVE s16 vertices for the runtime geometry."""
        return [(int(round(c[0] - origin[0])),
                 int(round(c[1] - origin[1])),
                 int(round(c[2] - origin[2]))) for c in self.corners()]

    def geometry_tris(self):
        """12 faces, then the same 12 reversed so surfaces are visible from inside."""
        base = [
            (0, 1, 2), (0, 2, 3),   # floor
            (4, 6, 5), (4, 7, 6),   # ceiling
            (0, 5, 1), (0, 4, 5),   # -z
            (3, 2, 6), (3, 6, 7),   # +z
            (0, 3, 7), (0, 7, 4),   # -x
            (1, 5, 6), (1, 6, 2),   # +x
        ]
        return base + [(c, b, a) for (a, b, c) in base]

    def collision_tiles(self):
        """
        World-space collision quads. The floor is what lets the player stand; the walls
        are what stop them leaving. Emitted as quads because that is what the stock data
        uses (uff is 16 axis-aligned quads on a lattice).
        """
        x0, y0, z0 = self.min
        x1, y1, z1 = self.max
        tiles = [tile([(x0, y0, z0), (x0, y0, z1), (x1, y0, z1), (x1, y0, z0)],
                      floortype=self.floortype)]

        if self.ceiling:
            tiles.append(tile([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
                              floortype=self.floortype))

        if self.walls:
            tiles += [
                tile([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], floortype=self.floortype),
                tile([(x0, y0, z1), (x0, y1, z1), (x1, y1, z1), (x1, y0, z1)], floortype=self.floortype),
                tile([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], floortype=self.floortype),
                tile([(x1, y0, z0), (x1, y0, z1), (x1, y1, z1), (x1, y1, z0)], floortype=self.floortype),
            ]

        return tiles


class Level:
    def __init__(self, spec, path):
        self.path = path
        self.name = spec['name']
        self.shortname = spec['shortname']
        self.romid = spec.get('romid', 'ntsc-final')
        # Must match PROC_ROOMCOUNT in bgprocedural.c: the room TABLE stays full-size so
        # a surviving stock setup cannot index past it.
        self.roomcount = int(spec.get('roomcount', 256))
        self.rooms = spec['rooms']
        self.pads = spec.get('pads', [])

    def room_index(self, room):
        return int(room['index'])

    # -- output 1: geometry as generated C data ---------------------------------

    def emit_geometry(self):
        out = []
        out.append('/*')
        out.append(' * GENERATED by tools/mklevel/mklevel.py from %s' % os.path.basename(self.path))
        out.append(' * DO NOT EDIT. Regenerate instead.')
        out.append(' *')
        out.append(' * Static geometry DATA only. Display lists are built at RUNTIME by')
        out.append(' * bgprocedural.c, because texture pointers can only be resolved after')
        out.append(' * texLoadFromTextureNum has run; baking Gfx here would force a segment-')
        out.append(' * patching pass later.')
        out.append(' *')
        out.append(' * Vertices are ROOM-RELATIVE (offset by the room origin below).')
        out.append(' */')
        out.append('')

        prefix = 'level_%s' % self.name

        for room in self.rooms:
            idx = self.room_index(room)
            origin = room['origin']
            verts = []
            tris = []
            base = 0

            for boxspec in room.get('boxes', []):
                box = Box(boxspec)
                verts += box.geometry_vertices(origin)
                tris += [(a + base, b + base, c + base) for (a, b, c) in box.geometry_tris()]
                base = len(verts)

            # Every symbol carries the level id: these files are included together at
            # file scope, so unprefixed names would collide the moment a second level
            # exists.
            sym = '%s_room%d' % (prefix, idx)

            out.append('static const struct procvtx %s_vertices[] = {' % sym)
            for v in verts:
                out.append('\t{ %d, %d, %d },' % v)
            out.append('};')
            out.append('')

            out.append('static const u8 %s_tris[][3] = {' % sym)
            for t in tris:
                out.append('\t{ %d, %d, %d },' % t)
            out.append('};')
            out.append('')

            out.append('static const struct procroom %s = {' % sym)
            out.append('\t/* index    */ %d,' % idx)
            out.append('\t/* origin   */ { %ff, %ff, %ff },' % tuple(float(v) for v in origin))
            out.append('\t/* bbmin    */ { %ff, %ff, %ff },' % tuple(float(v) for v in room['bbmin']))
            out.append('\t/* bbmax    */ { %ff, %ff, %ff },' % tuple(float(v) for v in room['bbmax']))
            out.append('\t/* vertices */ %s_vertices,' % sym)
            out.append('\t/* numverts */ %d,' % len(verts))
            out.append('\t/* tris     */ %s_tris,' % sym)
            out.append('\t/* numtris  */ %d,' % len(tris))
            out.append('};')
            out.append('')

        out.append('static const struct procroom *%s_rooms[] = {' % prefix)
        for room in self.rooms:
            out.append('\t&%s_room%d,' % (prefix, self.room_index(room)))
        out.append('};')
        out.append('')
        out.append('#define %s_NUMROOMS %d' % (prefix.upper(), len(self.rooms)))
        out.append('#define %s_ROOMCOUNT %d' % (prefix.upper(), self.roomcount))
        out.append('')

        return '\n'.join(out)

    # -- output 2: collision ----------------------------------------------------

    def emit_tiles(self):
        """
        Emits roomcount rooms so the tile room table lines up 1:1 with the engine's
        room table. Room 0 is the sentinel and is always empty, matching every stock
        file (ame and uff both have an empty room 0).
        """
        by_index = {}
        for room in self.rooms:
            tiles = []
            for boxspec in room.get('boxes', []):
                tiles += Box(boxspec).collision_tiles()

            # Collision is looked up PER ROOM: collision.c:989 bounds a room's tiles with
            # g_TileRooms[roomnum]..[roomnum+1]. While a procedural level still carries the
            # original stage's pads and setup, the engine may consult a room index that is
            # ours only in the geometry sense -- the stock spawn pad belongs to the original
            # room 2, but our box is room 1, and a room with no tiles reads as "no floor".
            # collisionRooms lets one authored volume publish its collision into several
            # room indices until the level owns its own pads (W4).
            for idx in room.get('collisionRooms', [self.room_index(room)]):
                by_index[int(idx)] = tiles

        rooms = {}
        for i in range(self.roomcount):
            key = 'ROOM_%s_%04d' % (self.shortname.upper(), i)
            rooms[key] = by_index.get(i, [])

        return {'rooms': rooms}

    # -- output 3: navigation ---------------------------------------------------

    def emit_pads(self):
        pads = []
        for i, pad in enumerate(self.pads):
            pos = pad['pos']
            pads.append({
                'id': 'PAD_%s_%04d' % (self.shortname.upper(), i),
                'pos': [int(round(v)) for v in pos],
                'dir': [int(v) for v in pad.get('dir', [0, 0, -1])],
                'up': [int(v) for v in pad.get('up', [0, 1, 0])],
                'xmin': -100, 'xmax': 100,
                'ymin': -100, 'ymax': 100,
                'zmin': -100, 'zmax': 100,
                'aiwaitlift': False, 'aionlift': False, 'aiwalkdirect': False,
                'aidrop': False, 'aicrouch': False, 'aiignorey': False,
                'aiduck': False, 'liftnum': 0,
            })

        # All four keys are required by mkpads and may legitimately be empty; uff proves
        # a level with no AI navigation at all is valid.
        return {'pads': pads, 'waypoints': [], 'waygroups': [], 'cover': []}


def main():
    ap = argparse.ArgumentParser(description='Transpile a level description into geometry C, tiles and pads.')
    ap.add_argument('level')
    ap.add_argument('--repo', default=None, help='fork root (default: inferred from this script)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    repo = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

    with open(args.level, 'r', encoding='utf-8') as fd:
        spec = json.load(fd)

    level = Level(spec, args.level)

    geom_path = os.path.join(repo, 'src', 'generated', '%s_geom.inc.c' % level.name)
    tiles_path = os.path.join(repo, 'src', 'assets', level.romid, 'tiles', '%s.json' % level.shortname)
    pads_path = os.path.join(repo, 'src', 'assets', level.romid, 'pads', '%s.json' % level.shortname)

    geom = level.emit_geometry()
    tiles = level.emit_tiles()
    pads = level.emit_pads()

    total_tiles = sum(len(v) for v in tiles['rooms'].values())
    print('level      : %s (shortname %s, romid %s)' % (level.name, level.shortname, level.romid))
    print('rooms      : %d authored, %d in table' % (len(level.rooms), level.roomcount))
    print('geometry   : %s' % geom_path)
    print('tiles      : %s  (%d tiles)' % (tiles_path, total_tiles))
    print('pads       : %s  (%d pads)' % (pads_path, len(pads['pads'])))

    if args.dry_run:
        print('\n--- dry run, nothing written ---')
        return 0

    os.makedirs(os.path.dirname(geom_path), exist_ok=True)
    with open(geom_path, 'w', encoding='utf-8') as fd:
        fd.write(geom)

    for path, data in ((tiles_path, tiles), (pads_path, pads)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fd:
            json.dump(data, fd, indent=1)
            fd.write('\n')

    print('\nwritten.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
