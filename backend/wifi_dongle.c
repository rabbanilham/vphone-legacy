/* SPDX-License-Identifier: GPL-2.0-or-later
 * Included by the board translation unit. USB and net callbacks, timers and
 * GPIO transitions run under BQL. Never call the NIC from a worker thread.
 */
#include "hw/usb/usb.h"
#include "../usb/desc.h"
#include "net/net.h"
#include "hw/core/qdev-properties-system.h"
#include "migration/vmstate.h"
#include "wifi_protocol.h"
#define TYPE_WIFI_DONGLE "bcm43342-wifi"
OBJECT_DECLARE_SIMPLE_TYPE(WifiDongle, WIFI_DONGLE)
struct WifiDongle {
    USBDevice parent_obj;
    NICConf conf;
    NICState *nic;
    char *ssid;
    WifiProtocol proto;
    QEMUTimer *reenumerate;
    bool detached_for_go, host_ready, rom_mode, resp_notified;
    uint8_t tx[WIFI_FRAME_MAX];
    size_t tx_len;
};
static const USBDescStrings wifi_strings = {
    [1]="Broadcom", [2]="BCM43342 HSIC", [3]="433420000001",
};
static const USBDescDevice wifi_usb_device = {
    .bcdUSB=0x0200, .bMaxPacketSize0=64, .bNumConfigurations=1,
    .confs=(USBDescConfig[]) {{
        .bNumInterfaces=1, .bConfigurationValue=1, .bmAttributes=0xc0,
        .nif=1, .ifs=(USBDescIface[]) {{
            .bInterfaceNumber=0, .bNumEndpoints=3,
            .bInterfaceClass=0xff, .bInterfaceSubClass=0xff, .bInterfaceProtocol=0xff,
            .eps=(USBDescEndpoint[]) {
                {.bEndpointAddress=0x81,.bmAttributes=3,.wMaxPacketSize=64,.bInterval=1},
                {.bEndpointAddress=0x82,.bmAttributes=2,.wMaxPacketSize=512},
                {.bEndpointAddress=0x02,.bmAttributes=2,.wMaxPacketSize=512},
            },
        }},
    }},
};
static const USBDesc wifi_rom_desc = {
    .id={.idVendor=0x0a5c,.idProduct=0x4334,.bcdDevice=0x100,
         .iManufacturer=1,.iProduct=2,.iSerialNumber=3},
    .high=&wifi_usb_device,.str=wifi_strings,
};
static const USBDesc wifi_runtime_desc = {
    .id={.idVendor=0x0a5c,.idProduct=0xbd21,.bcdDevice=0x100,
         .iManufacturer=1,.iProduct=2,.iSerialNumber=3},
    .high=&wifi_usb_device,.str=wifi_strings,
};
static void wifi_wake(WifiDongle *s)
{
    if (!s->host_ready || !USB_DEVICE(s)->attached) return;
    usb_wakeup(usb_ep_get(USB_DEVICE(s),USB_TOKEN_IN,1),0);
    usb_wakeup(usb_ep_get(USB_DEVICE(s),USB_TOKEN_IN,2),0);
}
static void wifi_host_ready(WifiDongle *s, bool ready)
{
    s->host_ready=ready;
    /* The module is powered (DEVICE_READY), but cannot signal an HSIC
     * connection until the platform enables the host link. Low HOST_READY
     * after enumeration is bus idle, not power loss (DT idle-disconnect=0). */
    if(ready && !USB_DEVICE(s)->attached && !s->detached_for_go) {
        usb_device_attach(USB_DEVICE(s),&error_abort);
        qemu_log_mask(LOG_UNIMP,"WIFI HSIC attach host_ready=1 pid=%04x\n",
                      s->proto.runtime?0xbd21:0x4334);
    }
    qemu_log_mask(LOG_UNIMP,"WIFI HOST_READY=%u attached=%u\n",ready,USB_DEVICE(s)->attached);
    if(ready) { wifi_wake(s); qemu_flush_queued_packets(qemu_get_queue(s->nic)); }
}
static void wifi_reenumerate(void *opaque)
{
    WifiDongle *s=opaque; USBDevice *dev=USB_DEVICE(s);
    if(!s->detached_for_go) {
        if(dev->attached) usb_device_detach(dev);
        s->detached_for_go=true;
        timer_mod(s->reenumerate,qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)+20*1000*1000);
    } else {
        s->proto.runtime=true; dev->usb_desc=&wifi_runtime_desc;
        usb_desc_init(dev); s->detached_for_go=false;
        if(s->host_ready) usb_device_attach(dev,&error_abort);
        qemu_log_mask(LOG_UNIMP,"WIFI runtime attach 0a5c:bd21 attached=%u\n",dev->attached);
    }
}
static void wifi_control(USBDevice *dev, USBPacket *p, int request,
                         int value, int index, int length, uint8_t *data)
{
    WifiDongle *s=WIFI_DONGLE(dev); WifiProtocol *w=&s->proto;
    qemu_log_mask(LOG_UNIMP,"WIFI EP0 addr=%u speed=%u req=%04x value=%04x index=%04x length=%d\n",
                  dev->addr,dev->speed,request,value,index,length);
    /* QEMU's generic helper accepts unmatched nonzero configurations.
     * This device implements exactly configuration 1 and interface 0. */
    if(request==0x0009 && value!=0 && value!=1) goto stall;
    if((request==0x8100 || request==0x8200) && value==0) {
        bool valid=request==0x8100 ? dev->configuration==1 && index==0 :
            (index==0 || index==0x80 || (dev->configuration==1 &&
             (index==0x81 || index==0x82 || index==2)));
        if(!valid) goto stall;
        uint8_t status[2]={0,0};
        if(request==0x8200) {
            USBEndpoint *ep=usb_ep_get(dev,index&0x80?USB_TOKEN_IN:USB_TOKEN_OUT,index&15);
            status[0]=ep->halted;
        }
        p->actual_length=MIN(length,2); memcpy(data,status,p->actual_length); return;
    }
    if(usb_desc_handle_control(dev,p,request,value,index,length,data)>=0) {
        qemu_log_mask(LOG_UNIMP,"WIFI EP0 complete req=%04x actual=%d configuration=%d\n",
                      request,p->actual_length,dev->configuration);
        return;
    }
    if(request==0xc105 && value==1 && index==0) {
        uint8_t ver[8]={0x4e,0xa9,0,0,1,0,0,0};
        if(w->dl_state==4) {
            ver[0]=0x23; ver[1]=0xa1; ver[2]=0; ver[3]=0;
            ver[4]=0x01; ver[5]=0; ver[6]=0; ver[7]=0;
        }
        p->actual_length=MIN(length,8); memcpy(data,ver,p->actual_length);
        qemu_log_mask(LOG_UNIMP,"WIFI DL_GETVER dl_state=%u actual=%d ver=%02x%02x\n",
                      w->dl_state,p->actual_length,ver[0],ver[1]);
        return;
    }
    if(request==0xc100 && value==1 && index==0) {
        uint8_t state[8]={0,0,0,0, 0,0,0,0};
        wifi_st32(state,w->dl_state);
        wifi_st32(state+4,w->dl_bytes);
        p->actual_length=MIN(length,8); memcpy(data,state,p->actual_length);
        qemu_log_mask(LOG_UNIMP,"WIFI RDL request=%x state=%u bytes=%u\n",request,
                      w->dl_state,w->dl_bytes);
        return;
    }
    if(request==0xc102 && value==1 && index==0) {
        if(!w->runtime) {
            if(w->dl_state!=4) goto stall;
            uint8_t state[8]; wifi_st32(state,w->dl_state); wifi_st32(state+4,w->dl_bytes);
            p->actual_length=MIN(length,8); memcpy(data,state,p->actual_length);
            timer_mod(s->reenumerate,qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)+1000000);
        } else {
            uint8_t state[8]={0};
            wifi_st32(state,w->dl_state);
            wifi_st32(state+4,w->dl_bytes);
            p->actual_length=MIN(length,8); memcpy(data,state,p->actual_length);
        }
        qemu_log_mask(LOG_UNIMP,"WIFI DL_GO state=%u bytes=%u\n",w->dl_state,w->dl_bytes);
        return;
    }
    if(request==0xc111) {
        static const uint8_t bcm_cis[38] = {
            0x15, 0x22,                         /* CISTPL_VERS_1, length 34 */
            0x05, 0x00,                         /* major 5, minor 0 */
            's', '=', 'A', '1', 0x00,           /* String 0: "s=A1" (HSICChipInfo) */
            'M', '=', 'I', 'M', 'P', 'E', ' ',
            'V', '=', 'm', 0x00,                /* String 1: "M=IMPE V=m" (HSICRFHardwareInfo) */
            'M', 'u', 'r', 'a', 't', 'a', 0x00, /* String 2: "Murata" */
            'B', 'C', 'M', '4', '3', '3', '4', '2', 0x00, /* String 3: "BCM43342" */
            0xff, 0x00                          /* CISTPL_END + padding */
        };
        uint32_t addr = ((uint32_t)(uint16_t)index << 16) | (uint16_t)value;
        uint8_t resp[16] = {0};
        if(addr == 0x1800083a) {
            wifi_st32(resp + 12, sizeof(bcm_cis) << 3);
        } else if(addr == 0x18000838) {
            wifi_st32(resp + 12, 0);
        } else if(addr >= 0x18000800 && addr < 0x18000800 + sizeof(bcm_cis)) {
            uint32_t off = addr - 0x18000800;
            resp[12] = bcm_cis[off];
            resp[13] = (off + 1 < sizeof(bcm_cis)) ? bcm_cis[off + 1] : 0xff;
        }
        p->actual_length = MIN(length, (int)sizeof(resp));
        memcpy(data, resp, p->actual_length);
        qemu_log_mask(LOG_UNIMP, "WIFI OTP addr=%08x actual=%d resp12=%02x %02x\n",
                      addr, p->actual_length, resp[12], resp[13]);
        return;
    }
    if(w->runtime && index==0 && value==0) {
        if(request==0x2100) {
            bool rpc = (length >= 32 && data[0] == 0x01 && data[1] == 0x80);
            if(rpc) w->darwin = true;
            const uint8_t *cmd_data = rpc ? data + 16 : data;
            size_t cmd_len = rpc ? length - 16 : length;
            if(!wifi_ioctl(w, cmd_data, cmd_len)) goto stall;
            if(rpc) {
                uint8_t rpc_hdr[16] = {0};
                memcpy(rpc_hdr, data, 16);
                rpc_hdr[0] = 0x02; rpc_hdr[1] = 0x80;
                rpc_hdr[4] = (w->response_len + 16) & 0xff;
                rpc_hdr[5] = ((w->response_len + 16) >> 8) & 0xff;
                rpc_hdr[2] = 0; rpc_hdr[3] = 0;
                uint32_t sum = 0;
                for (int i = 0; i < 16; i += 2) {
                    if (i == 2) continue;
                    sum += (uint32_t)rpc_hdr[i] | ((uint32_t)rpc_hdr[i+1] << 8);
                }
                sum = (sum & 0xffff) + (sum >> 16);
                uint16_t csum = (uint16_t)(sum ^ 0xffff);
                rpc_hdr[2] = csum & 0xff;
                rpc_hdr[3] = (csum >> 8) & 0xff;
                memmove(w->response + 16, w->response, w->response_len);
                memcpy(w->response, rpc_hdr, 16);
                w->response_len += 16;
            }
            s->resp_notified = false;
            qemu_log_mask(LOG_UNIMP,"WIFI CDC rpc=%d cmd=%u status=%d response=%zu queued=%u\n",
                          rpc, wifi_ld32(cmd_data),
                          (int32_t)(rpc ? wifi_ld32(w->response + 28) : wifi_ld32(w->response + 12)),
                          w->response_len, w->count);
            if(cmd_len > 16 && (wifi_ld32(cmd_data)==262 || wifi_ld32(cmd_data)==263)) {
                qemu_log_mask(LOG_UNIMP,"WIFI iovar=%.*s status=%d\n",
                              (int)MIN(cmd_len-16,(size_t)64), cmd_data+16,
                              (int32_t)(rpc ? wifi_ld32(w->response + 28) : wifi_ld32(w->response + 12)));
            }
            qemu_format_nic_info_str(qemu_get_queue(s->nic),w->mac);
            wifi_wake(s); return;
        }
        if(request==0xa101 || request==0xa100) {
            p->actual_length=MIN((size_t)length,w->response_len);
            memcpy(data,w->response,p->actual_length); w->response_len=0;
            s->resp_notified=false;
            return;
        }
    }
