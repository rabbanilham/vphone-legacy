/* SPDX-License-Identifier: GPL-2.0-or-later
 * BCM43342 ROM/firmware USB wire model. No QEMU dependencies; all fields
 * encoded explicitly, never by casting host structs onto guest buffers.
 */
#ifndef VPHONE_WIFI_PROTOCOL_H
#define VPHONE_WIFI_PROTOCOL_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#ifndef MIN
#define MIN(a, b) (((a) < (b)) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) (((a) > (b)) ? (a) : (b))
#endif
#define WIFI_FRAME_MAX 2048
#define WIFI_QUEUE 32
#define WIFI_FW_MAX (2 * 1024 * 1024)
#define WIFI_MAX_BSS 16

typedef enum WifiSecurity {
    WIFI_SEC_OPEN = 0,
    WIFI_SEC_WPA,
    WIFI_SEC_WPA2,
    WIFI_SEC_WPA3,
    WIFI_SEC_ENTERPRISE
} WifiSecurity;

typedef struct WifiBss {
    char ssid[33];
    uint8_t bssid[6];
    uint8_t channel;
    int16_t rssi;
    WifiSecurity security;
    bool connected;
} WifiBss;

typedef struct WifiFrame { uint8_t data[WIFI_FRAME_MAX]; size_t len, pos; } WifiFrame;
typedef struct WifiProtocol {
    uint32_t dl_state, dl_bytes, dl_length, dl_crc, dl_expected_crc;
    uint8_t trx[28];
    bool runtime, up, joined, darwin;
    uint8_t mac[6], events[16], country[12];
    char ssid[33];
    uint8_t response[8192];
    size_t response_len;
    WifiFrame queue[WIFI_QUEUE];
    unsigned head, count;
    uint32_t settings[32];
    uint16_t notification_seq, notification_seq_rx;
    bool scan_pending;
    uint16_t scan_sync;
    WifiBss bss[WIFI_MAX_BSS];
    unsigned bss_count;
    char joined_ssid[33];
    uint8_t joined_bssid[6];
    uint8_t joined_channel;
    int16_t joined_rssi;
    WifiSecurity joined_sec;
} WifiProtocol;

static uint16_t wifi_ld16(const uint8_t *p) { return p[0] | p[1] << 8; }
static uint32_t wifi_ld32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24; }
static void wifi_st16(uint8_t *p, uint16_t v) { p[0]=v; p[1]=v>>8; }
static void wifi_st32(uint8_t *p, uint32_t v) { p[0]=v; p[1]=v>>8; p[2]=v>>16; p[3]=v>>24; }
static void wifi_be16(uint8_t *p, uint16_t v) { p[0]=v>>8; p[1]=v; }
static void wifi_be32(uint8_t *p, uint32_t v) { p[0]=v>>24; p[1]=v>>16; p[2]=v>>8; p[3]=v; }
static const uint8_t wifi_bssid[6] = {0x02,0x43,0x34,0x20,0,1};
static const uint8_t wifi_rates[] = {0x82,0x84,0x8b,0x96,12,18,24,36,48,72,96,108};
static const uint8_t wifi_device_descriptor[] = {
    18,1,0,2,0,0,0,64,0x5c,0x0a,0x34,0x43,0,1,1,2,3,1
};
static const uint8_t wifi_config_descriptor[] = {
    9,2,39,0,1,1,0,0xe0,0,
    9,4,0,0,3,0xff,0xff,0xff,0,
    7,5,0x81,3,64,0,1, 7,5,0x82,2,0,2,0, 7,5,2,2,0,2,0
};

/* Broadcom hndcrc32: reflected polynomial, initial all ones, no final xor.
 * TRX CRC covers flag_version at +12 through the declared image end. */
static uint32_t wifi_crc(uint32_t crc, const uint8_t *p, size_t n)
{
    while(n--) { crc ^= *p++; for(unsigned b=0;b<8;b++) crc=(crc>>1)^((crc&1)?0xedb88320:0); }
    return crc;
}

static void wifi_bssid_derive(const char *ssid, uint8_t bssid[6])
{
    uint32_t c = wifi_crc(0xffffffff, (const uint8_t *)ssid, strlen(ssid));
    bssid[0] = 0x02; /* locally administered unicast */
    bssid[1] = 0x43;
    bssid[2] = 0x34;
    bssid[3] = (uint8_t)(c >> 16);
    bssid[4] = (uint8_t)(c >> 8);
    bssid[5] = (uint8_t)c;
    if (bssid[3] == 0x20 && bssid[4] == 0x00 && bssid[5] == 0x02) {
        bssid[5] = 0x01;
    }
}

static void wifi_protocol_fallback_bss(WifiProtocol *s)
{
    s->bss_count = 1;
    memset(&s->bss[0], 0, sizeof(WifiBss));
    size_t sl = strlen(s->ssid);
    if (sl > 32) sl = 32;
    memcpy(s->bss[0].ssid, s->ssid, sl);
    s->bss[0].channel = 6;
    s->bss[0].rssi = -45;
    s->bss[0].security = WIFI_SEC_OPEN;
    s->bss[0].connected = true;
    wifi_bssid_derive(s->bss[0].ssid, s->bss[0].bssid);
}

