#ifndef PLATFORM_N64
/**
 * Continuous campaign (PC port only). See campaign.h for what it does.
 *
 * Hooks:
 *   mainEndStage (port/src/pdmain.c)  -> campaignHandleStageEnd
 *   lvReset player loop (src/game/lv.c) -> campaignOnPlayerReset
 *   lvTick (src/game/lv.c)             -> campaignTick
 *   main (port/src/main.c)             -> campaignBootInit, config keys
 *
 * The chain itself reuses the engine's own direct-to-stage path: the same
 * calls endscreenContinue makes when Deep Sea rolls into Defense.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <ultra64.h>
#include "constants.h"
#include "bss.h"
#include "data.h"
#include "types.h"
#include "game/campaign.h"
#include "game/setup.h"
#include "game/bondgun.h"
#include "game/challenge.h"
#include "game/cheats.h"
#include "game/chraction.h"
#include "game/filemgr.h"
#include "game/inv.h"
#include "game/lv.h"
#include "game/objectives.h"
#include "game/pad.h"
#include "game/pdmode.h"
#include "game/chrai.h"
#include "game/env.h"
#include "game/music.h"
#include "game/lang.h"
#include "game/player.h"
#include "game/playermgr.h"
#include "game/playerreset.h"
#include "game/stagetable.h"
#include "game/title.h"
#include "lib/main.h"
#include "lib/vi.h"
#include "system.h"

s32 g_CampaignContinuous = 0;
s32 g_CampaignCarryInventory = 1;
s32 g_CampaignCarryHealth = 1;
s32 g_CampaignRetryOnDeath = 1;
s32 g_CampaignSkipCutscenes = 0;
s32 g_CampaignDifficulty = 0;
s32 g_CampaignDebugAutoComplete = 0;
s32 g_CampaignDebugAutoDie = 0;

#define CAMPAIGN_MAX_WEAPONS 48
#define CAMPAIGN_NUM_AMMOTYPES 33
#define CAMPAIGN_MIN_HEALTH 0.25f
#define CAMPAIGN_LAST_STAGEINDEX SOLOSTAGEINDEX_SKEDARRUINS

struct campaignloadout {
	bool valid;
	s32 numweapons;
	s32 weapon1[CAMPAIGN_MAX_WEAPONS];
	s32 weapon2[CAMPAIGN_MAX_WEAPONS]; // -1 when single
	s32 ammo[CAMPAIGN_NUM_AMMOTYPES];
	f32 health;
	f32 shield;
};

static void campaignMergedReset(void);
static void campaignTickMerged(struct player *player);
static struct campaignloadout s_JointStart;
static s32 s_CheckpointLeg = 0;                 // where the next reset of this stage begins
static bool s_CheckpointPending = false;        // the checkpoint applies to the next reset (a retry, or a leg chosen from the menu)
static struct campaignloadout s_Carry;      // what the next stage starts with
static struct campaignloadout s_StageStart; // what the current stage started with (for retry)
static bool s_RestorePending = false;       // s_Carry applies to the next stage start
static bool s_ShieldPending = false;
static f32 s_ShieldValue = 0;
static bool s_DebugCompleteFired = false;
static bool s_DebugDieFired = false;
static bool s_DebugForceComplete = false;

/**
 * Only real guns and throwables carry over. Gadgets, keys, props and the
 * mission's own devices (CamSpy, Data Uplink, ...) are given by each stage.
 */
static bool campaignWeaponCarries(s32 weaponnum)
{
	switch (weaponnum) {
	case WEAPON_FALCON2:
	case WEAPON_FALCON2_SILENCER:
	case WEAPON_FALCON2_SCOPE:
	case WEAPON_MAGSEC4:
	case WEAPON_MAULER:
	case WEAPON_PHOENIX:
	case WEAPON_DY357MAGNUM:
	case WEAPON_DY357LX:
	case WEAPON_CMP150:
	case WEAPON_CYCLONE:
	case WEAPON_CALLISTO:
	case WEAPON_RCP120:
	case WEAPON_LAPTOPGUN:
	case WEAPON_DRAGON:
	case WEAPON_K7AVENGER:
	case WEAPON_AR34:
	case WEAPON_SUPERDRAGON:
	case WEAPON_SHOTGUN:
	case WEAPON_REAPER:
	case WEAPON_SNIPERRIFLE:
	case WEAPON_FARSIGHT:
	case WEAPON_DEVASTATOR:
	case WEAPON_ROCKETLAUNCHER:
	case WEAPON_SLAYER:
	case WEAPON_COMBATKNIFE:
	case WEAPON_CROSSBOW:
	case WEAPON_TRANQUILIZER:
	case WEAPON_LASER:
	case WEAPON_GRENADE:
	case WEAPON_NBOMB:
	case WEAPON_TIMEDMINE:
	case WEAPON_PROXIMITYMINE:
	case WEAPON_REMOTEMINE:
	case WEAPON_PP9I:
	case WEAPON_CC13:
	case WEAPON_KL01313:
	case WEAPON_KF7SPECIAL:
	case WEAPON_ZZT:
	case WEAPON_DMC:
	case WEAPON_AR53:
	case WEAPON_RCP45:
		return true;
	}

	return false;
}

