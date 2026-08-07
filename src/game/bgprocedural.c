/**
 * bgprocedural.c - W1: build a room in memory instead of loading it from a bg file.
 *
 * The stock path is three phases, and this file replaces all three for stages listed
 * in bgIsProceduralStage():
 *
 *   1. bgReset        loads bg sections 1+2 -> g_BgRooms, g_BgPortals, roomcount
 *   2. bgBuildTables  reads section 3       -> per-room bbox, gfxdatalen, lights
 *   3. bgLoadRoom     streams per-room gfx  -> roomgfxdata, roomblocks, display lists
 *
 * Everything here is additive: bg.c gains three early-out branches and nothing else.
 * The contract this is written against is documented (with file:line evidence) in
 * ../../docs/w1-engine-contract.md. The three facts that matter most:
 *
 *   - ROOM 0 IS A SENTINEL. bg.c:1621 counts rooms from index 1 and bg.c:841/2800/3319
 *     reject roomnum 0. One usable room therefore means roomcount = 2, box at index 1.
 *   - Vertices are ROOM-RELATIVE to g_BgRooms[r].pos, and the room bbox is world space.
 *   - Room display lists address vertices through GBI SEGMENTS, not pointers.
 *     bgRenderRoomPass binds SPSEGMENT_BG_VTX/COL to the block's arrays immediately
 *     before running the list (bg.c:3231), and bgPopulateVtxBatchType reads G_VTX
 *     payloads back as byte offsets into them (bg.c:3369). Emitting a real pointer
 *     here produces garbage vertex batches and broken culling while looking correct.
 */
#include <ultra64.h>
#include "constants.h"
#include "game/bg.h"
#include "game/bgprocedural.h"
#include "bss.h"
#include "data.h"
#include "types.h"
#include "gbiex.h"
#include "lib/memp.h"

#ifndef PLATFORM_N64
#include "system.h"
#endif

#define PROC_ROOM        1     // the box. room 0 is the engine's sentinel.

/**
 * Why this is 256 and not 2.
 *
 * W1 replaces GEOMETRY only: the stage's setup file still loads and still creates the
 * original level's props and chrs, and those reference the original room numbers.
 * Defection has 168 rooms, so with roomcount = 2 setupCreateProps indexes g_Rooms far
 * out of bounds and the game dies with an access violation inside lvReset -- nowhere
 * near the procedural code, and easy to misread as a geometry bug.
 *
 * So the room TABLE stays generously sized while only room 1 has geometry. Rooms
 * 2..255 are degenerate: a one-unit box parked far outside the playable world, so
 * they are never visible, never stream, and never contain the player. That last part
 * matters -- with no portals the engine draws only the room the player is in, so the
 * player must resolve into room 1 for the box to render at all.
 */
#define PROC_ROOMCOUNT   256

// Somewhere no player or prop will ever be.
#define PROC_VOID_COORD  (-1000000.0f)

#define PROC_NUMVERTS    8
#define PROC_NUMCOLS     8
#define PROC_NUMTRIS     12
#define PROC_MAXGFX      32

// The box, in WORLD coordinates. Sized around Defection's measured player start
// (14.9, 159.0, -20.8): floor slightly below the player, generous headroom. W1
// replaces geometry only, so the player still spawns from the original setup and
// still collides with the original world; the box must ENCLOSE that point or the
// room is culled before it is ever drawn.
#define PROC_MIN_X   (-385.0f)
#define PROC_MAX_X    ( 415.0f)
#define PROC_MIN_Y    ( 109.0f)
#define PROC_MAX_Y    ( 509.0f)
#define PROC_MIN_Z   (-420.0f)
#define PROC_MAX_Z    ( 380.0f)

// Defined in bg.c but not exposed through a header.
extern struct bgcmd *g_BgCommands;
extern f32 *g_BgStanThings;

static struct bgroom *g_ProcBgRooms;
static struct bgportal *g_ProcBgPortals;

bool bgIsProceduralStage(s32 stagenum)
{
	return stagenum == STAGE_DEFECTION;
}

/**
 * Phase 1: stand in for the section 1 + 2 load.
 *
 * Produces the same globals bgReset would have produced: a bgroom array indexed from
 * 1 and terminated by a zero unk00, an empty portal table, and no commands, lights or
 * stan data.
 */
