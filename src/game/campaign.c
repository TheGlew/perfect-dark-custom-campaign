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
#include <ultra64.h>
#include "constants.h"
#include "bss.h"
#include "data.h"
#include "types.h"
#include "game/campaign.h"
#include "game/bondgun.h"
#include "game/challenge.h"
#include "game/cheats.h"
#include "game/chraction.h"
#include "game/filemgr.h"
#include "game/inv.h"
#include "game/lv.h"
#include "game/objectives.h"
#include "game/pdmode.h"
#include "game/player.h"
#include "game/playermgr.h"
#include "game/playerreset.h"
#include "game/stagetable.h"
#include "game/title.h"
#include "lib/main.h"
#include "lib/vi.h"
#include "fs.h"
#include "system.h"

s32 g_CampaignContinuous = 0;
s32 g_CampaignCarryInventory = 1;
s32 g_CampaignCarryHealth = 1;
s32 g_CampaignRetryOnDeath = 1;
s32 g_CampaignSkipCutscenes = 0;
s32 g_CampaignDifficulty = 0;
s32 g_CampaignDebugAutoComplete = 0;
s32 g_CampaignDebugAutoDie = 0;
char g_CampaignHandoffFile[512] = "";

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

static struct campaignloadout s_Carry;      // what the next stage starts with
static struct campaignloadout s_StageStart; // what the current stage started with (for retry)
static bool s_RestorePending = false;       // s_Carry applies to the next stage start
static bool s_ShieldPending = false;
static f32 s_ShieldValue = 0;
static bool s_DebugCompleteFired = false;
static bool s_DebugDieFired = false;
static bool s_DebugForceComplete = false;
static bool s_HandoffApplied = false;

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
 * Read another game's final loadout. One item per line:
 *   weapon <name> <ammo>    a GoldenEye item name and that ammo type's total
 *   health <0..1>
 *   armour <0..1>
 * GoldenEye's guns map onto the Perfect Dark weapons they became.
 */
struct campaignhandoffmap {
	const char *name;
	s32 weapon;
};

static const struct campaignhandoffmap s_HandoffMap[] = {
	{ "ITEM_KNIFE",         WEAPON_COMBATKNIFE },
	{ "ITEM_THROWKNIFE",    WEAPON_COMBATKNIFE },
	{ "ITEM_WPPK",          WEAPON_PP9I },
	{ "ITEM_WPPKSIL",       WEAPON_PP9I },
	{ "ITEM_SILVERWPPK",    WEAPON_PP9I },
	{ "ITEM_GOLDWPPK",      WEAPON_PP9I },
	{ "ITEM_TT33",          WEAPON_CC13 },
	{ "ITEM_SKORPION",      WEAPON_KL01313 },
	{ "ITEM_AK47",          WEAPON_KF7SPECIAL },
	{ "ITEM_UZI",           WEAPON_ZZT },
	{ "ITEM_MP5K",          WEAPON_DMC },
	{ "ITEM_MP5KSIL",       WEAPON_DMC },
	{ "ITEM_SPECTRE",       WEAPON_CYCLONE },
	{ "ITEM_M16",           WEAPON_AR53 },
	{ "ITEM_FNP90",         WEAPON_RCP45 },
	{ "ITEM_SHOTGUN",       WEAPON_SHOTGUN },
	{ "ITEM_AUTOSHOT",      WEAPON_SHOTGUN },
	{ "ITEM_SNIPERRIFLE",   WEAPON_SNIPERRIFLE },
	{ "ITEM_RUGER",         WEAPON_DY357MAGNUM },
	{ "ITEM_GOLDENGUN",     WEAPON_DY357LX },
	{ "ITEM_LASER",         WEAPON_LASER },
	{ "ITEM_WATCHLASER",    WEAPON_LASER },
	{ "ITEM_GRENADELAUNCH", WEAPON_DEVASTATOR },
	{ "ITEM_ROCKETLAUNCH",  WEAPON_ROCKETLAUNCHER },
	{ "ITEM_GRENADE",       WEAPON_GRENADE },
	{ "ITEM_TIMEDMINE",     WEAPON_TIMEDMINE },
	{ "ITEM_PROXIMITYMINE", WEAPON_PROXIMITYMINE },
	{ "ITEM_REMOTEMINE",    WEAPON_REMOTEMINE },
	{ NULL, 0 },
};