static bool campaignIsSolo(void)
{
	return g_Vars.mplayerisrunning == false
		&& g_Vars.normmplayerisrunning == false
		&& g_Vars.coopplayernum < 0
		&& g_Vars.antiplayernum < 0;
}

/**
 * Index into g_SoloStages for a main-campaign stage, or -1. Only Defection
 * through Skedar Ruins chain; the bonus missions are left alone.
 */
static s32 campaignSoloIndex(s32 stagenum)
{
	s32 i;

	stagenum = campaignFirstLegStage(stagenum);

	for (i = 0; i <= CAMPAIGN_LAST_STAGEINDEX; i++) {
		if ((s32)g_SoloStages[i].stagenum == stagenum) {
			return i;
		}
	}

	return -1;
}

static void campaignSnapshot(struct campaignloadout *out)
{
	struct player *player = g_Vars.currentplayer;
	struct invitem *item;
	s32 i;

	memset(out, 0, sizeof(*out));
	out->valid = true;

	item = player->weapons;

	while (item && out->numweapons < CAMPAIGN_MAX_WEAPONS) {
		if (item->type == INVITEMTYPE_WEAP && campaignWeaponCarries(item->type_weap.weapon1)) {
			out->weapon1[out->numweapons] = item->type_weap.weapon1;
			out->weapon2[out->numweapons] = -1;
			out->numweapons++;
		} else if (item->type == INVITEMTYPE_DUAL && campaignWeaponCarries(item->type_dual.weapon1)) {
			out->weapon1[out->numweapons] = item->type_dual.weapon1;
			out->weapon2[out->numweapons] = item->type_dual.weapon2;
			out->numweapons++;
		}

		item = item->next;

		if (item == player->weapons) {
			break;
		}
	}

	for (i = 1; i < CAMPAIGN_NUM_AMMOTYPES; i++) {
		out->ammo[i] = bgunGetAmmoCount(i);
	}

	out->health = player->bondhealth;
	out->shield = (player->prop && player->prop->chr) ? chrGetShield(player->prop->chr) : 0;
}

static void campaignLogLoadout(const char *what, struct campaignloadout *l)
{
	char buf[512];
	s32 len = 0;
	s32 i;

	buf[0] = '\0';

	for (i = 0; i < l->numweapons && len < (s32)sizeof(buf) - 24; i++) {
		if (l->weapon2[i] >= 0) {
			len += snprintf(buf + len, sizeof(buf) - len, " %02x+%02x", l->weapon1[i], l->weapon2[i]);
		} else {
			len += snprintf(buf + len, sizeof(buf) - len, " %02x", l->weapon1[i]);
		}
	}

	sysLogPrintf(LOG_NOTE, "campaign: %s: health %.2f shield %.2f weapons[%d]%s",
			what, l->health, l->shield, l->numweapons, buf);

	buf[0] = '\0';
	len = 0;

	for (i = 1; i < CAMPAIGN_NUM_AMMOTYPES && len < (s32)sizeof(buf) - 16; i++) {
		if (l->ammo[i] > 0) {
			len += snprintf(buf + len, sizeof(buf) - len, " %02x=%d", i, l->ammo[i]);
		}
	}

	sysLogPrintf(LOG_NOTE, "campaign: %s: ammo%s", what, buf);
}

static void campaignGiveWeapon(s32 weapon1, s32 weapon2)
{
	if (weapon2 >= 0) {
		if (!invHasDoubleWeaponExcAllGuns(weapon1, weapon2)) {
			modelmgrLoadProjectileModeldefs(weapon1);
			modelmgrLoadProjectileModeldefs(weapon2);
			invGiveDoubleWeapon(weapon1, weapon2);
		}
	} else if (!invHasSingleWeaponExcAllGuns(weapon1)) {
		modelmgrLoadProjectileModeldefs(weapon1);
		invGiveSingleWeapon(weapon1);
	}
}

/**
 * Apply a saved loadout on top of what the stage's own intro handed out.
 * Weapons are added, never removed; ammo and health only ever go up to the
 * carried value, so a stage that starts you better equipped stays that way.
 */
static void campaignRestore(struct campaignloadout *l)
{
	struct player *player = g_Vars.currentplayer;
	s32 i;

	if (g_CampaignCarryInventory) {
		for (i = 0; i < l->numweapons; i++) {
			campaignGiveWeapon(l->weapon1[i], l->weapon2[i]);
		}

		for (i = 1; i < CAMPAIGN_NUM_AMMOTYPES; i++) {
			if (l->ammo[i] > bgunGetAmmoCount(i)) {
				bgunSetAmmoQuantity(i, l->ammo[i]);
			}
		}
	}

	if (g_CampaignCarryHealth) {
		f32 health = l->health;

		if (health < CAMPAIGN_MIN_HEALTH) {
			health = CAMPAIGN_MIN_HEALTH;
		}

		if (health < player->bondhealth) {
			player->bondhealth = health;
			player->oldhealth = health;
			player->apparenthealth = health;
		}

		// The player's chr does not exist yet at reset time; applied on the first tick
		s_ShieldPending = l->shield > 0;
		s_ShieldValue = l->shield;
	}
}

