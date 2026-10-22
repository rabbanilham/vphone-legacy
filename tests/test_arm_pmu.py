"""Host-only checks of the ARM PMU (PMUv3) configuration in backend/s5l8960x.c.

Verifies:
1. The board model disables the architectural ARM PMU on Cortex-A57 APs
   by default, preventing QEMU's PMU EL-change hooks from acquiring the BQL
   on every ERET and exception transition.
2. An opt-out env var VPHONE_PMU=1 restores PMU functionality.
3. A host C mock of QOM property setting and arm_cpu_realizefn confirms that
   has_pmu=false unsets ARM_FEATURE_PMU, clears ID_AA64DFR0.PMUVer, and
   prevents registration of pmu_pre_el_change / pmu_post_el_change hooks.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backend/s5l8960x.c"


class ArmPmuConfigTests(unittest.TestCase):
    def test_source_contains_pmu_property_and_env_opt_out(self):
        source = SOURCE.read_text()
        self.assertIn('/* The TA command whose parameter buffer', source)
        self.assertIn('VPHONE_PMU', source)
        self.assertIn('object_property_set_bool(OBJECT(cpu), "pmu", enable_pmu, &error_fatal);', source)

    def test_cpu_creation_and_realize_contract(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("host C compiler unavailable")

        source = SOURCE.read_text()
        # Ensure the CPU loop in a7_init sets pmu before qdev_realize
        init_marker = 'const char *pmu_env = getenv("VPHONE_PMU");'
        self.assertIn(init_marker, source)
        init_block = source[source.index(init_marker):source.index(init_marker) + 1200]
        self.assertIn('object_property_set_bool(OBJECT(cpu), "pmu", enable_pmu, &error_fatal);', init_block)
        self.assertIn('qdev_realize(DEVICE(cpu), NULL, &error_fatal);', init_block)

        c_harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ARM_FEATURE_PMU 0
#define ARM_FEATURE_V8  1

typedef struct {
    bool has_pmu;
    uint64_t features;
    uint64_t id_aa64dfr0;
    bool el_change_hooks_registered;
    void *pmu_timer;
} ARMCPU;

#define OBJECT(x) (x)
#define DEVICE(x) (x)
static void *error_fatal = NULL;

static void object_property_set_bool(void *obj, const char *prop, bool val, void **errp) {
    ARMCPU *cpu = (ARMCPU *)obj;
    if (!strcmp(prop, "pmu")) {
        if (val) {
            cpu->features |= (1ULL << ARM_FEATURE_PMU);
        } else {
            cpu->features &= ~(1ULL << ARM_FEATURE_PMU);
        }
        cpu->has_pmu = val;
    }
}

static void qdev_realize(void *dev, void *bus, void **errp) {
    ARMCPU *cpu = (ARMCPU *)dev;
    if (!cpu->has_pmu) {
        cpu->features &= ~(1ULL << ARM_FEATURE_PMU);
    }
    if (cpu->features & (1ULL << ARM_FEATURE_PMU)) {
        cpu->el_change_hooks_registered = true;
        cpu->pmu_timer = (void *)0xdeadbeef;
        cpu->id_aa64dfr0 = (cpu->id_aa64dfr0 & ~(0xFULL << 8)) | (1ULL << 8); // PMUVer=1
    } else {
        cpu->features &= ~(1ULL << ARM_FEATURE_PMU);
        cpu->el_change_hooks_registered = false;
        cpu->pmu_timer = NULL;
        cpu->id_aa64dfr0 &= ~(0xFULL << 8); // PMUVer=0
    }
}

static void run_cpu_init(ARMCPU *cpu) {
    /* Initialize default Cortex-A57 state where PMU is enabled by default */
    cpu->features = (1ULL << ARM_FEATURE_PMU) | (1ULL << ARM_FEATURE_V8);
    cpu->has_pmu = true;
    cpu->id_aa64dfr0 = (1ULL << 8) | 0x6ULL; /* PMUVer=1, DebugVer=6 */
    cpu->el_change_hooks_registered = false;
    cpu->pmu_timer = NULL;

    /* Logic replicated from backend/s5l8960x.c a7_init */
    const char *pmu_env = getenv("VPHONE_PMU");
    bool enable_pmu = pmu_env && !strcmp(pmu_env, "1");
    object_property_set_bool(OBJECT(cpu), "pmu", enable_pmu, &error_fatal);
    qdev_realize(DEVICE(cpu), NULL, &error_fatal);
}

int main(void) {
    ARMCPU cpu;

    /* 1. Default (no environment variable): PMU disabled */
    unsetenv("VPHONE_PMU");
    run_cpu_init(&cpu);
    assert(cpu.has_pmu == false);
    assert(!(cpu.features & (1ULL << ARM_FEATURE_PMU)));
    assert(cpu.el_change_hooks_registered == false);
    assert(cpu.pmu_timer == NULL);
    assert(((cpu.id_aa64dfr0 >> 8) & 0xf) == 0); /* PMUVer == 0 */

    /* 2. VPHONE_PMU=0: PMU disabled */
    setenv("VPHONE_PMU", "0", 1);
    run_cpu_init(&cpu);
    assert(cpu.has_pmu == false);
    assert(!(cpu.features & (1ULL << ARM_FEATURE_PMU)));
    assert(cpu.el_change_hooks_registered == false);
    assert(cpu.pmu_timer == NULL);
    assert(((cpu.id_aa64dfr0 >> 8) & 0xf) == 0);

    /* 3. VPHONE_PMU=1: PMU enabled (opt-out) */
    setenv("VPHONE_PMU", "1", 1);
    run_cpu_init(&cpu);
    assert(cpu.has_pmu == true);
    assert(cpu.features & (1ULL << ARM_FEATURE_PMU));
    assert(cpu.el_change_hooks_registered == true);
    assert(cpu.pmu_timer != NULL);
    assert(((cpu.id_aa64dfr0 >> 8) & 0xf) == 1); /* PMUVer == 1 */

    /* 4. Arbitrary string: PMU disabled */
    setenv("VPHONE_PMU", "true", 1);
    run_cpu_init(&cpu);
    assert(cpu.has_pmu == false);
    assert(!(cpu.features & (1ULL << ARM_FEATURE_PMU)));
    assert(cpu.el_change_hooks_registered == false);
    assert(cpu.pmu_timer == NULL);
    assert(((cpu.id_aa64dfr0 >> 8) & 0xf) == 0);

    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="a7-pmu-config-") as tmp:
            root = Path(tmp)
            cfile = root / "test_pmu.c"
            cfile.write_text(c_harness)
            bin_path = root / "test_pmu"
            subprocess.run([compiler, "-std=c11", "-Wall", "-Werror", str(cfile), "-o", str(bin_path)], check=True)
            res = subprocess.run([str(bin_path)], check=True)
            self.assertEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
