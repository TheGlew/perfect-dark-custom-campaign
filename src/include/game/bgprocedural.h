#ifndef IN_GAME_BGPROCEDURAL_H
#define IN_GAME_BGPROCEDURAL_H
#include <ultra64.h>
#include "data.h"
#include "types.h"

/**
 * The contract between tools/mklevel/mklevel.py and bgprocedural.c.
 *
 * The transpiler emits static geometry DATA in these shapes, one generated file per level
 * (src/generated/<level>_geom.inc.c), and bgprocedural.c turns them into display lists at
 * RUNTIME. Data, never Gfx: texture pointers cannot be resolved until texLoadFromTextureNum
 * has run, so baking display lists at transpile time would force a segment-patching pass
 * later. Changing either struct means changing the emitter in the same commit.
 */

/**
 * One vertex, ROOM-RELATIVE.
 *
 * Room-relative, not world: the engine offsets a room's vertices by g_BgRooms[r].pos, while
 * COLLISION tiles are world space. That asymmetry is the whole reason the transpiler exists,
 * and it is why these values look nothing like the level description's coordinates.
 * s16 because that is what Vtx holds.
 */
struct procvtx {
	s16 x;
	s16 y;
	s16 z;
};

/**
 * One room's geometry.
 *
 * origin is world space and becomes g_BgRooms[index].pos; bbmin/bbmax are world space and
 * become g_Rooms[index].bbmin/bbmax. The vertices are relative to origin.
 */
struct procroom {
	s32 index;
	f32 origin[3];
	f32 bbmin[3];
	f32 bbmax[3];
	const struct procvtx *vertices;
	s32 numverts;
	const u8 (*tris)[3];
	s32 numtris;
};

bool bgIsProceduralStage(s32 stagenum);
void bgProceduralReset(s32 stagenum);
void bgProceduralBuildRoomMetrics(void);
void bgProceduralLoadRoom(s32 roomnum);

#endif