/**
 * The part of endscreenPrepare that records the completion in the save file:
 * best time, the next mission as the auto-selected one, the file thumbnail,
 * and whatever that unlocks. Same conditions as the end screen uses.
 */
static void campaignRecordCompletion(s32 stageindex)
{
	s32 secs;
	u16 prevbest;
	s32 d;
	s32 s;

	if (g_CheatsActiveBank0 || g_CheatsActiveBank1 || g_MissionConfig.pdmode) {
		return;
	}

	secs = playerGetMissionTime() / 60;

	if (secs > 0xfff) {
		secs = 0xfff;
	}

	if (secs == 0) {
		secs = 1;
	}

	prevbest = g_GameFile.besttimes[stageindex][g_MissionConfig.difficulty];

	if (secs < prevbest || prevbest == 0) {
		g_GameFile.besttimes[stageindex][g_MissionConfig.difficulty] = secs;
	}

	g_GameFile.autodifficulty = g_MissionConfig.difficulty;
	g_GameFile.autostageindex = stageindex + 1;

	if (g_GameFile.autostageindex > SOLOSTAGEINDEX_SKEDARRUINS) {
		g_GameFile.autostageindex = SOLOSTAGEINDEX_SKEDARRUINS;
	}

	for (d = 0; d != 3; d++) {
		for (s = 0; s <= SOLOSTAGEINDEX_SKEDARRUINS; s++) {
			if (g_GameFile.besttimes[s][d]) {
				g_GameFile.thumbnail = s + 1;
			}
		}
	}

	challengeDetermineUnlockedFeatures();
	filemgrSaveOrLoad(&g_GameFileGuid, FILEOP_SAVE_GAME_000, 0);
}

static void campaignGoToStage(s32 stagenum)
{
	g_Vars.bondplayernum = 0;
	g_Vars.coopplayernum = -1;
	g_Vars.antiplayernum = -1;
	setNumPlayers(1);

	titleSetNextStage(stagenum);
	lvSetDifficulty(g_MissionConfig.difficulty);
	titleSetNextMode(TITLEMODE_SKIP);
	mainChangeToStage(stagenum);
	viBlack(true);
}

void campaignBootInit(s32 *stagenum)
{
	if (sysArgCheck("--campaign")) {
		g_CampaignContinuous = 1;

		if (*stagenum == STAGE_TITLE) {
			*stagenum = STAGE_DEFECTION;
		}

		g_MissionConfig.difficulty = g_CampaignDifficulty;
		lvSetDifficulty(g_CampaignDifficulty);
	}

	if (g_CampaignContinuous) {
		s32 idx;

		*stagenum = campaignRedirectStage(*stagenum);
		idx = campaignSoloIndex(*stagenum);

		if (idx >= 0) {
			g_MissionConfig.stageindex = idx;
			g_MissionConfig.stagenum = *stagenum;
		}

		sysLogPrintf(LOG_NOTE, "campaign: continuous on, boot stage 0x%02x (solo index %d), difficulty %d, carry inventory %d health %d, retry on death %d, skip cutscenes %d",
				*stagenum, idx, g_MissionConfig.difficulty, g_CampaignCarryInventory, g_CampaignCarryHealth,
				g_CampaignRetryOnDeath, g_CampaignSkipCutscenes);
	}
}

/**
 * Called from mainEndStage before the end screen is pushed. Returns true when
 * the campaign took the transition over, so no end screen is shown.
 */
bool campaignHandleStageEnd(void)
{
	struct player *player;
	s32 idx;
	bool completed;

	if (!g_CampaignContinuous || !campaignIsSolo()) {
		return false;
	}

	idx = campaignSoloIndex(g_Vars.stagenum);

	if (idx < 0) {
		return false;
	}

	player = g_Vars.currentplayer;

	if (player->aborted) {
		// The player chose to leave: the normal menus take it from here
		return false;
	}

	completed = !player->isdead && (objectiveIsAllComplete() || s_DebugForceComplete);
	s_DebugForceComplete = false;

	g_MissionConfig.stageindex = idx;
	g_MissionConfig.stagenum = g_Vars.stagenum;
	g_MissionConfig.difficulty = lvGetDifficulty();

	if (completed) {
		campaignSnapshot(&s_Carry);
		campaignLogLoadout("carrying forward", &s_Carry);

		if (campaignIsMergedStage(g_Vars.stagenum)) {
			// every leg of the merged mission is complete
			s32 last = campaignSoloIndex(campaignEffectiveStage());
			s32 k;

			for (k = idx; k <= last && k >= 0; k++) {
				campaignRecordCompletion(k);
			}

			idx = last;
		} else {
			campaignRecordCompletion(idx);
		}

		s_CheckpointPending = false;

		if (idx >= CAMPAIGN_LAST_STAGEINDEX) {
			sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x (solo %d) complete -> credits; campaign complete", g_Vars.stagenum, idx);
			s_RestorePending = false;
			g_MissionConfig.stagenum = STAGE_CREDITS;
			campaignGoToStage(STAGE_CREDITS);
			return true;
		}

		g_MissionConfig.stageindex = idx + 1;
		g_MissionConfig.stagenum = g_SoloStages[idx + 1].stagenum;
		s_RestorePending = true;

		sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x (solo %d) complete -> next stage 0x%02x (solo %d)",
				g_Vars.stagenum, idx, g_MissionConfig.stagenum, idx + 1);

		campaignGoToStage(g_MissionConfig.stagenum);
		return true;
	}

	if (!g_CampaignRetryOnDeath) {
		return false;
	}

	// Death, or the exit taken with an objective failed: straight back in,
	// with what the stage started with; in a merged mission, at the current
	// leg's joint with what was held there.
	if (campaignIsMergedStage(g_Vars.stagenum) && g_CampaignLeg > 0) {
		s_Carry = s_JointStart;
		s_RestorePending = s_JointStart.valid;
		s_CheckpointLeg = g_CampaignLeg;
		s_CheckpointPending = true;
	} else {
		s_Carry = s_StageStart;
		s_RestorePending = s_StageStart.valid;
		s_CheckpointPending = false;
	}

	g_Vars.restartlevel = true;

	sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x (solo %d) %s -> retry", g_Vars.stagenum, idx,
			player->isdead ? "failed (dead)" : "failed (objectives)");

	campaignGoToStage(g_Vars.stagenum);
	return true;
}

