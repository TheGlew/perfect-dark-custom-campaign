/*
 * HD texture packs: identity registry, dump and replacement. See texpack.h.
 */
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <unordered_set>
#include <sys/stat.h>

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb/stb_image.h"
#include "stb/stb_image_write.h"

#include <PR/gbi.h>
#include "texpack.h"

extern "C" {
#include "fs.h"
#include "system.h"
}

char g_TexpackDir[512] = "$E/textures-hd";  // next to the executable
char g_TexpackDumpDir[512] = "$E/texdump";
int  g_TexpackDump = 0;

struct TexpackEntry {
    int num;
    const uint8_t *data;
    uint32_t bytes;
    int width, height, fmt, siz, lut;
    uint32_t rowbytes;
    uint32_t paletteoffset;
    int palettecount;
    bool dumped;
};

static std::vector<TexpackEntry> s_Entries;
static std::unordered_set<std::string> s_Missing;   /* names with no pack file */
static std::unordered_set<std::string> s_Replaced;  /* names drawn from the pack */
static std::unordered_set<std::string> s_Dumped;    /* names already written this run */
static bool s_DirsReady = false;
static std::string s_PackPath;
static std::string s_DumpPath;

/* current import, set by texpackImportBegin */
static const uint8_t *s_CurAddr = NULL;
static uint32_t s_CurBytes = 0;
static int s_CurFmt = 0, s_CurSiz = 0;
static uint16_t s_CurPalette[256];
static int s_CurPaletteCount = 0;
static bool s_CurActive = false;

static void texpackPrepareDirs(void)
{
    if (s_DirsReady) {
        return;
    }

    s_DirsReady = true;
    s_PackPath = g_TexpackDir[0] ? fsFullPath(g_TexpackDir) : "";
    s_DumpPath = fsFullPath(g_TexpackDumpDir);

    if (g_TexpackDump) {
        fsCreateDir(g_TexpackDumpDir);
    }

    sysLogPrintf(LOG_NOTE, "texpack: pack '%s' dump %d -> '%s'", s_PackPath.c_str(), g_TexpackDump, s_DumpPath.c_str());
}

static bool texpackFileExists(const std::string &path)
{
    struct stat st;
    return stat(path.c_str(), &st) == 0;
}

void texpackReset(void)
{
    s_Entries.clear();
}

void texpackRegister(int texturenum, const void *data, uint32_t bytes,
                     int width, int height, int gbifmt, int siz, int lutmode,
                     uint32_t rowbytes, uint32_t paletteoffset, int palettecount)
{
    const uint8_t *start = (const uint8_t *)data;
    const uint8_t *end = start + bytes;

    /* memory is reused between stages and pools: anything overlapping is dead */
    for (size_t i = 0; i < s_Entries.size();) {
        const TexpackEntry &e = s_Entries[i];
        if (e.data < end && start < e.data + e.bytes) {
            s_Entries[i] = s_Entries.back();
            s_Entries.pop_back();
        } else {
            i++;
        }
    }

    TexpackEntry e;
    e.num = texturenum;
    e.data = start;
    e.bytes = bytes;
    e.width = width;
    e.height = height;
    e.fmt = gbifmt;
    e.siz = siz;
    e.lut = lutmode;
    e.rowbytes = rowbytes;
    e.paletteoffset = paletteoffset;
    e.palettecount = palettecount;
    e.dumped = false;
    s_Entries.push_back(e);
}

static const TexpackEntry *texpackFind(const uint8_t *addr)
{
    for (const TexpackEntry &e : s_Entries) {
        if (addr >= e.data && addr < e.data + e.bytes) {
            return &e;
        }
    }

    return NULL;
}

static uint64_t texpackHash(const uint8_t *bytes, uint32_t n, int fmt, int siz, const uint16_t *pal, int palcount)
{
    uint64_t h = 1469598103934665603ULL;

    for (uint32_t i = 0; i < n; i++) {
        h ^= bytes[i];
        h *= 1099511628211ULL;
    }

    h ^= (uint64_t)fmt << 8 | (uint64_t)siz;
    h *= 1099511628211ULL;

    for (int i = 0; i < palcount; i++) {
        h ^= pal[i];
        h *= 1099511628211ULL;
    }

    return h;
}

void texpackImportBegin(const uint8_t *addr, uint32_t bytes, int fmt, int siz,
                        const uint16_t *palette, int palettecount)
{
    s_CurAddr = addr;
    s_CurBytes = bytes;
    s_CurFmt = fmt;
    s_CurSiz = siz;
    s_CurPaletteCount = 0;

    if (palette && palettecount > 0) {
        if (palettecount > 256) {
            palettecount = 256;
        }

        memcpy(s_CurPalette, palette, palettecount * sizeof(uint16_t));
        s_CurPaletteCount = palettecount;
    }

    s_CurActive = true;
}