void bgProceduralReset(s32 stagenum)
{
	s32 i;

	// bgroom array: [0] sentinel, [1] the box, [2] terminator (unk00 == 0 ends the
	// count loop at bg.c:1623).
	g_ProcBgRooms = mempAlloc(ALIGN16((PROC_ROOMCOUNT + 1) * sizeof(struct bgroom)), MEMPOOL_STAGE);

	for (i = 0; i < PROC_ROOMCOUNT + 1; i++) {
		g_ProcBgRooms[i].unk00 = 0;
		g_ProcBgRooms[i].pos.x = 0.0f;
		g_ProcBgRooms[i].pos.y = 0.0f;
		g_ProcBgRooms[i].pos.z = 0.0f;
		g_ProcBgRooms[i].br_light_min = 255;
		g_ProcBgRooms[i].br_light_max = 255;
	}

	// Non-zero marks an entry as a real room for the counting loop at bg.c:1623, which
	// stops at the first zero unk00. The stock value is a segment address into the bg
	// file; nothing dereferences it on this path.
	for (i = 1; i < PROC_ROOMCOUNT; i++) {
		g_ProcBgRooms[i].unk00 = 1;
		g_ProcBgRooms[i].pos.x = PROC_VOID_COORD;
		g_ProcBgRooms[i].pos.y = PROC_VOID_COORD;
		g_ProcBgRooms[i].pos.z = PROC_VOID_COORD;
	}

	g_ProcBgRooms[PROC_ROOM].pos.x = (PROC_MIN_X + PROC_MAX_X) * 0.5f;
	g_ProcBgRooms[PROC_ROOM].pos.y = (PROC_MIN_Y + PROC_MAX_Y) * 0.5f;
	g_ProcBgRooms[PROC_ROOM].pos.z = (PROC_MIN_Z + PROC_MAX_Z) * 0.5f;

	// Terminator.
	g_ProcBgRooms[PROC_ROOMCOUNT].unk00 = 0;

	// Empty portal table: a single terminator (verticesoffset == 0 ends every portal
	// loop, e.g. bg.c:1719 and bg.c:2037).
	g_ProcBgPortals = mempAlloc(ALIGN16(2 * sizeof(struct bgportal)), MEMPOOL_STAGE);
	g_ProcBgPortals[0].verticesoffset = 0;
	g_ProcBgPortals[0].roomnum1 = 0;
	g_ProcBgPortals[0].roomnum2 = 0;
	g_ProcBgPortals[0].flags = 0;
	g_ProcBgPortals[1] = g_ProcBgPortals[0];

	g_BgRooms = g_ProcBgRooms;
	g_BgPortals = g_ProcBgPortals;
	g_BgCommands = NULL;
	g_BgLightsFileData = NULL;
	g_BgStanThings = NULL;

	g_Vars.roomcount = PROC_ROOMCOUNT;

#ifndef PLATFORM_N64
	sysLogPrintf(LOG_NOTE, "bgprocedural: stage 0x%02x reset, roomcount=%d, room %d origin (%.1f, %.1f, %.1f)",
			stagenum, (s32)g_Vars.roomcount, PROC_ROOM,
			g_ProcBgRooms[PROC_ROOM].pos.x,
			g_ProcBgRooms[PROC_ROOM].pos.y,
			g_ProcBgRooms[PROC_ROOM].pos.z);
#endif
}

/**
 * Phase 2: stand in for the section 3 read inside bgBuildTables.
 *
 * Section 3 supplies, per room: the bounding box (as s16 offsets from the room origin),
 * the gfxdata length, and the light count. Mirrors bg.c:1957-2008 including the centre
 * and radius derivations, so the numbers are computed the same way rather than guessed.
 */
