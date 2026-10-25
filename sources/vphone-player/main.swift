import AppKit
import Darwin

// RFB 3.8, raw pixels only. All blocking I/O stays on the worker queue.
final class RFB {
    var fd: Int32 = -1
    let writes = NSLock()
    var frame: ((CGImage) -> Void)?
    var failed: ((String) -> Void)?
    var width = 0, height = 0
    func read(_ count: Int) throws -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: count), offset = 0
        while offset < count {
            let n = bytes.withUnsafeMutableBytes { Darwin.recv(fd, $0.baseAddress!.advanced(by: offset), count-offset, 0) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw NSError(domain: "Koneksi layar terputus", code: 1) }
            offset += n
        }
        return bytes
    }
    func send(_ bytes: [UInt8]) throws {
        writes.lock(); defer { writes.unlock() }
        var offset = 0
        while offset < bytes.count {
            let n = bytes.withUnsafeBytes { Darwin.send(fd, $0.baseAddress!.advanced(by: offset), bytes.count-offset, 0) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw NSError(domain: "Gagal mengirim input", code: 1) }
            offset += n
        }
    }
    func input(_ bytes: [UInt8]) { do { try send(bytes) } catch { failed?(error.localizedDescription) } }
    func home() {
        input([4,1,0,0,0,0,0xff,0x50])
        DispatchQueue.global().asyncAfter(deadline: .now()+0.1) { self.input([4,0,0,0,0,0,0xff,0x50]) }
    }
    func pointer(_ x: Int, _ y: Int, _ down: Bool) {
        input([5,down ? 1:0,UInt8(x>>8),UInt8(x&255),UInt8(y>>8),UInt8(y&255)])
    }
    func request(_ incremental: Bool) throws {
        try send([3,incremental ? 1:0,0,0,0,0,UInt8(width>>8),UInt8(width&255),UInt8(height>>8),UInt8(height&255)])
    }
    func run(_ path: String) {
        do {
            fd = socket(AF_UNIX, SOCK_STREAM, 0)
            guard fd >= 0 else { throw NSError(domain: "Socket gagal", code: 1) }
            defer {
                writes.lock()
                let old = fd; fd = -1
                close(old)
                writes.unlock()
            }
            var noPipe: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noPipe, socklen_t(MemoryLayout<Int32>.size))
            var addr = sockaddr_un(); addr.sun_family = sa_family_t(AF_UNIX)
            let pathBytes = Array(path.utf8CString)
            guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else { throw NSError(domain: "Socket path terlalu panjang", code: 1) }
            withUnsafeMutableBytes(of: &addr.sun_path) { raw in raw.copyBytes(from: pathBytes.map { UInt8(bitPattern:$0) }) }
            let result = withUnsafePointer(to: &addr) { p in p.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd,$0,socklen_t(MemoryLayout<sockaddr_un>.size)) } }
            guard result == 0 else { throw NSError(domain: "Tidak dapat terhubung ke QEMU", code: Int(errno)) }
            let version = try read(12)
            guard String(bytes: version, encoding:.ascii) == "RFB 003.008\n" else { throw NSError(domain:"Versi RFB tidak didukung",code:1) }
            try send(Array("RFB 003.008\n".utf8))
            let n = Int(try read(1)[0]); let security = try read(n)
            guard security.contains(1) else { throw NSError(domain:"Keamanan RFB tidak didukung",code:1) }
            try send([1]); guard try read(4) == [0,0,0,0] else { throw NSError(domain:"RFB ditolak",code:1) }
            try send([1]); let header = try read(24)
            width = Int(header[0])*256+Int(header[1]); height = Int(header[2])*256+Int(header[3])
            guard width == 640 && height == 1136 else { throw NSError(domain:"Ukuran layar tidak didukung",code:1) }
            let nameLength = header[20...23].reduce(0) { $0*256+Int($1) }
            guard nameLength <= 65536 else { throw NSError(domain:"Nama RFB terlalu panjang",code:1) }
            _ = try read(nameLength)
            // 32-bit little endian BGRX, 24-bit true color.
            try send([0,0,0,0,32,24,0,1,0,255,0,255,0,255,16,8,0,0,0,0])
            try send([2,0,0,1,0,0,0,0]); try request(false)
            var pixels = [UInt8](repeating:0,count:width*height*4)
            while true {
                let type = try read(1)[0]
                if type == 2 { continue }
                if type == 3 {
                    let h = try read(7); let size = h[3...6].reduce(0) { $0*256+Int($1) }
                    guard size <= 1048576 else { throw NSError(domain:"Clipboard terlalu besar",code:1) }
                    _ = try read(size); continue
                }
                guard type == 0 else { throw NSError(domain:"Pesan RFB tidak didukung",code:Int(type)) }
                let h = try read(3); let count = Int(h[1])*256+Int(h[2])
                for _ in 0..<count {
                    let rect = try read(12)
                    let x = Int(rect[0])*256+Int(rect[1]), y = Int(rect[2])*256+Int(rect[3])
                    let w = Int(rect[4])*256+Int(rect[5]), hh = Int(rect[6])*256+Int(rect[7])
                    guard rect[8...11].allSatisfy({$0 == 0}), x+w <= width, y+hh <= height else { throw NSError(domain:"Rectangle RFB tidak valid",code:1) }
                    let data = try read(w*hh*4)
                    for row in 0..<hh { pixels.replaceSubrange(((y+row)*width+x)*4..<((y+row)*width+x+w)*4,with:data[row*w*4..<(row+1)*w*4]) }
                }
                let provider = CGDataProvider(data: Data(pixels) as CFData)!
                if let image = CGImage(width:width,height:height,bitsPerComponent:8,bitsPerPixel:32,bytesPerRow:width*4,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGBitmapInfo(rawValue:CGImageAlphaInfo.noneSkipFirst.rawValue).union(.byteOrder32Little),provider:provider,decode:nil,shouldInterpolate:false,intent:.defaultIntent) { frame?(image) }
                try request(true)
            }
        } catch { failed?(error.localizedDescription) }
    }
}