void texpackImportEnd(void)
{
    s_CurActive = false;
}

/* The identity of the current import, or false when it has none (sub-tile loads). */
static bool texpackCurrentName(std::string &name, uint32_t w, uint32_t h)
{
    char buf[64];

    if (!s_CurActive || s_CurAddr == NULL || s_CurBytes == 0) {
        return false;
    }

    const TexpackEntry *e = texpackFind(s_CurAddr);

    if (e) {
        /* only the whole base image carries the texture number; a window into
         * it, or a mip level, keeps its own hash so nothing is misreplaced */
        if (s_CurAddr == e->data && w >= (uint32_t)e->width && h == (uint32_t)e->height) {
            snprintf(buf, sizeof(buf), "t%04d", e->num);
            name = buf;
            return true;
        }
    }

    if (s_CurBytes > 65536) {
        return false; /* framebuffer-sized sources are not textures */
    }

    snprintf(buf, sizeof(buf), "h%016llx", (unsigned long long)texpackHash(s_CurAddr, s_CurBytes, s_CurFmt, s_CurSiz, s_CurPalette, s_CurPaletteCount));
    name = buf;
    return true;
}

static std::unordered_set<std::string> s_CensusWritten;  /* census output not yet confirmed by the renderer */

/* fromRenderer: the image is exactly what the GPU got, so it overrides a
 * census decode of the same texture written earlier this run */
static void texpackDumpImage(const std::string &name, const uint8_t *rgba, uint32_t w, uint32_t h, bool fromRenderer)
{
    std::string path = s_DumpPath + "/" + name + ".png";

    if (s_Dumped.count(name)) {
        if (!fromRenderer || !s_CensusWritten.count(name)) {
            return;
        }

        s_CensusWritten.erase(name);
    } else {
        s_Dumped.insert(name);

        if (texpackFileExists(path)) {
            return;
        }

        if (!fromRenderer) {
            s_CensusWritten.insert(name);
        }
    }

    if (!stbi_write_png(path.c_str(), (int)w, (int)h, 4, rgba, (int)w * 4)) {
        sysLogPrintf(LOG_WARNING, "texpack: could not write %s", path.c_str());
    }
}

const uint8_t *texpackResolve(const uint8_t *rgba, uint32_t w, uint32_t h, uint32_t *outw, uint32_t *outh)
{
    std::string name;

    texpackPrepareDirs();

    if (w == 0 || h == 0 || !texpackCurrentName(name, w, h)) {
        return NULL;
    }

    if (g_TexpackDump) {
        texpackDumpImage(name, rgba, w, h, true);
    }

    if (s_PackPath.empty() || s_Missing.count(name)) {
        return NULL;
    }

    std::string path = s_PackPath + "/" + name + ".png";

    if (!texpackFileExists(path)) {
        s_Missing.insert(name);
        return NULL;
    }

    int rw = 0, rh = 0, rn = 0;
    uint8_t *rep = stbi_load(path.c_str(), &rw, &rh, &rn, 4);

    if (rep == NULL || rw <= 0 || rh <= 0) {
        sysLogPrintf(LOG_WARNING, "texpack: could not read %s", path.c_str());
        s_Missing.insert(name);
        return NULL;
    }

    *outw = (uint32_t)rw;
    *outh = (uint32_t)rh;
    s_Replaced.insert(name);
    return rep;
}

void texpackLogStats(void)
{
    static size_t replaced = 0, missing = 0;

    if (s_PackPath.empty() || (s_Replaced.size() == replaced && s_Missing.size() == missing)) {
        return;
    }

    replaced = s_Replaced.size();
    missing = s_Missing.size();
    sysLogPrintf(LOG_NOTE, "texpack: %d textures drawn from the pack, %d drawn with no pack file", (int)replaced, (int)missing);
}

void texpackRelease(const uint8_t *replacement)
{
    stbi_image_free((void *)replacement);
}

/* ---- census: decode every registered texture's base image ourselves ---- */

static inline uint16_t be16(const uint8_t *p)
{
    return (uint16_t)((p[0] << 8) | p[1]);
}

/* the same expansions fast3d's importers use, so the census matches the renderer byte for byte */
static inline void rgba16_to_rgba(uint16_t c, uint8_t *d)
{
    uint8_t r = c >> 11, g = (c >> 6) & 0x1f, b = (c >> 1) & 0x1f;
    d[0] = (uint8_t)((r * 0xff) / 0x1f);
    d[1] = (uint8_t)((g * 0xff) / 0x1f);
    d[2] = (uint8_t)((b * 0xff) / 0x1f);
    d[3] = (c & 1) ? 255 : 0;
}