void bgProceduralBuildRoomMetrics(void)
{
	s32 r;

	for (r = 1; r < g_Vars.roomcount; r++) {
		if (r == PROC_ROOM) {
			g_Rooms[r].bbmin[0] = PROC_MIN_X;
			g_Rooms[r].bbmin[1] = PROC_MIN_Y;
			g_Rooms[r].bbmin[2] = PROC_MIN_Z;
			g_Rooms[r].bbmax[0] = PROC_MAX_X;
			g_Rooms[r].bbmax[1] = PROC_MAX_Y;
			g_Rooms[r].bbmax[2] = PROC_MAX_Z;
		} else {
			// Degenerate filler so the surviving original setup can reference any
			// room number without indexing out of bounds. Parked far away so it is
			// never visible and never contains the player.
			g_Rooms[r].bbmin[0] = PROC_VOID_COORD;
			g_Rooms[r].bbmin[1] = PROC_VOID_COORD;
			g_Rooms[r].bbmin[2] = PROC_VOID_COORD;
			g_Rooms[r].bbmax[0] = PROC_VOID_COORD + 1.0f;
			g_Rooms[r].bbmax[1] = PROC_VOID_COORD + 1.0f;
			g_Rooms[r].bbmax[2] = PROC_VOID_COORD + 1.0f;
		}

		g_Rooms[r].centre.x = (g_Rooms[r].bbmin[0] + g_Rooms[r].bbmax[0]) / 2.0f;
		g_Rooms[r].centre.y = (g_Rooms[r].bbmin[1] + g_Rooms[r].bbmax[1]) / 2.0f;
		g_Rooms[r].centre.z = (g_Rooms[r].bbmin[2] + g_Rooms[r].bbmax[2]) / 2.0f;

		g_Rooms[r].radius = sqrtf(
				(g_Rooms[r].bbmin[0] - g_Rooms[r].bbmax[0]) * (g_Rooms[r].bbmin[0] - g_Rooms[r].bbmax[0])
				+ (g_Rooms[r].bbmin[1] - g_Rooms[r].bbmax[1]) * (g_Rooms[r].bbmin[1] - g_Rooms[r].bbmax[1])
				+ (g_Rooms[r].bbmin[2] - g_Rooms[r].bbmax[2]) * (g_Rooms[r].bbmin[2] - g_Rooms[r].bbmax[2])) / 2.0f;

		// Any non-zero length; the procedural loader does not stream, so this is only
		// used for allocation sizing decisions elsewhere.
		g_Rooms[r].gfxdatalen = 4096;

		// Explicit: only the box ever has geometry. bgBuildTables already nulls these
		// (bg.c:1919) but something on this path was observed writing a garbage
		// pointer into unloaded rooms' gfxdata afterwards, so re-assert it here.
		if (r != PROC_ROOM) {
			g_Rooms[r].gfxdata = NULL;
			g_Rooms[r].loaded240 = 0;
		}

		// No lights in W1. lightindex -1 is how the stock loader marks "no lights"
		// (bg.c:2004), and it keeps g_Rooms[r].colours NULL so bgRenderRoomPass uses
		// block->colours unchanged (bg.c:3245).
		g_Rooms[r].numlights = 0;
		g_Rooms[r].lightindex = -1;
	}
}

/**
 * Phase 3: stand in for bgLoadRoom's streamed geometry.
 *
 * Allocation layout deliberately mirrors the file layout the engine expects, which was
 * read off the reference dump's pointer arithmetic (gfxdata + 0x28 = opablock,
 * + 0x28 = xlublock, + 0x28 = vertices) and is assumed by bg.c:3242
 * (addr = ALIGN8(&gfxdata->vertices[numvertices]) for the colour base):
 *
 *   [roomgfxdata header][block 0][block 1][vertices][colours][display list]
 */
