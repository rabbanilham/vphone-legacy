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
 *        agx_replay_host --chain out-prefix frame1.bin frame2.bin ...
 * Writes "RGBA" + u32 width + u32 height + pixels, and a JSON line on stdout
 * per dump. A dump records memory when the pass was submitted, before the
 * live render thread had drawn the passes queued ahead of it; --chain carries
 * the pages each replay writes into the later dumps, in order, so a sequence
 * replays as the GPU would have drawn it. Output i is out-prefix-<i>.rgba.
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
static GHashTable *host_dirty;          /* page GVAs written by the replay */
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
            g_hash_table_add(host_dirty, g_memdup2(&base, sizeof(base)));
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

static GHashTable *new_page_table(void)
{
    return g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, g_free);
}

/* Replay one dump; pages in carried (written by earlier passes) override it. */
static int replay_one(const char *dump, const char *rgba_path, GHashTable *carried)
{
    uint8_t header[64], raw[0x280], ta[0x280];
    A7State state = {0};
    FILE *in, *out;
    GHashTableIter it;
    gpointer key, value;

    host_pages = new_page_table();
    host_dirty = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, NULL);
    g_hash_table_remove_all(host_skips);
    host_draws = host_guest_errors = 0;
    if (!(in = fopen(dump, "rb")) || fread(header, 1, 64, in) != 64 ||
        memcmp(header, "AGXF", 4) || ldl_le_p(header + 4) != 1 ||
        fread(raw, 1, sizeof(raw), in) != sizeof(raw) || fread(ta, 1, sizeof(ta), in) != sizeof(ta)) {
        fprintf(stderr, "%s: not an AGXF v1 dump\n", dump);
        return 1;
    }
    uint32_t flags = ldl_le_p(header + 12);
    uint64_t cat = ldq_le_p(header + 16), target = ldq_le_p(header + 24);
    uint64_t records = ldq_le_p(header + 48);
    for (uint64_t i = 0; i < records; i++) {
        uint64_t *gva = g_new(uint64_t, 1), pa;
        uint8_t *page = g_malloc(4096);
        if (fread(gva, 8, 1, in) != 1 || fread(&pa, 8, 1, in) != 1 || fread(page, 1, 4096, in) != 4096) {
            fprintf(stderr, "%s: truncated page record %llu\n", dump, (unsigned long long)i);
            return 1;
        }
        g_hash_table_insert(host_pages, gva, page);
    }
    fclose(in);
    unsigned carried_in = 0;
    if (carried) {
        g_hash_table_iter_init(&it, carried);
        while (g_hash_table_iter_next(&it, &key, &value)) {
            uint8_t *page = g_hash_table_lookup(host_pages, key);
            if (page) {
                memcpy(page, value, 4096);
                carried_in++;
            }
        }
    }

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
    if (!(out = fopen(rgba_path, "wb"))) {
        perror(rgba_path);
        return 1;
    }
    uint32_t dims[2] = { w, h };
    fwrite("RGBA", 1, 4, out);
    fwrite(dims, 4, 2, out);
    fwrite(pixels, 1, (size_t)w * h * 4, out);
    fclose(out);
    g_free(pixels);

    if (carried) {
        g_hash_table_iter_init(&it, host_dirty);
        while (g_hash_table_iter_next(&it, &key, NULL)) {
            g_hash_table_replace(carried, g_memdup2(key, 8),
                                 g_memdup2(g_hash_table_lookup(host_pages, key), 4096));
        }
    }

    GString *skips = g_string_new("{");
    g_hash_table_foreach(host_skips, add_skip_json, skips);
    g_string_append_c(skips, '}');
    printf("{\"width\": %u, \"height\": %u, \"target\": \"0x%llx\", \"clear\": %s, "
           "\"strip\": %s, \"pitch\": %u, \"draws\": %u, \"skips\": %s, "
           "\"guest_errors\": %u, \"missing_rows\": %u, \"capped\": %s, \"ta_valid\": %s, "
           "\"carried_pages\": %u}\n",
           w, h, (unsigned long long)target, is_clear ? "true" : "false",
           sf.strip ? "true" : "false", sf.pitch, host_draws, skips->str,
           host_guest_errors, missing_rows, (flags & 2) ? "true" : "false",
           (flags & 1) ? "true" : "false", carried_in);
    fflush(stdout);
    g_string_free(skips, true);
    g_hash_table_destroy(host_pages);
    g_hash_table_destroy(host_dirty);
    return 0;
}

int main(int argc, char **argv)
{
    host_verbose = getenv("AGX_HOST_VERBOSE") != NULL;
    host_skips = g_hash_table_new(g_str_hash, g_str_equal);
    if (argc >= 4 && !strcmp(argv[1], "--chain")) {
        GHashTable *carried = new_page_table();
        for (int i = 3; i < argc; i++) {
            g_autofree char *path = g_strdup_printf("%s-%d.rgba", argv[2], i - 3);
            if (replay_one(argv[i], path, carried)) {
                return 1;
            }
        }
        return 0;
    }
    if (argc != 3) {
        fprintf(stderr, "usage: %s frame.bin out.rgba | --chain out-prefix frame.bin...\n", argv[0]);
        return 2;
    }
    return replay_one(argv[1], argv[2], NULL);
}