stall:
    p->status=USB_RET_STALL;
}
static void wifi_data(USBDevice *dev, USBPacket *p)
{
    WifiDongle *s=WIFI_DONGLE(dev); WifiProtocol *w=&s->proto;
    if(!s->host_ready) { p->status=USB_RET_NAK; return; }
    if(p->pid==USB_TOKEN_IN && p->ep->nr==1) {
        if(!w->response_len || s->resp_notified) { p->status=USB_RET_NAK; return; }
        s->resp_notified=true;
        uint8_t intr[16]; wifi_notification(w,intr);
        usb_packet_copy(p,intr,MIN(p->iov.size,sizeof(intr))); return;
    }
    if(p->ep->nr!=2) { p->status=USB_RET_STALL; return; }
    if(p->pid==USB_TOKEN_IN) {
        if(!w->count) { p->status=USB_RET_NAK; return; }
        WifiFrame *f=&w->queue[w->head];
        size_t ev_off = w->darwin ? 20 : 4;
        if(!f->pos && f->len>=72+ev_off && f->data[12+ev_off]==0x88 && f->data[13+ev_off]==0x6c) {
            qemu_log_mask(LOG_UNIMP,"WIFI RX event=%u status=%u flags=%x\n",
                          ldl_be_p(f->data+28+ev_off),ldl_be_p(f->data+32+ev_off),
                          lduw_be_p(f->data+26+ev_off));
        }
        size_t n=MIN(p->iov.size,f->len-f->pos);
        usb_packet_copy(p,f->data+f->pos,n); f->pos+=n;
        /* Exact maxpacket frames need a ZLP to terminate a larger host read. */
        if(f->pos==f->len && (n%512 || n==0)) {
            w->head=(w->head+1)%WIFI_QUEUE; w->count--;
            qemu_flush_queued_packets(qemu_get_queue(s->nic));
        }
        return;
    }
    if(p->pid==USB_TOKEN_OUT) {
        uint8_t buf[WIFI_FRAME_MAX];
        if(p->iov.size>sizeof(buf)) { p->status=USB_RET_STALL; return; }
        usb_packet_copy(p,buf,p->iov.size);
        if(!w->runtime || w->dl_state!=4 || (p->iov.size>=4 && wifi_ld32(buf)==0x30524448)) {
            if (p->iov.size>=4 && wifi_ld32(buf)==0x30524448 && w->dl_state==4) {
                w->dl_state=0; w->dl_bytes=0; w->dl_crc=0xffffffff;
            }
            if(!wifi_download(w,buf,p->iov.size)) p->status=USB_RET_STALL;
            qemu_log_mask(LOG_UNIMP,"WIFI DL chunk size=%zu state=%u bytes=%u crc=%08x exp=%08x\n",
                          p->iov.size,w->dl_state,w->dl_bytes,w->dl_crc,w->dl_expected_crc);
            return;
        }
        if(p->iov.size>sizeof(s->tx)-s->tx_len) { s->tx_len=0; p->status=USB_RET_STALL; return; }
        memcpy(s->tx+s->tx_len,buf,p->iov.size); s->tx_len+=p->iov.size;
        if(p->iov.size%512 || !p->iov.size) {
            size_t off;
            if(w->up && w->joined && wifi_bdc_payload(s->tx,s->tx_len,&off))
                qemu_send_packet(qemu_get_queue(s->nic),s->tx+off,s->tx_len-off);
            s->tx_len=0;
        }
        return;
    }
    p->status=USB_RET_STALL;
}
static ssize_t wifi_receive(NetClientState *nc, const uint8_t *buf, size_t n)
{
    WifiDongle *s=qemu_get_nic_opaque(nc);
    assert(bql_locked());
    if(!s->proto.runtime || !s->proto.up || !s->proto.joined) return n;
    if(n>WIFI_FRAME_MAX-4) return n;
    if(!s->host_ready || s->proto.count >= WIFI_QUEUE-8 ||
       !wifi_enqueue(&s->proto,buf,n)) return 0;
    wifi_wake(s); return n;
}
static void wifi_net_cleanup(NetClientState *nc) { WIFI_DONGLE(qemu_get_nic_opaque(nc))->nic=NULL; }
static NetClientInfo wifi_net_info = {
    .type=NET_CLIENT_DRIVER_NIC,.size=sizeof(NICState),
    .receive=wifi_receive,.cleanup=wifi_net_cleanup,
};
static void wifi_reset(USBDevice *dev)
{
    WifiDongle *s=WIFI_DONGLE(dev);
    qemu_log_mask(LOG_UNIMP,"WIFI USB reset pid=%04x speed=%u\n",s->proto.runtime?0xbd21:0x4334,dev->speed);
    s->proto.response_len=0; s->proto.count=0; s->proto.head=0; s->tx_len=0;
    /* USB bus reset must preserve downloaded RAM and runtime personality. */
}
static void wifi_realize(USBDevice *dev, Error **errp)
{
    WifiDongle *s=WIFI_DONGLE(dev);
    dev->auto_attach=0; /* HSIC PHY starts only on the board HOST_READY signal. */
    const char *ssid=s->ssid?s->ssid:getenv("VPHONE_WIFI_SSID");
    if(!ssid || !*ssid) ssid="vphone-wifi";
    if(strlen(ssid)>32) { error_setg(errp,"Wi-Fi SSID exceeds 32 bytes"); return; }
    wifi_protocol_init(&s->proto,ssid);
    if(!memcmp(s->conf.macaddr.a,"\0\0\0\0\0\0",6)) memcpy(s->conf.macaddr.a,s->proto.mac,6);
    memcpy(s->proto.mac,s->conf.macaddr.a,6);
    if (s->rom_mode) {
        dev->usb_desc=&wifi_rom_desc;
        s->proto.runtime=false;
    } else {
        dev->usb_desc=&wifi_runtime_desc;
        s->proto.runtime=true;
    }
    usb_desc_init(dev);
    s->reenumerate=timer_new_ns(QEMU_CLOCK_VIRTUAL,wifi_reenumerate,s);
    s->nic=qemu_new_nic(&wifi_net_info,&s->conf,TYPE_WIFI_DONGLE,
                        dev->qdev.id,&dev->qdev.mem_reentrancy_guard,s);
    qemu_format_nic_info_str(qemu_get_queue(s->nic),s->proto.mac);
}
static void wifi_unrealize(USBDevice *dev)
{
    WifiDongle *s=WIFI_DONGLE(dev);
    timer_free(s->reenumerate); if(s->nic) qemu_del_nic(s->nic);
}
/* A whole-board power reset starts the ROM again. A USB bus reset above
 * deliberately preserves firmware RAM, as required during re-enumeration. */
