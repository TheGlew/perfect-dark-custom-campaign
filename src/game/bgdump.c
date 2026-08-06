/**
 * bgdump.c - W1 reference instrumentation.
 *
 * Dumps the live in-memory room state of a normally-loaded stage so that a
 * procedurally built room can be compared against it field for field. This is
 * the "measure the working case first" gate: a procedural room that renders as
 * nothing is almost never a geometry bug, it is a missing field in one of the
 * structures below.
 *
 * Enabled with Debug.DumpRooms=1 in pd.ini. Writes one text file per stage load
 * into the working directory and then disarms itself.
 *
 * This file is instrumentation only. Nothing in the game reads it, and it is
 * safe to leave compiled in (the dump costs one branch per tick when disabled).
 */
#include <ultra64.h>
#include <stdio.h>
#include "constants.h"
#include "game/bg.h"
#include "game/bgdump.h"
#include "bss.h"
#include "data.h"
#include "types.h"

#ifndef PLATFORM_N64
#include "system.h"
#endif

// Registered as Debug.DumpRooms. 0 = off.
s32 g_BgDumpRooms = 0;

// Number of ticks to wait after a stage becomes live before dumping. Rooms are
// streamed in by bgLoadRoom AFTER bgBuildTables returns, so dumping too early
// captures gfxdata == NULL for every room and proves nothing.
#define BGDUMP_DELAY_TICKS 90

static s32 g_BgDumpTicks = 0;
static s32 g_BgDumpStage = -1;

extern struct drawslotpointer *g_BgDrawSlotsByRoom;
extern struct portalcamcacheitem *g_PortalCameraCache;
extern struct portalmetric *g_PortalMetrics;
extern u8 *g_BgPortalAlphas;
extern struct bgcmd *g_BgCommands;
extern u8 *g_BgLightsFileData;
extern f32 *g_BgStanThings;

/**
 * Walk one roomblock chain, printing the tree shape and the leaf payloads.
 * Depth is bounded so a corrupt "next" pointer cannot hang the dump.
 */
static void bgDumpBlockChain(FILE *fp, struct roomblock *block, s32 depth, const char *label)
{
	s32 guard = 0;

	if (block == NULL) {
		fprintf(fp, "      %s: NULL\n", label);
		return;
	}

	fprintf(fp, "      %s:\n", label);

	while (block != NULL && guard < 256 && depth < 8) {
		fprintf(fp, "        %*sblock %p type=%u next=%p\n",
				depth * 2, "", (void *)block, block->type, (void *)block->next);

		if (block->type == 0) {
			fprintf(fp, "        %*s  leaf gdl=%p vertices=%p colours=%p\n",
					depth * 2, "", (void *)block->gdl,
					(void *)block->vertices, (void *)block->colours);

			// First few GBI command words, so the procedural display list can be
			// compared against a real one command for command.
			if (block->gdl != NULL) {
				s32 w;
				fprintf(fp, "        %*s  gdl words:", depth * 2, "");
				for (w = 0; w < 8; w++) {
					fprintf(fp, " %08x %08x",
							(u32)block->gdl[w].words.w0, (u32)block->gdl[w].words.w1);
				}
				fprintf(fp, "\n");
			}
		} else {
			fprintf(fp, "        %*s  parent child=%p unk0c=%p\n",
					depth * 2, "", (void *)block->child, (void *)block->unk0c);
			bgDumpBlockChain(fp, block->child, depth + 1, "child");
		}

		block = block->next;
		guard++;
	}
}