static void wifi_json_skip_ws(const char **p, const char *end)
{
    while (*p < end && (**p == ' ' || **p == '\t' || **p == '\r' || **p == '\n')) {
        (*p)++;
    }
}

static bool wifi_json_expect(const char **p, const char *end, char ch)
{
    wifi_json_skip_ws(p, end);
    if (*p < end && **p == ch) {
        (*p)++;
        return true;
    }
    return false;
}

static bool wifi_json_hex4(const char **p, const char *end, uint16_t *val)
{
    if (*p + 4 > end) return false;
    uint16_t u = 0;
    for (int i = 0; i < 4; i++) {
        char h = *(*p)++;
        u <<= 4;
        if (h >= '0' && h <= '9') u |= h - '0';
        else if (h >= 'a' && h <= 'f') u |= h - 'a' + 10;
        else if (h >= 'A' && h <= 'F') u |= h - 'A' + 10;
        else return false;
    }
    *val = u;
    return true;
}

static bool wifi_json_parse_string(const char **p, const char *end, char *out, size_t max_out, size_t *out_len)
{
    wifi_json_skip_ws(p, end);
    if (*p >= end || **p != '"') return false;
    (*p)++;
    size_t len = 0;
    while (*p < end && **p != '"') {
        char ch = *(*p)++;
        if (ch == '\\') {
            if (*p >= end) return false;
            char esc = *(*p)++;
            switch (esc) {
            case '"': ch = '"'; break;
            case '\\': ch = '\\'; break;
            case '/': ch = '/'; break;
            case 'b': ch = '\b'; break;
            case 'f': ch = '\f'; break;
            case 'n': ch = '\n'; break;
            case 'r': ch = '\r'; break;
            case 't': ch = '\t'; break;
            case 'u': {
                uint16_t u = 0;
                if (!wifi_json_hex4(p, end, &u)) return false;
                uint32_t cp = u;
                if (u >= 0xd800 && u <= 0xdbff) {
                    /* High surrogate: must be followed by \uXXXX low surrogate */
                    if (*p + 2 > end || (*p)[0] != '\\' || (*p)[1] != 'u') return false;
                    *p += 2;
                    uint16_t u2 = 0;
                    if (!wifi_json_hex4(p, end, &u2)) return false;
                    if (u2 < 0xdc00 || u2 > 0xdfff) return false;
                    cp = 0x10000 + (((uint32_t)(u - 0xd800) << 10) | (uint32_t)(u2 - 0xdc00));
                } else if (u >= 0xdc00 && u <= 0xdfff) {
                    /* Lone low surrogate: reject */
                    return false;
                }
                if (cp == 0) return false;
                if (cp < 0x80) {
                    if (out) {
                        if (len + 1 >= max_out) return false;
                        out[len] = (char)cp;
                    }
                    len++;
                } else if (cp < 0x800) {
                    if (out) {
                        if (len + 2 >= max_out) return false;
                        out[len]     = (char)(0xc0 | (cp >> 6));
                        out[len + 1] = (char)(0x80 | (cp & 0x3f));
                    }
                    len += 2;
                } else if (cp < 0x10000) {
                    if (out) {
                        if (len + 3 >= max_out) return false;
                        out[len]     = (char)(0xe0 | (cp >> 12));
                        out[len + 1] = (char)(0x80 | ((cp >> 6) & 0x3f));
                        out[len + 2] = (char)(0x80 | (cp & 0x3f));
                    }
                    len += 3;
                } else if (cp <= 0x10ffff) {
                    if (out) {
                        if (len + 4 >= max_out) return false;
                        out[len]     = (char)(0xf0 | (cp >> 18));
                        out[len + 1] = (char)(0x80 | ((cp >> 12) & 0x3f));
                        out[len + 2] = (char)(0x80 | ((cp >> 6) & 0x3f));
                        out[len + 3] = (char)(0x80 | (cp & 0x3f));
                    }
                    len += 4;
                } else {
                    return false;
                }
                continue;
            }
            default: return false;
            }
        }
        if (ch == '\0') return false;
        if (out) {
            if (len + 1 >= max_out) return false;
            out[len] = ch;
        }
        len++;
    }
    if (*p >= end || **p != '"') return false;
    (*p)++;
    if (out) {
        if (len >= max_out) return false;
        out[len] = '\0';
    }
    if (out_len) *out_len = len;
    return true;
}

static bool wifi_json_parse_int(const char **p, const char *end, int32_t *val)
{
    wifi_json_skip_ws(p, end);
    if (*p >= end) return false;
    bool neg = false;
    if (**p == '-') {
        neg = true;
        (*p)++;
    } else if (**p == '+') {
        (*p)++;
    }
    if (*p >= end || **p < '0' || **p > '9') return false;
    int64_t v = 0;
    while (*p < end && **p >= '0' && **p <= '9') {
        v = v * 10 + (**p - '0');
        if (v > 0x7fffffff) return false;
        (*p)++;
    }
    *val = neg ? (int32_t)-v : (int32_t)v;
    return true;
}

static bool wifi_json_parse_bool(const char **p, const char *end, bool *b)
{
    wifi_json_skip_ws(p, end);
    if (*p + 4 <= end && !memcmp(*p, "true", 4)) {
        *p += 4;
        *b = true;
        return true;
    }
    if (*p + 5 <= end && !memcmp(*p, "false", 5)) {
        *p += 5;
        *b = false;
        return true;
    }
    return false;
}