// VNC carries one pointer; QMP carries the same MTT slots used by wt/touch.
final class QMPInput {
    let path: String
    let io = RFB()
    let queue = DispatchQueue(label:"vphone.touch.qmp")
    var connected = false
    var failed: ((String)->Void)?
    init(_ path: String) { self.path = path }
    func line() throws -> [String:Any] {
        var bytes: [UInt8] = []
        while bytes.count < 1048576 {
            let byte = try io.read(1)[0]
            if byte == 10 { return try JSONSerialization.jsonObject(with:Data(bytes)) as? [String:Any] ?? [:] }
            bytes.append(byte)
        }
        throw NSError(domain:"QMP response too large",code:1)
    }
    func execute(_ command: String, _ arguments: [String:Any] = [:]) throws {
        let id = UUID().uuidString
        let data = try JSONSerialization.data(withJSONObject:["execute":command,"arguments":arguments,"id":id])
        try io.send(Array(data)+[10])
        while true {
            let response = try line()
            if response["id"] as? String != id { continue }
            if let error = response["error"] { throw NSError(domain:"QMP: \(error)",code:1) }
            return
        }
    }
    func connect() throws {
        io.fd = socket(AF_UNIX,SOCK_STREAM,0)
        guard io.fd >= 0 else { throw NSError(domain:"QMP socket gagal",code:1) }
        var noPipe: Int32 = 1
        setsockopt(io.fd,SOL_SOCKET,SO_NOSIGPIPE,&noPipe,socklen_t(MemoryLayout<Int32>.size))
        var timeout = timeval(tv_sec:5,tv_usec:0)
        setsockopt(io.fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,socklen_t(MemoryLayout<timeval>.size))
        setsockopt(io.fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,socklen_t(MemoryLayout<timeval>.size))
        var address = sockaddr_un(); address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8CString).map { UInt8(bitPattern:$0) }
        guard bytes.count <= MemoryLayout.size(ofValue:address.sun_path) else { throw NSError(domain:"QMP path terlalu panjang",code:1) }
        withUnsafeMutableBytes(of:&address.sun_path) {$0.copyBytes(from:bytes)}
        let result = withUnsafePointer(to:&address) { p in p.withMemoryRebound(to:sockaddr.self,capacity:1) {Darwin.connect(io.fd,$0,socklen_t(MemoryLayout<sockaddr_un>.size))} }
        guard result == 0 else { throw NSError(domain:"Koneksi input QMP gagal",code:Int(errno)) }
        guard try line()["QMP"] != nil else {throw NSError(domain:"QMP greeting tidak valid",code:1)}
        try execute("qmp_capabilities"); connected = true
    }
    static func events(_ type: String, _ points: [NSPoint]) -> [[String:Any]] {
        var events: [[String:Any]] = []
        for (slot,p) in points.enumerated() {
            for (axis,value,size) in [("x",p.x,640.0),("y",p.y,1136.0)] {
                // Match Cocoa's qemu_input_queue_mtt_abs(..., 0, size).
                let coordinate = Int(max(0,min(size-1,value)))*32767/Int(size)
                events.append(["type":"mtt","data":["type":"data","slot":slot,"tracking-id":slot,"axis":axis,"value":coordinate]])
            }
            events.append(["type":"mtt","data":["type":type,"slot":slot,"tracking-id":slot,"axis":"x","value":0]])
        }
        return events
    }
    func touch(_ type: String, _ points: [NSPoint]) {
        let events = Self.events(type,points)
        queue.async {
            do {
                if !self.connected {try self.connect()}
                try self.execute("input-send-event",["device":"a7-touch-display","events":events])
            } catch {
                if self.io.fd >= 0 {close(self.io.fd);self.io.fd = -1}
                self.connected = false; self.failed?(error.localizedDescription)
            }
        }
    }
    deinit { if io.fd >= 0 {close(io.fd)} }
}

