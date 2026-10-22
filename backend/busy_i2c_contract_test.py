#!/usr/bin/env python3
"""Host-only contracts for the native baseband/accessory/backlight providers.

Compiles actual board C with the existing PMU/I2C harness. No QEMU/iOS run.
"""
import os
from pmu_contract_test import host_check

# The TriStar model is opt-in on the board (VPHONE_TRISTAR=1).
os.environ.setdefault('VPHONE_TRISTAR', '1')

BASEBAND = r'''
 /* TriStar REV and baseband USB/UART mux control, repeated START and STOP. */
 i2c_write(&p,0x14,~0u);send(&p,0x134);send(&p,0x20f);
 send(&p,0x135);send(&p,0x601);
 assert(ldl_le_p(p.regs+0x14)==0x08100000);
 assert(i2c_read(&p,8)==256 && i2c_read(&p,4)==0x20);
 assert(i2c_read(&p,4)==0x100);
 send(&p,0x134);send(&p,1);send(&p,0x260);
 send(&p,0x134);send(&p,0x201);send(&p,0x135);send(&p,0x601);
 assert(i2c_read(&p,4)==0x60);
 send(&p,0x134);send(&p,2);send(&p,0x218);
 /* A controller reset preserves external mux state but clears FIFO/prefix. */
 i2c_write(&p,0x10,0x80000000);
 assert(!p.tristar.pointer_valid && !p.pmu_rx_count);
 send(&p,0x134);send(&p,0x202);send(&p,0x135);send(&p,0x601);
 assert(i2c_read(&p,4)==0x18);
 /* PMU baseband GPIO storage remains writable and separate. */
 store(&p,0x200,0x55);read_regs(&p,0x200,1);assert(i2c_read(&p,4)==0x55);
 assert(p.tristar.regs[1]==0x60 && p.tristar.regs[2]==0x18);
 p.base+=0x1000;i2c_write(&p,0x14,~0u);send(&p,0x134);
 assert(ldl_le_p(p.regs+0x14)==0x08200000);p.base-=0x1000;
'''

IAP = r'''
 /* Empty connector: no identity, no cable, no interrupt events. */
 send(&p,0x134);send(&p,0x20c);send(&p,0x135);send(&p,0x603);
 assert(i2c_read(&p,4)==4);assert(i2c_read(&p,4)==0);
 assert(i2c_read(&p,4)==0);assert(i2c_read(&p,4)==0x100);
 /* Native system ID read needs a 0x74 marker; no accessory identity. */
 send(&p,0x134);send(&p,0x220);send(&p,0x135);send(&p,0x603);
 assert(i2c_read(&p,4)==0x74 && i2c_read(&p,4)==0 && i2c_read(&p,4)==0);
 /* Native event mask write and readback; status/revision are read-only. */
 send(&p,0x134);send(&p,0xe);send(&p,0x2ff);
 send(&p,0x134);send(&p,0x20e);send(&p,0x135);send(&p,0x601);
 assert(i2c_read(&p,4)==0xff);
 send(&p,0x134);send(&p,0xc);send(&p,0x2ff);
 send(&p,0x134);send(&p,0x20c);send(&p,0x135);send(&p,0x602);
 assert(i2c_read(&p,4)==4 && i2c_read(&p,4)==0);
 /* Unsupported range / burst crossing / missing prefix must NACK. */
 i2c_write(&p,0x14,~0u);send(&p,0x134);send(&p,0x240);
 assert(ldl_le_p(p.regs+0x14)==0x08200000);
 i2c_write(&p,0x14,~0u);send(&p,0x134);send(&p,0x20f);
 send(&p,0x135);send(&p,0x602);
 assert(ldl_le_p(p.regs+0x14)&0x00200000);assert(!p.pmu_rx_count);
 i2c_write(&p,0x10,0x80000000);send(&p,0x135);send(&p,0x601);
 assert(ldl_le_p(p.regs+0x14)==0x08200000);
'''

BACKLIGHT = r'''
 /* Native LM3534 start reads 4 bytes at 0, enable writes 4 bytes back. */
 i2c_write(&p,0x14,~0u);send(&p,0x1c6);send(&p,0x200);
 send(&p,0x1c7);send(&p,0x604);
 assert(ldl_le_p(p.regs+0x14)==0x08100000);
 for(unsigned i=0;i<4;i++) assert(i2c_read(&p,4)==0);
 send(&p,0x1c6);send(&p,0);send(&p,1);send(&p,0x12);send(&p,0x34);send(&p,0x256);
 send(&p,0x1c6);send(&p,0x200);send(&p,0x1c7);send(&p,0x604);
 assert(i2c_read(&p,4)==1 && i2c_read(&p,4)==0x12);
 assert(i2c_read(&p,4)==0x34 && i2c_read(&p,4)==0x56);
 i2c_write(&p,0x10,0x80000000);
 assert(p.lm3534.regs[0]==1 && p.lm3534.regs[3]==0x56);
 /* Disable preserves the other settings, separate from TriStar and PMU. */
 send(&p,0x1c6);send(&p,0);send(&p,0x200);
 assert(p.lm3534.regs[0]==0 && p.lm3534.regs[1]==0x12);
 assert(!p.tristar.regs[1]);
 i2c_write(&p,0x14,~0u);send(&p,0x1c6);send(&p,0x203);
 send(&p,0x1c7);send(&p,0x602);
 assert(ldl_le_p(p.regs+0x14)&0x00200000);assert(!p.pmu_rx_count);
 p.base+=0x1000;i2c_write(&p,0x14,~0u);send(&p,0x1c6);
 assert(ldl_le_p(p.regs+0x14)==0x08200000);p.base-=0x1000;
'''

if __name__ == '__main__':
    for checks in (BASEBAND, IAP, BACKLIGHT):
        host_check(checks)
    print('PASS: baseband mux, unplugged accessory, LM3534 native register contracts')