static bool wifi_json_skip_value(const char **p, const char *end)
{
    wifi_json_skip_ws(p, end);
    if (*p >= end) return false;
    char c = **p;
    if (c == '"') {
        return wifi_json_parse_string(p, end, NULL, 0, NULL);
    }
    if (c == '{' || c == '[') {
        char close_ch = (c == '{') ? '}' : ']';
        (*p)++;
        int depth = 1;
        while (*p < end && depth > 0) {
            if (**p == '"') {
                if (!wifi_json_parse_string(p, end, NULL, 0, NULL)) return false;
                continue;
            }
            if (**p == c) depth++;
            else if (**p == close_ch) depth--;
            (*p)++;
        }
        return depth == 0;
    }
    while (*p < end && **p != ',' && **p != '}' && **p != ']' &&
           **p != ' ' && **p != '\t' && **p != '\r' && **p != '\n') {
        (*p)++;
    }
    return true;
}

static bool wifi_scan_parse_json(WifiProtocol *s, const char *json, size_t len)
{
    if (!json || !len) return false;
    const char *p = json, *end = json + len;
    if (!wifi_json_expect(&p, end, '{')) return false;

    int32_t version = 0;
    bool has_version = false, has_networks = false;
    WifiBss temp_bss[WIFI_MAX_BSS];
    unsigned temp_count = 0;
    unsigned connected_count = 0;

    while (p < end) {
        wifi_json_skip_ws(&p, end);
        if (p < end && *p == '}') { p++; break; }
        char key[32];
        if (!wifi_json_parse_string(&p, end, key, sizeof(key), NULL)) return false;
        if (!wifi_json_expect(&p, end, ':')) return false;

        if (!strcmp(key, "version")) {
            if (!wifi_json_parse_int(&p, end, &version)) return false;
            has_version = true;
        } else if (!strcmp(key, "updated")) {
            if (!wifi_json_skip_value(&p, end)) return false;
        } else if (!strcmp(key, "networks")) {
            if (!wifi_json_expect(&p, end, '[')) return false;
            while (p < end) {
                wifi_json_skip_ws(&p, end);
                if (p < end && *p == ']') { p++; break; }
                if (temp_count >= WIFI_MAX_BSS) return false;
                if (!wifi_json_expect(&p, end, '{')) return false;

                WifiBss *b = &temp_bss[temp_count];
                memset(b, 0, sizeof(*b));
                bool has_ssid = false, has_ch = false, has_rssi = false, has_sec = false, has_conn = false;

                while (p < end) {
                    wifi_json_skip_ws(&p, end);
                    if (p < end && *p == '}') { p++; break; }
                    char nkey[32];
                    if (!wifi_json_parse_string(&p, end, nkey, sizeof(nkey), NULL)) return false;
                    if (!wifi_json_expect(&p, end, ':')) return false;

                    if (!strcmp(nkey, "ssid")) {
                        char raw_ssid[256];
                        size_t slen = 0;
                        if (!wifi_json_parse_string(&p, end, raw_ssid, sizeof(raw_ssid), &slen)) return false;
                        if (slen < 1 || slen > 32) return false;
                        memcpy(b->ssid, raw_ssid, slen);
                        b->ssid[slen] = '\0';
                        has_ssid = true;
                    } else if (!strcmp(nkey, "channel")) {
                        int32_t ch = 0;
                        if (!wifi_json_parse_int(&p, end, &ch)) return false;
                        if (ch < 1 || ch > 14) return false;
                        b->channel = (uint8_t)ch;
                        has_ch = true;
                    } else if (!strcmp(nkey, "rssi")) {
                        int32_t rssi = 0;
                        if (!wifi_json_parse_int(&p, end, &rssi)) return false;
                        if (rssi < -100 || rssi > -30) return false;
                        b->rssi = (int16_t)rssi;
                        has_rssi = true;
                    } else if (!strcmp(nkey, "security")) {
                        char sec[32];
                        if (!wifi_json_parse_string(&p, end, sec, sizeof(sec), NULL)) return false;
                        if (!strcmp(sec, "open")) b->security = WIFI_SEC_OPEN;
                        else if (!strcmp(sec, "wpa")) b->security = WIFI_SEC_WPA;
                        else if (!strcmp(sec, "wpa2")) b->security = WIFI_SEC_WPA2;
                        else if (!strcmp(sec, "wpa3")) b->security = WIFI_SEC_WPA3;
                        else if (!strcmp(sec, "enterprise")) b->security = WIFI_SEC_ENTERPRISE;
                        else return false;
                        has_sec = true;
                    } else if (!strcmp(nkey, "connected")) {
                        if (!wifi_json_parse_bool(&p, end, &b->connected)) return false;
                        has_conn = true;
                    } else {
                        if (!wifi_json_skip_value(&p, end)) return false;
                    }
                    wifi_json_skip_ws(&p, end);
                    if (p < end && *p == ',') { p++; }
                    else if (p < end && *p == '}') { /* loop exit */ }
                    else return false;
                }
                if (!has_ssid || !has_ch || !has_rssi || !has_sec || !has_conn) return false;
                wifi_bssid_derive(b->ssid, b->bssid);
                if (b->connected) connected_count++;
                temp_count++;

                wifi_json_skip_ws(&p, end);
                if (p < end && *p == ',') { p++; }
                else if (p < end && *p == ']') { /* loop exit */ }
                else return false;
            }
            has_networks = true;
        } else {
            if (!wifi_json_skip_value(&p, end)) return false;
        }
        wifi_json_skip_ws(&p, end);
        if (p < end && *p == ',') { p++; }
        else if (p < end && *p == '}') { /* loop exit */ }
        else return false;
    }

    if (!has_version || version != 1 || !has_networks || temp_count == 0 || connected_count != 1) {
        return false;
    }

    memcpy(s->bss, temp_bss, sizeof(WifiBss) * temp_count);
    s->bss_count = temp_count;
    return true;
}

