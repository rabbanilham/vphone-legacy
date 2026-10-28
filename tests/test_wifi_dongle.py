"""Host execution of the actual dependency-free dongle protocol engine."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WifiDongleTests(unittest.TestCase):
    def test_wire_protocol_and_invalid_inputs(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        source = r'''
#include <assert.h>
#include "wifi_protocol.h"
static WifiProtocol w;
static void cmd(unsigned op, unsigned flags, const void *p, size_t n) {
 uint8_t b[4096]={0}; wifi_st32(b,op); wifi_st32(b+4,n);
 wifi_st32(b+8,flags | 0x12340000); if(n) memcpy(b+16,p,n);
 assert(wifi_ioctl(&w,b,n+16));
 assert(wifi_ld32(w.response)==op);
 assert((wifi_ld32(w.response+8)&0xffff0000)==0x12340000);
}
static void var(bool set, const char *name, const void *p, size_t n) {
 uint8_t b[512]; size_t k=strlen(name)+1; memcpy(b,name,k);
 if(n) memcpy(b+k,p,n); cmd(set?263:262,set?2:0,b,k+n);
}
int main(void) {
 wifi_protocol_init(&w,"Test network");
 assert(w.dl_state==0 && !w.runtime && (w.mac[0]&3)==2);
 assert(wifi_device_descriptor[8]==0x5c && wifi_device_descriptor[10]==0x34);
 assert(sizeof(wifi_config_descriptor)==39 && wifi_config_descriptor[14]==0xff);
 assert(wifi_config_descriptor[20]==0x81 && wifi_config_descriptor[27]==0x82 && wifi_config_descriptor[34]==2);
 uint8_t trx[3001]={0}; wifi_st32(trx,0x30524448); wifi_st32(trx+4,sizeof(trx));
 for(unsigned i=12;i<sizeof(trx);i++) trx[i]=i;
 wifi_st32(trx+8,wifi_crc(0xffffffff,trx+12,sizeof(trx)-12));
 assert(wifi_download(&w,trx,7)); assert(w.dl_state==1);
 assert(wifi_download(&w,trx+7,1493)); assert(w.dl_state==1);
 assert(wifi_download(&w,trx+1500,1500)); assert(w.dl_state==1);
 assert(wifi_download(&w,trx+3000,1)); assert(w.dl_state==4 && w.dl_bytes==3001);
 assert(!wifi_download(&w,trx,1));
 wifi_protocol_init(&w,"Test network"); trx[30]^=1;
 assert(!wifi_download(&w,trx,sizeof(trx))); assert(w.dl_state==5);
 wifi_protocol_init(&w,"Test network"); trx[0]=0;
 assert(!wifi_download(&w,trx,sizeof(trx))); assert(w.dl_state==3);
 wifi_protocol_init(&w,"Test network");
 assert(!wifi_ioctl(&w,trx,15));
 cmd(0,0,NULL,0); assert(wifi_ld32(w.response+16)==0x14e4);
 cmd(1,0,NULL,0); assert(wifi_ld32(w.response+16)==2);
 var(false,"ver",NULL,0); assert(strstr((char *)w.response+16,"6.25.87.120"));
 var(false,"cap",NULL,0); assert(!strcmp((char *)w.response+16,"sta escan"));
 var(false,"unknown",NULL,0); assert((int32_t)wifi_ld32(w.response+12)==-23 && (wifi_ld32(w.response+8)&1));
 uint8_t bad[3]={'b','a','d'}; cmd(262,0,bad,3); assert((int32_t)wifi_ld32(w.response+12)==-2);
 uint8_t mac[6]={2,3,4,5,6,7}; var(true,"cur_etheraddr",mac,6);
 var(false,"cur_etheraddr",NULL,0); assert(!memcmp(w.response+16,mac,6));
 var(true,"cur_etheraddr",mac,5); assert((int32_t)wifi_ld32(w.response+12)==-2);
 uint8_t country[12]={'U','S'}; var(true,"country",country,12);
 var(false,"country",NULL,0); assert(!memcmp(country,w.response+16,12));
 for(unsigned i=0;i<sizeof(wifi_knobs)/sizeof(*wifi_knobs);i++) {
  uint8_t v[4]={1}; var(true,wifi_knobs[i],v,4); assert(!wifi_ld32(w.response+12));
  var(false,wifi_knobs[i],NULL,0); assert(wifi_ld32(w.response+16)==1);
 }
 uint8_t scan[8]={1,0,0,0,1,0,0x34,0x12};
 var(true,"escan",scan,8); assert(w.count==0); /* mask honoured */
 uint8_t mask[16]; memset(mask,255,16); var(true,"event_msgs",mask,16);
 var(false,"event_msgs",NULL,0); assert(!memcmp(w.response+16,mask,16));
 var(true,"escan",scan,8); assert(w.count==2);
 const uint8_t *f=w.queue[0].data;
 assert(f[0]==0x20 && f[3]==0 && !memcmp(f+4,mac,6));
 assert(f[16]==0x88 && f[17]==0x6c && f[18]==0x80 && f[19]==1);
 assert(f[35]==67 && f[39]==8); /* big-endian event/status */
 const uint8_t *esc=f+4+72, *b=esc+12;
 assert(wifi_ld16(esc+8)==0x1234 && wifi_ld16(esc+10)==1);
 uint8_t exp_bssid[6]; wifi_bssid_derive("Test network", exp_bssid);
 assert(wifi_ld32(b)==109 && !memcmp(b+8,exp_bssid,6));
 assert(wifi_ld16(b+16)==1 && b[18]==12 && !memcmp(b+19,"Test network",12));
 assert((int16_t)wifi_ld16(b+76)==-45 && wifi_ld16(b+72)==0x1006);
 size_t ie=wifi_ld32(b+120), end=ie+wifi_ld16(b+116);
 while(ie<end) { assert(b[ie]!=48 && b[ie]!=221); ie+=2+b[ie+1]; }
 assert(ie==end && wifi_ld32(b+4)==end);
 assert(w.queue[1].data[39]==0); w.count=w.head=0;
 cmd(50,0,NULL,0);assert(w.count==3 && w.queue[2].data[35]==26);
 cmd(51,0,NULL,0);assert(wifi_ld32(w.response+24)==1);w.count=w.head=0;
 uint8_t ssid[36]={12,0,0,0}; memcpy(ssid+4,"Test network",12);
 cmd(26,2,ssid,36); assert((int32_t)wifi_ld32(w.response+12)==-4 && !w.joined);
 cmd(2,0,NULL,0); cmd(26,2,ssid,36); assert(w.joined && w.count==4);
 unsigned ev[]={0,1,7,16}; for(unsigned i=0;i<4;i++) assert(w.queue[i].data[35]==ev[i]);
 assert(w.queue[3].data[31]==1); cmd(52,0,NULL,0); assert(!w.joined && w.count==6);
 cmd(3,0,NULL,0); assert(!w.up);
 w.count=w.head=0;
 uint8_t frame[64]={0}; for(unsigned i=0;i<WIFI_QUEUE;i++) assert(wifi_enqueue(&w,frame,64));
 assert(!wifi_enqueue(&w,frame,64)); w.count=0;
 assert(!wifi_enqueue(&w,frame,WIFI_FRAME_MAX));
 uint8_t notification[16]; wifi_notification(&w,notification);
 assert((wifi_ld16(notification)&0xf000)==0x9000 && wifi_ld16(notification+6)==0);
 unsigned sum=0;for(unsigned i=0;i<16;i+=2) sum+=wifi_ld16(notification+i);
 assert(((sum&65535)+(sum>>16))==65535);
 wifi_notification(&w,notification);assert(wifi_ld16(notification+6)==1);
 size_t off; uint8_t tx[64]={0x20,0,0,2};
 assert(wifi_bdc_payload(tx,sizeof(tx),&off) && off==12);
 tx[3]=255; assert(!wifi_bdc_payload(tx,sizeof(tx),&off));
 tx[3]=0; tx[0]=0x10; assert(!wifi_bdc_payload(tx,sizeof(tx),&off));
 /* Darwin 11D257: legacy chanspecs and the aligned wlioctl.h v109 layout. */
 wifi_protocol_init(&w,"Test network"); w.darwin=true;
 var(false,"chanspec",NULL,0); assert(wifi_ld32(w.response+16)==0x2b06);
 var(false,"chanspecs",NULL,0);
 assert(wifi_ld32(w.response+16)==11 && wifi_ld32(w.response+20)==0x2b01 && wifi_ld32(w.response+60)==0x2b0b);
 var(true,"escan",scan,8); assert(w.count==0 && w.scan_pending); /* deferred */
 wifi_scan_finish(&w); assert(w.count==2 && !w.scan_pending);
 wifi_scan_finish(&w); assert(w.count==2);
 f=w.queue[0].data; assert(wifi_ld16(f)==0xa001 && f[16]==0x20 && f[20+31]==69);
 esc=f+20+72; b=esc+12;
 assert(wifi_ld32(esc+4)==109 && wifi_ld16(esc+8)==0x1234 && wifi_ld16(esc+10)==1);
 assert(wifi_ld16(b+72)==0x2b06 && b[76]==1 && (int16_t)wifi_ld16(b+78)==-45);
 assert((int8_t)b[80]==-92 && !wifi_ld32(b+84) && b[88]==6 && wifi_ld16(b+124)==47);
 assert(wifi_ld16(b+116)==128 && 128+wifi_ld32(b+120)==wifi_ld32(b+4) && b[128]==0 && b[129]==12);
 w.count=w.head=0;
 uint8_t abort_scan[8]={1,0,0,0,3,0,0x35,0x12};
 var(true,"escan",abort_scan,8); assert(!wifi_ld32(w.response+12) && w.count==0); /* idle abort */
 var(true,"escan",scan,8); var(true,"escan",abort_scan,8);
 assert(!w.scan_pending && w.count==1 && w.queue[0].data[20+31]==69 && w.queue[0].data[20+35]==4);
 wifi_scan_finish(&w); assert(w.count==1);
 /* Open join commands from AppleBCMWLANJoinManager::join. */
 uint8_t zero[4]={0}, ka[8]={0};
 var(true,"keep_alive",ka,8); assert(!wifi_ld32(w.response+12));
 cmd(165,2,zero,4); assert(!wifi_ld32(w.response+12));
 cmd(134,2,zero,4); assert(!wifi_ld32(w.response+12));
 cmd(164,0,NULL,0); assert(!wifi_ld32(w.response+12) && wifi_ld32(w.response+4)==4);
 cmd(23,0,NULL,0); assert((int32_t)wifi_ld32(w.response+12)==-17); /* GET_BSSID, unjoined */
 cmd(25,0,NULL,0); assert(!wifi_ld32(w.response+12) && !wifi_ld32(w.response+16));

 /* --- Contract extension: JSON parser and multi-BSS table tests --- */
 /* 1. Invalid JSON inputs fall back to legacy single AP */
 wifi_protocol_init(&w, "Test network");
 assert(w.bss_count == 1 && !strcmp(w.bss[0].ssid, "Test network"));
 assert(!wifi_scan_parse_json(&w, NULL, 0));
 assert(!wifi_scan_parse_json(&w, "{bad json", 9));
 /* Version mismatch */
 const char *bad_v = "{\"version\":2,\"networks\":[{\"ssid\":\"a\",\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_v, strlen(bad_v)));
 /* Empty networks array */
 const char *empty_nets = "{\"version\":1,\"networks\":[]}";
 assert(!wifi_scan_parse_json(&w, empty_nets, strlen(empty_nets)));
 /* Missing connected:true (0 connected) */
 const char *no_conn = "{\"version\":1,\"networks\":[{\"ssid\":\"a\",\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":false}]}";
 assert(!wifi_scan_parse_json(&w, no_conn, strlen(no_conn)));
 /* Multiple connected:true (2 connected) */
 const char *multi_conn = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"a\",\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":true},"
   "{\"ssid\":\"b\",\"channel\":6,\"rssi\":-60,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, multi_conn, strlen(multi_conn)));
 /* Invalid security mode */
 const char *bad_sec = "{\"version\":1,\"networks\":[{\"ssid\":\"a\",\"channel\":1,\"rssi\":-50,\"security\":\"wep\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_sec, strlen(bad_sec)));
 /* Out-of-bounds channel */
 const char *bad_ch0 = "{\"version\":1,\"networks\":[{\"ssid\":\"a\",\"channel\":0,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_ch0, strlen(bad_ch0)));
 const char *bad_ch15 = "{\"version\":1,\"networks\":[{\"ssid\":\"a\",\"channel\":15,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_ch15, strlen(bad_ch15)));
 /* Out-of-bounds RSSI */
 const char *bad_rssi1 = "{\"version\":1,\"networks\":[{\"ssid\":\"a\",\"channel\":1,\"rssi\":-101,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_rssi1, strlen(bad_rssi1)));
 const char *bad_rssi2 = "{\"version\":1,\"networks\":[{\"ssid\":\"a\",\"channel\":1,\"rssi\":-29,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_rssi2, strlen(bad_rssi2)));
 /* Empty SSID */
 const char *bad_ssid = "{\"version\":1,\"networks\":[{\"ssid\":\"\",\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_ssid, strlen(bad_ssid)));

 /* 2. Valid multi-AP JSON with open, wpa, wpa2, wpa3, enterprise */
 const char *valid_json = "{\n"
   "  \"version\": 1,\n"
   "  \"updated\": 1700000000,\n"
   "  \"networks\": [\n"
   "    {\"ssid\": \"Home-5G\", \"channel\": 6, \"rssi\": -45, \"security\": \"open\", \"connected\": true},\n"
   "    {\"ssid\": \"Office-Secure\", \"channel\": 1, \"rssi\": -65, \"security\": \"wpa2\", \"connected\": false},\n"
   "    {\"ssid\": \"Legacy-WPA\", \"channel\": 11, \"rssi\": -75, \"security\": \"wpa\", \"connected\": false},\n"
   "    {\"ssid\": \"NextGen-WPA3\", \"channel\": 3, \"rssi\": -55, \"security\": \"wpa3\", \"connected\": false},\n"
   "    {\"ssid\": \"Corp-8021X\", \"channel\": 9, \"rssi\": -70, \"security\": \"enterprise\", \"connected\": false}\n"
   "  ]\n"
   "}";
 assert(wifi_scan_parse_json(&w, valid_json, strlen(valid_json)));
 assert(w.bss_count == 5);
 assert(!strcmp(w.bss[0].ssid, "Home-5G") && w.bss[0].channel == 6 && w.bss[0].rssi == -45 && w.bss[0].security == WIFI_SEC_OPEN && w.bss[0].connected);
 assert(!strcmp(w.bss[1].ssid, "Office-Secure") && w.bss[1].channel == 1 && w.bss[1].rssi == -65 && w.bss[1].security == WIFI_SEC_WPA2 && !w.bss[1].connected);
 assert(!strcmp(w.bss[2].ssid, "Legacy-WPA") && w.bss[2].channel == 11 && w.bss[2].rssi == -75 && w.bss[2].security == WIFI_SEC_WPA && !w.bss[2].connected);
 assert(!strcmp(w.bss[3].ssid, "NextGen-WPA3") && w.bss[3].channel == 3 && w.bss[3].rssi == -55 && w.bss[3].security == WIFI_SEC_WPA3 && !w.bss[3].connected);
 assert(!strcmp(w.bss[4].ssid, "Corp-8021X") && w.bss[4].channel == 9 && w.bss[4].rssi == -70 && w.bss[4].security == WIFI_SEC_ENTERPRISE && !w.bss[4].connected);

 /* Verify BSSID derivation uniqueness and local administered bit */
 for(unsigned i=0; i<5; i++) {
  assert((w.bss[i].bssid[0] & 3) == 2); /* unicast, locally administered */
  for(unsigned j=i+1; j<5; j++) {
   assert(memcmp(w.bss[i].bssid, w.bss[j].bssid, 6) != 0);
  }
 }

 /* 3. Multi-AP Escan Results (Darwin mode) */
 w.darwin = true;
 w.count = w.head = 0;
 var(true, "escan", scan, 8);
 assert(w.scan_pending && w.count == 0);
 wifi_scan_finish(&w);
 /* 5 BSS entries + 1 SCAN_DONE = 6 queue entries */
 assert(w.count == 6);
 for(unsigned i=0; i<5; i++) {
  const uint8_t *fr = w.queue[i].data;
  assert(fr[20+31] == 69 && fr[20+35] == 8); /* ESCAN_RESULT, status 8 */
  const uint8_t *bi = fr + 20 + 72 + 12;
  assert(wifi_ld32(bi) == 109);
  assert(!memcmp(bi+8, w.bss[i].bssid, 6));
  assert(bi[88] == w.bss[i].channel);
  assert((int16_t)wifi_ld16(bi+78) == w.bss[i].rssi);
  /* Capability check: Privacy bit (0x10) must be set for secured, 0 for open */
  uint16_t cap = wifi_ld16(bi+16);
  if (w.bss[i].security == WIFI_SEC_OPEN) {
   assert((cap & 0x0010) == 0);
  } else {
   assert((cap & 0x0010) == 0x0010);
  }
  /* Check IEs */
  size_t cur = 128, ielen = wifi_ld32(bi+120);
  bool has_rsn = false, has_wpa = false;
  while(cur < 128 + ielen) {
   if (bi[cur] == 48) has_rsn = true;
   if (bi[cur] == 221) has_wpa = true;
   cur += 2 + bi[cur+1];
  }
  assert(cur == 128 + ielen);
  if (w.bss[i].security == WIFI_SEC_OPEN) {
   assert(!has_rsn && !has_wpa);
  } else if (w.bss[i].security == WIFI_SEC_WPA) {
   assert(has_wpa && !has_rsn);
  } else {
   assert(has_rsn && !has_wpa);
  }
 }
 /* 6th entry is SCAN_DONE: status 0 */
 assert(w.queue[5].data[20+35] == 0);
 w.count = w.head = 0;

 /* 4. Join second SSID (Office-Secure) */
 cmd(2, 0, NULL, 0); /* Bring interface UP */
 uint8_t join_second[36] = {13, 0, 0, 0};
 memcpy(join_second + 4, "Office-Secure", 13);
 cmd(26, 2, join_second, 36);
 assert(w.joined && w.count == 5); /* SET_SSID, JOIN, AUTH, LINK, PSK_SUP keyed */
 assert(!strcmp(w.joined_ssid, "Office-Secure"));
 assert(!memcmp(w.joined_bssid, w.bss[1].bssid, 6));
 assert(w.joined_channel == 1);
 assert(w.joined_rssi == -65);
 /* Verify link state queries report joined network */
 cmd(23, 0, NULL, 0);
 assert(!wifi_ld32(w.response+12) && !memcmp(w.response+16, w.bss[1].bssid, 6)); /* GET_BSSID */
 cmd(25, 0, NULL, 0);
 assert(!wifi_ld32(w.response+12) && wifi_ld32(w.response+16) == 13 && !memcmp(w.response+20, "Office-Secure", 13)); /* GET_SSID */
 cmd(127, 0, NULL, 0);
 assert(!wifi_ld32(w.response+12) && (int32_t)wifi_ld32(w.response+16) == -65); /* GET_RSSI */
 cmd(29, 0, NULL, 0);
 assert(!wifi_ld32(w.response+12) && wifi_ld32(w.response+16) == 1); /* GET_CHANNEL */
 var(false, "chanspec", NULL, 0);
 assert(wifi_ld32(w.response+16) == 0x2b01); /* channel 1 chanspec */

 /* 5. Joining unknown SSID fails */
 uint8_t join_unknown[36] = {7, 0, 0, 0};
 memcpy(join_unknown + 4, "Unknown", 7);
 w.count = w.head = 0;
 cmd(26, 2, join_unknown, 36);
 assert(w.count == 1 && w.queue[0].data[20+31] == 0 && w.queue[0].data[20+35] == 3); /* event 0, status 3 */

 /* 6. Emoji SSID via surrogate pairs and 32-byte limit enforcement */
 /* Valid surrogate pair \uD83C\uDF89 -> 4-byte UTF-8 emoji */
 const char *emoji_json = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83C\\uDF89-party\",\"channel\":6,\"rssi\":-45,\"security\":\"open\",\"connected\":true}]}";
 assert(wifi_scan_parse_json(&w, emoji_json, strlen(emoji_json)));
 assert(w.bss_count == 1);
 assert(!strcmp(w.bss[0].ssid, "\xf0\x9f\x8e\x89-party"));
 uint8_t emoji_bssid[6];
 wifi_bssid_derive("\xf0\x9f\x8e\x89-party", emoji_bssid);
 assert(!memcmp(w.bss[0].bssid, emoji_bssid, 6));

 /* Lone high surrogate rejected */
 const char *lone_high = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83C\",\"channel\":6,\"rssi\":-45,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, lone_high, strlen(lone_high)));

 /* Lone low surrogate rejected */
 const char *lone_low = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uDF89\",\"channel\":6,\"rssi\":-45,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, lone_low, strlen(lone_low)));

 /* High surrogate followed by non-surrogate \u escape rejected */
 const char *bad_pair = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83C\\u0041\",\"channel\":6,\"rssi\":-45,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_pair, strlen(bad_pair)));

 /* High surrogate followed by literal char rejected */
 const char *bad_pair2 = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83Chello\",\"channel\":6,\"rssi\":-45,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, bad_pair2, strlen(bad_pair2)));

 /* 8 emojis = 8 * 4 = 32 UTF-8 bytes (exactly at 32-byte SSID limit) -> succeeds */
 const char *emojis_32 = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00"
   "\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\","
   "\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(wifi_scan_parse_json(&w, emojis_32, strlen(emojis_32)));
 assert(w.bss_count == 1);
 assert(strlen(w.bss[0].ssid) == 32);

 /* 8 emojis + 1 ASCII byte = 33 UTF-8 bytes -> rejected after decoding */
 const char *emojis_33 = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00"
   "\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00X\","
   "\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, emojis_33, strlen(emojis_33)));

 /* 9 emojis = 36 UTF-8 bytes -> rejected after decoding */
 const char *emojis_36 = "{\"version\":1,\"networks\":["
   "{\"ssid\":\"\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00"
   "\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\\uD83D\\uDE00\","
   "\"channel\":1,\"rssi\":-50,\"security\":\"open\",\"connected\":true}]}";
 assert(!wifi_scan_parse_json(&w, emojis_36, strlen(emojis_36)));

 /* 7. Joined AP removed after scan refresh (Command 51 GET_BSS_INFO) */
 /* Reset and reload multi-AP JSON */
 wifi_protocol_init(&w, "Test network");
 assert(wifi_scan_parse_json(&w, valid_json, strlen(valid_json)));
 cmd(2, 0, NULL, 0); /* UP */
 cmd(26, 2, join_second, 36); /* Join Office-Secure (channel 1, rssi -65, sec wpa2) */
 assert(w.joined);

 /* Cmd 51 reports joined AP while it is in the scan table */
 cmd(51, 0, NULL, 0);
 assert(!wifi_ld32(w.response + 12));
 const uint8_t *b51 = w.response + 16 + 12;
 assert(wifi_ld32(b51) == 109);
 assert(b51[18] == 13 && !memcmp(b51 + 19, "Office-Secure", 13));
 assert(!memcmp(b51 + 8, w.joined_bssid, 6));
 assert(b51[88] == 1 && (int16_t)wifi_ld16(b51 + 76) == -65);
 assert((wifi_ld16(b51 + 16) & 0x0010) == 0x0010); /* Privacy bit */

 /* Now scan is refreshed with a new JSON where Office-Secure is no longer present */
 const char *refreshed_json = "{\n"
   "  \"version\": 1,\n"
   "  \"updated\": 1700000001,\n"
   "  \"networks\": [\n"
   "    {\"ssid\": \"Home-5G\", \"channel\": 6, \"rssi\": -45, \"security\": \"open\", \"connected\": true},\n"
   "    {\"ssid\": \"Brand-New-AP\", \"channel\": 11, \"rssi\": -80, \"security\": \"wpa3\", \"connected\": false}\n"
   "  ]\n"
   "}";
 assert(wifi_scan_parse_json(&w, refreshed_json, strlen(refreshed_json)));
 assert(w.bss_count == 2);
 assert(strcmp(w.bss[0].ssid, "Office-Secure") != 0 && strcmp(w.bss[1].ssid, "Office-Secure") != 0);

 /* Cmd 51 MUST report the joined AP from joined_* fields, never bss[0] ("Home-5G") */
 cmd(51, 0, NULL, 0);
 assert(!wifi_ld32(w.response + 12));
 b51 = w.response + 16 + 12;
 assert(wifi_ld32(b51) == 109);
 assert(b51[18] == 13 && !memcmp(b51 + 19, "Office-Secure", 13));
 assert(strcmp((const char *)(b51 + 19), "Home-5G") != 0);
 assert(!memcmp(b51 + 8, w.joined_bssid, 6));
 assert(b51[88] == 1);
 assert((int16_t)wifi_ld16(b51 + 76) == -65);
 assert((wifi_ld16(b51 + 16) & 0x0010) == 0x0010);

 /* 8. IE length consistency for all security types (open, wpa, wpa2, wpa3, enterprise) */
 /* In both Darwin and non-Darwin modes, walk IEs and assert they end exactly at ie_length */
 for (int darwin_mode = 0; darwin_mode <= 1; darwin_mode++) {
  w.darwin = (bool)darwin_mode;
  memset(w.events, 0xff, sizeof(w.events));
  assert(wifi_scan_parse_json(&w, valid_json, strlen(valid_json)));
  w.count = w.head = 0;
  w.scan_pending = false;
  wifi_scan(&w, 0x5678);
  assert(w.count == w.bss_count + 1); /* BSS entries + SCAN_DONE */
  for (unsigned i = 0; i < w.bss_count; i++) {
   const uint8_t *pkt = w.queue[i].data;
   const uint8_t *bss_raw = w.darwin ? (pkt + 20 + 72 + 12) : (pkt + 4 + 72 + 12);
   size_t ie_off = w.darwin ? wifi_ld16(bss_raw + 116) : (size_t)wifi_ld32(bss_raw + 120);
   size_t ie_len = w.darwin ? (size_t)wifi_ld32(bss_raw + 120) : (size_t)wifi_ld16(bss_raw + 116);
   assert(ie_off == 128);
   size_t total_bss_len = wifi_ld32(bss_raw + 4);
   assert(ie_off + ie_len == total_bss_len);

   size_t cur = ie_off;
   size_t end = ie_off + ie_len;
   while (cur < end) {
    assert(cur + 2 <= end);
    uint8_t elem_id = bss_raw[cur];
    uint8_t elem_len = bss_raw[cur + 1];
    cur += 2 + elem_len;
    (void)elem_id;
   }
   /* MUST end exactly at ie_length */
   assert(cur == end);
  }
 }

 return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='wifi-host-') as tmp:
            root = Path(tmp)
            (root / 'test.c').write_text(source)
            result = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', '-I', str(ROOT / 'backend'),
                                    str(root / 'test.c'), '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(root / 'test')], check=True)


if __name__ == '__main__':
    unittest.main()