void bgProceduralLoadRoom(s32 roomnum)
{
	u8 *buf;
	struct roomgfxdata *g;
	struct roomblock *opa;
	Vtx *verts;
	Col *cols;
	Gfx *gdl;
	Gfx *gdlstart;
	u32 total;
	s32 i;

	// Box corners, room-relative. 0-3 bottom (y-), 4-7 top (y+).
	static const s16 corner[PROC_NUMVERTS][3] = {
		{ -1, -1, -1 }, {  1, -1, -1 }, {  1, -1,  1 }, { -1, -1,  1 },
		{ -1,  1, -1 }, {  1,  1, -1 }, {  1,  1,  1 }, { -1,  1,  1 },
	};

	// 12 triangles, 2 per face. Culling is disabled below, so winding is not load-bearing.
	static const u8 tris[PROC_NUMTRIS][3] = {
		{ 0, 1, 2 }, { 0, 2, 3 },   // floor
		{ 4, 6, 5 }, { 4, 7, 6 },   // ceiling
		{ 0, 5, 1 }, { 0, 4, 5 },   // -z wall
		{ 3, 2, 6 }, { 3, 6, 7 },   // +z wall
		{ 0, 3, 7 }, { 0, 7, 4 },   // -x wall
		{ 1, 5, 6 }, { 1, 6, 2 },   // +x wall
	};

	// Distinct per-corner colours: makes it unmistakable that what is on screen is
	// OUR geometry and not a surviving piece of the original level.
	static const u32 palette[PROC_NUMCOLS] = {
		0xff3050ff, 0xff30ff50, 0xffff5030, 0xffffe030,
		0xff9030ff, 0xff30e0ff, 0xffff30a0, 0xffe0e0e0,
	};

	if (roomnum != PROC_ROOM) {
		return;
	}

	if (g_Rooms[roomnum].gfxdata != NULL) {
		g_Rooms[roomnum].loaded240 = 1;
		return;
	}

	total = sizeof(struct roomgfxdata)
			+ sizeof(struct roomblock) * 2
			+ sizeof(Vtx) * PROC_NUMVERTS + 8
			+ sizeof(Col) * PROC_NUMCOLS + 8
			+ sizeof(Gfx) * PROC_MAXGFX + 8;

	buf = sysMemAlloc(total);

	if (buf == NULL) {
#ifndef PLATFORM_N64
		sysLogPrintf(LOG_ERROR, "bgprocedural: failed to alloc %u bytes for room %d", total, roomnum);
#endif
		return;
	}

	g = (struct roomgfxdata *)buf;
	opa = &g->blocks[0];
	verts = (Vtx *)ALIGN8((uintptr_t)&g->blocks[2]);
	cols = (Col *)ALIGN8((uintptr_t)&verts[PROC_NUMVERTS]);
	gdl = (Gfx *)ALIGN8((uintptr_t)&cols[PROC_NUMCOLS]);
	gdlstart = gdl;

	for (i = 0; i < PROC_NUMVERTS; i++) {
		verts[i].x = (s16)(corner[i][0] < 0 ? (PROC_MIN_X - g_BgRooms[roomnum].pos.x) : (PROC_MAX_X - g_BgRooms[roomnum].pos.x));
		verts[i].y = (s16)(corner[i][1] < 0 ? (PROC_MIN_Y - g_BgRooms[roomnum].pos.y) : (PROC_MAX_Y - g_BgRooms[roomnum].pos.y));
		verts[i].z = (s16)(corner[i][2] < 0 ? (PROC_MIN_Z - g_BgRooms[roomnum].pos.z) : (PROC_MAX_Z - g_BgRooms[roomnum].pos.z));
		verts[i].flags = 0;
		// BYTE offset into the colour segment, not an index (bgAddXrayTri: i << 2).
		verts[i].colour = (u8)(i << 2);
		verts[i].s = 0;
		verts[i].t = 0;
	}

	for (i = 0; i < PROC_NUMCOLS; i++) {
		cols[i].word = PD_BE32(palette[i]);
	}

	// --- display list ---
	// Untextured, vertex-coloured, no culling, z-buffered opaque.
	gDPPipeSync(gdl++);
	gSPClearGeometryMode(gdl++, G_CULL_BOTH | G_LIGHTING | G_TEXTURE_GEN | G_FOG);
	gSPSetGeometryMode(gdl++, G_ZBUFFER | G_SHADE | G_SHADING_SMOOTH);
	gDPSetCycleType(gdl++, G_CYC_1CYCLE);
	gDPSetTextureLUT(gdl++, G_TT_NONE);
	gSPTexture(gdl++, 0, 0, 0, G_TX_RENDERTILE, G_OFF);
	gDPSetCombineMode(gdl++, G_CC_SHADE, G_CC_SHADE);
	gDPSetRenderMode(gdl++, G_RM_AA_ZB_OPA_SURF, G_RM_AA_ZB_OPA_SURF2);

	// SEGMENTED addresses, and they MUST be marked with SEGADDR (bit 0 set).
	// fast3d's seg_addr() only resolves against the segment table when the low bit is
	// set, and otherwise uses the word AS A RAW POINTER (gfx_pc.cpp:2262). Passing a
	// bare (segment << 24) therefore hands the renderer 0x0e000000 to dereference,
	// which is exactly how this first failed. The offset field is masked 0x00fffffe,
	// so segment-relative offsets must also be even.
	gSPColor(gdl++, SEGADDR(SPSEGMENT_BG_COL << 24), PROC_NUMCOLS);
	gSPVertex(gdl++, SEGADDR(SPSEGMENT_BG_VTX << 24), PROC_NUMVERTS, 0);

	for (i = 0; i < PROC_NUMTRIS; i += 4) {
		gSPTri4(gdl++,
				tris[i + 0][0], tris[i + 0][1], tris[i + 0][2],
				tris[i + 1][0], tris[i + 1][1], tris[i + 1][2],
				tris[i + 2][0], tris[i + 2][1], tris[i + 2][2],
				tris[i + 3][0], tris[i + 3][1], tris[i + 3][2]);
	}

	gDPPipeSync(gdl++);
	gSPEndDisplayList(gdl++);

	opa->type = ROOMBLOCKTYPE_LEAF;
	opa->next = NULL;
	opa->gdl = gdlstart;
	opa->vertices = verts;
	opa->colours = cols;

	g->vertices = verts;
	g->colours = cols;
	g->opablocks = opa;
	g->xlublocks = NULL;   // nothing translucent in W1
	g->lightsindex = -1;
	g->numlights = 0;
	g->numvertices = PROC_NUMVERTS;
	g->numcolours = PROC_NUMCOLS;

	g_Rooms[roomnum].gfxdata = g;
	g_Rooms[roomnum].loaded240 = 1;

#ifndef PLATFORM_N64
	sysLogPrintf(LOG_NOTE, "bgprocedural: built room %d, %d verts %d tris, gdl %d cmds, %u bytes",
			roomnum, PROC_NUMVERTS, PROC_NUMTRIS, (s32)(gdl - gdlstart), total);
#endif
}

/**
 * Procedural rooms are permanently resident: there is no file to stream back in, so
 * unloading one would leave a permanently empty room.
 */
bool bgProceduralKeepRoomLoaded(s32 roomnum)
{
	return roomnum == PROC_ROOM;
}
