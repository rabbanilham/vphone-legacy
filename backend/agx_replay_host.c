/*
 * Offline host harness for the AGX high-level replay.
 *
 * scripts/agx_corpus.py copies the code between AGX_REPLAY_CORE_BEGIN and
 * AGX_REPLAY_CORE_END in s5l8960x.c (plus agx_ctx_write) into
 * agx_replay_core.inc, so this replays an AGXF frame dump with exactly the
 * renderer QEMU runs. GPU memory is the dump's page records: a GVA is its own
 * "physical" address here, and pages absent from the dump are unmapped.
 *
 * Usage: agx_replay_host frame.bin out.rgba
 * Writes "RGBA" + u32 width + u32 height + pixels, and a JSON line on stdout.
 */
#include <glib.h>
#include <math.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint64_t hwaddr;
typedef int MemTxResult;
typedef struct { int unused; } MemTxAttrs;
typedef struct A7State { int unused; } A7State;
typedef struct AGXReplaySnapshot AGXReplaySnapshot;

#define MEMTX_OK 0
#define MEMTX_ERROR 1
#define MEMTXATTRS_UNSPECIFIED ((MemTxAttrs){0})
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define LOG_GUEST_ERROR 1
#define LOG_UNIMP 2

static int address_space_memory;
static GHashTable *host_pages;          /* page GVA -> 4 KiB */
static GHashTable *host_skips;          /* reason -> count */
static unsigned host_draws, host_guest_errors;
static bool host_verbose;

