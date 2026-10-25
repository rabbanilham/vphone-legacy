"""Compile the real Home/GPIO interrupt functions in a small native harness."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

class HomeButtonTests(unittest.TestCase):
    def test_gpio_interrupt_contract(self):
        source = (ROOT/'backend/s5l8960x.c').read_text()
        def function(name):
            start = source.index('static void '+name+'(')
            brace = source.index('{',start); depth = 1; end = brace+1
            while depth:
                depth += (source[end]=='{')-(source[end]=='}'); end += 1
            return source[start:end]
        prelude = '''
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <assert.h>
typedef struct A7State {
 uint8_t gpio_regs[0x100000]; bool touch_irq_low, home_pressed;
 struct { bool irq_low; } pmu; void *wifi; uint32_t device_irq[32];
} A7State;
static uint32_t ldl_le_p(const void *p) { uint32_t v; memcpy(&v,p,4); return v; }
static void stl_le_p(void *p,uint32_t v) { memcpy(p,&v,4); }
static void aic_update(A7State *s) { (void)s; }
'''
        harness = '''
int main(void) {
 static A7State s;
 for (unsigned group=0; group<7;group++) {
  for (unsigned type=0; type<16;type+=2) {
   memset(&s,0,sizeof(s));
   stl_le_p(s.gpio_regs+8,(group<<16)|type);
   uint8_t *status=s.gpio_regs+0x800+group*0x40;
   home_level(&s,true); assert(s.home_pressed);
   bool press=(type==0xa || type==0xc || type==6);
   assert(!!(ldl_le_p(status)&4)==press);
   assert(!!(s.device_irq[(108+group)/32]&(1u<<((108+group)%32)))==press);
   stl_le_p(status,0); touch_gpio_update(&s);
   assert(!!(ldl_le_p(status)&4)==(type==6));
   stl_le_p(status,0); home_release(&s); assert(!s.home_pressed);
   assert(!!(ldl_le_p(status)&4)==(type==8 || type==0xc || type==4));
  }
 }
 memset(&s,0,sizeof(s)); stl_le_p(s.gpio_regs+8,0xff000c);
 home_level(&s,true); home_release(&s);
 for(unsigned i=0;i<32;i++) assert(!s.device_irq[i]);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp); (tmp/'home.c').write_text(prelude+'\n'.join(function(n) for n in ['touch_gpio_update','home_level','home_release'])+harness)
            subprocess.run(['cc','-Wall','-Werror',str(tmp/'home.c'),'-o',str(tmp/'home')],check=True)
            subprocess.run([str(tmp/'home')],check=True)

if __name__=='__main__': unittest.main()
