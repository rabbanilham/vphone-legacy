#!/usr/bin/env python3
"""Real EHCI1 DMA/USB contract with a bare-metal AArch64 fixture, never iOS.

Reads both descriptors and ROM DL_GETSTATE via actual setup/data/status qTDs.
"""
import struct
from ehci_contract_test import main as run_contract
from gpio_contract_test import move64


def fixture():
    code = move64(4, 0x800002000)
    expected = []
    op = 0x20c400010

    def write(addr, value):
        code.extend(move64(2, addr) + move64(0, value) + [0xb9000040])

    def read(addr, value, mask=None):
        code.extend(move64(2, addr) + [0xb9400043])
        if mask is not None:
            code.extend(move64(5, mask) + [0x0a050063])
        code.append(0xb9000003 | len(expected) << 10 | 4 << 5)
        expected.append(value)

    # GPIO166 host-ready, GPIO167 external device-ready; input ignores latch.
    write(0x20e300298, 3)
    read(0x20e30029c, 1, 1)
    write(op, 2)
    write(op + 0x40, 1)
    read(op + 0x44, 1, 1)
    write(op + 0x44, 0x1100)
    write(op + 0x44, 0x1000)
    read(op + 0x44, 5, 5)

    def control(setup, length):
        write(op, 0)
        # Circular async QH, address zero, EP0, high speed, DTC, head, MPS64.
        qh = [0x11002, (64 << 16) | (1 << 15) | (1 << 14) | (2 << 12),
              1 << 30, 0, 0x12000, 1, 0, 0, 0, 0, 0, 0]
        for i, word in enumerate(qh):
            write(0x800011000 + i * 4, word)
        tds = [[0x12040, 1, (8 << 16) | (3 << 10) | (2 << 8) | 0x80, 0x13000, 0, 0, 0, 0],
               [0x12080, 1, (1 << 31) | (length << 16) | (3 << 10) | (1 << 8) | 0x80, 0x14000, 0, 0, 0, 0],
               [1, 1, (1 << 31) | (3 << 10) | 0x80, 0, 0, 0, 0, 0]]
        for k, td in enumerate(tds):
            for i, word in enumerate(td):
                write(0x800012000 + k * 64 + i * 4, word)
        for i, word in enumerate(struct.unpack('<II', setup)):
            write(0x800013000 + 4 * i, word)
        for i in range(0, length, 4):
            write(0x800014000 + i, 0xdeadbeef)
        write(op + 0x18, 0x11000)
        write(op, 0x21)
        # Poll final status qTD Active; harness supplies an external deadline.
        code.extend(move64(2, 0x800012088))
        code.extend([0xb9400043, 0x373fffe3])  # ldr w3,[x2]; tbnz w3,#7,-4
        for k in range(3):
            read(0x800012008 + k * 64, 0, 0x7fff00fc)
        write(op, 0)

    control(struct.pack('<BBHHH', 0x80, 6, 0x100, 0, 18), 18)
    read(0x800014000, 0x02000112)
    read(0x800014008, 0x43340a5c)
    read(0x800014010, 0x00000103, 0xffff)
    control(struct.pack('<BBHHH', 0x80, 6, 0x200, 0, 39), 39)
    read(0x800014000, 0x00270209)
    read(0x800014014, 0x00400381)
    read(0x800014018, 0x82050701)
    control(struct.pack('<BBHHH', 0xc1, 0, 1, 0, 8), 8)
    read(0x800014000, 0)
    read(0x800014004, 0)
    write(0x800002000 + 4 * len(expected), 0xface)
    expected.append(0xface)
    code.append(0x14000000)
    assert len(code) * 4 < 0x2000, 'fixture overlaps results'
    return struct.pack('<' + 'I' * len(code), *code), expected


def runtime_fixture():
    """TRX -> runtime PID -> CDC -> association -> slirp ARP/DHCP replies."""
    import binascii
    code = move64(4, 0x800010000)
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

    def transfer(ep, pid, length, setup=None, payload=None):
        write(op, 0)
        words(ram+qh, [qh | 2, ((512 if ep else 64) << 16) | 0xc000 | (2 << 12) | (ep << 8),
                       1 << 30, 0, td, 1, 0, 0, 0, 0, 0, 0])
        tds = []
        if setup is not None:
            blob(ram+setup_addr, setup)
            tds.append([td+64, 1, (8 << 16) | 0xe80, setup_addr, 0, 0, 0, 0])
        if length:
            tds.append([td+64*len(tds)+64 if setup is not None else 1, 1,
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
            read(ram+td+64*k+8, 0, 0xfc)  # no Active/Halt/Babble/XactErr
        write(op, 0)

    def control(request_type, request, value, length, data=None):
        transfer(0, 1 if request_type & 0x80 else 0, length,
                 struct.pack('<BBHHH', request_type, request, value, 0, length), data)

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

    write(0x20e300298, 3)
    write(op, 2)
    write(op + 0x40, 1)
    port_reset()
    control(0, 9, 1, 0)  # configuration 1 opens ROM bulk endpoint
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
    read_bytes(ram+buf+84,bytes.fromhex('34120100'))
    read(ram+buf+88,109)
    read_bytes(ram+buf+164,struct.pack('<h',-45))
    read_bytes(ram+buf+204,struct.pack('<H',32))
    read(ram+buf+208,128)
    transfer(2,1,2048)
    read(ram+buf+36,0)
    ssid=b'vphone-wifi'
    ioctl(26,struct.pack('<I',len(ssid))+ssid+bytes(32-len(ssid)))
    for event in (0,1,7,16):
        transfer(2,1,2048)
        read(ram+buf+32,event<<24)
    read_bytes(ram+buf+30,b'\0\1')  # final WLC_E_LINK flags UP
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
    write(0x800010000+4*len(expected),0xface)
    expected.append(0xface)
    code.append(0x14000000)
    assert len(code)*4 < 0x10000, 'fixture overlaps results'
    return struct.pack('<'+'I'*len(code),*code), expected


if __name__ == '__main__':
    options=('-netdev', 'user,id=wifi0', '-machine', 'wifi-netdev=wifi0,wifi-ssid=vphone-wifi')
    run_contract(fixture, 'PASS: HSIC GPIO, EHCI1 high-speed reset, USB descriptors and DL_GETSTATE', options)
    run_contract(runtime_fixture, 'PASS: TRX CRC download, runtime re-enumeration, CDC, join, slirp ARP and DHCP offer',
                 options, result_address=0x800010000)
