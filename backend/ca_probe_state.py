"""Read-only task-specific CA thread snapshots; unmapped data remains unknown."""
import bisect
import json
import struct
from pathlib import Path

def snapshot(gs, memory, u64, out, mapping_path, index_path):
    phys=u64(gs,0xffffff8000694020);virt=u64(gs,0xffffff8000694018)
    def direct(pa): return pa-phys+virt
    def read_user(pmap,va,size):
        result=bytearray()
        while len(result)<size:
            v=va+len(result);table=u64(gs,pmap)
            l1=u64(gs,table+((v>>27)&0xff8))
            if l1&3!=3:return None
            l2=u64(gs,direct(l1&0x0000fffffffff000)+((v>>18)&0xff8))
            if not l2&1:return None
            if l2&2:
                l3=u64(gs,direct(l2&0x0000fffffffff000)+((v>>9)&0xff8))
                if not l3&1:return None
                pa=(l3&0x0000fffffffff000)|(v&0xfff)
            else:pa=(l2&0x0000ffffffe00000)|(v&0x1fffff)
            n=min(size-len(result),0x1000-(v&0xfff));chunk=memory(gs,direct(pa),n)
            if len(chunk)!=n:return None
            result.extend(chunk)
        return bytes(result)
    mapping=json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    slide=int(mapping.get('slide','0'),0)
    symbols=json.loads(Path(index_path).read_text())['symbols']
    local=Path(index_path).with_name('cache-local-symbols.json')
    if local.exists():symbols+=json.loads(local.read_text())
    syms=sorted((int(s['address'],16),s['name']) for s in symbols if int(s['address'],16))
    addrs=[a for a,_ in syms]
    def symbol(a):
        n=bisect.bisect_right(addrs,a-slide)-1
        if n>=0 and a-slide-addrs[n]<0x1000:return syms[n][1]+'+'+hex(a-slide-addrs[n])
        return None
    mask=u64(gs,0xffffff800067c978);table=u64(gs,0xffffff800067c980);result=[];inventory=[]
    for bucket in range(min(mask+1,4096)):
        proc=u64(gs,table+bucket*8);seen=set()
        while proc and proc not in seen and len(seen)<256:
            seen.add(proc);name=memory(gs,proc+0x254,16).split(b'\0')[0].decode(errors='replace')
            inventory.append(dict(name=name,pid=int.from_bytes(memory(gs,proc+0x10,4),'little'),proc=hex(proc)))
            if name in ('backboardd','SpringBoard','lsd','installd'):
                task=u64(gs,proc+0x18);vmap=u64(gs,task+0x28);pmap=u64(gs,vmap+0x58)
                record=dict(process=name,pid=int.from_bytes(memory(gs,proc+0x10,4),'little'),task=hex(task),pmap=hex(pmap),threads=[])
                head=task+0x48;thread=u64(gs,head);threads_seen=set()
                while thread and thread!=head and thread not in threads_seen and len(threads_seen)<20:
                    threads_seen.add(thread);raw=memory(gs,thread+0x328,33*8)
                    if len(raw)==33*8:
                        x=struct.unpack('<33Q',raw);fp=x[29];frames=[];frames_seen=set()
                        for _ in range(12):
                            if fp in frames_seen:break
                            frames_seen.add(fp);data=read_user(pmap,fp,16)
                            if not data:break
                            fp,lr=struct.unpack('<QQ',data);frames.append(dict(lr=hex(lr),symbol=symbol(lr)))
                        message=read_user(pmap,x[0],256)
                        pcb=u64(gs,thread+0x680);k_event=u64(gs,thread+0x20);k_cont=u64(gs,thread+0x58)
                        k_fp=u64(gs,pcb+0xf0) if pcb>>56==0xff else 0
                        k_lr=u64(gs,pcb+0xf8) if pcb>>56==0xff else 0
                        k_sp=u64(gs,pcb+0x100) if pcb>>56==0xff else 0
                        k_frames=[];curr_fp=k_fp
                        for _ in range(16):
                            if not (curr_fp>>56==0xff):break
                            fdata=memory(gs,curr_fp,16)
                            if len(fdata)!=16:break
                            prev_fp,ret_lr=struct.unpack('<QQ',fdata)
                            k_frames.append(dict(fp=hex(curr_fp),lr=hex(ret_lr)))
                            if prev_fp<=curr_fp:break
                            curr_fp=prev_fp
                        record['threads'].append(dict(thread=hex(thread),tid=u64(gs,thread+0x740),pc=hex(x[32]),pc_symbol=symbol(x[32]),lr=hex(x[30]),lr_symbol=symbol(x[30]),registers=[hex(v) for v in x],frames=frames,message_head=message.hex() if message else None,kernel_state=dict(pcb=hex(pcb),wait_event=hex(k_event),continuation=hex(k_cont),lr=hex(k_lr),fp=hex(k_fp),sp=hex(k_sp),frames=k_frames)))
                    thread=u64(gs,thread+0x308)
                result.append(record)
            proc=u64(gs,proc+0xb0)
    (out/'ca-thread-state.json').write_text(json.dumps(dict(cache_slide=hex(slide),tasks=result,process_inventory=inventory),indent=2)+'\n')
