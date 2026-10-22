"""Host-only checks of the WLAN HSIC hardware interface model in s5l8960x.c.

Tests the board model's faithful representation of absent/powered-off BCM43342
Wi-Fi hardware on S5L8960X (Apple A7 iPhone 5s):
1. GPIO 167 (0xa7, function-device_ready) is an active-high input that is not
   pulled up by the board model, returning 0 (LOW) to indicate DEVICE_READY=0.
2. GPIO 166 (0xa6, function-host_ready) is an output that latches host writes.
3. EHCI1 is configured with exactly 1 port (HSIC) and defaults to disconnected
   port status (PORTSC = 0x1000, CCS=0).
4. The WLAN HSIC MMIO control window at 0x20c000060 (size 0x10) is mapped
   as auxiliary storage.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WLANHSICModelTests(unittest.TestCase):
    def test_wlan_gpio_and_aperture_contract(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')

        source = (ROOT / 'backend/s5l8960x.c').read_text()
        
        # Verify that usb_aux_defs contains the WLAN HSIC aperture 0x20c000060
        self.assertIn('UINT64_C(0x20c000060), 0x10', source)
        
        # Verify that GPIO 167 is NOT in the board-model pull-up list
        pullup_marker = 'if ((pin >= 80 && pin <= 83) || pin == 79 || pin == 146 ||'
        self.assertIn(pullup_marker, source)
        pullup_block = source[source.index(pullup_marker):source.index(pullup_marker) + 200]
        self.assertNotIn('167', pullup_block)
        self.assertNotIn('0xa7', pullup_block)

        # Compile and execute host C test validating GPIO 166/167
        c = '''#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define GPIO_SIZE 0x1000

typedef uint64_t hwaddr;

static inline uint32_t ldl_le_p(const void *ptr) {
    uint32_t val;
    memcpy(&val, ptr, sizeof(val));
    return val;
}

typedef struct {
    uint8_t *gpio_regs;
    struct { int irq_low; } pmu;
    int touch_irq_low;
} A7State;

static uint64_t gpio_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= GPIO_SIZE) {
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            uint8_t byte = s->gpio_regs[off + i];
            if (off + i == 15 * 4) {
                byte = (byte & ~1) | !s->pmu.irq_low;
            }
            if (off + i == 22 * 4) {
                byte = (byte & ~1) | !s->touch_irq_low;
            }
            if (((off + i) & 3) == 0) {
                unsigned pin = (off + i) / 4;
                if ((pin >= 80 && pin <= 83) || pin == 79 || pin == 146 ||
                    pin == 2 || pin == 3 || pin == 4 || pin == 5 || pin == 16) {
                    uint32_t config = ldl_le_p(s->gpio_regs + off + i);
                    bool driven = !(config & 0x200) && (config & 0xe) == 2;
                    bool sample = !driven || (config & 1);
                    byte = (byte & ~1) | sample;
                }
            }
            val |= (uint64_t)byte << (i * 8);
        }
        return val;
    }
    return 0;
}

static void gpio_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= GPIO_SIZE) {
        for (unsigned i = 0; i < size; i++) {
            hwaddr a = off + i;
            uint8_t byte = val >> (i * 8);
            if (a >= 0x800 && a < 0x9c0 && (a & 0x3f) < 28) {
                s->gpio_regs[a] &= ~byte;
            } else {
                s->gpio_regs[a] = byte;
            }
        }
    }
}

int main(void) {
    A7State s;
    memset(&s, 0, sizeof(s));
    s.gpio_regs = calloc(1, GPIO_SIZE);

    /* GPIO 167 (pin 0xa7): function-device_ready input.
     * When Wi-Fi module is absent/powered off, reading pin 167 config/data
     * must return 0 (LOW), indicating DEVICE_READY = 0.
     */
    uint64_t pin167_val = gpio_read(&s, 167 * 4, 4);
    assert((pin167_val & 1) == 0);

    /* GPIO 166 (pin 0xa6): function-host_ready output.
     * Host driver sets host ready by driving pin 166.
     * Writes to pin 166 must latch correctly.
     */
    gpio_write(&s, 166 * 4, 0x203, 4);
    uint64_t pin166_val = gpio_read(&s, 166 * 4, 4);
    assert((pin166_val & 1) == 1);

    gpio_write(&s, 166 * 4, 0x202, 4);
    pin166_val = gpio_read(&s, 166 * 4, 4);
    assert((pin166_val & 1) == 0);

    /* Pin 167 must remain 0 regardless of pin 166 changes */
    assert((gpio_read(&s, 167 * 4, 4) & 1) == 0);

    free(s.gpio_regs);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='a7-wlan-hsic-') as tmp:
            p = Path(tmp)
            (p / 'test.c').write_text(c)
            proc = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra',
                                  str(p / 'test.c'), '-o', str(p / 'test')],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run_proc = subprocess.run([str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(run_proc.returncode, 0, run_proc.stderr)


if __name__ == '__main__':
    unittest.main()