/**
 * Called from lvReset after playerReset has handed out the stage's intro
 * weapons, for the solo player.
 */
void campaignOnPlayerReset(void)
{
	s32 idx;

	s_ShieldPending = false;
	s_DebugCompleteFired = false;
	s_DebugForceComplete = false;

	if (!g_CampaignContinuous || !campaignIsSolo()) {
		s_RestorePending = false;
		return;
	}

	idx = campaignSoloIndex(g_Vars.stagenum);

	if (idx < 0) {
		s_RestorePending = false;
		return;
	}

	campaignMergedReset();

	if (s_RestorePending && s_Carry.valid) {
		campaignRestore(&s_Carry);
		campaignLogLoadout("restored", &s_Carry);
	}

	s_RestorePending = false;

	campaignSnapshot(&s_StageStart);
	s_StageStart.shield = s_ShieldPending ? s_ShieldValue : s_StageStart.shield;
	campaignLogLoadout("stage start", &s_StageStart);

	sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x (solo %d) started, difficulty %d", g_Vars.stagenum, idx, lvGetDifficulty());
}

/**
 * Move the player to a pad: position, room and facing, through the same path
 * the AI's chr_move_to_pad takes. The joints between a merged mission's legs
 * use this; so does the PD_TESTWARP development switch.
 */
bool campaignWarpPlayerToPad(s32 padnum)
{
	struct player *player = g_Vars.bond ? g_Vars.bond : g_Vars.currentplayer;
	struct pad pad;
	RoomNum rooms[2];
	f32 theta;
	bool ok;

	if (player == NULL || player->prop == NULL || player->prop->chr == NULL) {
		return false;
	}

	padUnpack(padnum, PADFIELD_POS | PADFIELD_LOOK | PADFIELD_ROOM, &pad);
	rooms[0] = pad.room;
	rooms[1] = -1;
	theta = atan2f(pad.look.x, pad.look.z);
	ok = chrMoveToPos(player->prop->chr, &pad.pos, rooms, theta, true);

	sysLogPrintf(LOG_NOTE, "campaign: warp to pad %d (%.0f, %.0f, %.0f) room %d: %s; player now in room %d",
			padnum, pad.pos.x, pad.pos.y, pad.pos.z, pad.room, ok ? "ok" : "FAILED", player->prop->rooms[0]);

	return ok;
}

/**
 * PD_TESTWARP="<seconds>:<pad>" warps the player that many seconds into the
 * stage. A development switch for checking geometry a level never reaches.
 */
static void campaignTickTestWarp(s32 elapsed)
{
	static s32 secs = -2; // -2 unparsed, -1 off
	static s32 pad = -1;
	static bool fired = false;

	if (secs == -2) {
		const char *spec = getenv("PD_TESTWARP");
		secs = -1;

		if (spec && sscanf(spec, "%d:%d", &secs, &pad) != 2) {
			secs = -1;
		}
	}

	if (secs >= 0 && !fired && elapsed >= secs * 60) {
		fired = true;
		campaignWarpPlayerToPad(pad);
	}
}

/**
 * PD_TESTRESTART="<seconds>" restarts the mission that many seconds in, the
 * way the pause menu's Restart Mission does (mainmenu.c menuhandlerAcceptMission).
 */
static void campaignTickTestRestart(s32 elapsed)
{
	static s32 secs = -2;
	static bool fired = false;

	if (secs == -2) {
		const char *spec = getenv("PD_TESTRESTART");
		secs = -1;

		if (spec && sscanf(spec, "%d", &secs) != 1) {
			secs = -1;
		}
	}

	if (secs >= 0 && !fired && elapsed >= secs * 60) {
		fired = true;
		sysLogPrintf(LOG_NOTE, "campaign: test: restart mission after %d seconds", secs);
		g_Vars.restartlevel = true;
		mainChangeToStage(mainGetStageNum());
	}
}

/**
 * Called every frame from lvTick.
 */