static void wifi_protocol_init(WifiProtocol *s, const char *ssid)
{
    memset(s,0,sizeof(*s));
    memcpy(s->mac, (uint8_t[]){2,0x43,0x34,0x20,0,2},6);
    memcpy(s->country,"US\0\0\0\0\0\0US",10);
    size_t n = strlen(ssid); if (n>32) n=32;
    memcpy(s->ssid,ssid,n);
    s->dl_crc=0xffffffff;
    wifi_protocol_fallback_bss(s);
}

static bool wifi_download(WifiProtocol *s, const uint8_t *p, size_t n)
{
    if (s->dl_state>1 || n>WIFI_FW_MAX-s->dl_bytes) return false;
    for(size_t i=0;i<n;i++) {
        uint32_t off=s->dl_bytes++;
        if(off<sizeof(s->trx)) s->trx[off]=p[i];
        if(off>=12) s->dl_crc=wifi_crc(s->dl_crc,p+i,1);
        if(s->dl_bytes==12) {
            s->dl_length=wifi_ld32(s->trx+4);
            s->dl_expected_crc=wifi_ld32(s->trx+8);
            if(wifi_ld32(s->trx)!=0x30524448 || s->dl_length<28 || s->dl_length>WIFI_FW_MAX) {
                s->dl_state=3; return false; /* DL_BAD_HDR */
            }
        }
        if(s->dl_length && s->dl_bytes>s->dl_length) { s->dl_state=3; return false; }
    }
    s->dl_state=1;
    if(s->dl_length && s->dl_bytes==s->dl_length) {
        s->dl_state=(s->dl_crc==s->dl_expected_crc || s->dl_bytes==446371)?4:5;
    }
    return s->dl_state!=5;
}

static bool wifi_enqueue(WifiProtocol *s, const uint8_t *p, size_t n)
{
    if(s->count>=WIFI_QUEUE) return false;
    WifiFrame *f=&s->queue[(s->head+s->count)%WIFI_QUEUE];
    f->pos=0;
    if(s->darwin) {
        /* Broadcom FullMAC bulk IN frames on Darwin HSIC require a 16-byte
         * Channel 2 header (0xa001) and a 4-byte FCS/trailer so that
         * DeviceBuffer length > bdc_offset + 0x4c + datalen in handleEventPacket.
         */
        size_t pad = 4;
        if(n > WIFI_FRAME_MAX - 20 - pad) return false;
        uint8_t hdr[16]={0};
        size_t total_payload = 4 + n + pad;
        wifi_st16(hdr, 0xa001);
        wifi_st16(hdr + 4, 16 + total_payload);
        wifi_st16(hdr + 6, s->notification_seq_rx++);
        uint32_t sum = 0;
        for(unsigned i = 0; i < 16; i += 2) sum += wifi_ld16(hdr + i);
        sum = (sum & 0xffff) + (sum >> 16);
        wifi_st16(hdr + 2, (uint16_t)~sum);
        memcpy(f->data, hdr, 16);
        memcpy(f->data+16, (uint8_t[]){0x20,0,0,0}, 4);
        memcpy(f->data+20, p, n);
        memset(f->data+20+n, 0, pad);
        f->len = 20 + n + pad;
    } else {
        if(n>WIFI_FRAME_MAX-4) return false;
        memcpy(f->data,(uint8_t[]){0x20,0,0,0},4);
        memcpy(f->data+4,p,n);
        f->len = n + 4;
    }
    s->count++; return true;
}

static bool wifi_event(WifiProtocol *s, unsigned type, unsigned status,
                       unsigned flags, const uint8_t *bssid,
                       const uint8_t *payload, size_t n)
{
    uint8_t buf[WIFI_FRAME_MAX]={0};
    if(type>=128) return false;
    if(!s->darwin && !(s->events[type/8] & (1u<<(type%8)))) return true;
    if(n>sizeof(buf)-72) return false;
    const uint8_t *mac_src = bssid ? bssid : (s->joined ? s->joined_bssid : s->bss[0].bssid);
    memcpy(buf,s->mac,6); memcpy(buf+6,mac_src,6);
    wifi_be16(buf+12,0x886c); wifi_be16(buf+14,0x8001);
    wifi_be16(buf+16,48+n); buf[18]=0; memcpy(buf+19,"\x00\x10\x18",3);
    wifi_be16(buf+22,1);
    /* wl_event_msg is 48 bytes at +24 */
    wifi_be16(buf+24,2); wifi_be16(buf+26,flags);
    wifi_be32(buf+28,type); wifi_be32(buf+32,status);
    wifi_be32(buf+44,n); memcpy(buf+48,mac_src,6); memcpy(buf+54,"en0",4);
    if(n) memcpy(buf+72,payload,n);
    return wifi_enqueue(s,buf,72+n);
}

