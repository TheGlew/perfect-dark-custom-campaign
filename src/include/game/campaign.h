#ifndef IN_GAME_CAMPAIGN_H
#define IN_GAME_CAMPAIGN_H
#include <ultra64.h>
#include "data.h"
#include "types.h"

/**
 * Continuous campaign (PC port only).
 *
 * With Campaign.Continuous on, a completed solo mission rolls straight into
 * the next one: no end screen, no "Next Mission" menu. Joanna keeps her guns,
 * ammo, health and shield between missions, and dying restarts the current
 * mission directly instead of showing the failure screen.
 *
 * Nothing here runs in Combat Simulator, co-op or counter-op, and nothing runs
 * at all while Campaign.Continuous is off.
 */

// pd.ini [Campaign]
extern s32 g_CampaignContinuous;
extern s32 g_CampaignCarryInventory;
extern s32 g_CampaignCarryHealth;
extern s32 g_CampaignRetryOnDeath;
extern s32 g_CampaignSkipCutscenes;
extern s32 g_CampaignDifficulty;

// pd.ini [Debug] - test scaffolding for the automated soak, never on by default
extern s32 g_CampaignDebugAutoComplete; // seconds into a stage before it is force-completed
extern s32 g_CampaignDebugAutoDie;      // seconds into the first stage before Joanna is killed once

void campaignBootInit(s32 *stagenum);
bool campaignHandleStageEnd(void);
void campaignOnPlayerReset(void);
void campaignTick(void);

#endif
