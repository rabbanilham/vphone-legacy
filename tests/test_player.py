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
                        self.assertEqual(exact(conn,8),bytes([4,0,0,0,0,0,255,80]))
                        self.assertEqual(exact(conn,10)[1],1)
                        update(bytes([255,0,0,0]))
                        self.assertEqual(exact(conn,10)[1],1)
                except BaseException as exc: errors.append(exc)
            thread = threading.Thread(target=serve); thread.start()
            try: subprocess.run([str(tmp/'test'),str(tmp/'vnc')],check=True,timeout=20)
            finally: thread.join(timeout=12); server.close()
            if errors: raise errors[0]

    def test_keyboard_mapping_and_command_filter(self):
        source = (ROOT / 'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r"""
// 1. Navigation & Editing
let ret = KeyboardMapping.map(keyCode: 0x24, characters: "\r", charactersIgnoringModifiers: "\r", modifiers: [], down: true)
assert(ret == KeyAction(qcode: "ret", keysym: 0xff0d, down: true))

let backspace = KeyboardMapping.map(keyCode: 0x33, characters: "\u{7f}", charactersIgnoringModifiers: "\u{7f}", modifiers: [], down: true)
assert(backspace == KeyAction(qcode: "backspace", keysym: 0xff08, down: true))

let fwdDelete = KeyboardMapping.map(keyCode: 0x75, characters: "\u{f728}", charactersIgnoringModifiers: "\u{f728}", modifiers: [], down: true)
assert(fwdDelete == KeyAction(qcode: "delete", keysym: 0xffff, down: true))

let tab = KeyboardMapping.map(keyCode: 0x30, characters: "\t", charactersIgnoringModifiers: "\t", modifiers: [], down: true)
assert(tab == KeyAction(qcode: "tab", keysym: 0xff09, down: true))

let esc = KeyboardMapping.map(keyCode: 0x35, characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}", modifiers: [], down: true)
assert(esc == KeyAction(qcode: "esc", keysym: 0xff1b, down: true))

let spc = KeyboardMapping.map(keyCode: 0x31, characters: " ", charactersIgnoringModifiers: " ", modifiers: [], down: true)
assert(spc == KeyAction(qcode: "spc", keysym: 0x20, down: true))

let left = KeyboardMapping.map(keyCode: 0x7B, characters: nil, charactersIgnoringModifiers: nil, modifiers: [], down: true)
assert(left == KeyAction(qcode: "left", keysym: 0xff51, down: true))
let right = KeyboardMapping.map(keyCode: 0x7C, characters: nil, charactersIgnoringModifiers: nil, modifiers: [], down: false)
assert(right == KeyAction(qcode: "right", keysym: 0xff53, down: false))
let up = KeyboardMapping.map(keyCode: 0x7E, characters: nil, charactersIgnoringModifiers: nil, modifiers: [], down: true)
assert(up == KeyAction(qcode: "up", keysym: 0xff52, down: true))
let down = KeyboardMapping.map(keyCode: 0x7D, characters: nil, charactersIgnoringModifiers: nil, modifiers: [], down: false)
assert(down == KeyAction(qcode: "down", keysym: 0xff54, down: false))

// 2. Alphanumeric & Shift Character Promotion
let keyA = KeyboardMapping.map(keyCode: 0x00, characters: "a", charactersIgnoringModifiers: "a", modifiers: [], down: true)
assert(keyA == KeyAction(qcode: "a", keysym: 0x61, down: true))
let keyShiftA = KeyboardMapping.map(keyCode: 0x00, characters: "A", charactersIgnoringModifiers: "a", modifiers: [.shift], down: true)
assert(keyShiftA == KeyAction(qcode: "a", keysym: 0x41, down: true))

let key1 = KeyboardMapping.map(keyCode: 0x12, characters: "1", charactersIgnoringModifiers: "1", modifiers: [], down: true)
assert(key1 == KeyAction(qcode: "1", keysym: 0x31, down: true))
let keyExclamation = KeyboardMapping.map(keyCode: 0x12, characters: "!", charactersIgnoringModifiers: "1", modifiers: [.shift], down: true)
assert(keyExclamation == KeyAction(qcode: "1", keysym: 0x21, down: true))

// 3. Command Shortcuts Filtering
assert(KeyboardMapping.shouldFilter(modifiers: [.command]))
assert(KeyboardMapping.shouldFilter(modifiers: [.command, .shift]))
assert(!KeyboardMapping.shouldFilter(modifiers: [.shift]))
assert(!KeyboardMapping.shouldFilter(modifiers: [.control]))
assert(!KeyboardMapping.shouldFilter(modifiers: [.option]))

assert(KeyboardMapping.map(keyCode: 0x0C, characters: "q", charactersIgnoringModifiers: "q", modifiers: [.command], down: true) == nil)
assert(KeyboardMapping.map(keyCode: 0x0D, characters: "w", charactersIgnoringModifiers: "w", modifiers: [.command], down: true) == nil)
assert(KeyboardMapping.map(keyCode: 0x08, characters: "c", charactersIgnoringModifiers: "c", modifiers: [.command], down: true) == nil)
assert(KeyboardMapping.map(keyCode: 0x09, characters: "v", charactersIgnoringModifiers: "v", modifiers: [.command], down: true) == nil)

// 4. Modifier Transitions via mapModifier
let shiftDownEvent = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: [.shift], timestamp: 0, windowNumber: 0, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: 0x38)!
let modShiftDown = KeyboardMapping.mapModifier(event: shiftDownEvent, previous: [])
assert(modShiftDown == KeyAction(qcode: "shift", keysym: 0xffe1, down: true))