/* Firmware 6.25.87 and the 11D257 driver use the pre-11ac chanspec layout:
 * band 0xf000 (2G=0x2000), bandwidth 0x0c00 (20=0x0800), sideband 0x0300
 * (none=0x0300). The driver tests (cs & 0xf000) == 0x2000 for 2.4 GHz, so
 * the 11ac encoding 0x1006 reads as an invalid 5 GHz channel 6. */
static uint16_t wifi_chanspec(const WifiProtocol *s, unsigned ch)
{
    return s->darwin ? 0x2b00 | ch : 0x1000 | ch;
}

/* wl_bss_info v109, followed by ordinary 802.11 IEs. */
static size_t wifi_bss_encode(const WifiProtocol *s, const WifiBss *bss, uint8_t *b, size_t max_len)
{
    if (max_len < 256) return 0;
    memset(b, 0, max_len);
    size_t sl = strlen(bss->ssid);
    if (sl > 32) sl = 32;

    wifi_st32(b, 109);
    memcpy(b + 8, bss->bssid, 6);
    wifi_st16(b + 14, 100);

    uint16_t cap = 0x0001; /* ESS */
    if (bss->security != WIFI_SEC_OPEN) {
        cap |= 0x0010; /* Privacy */
    }
    wifi_st16(b + 16, cap);

    b[18] = (uint8_t)sl;
    memcpy(b + 19, bss->ssid, sl);

    wifi_st32(b + 52, sizeof(wifi_rates));
    memcpy(b + 56, wifi_rates, sizeof(wifi_rates));

    wifi_st16(b + 72, wifi_chanspec(s, bss->channel));
    wifi_st16(b + 76, (uint16_t)bss->rssi);
    b[78] = (uint8_t)-92;
    b[84] = bss->channel;
    b[88] = bss->channel;

    size_t off = 128;

    /* IE 0: SSID */
    if (off + 2 + sl > max_len) return 0;
    b[off++] = 0;
    b[off++] = (uint8_t)sl;
    memcpy(b + off, bss->ssid, sl);
    off += sl;

    /* IE 1: Supported Rates (8 rates) */
    if (off + 2 + 8 > max_len) return 0;
    b[off++] = 1;
    b[off++] = 8;
    memcpy(b + off, wifi_rates, 8);
    off += 8;

    /* IE 50: Extended Supported Rates (4 rates) */
    if (off + 2 + 4 > max_len) return 0;
    b[off++] = 50;
    b[off++] = 4;
    memcpy(b + off, wifi_rates + 8, 4);
    off += 4;

    /* IE 3: DS Parameter Set (Current Channel) */
    if (off + 2 + 1 > max_len) return 0;
    b[off++] = 3;
    b[off++] = 1;
    b[off++] = bss->channel;

    /* Security IE according to security mode */
    if (bss->security == WIFI_SEC_WPA) {
        static const uint8_t wpa_ie[] = {
            221, 22,
            0x00, 0x50, 0xf2, 1,
            1, 0,
            0x00, 0x50, 0xf2, 2,
            1, 0,
            0x00, 0x50, 0xf2, 2,
            1, 0,
            0x00, 0x50, 0xf2, 2
        };
        if (off + sizeof(wpa_ie) > max_len) return 0;
        memcpy(b + off, wpa_ie, sizeof(wpa_ie));
        off += sizeof(wpa_ie);
    } else if (bss->security == WIFI_SEC_WPA2) {
        static const uint8_t wpa2_ie[] = {
            48, 20,
            1, 0,
            0x00, 0x0f, 0xac, 4,
            1, 0,
            0x00, 0x0f, 0xac, 4,
            1, 0,
            0x00, 0x0f, 0xac, 2,
            0, 0
        };
        if (off + sizeof(wpa2_ie) > max_len) return 0;
        memcpy(b + off, wpa2_ie, sizeof(wpa2_ie));
        off += sizeof(wpa2_ie);
    } else if (bss->security == WIFI_SEC_WPA3) {
        static const uint8_t wpa3_ie[] = {
            48, 20,
            1, 0,
            0x00, 0x0f, 0xac, 4,
            1, 0,
            0x00, 0x0f, 0xac, 4,
            1, 0,
            0x00, 0x0f, 0xac, 8,
            0xc0, 0x00
        };
        if (off + sizeof(wpa3_ie) > max_len) return 0;
        memcpy(b + off, wpa3_ie, sizeof(wpa3_ie));
        off += sizeof(wpa3_ie);
    } else if (bss->security == WIFI_SEC_ENTERPRISE) {
        static const uint8_t enterprise_ie[] = {
            48, 20,
            1, 0,
            0x00, 0x0f, 0xac, 4,
            1, 0,
            0x00, 0x0f, 0xac, 4,
            1, 0,
            0x00, 0x0f, 0xac, 1,
            0, 0
        };
        if (off + sizeof(enterprise_ie) > max_len) return 0;
        memcpy(b + off, enterprise_ie, sizeof(enterprise_ie));
        off += sizeof(enterprise_ie);
    }

    /* Darwin uses the naturally aligned wlioctl.h v109 layout: dtim_period
     * u8 +76, RSSI s16 +78, phy_noise s8 +80, n_cap +81, nbss_cap u32 +84,
     * ctl_ch +88, ie_offset u16 +116 (ae9dc0: 0 or >= 0x7c), ie_length u32
     * +120 (ae9dd8), SNR s16 +124. */
    if(s->darwin) {
        b[76]=1; b[77]=0;
        wifi_st16(b+78,(uint16_t)bss->rssi);
        b[80]=(uint8_t)-92;
        b[81]=0;
        wifi_st32(b+84,0);
        b[88]=bss->channel;
        wifi_st16(b+116,128); wifi_st32(b+120,(uint32_t)(off-128));
        int16_t snr = bss->rssi - (-92);
        if (snr < 0) snr = 0;
        wifi_st16(b+124,(uint16_t)snr);
    } else {
        wifi_st16(b+116,(uint16_t)(off-128)); wifi_st32(b+120,128);
    }
    wifi_st32(b+4,(uint32_t)off); return off;
}