static inline void ia16_to_rgba(uint16_t c, uint8_t *d)
{
    d[0] = d[1] = d[2] = c >> 8;
    d[3] = c & 0xff;
}

/* palette_to_rgba32 reads an IA16 palette entry the other way round */
static inline void ia16pal_to_rgba(uint16_t c, uint8_t *d)
{
    d[0] = d[1] = d[2] = c & 0xff;
    d[3] = c >> 8;
}

/* Decodes the base image the way fast3d would upload it (padded width). */
static bool texpackDecode(const TexpackEntry &e, std::vector<uint8_t> &out, uint32_t *outw, uint32_t *outh)
{
    const uint8_t *src = e.data;
    uint32_t rowbytes = e.rowbytes;
    uint32_t height = (uint32_t)e.height;
    uint32_t width;
    const uint8_t *pal = e.data + e.paletteoffset;

    if (rowbytes == 0 || height == 0) {
        return false;
    }

    switch (e.siz) {
    case G_IM_SIZ_32b: width = rowbytes / 2; break;
    case G_IM_SIZ_16b: width = rowbytes / 2; break;
    case G_IM_SIZ_8b:  width = rowbytes; break;
    default:           width = rowbytes * 2; break;
    }

    if (width == 0 || width > 1024 || height > 1024) {
        return false;
    }

    out.assign((size_t)width * height * 4, 0);

    for (uint32_t y = 0; y < height; y++) {
        for (uint32_t x = 0; x < width; x++) {
            uint8_t *d = &out[((size_t)y * width + x) * 4];

            switch (e.siz) {
            case G_IM_SIZ_32b: {
                /* import_texture_rgba32 byte-swaps each 32-bit word */
                const uint8_t *p = src + (size_t)y * rowbytes * 2 + x * 4;
                if (e.fmt == G_IM_FMT_RGBA) { d[0] = p[3]; d[1] = p[2]; d[2] = p[1]; d[3] = p[0]; }
                break;
            }
            case G_IM_SIZ_16b: {
                uint16_t c = be16(src + (size_t)y * rowbytes + x * 2);
                if (e.fmt == G_IM_FMT_IA) { ia16_to_rgba(c, d); } else { rgba16_to_rgba(c, d); }
                break;
            }
            case G_IM_SIZ_8b: {
                uint8_t v = src[(size_t)y * rowbytes + x];
                if (e.fmt == G_IM_FMT_CI) {
                    uint16_t c = be16(pal + v * 2);
                    if (e.lut == (G_TT_IA16 >> G_MDSFT_TEXTLUT)) { ia16pal_to_rgba(c, d); } else { rgba16_to_rgba(c, d); }
                } else if (e.fmt == G_IM_FMT_IA) {
                    uint8_t i = (v >> 4) * 17, a = (v & 0xf) * 17;
                    d[0] = d[1] = d[2] = i; d[3] = a;
                } else {
                    d[0] = d[1] = d[2] = v; d[3] = v;
                }
                break;
            }
            default: {
                uint8_t byte = src[(size_t)y * rowbytes + x / 2];
                uint8_t v = (x & 1) ? (byte & 0xf) : (byte >> 4);
                if (e.fmt == G_IM_FMT_CI) {
                    uint16_t c = be16(pal + v * 2);
                    if (e.lut == (G_TT_IA16 >> G_MDSFT_TEXTLUT)) { ia16pal_to_rgba(c, d); } else { rgba16_to_rgba(c, d); }
                } else if (e.fmt == G_IM_FMT_IA) {
                    uint8_t i = (v >> 1) * 36, a = (v & 1) ? 255 : 0;
                    d[0] = d[1] = d[2] = i; d[3] = a;
                } else {
                    uint8_t i = v * 17;
                    d[0] = d[1] = d[2] = i; d[3] = i;
                }
                break;
            }
            }
        }
    }

    *outw = width;
    *outh = height;
    return true;
}

void texpackCensus(void)
{
    std::vector<uint8_t> rgba;
    int written = 0;

    if (g_TexpackDump != 1) {
        return; /* Dump=2 leaves it to the renderer, to check this decoder against it */
    }

    texpackPrepareDirs();

    for (TexpackEntry &e : s_Entries) {
        char buf[32];
        uint32_t w, h;

        if (e.dumped) {
            continue;
        }

        e.dumped = true;
        snprintf(buf, sizeof(buf), "t%04d", e.num);

        std::string name = buf;

        if (s_Dumped.count(name)) {
            continue;
        }

        if (!texpackDecode(e, rgba, &w, &h)) {
            continue;
        }

        texpackDumpImage(name, rgba.data(), w, h, false);
        written++;
    }

    if (written) {
        sysLogPrintf(LOG_NOTE, "texpack: census wrote %d textures (%d registered)", written, (int)s_Entries.size());
    }
}