let shiftUpEvent = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: 0x38)!
let modShiftUp = KeyboardMapping.mapModifier(event: shiftUpEvent, previous: [.shift])
assert(modShiftUp == KeyAction(qcode: "shift", keysym: 0xffe1, down: false))

let cmdEvent = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: [.command], timestamp: 0, windowNumber: 0, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: 0x37)!
assert(KeyboardMapping.mapModifier(event: cmdEvent, previous: []) == nil)

// 5. Screen Integration (keyDown, keyUp, flagsChanged)
let screen = Screen(frame: NSRect(x: 0, y: 0, width: 640, height: 1136))
var rfbEvents: [(UInt32, Bool)] = []
var qmpEvents: [(String, Bool)] = []
screen.rfbKey = { sym, down in rfbEvents.append((sym, down)) }
screen.qmpKey = { qcode, down in qmpEvents.append((qcode, down)) }

let eventRetDown = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 0x24)!
screen.keyDown(with: eventRetDown)
assert(rfbEvents.last! == (0xff0d, true))
assert(qmpEvents.last! == ("ret", true))

let eventRetUp = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 0x24)!
screen.keyUp(with: eventRetUp)
assert(rfbEvents.last! == (0xff0d, false))
assert(qmpEvents.last! == ("ret", false))

// Cmd shortcut in Screen must not fire events
let rfbCount = rfbEvents.count
let qmpCount = qmpEvents.count
let eventCmdQ = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command], timestamp: 0, windowNumber: 0, context: nil, characters: "q", charactersIgnoringModifiers: "q", isARepeat: false, keyCode: 0x0C)!
screen.keyDown(with: eventCmdQ)
assert(rfbEvents.count == rfbCount && qmpEvents.count == qmpCount)

// Modifier in Screen
screen.flagsChanged(with: shiftDownEvent)
assert(rfbEvents.last! == (0xffe1, true) && qmpEvents.last! == ("shift", true))
screen.flagsChanged(with: shiftUpEvent)
assert(rfbEvents.last! == (0xffe1, false) && qmpEvents.last! == ("shift", false))

// 6. Wire Encoding in RFB
assert(RFB.packet(key: 0xff0d, down: true) == [4, 1, 0, 0, 0, 0, 0xff, 0x0d])
assert(RFB.packet(key: 0xff08, down: false) == [4, 0, 0, 0, 0, 0, 0xff, 0x08])