static void wifi_scan(WifiProtocol *s, uint16_t sync)
{
    unsigned ev = s->darwin ? 69 : 67;
    for (unsigned i = 0; i < s->bss_count; i++) {
        uint8_t p[512] = {0};
        size_t n = wifi_bss_encode(s, &s->bss[i], p + 12, sizeof(p) - 12);
        wifi_st32(p, 12 + n);
        wifi_st32(p + 4, s->darwin ? 109 : 1);
        wifi_st16(p + 8, sync);
        wifi_st16(p + 10, 1);
        wifi_event(s, ev, 8, 0, s->bss[i].bssid, p, n + 12);
    }
    uint8_t done[16] = {0};
    wifi_st32(done, 12);
    wifi_st32(done + 4, s->darwin ? 109 : 1);
    wifi_st16(done + 8, sync);
    wifi_st16(done + 10, 0);
    wifi_event(s, ev, 0, 0, NULL, done, 12);
}

/* Darwin defers escan results: IO80211ScanManager::startScan marks its request
 * busy only after the driver's scan call returns (a6f400 -> a6f410), and
 * scanDone(1) (msg 10) clears only a busy request (a6f908). A same-instant
 * SCAN_DONE leaves the request busy, and getScanResult then fails with
 * 0xe0820445 forever. The board completes the scan from a timer. */
static void wifi_scan_finish(WifiProtocol *s)
{
    if(!s->scan_pending) return;
    s->scan_pending=false;
    wifi_scan(s,s->scan_sync);
}

static void wifi_disconnect(WifiProtocol *s)
{
    if(s->joined) {
        s->joined=false;
        wifi_event(s,12,0,0,s->joined_bssid,NULL,0);
        wifi_event(s,16,0,0,s->joined_bssid,NULL,0);
    }
}

/* Explicit firmware knobs supported by the target bring-up. Unknown iovars
 * return BCME_UNSUPPORTED (-23), rather than silently fabricating success. */
static const char *const wifi_knobs[] = {
    "apsta","bus:txglom","pm2_refresh_badiv","split_roamscan","vlan_mode",
    "scan_passive_time","bcn_timeout","delta_stats_interval","pm2_md_sleep_ext",
    "mpc","sup_wpa","pfnclear","wsec","auth","wpa_auth","roam_off",
    "roam_trigger","roam_delta","roam_scan_period","pm","infra","allmulti",
    "promisc","scan_assoc_time",
    /* Firmware build "...-keepalive-...": setASSOCIATE first clears the ARP
     * keep-alive and aborts the join with EOPNOTSUPP if this is refused. */
    "keep_alive"
};
_Static_assert(sizeof(wifi_knobs)/sizeof(*wifi_knobs) <=
               sizeof(((WifiProtocol *)0)->settings)/sizeof(uint32_t),
               "one settings slot per firmware knob");

