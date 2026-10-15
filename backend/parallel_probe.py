"""Run several display_probe.py variants concurrently on clonefile disk copies.

Each variant is one hypothesis; QEMU runs single-threaded TCG, so independent
boots share the host without slowing each other. Disk copies are removed by
probe_cleanup after every reaped probe.
"""
import argparse,json,shlex,subprocess,sys,time
from pathlib import Path
from probe_cleanup import cleanup

HERE=Path(__file__).resolve().parent

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-root',type=Path,required=True,help='Parent directory; each variant writes <out-root>/<name>')
    p.add_argument('--common',default='',help='Arguments shared by every variant')
    p.add_argument('--variant',action='append',required=True,help='name=extra display_probe arguments')
    args=p.parse_args()
    common=shlex.split(args.common);procs={}
    args.out_root.mkdir(parents=True,exist_ok=True)
    for item in args.variant:
        name,extra=item.split('=',1);out=args.out_root/name
        cmd=[sys.executable,str(HERE/'display_probe.py'),'--output',str(out),*common,*shlex.split(extra)]
        log=(args.out_root/f'{name}.log').open('wb')
        procs[name]=(subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT),out,time.monotonic())
    summary={}
    for name,(proc,out,started) in procs.items():
        code=proc.wait();record=dict(exit_code=code,wall_seconds=round(time.monotonic()-started,1))
        observed=out/'observations.json'
        if observed.exists():record.update(json.loads(observed.read_text()))
        if (out/'process-exit.json').exists():
            try:cleanup(out);record['images_removed']=True
            except Exception as exc:record['cleanup_error']=repr(exc)
        summary[name]=record
    (args.out_root/'parallel-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    return 0 if all(v['exit_code']==0 for v in summary.values()) else 1

if __name__=='__main__':
    sys.exit(main())