// Coordinates and gesture semantics match 0004-cocoa-trackpad-touch-gestures.patch.
final class TouchGestures {
    enum Kind { case none, scroll, wheel, pinch, magnify, controlCenter }
    var kind: Kind = .none
    var fingers: [NSPoint] = []
    var center: NSPoint?
    var scale: CGFloat = 1
    var magnifyCenter = NSPoint.zero
    var send: ((String,[NSPoint])->Void)?
    var overlay: [NSPoint] = []
    func clamp(_ p:NSPoint)->NSPoint { NSPoint(x:max(0,min(639,p.x)),y:max(0,min(1135,p.y))) }
    func emit(_ type:String) {
        fingers = fingers.map(clamp);send?(type,fingers)
        overlay = fingers.count == 2 ? fingers : []
        if type == "end" || type == "cancel" {kind = .none;fingers = [];overlay = []}
    }
    func end() { if kind != .none {emit("end")} }
    func hint(_ point:NSPoint?, option:Bool) {
        guard kind == .none else {return}
        if !option {center = nil;overlay = [];return}
        guard let point else {overlay = [];return}
        if center == nil {center = NSPoint(x:320,y:568)}
        overlay = [clamp(point),clamp(NSPoint(x:2*center!.x-point.x,y:2*center!.y-point.y))]
    }
    func pinch(_ point:NSPoint, begin:Bool, pan:Bool = false) {
        if begin {
            guard kind == .none else {return}
            kind = .pinch
            if center == nil {center = NSPoint(x:320,y:568)}
        } else {
            guard kind == .pinch else {return}
            if pan {center!.x += point.x-fingers[0].x;center!.y += point.y-fingers[0].y}
        }
        fingers = [point,NSPoint(x:2*center!.x-point.x,y:2*center!.y-point.y)]
        emit(begin ? "begin":"update")
    }
    func scroll(_ point:NSPoint?, dx:CGFloat, dy:CGFloat, begin:Bool, end:Bool, wheel:Bool) {
        let expected:Kind = wheel ? .wheel:.scroll
        if begin {
            guard kind == .none,let point else {return}
            kind = expected;fingers = [point];emit("begin")
        }
        guard kind == expected else {return}
        if dx != 0 || dy != 0 {fingers[0].x += dx;fingers[0].y += dy;emit("update")}
        if end {emit("end")}
    }
    @discardableResult func beginControlCenter() -> Bool {
        guard kind == .none else {return false}
        kind = .controlCenter
        // The board adds the existing 16px Y correction once. This lands
        // within the bottom edge without emitting an out-of-range contact.
        fingers = [NSPoint(x:320,y:1118)];emit("begin");return true
    }
    func advanceControlCenter(_ progress:CGFloat) {
        guard kind == .controlCenter else {return}
        fingers = [NSPoint(x:320,y:1118-698*max(0,min(1,progress)))];emit("update")
    }
    func magnify(_ point:NSPoint?, delta:CGFloat, begin:Bool, end:Bool) {
        if begin {
            guard kind == .none,let point else {return}
            kind = .magnify;magnifyCenter = point;scale = 1
        }
        guard kind == .magnify else {return}
        scale = max(0.1,scale+delta)
        let radius = min(80*scale,1136)
        fingers = [NSPoint(x:magnifyCenter.x-radius,y:magnifyCenter.y),NSPoint(x:magnifyCenter.x+radius,y:magnifyCenter.y)]
        emit(end ? "end":(begin ? "begin":"update"))
    }
}

