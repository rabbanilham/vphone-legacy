"""Small state machines for qualified native debugger observations."""
import time

def pixel_present(analysis,condition):
    return bool(analysis['non_zero_bytes'] if condition=='first-pixel' else analysis['non_zero_rgb_bytes'])

def instruction_mapped(actual,expected):
    if not actual:return False
    if actual!=expected:raise ValueError('Executed instruction bytes differ from the verified image')
    return True

class RunClock:
    def __init__(self, now=time.monotonic):
        self.now=now;self.started=now();self.resumed=None;self.active=0.0
    def resume(self):
        if self.resumed is None:self.resumed=self.now()
    def pause(self):
        if self.resumed is not None:
            self.active+=self.now()-self.resumed;self.resumed=None
    def elapsed(self):
        return self.active+(self.now()-self.resumed if self.resumed is not None else 0)
    def wall(self):return self.now()-self.started

class ReturnRegistry:
    def __init__(self):self.pending={}
    def add(self, pc, item):
        first=pc not in self.pending
        self.pending.setdefault(pc,[]).append(item)
        return first
    def match(self,pc,thread,stack):
        return next((p for p in reversed(self.pending.get(pc,[])) if p['thread']==thread and p['stack']==stack),None)
    def remove(self,pc,item):
        self.pending[pc].remove(item)
        if not self.pending[pc]:del self.pending[pc];return True
        return False
    def __contains__(self,pc):return pc in self.pending

class ScanoutTracker:
    """Conservative candidate gate; final surface/commit attribution stays explicit."""
    def __init__(self):
        self.client_commit=False;self.surfaces=set();self.physical_clients=set();self.successful_swaps=0
    def observe(self,e):
        name=e['name'];ret=e.get('return_of');process=e.get('identity',{}).get('process');x0=int(e['registers'][0],16)
        if name=='ca_commit' and not ret and process=='SpringBoard':self.client_commit=True
        if name=='surface_create' and ret and x0:self.surfaces.add(x0)
        if name=='swap_submit':
            if not ret and e.get('provider_properties',{}).get('IOClass',{}).get('text')=='AppleMobileADBE0':self.physical_clients.add(x0)
            elif ret and int(ret['object'],16) in self.physical_clients and x0&0xffffffff==0:self.successful_swaps+=1
    def eligible(self,rgb,layer_base,completion_token):
        return bool(rgb and self.client_commit and self.surfaces and self.successful_swaps and layer_base==0x18e0000 and completion_token)
