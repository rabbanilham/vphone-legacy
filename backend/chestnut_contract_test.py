#!/usr/bin/env python3
"""Native guest checks of the restricted Chestnut power-control/I2C ABI."""
import struct
from gpio_contract_test import move64
import i2c_contract_test as harness


def fixture():
    out = 0x800002000; bus = 0x20a110000; aic = 0x20e100000
    code = move64(4,out); expected=[]

    def write(addr,val):
        code.extend(move64(2,addr)+move64(0,val)+[0xb9000040])

    def read(addr,val):
        code.extend(move64(2,addr)+[0xb9400043,0xb9000003 | len(expected)<<10 | 4<<5])
        expected.append(val)

    def clear(): write(bus+0x14,0xffffffff)

    def prefix():
        clear();write(bus,0x14e);read(bus+0x14,0)
        write(bus,0x205);read(bus+0x14,0x08000000)

    def receive(value):
        prefix();clear();write(bus,0x14f);write(bus,0x601)
        read(bus+0x14,0x00100000);read(bus+8,0x100)
        read(bus+4,value);read(bus+8,0);read(bus+4,0x100)

    receive(0)  # Explicit board initial state: virtual rails disabled.
    clear();write(bus,0x14e);write(bus,5);write(bus,0x20f)
    read(bus+0x14,0x08000000);receive(0x0f)
    # Controller reset flushes transfers, not the external chip's power state.
    write(bus+0x10,0x80000000);read(bus+4,0x100);receive(0x0f)
    # Read completion asserts the native event mask and AIC IRQ154.
    write(bus+0x18,0x00b00040);read(aic+0x4210,0x04000000)
    clear();read(aic+0x4210,0)
    # Unsupported registers and lengths fail, without inventing RX data.
    write(bus,0x14e);write(bus,0x206);read(bus+0x14,0x08200000);read(bus+4,0x100)
    prefix();clear();write(bus,0x14f);write(bus,0x602)
    read(bus+0x14,0x08200000);read(bus+4,0x100)
    # Native setLDO values (probe-151): 0x17 rails on, 0x10 rails off.
    clear();write(bus,0x14e);write(bus,5);write(bus,0x217)
    read(bus+0x14,0x08000000);receive(0x17)
    clear();write(bus,0x14e);write(bus,5);write(bus,0x210)
    read(bus+0x14,0x08000000);receive(0x10)
    clear();write(bus,0x14e);write(bus,5);write(bus,0x280)
    read(bus+0x14,0x08200000);receive(0x10)  # Unsupported control bits do not mutate state.
    clear();write(bus,0x14c);read(bus+0x14,0x08200000)
    # Identical address on I2C1 remains unmatched.
    write(bus+0x1014,0xffffffff);write(bus+0x1000,0x14e)
    read(bus+0x1014,0x08200000);read(bus+0x1004,0x100)
    write(out+len(expected)*4,0xface);expected.append(0xface);code.append(0x14000000)
    return struct.pack('<'+'I'*len(code),*code),expected


if __name__=='__main__':
    harness.fixture=fixture
    harness.main()
    print('PASS: Chestnut register 0x05 state/readback; RX/STOP events; IRQ; controller reset; unsupported registers, lengths, addresses and I2C1')
