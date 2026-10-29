"""Program-scoped folder descriptors and complementary procedural corner legs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import agx_corpus

ROOT = Path(__file__).resolve().parents[1]


class FolderCornersTests(unittest.TestCase):
    def test_c_descriptor_and_complementary_coverage(self):
        """Execute the production C helpers, including descriptor rejection controls."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            agx_corpus.build_harness(work)
            host = agx_corpus.HARNESS_SRC.read_text()
            main = host.index('int main(')
            host = host[:main] + r'''
#include <assert.h>
int main(void) {
    A7State s = {0}; AGXTexture t;
    host_pages = new_page_table();
    uint64_t pds = AGX_PDS_HEAP | 0x80001000, block = 0x10000000, addr = 0x10758000;
    uint8_t *p = g_malloc0(4096), *b = g_malloc0(4096);
    memcpy(p, &block, 8);
    uint32_t words[] = {0x38d1c000,0x38d1c000,0xfff,0x2400,0,0,
                       0x60000a61,0x009f027c,0x60000001,0x41d,0x3d93a000,0x3e59a000};
    memcpy(b, words, sizeof(words));
    g_hash_table_insert(host_pages,g_memdup2(&pds,8),p);
    g_hash_table_insert(host_pages,g_memdup2(&block,8),b);
    g_hash_table_insert(host_pages,g_memdup2(&addr,8),g_malloc0(4096));
    uint32_t ppp[] = {0x80001000};
    assert(agx_replay_texture(&s,1,ppp,1,AGX_USC_FOLDER_MASK,&t)==AGX_TEX_FOUND);
    assert(t.addr==addr && t.w==160 && t.h==160 && t.stride==640 && t.bpp==4);
    assert(t.sampler_words[0]==0xfff && t.sampler_words[1]==0x2400);
    assert(agx_replay_texture(&s,1,ppp,1,AGX_USC_COPY,&t)==AGX_TEX_NONE);
    words[3]=0; memcpy(b,words,sizeof(words));
    assert(agx_replay_texture(&s,1,ppp,1,AGX_USC_FOLDER_MASK,&t)==AGX_TEX_NONE);
    float map[] = {1.0f/70,0,-1,0,1.0f/70,-1};
    assert(agx_folder_coverage(-1,-1,map)==0);
    assert(agx_folder_coverage(0,0,map)==255);
    assert(agx_folder_coverage(0,-0.5f,map)==255);
    unsigned aa=agx_folder_coverage(-0.70710678f,-0.70710678f,map);
    assert(aa>100 && aa<155);
    for (int mirror=0;mirror<4;mirror++) {
        float m[6]; memcpy(m,map,sizeof(m));
        if (mirror&1) m[0]=-m[0];
        if (mirror&2) m[4]=-m[4];
        assert(agx_folder_coverage(-0.70710678f,-0.70710678f,m)==aa);
    }
    /* Opaque white stays white to one quantization step through both legs. */
    for(unsigned a=0;a<=255;a++) {
        uint32_t out=agx_blend_plus_lighter(agx_scale_coverage(0xffffffff,a),
                                           agx_scale_coverage(0xffffffff,255-a));
        assert(out==0xffffffff);
    }
    return 0;
}
'''
            source = work / 'helpers.c'
            source.write_text(host)
            # Reuse the platform/compiler flags selected by the host builder.
            original = agx_corpus.HARNESS_SRC
            try:
                agx_corpus.HARNESS_SRC = source
                helper = agx_corpus.build_harness(work)
            finally:
                agx_corpus.HARNESS_SRC = original
            result = subprocess.run([str(helper)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_folder_corpus_draws_are_no_longer_skipped(self):
        corpus_root = Path(os.environ.get('AGX_TEST_CORPUS', ROOT / 'corpus'))
        corpus = corpus_root / 'folder/dumps'
        if not corpus.exists():
            corpus = ROOT / 'build/folder-fix/corpus/folder/dumps'
        if not corpus.exists():
            self.skipTest('optional folder corpus not available')
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            binary = agx_corpus.build_harness(work)
            dumps = sorted(corpus.glob('frame-*.bin'), key=lambda p: int(p.stem.split('-')[1]))
            res = subprocess.run([str(binary), '--chain', str(work/'out'), *map(str,dumps)],
                                 check=True, capture_output=True, text=True)
            infos = [json.loads(line) for line in res.stdout.splitlines()]
            for number, draws in ((13,17),(16,17),(22,14)):
                self.assertEqual(infos[number-1]['draws'], draws)
                self.assertEqual(infos[number-1]['skips'], {})
                self.assertEqual(infos[number-1]['guest_errors'], 0)
