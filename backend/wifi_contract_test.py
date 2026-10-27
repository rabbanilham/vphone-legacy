#!/usr/bin/env python3
"""Real EHCI1 DMA/USB contract with a bare-metal AArch64 fixture, never iOS.

Reads both descriptors and ROM DL_GETSTATE via actual setup/data/status qTDs.
"""
import struct
from ehci_contract_test import main as run_contract
from gpio_contract_test import move64


def fixture():
    return runtime_fixture(enumeration_only=True)


def runtime_fixture(enumeration_only=False, join_second=False, target_ssid=None):
    """TRX -> runtime PID -> CDC -> association -> slirp ARP/DHCP replies."""
    import binascii
    code = move64(4, 0x800018000)
    address = 0
    expected = []
    op = 0x20c400010
    ram = 0x800000000
    qh, td, setup_addr, buf = 0x20000, 0x21000, 0x22000, 0x23000

    def write(addr, value):
        code.extend(move64(2, addr) + move64(0, value) + [0xb9000040])

    def words(addr, values):
        for i, word in enumerate(values):
            write(addr + 4 * i, word)

    def blob(addr, data):
        data += bytes(-len(data) % 4)
        words(addr, struct.unpack('<' + 'I' * (len(data)//4), data))

    def read(addr, value, mask=0xffffffff):
        code.extend(move64(2, addr) + [0xb9400043] + move64(5, mask) + [0x0a050063])
        code.append(0xb9000003 | len(expected) << 10 | 4 << 5)
        expected.append(value)

    def read_bytes(addr, data):
        while data:
            shift = addr % 4
            n = min(4-shift, len(data))
            mask = ((1 << (n*8))-1) << (shift*8)
            read(addr-shift, int.from_bytes(data[:n], 'little') << (shift*8), mask)
            addr += n
            data = data[n:]

    def poll(addr, bit, while_set):
        code.extend(move64(2, addr) + [0xb9400043,
                    (0x37000003 if while_set else 0x36000003) | (bit << 19) | (0x3fff << 5)])

    def port_reset():
        write(op + 0x44, 0x1100)
        write(op + 0x44, 0x1000)
        read(op + 0x44, 5, 5)

    def transfer(ep, pid, length, setup=None, payload=None, response_length=None):
        write(op, 0)
        words(ram+qh, [qh | 2, ((512 if ep else 64) << 16) | 0xc000 | (2 << 12) | (ep << 8) | address,
                       1 << 30, 0, td, 1, 0, 0, 0, 0, 0, 0])
        tds = []
        if setup is not None:
            blob(ram+setup_addr, setup)
            tds.append([td+64, 1, (8 << 16) | 0xe80, setup_addr, 0, 0, 0, 0])
        if length:
            tds.append([td+64*len(tds)+64 if setup is not None else 1,
                        td+64*len(tds)+64 if setup is not None else 1,
                        (0x80000000 if setup is not None else 0) | (length << 16) | 0xc80 | (pid << 8), buf, 0, 0, 0, 0])
        if setup is not None:
            tds.append([1, 1, 0x80000c80 | ((0 if pid else 1) << 8), 0, 0, 0, 0, 0])
        for k, item in enumerate(tds):
            words(ram+td+64*k, item)
        if payload is not None:
            blob(ram+buf, payload)
        else:
            write(ram+buf, 0xdeadbeef)
        write(op + 0x18, qh)
        write(op, 0x21)
        poll(ram+td+64*(len(tds)-1)+8, 7, True)
        for k in range(len(tds)):
            if setup is not None:
                residual = length-response_length if length and pid==1 and k==1 and response_length is not None else 0
                read(ram+td+64*k+8,residual<<16,0x7fff00fc)
            else:
                read(ram+td+64*k+8,0,0xfc)  # no Active/Halt/Babble/XactErr
        write(op, 0)

    def control(request_type, request, value, length, data=None, index=0, response_length=None):
        nonlocal address
        transfer(0, 1 if request_type & 0x80 else 0, length,
                 struct.pack('<BBHHH', request_type, request, value, index, length), data, response_length=response_length)
        if request_type == 0 and request == 5:
            address = value

    def ioctl(cmd, data=b'', output=None, notify=False):
        packet = struct.pack('<IIII', cmd, len(data), 0x43210002 if cmd in (26,263) else 0x43210000, 0) + data
        control(0x21, 0, 0, len(packet), packet)
        if notify:
            transfer(1,1,16)
            read(ram+buf,0x9000,0xf000)
            read_bytes(ram+buf+6,b'\0\0')
        control(0xa1, 1, 0, 16 + (len(output) if output else 0))
        read(ram+buf, cmd)
        read(ram+buf+8, 0x43210002 if cmd in (26,263) else 0x43210000)
        read(ram+buf+12, 0)
        if output:
            for i in range(0,len(output),4):
                part=output[i:i+4]
                read(ram+buf+16+i, int.from_bytes(part,'little'), (1 << (8*len(part)))-1)

    # DEVICE_READY is sideband power/awake; initial USB connect waits for
    # HOST_READY, rather than being connected at machine initialization.
    read(0x20e30029c,1,1)
    read(op+0x44,0,1)
    write(0x20e300298,2)
    read(op+0x44,0,1)
    write(0x20e300298,3)
    read(op+0x44,1,1)
    write(op, 2)
    write(op + 0x40, 1)
    port_reset()
    # IOUSBFamily enumerates at a nonzero USB address. The old contract
    # exercised only exact-size descriptor reads at address zero.
    control(0x80,6,0x100,8)
    read(ram+buf,0x02000112)
    control(0,5,7,0)
    control(0x80,6,0x100,18)
    read(ram+buf+8,0x43340a5c)
    read_bytes(ram+buf+14,bytes([1,2,3,1]))
    control(0x80,0,0,2)
    read_bytes(ram+buf,b'\x01\x00')  # externally powered HSIC module
    control(0x82,0,0,2,index=0)
    read_bytes(ram+buf,b'\0\0')
    for index,text in [(0,None),(1,'Broadcom'),(2,'BCM43342 HSIC'),(3,'433420000001')]:
        desc=bytes([4,3,9,4]) if text is None else bytes([2+2*len(text),3])+text.encode('utf-16le')
        control(0x80,6,0x300+index,2,index=0 if not index else 0x409)
        read_bytes(ram+buf,desc[:2])
        control(0x80,6,0x300+index,255,index=0 if not index else 0x409,response_length=len(desc))
        read_bytes(ram+buf,desc)
    # GetFullConfigurationDescriptor b75664 first asks for FOUR bytes;
    # b75798 then asks for exactly wTotalLength. Nine-byte retry is also
    # covered, although it is only used after a failed four-byte probe.
    for length in (4,9,39):
        control(0x80,6,0x200,length)
        read(ram+buf,0x00270209)
    read_bytes(ram+buf+4,bytes([1,1,0,0xe0,0]))
    read_bytes(ram+buf+18,bytes([7,5,0x81,3,64,0,1]))
    read_bytes(ram+buf+25,bytes([7,5,0x82,2,0,2,0]))
    read_bytes(ram+buf+32,bytes([7,5,2,2,0,2,0]))
    control(0x80,8,0,1)
    read_bytes(ram+buf,b'\0')
    control(0,9,1,0)  # b761cc: bmRequestType=0, request9, value1
    control(0x80,8,0,1)
    read_bytes(ram+buf,b'\1')
    control(0x80,0,0,2)
    read_bytes(ram+buf,b'\x01\0')
    control(0x81,0,0,2,index=0)
    read_bytes(ram+buf,b'\0\0')
    for endpoint in (0x81,0x82,2):
        control(0x82,0,0,2,index=endpoint)
        read_bytes(ram+buf,b'\0\0')
    control(0x81,10,0,1,index=0)
    read_bytes(ram+buf,b'\0')
    # idle-disconnect=0: host-ready low after bring-up is not unplug.
    write(0x20e300298,2)
    read(op+0x44,1,1)
    write(0x20e300298,3)
    if enumeration_only:
        control(0xc1,0,1,8)
        read(ram+buf,0)
        read(ram+buf+4,0)
        write(0x800018000+4*len(expected),0xface)
        expected.append(0xface)
        code.append(0x14000000)
        assert len(code)*4 < 0x18000
        return struct.pack('<'+'I'*len(code),*code), expected
    trx = bytearray(64)
    struct.pack_into('<II',trx,0,0x30524448,len(trx))
    struct.pack_into('<I',trx,8,binascii.crc32(trx[12:]) ^ 0xffffffff)
    transfer(2,0,len(trx),payload=bytes(trx))
    control(0xc1,0,1,8)
    read(ram+buf,4)
    read(ram+buf+4,len(trx))
    control(0xc1,2,1,8)
    poll(op+0x44,0,True)
    poll(op+0x44,0,False)
    port_reset()
    address=0
    control(0x80,6,0x100,18)
    read(ram+buf+8,0xbd210a5c)
    control(0,9,1,0)
    ioctl(0,output=struct.pack('<I',0x14e4),notify=True)
    ioctl(2)
    ioctl(263,b'event_msgs\0'+bytes([255])*16)
    ioctl(263,b'escan\0'+struct.pack('<IHH',1,1,0x1234))
    transfer(2,1,2048)
    read(ram+buf+32,67<<24)
    read(ram+buf+36,8<<24)
    first_ssid = target_ssid if target_ssid is not None else b'vphone-wifi'
    first_ie_len = 2 + len(first_ssid) + 19
    read_bytes(ram+buf+84,bytes.fromhex('34120100'))
    read(ram+buf+88,109)
    read_bytes(ram+buf+106, bytes([len(first_ssid)]) + first_ssid)
    read_bytes(ram+buf+164,struct.pack('<h',-45))
    read_bytes(ram+buf+204,struct.pack('<H',first_ie_len))
    read(ram+buf+208,128)
    if join_second:
        transfer(2,1,2048)
        read(ram+buf+32,67<<24)
        read(ram+buf+36,8<<24)
        read_bytes(ram+buf+84,bytes.fromhex('34120100'))
        read(ram+buf+88,109)
        read_bytes(ram+buf+164,struct.pack('<h',-65))
    transfer(2,1,2048)
    read(ram+buf+36,0)
    ssid=target_ssid if target_ssid is not None else (b'second-wifi' if join_second else b'vphone-wifi')
    ioctl(26,struct.pack('<I',len(ssid))+ssid+bytes(32-len(ssid)))
    for event in (0,1,7,16):
        transfer(2,1,2048)
        read(ram+buf+32,event<<24)
    read_bytes(ram+buf+30,b'\0\1')  # WLC_E_LINK flags UP
    if join_second:
        # Secured AP: the supplicant reports WLC_E_PSK_SUP / WLC_SUP_KEYED (6),
        # otherwise Darwin reports an incorrect password.
        transfer(2,1,2048)
        read(ram+buf+32,46<<24)
        read(ram+buf+36,6<<24)
    # Verify GET_BSS_INFO (cmd 51) reports the joined AP
    joined_ie_len = 2 + len(ssid) + 19 + (22 if join_second else 0)
    ioctl(51, output=struct.pack('<III', 12 + 128 + joined_ie_len, 109, 1))
    # A real Ethernet ARP request to slirp's gateway, delivered through BDC.
    mac=bytes.fromhex('024334200002')
    arp = bytes.fromhex('ffffffffffff')+mac+bytes.fromhex('0806')
    arp += struct.pack('!HHBBH',1,0x800,6,4,1)+mac+bytes([10,0,2,15])+bytes(6)+bytes([10,0,2,2])
    transfer(2,0,len(arp)+4,payload=bytes([0x20,0,0,0])+arp)
    transfer(2,1,2048)
    read(ram+buf,0x20)
    read(ram+buf+4,int.from_bytes(mac[:4],'little'))
    read(ram+buf+16,0x01000608)  # EtherType ARP, htype Ethernet
    read(ram+buf+24,0x55520200)  # reply opcode 2, slirp source MAC prefix
    # DHCPDISCOVER: prove slirp's DHCP server sees the guest's Ethernet path.
    bootp=bytearray(240)
    bootp[0:4]=bytes([1,1,6,0]);struct.pack_into('!I',bootp,4,0x43342001)
    struct.pack_into('!H',bootp,10,0x8000);bootp[28:34]=mac
    bootp[236:240]=bytes.fromhex('63825363')
    dhcp=bytes(bootp)+bytes.fromhex('3501013703010306ff')
    udp=struct.pack('!HHHH',68,67,8+len(dhcp),0)+dhcp
    ip=bytearray(struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),1,0,64,17,0,bytes(4),bytes([255]*4)))
    checksum=sum(struct.unpack('!10H',ip));checksum=(checksum&65535)+(checksum>>16)
    struct.pack_into('!H',ip,10,(~checksum)&65535)
    frame=bytes.fromhex('ffffffffffff')+mac+bytes.fromhex('0800')+bytes(ip)+udp
    transfer(2,0,len(frame)+4,payload=bytes([0x20,0,0,0])+frame)
    transfer(2,1,2048)
    read(ram+buf,0x20)
    read(ram+buf+16,0x00450008,0x00ffffff)  # IPv4 EtherType + version/IHL
    read_bytes(ram+buf+38,bytes.fromhex('00430044'))  # DHCP server/client UDP ports
    read_bytes(ram+buf+46,bytes.fromhex('02010600'))  # BOOTREPLY/Ethernet/6-byte MAC
    read_bytes(ram+buf+50,bytes.fromhex('43342001'))  # transaction ID echoed
    read_bytes(ram+buf+62,bytes([10,0,2,15]))  # yiaddr 10.0.2.15
    write(0x800018000+4*len(expected),0xface)
    expected.append(0xface)
    code.append(0x14000000)
    assert len(code)*4 < 0x18000, 'fixture overlaps results'
    return struct.pack('<'+'I'*len(code),*code), expected


if __name__ == '__main__':
    import json
    import tempfile
    options=('-netdev', 'user,id=wifi0', '-machine', 'wifi-netdev=wifi0,wifi-ssid=vphone-wifi,wifi-rom=1')
    run_contract(fixture, 'PASS: HOST_READY connection ordering, high-speed reset, native descriptor/string/status/configuration sequence and DL_GETSTATE', options, result_address=0x800018000)
    run_contract(runtime_fixture, 'PASS: TRX CRC download, runtime re-enumeration, CDC, join, slirp ARP and DHCP offer (fallback single AP)',
                 options, result_address=0x800018000)

    # Multi-AP fixture: scan returns multiple APs, join second SSID, slirp traffic works
    with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
        scan_data = {
            "version": 1,
            "updated": 1700000000,
            "networks": [
                {"ssid": "vphone-wifi", "channel": 6, "rssi": -45, "security": "open", "connected": True},
                {"ssid": "second-wifi", "channel": 1, "rssi": -65, "security": "wpa2", "connected": False}
            ]
        }
        json.dump(scan_data, f)
        f.flush()
        multi_options = ('-netdev', 'user,id=wifi0', '-machine', f'wifi-netdev=wifi0,wifi-ssid=vphone-wifi,wifi-rom=1,wifi-scan-file={f.name}')
        run_contract(lambda: runtime_fixture(join_second=True),
                     'PASS: Multi-AP scan result count, second SSID join, and slirp data path',
                     multi_options, result_address=0x800018000)

    # Invalid scan file fallback: invalid JSON falls back to single AP
    with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
        f.write('{"version": 999, "networks": []}')
        f.flush()
        fallback_options = ('-netdev', 'user,id=wifi0', '-machine', f'wifi-netdev=wifi0,wifi-ssid=vphone-wifi,wifi-rom=1,wifi-scan-file={f.name}')
        run_contract(runtime_fixture,
                     'PASS: Invalid scan file fallback to single AP',
                     fallback_options, result_address=0x800018000)

    # Emoji SSID fixture: surrogate pair in JSON decoded to 4-byte UTF-8, scanned, joined, and slirp traffic works
    with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
        f.write('{"version": 1, "updated": 1700000000, "networks": ['
                '{"ssid": "\\uD83C\\uDF89-wifi", "channel": 6, "rssi": -45, "security": "open", "connected": true}'
                ']}')
        f.flush()
        emoji_options = ('-netdev', 'user,id=wifi0', '-machine', f'wifi-netdev=wifi0,wifi-ssid=vphone-wifi,wifi-rom=1,wifi-scan-file={f.name}')
        emoji_ssid = '🎉-wifi'.encode('utf-8')
        run_contract(lambda: runtime_fixture(target_ssid=emoji_ssid),
                     'PASS: Emoji SSID via JSON surrogate pair, scan decode, join, GET_BSS_INFO, and slirp data path',
                     emoji_options, result_address=0x800018000)
