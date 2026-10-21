"""Perfect Dark background (bg) files: parse, write and merge.

Layout (port/src/preprocess/filebg.c):
  u32 primary inflated size, u32 section 1 compressed size, u32 primary compressed size
  section 1: primary (1173) then one 1173 blob per room
  section 2: u16 inflated size | 0x8000, u16 compressed size, 1173 texture id list (u16 each)
  section 3: u16 inflated size | 0x8000, u16 compressed size, 1173: room bboxes (6 s16 per room,
             room-relative), room gfxdata lengths (u16 per room, in 0x10 units), light counts (u8 per room)

The primary, inflated, starts with six u32: 0, ptr rooms, ptr portals, ptr bgcmds, ptr lights, 0.
Pointers are 0x0f000000 + offset within the inflated primary. Room table entries are 20 bytes:
u32 unk00 (virtual address of the room's compressed blob, see below), 3 f32 pos, u8 light min, u8 light max.
Entry 0 is a dummy, entries 1..N are rooms, entry N+1 holds the end address, entry N+2 is zero.

Room blobs: a room's compressed data sits at file offset unk00 - 0x0f000000 - K where
K = primary inflated size - primary compressed size - 12, and runs to the next entry's unk00. The
inflated room data's internal pointers are unk00-based: the engine relocates them with ptr - unk00.
"""
import struct
from . import rzip

BASE = 0x0F000000
ROOM_SIZE = 20
PORTAL_SIZE = 8
LIGHT_SIZE = 34
BGCMD_SIZE = 8
BGCMD_END = 0x00
BGCMD_PORTALARG = 0x64
BGCMD_ROOMARG = 0x65


class Room:
    __slots__ = ("unk00", "pos", "light_min", "light_max", "blob", "inflated")

    def __init__(self, unk00, pos, light_min, light_max, blob):
        self.unk00 = unk00
        self.pos = list(pos)
        self.light_min = light_min
        self.light_max = light_max
        self.blob = blob          # compressed bytes as stored
        self.inflated = None      # filled on demand


class Portal:
    __slots__ = ("room1", "room2", "flags", "vertices")

    def __init__(self, room1, room2, flags, vertices):
        self.room1 = room1
        self.room2 = room2
        self.flags = flags
        self.vertices = vertices  # list of (x, y, z) floats


class Light:
    __slots__ = ("roomnum", "colour", "brightness", "bits", "brightnessmult", "dir", "bbox")

    def __init__(self, roomnum, colour, brightness, bits, brightnessmult, dir, bbox):
        self.roomnum = roomnum
        self.colour = colour
        self.brightness = brightness
        self.bits = bits
        self.brightnessmult = brightnessmult
        self.dir = list(dir)
        self.bbox = [list(v) for v in bbox]  # 4 x (x, y, z) s16, world space


class BgCmd:
    __slots__ = ("type", "len", "param")

    def __init__(self, type, len, param):
        self.type = type
        self.len = len
        self.param = param


