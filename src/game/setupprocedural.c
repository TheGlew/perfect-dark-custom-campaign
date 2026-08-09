/**
 * setupprocedural.c - own the stage SETUP in memory, instead of loading it from a file.
 *
 * Sibling of bgprocedural.c, and the same trick one layer up: bgprocedural.c replaces the
 * bg file that describes a level's GEOMETRY, this replaces the setup file that describes
 * its CONTENTS (props, objectives, the player spawn, AI).
 *
 * Why this is possible at all: struct stagesetup (types.h:2865) is eight pointers, and
 * g_StageSetup is a global INSTANCE of it, not a pointer into the loaded file. The stock
 * loader's whole job is to fill those eight fields in (setup.c:1327-1338). So owning the
 * setup means populating a global, not synthesising a file format.
 *
 * Two facts make the data itself cheap, and both are easy to get wrong in the other
 * direction:
 *
 *   - THE PC PORT CONVERTS N64 SETUP DATA TO NATIVE STRUCTS AT LOAD
 *     (port/src/preprocess/filesetup.c maps struct n64_defaultobj -> struct defaultobj).
 *     Data we build ourselves is therefore plain native C. No byte swapping, and no
 *     file-relative offsets to promote into pointers.
 *
 *   - A COMMAND'S TYPE LIVES IN BYTE 3 OF ITS FIRST WORD, not byte 0. defaultobj is
 *     { u16 extrascale; u8 hidden2; u8 type; } (types.h:1464-1467), and setupGetCmdLength
 *     reads it as (u8)PD_BE32(cmd[0]) (setuputils.c:27), which is a portable way of saying
 *     "byte 3". Writing the struct field directly, as this file does, agrees with both
 *     readers on either endianness. Hand-assembling command words does not.
 *
 * Three hazards this file exists to avoid, all of them silent:
 *
 *   1. PROP PARSING MUTATES ITS INPUT. setupCreateProps rewrites CAMERAPOS ints into floats
 *      in place (setup.c:2060-2064), marks lift doors (setup.c:1477) and stores obj->prop
 *      back into the array. A static template handed straight to the engine is therefore
 *      corrupted by the FIRST load and misparsed by the second. So the template is const
 *      and a fresh copy is made per load. Same failure class as the W1 residency bug:
 *      fine on load one, broken on restart.
 *
 *   2. THE AILIST SORT READS PAST A SHORT ARRAY. setup.c:1367 iterates on
 *      ailists[i + 1].list with no NULL check on the array itself, and setup.c:1381
 *      dereferences it again to count. A NULL or single-entry array reads out of bounds.
 *      Hence TWO zeroed terminators below, which is cheaper than patching the engine.
 *
 *   3. A SETUP WITH NO SPAWN POINTS IS UNSAFE. With g_NumSpawnPoints == 0 the spawn block
 *      is skipped entirely (playerreset.c:402-414), which leaves that function's local
 *      rooms[8] UNINITIALISED when it reaches cdFindGroundInfoAtCyl (playerreset.c:416).
 *      So the intro list always carries a real INTROCMD_SPAWN, even in the minimal setup.
 *
 * What this file deliberately does NOT own: the pads file and the language bank. Pads stay
 * a real file load (setup.c:1334) because the spawn is a PAD and a pad's room is derived
 * from geometry at load (setuppads.c:52-77); the language bank stays a real load because
 * text is a bank id, not a string.
 */
#include <ultra64.h>
#include <string.h>
#include "constants.h"
#include "lang.h"
#include "game/setup.h"
#include "game/setupprocedural.h"
#include "bss.h"
#include "data.h"
#include "types.h"
#include "lib/memp.h"

/**
 * The spawn pad.
 *
 * mklevel.py emits pads in authoring order and the level description keeps the spawn pad
 * first, so this is 0 by construction rather than by luck. It is a named constant because
 * a bare 0 in an intro list is unreadable and would shift silently if a pad were ever
 * inserted ahead of it.
 */
#define PROC_SPAWN_PAD 0

/**
 * The intro command list: where the player starts.
 *
 * INTROCMD_SPAWN's param1 is a PAD NUMBER, pushed into g_SpawnPoints at
 * playerreset.c:169-173 and resolved for position AND room by playerChooseSpawnLocation
 * (player.c:219, player.c:256). param2 == 0 means "this spawn applies", per the
 * cmd->param2 test at playerreset.c:170.
 *
 * Unlike props, intro commands are plain native s32 words with the type in word 0
 * (player.c:606 switches on cmd[0] directly). Two different conventions in one struct.
 */
static const s32 g_ProcIntroTemplate[] = {
	INTROCMD_SPAWN, PROC_SPAWN_PAD, 0,
	INTROCMD_END,
};

/**
 * The mission: one objective, completed by being in a room.
 *
 * OBJECTIVETYPE_ENTERROOM's field is NAMED `pad` (types.h:4346-4351) but a value below
 * 10000 is used AS A ROOM NUMBER: chrGetPadRoom returns it unchanged and only treats it
 * as a pad at 10000 and above (chraction.c:14118-14139). So this needs no pad, and the
 * field name is the trap.
 */