void bgDumpRoomState(const char *tag)
{
	FILE *fp;
	char path[256];
	s32 i;

	sprintf(path, "pd-roomdump-stage%02x.txt", (s32)g_Vars.stagenum);

	fp = fopen(path, "w");
	if (fp == NULL) {
		return;
	}

	fprintf(fp, "=== PD room state dump (%s) ===\n", tag);
	fprintf(fp, "stagenum       = 0x%02x\n", (s32)g_Vars.stagenum);
	fprintf(fp, "roomcount      = %d\n", (s32)g_Vars.roomcount);
	fprintf(fp, "mplayerrunning = %d\n", (s32)g_Vars.mplayerisrunning);
	fprintf(fp, "roomportalrecursionlimit = %d\n", (s32)g_Vars.roomportalrecursionlimit);
	fprintf(fp, "\n");

	fprintf(fp, "globals:\n");
	fprintf(fp, "  g_Rooms             = %p\n", (void *)g_Rooms);
	fprintf(fp, "  g_BgRooms           = %p\n", (void *)g_BgRooms);
	fprintf(fp, "  g_BgPortals         = %p\n", (void *)g_BgPortals);
	fprintf(fp, "  g_RoomPortals       = %p\n", (void *)g_RoomPortals);
	fprintf(fp, "  g_BgDrawSlotsByRoom = %p\n", (void *)g_BgDrawSlotsByRoom);
	fprintf(fp, "  g_MpRoomVisibility  = %p\n", (void *)g_MpRoomVisibility);
	fprintf(fp, "  g_PortalCameraCache = %p\n", (void *)g_PortalCameraCache);
	fprintf(fp, "  g_PortalMetrics     = %p\n", (void *)g_PortalMetrics);
	fprintf(fp, "  g_BgPortalAlphas    = %p\n", (void *)g_BgPortalAlphas);
	fprintf(fp, "  g_BgCommands        = %p\n", (void *)g_BgCommands);
	fprintf(fp, "  g_BgLightsFileData  = %p\n", (void *)g_BgLightsFileData);
	fprintf(fp, "  g_BgStanThings      = %p\n", (void *)g_BgStanThings);
	fprintf(fp, "\n");

	// Portal table. Terminator is verticesoffset == 0.
	fprintf(fp, "portals:\n");
	if (g_BgPortals != NULL) {
		for (i = 0; g_BgPortals[i].verticesoffset != 0 && i < 4096; i++) {
			fprintf(fp, "  portal %3d: voff=%5u room1=%3d room2=%3d flags=0x%02x alpha=%3u\n",
					i, g_BgPortals[i].verticesoffset,
					g_BgPortals[i].roomnum1, g_BgPortals[i].roomnum2,
					g_BgPortals[i].flags,
					g_BgPortalAlphas ? g_BgPortalAlphas[i] : 0);
		}
		if (i == 0) {
			fprintf(fp, "  (none)\n");
		}
	} else {
		fprintf(fp, "  g_BgPortals is NULL\n");
	}
	fprintf(fp, "\n");

	// NOTE room 0 is a sentinel and is deliberately dumped so the difference
	// between it and a real room is visible in the reference.
	for (i = 0; i < g_Vars.roomcount; i++) {
		struct room *r = &g_Rooms[i];

		fprintf(fp, "room %d%s:\n", i, i == 0 ? "  (SENTINEL)" : "");
		fprintf(fp, "  flags=0x%04x loaded240=%d numportals=%d numlights=%d numwaypoints=%d\n",
				r->flags, r->loaded240, r->numportals, r->numlights, r->numwaypoints);
		fprintf(fp, "  lightindex=%d firstwaypoint=%u roomportallistoffset=%d roommtxindex=%d\n",
				(s16)r->lightindex, r->firstwaypoint, r->roomportallistoffset, r->roommtxindex);
		fprintf(fp, "  bbmin=(%.3f, %.3f, %.3f) bbmax=(%.3f, %.3f, %.3f)\n",
				r->bbmin[0], r->bbmin[1], r->bbmin[2],
				r->bbmax[0], r->bbmax[1], r->bbmax[2]);
		fprintf(fp, "  centre=(%.3f, %.3f, %.3f) radius=%.3f\n",
				r->centre.x, r->centre.y, r->centre.z, r->radius);
		fprintf(fp, "  gfxdata=%p gfxdatalen=%d numvtxbatches=%d vtxbatches=%p\n",
				(void *)r->gfxdata, r->gfxdatalen, r->numvtxbatches, (void *)r->vtxbatches);
		fprintf(fp, "  br: min=%u max=%u each=%u base=%u settled_local=%d settled_regional=%u flash=%d lightop=%u\n",
				r->br_light_min, r->br_light_max, r->br_light_each, r->br_base,
				r->br_settled_local, r->br_settled_regional, r->br_flash, r->lightop);

		if (g_BgRooms != NULL) {
			fprintf(fp, "  bgroom: unk00=0x%08x pos=(%.3f, %.3f, %.3f) br=(%u..%u)\n",
					(u32)g_BgRooms[i].unk00,
					g_BgRooms[i].pos.x, g_BgRooms[i].pos.y, g_BgRooms[i].pos.z,
					g_BgRooms[i].br_light_min, g_BgRooms[i].br_light_max);
		}

		if (g_BgDrawSlotsByRoom != NULL) {
			fprintf(fp, "  drawslot: updatedframe=%u slotnum=%u\n",
					g_BgDrawSlotsByRoom[i].updatedframe, g_BgDrawSlotsByRoom[i].slotnum);
		}

		if (g_MpRoomVisibility != NULL) {
			fprintf(fp, "  mpvisibility=0x%02x\n", g_MpRoomVisibility[i]);
		}

		// This room's portal numbers, read through roomportallistoffset.
		if (r->numportals > 0 && g_RoomPortals != NULL) {
			s32 p;
			fprintf(fp, "  portallist:");
			for (p = 0; p < r->numportals; p++) {
				fprintf(fp, " %d", g_RoomPortals[r->roomportallistoffset + p]);
			}
			fprintf(fp, "\n");
		}

		if (r->gfxdata != NULL) {
			struct roomgfxdata *g = r->gfxdata;
			fprintf(fp, "    roomgfxdata @ %p:\n", (void *)g);
			fprintf(fp, "      vertices=%p colours=%p numvertices=%d numcolours=%d\n",
					(void *)g->vertices, (void *)g->colours, g->numvertices, g->numcolours);
			fprintf(fp, "      lightsindex=%d numlights=%d opablocks=%p xlublocks=%p\n",
					g->lightsindex, g->numlights,
					(void *)g->opablocks, (void *)g->xlublocks);

			// First few vertices, so procedural Vtx packing can be compared.
			// NOTE this port redefines Vtx as a compact 12-byte struct
			// (x,y,z,flags,colour,s,t), NOT the N64 union with .v.ob. The
			// colour field is an INDEX into the room's Col array, not RGBA.
			if (g->vertices != NULL && g->numvertices > 0) {
				s32 v;
				s32 n = g->numvertices < 8 ? g->numvertices : 8;
				for (v = 0; v < n; v++) {
					fprintf(fp, "      vtx[%d] = (%d, %d, %d) flags=0x%02x colour=%u st=(%d, %d)\n",
							v,
							g->vertices[v].x, g->vertices[v].y, g->vertices[v].z,
							g->vertices[v].flags, g->vertices[v].colour,
							g->vertices[v].s, g->vertices[v].t);
				}
			}

			// Colour table entries referenced by Vtx.colour.
			if (g->colours != NULL && g->numcolours > 0) {
				s32 c;
				s32 n = g->numcolours < 6 ? g->numcolours : 6;
				for (c = 0; c < n; c++) {
					fprintf(fp, "      col[%d] = 0x%08x\n", c, *(u32 *)&g->colours[c]);
				}
			}

			bgDumpBlockChain(fp, g->opablocks, 0, "opablocks");
			bgDumpBlockChain(fp, g->xlublocks, 0, "xlublocks");
		}

		fprintf(fp, "\n");
	}

	fclose(fp);

#ifndef PLATFORM_N64
	sysLogPrintf(LOG_NOTE, "bgdump: wrote %s (roomcount=%d)", path, (s32)g_Vars.roomcount);
#endif
}

/**
 * Called once per tick from bgTick. Fires the dump once per stage, a fixed
 * number of ticks after the stage changed, by which point the rooms the player
 * is standing in have been streamed in.
 */
void bgDumpTick(void)
{
	if (!g_BgDumpRooms) {
		return;
	}

	if (g_Vars.stagenum != g_BgDumpStage) {
		g_BgDumpStage = g_Vars.stagenum;
		g_BgDumpTicks = 0;
		return;
	}

	if (g_BgDumpTicks < 0) {
		return; // already dumped for this stage
	}

	g_BgDumpTicks++;

	if (g_BgDumpTicks >= BGDUMP_DELAY_TICKS && g_Vars.roomcount > 0 && g_Rooms != NULL) {
		bgDumpRoomState("post-load");
		g_BgDumpTicks = -1;
	}
}
