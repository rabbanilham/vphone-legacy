"""Compile the board's mouse sequencer, QEMU callbacks and report builder."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class MouseTouchTests(unittest.TestCase):
    def test_mouse_contact_pipeline(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')
        source = SOURCE.read_text()

        def extract(start, end):
            return source[source.index(start):source.index(end)]

        harness = r'''
#include <assert.h>
#include <stdio.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define TOUCH_TICK_NS INT64_C(16666667)
#define QEMU_CLOCK_VIRTUAL 0
#define INPUT_EVENT_ABS_MAX 32767
#define INPUT_EVENT_KIND_ABS 0
#define INPUT_EVENT_KIND_BTN 1
#define INPUT_EVENT_KIND_MTT 2
#define INPUT_AXIS_X 0
#define INPUT_AXIS_Y 1
#define INPUT_BUTTON_LEFT 0
#define INPUT_EVENT_MASK_ABS 1
#define INPUT_EVENT_MASK_BTN 2
#define INPUT_EVENT_MASK_MTT 4
enum { INPUT_MULTI_TOUCH_TYPE_BEGIN, INPUT_MULTI_TOUCH_TYPE_UPDATE,
       INPUT_MULTI_TOUCH_TYPE_END, INPUT_MULTI_TOUCH_TYPE_CANCEL,
       INPUT_MULTI_TOUCH_TYPE_DATA };
#define LOG_UNIMP 0
#define qemu_log_mask(mask, ...) do {} while (0)
static int64_t now, deadline;
static int64_t qemu_clock_get_ns(int clock) { (void)clock; return now; }
static void timer_mod(void *timer, int64_t ns) { (void)timer; deadline=ns; }
static void timer_del(void *timer) { (void)timer; deadline=-1; }
typedef struct TouchScriptFrame { int64_t ns; int x,y; unsigned state; } TouchScriptFrame;
'''
        harness += extract('/* Host-testable mouse contact sequencer.', 'typedef struct A7Bridge')
        harness += r'''
typedef struct A7Touch {
    uint8_t sequence, reporting, input_control[4];
    uint8_t frames[64][TOUCH_FRAME_MAX], frame_len[64];
    unsigned frame_count;
    uint32_t last_timestamp_us;
} A7Touch;
typedef void QemuConsole;
typedef void DeviceState;
typedef struct { int dummy; } SysBusDevice;
typedef struct A7State {
    MouseTouch mouse_touch;
    MultiTouch multi_touch;
    void *mouse_touch_timer;
    QemuConsole *con;
    bool touch_force, mouse_touch_enabled;
    unsigned touch_step;
    struct { unsigned count; } touch_script;
    A7Touch touch;
} A7State;
#define OBJECT_DECLARE_SIMPLE_TYPE(T, C) typedef struct T T; \
    static T *C(void *p) { return p; }
typedef struct QemuInputEvent {
    int type;
    struct { int axis, value; } abs;
    struct { int button; bool down; } btn;
    struct { int type, slot, tracking_id, axis, value; } mtt;
} QemuInputEvent;
typedef struct QemuInputHandler {
    const char *name; unsigned mask;
    void (*event)(DeviceState *, QemuConsole *, QemuInputEvent *);
    void (*sync)(DeviceState *);
} QemuInputHandler;
static void stw_le_p(uint8_t *p, unsigned n) { p[0]=n; p[1]=n>>8; }
static void stl_le_p(uint8_t *p, unsigned n) {
    stw_le_p(p,n); stw_le_p(p+2,n>>16);
}
static unsigned lduw_le_p(const uint8_t *p) { return p[0] | p[1]<<8; }
static void touch_enqueue(A7Touch *t, const uint8_t *f, unsigned len) {
    assert(t->frame_count<64 && (len==56 || len==86));
    t->frame_len[t->frame_count]=len;
    memcpy(t->frames[t->frame_count++],f,len);
}
static A7Touch *touch_device(A7State *s) { return &s->touch; }
'''
        harness += extract('static void touch_contacts(', 'static A7Touch *touch_device(')
        harness += extract('static bool touch_reporting_enabled(', 'static void touch_boot_complete(')
        harness += extract('/* Board-owned display device', 'static uint32_t touch_word(')
        harness += r'''
static A7State s;
static A7TouchDisplay display;
static void event(int type, int axis, int value) {
    QemuInputEvent e={.type=type};
    e.abs.axis=axis; e.abs.value=value;
    e.btn.button=axis; e.btn.down=value;
    mouse_touch_handler.event(&display,s.con,&e);
}
static void packet(int x, int y, bool down) {
    /* Cocoa sends BTN before ABS; only sync may start a contact. */
    event(INPUT_EVENT_KIND_BTN,INPUT_BUTTON_LEFT,down);
    event(INPUT_EVENT_KIND_ABS,INPUT_AXIS_X,x);
    event(INPUT_EVENT_KIND_ABS,INPUT_AXIS_Y,y);
    mouse_touch_handler.sync(&display);
}
static void mtt(int type, int slot, int axis, int value) {
    QemuInputEvent e={.type=INPUT_EVENT_KIND_MTT};
    e.mtt.type=type; e.mtt.slot=slot; e.mtt.axis=axis; e.mtt.value=value;
    mouse_touch_handler.event(&display,s.con,&e);
}
static void finger(int slot, int x, int y, int type) {
    mtt(INPUT_MULTI_TOUCH_TYPE_DATA,slot,INPUT_AXIS_X,x);
    mtt(INPUT_MULTI_TOUCH_TYPE_DATA,slot,INPUT_AXIS_Y,y);
    mtt(type,slot,0,0);
}
/* Contact i of frame at: path, state, identity, top-left pixel. */
static void contact(unsigned at, unsigned n, unsigned i, unsigned path,
                    unsigned state, unsigned x, unsigned y) {
    uint8_t *f=s.touch.frames[at];
    const uint8_t *c=f+26+30*i;
    assert(at<s.touch.frame_count && f[0]==0x74 && f[2]==26);
    assert(lduw_le_p(f+12)==30*n && f[14]==n && s.touch.frame_len[at]==26+30*n);
    assert(c[0]==path && c[1]==state && c[2]==2+i && c[3]==1);
    assert(lduw_le_p(c+4)==x && lduw_le_p(c+6)==1136-y);
    assert(lduw_le_p(c+18)==(state==7?0:256));
}
static void tick(void) {
    assert(deadline>=now); now=deadline; mouse_touch_timer_cb(&s);
}
static void frame(unsigned at, unsigned state, unsigned x, unsigned y) {
    uint8_t *f=s.touch.frames[at];
    assert(at<s.touch.frame_count && f[0]==0x74 && f[2]==26 && f[12]==30);
    assert(f[27]==state && f[28]==2 && f[26]==1);
    assert(lduw_le_p(f+30)==x && lduw_le_p(f+32)==1136-y);
    assert(lduw_le_p(f+44)==(state==7?0:256));
}
static void reset(void) {
    memset(&s,0,sizeof(s)); display.board=&s; now=0; deadline=-1;
    s.con=&s; s.mouse_touch_enabled=true; s.touch.input_control[0]=4;
}
int main(void) {
    assert(mouse_touch_handler.mask==
           (INPUT_EVENT_MASK_ABS|INPUT_EVENT_MASK_BTN|INPUT_EVENT_MASK_MTT));
    assert(mouse_touch_scale(-5,32767,640)==0);
    assert(mouse_touch_scale(32768,32767,640)==640);
    reset();
    packet(32767,32767,true); frame(0,1,640,1136);
    assert(s.touch.frame_count==1 && deadline==TOUCH_TICK_NS);
    tick(); frame(1,3,640,1136);
    assert(deadline==TOUCH_TICK_NS*2); /* continuous 60 Hz hold */
    tick(); frame(2,4,640,1136);
    now+=100;
    packet(1000,2000,true);
    int64_t due=deadline;
    for (int i=1;i<=50;i++) {
        now++; packet(2000+i,3000+i,true);
        assert(deadline==due && s.touch.frame_count==3);
    }
    tick(); frame(3,4,mouse_touch_scale(2050,32767,640),
                 MIN(1136, mouse_touch_scale(3050,32767,1136)+16));
    now++; packet(32767,0,false);
    assert(s.touch.frame_count==4);
    /* Hover after release cannot alter final move/up coordinates. */
    packet(0,32767,false);
    for (int i=0; i<18; i++) {
        tick(); frame(4+i,4,640,16);
    }
    tick(); frame(22,4,640,16);
    tick(); frame(23,7,640,16);
    assert(!s.mouse_touch.phase && deadline==-1);
    /* A quick click still preserves all transitions with settle delay. */
    reset(); packet(0,0,true); packet(0,0,false);
    frame(0,1,0,16);
    tick(); frame(1,3,0,16);
    tick(); frame(2,4,0,16);
    tick(); frame(3,4,0,16);
    tick(); frame(4,4,0,16);
    tick(); frame(5,4,0,16);
    tick(); frame(6,7,0,16);
    assert(!s.mouse_touch.phase && deadline==-1);
    /* FIFO pressure preserves transitions and coalesces the latest position. */
    reset(); s.touch.frame_count=64; packet(0,0,true);
    assert(s.mouse_touch.phase==1); tick(); assert(s.mouse_touch.phase==1);
    s.touch.frame_count=0; tick(); frame(0,1,0,16);
    tick(); s.touch.frame_count=64;
    packet(32767,32767,true); tick(); assert(s.mouse_touch.phase==3);
    packet(32767,32767,false); s.touch.frame_count=0;
    for (int i=0; i<18; i++) {
        tick(); frame(i,4,640,1136);
    }
    tick(); frame(18,4,640,1136);
    tick(); frame(19,7,640,1136);
    assert(!s.mouse_touch.phase && deadline==-1);
    /* Script ownership includes future scheduled gestures; no late start
     * from an ignored held button once the script finishes. */
    reset(); s.touch_script.count=5; packet(0,0,true);
    assert(!s.touch.frame_count);
    s.touch_step=5; packet(32767,0,true); assert(!s.touch.frame_count);
    packet(32767,0,false); packet(32767,0,true); frame(0,1,640,16);
    /* BF detectable, UI lock, force, disabled input. */
    reset(); s.touch.input_control[0]=0; packet(0,0,true);
    assert(!s.touch.frame_count);
    s.touch.input_control[0]=4; packet(0,0,true); assert(!s.touch.frame_count);
    packet(0,0,false); s.touch.reporting=1; packet(0,0,true);
    assert(!s.touch.frame_count);
    packet(0,0,false); s.touch_force=true; packet(0,0,true);
    frame(0,1,0,16); tick(); frame(1,3,0,16);
    reset(); s.mouse_touch_enabled=false; packet(0,0,true);
    assert(!s.touch.frame_count);
    /* Gate/FIFO waits don't advance the state machine or burst on resume. */
    reset(); packet(0,0,true); s.touch.input_control[0]=0;
    tick(); assert(s.touch.frame_count==1 && s.mouse_touch.phase==2);
    now+=10*TOUCH_TICK_NS; s.touch.input_control[0]=4;
    mouse_touch_timer_cb(&s); frame(1,3,0,16);
    packet(32767,0,false); assert(s.touch.frame_count==2);
    for (int i=0; i<18; i++) {
        tick(); frame(2+i,4,640,16);
    }
    tick(); frame(20,4,640,16);
    tick(); frame(21,7,640,16);
    assert(!s.mouse_touch.phase && deadline==-1);
    /* Other buttons and other consoles cannot start a finger contact. */
    reset(); event(INPUT_EVENT_KIND_BTN,1,true);
    mouse_touch_handler.sync(&display); assert(!s.touch.frame_count);
    QemuInputEvent other={.type=INPUT_EVENT_KIND_BTN};
    other.btn.button=INPUT_BUTTON_LEFT; other.btn.down=true;
    mouse_touch_handler.event(&display,&display,&other);
    mouse_touch_handler.sync(&display); assert(!s.touch.frame_count);
    /* A fresh click while the preceding release drains is ignored. */
    reset(); packet(0,0,true); packet(32767,0,false);
    packet(0,32767,true);
    tick(); frame(1,3,0,16);
    tick(); frame(2,4,640,16);
    for (int i=1; i<18; i++) {
        tick(); frame(2+i,4,640,16);
    }
    tick(); frame(20,4,640,16);
    tick(); frame(21,7,640,16);
    assert(!s.mouse_touch.phase && deadline==-1);
    packet(0,32767,true); assert(s.touch.frame_count==22);
    packet(0,32767,false); packet(0,32767,true); frame(22,1,0,1136);
    /* Motion before the down timer preserves the original start/down point. */
    reset(); packet(0,0,true); packet(32767,32767,true);
    tick(); frame(1,3,0,16); tick(); frame(2,4,640,1136);
    /* Two MTT fingers (pinch): both in every frame, stable paths. */
    reset();
    finger(0,0,0,INPUT_MULTI_TOUCH_TYPE_BEGIN);
    finger(1,32767,32767,INPUT_MULTI_TOUCH_TYPE_BEGIN);
    mouse_touch_handler.sync(&display);
    contact(0,2,0,1,1,0,16); contact(0,2,1,2,1,640,1136);
    tick(); contact(1,2,0,1,3,0,16); contact(1,2,1,2,3,640,1136);
    tick(); contact(2,2,1,2,4,640,1136); /* stationary hold keeps 60 Hz */
    finger(0,16384,16384,INPUT_MULTI_TOUCH_TYPE_UPDATE);
    finger(1,16384,16384,INPUT_MULTI_TOUCH_TYPE_UPDATE);
    mouse_touch_handler.sync(&display);
    tick(); contact(3,2,0,1,4,320,584); contact(3,2,1,2,4,320,584);
    /* A click during a live MTT gesture cannot start the mouse finger. */
    packet(0,0,true); assert(!s.mouse_touch.phase); packet(0,0,false);
    mtt(INPUT_MULTI_TOUCH_TYPE_END,0,0,0);
    mtt(INPUT_MULTI_TOUCH_TYPE_END,1,0,0);
    mouse_touch_handler.sync(&display);
    for (int i=0; i<18; i++) {
        tick(); contact(4+i,2,0,1,4,320,584); contact(4+i,2,1,2,4,320,584);
    }
    tick(); contact(22,2,1,2,4,320,584);
    tick(); contact(23,2,0,1,7,320,584); contact(23,2,1,2,7,320,584);
    assert(!multi_touch_active(&s.multi_touch) && deadline==-1);
    /* Re-touch while draining (quick flicks): up now, then a fresh start. */
    reset();
    finger(0,0,0,INPUT_MULTI_TOUCH_TYPE_BEGIN); mouse_touch_handler.sync(&display);
    tick(); tick();
    finger(0,32767,0,INPUT_MULTI_TOUCH_TYPE_END); mouse_touch_handler.sync(&display);
    tick(); contact(3,1,0,1,4,640,16); /* settle */
    finger(0,0,32767,INPUT_MULTI_TOUCH_TYPE_BEGIN); mouse_touch_handler.sync(&display);
    tick(); contact(4,1,0,1,4,640,16);
    tick(); contact(5,1,0,1,7,640,16);
    tick(); contact(6,1,0,1,1,0,1136);
    tick(); contact(7,1,0,1,3,0,1136);
    /* A finger leaving a live pinch goes up at once; the other stays. */
    reset();
    finger(0,0,0,INPUT_MULTI_TOUCH_TYPE_BEGIN);
    finger(1,32767,0,INPUT_MULTI_TOUCH_TYPE_BEGIN);
    mouse_touch_handler.sync(&display); tick(); tick();
    mtt(INPUT_MULTI_TOUCH_TYPE_END,1,0,0); mouse_touch_handler.sync(&display);
    tick(); contact(3,2,1,2,4,640,16);
    tick(); contact(4,2,1,2,7,640,16); contact(4,2,0,1,4,0,16);
    tick(); contact(5,1,0,1,4,0,16);
    /* A live mouse finger blocks MTT; that begin needs a fresh one. */
    reset(); packet(0,0,true);
    finger(0,0,0,INPUT_MULTI_TOUCH_TYPE_BEGIN); mouse_touch_handler.sync(&display);
    assert(!multi_touch_active(&s.multi_touch) && s.multi_touch.slot[0].blocked);
    /* Gate closed: no MTT contact either. Slots past the limit ignored. */
    reset(); s.touch.input_control[0]=0;
    finger(0,0,0,INPUT_MULTI_TOUCH_TYPE_BEGIN); mouse_touch_handler.sync(&display);
    assert(!s.touch.frame_count);
    reset(); finger(MULTI_TOUCH_SLOTS,0,0,INPUT_MULTI_TOUCH_TYPE_BEGIN);
    mouse_touch_handler.sync(&display); assert(!s.touch.frame_count);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='mouse-touch-host-') as directory:
            root = Path(directory)
            (root / 'test.c').write_text(harness)
            build = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', str(root / 'test.c'),
                                    '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