static void wifi_power_reset(WifiDongle *s)
{
    USBDevice *dev=USB_DEVICE(s);
    char ssid[33]; memcpy(ssid,s->proto.ssid,sizeof(ssid));
    timer_del(s->reenumerate);
    if(dev->attached) usb_device_detach(dev);
    wifi_protocol_init(&s->proto,ssid);
    memcpy(s->proto.mac,s->conf.macaddr.a,6);
    s->detached_for_go=false; s->host_ready=false; s->tx_len=0;
    if (s->rom_mode) {
        dev->usb_desc=&wifi_rom_desc;
        s->proto.runtime=false;
    } else {
        dev->usb_desc=&wifi_runtime_desc;
        s->proto.runtime=true;
    }
    usb_desc_init(dev);
}
static const Property wifi_properties[] = {
    DEFINE_NIC_PROPERTIES(WifiDongle,conf), DEFINE_PROP_STRING("ssid",WifiDongle,ssid),
    DEFINE_PROP_BOOL("rom",WifiDongle,rom_mode,false),
};
static const VMStateDescription wifi_vmstate = {.name=TYPE_WIFI_DONGLE,.unmigratable=1};
static void wifi_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc=DEVICE_CLASS(oc); USBDeviceClass *uc=USB_DEVICE_CLASS(oc);
    uc->realize=wifi_realize; uc->unrealize=wifi_unrealize;
    uc->handle_control=wifi_control; uc->handle_data=wifi_data;
    uc->handle_reset=wifi_reset; uc->handle_attach=usb_desc_attach;
    uc->usb_desc=&wifi_rom_desc; uc->product_desc="BCM43342 HSIC Wi-Fi";
    dc->vmsd=&wifi_vmstate; device_class_set_props(dc,wifi_properties);
}
static const TypeInfo wifi_type = {
    .name=TYPE_WIFI_DONGLE,.parent=TYPE_USB_DEVICE,
    .instance_size=sizeof(WifiDongle),.class_init=wifi_class_init,
};
