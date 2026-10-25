"""GH: sparse Morton targets, region clears, exact uniforms and TA snapshots.

The optional corpus checks use build/agx-corpus (the capture-time framebuffer
is not a device oracle). Synthetic checks always run, including the C core.
"""
from pathlib import Path
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

from backend import agx_replay_offline as replay
from backend.agx_frame import Frame

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'build/agx-corpus/notification-center/dumps'
sys.path.insert(0, str(ROOT / 'scripts'))
import agx_corpus


def build_test_harness(directory):
    """Compile the actual core with host memory and a functional test entry."""
    source = (ROOT / 'backend/agx_replay_host.c').read_text()
    path = directory / 'nc_host.c'
    path.write_text('#define main original_main\n' + source + '\n#undef main\n' + C_TEST)
    saved = agx_corpus.HARNESS_SRC
    try:
        agx_corpus.HARNESS_SRC = path
        return agx_corpus.build_harness(directory)
    finally:
        agx_corpus.HARNESS_SRC = saved


# Tests use unmapped Morton padding as a fault detector. No iOS or sockets.
C_TEST = r'''
static void page_at(uint64_t base) {
    uint64_t *key = g_new(uint64_t, 1); *key = base;
    g_hash_table_insert(host_pages, key, g_malloc0(4096));
}
static int sparse_target(void) {
    A7State state = {0};
    host_pages = new_page_table();
    host_dirty = g_hash_table_new_full(g_int64_hash,g_int64_equal,g_free,NULL);
    /* 128x96 Y-first Morton: four padding pages must remain unmapped. */
    for (unsigned p = 0; p < 16; p++) {
        if (p != 5 && p != 7 && p != 13 && p != 15) page_at(0x100000 + p * 4096);
    }
    AGXSurface sf = {.base=0x100000, .w=128, .h=96, .twiddled=true};
    AGXTexture t = {.addr=sf.base, .w=128, .h=96, .twiddled=true};
    uint32_t row[128], got[128];
    for (unsigned y=0; y<96; y++) {
        for (unsigned x=0; x<128; x++) row[x] = 0xff000000 | (y<<8) | x;
        if (!agx_target_write(&state, 1, &sf, 0, y, row, 128)) return 1;
    }
    for (unsigned y=0; y<96; y++) {
        if (!agx_texture_row(&state, 1, &t, y, got)) return 2;
        for (unsigned x=0; x<128; x++) if (got[x] != (0xff000000 | (y<<8) | x)) return 3;
    }
    uint32_t *linear = g_new(uint32_t,128*96);
    if(!agx_nc_transfer(&state,1,sf.base,128,96,linear,false)) return 30;
    for(unsigned y=0;y<96;y++) for(unsigned x=0;x<128;x++) {
        if(linear[y*128+x]!=(0xff000000|(y<<8)|x)) return 31;
        linear[y*128+x]^=0x00010101;
    }
    if(!agx_nc_transfer(&state,1,sf.base,128,96,linear,true)) return 32;
    for(unsigned y=0;y<96;y++) {
        if(!agx_texture_row(&state,1,&t,y,got)) return 33;
        for(unsigned x=0;x<128;x++) if(got[x]!=linear[y*128+x]) return 34;
    }
    /* Restore the fixture before testing a partial clear. */
    for(unsigned y=0;y<96;y++) for(unsigned x=0;x<128;x++) linear[y*128+x]^=0x00010101;
    if(!agx_nc_transfer(&state,1,sf.base,128,96,linear,true)) return 35;
    g_free(linear);
    /* A five-pixel partial clear must neither truncate nor overwrite neighbours. */
    for (unsigned x=0; x<5; x++) row[x] = 0x80402010;
    if (!agx_target_write(&state, 1, &sf, 31, 65, row, 5)) return 4;
    if (!agx_target_read(&state, 1, &sf, 30, 65, got, 7)) return 5;
    if (got[0] != (0xff000000 | (65<<8) | 30) || got[6] != (0xff000000 | (65<<8) | 36)) return 6;
    for (unsigned x=1; x<6; x++) if (got[x] != 0x80402010) return 7;
    if(!agx_nc_sparse_layout(&state,1,sf.base,128,96)) return 36;
    for(unsigned p=5;p<16;p+=2) if(p==5||p==7||p==13||p==15) page_at(sf.base+p*4096);
    if(agx_nc_sparse_layout(&state,1,sf.base,128,96)) return 37;
    if(agx_nc_sparse_layout(&state,1,sf.base,640,96)) return 38;
    if(agx_nc_sparse_layout(&state,1,sf.base,640,416)) return 39;
    return 0;
}
static int clear_and_reduction(void) {
    A7State state = {0};
    host_pages = new_page_table(); host_dirty = g_hash_table_new_full(g_int64_hash,g_int64_equal,g_free,NULL);
    page_at(AGX_PDS_HEAP | 0x80001000); page_at(0x2000);
    uint32_t ppp[32]={0}, desc[8]={0x2000,0,0x90000004,0x10000000};
    ppp[12]=0x80001000;
    float rgba[4]={0.25f,0.5f,0.75f,0.5f};
    agx_ctx_write(&state,1,AGX_PDS_HEAP|ppp[12],desc,32);
    agx_ctx_write(&state,1,0x2000,rgba,16);
    uint32_t colour=0;
    if (!agx_extract_region_clear(&state,1,ppp,&colour) || colour!=0x804080bf) return 10;
    desc[4]=0x2100; desc[6]=0x2130;
    agx_ctx_write(&state,1,AGX_PDS_HEAP|ppp[12],desc,32);
    float constants[28]={0};
    constants[0]=0.125f; constants[12]=0.25f; constants[20]=0.375f; constants[24]=0.5f;
    agx_ctx_write(&state,1,0x2100,constants,sizeof(constants));
    float offsets[4][2];
    if (!agx_extract_downsample(&state,1,ppp,offsets)) return 11;
    for (unsigned i=0;i<4;i++) if(offsets[i][0]!=(i+1)*0.125f || offsets[i][1]!=0) return 12;
    uint32_t vs[16]={0}; vs[10]=0x112233; vs[12]=0x2300;
    float uniforms[28]={0}; uniforms[24]=uniforms[25]=1;
    agx_ctx_write(&state,1,0x2200,vs,sizeof(vs));
    agx_ctx_write(&state,1,0x2300,uniforms,sizeof(uniforms));
    uint32_t last_code=0; bool normalized=false,have=false;
    if(!agx_nc_copy_texmat(&state,1,0x2200,&last_code,&normalized,&have)) return 40;
    vs[12]=0;agx_ctx_write(&state,1,0x2200,vs,sizeof(vs));
    if(!agx_nc_copy_texmat(&state,1,0x2200,&last_code,&normalized,&have)) return 41;
    vs[10]++;agx_ctx_write(&state,1,0x2200,vs,sizeof(vs));
    if(agx_nc_copy_texmat(&state,1,0x2200,&last_code,&normalized,&have)) return 42;
    uint32_t pixels[8]={0xff000000,0xff000000,0xff000000,0xff000000,
                        0xffffffff,0xffffffff,0xffffffff,0xffffffff};
    float taps[4][2]={{0.5f,0},{0.375f,0},{0.25f,0},{0.125f,0}};
    if(agx_sample_downsample(pixels,8,1,4.5f,0.5f,taps)!=0xff808080) return 13;
    return 0;
}
static int snapshot_case(const char *dump) {
    FILE *f=fopen(dump,"rb"); if(!f) return 20;
    uint8_t hdr[64],raw[0x280],ta[0x280],record[4112];
    if(fread(hdr,1,64,f)!=64 || fread(raw,1,sizeof(raw),f)!=sizeof(raw) || fread(ta,1,sizeof(ta),f)!=sizeof(ta)) return 21;
    host_pages=new_page_table(); host_dirty=g_hash_table_new_full(g_int64_hash,g_int64_equal,g_free,NULL);
    for(uint64_t i=0;i<ldq_le_p(hdr+48);i++) {
        if(fread(record,1,sizeof(record),f)!=sizeof(record)) return 22;
        uint64_t *key=g_new(uint64_t,1); *key=ldq_le_p(record);
        g_hash_table_insert(host_pages,key,g_memdup2(record+16,4096));
    }
    fclose(f);
    A7State state={0}; uint64_t cat=ldq_le_p(hdr+16);
    AGXReplaySnapshot *snap=agx_capture_ta(&state,cat,ta);
    /* Locate the 7-pair draw without consulting the following allocation. */
    uint64_t pc=ldq_le_p(ta+0x68),vs=0; uint32_t retained[32]={0};
    for(unsigned guard=0;guard<4096;guard++) {
        uint32_t w[64]; if(!agx_ctx_read(&state,cat,pc,w,4)) return 23;
        unsigned kind=w[0]>>29;
        if(kind==6) break;
        if(kind==4) { agx_ctx_read(&state,cat,pc+4,w+1,4);pc=((uint64_t)(w[0]&255)<<32)|(w[1]&~3u); }
        else if(kind==2) {
            unsigned extra=!!(w[0]&(1u<<28))+!!(w[0]&(1u<<27))+3*!!(w[0]&(1u<<26));
            if(w[0]&(1u<<27)) {agx_ctx_read(&state,cat,pc+4+((w[0]&(1u<<28))?4:0),w+1,4);vs=AGX_PDS_HEAP|w[1];} pc+=4*(1+extra);
        } else if(kind==0) {
            agx_ctx_read(&state,cat,pc+4,w+1,4); uint64_t p=((uint64_t)(w[0]&255)<<32)|(w[1]&~3u);unsigned n=MIN((w[0]>>8)&0xfffff,64u);
            agx_ctx_read(&state,cat,p,w,n*4);agx_update_ppp_state(retained,w,n);pc+=8;
        } else if(kind==1) pc+=12;
        else if(kind==3) {
            pc+=12; uint64_t prog=agx_replay_program(&state,cat,retained,32);
            if(prog!=AGX_USC_BLUR_7TAP) continue;
            uint64_t up,wp;agx_ctx_read(&state,cat,vs+48,&up,8);agx_ctx_read(&state,cat,(AGX_PDS_HEAP|retained[12])+16,&wp,8);
            uint64_t ub=up&~UINT64_C(4095),wb=wp&~UINT64_C(4095);
            if(!g_hash_table_lookup(snap->pages,&ub)||!g_hash_table_lookup(snap->pages,&wb)) return 24;
            float off[8],weights[8];unsigned n;bool vert;
            memset(g_hash_table_lookup(host_pages,&ub),0,4096);
            memset(g_hash_table_lookup(host_pages,&wb),0,4096);
            agx_read_snapshot=snap;
            bool ok=agx_extract_nc_blur_params(&state,cat,vs,retained,32,prog,off,weights,&n,&vert);
            agx_read_snapshot=NULL;
            if(!ok || n!=7 || fabsf(off[6]-12.37754f)>0.0001f) return 25;
            agx_snapshot_unref(snap);return 0;
        } else return 26;
    }
    return 27;
}
static int roi_case(void) {
    enum {W=160,H=160};
    uint32_t *full=g_new(uint32_t,W*H),*roi=g_new(uint32_t,W*H),*tmp=g_new(uint32_t,W*H);
    float offsets[7],weights[7];
    for(unsigned i=0;i<7;i++){offsets[i]=agx_blur_taps_7[i].offset;weights[i]=agx_blur_taps_7[i].weight;}
    for(unsigned vertical=0;vertical<2;vertical++) {
        for(unsigned y=0;y<H;y++) for(unsigned x=0;x<W;x++) {
            full[y*W+x]=roi[y*W+x]=((x*13+y*7)&255)|(((x*9+y*3)&255)<<8)|0xff600000;
        }
        agx_blur_1d(full,tmp,W,H,offsets,weights,7,vertical);
        agx_nc_blur_roi(roi,W,H,20,20,120,130,offsets,weights,7,vertical);
        for(unsigned y=34;y<116;y++) for(unsigned x=34;x<106;x++) {
            if(full[y*W+x]!=roi[y*W+x]) return 50;
            if(agx_sample_bilinear(full,W,H,x+0.25f,y+0.75f)!=
               agx_sample_bilinear(roi,W,H,x+0.25f,y+0.75f)) return 51;
        }
    }
    g_free(full);g_free(roi);g_free(tmp);return 0;
}
int main(int argc,char **argv) {
    if(argc>1 && !strcmp(argv[1],"roi")) return roi_case();
    if(argc>2 && !strcmp(argv[1],"snapshot")) return snapshot_case(argv[2]);
    if(argc>1 && !strcmp(argv[1],"sparse")) return sparse_target();
    return clear_and_reduction();
}
'''


class NCBlurTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = build_test_harness(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_c(self, *args):
        r = subprocess.run([self.binary, *map(str, args)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sparse_morton_target_roundtrip_and_partial_clear(self):
        self.run_c('sparse')

    def test_nonzero_clear_and_downsample_constant_slots_c(self):
        self.run_c('constants')

    def test_roi_prefilter_preserves_full_source_hv_values(self):
        self.run_c('roi')

    def test_normalized_repeated_u_keeps_v_normalized(self):
        path = CORPUS / 'frame-53.bin'
        if not path.exists():
            self.skipTest('NC capture not installed')
        f = Frame(path)
        state = {}
        draws = list(replay.walk(f))
        self.assertTrue(replay.nc_copy_texmat(f, draws[1][0], state))
        self.assertTrue(replay.nc_copy_texmat(f, draws[2][0], state))
        retained = [0] * 32
        for vs, ppp, draw, vp in draws:
            replay.update_ppp_state(retained, ppp)
        tex = replay.texture(f, retained, replay.program(f, retained))
        self.assertEqual((tex['w'], tex['h']), (4, 2048))
        # Old max-UV heuristic samples row zero; normalized V samples row 195.
        self.assertEqual(tuple(replay.texel(f, tex, 0, 0)), (140, 140, 140, 255))
        self.assertEqual(tuple(replay.texel(f, tex, 0, 195)), (0, 0, 0, 0))
        # U repeat does not affect this strip: all four columns are identical.
        for y in range(tex['h']):
            self.assertEqual(len({replay.texel(f, tex, x, y) for x in range(4)}), 1)

    def test_sparse_layout_requires_shape_holes_and_complete_tiles(self):
        class Memory:
            pages = {0x100000 + p * 4096: bytes(4096) for p in range(16)
                     if p not in (5, 7, 13, 15)}
        f = Memory()
        self.assertTrue(replay.nc_sparse_layout(f, 0x100000, 128, 96))
        # A dense allocation of the same size retains main's addressing.
        f.pages = {0x100000 + p * 4096: bytes(4096) for p in range(16)}
        self.assertFalse(replay.nc_sparse_layout(f, 0x100000, 128, 96))
        # A single missing logical tile is not evidence for the Morton case.
        del f.pages[0x100000]
        self.assertFalse(replay.nc_sparse_layout(f, 0x100000, 128, 96))
        # Existing unlock/status glyph extents must remain untouched.
        self.assertFalse(replay.nc_sparse_layout(f, 0x100000, 640, 96))
        self.assertFalse(replay.nc_sparse_layout(f, 0x100000, 640, 416))

    def test_downsample_eight_samples_do_not_collapse_to_single_tap(self):
        # Alternating wide stripes: the center is white; symmetric offsets
        # deliberately sample both colours. Check against an independent mean.
        pixels = [(0, 0, 0, 255)] * 4 + [(255, 255, 255, 255)] * 4
        offsets = [(0.5, 0), (0.375, 0), (0.25, 0), (0.125, 0)]
        result = replay.sample_downsample(pixels, 8, 1, 4.5, 0.5, offsets)
        self.assertEqual(result, (128, 128, 128, 255))
        self.assertNotEqual(result, replay.sample_bilinear(pixels, 8, 1, 4.5, 0.5))

    def test_captured_chain_layout_and_real_blur_uniforms(self):
        if not (CORPUS / 'frame-8.bin').exists():
            self.skipTest('NC capture not installed')
        for n, direction in [(8, 'horizontal'), (9, 'vertical')]:
            f = Frame(CORPUS / f'frame-{n}.bin')
            state = [0] * 32
            for vs, ppp, draw, _ in replay.walk(f):
                replay.update_ppp_state(state, ppp)
                if replay.program(f, state) == replay.BLUR_7TAP:
                    params = replay.extract_nc_blur_params(f, vs, state, replay.BLUR_7TAP)
                    self.assertTrue(params['extracted'])
                    self.assertEqual(params['direction'], direction)
                    self.assertEqual(params['n_taps'], 7)
                    self.assertAlmostEqual(params['offsets'][-1], 12.37754, places=4)
                    self.assertAlmostEqual(sum(params['weights']), 0.5, places=6)
                    # Malformed/missing own uniforms must never borrow neighbours.
                    bad = state.copy(); bad[12] = 0
                    self.assertFalse(replay.extract_nc_blur_params(f, vs, bad, replay.BLUR_7TAP)['extracted'])
                    break
            self.assertEqual(replay.audit_frame(f)[0][1], 'replayed exactly: region clear (uniform RGBA)')
            self.run_c('snapshot', f.path)

    def test_real_sparse_target_all_logical_texels_are_mapped(self):
        path = CORPUS / 'frame-6.bin'
        if not path.exists():
            self.skipTest('NC capture not installed')
        f = Frame(path)
        rt = replay.render_target_texture(f, 128, 96)
        self.assertTrue(rt['twiddled'])
        class CheckedMemory:
            def read(self, address, count):
                return f.read(address, count)  # propagate missing page errors
        for y in range(96):
            for x in range(128):
                replay.texel(CheckedMemory(), rt, x, y)
        with self.assertRaises(KeyError):
            f.read(rt['addr'] + 40 * 512, 512)  # previous linear addressing


if __name__ == '__main__':
    unittest.main()