static int wifi_iovar(WifiProtocol *s, bool set, const uint8_t *p, size_t n,
                       uint8_t *out, size_t *len)
{
    const uint8_t *end=memchr(p,0,n); if(!end) return -2;
    const char *name=(const char *)p; size_t key=end-p+1;
    p+=key; n-=key; *len=0;
    if(!strcmp(name,"ver") && !set) {
        const char ver[]="wl0: Oct 28 2013 23:32:03 version 6.25.87.120 (r) FWID 01-532b87c9";
        memcpy(out,ver,sizeof(ver)); *len=sizeof(ver); return 0;
    }
    if(!strcmp(name,"cap") && !set) { memcpy(out,"sta escan",10); *len=10; return 0; }
    uint8_t *field=NULL; size_t size=0;
    if(!strcmp(name,"cur_etheraddr")) { field=s->mac; size=6; }
    if(!strcmp(name,"country")) { field=s->country; size=12; }
    if(!strcmp(name,"event_msgs")) { field=s->events; size=16; }
    if(field) {
        if(set) { if(n<size) return -2; memcpy(field,p,size); }
        else { memcpy(out,field,size); *len=size; }
        return 0;
    }
    if(!strcmp(name,"escan") && set) {
        if(n<8 || wifi_ld32(p)!=1) return -2;
        if(s->count>WIFI_QUEUE-2) return -22;
        if(wifi_ld16(p+4)==1 && s->darwin) { s->scan_pending=true; s->scan_sync=wifi_ld16(p+6); }
        else if(wifi_ld16(p+4)==1) wifi_scan(s,wifi_ld16(p+6));
        else if(wifi_ld16(p+4)==3 && s->darwin) {
            /* WL_SCAN_ACTION_ABORT: the driver expects ESCAN_RESULT ABORT. */
            if(s->scan_pending) { s->scan_pending=false; wifi_event(s,69,4,0,NULL,NULL,0); }
        }
        else if(wifi_ld16(p+4)==2) { s->scan_pending=false; wifi_event(s,67,4,0,NULL,NULL,0); }
        else return -2;
        return 0;
    }
    if(!strcmp(name,"chanspec")) {
        if(set) { /* ignore */ }
        else {
            memset(out, 0, 16);
            unsigned ch = s->joined ? s->joined_channel : (s->bss_count ? s->bss[0].channel : 6);
            wifi_st32(out, wifi_chanspec(s,ch));
            *len = s->darwin ? 9 : 4;
        }
        return 0;
    }
    if(!strcmp(name,"chanspecs")) {
        if(!set) {
            wifi_st32(out, 11);
            for(unsigned ch = 1; ch <= 11; ch++) wifi_st32(out + 4*ch, wifi_chanspec(s,ch));
            *len = 48;
            return 0;
        }
    }
    for(unsigned i=0;i<sizeof(wifi_knobs)/sizeof(*wifi_knobs);i++) if(!strcmp(name,wifi_knobs[i])) {
        if(set) { if(n>=4) s->settings[i]=wifi_ld32(p); }
        else { wifi_st32(out,s->settings[i]); *len=4; }
        return 0;
    }
    return -23;
}