static inline uint32_t ldl_le_p(const void *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static inline uint64_t ldq_le_p(const void *p) { uint64_t v; memcpy(&v, p, 8); return v; }

static void qemu_log_mask(int level, const char *fmt, ...)
{
    char line[1024];
    va_list ap;
    unsigned long long target;
    unsigned draws;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    if (sscanf(line, "AGX replay target 0x%llx draws %u", &target, &draws) == 2) {
        host_draws = draws;
    }
    if (level == LOG_GUEST_ERROR) {
        host_guest_errors++;
    }
    if (host_verbose || level == LOG_GUEST_ERROR) {
        fputs(line, stderr);
    }
}

static void host_skip(const char *reason)
{
    gpointer n = g_hash_table_lookup(host_skips, reason);
    g_hash_table_insert(host_skips, (gpointer)reason, GUINT_TO_POINTER(GPOINTER_TO_UINT(n) + 1));
}
#define AGX_REPLAY_SKIP(reason) host_skip(reason)

static bool agx_gpu_translate_ctx(A7State *s, uint64_t cat, uint64_t gva, hwaddr *pa)
{
    uint64_t base = gva & ~UINT64_C(0xfff);
    if (!cat || !g_hash_table_lookup(host_pages, &base)) {
        return false;
    }
    *pa = gva;
    return true;
}

static MemTxResult host_access(hwaddr pa, void *buf, size_t n, bool write)
{
    uint8_t *bytes = buf;
    while (n) {
        uint64_t base = pa & ~UINT64_C(0xfff);
        size_t chunk = MIN(n, 0x1000 - (pa & 0xfff));
        uint8_t *page = g_hash_table_lookup(host_pages, &base);
        if (!page) {
            return MEMTX_ERROR;
        }
        if (write) {
            memcpy(page + (pa & 0xfff), bytes, chunk);
        } else {
            memcpy(bytes, page + (pa & 0xfff), chunk);
        }
        pa += chunk; bytes += chunk; n -= chunk;
    }
    return MEMTX_OK;
}
#define address_space_read(as, pa, attrs, buf, n) host_access((pa), (buf), (n), false)
#define address_space_write(as, pa, attrs, buf, n) host_access((pa), (void *)(buf), (n), true)

#include "agx_replay_core.inc"

static void add_skip_json(gpointer key, gpointer value, gpointer data)
{
    GString *json = data;
    g_string_append_printf(json, "%s\"%s\": %u", json->str[json->len - 1] == '{' ? "" : ", ",
                           (const char *)key, GPOINTER_TO_UINT(value));
}

int main(int argc, char **argv)
{
    uint8_t header[64], raw[0x280], ta[0x280];
    A7State state = {0};
    FILE *in, *out;

    if (argc != 3) {
        fprintf(stderr, "usage: %s frame.bin out.rgba\n", argv[0]);
        return 2;
    }
    host_verbose = getenv("AGX_HOST_VERBOSE") != NULL;
    host_pages = g_hash_table_new(g_int64_hash, g_int64_equal);
    host_skips = g_hash_table_new(g_str_hash, g_str_equal);
    if (!(in = fopen(argv[1], "rb")) || fread(header, 1, 64, in) != 64 ||
        memcmp(header, "AGXF", 4) || ldl_le_p(header + 4) != 1 ||
        fread(raw, 1, sizeof(raw), in) != sizeof(raw) || fread(ta, 1, sizeof(ta), in) != sizeof(ta)) {
        fprintf(stderr, "%s: not an AGXF v1 dump\n", argv[1]);
        return 1;
    }
    uint32_t flags = ldl_le_p(header + 12);
    uint64_t cat = ldq_le_p(header + 16), target = ldq_le_p(header + 24);
    uint64_t records = ldq_le_p(header + 48);
    for (uint64_t i = 0; i < records; i++) {
        uint64_t *key = g_new(uint64_t, 1), pa;
        uint8_t *page = g_malloc(4096);
        if (fread(key, 8, 1, in) != 1 || fread(&pa, 8, 1, in) != 1 || fread(page, 1, 4096, in) != 4096) {
            fprintf(stderr, "%s: truncated page record %llu\n", argv[1], (unsigned long long)i);
            return 1;
        }
        g_hash_table_insert(host_pages, key, page);
    }
    fclose(in);

    /* The same pass parameters agx_trace_rt derives for agx_replay_frame. */
    uint32_t width = (ldl_le_p(raw + 0x74) & 0xffff) + 1;
    uint32_t height = (ldl_le_p(raw + 0x70) & 0xffff) + 1;
    bool is_clear = ldl_le_p(raw + 0x178) != 0 && !(ldl_le_p(raw + 0x23c) & 1);
    uint64_t scissor_base = ldl_le_p(raw + 0x50);
    uint32_t pitch = 0, words[3], tsw[4];
    AGXTexture rt;
    uint64_t eot = AGX_PDS_HEAP | ldl_le_p(raw + 0x1bc);
    if (agx_ctx_read(&state, cat, eot, words, sizeof(words)) &&
        agx_ctx_read(&state, cat, words[2] + 8, tsw, sizeof(tsw)) &&
        agx_replay_decode_tsw(&state, cat, tsw, &rt) &&
        rt.addr == target && !rt.strip && !rt.twiddled) {
        pitch = rt.stride;
    }

    agx_replay_frame(&state, cat, ta, target, width, height, is_clear, pitch, scissor_base);

    /* Read the target back the way the replay addressed it. */
    uint32_t w = MIN(width, 640u * 4), h = MIN(height, 4096u);
    AGXSurface sf;
    agx_surface_layout(&state, cat, target, w, &sf);
    if (!sf.strip && pitch >= w * 4) {
        sf.pitch = pitch;
    }
    uint8_t *pixels = g_malloc0((size_t)w * h * 4);
    unsigned missing_rows = 0;
    for (uint32_t y = 0; y < h; y++) {
        uint8_t *row = pixels + (size_t)y * w * 4;
        if (!agx_ctx_read(&state, cat, agx_surface_gva(&sf, 0, y), row, w * 4)) {
            memset(row, 0, w * 4);
            missing_rows++;
        }
        for (uint32_t x = 0; x < w; x++) {      /* BGRA -> RGBA */
            uint8_t b = row[x * 4];
            row[x * 4] = row[x * 4 + 2];
            row[x * 4 + 2] = b;
        }
    }
    if (!(out = fopen(argv[2], "wb"))) {
        perror(argv[2]);
        return 1;
    }
    uint32_t dims[2] = { w, h };
    fwrite("RGBA", 1, 4, out);
    fwrite(dims, 4, 2, out);
    fwrite(pixels, 1, (size_t)w * h * 4, out);
    fclose(out);

    GString *skips = g_string_new("{");
    g_hash_table_foreach(host_skips, add_skip_json, skips);
    g_string_append_c(skips, '}');
    printf("{\"width\": %u, \"height\": %u, \"target\": \"0x%llx\", \"clear\": %s, "
           "\"strip\": %s, \"pitch\": %u, \"draws\": %u, \"skips\": %s, "
           "\"guest_errors\": %u, \"missing_rows\": %u, \"capped\": %s, \"ta_valid\": %s}\n",
           w, h, (unsigned long long)target, is_clear ? "true" : "false",
           sf.strip ? "true" : "false", sf.pitch, host_draws, skips->str,
           host_guest_errors, missing_rows, (flags & 2) ? "true" : "false",
           (flags & 1) ? "true" : "false");
    return 0;
}