void campaignTick(void)
{
	struct player *player;
	s32 elapsed;

	if (!campaignIsSolo() || campaignSoloIndex(g_Vars.stagenum) < 0) {
		return;
	}

	player = g_Vars.bond ? g_Vars.bond : g_Vars.currentplayer;

	if (player == NULL) {
		return;
	}

	if (!g_CampaignContinuous) {
		// the stock game; only the soak's forced completion applies, so a stock
		// level can be seen ending through its own end screen
		if (g_CampaignDebugAutoComplete > 0 && !s_DebugCompleteFired && !g_MainIsEndscreen && !player->isdead
				&& lvGetStageTime60() >= g_CampaignDebugAutoComplete * 60) {
			s_DebugCompleteFired = true;
			sysLogPrintf(LOG_NOTE, "campaign: debug: forcing stock stage 0x%02x complete after %d seconds", g_Vars.stagenum, g_CampaignDebugAutoComplete);
			func0000e990();
		}

		return;
	}

	if (!g_MainIsEndscreen && !player->isdead) {
		campaignTickTestWarp(lvGetStageTime60());
		campaignTickTestRestart(lvGetStageTime60());
		campaignTickMerged(player);
	}

	if (s_ShieldPending && player->prop && player->prop->chr) {
		if (chrGetShield(player->prop->chr) < s_ShieldValue) {
			chrSetShield(player->prop->chr, s_ShieldValue);
		}

		s_ShieldPending = false;
	}

	if (g_CampaignSkipCutscenes) {
		if (g_Vars.in_cutscene || g_Vars.tickmode == TICKMODE_CUTSCENE) {
			g_CutsceneSkipRequested = true;
		}

		if (g_Vars.autocutplaying) {
			g_Vars.autocutgroupskip = true;
		}
	}

	if (g_MainIsEndscreen || player->isdead) {
		return;
	}

	elapsed = lvGetStageTime60();

	if (g_CampaignDebugAutoDie > 0 && !s_DebugDieFired && elapsed >= g_CampaignDebugAutoDie * 60) {
		s_DebugDieFired = true;
		sysLogPrintf(LOG_NOTE, "campaign: debug: killing the player after %d seconds", g_CampaignDebugAutoDie);
		playerDie(true);
		return;
	}

	if (g_CampaignDebugAutoComplete > 0 && !s_DebugCompleteFired && elapsed >= g_CampaignDebugAutoComplete * 60) {
		s_DebugCompleteFired = true;
		s_DebugForceComplete = true;
		sysLogPrintf(LOG_NOTE, "campaign: debug: forcing stage 0x%02x complete after %d seconds", g_Vars.stagenum, g_CampaignDebugAutoComplete);
		func0000e990();
	}
}

/*
 * ---------------------------------------------------------------------------
 * Merged missions
 * ---------------------------------------------------------------------------
 */

s32 g_CampaignLeg = 0;
s32 g_CampaignDebugAutoJoint = 0;

#define CAMPAIGN_MAX_LEGS 4

struct campaignmission {
	s32 stagenum;
	s32 numlegs;
	const struct campaignleg *legs;
};

#include "merged/legs_a51.inc.c"
#include "merged/legs_dd.inc.c"
#include "merged/legs_chi.inc.c"

static const struct campaignmission g_CampaignMissions[] = {
	{ STAGE_MERGED_DD,  ARRAYCOUNT(g_CampaignLegs_dd),  g_CampaignLegs_dd  },
	{ STAGE_MERGED_CHI, ARRAYCOUNT(g_CampaignLegs_chi), g_CampaignLegs_chi },
	{ STAGE_MERGED_A51, ARRAYCOUNT(g_CampaignLegs_a51), g_CampaignLegs_a51 },
};

static u32 s_LegFlagWords[CAMPAIGN_MAX_LEGS];   // each leg's stage flags; the current leg's live in g_StageFlags
static u32 s_ForcedObjectives = 0;              // bit per objective: complete for good (a finished leg's)
static s32 s_PendingWarpPad = -1;               // a joint's teleport, waiting for the white screen
static s32 s_LegStartTime60 = 0;
static s32 s_ResumeLeg = 0;                     // joints to run on the first tick after a reset
static bool s_WarpWaitLogged = false;

static const struct campaignmission *campaignMissionForStage(s32 stagenum)
{
	s32 i;

	for (i = 0; i < ARRAYCOUNT(g_CampaignMissions); i++) {
		if (g_CampaignMissions[i].stagenum == stagenum) {
			return &g_CampaignMissions[i];
		}
	}

	return NULL;
}

static const struct campaignmission *campaignCurrentMission(void)
{
	return campaignMissionForStage(g_Vars.stagenum);
}

bool campaignIsMergedStage(s32 stagenum)
{
	return campaignMissionForStage(stagenum) != NULL;
}

/**
 * The level the game should behave as: the current leg's, in a merged mission.
 */
s32 campaignEffectiveStage(void)
{
	const struct campaignmission *mission = campaignCurrentMission();

	if (mission && g_CampaignLeg >= 0 && g_CampaignLeg < mission->numlegs) {
		return mission->legs[g_CampaignLeg].stagenum;
	}

	return g_Vars.stagenum;
}