class BgFile:
    def __init__(self):
        self.rooms = []          # index 1..N (index 0 is a dummy with blob None)
        self.portals = []
        self.bgcmds = []         # without the END command
        self.lights = []
        self.textures = []       # section 2 u16 ids
        self.bboxes = []         # per room 1..N: 6 s16, room-relative
        self.gfxdatalens = []    # per room 1..N: u16
        self.lightcounts = []    # per room 1..N: u8
        self.room0 = None        # (pos, light_min, light_max) of the dummy entry

    # ------------------------------------------------------------------ parse
    @classmethod
    def parse(cls, data: bytes) -> "BgFile":
        bg = cls()
        prim_inf, s1_cmp, prim_cmp = struct.unpack(">III", data[:12])
        primary = rzip.inflate(data[12:12 + prim_cmp])
        if len(primary) != prim_inf:
            raise ValueError("primary inflated size mismatch")
        K = prim_inf - prim_cmp - 12
        hdr = struct.unpack(">6I", primary[:24])
        ptr_rooms, ptr_portals, ptr_bgcmds, ptr_lights = hdr[1], hdr[2], hdr[3], hdr[4]

        # rooms
        off = ptr_rooms - BASE
        entries = []
        while True:
            unk00, x, y, z, lmin, lmax = struct.unpack(">IfffBB", primary[off:off + 18])
            entries.append((unk00, (x, y, z), lmin, lmax))
            off += ROOM_SIZE
            if len(entries) > 1 and unk00 == 0:
                break
        # entries: [dummy, room1..roomN, endmarker, zero]
        bg.room0 = (entries[0][1], entries[0][2], entries[0][3])
        n = len(entries) - 3
        bg.rooms = [None]
        for i in range(1, n + 1):
            unk00 = entries[i][0]
            nxt = entries[i + 1][0]
            fileoff = unk00 - BASE - K
            blob = data[fileoff:fileoff + (nxt - unk00)]
            bg.rooms.append(Room(unk00, entries[i][1], entries[i][2], entries[i][3], blob))
        bg.end_unk00 = entries[n + 1][0]

        # lights (between ptr_lights and ptr_bgcmds, per the port's converter)
        if ptr_lights:
            off = ptr_lights - BASE
            end = ptr_bgcmds - BASE
            while off + LIGHT_SIZE <= end:
                roomnum, colour, brightness, bits, bmult, dx, dy, dz = struct.unpack(">HHBBBbbb", primary[off:off + 10])
                bbox = [struct.unpack(">hhh", primary[off + 10 + 6 * k: off + 16 + 6 * k]) for k in range(4)]
                bg.lights.append(Light(roomnum, colour, brightness, bits, bmult, (dx, dy, dz), bbox))
                off += LIGHT_SIZE

        # portals. In the file a portal's verticesoffset is a 1-based index into the sequence of
        # vertex blobs that follows the table (bgBuildTables converts it to an offset at load).
        off = ptr_portals - BASE
        raw = []
        while True:
            vofs, r1, r2, flags = struct.unpack(">HhhB", primary[off:off + 7])
            if vofs == 0:
                break
            raw.append((vofs, r1, r2, flags))
            off += PORTAL_SIZE
        blobs = []       # (address, vertices) in storage order
        voff = ptr_portals - BASE + PORTAL_SIZE * (len(raw) + 1)
        while True:
            count = primary[voff]
            if count == 0:
                break
            verts = [struct.unpack(">fff", primary[voff + 4 + 12 * k: voff + 16 + 12 * k]) for k in range(count)]
            blobs.append((BASE + voff, verts))
            voff += 4 + 12 * count
        for vofs, r1, r2, flags in raw:
            bg.portals.append(Portal(r1, r2, flags, blobs[vofs - 1][1]))
        bg.portal_blob_index = [vofs - 1 for vofs, _, _, _ in raw]

        # bg commands
        if ptr_bgcmds:
            off = ptr_bgcmds - BASE
            # PORTALARG carries the address of a vertex blob; keep it as the blob's index
            vtx_addr_to_portal = {addr: i for i, (addr, _) in enumerate(blobs)}
            while True:
                t, l, param = struct.unpack(">BBxxi", primary[off:off + BGCMD_SIZE])
                if t == BGCMD_END:
                    break
                if t == BGCMD_PORTALARG:
                    param = vtx_addr_to_portal[param & 0xFFFFFFFF]
                bg.bgcmds.append(BgCmd(t, l, param))
                off += BGCMD_SIZE

        # section 2
        s2 = 12 + s1_cmp
        inf2, cmp2 = struct.unpack(">HH", data[s2:s2 + 4])
        sec2 = rzip.inflate(data[s2 + 4:s2 + 4 + cmp2])
        cnt = (inf2 & 0x7FFF) // 2
        bg.textures = list(struct.unpack(">%dH" % cnt, sec2[:cnt * 2]))
        bg.sec2_flag = inf2 & 0x8000

        # section 3
        s3 = s2 + 4 + cmp2
        inf3, cmp3 = struct.unpack(">HH", data[s3:s3 + 4])
        sec3 = rzip.inflate(data[s3 + 4:s3 + 4 + cmp3])
        bg.sec3_flag = inf3 & 0x8000
        off = 0
        for i in range(n):
            bg.bboxes.append(list(struct.unpack(">6h", sec3[off:off + 12])))
            off += 12
        for i in range(n):
            bg.gfxdatalens.append(struct.unpack(">H", sec3[off:off + 2])[0])
            off += 2
        for i in range(n):
            bg.lightcounts.append(sec3[off])
            off += 1
        bg.sec3_extra = sec3[off:]
        bg.sec2_extra = sec2[cnt * 2:]
        return bg

    @property
    def numrooms(self):
        return len(self.rooms) - 1

    def room_inflated(self, i):
        r = self.rooms[i]
        if r.inflated is None:
            r.inflated = rzip.inflate(r.blob) if rzip.is1173(r.blob) else r.blob
        return r.inflated

    # ------------------------------------------------------------------ write
    def write(self, recompress_rooms=True) -> bytes:
        """Serialise. Room blobs are relocated to their new virtual addresses: every unk00-based
        pointer inside a room's inflated data is rebased, and the blob recompressed."""
        n = self.numrooms
        # The primary's inflated size does not depend on the room addresses, so build it once
        # with placeholders to learn the size, assign addresses, then build it for real.
        prim_inf = len(self._build_primary([0] * (n + 2)))
        # The engine reads room i's blob at file offset unk00(i) - BASE - K with
        # K = prim_inf - prim_cmp - 12, so blob 1 sits right after the compressed primary
        # (file offset 12 + prim_cmp) and unk00(1) = BASE + prim_inf whatever prim_cmp is.
        addr = BASE + prim_inf
        blobs = []
        unk00s = [0] * (n + 2)
        for i in range(1, n + 1):
            unk00s[i] = addr
            blob = self._relocated_blob(i, addr, recompress_rooms)
            blob += b"\0" * ((-len(blob)) % 16)
            blobs.append(blob)
            addr += len(blob)
        unk00s[n + 1] = addr
        prim = self._build_primary(unk00s)
        if len(prim) != prim_inf:
            raise AssertionError("primary size changed between passes")
        prim_z = rzip.deflate(prim)
        prim_cmp = len(prim_z)
        section1 = prim_z + b"".join(blobs)
        # section 2: texture ids
        sec2 = struct.pack(">%dH" % len(self.textures), *self.textures) + getattr(self, "sec2_extra", b"")
        sec2_z = rzip.deflate(sec2)
        sec2_z += b"\0" * ((-len(sec2_z)) % 16)
        sec2_hdr = struct.pack(">HH", len(sec2) | self.sec2_flag, len(sec2_z))
        # section 3: room bboxes, gfxdata lengths, light counts
        sec3 = b"".join(struct.pack(">6h", *b) for b in self.bboxes)
        sec3 += struct.pack(">%dH" % n, *self.gfxdatalens)
        sec3 += bytes(self.lightcounts)
        sec3 += getattr(self, "sec3_extra", b"")
        sec3_z = rzip.deflate(sec3)
        sec3_z += b"\0" * ((-len(sec3_z)) % 16)
        sec3_hdr = struct.pack(">HH", len(sec3) | self.sec3_flag, len(sec3_z))
        header = struct.pack(">III", prim_inf, len(section1), prim_cmp)
        out = header + section1 + sec2_hdr + sec2_z + sec3_hdr + sec3_z
        out += b"\0" * ((-len(out)) % 16)
        return out

    def _build_primary(self, unk00s) -> bytes:
        """Order as the originals have it: header, rooms, lights, bg commands, portals, portal
        vertices. The port counts lights as (ptr_bgcmds - ptr_lights) / 34, so nothing may sit
        between the lights and the commands."""
        n = self.numrooms
        off = 24
        ptr_rooms = BASE + off
        rooms = bytearray()
        pos0, lmin0, lmax0 = self.room0
        rooms += struct.pack(">IfffBBxx", 0, *pos0, lmin0, lmax0)
        for i in range(1, n + 1):
            r = self.rooms[i]
            rooms += struct.pack(">IfffBBxx", unk00s[i], *r.pos, r.light_min, r.light_max)
        rooms += struct.pack(">IfffBBxx", unk00s[n + 1], 0.0, 0.0, 0.0, 0, 0)
        rooms += struct.pack(">IfffBBxx", 0, 0.0, 0.0, 0.0, 0, 0)
        off += len(rooms)
        # lights
        ptr_lights = 0
        lights = bytearray()
        if self.lights:
            ptr_lights = BASE + off
            for L in self.lights:
                lights += struct.pack(">HHBBBbbb", L.roomnum, L.colour, L.brightness, L.bits, L.brightnessmult, *L.dir)
                for v in L.bbox:
                    lights += struct.pack(">hhh", *v)
            off += len(lights)
        # bg commands (always present, at least the END)
        ptr_bgcmds = BASE + off
        ncmds = len(self.bgcmds) + 1
        off += BGCMD_SIZE * ncmds
        # portals: table then one vertex blob per portal, in portal order; the table stores the
        # 1-based blob index. PORTALARG commands hold a blob's address.
        ptr_portals = BASE + off
        table_len = PORTAL_SIZE * (len(self.portals) + 1)
        vtx = bytearray()
        blob_addrs = []
        for p in self.portals:
            blob_addrs.append(ptr_portals + table_len + len(vtx))
            vtx += struct.pack(">Bxxx", len(p.vertices))
            for v in p.vertices:
                vtx += struct.pack(">fff", *v)
        vtx += struct.pack(">Bxxx", 0)
        portals = bytearray()
        for i, p in enumerate(self.portals):
            portals += struct.pack(">HhhBx", i + 1, p.room1, p.room2, p.flags)
        portals += struct.pack(">HhhBx", 0, 0, 0, 0)
        cmds = bytearray()
        for c in self.bgcmds:
            param = c.param
            if c.type == BGCMD_PORTALARG:
                param = blob_addrs[c.param]
            if param >= 0x80000000:
                param -= 0x100000000
            cmds += struct.pack(">BBxxi", c.type, c.len, param)
        cmds += struct.pack(">BBxxi", BGCMD_END, 0, 0)
        header = struct.pack(">6I", 0, ptr_rooms, ptr_portals, ptr_bgcmds, ptr_lights, 0)
        prim = header + rooms + lights + cmds + portals + vtx
        prim += b"\0" * ((-len(prim)) % 16)
        return bytes(prim)

    def _relocated_blob(self, i, new_unk00, recompress) -> bytes:
        r = self.rooms[i]
        if r.unk00 == new_unk00 and not recompress:
            return r.blob
        data = bytearray(self.room_inflated(i))
        delta = new_unk00 - r.unk00
        if delta:
            relocate_room(data, delta, r.unk00)
        return rzip.deflate(bytes(data))