#define PROC_OBJECTIVE_ROOM         1
#define PROC_OBJECTIVE_INDEX        0
#define PROC_OBJECTIVE_TEXT         L_AME_016
#define PROC_OBJECTIVE_DIFFICULTIES (DIFFBIT_A | DIFFBIT_SA | DIFFBIT_PA | DIFFBIT_PD)

/**
 * Set a props command's TYPE.
 *
 * The type is byte 3 of the command's first word, not byte 0. That is not a quirk of this
 * file: struct defaultobj declares { u16 extrascale; u8 hidden2; u8 type; }
 * (types.h:1464-1467), and the port's own preprocessor copies that byte VERBATIM while
 * byte-swapping everything around it (convertDefaultObjHdr, filesetup.c:148-166). Writing
 * byte 3 directly therefore agrees with both readers, on either endianness, where
 * assembling a whole word by hand would be correct on exactly one.
 */
static void procSetCmdType(void *cmd, u8 type)
{
	((u8 *)cmd)[3] = type;
}

/**
 * Two terminators, never one, and never NULL. See hazard 2 in the file header.
 */
static struct ailist g_ProcAilists[2];

bool setupIsProceduralStage(s32 stagenum)
{
	return stagenum == STAGE_DEFECTION;
}

/**
 * Stand in for the setup file load.
 *
 * Populates g_StageSetup with real pointers. The caller keeps the pads load, the language
 * load, the global-ailist sort and the whole prop-counting tail, all of which operate on
 * whatever this leaves behind.
 */
void setupProceduralLoad(s32 stagenum)
{
	struct objective *objective;
	struct criteria_roomentered *enterroom;
	u32 *props;
	u32 *p;
	s32 propwords;
	s32 *intro;
	s32 i;

	// The props list is BUILT FRESH each load rather than copied from a template, which
	// is a stronger form of the same guarantee hazard 1 asks for: there is no master
	// copy for the engine's in-place rewrites to corrupt.
	//
	// Command sizes come from the same sizeof arithmetic setupGetCmdLength uses to WALK
	// them (setuputils.c:19-60), so the layout cannot drift from the walker. Writing the
	// commands as a packed C struct would look tidier and would break the moment the
	// compiler inserted padding, because criteria_roomentered carries a pointer and is
	// therefore 8-byte aligned on this target.
	propwords = sizeof(struct objective) / sizeof(u32)
			+ sizeof(struct criteria_roomentered) / sizeof(u32)
			+ 1   // ENDOBJECTIVE
			+ 1;  // END

	props = mempAlloc(ALIGN16(propwords * sizeof(u32)), MEMPOOL_STAGE);
	memset(props, 0, propwords * sizeof(u32));

	p = props;

	objective = (struct objective *)p;
	procSetCmdType(objective, OBJTYPE_BEGINOBJECTIVE);
	objective->index = PROC_OBJECTIVE_INDEX;
	objective->text = PROC_OBJECTIVE_TEXT;
	objective->difficulties = PROC_OBJECTIVE_DIFFICULTIES;
	p += sizeof(struct objective) / sizeof(u32);

	enterroom = (struct criteria_roomentered *)p;
	procSetCmdType(enterroom, OBJECTIVETYPE_ENTERROOM);
	enterroom->pad = PROC_OBJECTIVE_ROOM;
	enterroom->status = OBJECTIVE_INCOMPLETE;
	enterroom->next = NULL;
	p += sizeof(struct criteria_roomentered) / sizeof(u32);

	procSetCmdType(p, OBJTYPE_ENDOBJECTIVE);
	p += 1;

	procSetCmdType(p, OBJTYPE_END);

	// The intro list is copied for the same reason, even though nothing is known to
	// write to it: the cost is a few bytes and the alternative is discovering the
	// exception on a restart.
	intro = mempAlloc(ALIGN16(sizeof(g_ProcIntroTemplate)), MEMPOOL_STAGE);
	memcpy(intro, g_ProcIntroTemplate, sizeof(g_ProcIntroTemplate));

	for (i = 0; i < 2; i++) {
		g_ProcAilists[i].list = NULL;
		g_ProcAilists[i].id = 0;
	}

	g_StageSetup.props = (u32 *)props;
	g_StageSetup.intro = intro;
	g_StageSetup.ailists = g_ProcAilists;
	g_StageSetup.paths = NULL;

	// waypoints, waygroups and cover are left exactly as the stock path leaves them
	// (setup.c:1336-1338): NULL here, then pointed into the pads file by
	// setupPreparePads (setuppads.c:95-97). Setting them here would pre-empt that.
	g_StageSetup.waypoints = NULL;
	g_StageSetup.waygroups = NULL;
	g_StageSetup.cover = NULL;

	// g_GeCreditsData is the raw setup file buffer. Nothing we emit reads it (only
	// INTROCMD_CREDITOFFSET does, playerreset.c:239), and leaving a pointer from a
	// previous stage's freed pool is a dangling read waiting for a future step to find.
	g_GeCreditsData = NULL;
}