bool campaignStageIs(s32 stagenum)
{
	return g_Vars.stagenum == stagenum || campaignEffectiveStage() == stagenum;
}

/**
 * An object tag the engine names by number for a level (the EyeSpy's, say) is
 * that number plus the leg's tag base in a merged mission.
 */
s32 campaignLegTag(s32 tagnum)
{
	const struct campaignmission *mission = campaignCurrentMission();

	if (mission && g_CampaignLeg >= 0 && g_CampaignLeg < mission->numlegs) {
		return tagnum + mission->legs[g_CampaignLeg].tagbase;
	}

	return tagnum;
}

/**
 * The characters of a merged mission's later legs, with whatever is assigned
 * to them, are left out at stage load and created at their leg's joint: three
 * levels' guards do not share one building from the first second. This is
 * the first character number stage load leaves out; nothing elsewhere.
 */
s32 campaignDeferredChrBase(void)
{
	const struct campaignmission *mission = campaignCurrentMission();

	if (mission && mission->numlegs > 1) {
		return mission->legs[1].chrbase;
	}

	return 0x10000;
}

/**
 * An objective of a leg the mission has not reached is incomplete whatever
 * the world says: its characters and items are not there yet.
 */
bool campaignObjectivePending(s32 index)
{
	const struct campaignmission *mission = campaignCurrentMission();

	if (mission && g_CampaignLeg + 1 < mission->numlegs) {
		return index >= mission->legs[g_CampaignLeg + 1].objfirst;
	}

	return false;
}

/**
 * Move a position authored in the current leg's level (a cutscene camera's)
 * to where that level's world sits in the merged one.
 */
void campaignLegOffset(struct coord *pos)
{
	const struct campaignmission *mission = campaignCurrentMission();

	if (mission && g_CampaignLeg >= 0 && g_CampaignLeg < mission->numlegs) {
		const struct campaignleg *leg = &mission->legs[g_CampaignLeg];
		pos->x += leg->offset[0];
		pos->y += leg->offset[1];
		pos->z += leg->offset[2];
	}
}

/**
 * For a merged stage, the current leg's level; anything else unchanged. For
 * per-level tables (music, environment, weather, bodies).
 */
s32 campaignLegStage(s32 stagenum)
{
	if (stagenum == g_Vars.stagenum && campaignIsMergedStage(stagenum)) {
		return campaignEffectiveStage();
	}

	return stagenum;
}

/**
 * For a merged stage, its first leg's level; anything else unchanged. For what
 * is decided once per stage load (the language bank, the memory layout).
 */
s32 campaignFirstLegStage(s32 stagenum)
{
	const struct campaignmission *mission = campaignMissionForStage(stagenum);

	return mission ? mission->legs[0].stagenum : stagenum;
}

/**
 * A level that is a leg of a merged mission is played as that mission, from
 * that leg: the campaign's own chain, --boot-stage and the mission select all
 * come through here (mainChangeToStage). Returns the stage to load.
 */
s32 campaignRedirectStage(s32 stagenum)
{
	s32 i;
	s32 j;

	if (!g_CampaignContinuous) {
		return stagenum;
	}

	// g_Vars is not set up at boot (varsInit runs later); the co-op and
	// counter-op fields are -1 once it is, and 0 before, so test for players
	// actually running
	if (g_Vars.normmplayerisrunning || g_Vars.mplayerisrunning
			|| (g_Vars.coopplayernum > 0) || (g_Vars.antiplayernum > 0)) {
		return stagenum;
	}

	for (i = 0; i < ARRAYCOUNT(g_CampaignMissions); i++) {
		for (j = 0; j < g_CampaignMissions[i].numlegs; j++) {
			if (g_CampaignMissions[i].legs[j].stagenum == stagenum) {
				if (!s_CheckpointPending) {
					s_CheckpointLeg = j;
					s_CheckpointPending = true;
				}

				sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x is leg %d of merged stage 0x%02x; loading that from leg %d",
						stagenum, j, g_CampaignMissions[i].stagenum, s_CheckpointLeg);
				return g_CampaignMissions[i].stagenum;
			}
		}
	}

	return stagenum;
}

/**
 * A merged stage's objectives are every leg's; the ones of a finished leg stay
 * complete whatever the world says, and each leg's flag requirements read that
 * leg's flag word.
 */
static const struct campaignleg *campaignLegForObjective(s32 index, s32 *legnum)
{
	const struct campaignmission *mission = campaignCurrentMission();
	s32 i;

	if (mission) {
		for (i = 0; i < mission->numlegs; i++) {
			if (index >= mission->legs[i].objfirst && index < mission->legs[i].objfirst + mission->legs[i].objcount) {
				*legnum = i;
				return &mission->legs[i];
			}
		}
	}

	return NULL;
}

static u32 campaignFlagWord(s32 leg)
{
	if (leg == g_CampaignLeg) {
		return g_StageFlags;
	}

	if (leg >= 0 && leg < CAMPAIGN_MAX_LEGS) {
		return s_LegFlagWords[leg];
	}

	return 0;
}

static void campaignSetFlagWord(s32 leg, u32 value)
{
	if (leg == g_CampaignLeg) {
		g_StageFlags = value;
	} else if (leg >= 0 && leg < CAMPAIGN_MAX_LEGS) {
		s_LegFlagWords[leg] = value;
	}
}

