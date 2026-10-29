"""Folder backdrop Morton storage, corner coverage and captured chain regression."""
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_agx_nc_blur as nc
from backend import agx_replay_offline as replay
from backend.agx_frame import Frame

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'build/agx-corpus/folder/dumps'

C_FOLDER = r'''
static int folder_storage(void) {
    A7State state={0};
    host_pages=new_page_table();
    host_dirty=g_hash_table_new_full(g_int64_hash,g_int64_equal,g_free,NULL);
    AGXTexture tex={.addr=0x2000000,.w=640,.h=640,.twiddled=true};
    for(unsigned y=0;y<640;y+=32) for(unsigned x=0;x<640;x+=32)
        page_at(agx_texel_gva(&tex,x,y)&~UINT64_C(4095));
    if(!agx_nc_sparse_layout(&state,1,tex.addr,640,640)) return 60;
    uint32_t *pixels=g_new(uint32_t,640*640), row[640];
    for(unsigned y=0;y<640;y++) for(unsigned x=0;x<640;x++)
        pixels[y*640+x]=0xff000000|((x&255)<<8)|(y&255);
    if(!agx_nc_transfer(&state,1,tex.addr,640,640,pixels,true)) return 61;
    AGXSurface sf={.base=tex.addr,.w=640,.h=640,.twiddled=true};
    for(unsigned y=0;y<640;y++) {
        if(!agx_target_read(&state,1,&sf,0,y,row,640)) return 62;
        for(unsigned x=0;x<640;x++) if(row[x]!=pixels[y*640+x]) return 63;
    }
    /* A partial destination update must survive flush and texture sampling. */
    sf.pixels=g_new0(uint32_t,640*640);sf.loaded=g_malloc0(800);sf.dirty=sf.loaded+400;
    row[0]=0x80402010;
    if(!agx_target_write(&state,1,&sf,79,79,row,1)) return 64;
    if(!agx_nc_transfer_region(&state,1,sf.base,640,640,sf.pixels,true,64,64,96,96)) return 65;
    if(!agx_texture_row(&state,1,&tex,79,row)||row[79]!=0x80402010||row[78]!=pixels[79*640+78]) return 66;
    /* Dense or incomplete allocations cannot select the new layout. */
    uint64_t hole=agx_texel_gva(&tex,608,608)&~UINT64_C(4095);
    g_hash_table_remove(host_pages,&hole);
    if(agx_nc_sparse_layout(&state,1,tex.addr,640,640)) return 67;
    page_at(hole);
    for(unsigned i=0;i<1024;i++) {
        uint64_t base=tex.addr+i*4096;
        if(!g_hash_table_lookup(host_pages,&base)) page_at(base);
    }
    if(agx_nc_sparse_layout(&state,1,tex.addr,640,640)) return 68;
    return 0;
}
static int folder_corners(void) {
    float xy[3][2]={{16,264},{92,264},{92,340}};
    float uv[3][2]={{-1,-1},{0,-1},{0,0}}, map[6];
    if(!agx_quad_affine(xy,uv,map)) return 70;
    if(agx_radial_coverage(-0.99f,-0.99f,map)!=0) return 71;
    if(agx_radial_coverage(-0.5f,-0.5f,map)!=255) return 72;
    unsigned a=agx_radial_coverage(-0.7071f,-0.7071f,map);
    if(a==0||a==255) return 73;
    float mirror[6]={-map[0],-map[1],0,map[3],map[4],map[5]};
    if(agx_radial_coverage(0.7071f,-0.7071f,mirror)!=a) return 75;
    uint32_t colour=0xff804020;
    uint32_t got=agx_blend_plus_lighter(agx_scale_premul(colour,a),agx_scale_premul(colour,255-a));
    for(unsigned c=0;c<4;c++) if(abs((int)((got>>(c*8))&255)-(int)((colour>>(c*8))&255))>1) return 74;
    return 0;
}
'''

class FolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        saved = nc.C_TEST
        try:
            nc.C_TEST = saved.replace('int main(int argc,char **argv) {', C_FOLDER + '''
int main(int argc,char **argv) {
    if(argc>1 && !strcmp(argv[1],"folder-storage")) return folder_storage();
    if(argc>1 && !strcmp(argv[1],"folder-corners")) return folder_corners();
''')
            cls.binary = nc.build_test_harness(Path(cls.tmp.name))
        finally:
            nc.C_TEST = saved

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sparse_640_target_flush_and_texture_readback(self):
        subprocess.run([self.binary, 'folder-storage'], check=True)

    def test_mirrored_corner_coverage_and_complementary_restore(self):
        subprocess.run([self.binary, 'folder-corners'], check=True)

    def test_folder_capture_chain_preserves_panel_and_icon_corners(self):
        dumps = [CORPUS / f'frame-{n}.bin' for n in range(1, 23)]
        if not all(p.exists() for p in dumps):
            self.skipTest('folder corpus not installed')
        prefix = Path(self.tmp.name) / 'folder'
        # The functional harness main is reserved for synthetic checks; use
        # the unmodified host main for the captured end-to-end replay.
        binary = nc.agx_corpus.build_harness(Path(self.tmp.name) / 'chain')
        result = subprocess.run([binary, '--chain', prefix, *dumps], check=True, capture_output=True, text=True)
        infos = [json.loads(s) for s in result.stdout.splitlines()]
        for n in (18, 19, 21):
            self.assertEqual(infos[n-1]['missing_rows'], 0)
            self.assertEqual(infos[n-1]['guest_errors'], 0)
        def pixel(frame, x, y):
            data = Path(f'{prefix}-{frame-1}.rgba').read_bytes()
            w, h = struct.unpack_from('<II', data, 4)
            off = 12 + (y*w+x)*4
            return tuple(data[off:off+4])
        # Empty middle of panel remains exactly the pre-children composite.
        self.assertEqual(pixel(22, 320, 600), pixel(20, 320, 600))
        self.assertGreater(pixel(22, 320, 600)[2], 100)
        self.assertGreater(sum(abs(a-b) for a,b in zip(pixel(22,320,600),pixel(17,320,600))), 40)
        # Background restore must preserve Contacts inside the rounded panel.
        contact = pixel(22, 80, 330)
        self.assertGreater(contact[0], 200)
        self.assertEqual(contact[:3], (contact[0],)*3)
        self.assertEqual(contact[3], 255)
        # Home corner must show wallpaper rather than the grey square backdrop.
        self.assertLess(max(abs(a-b) for a,b in zip(pixel(1,33,51),pixel(1,31,51))), 8)
        self.assertGreater(pixel(1,92,110)[0]-pixel(1,33,51)[0], 50)
        for n in (18, 19):
            f = Frame(dumps[n-1])
            self.assertTrue(replay.nc_sparse_layout(f, f.header['pbe_target'], 640, 640))
            self.assertIn('region clear', replay.audit_frame(f)[0][1])
