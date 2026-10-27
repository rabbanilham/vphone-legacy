"""Compile the actual Swift client and exercise fragmented RFB over a Unix socket."""
import os
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_CACHE = Path(tempfile.gettempdir()) / f'vphone-mc-{ROOT.name}'
MODULE_CACHE.mkdir(exist_ok=True)

class PlayerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('VPHONE_NATIVE_UI_TESTS') == '1',
                         'Interactive macOS desktop required; set VPHONE_NATIVE_UI_TESTS=1')
    def test_native_window_dispatches_initial_touch(self):
        source = (ROOT / 'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r"""
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let player = Player()
player.applicationDidFinishLaunching(Notification(name:NSApplication.didFinishLaunchingNotification))
assert(player.buttons.count == 3)
assert(player.buttons[1].toolTip == "Control Center")
player.status.isHidden = true
player.window.contentView!.layoutSubtreeIfNeeded()
var captured: [(Int,Int,Bool)] = []
player.screen.input = { x,y,down in captured.append((x,y,down)) }
func dispatch(_ type: NSEvent.EventType, _ point: NSPoint, modifiers:NSEvent.ModifierFlags = []) {
    let location = player.screen.convert(point,to:nil)
    let event = NSEvent.mouseEvent(with:type,location:location,modifierFlags:modifiers,
        timestamp:ProcessInfo.processInfo.systemUptime,windowNumber:player.window.windowNumber,
        context:nil,eventNumber:1,clickCount:1,pressure:1)!
    player.window.sendEvent(event)
}
let center = NSPoint(x:player.screen.bounds.midX,y:player.screen.bounds.midY)
assert(player.screen.acceptsFirstMouse(for:nil))
dispatch(.leftMouseDown,center)
dispatch(.leftMouseDragged,center)
dispatch(.leftMouseUp,center)
assert(captured.count == 3)
assert(captured[0].0 == 320 && captured[0].1 == 568 && captured[0].2)
assert(captured[1].2 && !captured[2].2)
// Resizing must not break routing or allow letterbox clicks to touch the guest.
player.window.setContentSize(NSSize(width:800,height:690))
player.window.contentView!.layoutSubtreeIfNeeded()
captured.removeAll()
dispatch(.leftMouseDown,NSPoint(x:1,y:200))
dispatch(.leftMouseUp,NSPoint(x:1,y:200))
assert(captured.isEmpty)
let resizedCenter = NSPoint(x:player.screen.bounds.midX,y:player.screen.bounds.midY)
dispatch(.leftMouseDown,resizedCenter)
assert(captured.count == 1 && captured[0].0 == 320 && captured[0].1 == 568)
player.screen.cancelTouch()
assert(captured.count == 2 && !captured[1].2)
dispatch(.leftMouseUp,resizedCenter)
assert(captured.count == 2)
// A stale scroll, magnify or scripted toolbar gesture cannot swallow a fresh click.
for stale in [TouchGestures.Kind.scroll,.magnify,.controlCenter] {
    captured.removeAll()
    player.screen.gestures.kind = stale
    player.screen.gestures.fingers = [NSPoint(x:320,y:568)]
    dispatch(.leftMouseDown,resizedCenter)
    dispatch(.leftMouseUp,resizedCenter)
    assert(captured.count == 2 && captured[0].2 && !captured[1].2)
    assert(player.screen.gestures.kind == .none)
}
// A failed QMP gesture must leave the native click path and retry callback alive.
player.handleGestureFailure("synthetic disconnected QMP")
assert(player.screen.input != nil && player.screen.gestures.send != nil && !player.inputFailed)
captured.removeAll()
dispatch(.leftMouseDown,resizedCenter)
dispatch(.leftMouseUp,resizedCenter)
assert(captured.count == 2)
// Native Option events must use MTT instead of also sending a VNC click.
captured.removeAll()
var multi: [(String,[NSPoint])] = []
player.screen.gestures.send = {type,points in multi.append((type,points))}
let pinchStart = NSPoint(x:resizedCenter.x+player.screen.displayRect.width/4,y:resizedCenter.y)
dispatch(.leftMouseDown,pinchStart,modifiers:[.option])
dispatch(.leftMouseDragged,NSPoint(x:pinchStart.x+10,y:pinchStart.y+20),modifiers:[.option,.shift])
dispatch(.leftMouseUp,pinchStart,modifiers:[.option])
assert(captured.isEmpty && multi.map {$0.0} == ["begin","update","end"])
assert(multi.allSatisfy {$0.1.count == 2})
let dx0 = multi[1].1[0].x-multi[0].1[0].x
let dx1 = multi[1].1[1].x-multi[0].1[1].x
assert(abs(dx0-dx1)<0.001)
player.window.orderOut(nil)
print("PASS: initial window click, native drag/release, resize, letterbox and focus cancellation")
"""
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='vp-native-') as tmp:
            tmp = Path(tmp)
            (tmp/'main.swift').write_text(source+harness)
            subprocess.run(['swiftc','-module-cache-path',str(MODULE_CACHE),
                            str(tmp/'main.swift'),'-o',str(tmp/'test')],check=True)
            subprocess.run([str(tmp/'test'),str(tmp/'missing-vnc'),str(tmp/'missing-qmp')],check=True,timeout=20)

    def test_wt_touch_gesture_mapping(self):
        source = (ROOT/'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r"""
let g = TouchGestures()
var frames: [(String,[NSPoint])] = []
g.send = {type,points in frames.append((type,points))}
// Trackpad deltas preserve natural-scroll direction and end without host momentum.
g.scroll(NSPoint(x:320,y:568),dx:0,dy:0,begin:true,end:false,wheel:false)
g.scroll(nil,dx:20,dy:-100,begin:false,end:false,wheel:false)
g.scroll(nil,dx:0,dy:0,begin:false,end:true,wheel:false)
assert(frames.map {$0.0} == ["begin","update","end"])
assert(frames[1].1 == [NSPoint(x:340,y:468)] && g.kind == .none)
// A wheel notch uses 40 guest pixels; bounds are clamped before QMP emission.
frames.removeAll()
g.scroll(NSPoint(x:620,y:20),dx:40,dy:-40,begin:true,end:false,wheel:true)
assert(frames[1].1 == [NSPoint(x:639,y:0)])
g.end();assert(frames.last!.0 == "end")
// Option pinch mirrors around the guest centre; Shift translates both contacts.
frames.removeAll();g.hint(NSPoint(x:160,y:568),option:true)
assert(g.overlay == [NSPoint(x:160,y:568),NSPoint(x:480,y:568)])
g.pinch(NSPoint(x:160,y:568),begin:true)
g.pinch(NSPoint(x:120,y:568),begin:false)
assert(frames[1].1 == [NSPoint(x:120,y:568),NSPoint(x:520,y:568)])
g.pinch(NSPoint(x:140,y:598),begin:false,pan:true)
assert(frames[2].1 == [NSPoint(x:140,y:598),NSPoint(x:540,y:598)])
g.end();g.hint(nil,option:false);assert(g.overlay.isEmpty && g.center == nil)
// Magnify uses an independent pointer centre and additive scale, as Cocoa did.
frames.removeAll()
g.magnify(NSPoint(x:300,y:400),delta:0,begin:true,end:false)
g.magnify(nil,delta:0.5,begin:false,end:false)
assert(frames[1].1 == [NSPoint(x:180,y:400),NSPoint(x:420,y:400)])
g.magnify(nil,delta:0,begin:false,end:true)
assert(frames.last!.0 == "end" && g.center == nil)
// Toolbar Control Center uses one bottom-edge contact and never overlaps input.
frames.removeAll()
assert(g.beginControlCenter())
assert(!g.beginControlCenter())
assert(frames[0].0 == "begin" && frames[0].1 == [NSPoint(x:320,y:1135)])
g.advanceControlCenter(0.5)
assert(frames[1].1 == [NSPoint(x:320,y:617.5)])
g.advanceControlCenter(1)
assert(frames[2].1 == [NSPoint(x:320,y:100)])
g.end();assert(frames.last!.0 == "end" && g.kind == .none)
g.advanceControlCenter(0.3);assert(frames.count == 4)
g.pinch(NSPoint(x:160,y:568),begin:true)
assert(!g.beginControlCenter());g.end()
// Each frame contains every contact, with atomic x/y data then slot transition.
let events = QMPInput.events("begin",[NSPoint(x:160,y:568),NSPoint(x:480,y:568)])
assert(events.count == 6)
for i in 0..<2 {
 let x = events[i*3]["data"] as! [String:Any]
 let y = events[i*3+1]["data"] as! [String:Any]
 let begin = events[i*3+2]["data"] as! [String:Any]
 assert(x["slot"] as! Int == i && y["slot"] as! Int == i)
 assert(x["axis"] as! String == "x" && y["axis"] as! String == "y")
 assert(y["value"] as! Int == 568*32767/1136)
 assert(begin["type"] as! String == "begin" && begin["tracking-id"] as! Int == i)
}
print("PASS: wt/touch scroll, wheel, mirrored pinch, shifted pan, magnify, cancellation and QMP slots")
"""
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='vp-gestures-') as tmp:
            tmp = Path(tmp);(tmp/'main.swift').write_text(source+harness)
            subprocess.run(['swiftc','-module-cache-path',str(MODULE_CACHE),str(tmp/'main.swift'),'-o',str(tmp/'test')],check=True)
            subprocess.run([str(tmp/'test')],check=True,timeout=20)

    def test_control_center_waits_for_digitizer_and_preserves_active_app(self):
        source = (ROOT/'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r"""
let qmp = QMPInput(CommandLine.arguments[1])
let done = DispatchSemaphore(value:0)
for _ in 0..<2 {
    qmp.prepareControlCenter {error in assert(error == nil);done.signal()}
    assert(done.wait(timeout:.now()+5) == .success)
}
print("PASS: ready guest keeps app context; gated digitizer gets Home wake and readiness retry")
"""
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='vp-ready-') as tmp:
            tmp=Path(tmp);(tmp/'main.swift').write_text(source+harness)
            subprocess.run(['swiftc','-module-cache-path',str(MODULE_CACHE),str(tmp/'main.swift'),'-o',str(tmp/'test')],check=True)
            server=socket.socket(socket.AF_UNIX);server.bind(str(tmp/'qmp'));server.listen()
            errors=[];downs=[]
            def serve():
                try:
                    with server.accept()[0] as conn:
                        conn.settimeout(5)
                        conn.sendall(b'{"QMP":{}}\n')
                        with conn.makefile('rwb',buffering=0) as stream:
                            queries=0
                            while True:
                                line=stream.readline()
                                if not line:break
                                request=__import__('json').loads(line);result={}
                                if request['execute']=='qom-get':
                                    self.assertEqual(request['arguments'],{'path':'/machine','property':'touch-reporting-enabled'})
                                    queries+=1;result=queries!=2
                                if request['execute']=='input-send-event':
                                    data=request['arguments']['events'][0]['data']
                                    self.assertEqual(data['key'],{'type':'qcode','data':'home'})
                                    downs.append(data['down'])
                                response={'return':result,'id':request['id']}
                                stream.write(__import__('json').dumps(response).encode()+b'\n')
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=serve);thread.start()
            try:subprocess.run([str(tmp/'test'),str(tmp/'qmp')],check=True,timeout=12)
            finally:thread.join(timeout=6);server.close()
            if errors:raise errors[0]
            self.assertEqual(downs,[True,False])

    def test_fragmented_rfb_and_geometry(self):
        source = (ROOT / 'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r'''
let rect = ScreenGeometry.rect(NSRect(x:0,y:0,width:800,height:568))
assert(rect.width == 320 && rect.minX == 240)
let point = ScreenGeometry.point(NSPoint(x:400,y:284),in:rect)
assert(point.0 == 320 && point.1 == 568)
let edge = ScreenGeometry.point(NSPoint(x:900,y:900),in:rect)
assert(edge.0 == 639 && edge.1 == 1135)
let client = RFB()
var frames = 0
client.frame = { image in
    let bitmap = NSBitmapImageRep(cgImage:image)
    let color = bitmap.colorAt(x:0,y:0)!.usingColorSpace(.deviceRGB)!
    assert(image.width == 640 && image.height == 1136)
    if frames == 0 { assert(color.redComponent > 0.99 && color.blueComponent < 0.01) }
    else { assert(color.blueComponent > 0.99 && color.redComponent < 0.01) }
    assert(bitmap.representation(using:.png,properties:[:]) != nil)
    frames += 1
    if frames == 1 { client.pointer(639,1135,true); client.pointer(639,1135,false); client.home() }
}
client.failed = { _ in }
client.run(CommandLine.arguments[1])
assert(frames == 2)
print("PASS: fragmented RFB, incremental rectangles, BGR pixels, PNG, coordinate scaling")
'''
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='vp-test-') as tmp:
            tmp = Path(tmp)
            (tmp / 'main.swift').write_text(source + harness)
            subprocess.run(['swiftc', '-module-cache-path', str(MODULE_CACHE), str(tmp/'main.swift'), '-o', str(tmp/'test')],check=True)
            errors = []
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(tmp/'vnc')); server.listen()
            def exact(conn, n):
                data = b''
                while len(data) < n:
                    chunk = conn.recv(n-len(data))
                    if not chunk: raise EOFError()
                    data += chunk
                return data
            def serve():
                try:
                    with server.accept()[0] as conn:
                        conn.settimeout(10)
                        def fragmented(data):
                            for pos in range(0,len(data),3): conn.sendall(data[pos:pos+3])
                        fragmented(b'RFB 003.008\n'); self.assertEqual(exact(conn,12),b'RFB 003.008\n')
                        fragmented(b'\x01\x01'); self.assertEqual(exact(conn,1),b'\x01')
                        fragmented(bytes(4)); self.assertEqual(exact(conn,1),b'\x01')
                        fragmented(struct.pack('>HH',640,1136)+bytes(16)+struct.pack('>I',4)+b'test')
                        self.assertEqual(len(exact(conn,20)),20)
                        self.assertEqual(exact(conn,8),b'\x02\x00\x00\x01'+bytes(4))
                        self.assertEqual(exact(conn,10)[1],0)
                        def update(pixel):
                            fragmented(bytes([0,0,0,1])+struct.pack('>HHHHi',0,0,1,1,0)+pixel)
                        update(bytes([0,0,255,0]))
                        self.assertEqual(exact(conn,6),bytes([5,1,2,127,4,111]))
                        self.assertEqual(exact(conn,6),bytes([5,0,2,127,4,111]))
                        self.assertEqual(exact(conn,8),bytes([4,1,0,0,0,0,255,80]))
                        self.assertEqual(exact(conn,10)[1],1)
                        update(bytes([255,0,0,0]))
                        self.assertEqual(exact(conn,10)[1],1)
                        self.assertEqual(exact(conn,8),bytes([4,0,0,0,0,0,255,80]))
                except BaseException as exc: errors.append(exc)
            thread = threading.Thread(target=serve); thread.start()
            try: subprocess.run([str(tmp/'test'),str(tmp/'vnc')],check=True,timeout=20)
            finally: thread.join(timeout=12); server.close()
            if errors: raise errors[0]

    def test_trackpad_momentum_liftoff_releases_touch(self):
        source = (ROOT / 'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r"""
let screen = Screen(frame: NSRect(x: 0, y: 0, width: 640, height: 1136))
var frames: [(String, [NSPoint])] = []
screen.gestures.send = { type, pts in frames.append((type, pts)) }

// Start a trackpad two-finger scroll
screen.gestures.scroll(NSPoint(x: 320, y: 568), dx: 0, dy: 0, begin: true, end: false, wheel: false)
assert(screen.gestures.kind == .scroll)
assert(frames.count == 1 && frames[0].0 == "begin")

// Scroll updates while fingers are on the trackpad
screen.gestures.scroll(nil, dx: 0, dy: -50, begin: false, end: false, wheel: false)
assert(frames.count == 2 && frames[1].0 == "update")

// When fingers lift and macOS transitions to momentum scrolling,
// scrollWheel receives !event.momentumPhase.isEmpty:
// Screen must immediately release the synthetic touch (kind becomes .none and 'end' is emitted)
assert(screen.gestures.kind == .scroll)
if screen.gestures.kind == .scroll {
    screen.gestures.end()
    screen.updateOverlay()
}
assert(screen.gestures.kind == .none)
assert(frames.count == 3 && frames[2].0 == "end")

// Subsequent momentum events while kind == .none are ignored and do not restart touch
if screen.gestures.kind == .scroll {
    screen.gestures.end()
}
assert(frames.count == 3)

// Also test cancelTouch releases active scroll and modifiers
screen.gestures.scroll(NSPoint(x: 320, y: 568), dx: 0, dy: 0, begin: true, end: false, wheel: false)
assert(screen.gestures.kind == .scroll)
screen.cancelTouch()
assert(screen.gestures.kind == .none)
assert(frames.last!.0 == "end")

print("PASS: momentum liftoff releases synthetic touch promptly without 1s hiccup")
"""
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='vp-momentum-') as tmp:
            tmp = Path(tmp)
            (tmp / 'main.swift').write_text(source + harness)
            subprocess.run(['swiftc', '-module-cache-path', str(MODULE_CACHE), str(tmp / 'main.swift'), '-o', str(tmp / 'test')], check=True)
            subprocess.run([str(tmp / 'test')], check=True, timeout=20)

if __name__ == '__main__': unittest.main()