u32 campaignObjectiveFlags(s32 index)
{
	s32 legnum;

	if (campaignLegForObjective(index, &legnum)) {
		return campaignFlagWord(legnum);
	}

	return g_StageFlags;
}

bool campaignObjectiveForced(s32 index)
{
	return index >= 0 && index < 32 && (s_ForcedObjectives & (1u << index)) != 0;
}

/**
 * Hand over to a leg: the finished legs' objectives stay complete, the leg's
 * flag word comes in, the player gets the leg's own kit, the level's own music
 * and sky apply, and the player stands at the leg's start. With fade, the
 * teleport happens behind the game's white teleport screen.
 */
void campaignJoint(s32 leg, bool fade)
{
	const struct campaignmission *mission = campaignCurrentMission();
	const struct campaignleg *to;
	struct player *player = g_Vars.bond ? g_Vars.bond : g_Vars.currentplayer;
	s32 i;
	s32 j;

	if (mission == NULL || leg <= g_CampaignLeg || leg >= mission->numlegs) {
		sysLogPrintf(LOG_NOTE, "campaign: joint to leg %d ignored (current leg %d)", leg, g_CampaignLeg);
		return;
	}

	to = &mission->legs[leg];

	// objectives of the legs behind us are done for good
	for (i = 0; i < leg; i++) {
		for (j = 0; j < mission->legs[i].objcount; j++) {
			s_ForcedObjectives |= 1u << (mission->legs[i].objfirst + j);
		}
	}

	// park the finished leg's flags, bring in the new leg's
	s_LegFlagWords[g_CampaignLeg] = g_StageFlags;
	g_StageFlags = s_LegFlagWords[leg];
	g_CampaignLeg = leg;
	s_LegStartTime60 = lvGetStageTime60();

	// the leg's own characters, and what they carry
	{
		s32 hi = leg + 1 < mission->numlegs ? mission->legs[leg + 1].chrbase : 0x10000;
		s32 created = setupCreateDeferredProps(to->chrbase, hi);

		sysLogPrintf(LOG_NOTE, "campaign: leg %d: %d characters created (numbers %d-%d)", leg, created, to->chrbase, hi - 1);
	}

	// the leg's kit, on top of what is carried
	for (i = 0; i < to->numweapons; i++) {
		campaignGiveWeapon(to->weapons[i], -1);
	}

	for (i = 0; i < to->numammo; i++) {
		if (to->ammo[i].qty > bgunGetAmmoCount(to->ammo[i].type)) {
			bgunSetAmmoQuantity(to->ammo[i].type, to->ammo[i].qty);
		}
	}

	// the level's own sky and music
	envChooseAndApply(to->stagenum, true);
	musicSetStageAndStartMusic(to->stagenum);

	if (fade && player && player->prop) {
		s_PendingWarpPad = to->spawnpad;
		player->teleportstate = TELEPORTSTATE_PREENTER;
		player->teleporttime = 0;
		player->teleportpad = to->spawnpad;
		player->teleportcamerapad = to->spawnpad;
	} else {
		campaignWarpPlayerToPad(to->spawnpad);
	}

	objectivesCheckAll();
	campaignSnapshot(&s_JointStart);
	campaignLogLoadout("leg start", &s_JointStart);

	sysLogPrintf(LOG_NOTE, "campaign: joint -> leg %d (stage 0x%02x) of merged stage 0x%02x, objectives %d-%d, spawn pad %d, %s",
			leg, to->stagenum, g_Vars.stagenum, to->objfirst, to->objfirst + to->objcount - 1, to->spawnpad,
			fade ? "fading" : "placed");
}

/**
 * Called from setupLoadFiles after the stage's own language bank: a merged
 * mission's text comes from every leg's bank.
 */
void campaignLoadLangBanks(s32 stagenum)
{
	const struct campaignmission *mission = campaignMissionForStage(stagenum);
	s32 i;

	if (mission) {
		for (i = 1; i < mission->numlegs; i++) {
			langLoad(langGetLangBankIndexFromStagenum(mission->legs[i].stagenum));
		}
	}
}

/**
 * At a stage reset: back to leg 0 with nothing forced, then up to the
 * checkpoint when one is pending (a retry after death, or a leg chosen from
 * the menu or --boot-stage).
 */
static void campaignMergedReset(void)
{
	const struct campaignmission *mission = campaignCurrentMission();
	s32 i;

	g_CampaignLeg = 0;
	s_ForcedObjectives = 0;
	s_PendingWarpPad = -1;
	s_LegStartTime60 = 0;

	for (i = 0; i < CAMPAIGN_MAX_LEGS; i++) {
		s_LegFlagWords[i] = 0;
	}

	if (mission == NULL) {
		s_CheckpointPending = false;
		return;
	}

	// The checkpoint's joints run on the first tick (campaignTickMerged): the
	// player's chr does not exist yet at reset time, and a joint moves it.
	if (s_CheckpointPending && s_CheckpointLeg > 0 && s_CheckpointLeg < mission->numlegs) {
		sysLogPrintf(LOG_NOTE, "campaign: merged stage 0x%02x resumes at leg %d", g_Vars.stagenum, s_CheckpointLeg);
		s_ResumeLeg = s_CheckpointLeg;
	} else {
		s_ResumeLeg = 0;
	}

	s_CheckpointPending = false;
	campaignSnapshot(&s_JointStart);
}