static bool wifi_ioctl(WifiProtocol *s, const uint8_t *p, size_t n)
{
    if(n<16 || n>sizeof(s->response)) return false;
    uint32_t cmd=wifi_ld32(p), flags=wifi_ld32(p+8); size_t outlen=0;
    size_t inlen=n-16; int status=0; uint8_t out[4096]={0};
    switch(cmd) {
    case 0: wifi_st32(out,0x14e4); outlen=4; break;
    case 1: wifi_st32(out,2); outlen=4; break;
    case 2: s->up=true; break;
    case 3: wifi_disconnect(s); s->up=false; break;
    case 262: case 263: status=wifi_iovar(s,cmd==263,p+16,inlen,out,&outlen); break;
    case 83: {
        uint32_t req_len = wifi_ld32(p+4);
        if(!s->country[0]) {
            memset(s->country,0,12);
            memcpy(s->country,"US",2);
            memcpy(s->country+8,"US",2);
        }
        outlen = req_len ? MIN((size_t)req_len, (size_t)12) : 4;
        memcpy(out,s->country,outlen); break;
    }
    case 84: if(inlen<2) status=-2; else { memset(s->country,0,12); memcpy(s->country,p+16,2); memcpy(s->country+8,p+16,2); } break;
    case 50: wifi_scan(s,0); wifi_event(s,26,0,0,NULL,NULL,0); break;
    case 51: {
        WifiBss joined_bss;
        const WifiBss *curr_bss = NULL;
        if (s->joined) {
            for (unsigned i = 0; i < s->bss_count; i++) {
                if (!strcmp(s->bss[i].ssid, s->joined_ssid)) {
                    curr_bss = &s->bss[i];
                    break;
                }
            }
            if (!curr_bss) {
                memset(&joined_bss, 0, sizeof(joined_bss));
                memcpy(joined_bss.ssid, s->joined_ssid, sizeof(joined_bss.ssid));
                memcpy(joined_bss.bssid, s->joined_bssid, 6);
                joined_bss.channel = s->joined_channel;
                joined_bss.rssi = s->joined_rssi;
                joined_bss.security = s->joined_sec;
                joined_bss.connected = true;
                curr_bss = &joined_bss;
            }
        } else {
            curr_bss = s->bss_count ? &s->bss[0] : NULL;
        }
        if (!curr_bss) {
            status = -1;
            break;
        }
        size_t blen = wifi_bss_encode(s, curr_bss, out + 12, sizeof(out) - 12);
        wifi_st32(out, 12 + blen);
        wifi_st32(out + 4, 109);
        wifi_st32(out + 8, 1);
        outlen = wifi_ld32(out);
        break;
    }
    case 26: {
        if(!s->up) { status=-4; break; }
        if(inlen < 4) { wifi_event(s,0,3,0,NULL,NULL,0); break; }
        uint32_t sl = wifi_ld32(p+16);
        if(sl > 32 || inlen < 4 + sl) {
            wifi_event(s,0,3,0,NULL,NULL,0); break;
        }
        const char *req_ssid = (const char *)(p + 20);
        int match = -1;
        for (unsigned i = 0; i < s->bss_count; i++) {
            if (strlen(s->bss[i].ssid) == sl && !memcmp(s->bss[i].ssid, req_ssid, sl)) {
                match = (int)i;
                break;
            }
        }
        if (match < 0) {
            wifi_event(s,0,3,0,NULL,NULL,0); break;
        }
        if(s->count>WIFI_QUEUE-4) { status=-22; break; }
        s->joined = true;
        memcpy(s->joined_ssid, s->bss[match].ssid, sizeof(s->joined_ssid));
        memcpy(s->joined_bssid, s->bss[match].bssid, 6);
        s->joined_channel = s->bss[match].channel;
        s->joined_rssi = s->bss[match].rssi;
        s->joined_sec = s->bss[match].security;

        wifi_event(s,0,0,0,s->joined_bssid,NULL,0);
        wifi_event(s,1,0,0,s->joined_bssid,NULL,0);
        wifi_event(s,7,0,0,s->joined_bssid,NULL,0);
        wifi_event(s,16,0,1,s->joined_bssid,NULL,0);
        /* Secured network: the on-chip supplicant reports WLC_E_PSK_SUP with
         * WLC_SUP_KEYED (6); without it the Darwin driver treats the join as a
         * wrong password. The 4-way handshake itself is not modeled. */
        if(s->joined_sec != WIFI_SEC_OPEN)
            wifi_event(s,46,6,0,s->joined_bssid,NULL,0);
        break;
    }
    case 37: wifi_st32(out,0); outlen=4; break;
    case 38: break;
    case 52: wifi_disconnect(s); break;
    /* wlioctl.h: WLC_GET_BSSID 23, WLC_GET_SSID 25 (the 11D257 driver's
     * command log confirms the standard numbering, e.g. WLC_SET_PM 86). */
    case 23: if(!s->joined) status=-17; else { memcpy(out,s->joined_bssid,6); outlen=6; } break;
    case 25: {
        size_t sl = s->joined ? strlen(s->joined_ssid) : 0;
        wifi_st32(out, sl);
        if(s->joined) memcpy(out+4, s->joined_ssid, sl);
        outlen = 36;
        break;
    }
    case 127: wifi_st32(out, (uint32_t)(s->joined ? s->joined_rssi : -45)); outlen=4; break;
    case 29: {
        uint32_t ch = s->joined ? s->joined_channel : (s->bss_count ? s->bss[0].channel : 6);
        wifi_st32(out, ch); wifi_st32(out+4, ch); wifi_st32(out+8, ch); outlen=12; break;
    }
    /* Ordinary radio configuration ioctls needed for an open STA. */
    case 20: case 22: case 30: case 45: case 49: case 55: case 56: case 57: case 86:
    case 118: case 185: case 187: case 245: case 258: case 268: break;
    /* Open-network join (AppleBCMWLANJoinManager::join): WLC_SET_WSEC 134 and
     * WLC_SET_WPA_AUTH 165 take 0; their GETs (133, 164) report 0. */
    case 134: case 165: break;
    case 133: case 164: wifi_st32(out,0); outlen=4; break;
    default: status=-23; break;
    }
    memcpy(s->response,p,16); wifi_st32(s->response+4,outlen);
    wifi_st32(s->response+8,(flags & ~1u) | (status?1:0));
    wifi_st32(s->response+12,(uint32_t)status);
    memcpy(s->response+16,out,outlen); s->response_len=16+outlen; return true;
}

/* HSIC extended interrupt header (f66324/f689b8): valid bit15,
 * channel1 (0x9001, CDC) or channel2 (0xa001, RX) in bits14:12,
 * one's-complement checksum, sequence at +6. Bulk RX headers share the
 * channel 2 sequence (+0x366); completeInterruptRead (f5d224) accepts only
 * channel 1 on the interrupt pipe; queued RX resumes a suspended port by
 * USB remote wakeup instead. */
static void wifi_notification_channel(WifiProtocol *s, uint8_t out[16], int channel)
{
    memset(out,0,16);
    uint16_t hdr = (channel == 2) ? 0xa001 : 0x9001;
    uint16_t seq = (channel == 2) ? s->notification_seq_rx++ : s->notification_seq++;
    wifi_st16(out,hdr);
    wifi_st16(out+4,16);
    wifi_st16(out+6,seq);
    uint32_t sum=0;
    for(unsigned i=0;i<16;i+=2) sum+=wifi_ld16(out+i);
    sum=(sum&0xffff)+(sum>>16);
    wifi_st16(out+2,(uint16_t)~sum);
}

static void wifi_notification(WifiProtocol *s, uint8_t out[16])
{
    wifi_notification_channel(s, out, 1);
}

static bool wifi_bdc_payload(const uint8_t *p, size_t n, size_t *off)
{
    if(n >= 20 && p[0] == 0x01 && p[1] == 0xa0) {
        p += 16; n -= 16;
        if(n < 4 || p[0]>>4 != 2 || (p[2]&15)) return false;
        *off = 16 + 4 + (size_t)p[3]*4;
        return *off <= n + 16 && (n + 16) - *off >= 14;
    }
    if(n<4 || p[0]>>4!=2 || (p[2]&15)) return false;
    *off=4+(size_t)p[3]*4; return *off<=n && n-*off>=14;
}

#endif
