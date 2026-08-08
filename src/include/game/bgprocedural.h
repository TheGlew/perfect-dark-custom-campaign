#ifndef IN_GAME_BGPROCEDURAL_H
#define IN_GAME_BGPROCEDURAL_H
#include <ultra64.h>
#include "data.h"
#include "types.h"

bool bgIsProceduralStage(s32 stagenum);
void bgProceduralReset(s32 stagenum);
void bgProceduralBuildRoomMetrics(void);
void bgProceduralLoadRoom(s32 roomnum);

#endif
