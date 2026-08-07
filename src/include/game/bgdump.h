#ifndef IN_GAME_BGDUMP_H
#define IN_GAME_BGDUMP_H
#include <ultra64.h>
#include "data.h"
#include "types.h"

// Debug.DumpRooms config var. 0 = off.
extern s32 g_BgDumpRooms;

// Debug.TracePlayer config var. 0 = off.
extern s32 g_BgTracePlayer;

void bgDumpRoomState(const char *tag);
void bgDumpTick(void);
void bgTracePlayerTick(void);

#endif
