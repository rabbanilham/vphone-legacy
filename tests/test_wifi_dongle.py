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
 assert(wifi_ld32(b)==109 && !memcmp(b+8,wifi_bssid,6));
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
 var(true,"escan",scan,8); assert(w.count==2);
 f=w.queue[0].data; assert(wifi_ld16(f)==0xa001 && f[16]==0x20 && f[20+31]==69);
 esc=f+20+72; b=esc+12;
 assert(wifi_ld32(esc+4)==109 && wifi_ld16(esc+8)==0x1234 && wifi_ld16(esc+10)==1);
 assert(wifi_ld16(b+72)==0x2b06 && b[76]==1 && (int16_t)wifi_ld16(b+78)==-45);
 assert((int8_t)b[80]==-92 && !wifi_ld32(b+84) && b[88]==6 && wifi_ld16(b+124)==47);
 assert(wifi_ld16(b+116)==128 && 128+wifi_ld32(b+120)==wifi_ld32(b+4) && b[128]==0 && b[129]==12);
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