static void campaignTickMerged(struct player *player)
{
	const struct campaignmission *mission = campaignCurrentMission();

	if (mission == NULL) {
		return;
	}

	if (s_ResumeLeg > 0 && player->prop && player->prop->chr && player->prop->chr->model && player->haschrbody) {
		s32 i;

		for (i = 1; i <= s_ResumeLeg; i++) {
			campaignJoint(i, false);
		}

		s_ResumeLeg = 0;

		// the kit held at the joint, over what the resumed legs handed out
		if (s_Carry.valid) {
			campaignRestore(&s_Carry);
			campaignLogLoadout("restored at the joint", &s_Carry);
		}

		campaignSnapshot(&s_JointStart);
	}

	if (s_PendingWarpPad >= 0 && player->teleportstate == TELEPORTSTATE_WHITE) {
		// the white screen waits for us; the body may be away for a moment
		// (a cutscene the new leg's intro starts, or its end), and moving a
		// bodiless player is a crash
		if (player->prop && player->prop->chr && player->prop->chr->model && player->haschrbody) {
			campaignWarpPlayerToPad(s_PendingWarpPad);
			s_PendingWarpPad = -1;
			player->teleportstate = TELEPORTSTATE_EXITING;
			player->teleporttime = 0;
		} else if (!s_WarpWaitLogged) {
			s_WarpWaitLogged = true;
			sysLogPrintf(LOG_NOTE, "campaign: warp to pad %d waits: body %d model %d cutscene %d tickmode %d", s_PendingWarpPad,
					player->haschrbody, player->prop && player->prop->chr && player->prop->chr->model ? 1 : 0,
					g_Vars.in_cutscene, g_Vars.tickmode);
		}
	} else {
		s_WarpWaitLogged = false;
	}

	if (g_CampaignDebugAutoJoint > 0 && s_PendingWarpPad < 0 && g_CampaignLeg + 1 < mission->numlegs
			&& lvGetStageTime60() - s_LegStartTime60 >= g_CampaignDebugAutoJoint * 60) {
		sysLogPrintf(LOG_NOTE, "campaign: debug: forcing the joint to leg %d after %d seconds", g_CampaignLeg + 1, g_CampaignDebugAutoJoint);
		campaignJoint(g_CampaignLeg + 1, true);
	}
}

/*
 * The AI commands. Bytecode: two opcode bytes, then the operands listed in
 * commands.h.
 */

static u32 campaignReadWord(u8 *cmd)
{
	return (cmd[0] << 24) | (cmd[1] << 16) | (cmd[2] << 8) | cmd[3];
}

bool aiSetStageFlagW(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;
	campaignSetFlagWord(cmd[2], campaignFlagWord(cmd[2]) | campaignReadWord(cmd + 3));
	g_Vars.aioffset += 7;
	return false;
}

bool aiUnsetStageFlagW(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;
	campaignSetFlagWord(cmd[2], campaignFlagWord(cmd[2]) & ~campaignReadWord(cmd + 3));
	g_Vars.aioffset += 7;
	return false;
}

bool aiIfStageFlagWEq(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;
	bool has = (campaignFlagWord(cmd[2]) & campaignReadWord(cmd + 3)) != 0;

	if ((has && cmd[7] == 1) || (!has && cmd[7] == 0)) {
		g_Vars.aioffset = chraiGoToLabel(g_Vars.ailist, g_Vars.aioffset, cmd[8]);
	} else {
		g_Vars.aioffset += 9;
	}

	return false;
}

bool aiIfObjectivesRangeComplete(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;
	s32 i;
	bool all = true;

	for (i = cmd[2]; i <= cmd[3]; i++) {
		if ((objectiveGetDifficultyBits(i) & (1 << lvGetDifficulty())) && objectiveCheck(i) != OBJECTIVE_COMPLETE) {
			all = false;
			break;
		}
	}

	if (all) {
		g_Vars.aioffset = chraiGoToLabel(g_Vars.ailist, g_Vars.aioffset, cmd[4]);
	} else {
		g_Vars.aioffset += 5;
	}

	return false;
}

bool aiCampaignJoint(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;
	campaignJoint(cmd[2], true);
	g_Vars.aioffset += 3;
	return false;
}

bool aiIfCampaignLegGe(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;

	if (g_CampaignLeg >= cmd[2]) {
		g_Vars.aioffset = chraiGoToLabel(g_Vars.ailist, g_Vars.aioffset, cmd[3]);
	} else {
		g_Vars.aioffset += 4;
	}

	return false;
}

bool aiIfCampaignLegEq(void)
{
	u8 *cmd = g_Vars.ailist + g_Vars.aioffset;

	if (g_CampaignLeg == cmd[2]) {
		g_Vars.aioffset = chraiGoToLabel(g_Vars.ailist, g_Vars.aioffset, cmd[3]);
	} else {
		g_Vars.aioffset += 4;
	}

	return false;
}

#endif
