#!/usr/bin/env python3
"""Host-only contracts for the native baseband/accessory/backlight providers.

Compiles actual board C with the existing PMU/I2C harness. No QEMU/iOS run.
"""
import json
import os
from pathlib import Path
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

def tristar_startup_checks(*, observed_prefix=False):
    """Replay every TriStar transfer in probe139, resetting as its controller does.

    The captured 0x5d prefix is inconsistent with the native forwarding ABI.
    Default replays the recovered 0x20 request; observed_prefix replays the
    capture unchanged against the retained compatibility alias. Neither
    host replay establishes where the native guest buffer receives bytes.
    """
    trace = Path(__file__).resolve().parents[1] / 'tests/fixtures/probe139_tristar.json'
    checks = []
    for row in json.loads(trace.read_text()):
        tx = row['tx'][:]
        bad_prefix = tx[1] == 0x25d
        rx = row['rx']
        if bad_prefix and not observed_prefix:
            tx[1] = 0x220
        elif bad_prefix:
            # Runtime variant e9daa8..b00 reads 0x5d: ID low, ID high, then
            # the 0x74 marker (probe139 captured the old marker-first reply).
            rx = [0, 0, 0x74]
        checks.append('i2c_write(&p,0x10,0x80000000);')
        checks.append('i2c_write(&p,0x14,~0u);')
        checks.extend(f'send(&p,{word:#x});' for word in tx)
        checks.append('assert(!(ldl_le_p(p.regs+0x14)&0x00200000));')
        checks.append(f'assert(i2c_read(&p,8)=={len(rx) << 8});')
        for i, byte in enumerate(rx):
            checks.append(f'assert(i2c_read(&p,8)=={(len(rx)-i) << 8});')
            checks.append(f'assert(i2c_read(&p,4)=={byte});')
        checks.append('assert(i2c_read(&p,4)==0x100);')
    checks.append('assert(p.tristar.regs[0xe]==1);')
    # Receive into an offset, as getSystemID uses sp+0xd; no framing byte
    # is consumed by either the nub or the controller before byte zero.
    checks.append(r'''
      uint8_t frame[16]; memset(frame,0xa5,sizeof(frame));
      send(&p,0x134);send(&p,0x220);send(&p,0x135);send(&p,0x603);
      for(unsigned i=0;i<3;i++) frame[13+i]=i2c_read(&p,4);
      assert(frame[12]==0xa5 && frame[13]==0x74);
      assert(((frame[14]<<2)|frame[15])==0);
    ''')
    return '\n'.join(checks)


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
    for checks in (BASEBAND, IAP, BACKLIGHT, tristar_startup_checks(),
                   tristar_startup_checks(observed_prefix=True)):
        host_check(checks)
    print('PASS: baseband mux, unplugged accessory, LM3534 native register contracts')