def relocate_room(data: bytearray, delta: int, old_base: int):
    """Rebase every unk00-based pointer in a room's inflated gfx data (see bgLoadRoom in bg.c):
    the header's vertices, colours, opablocks and xlublocks, and each roomblock's next, gdl,
    vertices and colours. Display lists address vertices by segment offset and need nothing."""
    def rd(o):
        return struct.unpack(">I", data[o:o + 4])[0]

    def wr(o, v):
        data[o:o + 4] = struct.pack(">I", v & 0xFFFFFFFF)

    def fix(o):
        v = rd(o)
        if v:
            wr(o, v + delta)

    vertices = rd(0)
    for o in (0, 4, 8, 12):
        fix(o)
    end = (vertices - old_base) if vertices else len(data)
    off = 24  # sizeof n64_roomgfxdata header (4 ptrs + 4 s16)
    while off + 20 <= end:
        typ = data[off]
        if typ == 1:  # parent: vertices field points at the coords block, which sits before the vertices
            coords = rd(off + 12)
            if coords and coords - old_base < end:
                end = coords - old_base
        for o in (off + 4, off + 8, off + 12, off + 16):
            fix(o)
        off += 20


def merge(a: BgFile, b: BgFile, offset) -> BgFile:
    """One file holding a's rooms followed by b's, with b moved by `offset` (x, y, z)."""
    out = BgFile()
    out.room0 = a.room0
    na = a.numrooms
    out.rooms = [None] + [Room(r.unk00, r.pos, r.light_min, r.light_max, r.blob) for r in a.rooms[1:]]
    for r in a.rooms[1:]:
        pass
    for i in range(1, na + 1):
        out.rooms[i].inflated = a.room_inflated(i)
    nla = len(a.lights)
    for i in range(1, b.numrooms + 1):
        r = b.rooms[i]
        nr = Room(r.unk00, [r.pos[k] + offset[k] for k in range(3)], r.light_min, r.light_max, r.blob)
        inflated = bytearray(b.room_inflated(i))
        # lightsindex (s16 at 0x10) indexes the primary lights table: shift by a's light count
        li = struct.unpack(">h", inflated[16:18])[0]
        if struct.unpack(">h", inflated[18:20])[0] > 0:
            inflated[16:18] = struct.pack(">h", li + nla)
        nr.inflated = bytes(inflated)
        out.rooms.append(nr)
    out.end_unk00 = 0
    out.lights = [Light(L.roomnum, L.colour, L.brightness, L.bits, L.brightnessmult, L.dir, L.bbox) for L in a.lights]
    for L in b.lights:
        bbox = [[v[0] + int(offset[0]), v[1] + int(offset[1]), v[2] + int(offset[2])] for v in L.bbox]
        out.lights.append(Light(L.roomnum + na, L.colour, L.brightness, L.bits, L.brightnessmult, L.dir, bbox))
    out.portals = [Portal(p.room1, p.room2, p.flags, p.vertices) for p in a.portals]
    npa = len(a.portals)
    for p in b.portals:
        verts = [(v[0] + offset[0], v[1] + offset[1], v[2] + offset[2]) for v in p.vertices]
        out.portals.append(Portal(p.room1 + na, p.room2 + na, p.flags, verts))
    out.bgcmds = [BgCmd(c.type, c.len, c.param) for c in a.bgcmds]
    for c in b.bgcmds:
        param = c.param
        if c.type == BGCMD_PORTALARG:
            param = c.param + npa
        elif c.type == BGCMD_ROOMARG:
            param = c.param + na
        out.bgcmds.append(BgCmd(c.type, c.len, param))
    seen = set(a.textures)
    out.textures = list(a.textures) + [t for t in b.textures if not (t in seen or seen.add(t))]
    out.bboxes = [list(x) for x in a.bboxes] + [list(x) for x in b.bboxes]
    out.gfxdatalens = list(a.gfxdatalens) + list(b.gfxdatalens)
    out.lightcounts = list(a.lightcounts) + list(b.lightcounts)
    out.sec2_extra = b""
    out.sec3_extra = b""
    out.sec2_flag = a.sec2_flag
    out.sec3_flag = a.sec3_flag
    return out