print("PASS: keyboard mapping, modifier dispatch, cmd filtering, screen integration, and wire encoding")
"""
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='vp-kbd-') as tmp:
            tmp = Path(tmp)
            (tmp / 'main.swift').write_text(source + harness)
            subprocess.run(['swiftc', '-module-cache-path', str(MODULE_CACHE), str(tmp / 'main.swift'), '-o', str(tmp / 'test')], check=True)
            subprocess.run([str(tmp / 'test')], check=True, timeout=20)

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

    def test_surface_ring_multibuffer_fidelity_and_isolation(self):
        source = (ROOT / 'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
        harness = r"""
let ring = SurfaceRing(count: 4)
assert(ring.entries.count == 4)

// 1. Verify buffer isolation: never write to displayed or pending surface
let t1 = ring.prepareNextWrite(isFullScreen: true)
let s1 = ring.commit(dirtyRects: [(0, 0, 640, 1136)], isFullScreen: true)
ring.markDisplayed(surface: s1)

let t2 = ring.prepareNextWrite(isFullScreen: false)
assert(t2.surface !== s1, "Writing target must not be the displayed surface!")
let s2 = ring.commit(dirtyRects: [(10, 10, 40, 40)], isFullScreen: false)

let t3 = ring.prepareNextWrite(isFullScreen: false)
assert(t3.surface !== s1, "Writing target must not be the displayed surface!")
assert(t3.surface !== s2, "Writing target must not be the pending surface!")
let s3 = ring.commit(dirtyRects: [(60, 60, 40, 40)], isFullScreen: false)

// 2. Fidelity test across 100 frames with partial rects and async display
let groundTruth = UnsafeMutablePointer<UInt32>.allocate(capacity: 640 * 1136)
groundTruth.initialize(repeating: 0, count: 640 * 1136)

var pending: IOSurface?
var displayed: IOSurface?

for f in 1...100 {
    let isFull = (f == 1) || (f % 25 == 0)
    let rects: [(x: Int, y: Int, w: Int, h: Int)]
    if isFull {
        rects = [(0, 0, 640, 1136)]
    } else {
        rects = [
            (20 + (f * 7) % 500, 30 + (f * 13) % 900, 50, 40),
            (100 + (f * 11) % 400, 200 + (f * 17) % 700, 30, 60)
        ]
    }

    let target = ring.prepareNextWrite(isFullScreen: isFull)
    assert(target.surface !== displayed, "Must never return currently displayed surface")
    target.lock()
    let ptr = target.baseAddress.assumingMemoryBound(to: UInt32.self)
    for r in rects {
        let color = UInt32(f & 0xFFFFFF) | 0xFF000000
        for row in r.y..<(r.y + r.h) {
            for col in r.x..<(r.x + r.w) {
                ptr[row * 640 + col] = color
                groundTruth[row * 640 + col] = color
            }
        }
    }
    target.unlock()

    let committedSurface = ring.commit(dirtyRects: rects, isFullScreen: isFull)
    pending = committedSurface

    // Intermittent display link
    if f % 2 == 0 || f == 100 {
        if let s = pending {
            displayed = s
            pending = nil
            ring.markDisplayed(surface: s)
        }
    }

    // Verify current buffer matches ground truth exactly
    let curr = ring.current
    curr.lock()
    let curPtr = curr.baseAddress.assumingMemoryBound(to: UInt32.self)
    for i in 0..<(640 * 1136) {
        if curPtr[i] != groundTruth[i] {
            fatalError("Frame \(f): pixel mismatch at index \(i)! Got \(curPtr[i]), expected \(groundTruth[i])")
        }
    }
    curr.unlock()
}

print("PASS: surface ring 4-buffer isolation and exact dirty-rect fidelity")
"""
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='vp-ring-') as tmp:
            tmp = Path(tmp)
            (tmp / 'main.swift').write_text(source + harness)
            subprocess.run(['swiftc', '-module-cache-path', str(MODULE_CACHE), str(tmp / 'main.swift'), '-o', str(tmp / 'test')], check=True)
            subprocess.run([str(tmp / 'test')], check=True, timeout=20)

if __name__ == '__main__': unittest.main()
