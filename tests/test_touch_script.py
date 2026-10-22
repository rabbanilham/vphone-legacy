"""Compile the actual gesture parser; check timelines without QEMU/iOS."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class TouchScriptTests(unittest.TestCase):
    def test_parser_and_timeline(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')
        source = SOURCE.read_text()
        parser = source[source.index('/* Bounded, host-testable script compiler.'):
                        source.index('/* Host-testable mouse contact sequencer.')]
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#define MAX(a,b) ((a) > (b) ? (a) : (b))
'''+parser+r'''
static TouchScript script;
static char error[256];
static void parse(const char *s) {
    assert(touch_script_parse(s, &script, error, sizeof(error)));
    assert(script.count);
    for (unsigned i=1;i<script.count;i++) assert(script.frames[i].ns>script.frames[i-1].ns);
}
static void invalid(const char *s) {
    assert(!touch_script_parse(s, &script, error, sizeof(error)));
    assert(script.count==0 && strstr(error,"gesture") && strstr(error,"command"));
}
int main(void) {
    parse("125,60,1018,600,1018,0.3");
    assert(script.count==21 && script.frames[0].ns==125000000000LL);
    assert(script.frames[0].state==1 && script.frames[1].state==3);
    for (unsigned i=2;i<20;i++) {
        assert(script.frames[i].state==4 && script.frames[i].x==60+30*(int)(i-1));
    }
    assert(script.frames[20].state==7 && script.frames[20].x==600);
    for (unsigned i=0;i<21;i++)
        assert(script.frames[i].ns==125000000000LL+i*(300000000LL/18));
    parse("at=125;down 100,1018;hold 0.15;move 560,1018 dur=0.8 steps=48;hold 0.1;up");
    assert(script.count==66);
    assert(script.frames[10].x==100 && script.frames[10].state==4);
    assert(script.frames[10].ns==125000000000LL+TOUCH_TICK_NS+150000000);
    assert(script.frames[11].x==109 && script.frames[58].x==560);
    assert(script.frames[58].ns==125000000000LL+TOUCH_TICK_NS+950000000);
    assert(script.frames[65].state==7 && script.frames[65].y==1018);
    parse(" at=0;tap 0,1136 | at=2;down 640,0;move 0,1136 dur=1;up ");
    assert(script.count==72 && script.frames[9].ns==2000000000LL);
    assert(script.frames[8].state==7 && script.frames[9].state==1);
    assert(script.frames[70].x==0 && script.frames[70].y==1136);
    parse("at=0;down 1,2;up");
    assert(script.count==3 && script.frames[2].ns==2*TOUCH_TICK_NS);
    parse("at=0;down 1,2;hold 0.001;move 2,3 dur=0.001 steps=1;up");
    assert(script.count==5 && script.frames[3].ns==TOUCH_TICK_NS+2000000);
    const char *bad[]={"", "nan,1,2,3,4,.3", "0,1,2,3,4,.3junk", "0,1,2,3,4,0",
      "at=nan;tap 1,2", "at=-1;tap 1,2", "at=86401;tap 1,2", "at=0;",
      "at=0;down 641,0;up", "at=0;tap 1.5,0", "at=0;tap 0,-1", "at=0;tap 0,1137",
      "at=0;down 0,0", "at=0;up", "at=0;move 1,2 dur=1;up", "at=0;hold 1;up",
      "at=0;down 0,0;down 1,1;up", "at=0;tap 1,2;up", "at=0;down 0,0;move 1,2;up",
      "at=0;down 0,0;move 1,2 dur=inf;up", "at=0;down 0,0;hold 61;up",
      "at=0;down 0,0;move 1,2 dur=1 steps=0;up", "at=0;down 0,0;move 1,2 dur=1 steps=1.5;up",
      "at=0;down 0,0;move 1,2 dur=1 steps=3601;up", "at=0;tap 1,2;", "at=0;tap 1,2|",
      "at=0;tap 1,2|at=0.1;tap 1,2", "at=0;tap 1,2|tap 1,2", "at=0;tap 1,2 trailing"};
    for (unsigned i=0;i<sizeof(bad)/sizeof(*bad);i++) invalid(bad[i]);
    /* Every truncated prefix is a separate allocation: sanitizers catch
     * missing-separator reads beyond NUL as well as invalid conversions. */
    const char *full="at=0;down 1,2;move 3,4 dur=1 steps=60;hold .1;up|at=5;tap 1,2";
    for (size_t i=0;i<strlen(full);i++) {
        char *p=malloc(i+1); memcpy(p,full,i);p[i]=0;
        touch_script_parse(p,&script,error,sizeof(error));free(p);
    }
    char big[8192]="at=0;down 0,0";
    for (unsigned i=0;i<6;i++) strcat(big,";move 1,1 dur=60 steps=3600");
    strcat(big,";up"); invalid(big);assert(strstr(error,"16384"));
    parse("at=0;tap 1,2"); /* clean recovery after partial parse/capacity failure */
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='touch-script-host-') as directory:
            root = Path(directory)
            (root / 'test.c').write_text(harness)
            build = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', str(root / 'test.c'),
                                    '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