static bool campaignLoadHandoff(const char *path, struct campaignloadout *out)
{
	FILE *fp = fopen(path, "r");
	char line[256];
	s32 mapped = 0;
	s32 skipped = 0;

	if (fp == NULL) {
		sysLogPrintf(LOG_WARNING, "campaign: handoff file not found: %s", path);
		return false;
	}

	memset(out, 0, sizeof(*out));
	out->valid = true;
	out->health = 1;

	while (fgets(line, sizeof(line), fp)) {
		char key[32];
		char name[64];
		s32 ammo = 0;
		f32 frac = 0;

		if (sscanf(line, "%31s %63s %d", key, name, &ammo) >= 2 && strcmp(key, "weapon") == 0) {
			const struct campaignhandoffmap *m;
			s32 weapon = -1;

			for (m = s_HandoffMap; m->name; m++) {
				if (strcmp(m->name, name) == 0) {
					weapon = m->weapon;
					break;
				}
			}

			if (weapon < 0) {
				skipped++;
				continue;
			}

			if (out->numweapons < CAMPAIGN_MAX_WEAPONS) {
				s32 i;
				bool dupe = false;

				for (i = 0; i < out->numweapons; i++) {
					if (out->weapon1[i] == weapon) {
						dupe = true;
					}
				}

				if (!dupe) {
					out->weapon1[out->numweapons] = weapon;
					out->weapon2[out->numweapons] = -1;
					out->numweapons++;
				}

				{
					s32 ammotype = bgunGetAmmoTypeForWeapon(weapon, FUNC_PRIMARY);

					if (ammotype > 0 && ammotype < CAMPAIGN_NUM_AMMOTYPES && ammo > out->ammo[ammotype]) {
						out->ammo[ammotype] = ammo;
					}
				}

				mapped++;
			}
		} else if (sscanf(line, "%31s %f", key, &frac) == 2 && strcmp(key, "health") == 0) {
			out->health = frac;
		} else if (sscanf(line, "%31s %f", key, &frac) == 2 && strcmp(key, "armour") == 0) {
			out->shield = frac;
		}
	}

	fclose(fp);

	sysLogPrintf(LOG_NOTE, "campaign: handoff %s: %d weapons mapped, %d without an equivalent", path, mapped, skipped);

	return true;
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

static void campaignWriteMarker(const char *name)
{
	char path[600];
	FILE *fp;

	snprintf(path, sizeof(path), "$S/%s", name);
	fp = fopen(fsFullPath(path), "w");

	if (fp) {
		fprintf(fp, "difficulty %d\n", g_MissionConfig.difficulty);
		fclose(fp);
	}
}

void campaignBootInit(s32 *stagenum)
{
	const char *handoff;

	if (sysArgCheck("--campaign")) {
		g_CampaignContinuous = 1;

		if (*stagenum == STAGE_TITLE) {
			*stagenum = STAGE_DEFECTION;
		}

		g_MissionConfig.difficulty = g_CampaignDifficulty;
		lvSetDifficulty(g_CampaignDifficulty);
	}

	handoff = sysArgGetString("--handoff");

	if (handoff) {
		strncpy(g_CampaignHandoffFile, handoff, sizeof(g_CampaignHandoffFile) - 1);
	}

	if (g_CampaignContinuous) {
		s32 idx = campaignSoloIndex(*stagenum);

		if (idx >= 0) {
			g_MissionConfig.stageindex = idx;
			g_MissionConfig.stagenum = *stagenum;
		}

		sysLogPrintf(LOG_NOTE, "campaign: continuous on, boot stage 0x%02x (solo index %d), difficulty %d, carry inventory %d health %d, retry on death %d, skip cutscenes %d%s%s",
				*stagenum, idx, g_MissionConfig.difficulty, g_CampaignCarryInventory, g_CampaignCarryHealth,
				g_CampaignRetryOnDeath, g_CampaignSkipCutscenes,
				g_CampaignHandoffFile[0] ? ", handoff " : "", g_CampaignHandoffFile);
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
		campaignRecordCompletion(idx);

		if (idx >= CAMPAIGN_LAST_STAGEINDEX) {
			sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x (solo %d) complete -> credits; campaign complete", g_Vars.stagenum, idx);
			campaignWriteMarker("campaign-complete.txt");
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
	// with what the stage started with.
	s_Carry = s_StageStart;
	s_RestorePending = s_StageStart.valid;
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

	if (s_RestorePending && s_Carry.valid) {
		campaignRestore(&s_Carry);
		campaignLogLoadout("restored", &s_Carry);
	} else if (!s_HandoffApplied && g_CampaignHandoffFile[0]) {
		struct campaignloadout handoff;

		s_HandoffApplied = true;

		if (campaignLoadHandoff(g_CampaignHandoffFile, &handoff)) {
			campaignRestore(&handoff);
			campaignLogLoadout("handoff applied", &handoff);
		}
	}

	s_RestorePending = false;

	campaignSnapshot(&s_StageStart);
	s_StageStart.shield = s_ShieldPending ? s_ShieldValue : s_StageStart.shield;
	campaignLogLoadout("stage start", &s_StageStart);

	sysLogPrintf(LOG_NOTE, "campaign: stage 0x%02x (solo %d) started, difficulty %d", g_Vars.stagenum, idx, lvGetDifficulty());
}

/**
 * Called every frame from lvTick.
 */
void campaignTick(void)
{
	struct player *player;
	s32 elapsed;

	if (!g_CampaignContinuous || !campaignIsSolo() || campaignSoloIndex(g_Vars.stagenum) < 0) {
		return;
	}

	player = g_Vars.bond ? g_Vars.bond : g_Vars.currentplayer;

	if (player == NULL) {
		return;
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
#endif
