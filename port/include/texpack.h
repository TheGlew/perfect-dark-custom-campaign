#ifndef PORT_TEXPACK_H
#define PORT_TEXPACK_H

/*
 * HD texture packs (PC port only).
 *
 * The game inflates every texture it uses into a texture pool (texLoad) and
 * registers it here by texture number. When fast3d imports a texture for the
 * GPU it asks this module for the identity of the source bytes: a registered
 * texture is named t<num> (its texture number), anything else (fonts, HUD
 * images, dynamic textures) h<hash> (a hash of the exact bytes and palette).
 *
 * Textures.Dump=1 writes every texture the game draws, plus every texture a
 * stage loads (the census), as <dumpdir>/<name>.png at the original size.
 * Textures.Pack=<dir> replaces a texture at upload time with <dir>/<name>.png
 * when it exists, at whatever size the PNG has; texture coordinates are
 * normalised against the original tile size, so a 4x PNG just works.
 */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

extern char g_TexpackDir[512];   /* pd.ini Textures.Pack; "" turns replacement off */
extern char g_TexpackDumpDir[512];
extern int  g_TexpackDump;       /* pd.ini Textures.Dump */

/* game side */
void texpackReset(void);
void texpackRegister(int texturenum, const void *data, uint32_t bytes,
                     int width, int height, int gbifmt, int siz, int lutmode,
                     uint32_t rowbytes, uint32_t paletteoffset, int palettecount);
void texpackCensus(void);
void texpackLogStats(void); /* logs how many drawn textures came from the pack, when the count changed */

/* renderer side (fast3d) */
void texpackImportBegin(const uint8_t *addr, uint32_t bytes, int fmt, int siz,
                        const uint16_t *palette, int palettecount);
void texpackImportEnd(void);
/* Returns a replacement RGBA32 image to upload instead of rgba (free it with
 * texpackRelease), or NULL to upload rgba as is. Dumps rgba when dumping. */
const uint8_t *texpackResolve(const uint8_t *rgba, uint32_t w, uint32_t h, uint32_t *outw, uint32_t *outh);
void texpackRelease(const uint8_t *replacement);

#ifdef __cplusplus
}
#endif

#endif