enum ScreenGeometry {
    static func rect(_ bounds: NSRect) -> NSRect {
        let scale = min(bounds.width/640,bounds.height/1136)
        return NSRect(x:(bounds.width-640*scale)/2,y:(bounds.height-1136*scale)/2,width:640*scale,height:1136*scale)
    }
    static func point(_ point: NSPoint, in rect: NSRect) -> (Int,Int) {
        (max(0,min(639,Int((point.x-rect.minX)/rect.width*640))),
         max(0,min(1135,Int((point.y-rect.minY)/rect.height*1136))))
    }
}

final class Screen: NSView {
    var image: CGImage? { didSet { needsDisplay = true } }
    var input: ((Int,Int,Bool)->Void)?
    let gestures = TouchGestures()
    var wheelTimer: Timer?
    var controlCenterTimer: Timer?
    var tracking: NSTrackingArea?
    var held = false
    var lastPoint = (0,0)
    func cancelTouch() {
        wheelTimer?.invalidate();wheelTimer = nil
        controlCenterTimer?.invalidate();controlCenterTimer = nil
        gestures.end();gestures.hint(nil,option:false);needsDisplay = true
        if held { held = false; input?(lastPoint.0,lastPoint.1,false) }
    }
    var displayRect: NSRect {
        ScreenGeometry.rect(bounds)
    }
    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool {true}
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking {removeTrackingArea(tracking)}
        let area = NSTrackingArea(rect:.zero,options:[.mouseMoved,.mouseEnteredAndExited,.activeInKeyWindow,.inVisibleRect],owner:self,userInfo:nil)
        tracking = area;addTrackingArea(area)
    }
    // The launcher activates asynchronously. The activating click must also
    // start the guest contact, otherwise the entire first drag is discarded.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill(); bounds.fill()
        if let image { NSImage(cgImage:image,size:NSSize(width:640,height:1136)).draw(in:displayRect,from:.zero,operation:.copy,fraction:1,respectFlipped:true,hints:nil) }
        let r = displayRect, diameter = 56*r.width/640
        for p in gestures.overlay {
            let dot = NSRect(x:r.minX+p.x*r.width/640-diameter/2,y:r.minY+p.y*r.height/1136-diameter/2,width:diameter,height:diameter)
            NSColor.gray.withAlphaComponent(0.45).setFill();NSBezierPath(ovalIn:dot).fill()
            NSColor.white.withAlphaComponent(0.85).setStroke();let path = NSBezierPath(ovalIn:dot);path.lineWidth = 1;path.stroke()
        }
    }
    func guestPoint(_ event:NSEvent)->NSPoint? {
        let p = convert(event.locationInWindow,from:nil), r = displayRect
        guard r.width > 0,r.contains(p) else {return nil}
        return NSPoint(x:(p.x-r.minX)/r.width*640,y:(p.y-r.minY)/r.height*1136)
    }
    func pointer(_ event: NSEvent, _ down: Bool) {
        let point = convert(event.locationInWindow,from:nil), r = displayRect
        guard r.width > 0 else { return }
        if down && !held && !r.contains(point) { return }
        held = down
        let (x,y) = ScreenGeometry.point(point,in:r)
        lastPoint = (x,y)
        input?(x,y,down)
    }
    func openControlCenter() {
        guard !held,gestures.beginControlCenter() else {return}
        var tick = 0
        // Hold the edge for 0.2s, drag for 3s, settle for 1s, then lift.
        // The slower pacing matches the existing emulated UIKit touch path.
        let timer = Timer(timeInterval:1.0/30,repeats:true) {[weak self] timer in
            guard let self,self.gestures.kind == .controlCenter else {timer.invalidate();return}
            tick += 1
            if tick >= 6 && tick <= 96 {self.gestures.advanceControlCenter(CGFloat(tick-6)/90)}
            if tick >= 126 {self.gestures.end();timer.invalidate();self.controlCenterTimer = nil}
        }
        controlCenterTimer = timer;RunLoop.main.add(timer,forMode:.common)
    }
    override func mouseDown(with event:NSEvent) {
        window?.makeFirstResponder(self)
        guard gestures.kind == .none else {return}
        if event.modifierFlags.contains(.option),let point = guestPoint(event) {
            gestures.pinch(point,begin:true);needsDisplay = true
        } else {pointer(event,true)}
    }
    override func mouseDragged(with event:NSEvent) {
        if gestures.kind == .pinch {
            let p = convert(event.locationInWindow,from:nil),r = displayRect
            let point = gestures.clamp(NSPoint(x:(p.x-r.minX)/r.width*640,y:(p.y-r.minY)/r.height*1136))
            gestures.pinch(point,begin:false,pan:event.modifierFlags.contains(.shift));needsDisplay = true
        } else if held {pointer(event,true)}
    }
    override func mouseUp(with event:NSEvent) {
        if gestures.kind == .pinch {gestures.end();gestures.hint(guestPoint(event),option:event.modifierFlags.contains(.option));needsDisplay = true}
        else if held {pointer(event,false)}
    }
    override func flagsChanged(with event:NSEvent) {
        let p = window.map {convert($0.mouseLocationOutsideOfEventStream,from:nil)}
        let point = p.flatMap {displayRect.contains($0) ? NSPoint(x:($0.x-displayRect.minX)/displayRect.width*640,y:($0.y-displayRect.minY)/displayRect.height*1136):nil}
        gestures.hint(point,option:event.modifierFlags.contains(.option));needsDisplay = true
    }
    override func mouseMoved(with event:NSEvent) {gestures.hint(guestPoint(event),option:event.modifierFlags.contains(.option));needsDisplay = true}
    override func mouseExited(with event:NSEvent) {gestures.hint(nil,option:false);needsDisplay = true}
    override func scrollWheel(with event:NSEvent) {
        guard !held,event.momentumPhase.isEmpty else {return}
        let wheel = event.phase.isEmpty
        let scale = event.hasPreciseScrollingDeltas ? 640/displayRect.width:40
        let moves = wheel || event.phase.contains(.changed)
        gestures.scroll(guestPoint(event),dx:moves ? event.scrollingDeltaX*scale:0,dy:moves ? event.scrollingDeltaY*scale:0,
            begin:wheel ? gestures.kind == .none:event.phase.contains(.began),
            end:event.phase.contains(.ended) || event.phase.contains(.cancelled),wheel:wheel)
        if wheel,gestures.kind == .wheel {
            wheelTimer?.invalidate()
            wheelTimer = Timer.scheduledTimer(withTimeInterval:0.15,repeats:false) {[weak self] _ in self?.gestures.end();self?.needsDisplay = true}
        }
        needsDisplay = true
    }
    override func magnify(with event:NSEvent) {
        guard !held else {return}
        gestures.magnify(guestPoint(event),delta:event.magnification,begin:event.phase.contains(.began),end:event.phase.contains(.ended) || event.phase.contains(.cancelled))
        needsDisplay = true
    }
}

final class Player: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let rfb = RFB(), screen = Screen(), status = NSTextField(labelWithString:"Menunggu layar QEMU…")
    var touchInput: QMPInput?
    var activity: NSObjectProtocol?
    var inputFailed = false
    var window: NSWindow!
    var buttons: [NSButton] = []
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle:"Quit vphone",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q")
        let windowItem = NSMenuItem(); menu.addItem(windowItem)
        let windowMenu = NSMenu(title:"Window"); windowItem.submenu = windowMenu
        windowMenu.addItem(withTitle:"Close",action:#selector(NSWindow.performClose(_:)),keyEquivalent:"w")
        NSApp.mainMenu = menu
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:360,height:690),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title = "vphone — iOS 7.1.2"; window.minSize = NSSize(width:260,height:440); window.delegate = self
        let root = NSView(); window.contentView = root
        let bar = NSStackView(); bar.orientation = .horizontal; bar.spacing = 16
        for (icon,title,action) in [("house","Home",#selector(home)),("slider.horizontal.3","Control Center",#selector(controlCenter)),("camera","Screen Capture",#selector(capture))] {
            let button = NSButton(image:NSImage(systemSymbolName:icon,accessibilityDescription:title)!,target:self,action:action)
            button.bezelStyle = .texturedRounded; button.toolTip = title; button.setAccessibilityLabel(title); button.isEnabled = false
            buttons.append(button); bar.addArrangedSubview(button)
        }
        for view in [screen,bar,status] { view.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(view) }
        status.textColor = .secondaryLabelColor; status.alignment = .center; status.maximumNumberOfLines = 2
        NSLayoutConstraint.activate([
            bar.topAnchor.constraint(equalTo:root.topAnchor,constant:10),bar.centerXAnchor.constraint(equalTo:root.centerXAnchor),bar.heightAnchor.constraint(equalToConstant:32),
            screen.topAnchor.constraint(equalTo:bar.bottomAnchor,constant:10),screen.leadingAnchor.constraint(equalTo:root.leadingAnchor),screen.trailingAnchor.constraint(equalTo:root.trailingAnchor),screen.bottomAnchor.constraint(equalTo:root.bottomAnchor),
            status.centerXAnchor.constraint(equalTo:screen.centerXAnchor),status.centerYAnchor.constraint(equalTo:screen.centerYAnchor),status.widthAnchor.constraint(lessThanOrEqualTo:screen.widthAnchor,constant:-24)])
        activity = ProcessInfo.processInfo.beginActivity(options:[.userInitiatedAllowingIdleSystemSleep,.latencyCritical],reason:"Interactive emulator input")
        let qmp = QMPInput(CommandLine.arguments[2]);touchInput = qmp
        screen.gestures.send = {[weak qmp] type,points in qmp?.touch(type,points)}
        qmp.failed = {[weak self] message in DispatchQueue.main.async {
            guard let self else {return};self.inputFailed = true;self.status.stringValue = "Input gagal: \(message)";self.status.isHidden = false
            self.screen.cancelTouch();self.screen.input = nil;self.screen.gestures.send = nil
        }}
        screen.input = { [weak self] x,y,down in self?.rfb.pointer(x,y,down) }
        rfb.frame = { [weak self] image in DispatchQueue.main.async { guard let self else {return}; self.screen.image = image; if !self.inputFailed {self.status.isHidden = true}; self.buttons.forEach {$0.isEnabled = !self.inputFailed} } }
        rfb.failed = { [weak self] message in DispatchQueue.main.async { guard let self else {return}; self.status.stringValue = "QEMU berhenti / koneksi gagal: \(message)"; self.status.isHidden = false; self.buttons.forEach {$0.isEnabled = false}; self.screen.input = nil } }
        window.center(); window.makeKeyAndOrderFront(nil);window.makeFirstResponder(screen); NSApp.activate(ignoringOtherApps:true)
        let path = CommandLine.arguments[1]
        DispatchQueue.global(qos:.userInteractive).async { self.rfb.run(path) }
    }
    @objc func home() { screen.cancelTouch();rfb.home() }
    @objc func controlCenter() { screen.openControlCenter() }
    @objc func capture() {
        guard let image = screen.image else {return}
        let png = NSBitmapImageRep(cgImage:image).representation(using:.png,properties:[:])
        let panel = NSSavePanel(); panel.allowedContentTypes = [.png]; panel.nameFieldStringValue = "vphone-\(Int(Date().timeIntervalSince1970)).png"
        panel.beginSheetModal(for:window) { result in
            guard result == .OK, let url = panel.url, let png else {return}
            do {try png.write(to:url,options:.atomic)} catch {NSAlert(error:error).runModal()}
        }
    }
    func windowDidResignKey(_ notification: Notification) { screen.cancelTouch() }
    func windowWillClose(_ notification:Notification) {
        screen.cancelTouch()
        if let activity {ProcessInfo.processInfo.endActivity(activity)}
        NSApp.terminate(nil)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {true}
}
guard CommandLine.arguments.count == 3 else {
    fputs("Usage: vphone-player VNC_UNIX_SOCKET QMP_UNIX_SOCKET\n", stderr); exit(2)
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let player = Player(); app.delegate = player
app.run()
