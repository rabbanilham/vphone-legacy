import AppKit
import Darwin
import IOSurface
import QuartzCore

// Persistent pixel buffer backed by IOSurface for zero-copy GPU presentation.
final class PixelBuffer {
    let width = 640
    let height = 1136
    let bytesPerRow: Int
    let surface: IOSurface
    let baseAddress: UnsafeMutableRawPointer

    init() {
        self.bytesPerRow = width * 4
        let props: [CFString: Any] = [
            kIOSurfaceWidth: width,
            kIOSurfaceHeight: height,
            kIOSurfaceBytesPerElement: 4,
            kIOSurfaceBytesPerRow: width * 4,
            kIOSurfaceAllocSize: width * height * 4,
            kIOSurfacePixelFormat: 0x42475241 // 'BGRA'
        ]
        self.surface = IOSurfaceCreate(props as CFDictionary)!
        IOSurfaceLock(surface, [], nil)
        self.baseAddress = IOSurfaceGetBaseAddress(surface)
        memset(baseAddress, 0, width * height * 4)
        IOSurfaceUnlock(surface, [], nil)
    }

    func lock() {
        IOSurfaceLock(surface, [], nil)
    }

    func unlock() {
        IOSurfaceUnlock(surface, [], nil)
    }

    func copy(into other: PixelBuffer) {
        other.lock()
        memcpy(other.baseAddress, baseAddress, bytesPerRow * height)
        other.unlock()
    }

    func makeImage() -> CGImage? {
        lock()
        defer { unlock() }
        let ctx = CGContext(data: baseAddress,
                            width: width,
                            height: height,
                            bitsPerComponent: 8,
                            bytesPerRow: bytesPerRow,
                            space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        return ctx?.makeImage()
    }
}

// CALayer.contents only recomposites when it is given a different object, so
// two surfaces are used in a ping-pong swapchain. Incoming rectangles are
// received directly into the back buffer without a 2.9MB full-frame memcpy.
final class SurfaceRing {
    let buffers = [PixelBuffer(), PixelBuffer()]
    private var writeIndex = 0
    private var lastDirtyRects: [(x: Int, y: Int, w: Int, h: Int)] = []

    var current: PixelBuffer {
        buffers[writeIndex]
    }

    func prepareNextWrite(isFullScreen: Bool) -> PixelBuffer {
        let nextIndex = 1 - writeIndex
        let target = buffers[nextIndex]
        let prev = buffers[writeIndex]

        if !isFullScreen && !lastDirtyRects.isEmpty {
            target.lock()
            prev.lock()
            for r in lastDirtyRects {
                if r.w == prev.width && r.x == 0 {
                    memcpy(target.baseAddress.advanced(by: r.y * prev.bytesPerRow),
                           prev.baseAddress.advanced(by: r.y * prev.bytesPerRow),
                           r.w * 4 * r.h)
                } else {
                    for row in 0..<r.h {
                        let offset = (r.y + row) * prev.bytesPerRow + r.x * 4
                        memcpy(target.baseAddress.advanced(by: offset),
                               prev.baseAddress.advanced(by: offset),
                               r.w * 4)
                    }
                }
            }
            prev.unlock()
            target.unlock()
        }
        return target
    }

    func commit(dirtyRects: [(x: Int, y: Int, w: Int, h: Int)]) -> IOSurface {
        lastDirtyRects = dirtyRects
        writeIndex = 1 - writeIndex
        return buffers[writeIndex].surface
    }
}

// RFB 3.8, raw pixels with persistent pixel buffer. All blocking I/O stays on the worker queue.
final class RFB {
    var fd: Int32 = -1
    let writes = NSLock()
    var frame: ((CGImage) -> Void)?
    var surfaceFrame: ((IOSurface) -> Void)?
    var trace: ((String) -> Void)?
    var failed: ((String) -> Void)?
    var presentStats: (() -> String)?
    var width = 0, height = 0
    let ring = SurfaceRing()
    var pixelBuffer: PixelBuffer { ring.current }
    private var scratch = [UInt8](repeating: 0, count: 65536)

    func readIntoScratch(_ count: Int) throws {
        if count > scratch.count {
            scratch = [UInt8](repeating: 0, count: max(count, scratch.count * 2))
        }
        var offset = 0
        while offset < count {
            let n = scratch.withUnsafeMutableBytes {
                Darwin.recv(fd, $0.baseAddress!.advanced(by: offset), count - offset, 0)
            }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw NSError(domain: "Koneksi layar terputus", code: 1) }
            offset += n
        }
    }

    func read(_ count: Int) throws -> [UInt8] {
        try readIntoScratch(count)
        return Array(scratch[0..<count])
    }

    func read(into ptr: UnsafeMutableRawPointer, count: Int) throws {
        var offset = 0
        while offset < count {
            let n = Darwin.recv(fd, ptr.advanced(by: offset), count - offset, 0)
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw NSError(domain: "Koneksi layar terputus", code: 1) }
            offset += n
        }
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
    static func packet(key sym: UInt32, down: Bool) -> [UInt8] {
        [4, down ? 1 : 0, 0, 0,
         UInt8((sym >> 24) & 0xff),
         UInt8((sym >> 16) & 0xff),
         UInt8((sym >> 8) & 0xff),
         UInt8(sym & 0xff)]
    }
    func key(_ sym: UInt32, _ down: Bool) {
        input(Self.packet(key: sym, down: down))
    }
    func home() {
        // The device owns the 100 ms guest-time pulse and lost-key-up guard.
        // A host timer only delays key-up and can slip under host load.
        key(0xff50, true)
        key(0xff50, false)
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
            let statsEnabled = ProcessInfo.processInfo.environment["VPHONE_PLAYER_STATS"] != nil
            var statsLastTime = Date().timeIntervalSince1970
            var statsFrames = 0
            var statsRects = 0
            var statsBytes = 0
            var totalFrames = 0
            while true {
                try readIntoScratch(1)
                let type = scratch[0]
                if type == 2 { continue }
                if type == 3 {
                    try readIntoScratch(7)
                    let size = scratch[3...6].reduce(0) { $0*256+Int($1) }
                    guard size <= 1048576 else { throw NSError(domain:"Clipboard terlalu besar",code:1) }
                    try readIntoScratch(size); continue
                }
                guard type == 0 else { throw NSError(domain:"Pesan RFB tidak didukung",code:Int(type)) }
                try readIntoScratch(3); let count = Int(scratch[1])*256+Int(scratch[2])
                if count > 0 {
                    var rectHeaders: [(x: Int, y: Int, w: Int, hh: Int)] = []
                    rectHeaders.reserveCapacity(count)
                    var frameBytes = 0
                    var target: PixelBuffer?
                    // RFB interleaves each rectangle header with its pixel data,
                    // so read them in order; the back buffer is chosen at the
                    // first header (a lone full-screen rect needs no carry-over).
                    for _ in 0..<count {
                        try readIntoScratch(12)
                        let x = Int(scratch[0])*256+Int(scratch[1]), y = Int(scratch[2])*256+Int(scratch[3])
                        let w = Int(scratch[4])*256+Int(scratch[5]), hh = Int(scratch[6])*256+Int(scratch[7])
                        guard scratch[8...11].allSatisfy({$0 == 0}), x+w <= width, y+hh <= height else {
                            target?.unlock()
                            throw NSError(domain:"Rectangle RFB tidak valid",code:1)
                        }
                        if target == nil {
                            let isFullScreen = count == 1 && x == 0 && y == 0 && w == width && hh == height
                            let t = ring.prepareNextWrite(isFullScreen: isFullScreen)
                            t.lock()
                            target = t
                        }
                        let t = target!
                        frameBytes += w*hh*4
                        rectHeaders.append((x: x, y: y, w: w, hh: hh))
                        if w == width && x == 0 {
                            try read(into: t.baseAddress.advanced(by: y * t.bytesPerRow), count: width * hh * 4)
                        } else {
                            for row in 0..<hh {
                                let dst = t.baseAddress.advanced(by: (y + row) * t.bytesPerRow + x * 4)
                                try read(into: dst, count: w * 4)
                            }
                        }
                    }
                    target?.unlock()

                    let surface = ring.commit(dirtyRects: rectHeaders.map { (x: $0.x, y: $0.y, w: $0.w, h: $0.hh) })

                    totalFrames += 1
                    if statsEnabled {
                        statsFrames += 1
                        statsRects += count
                        statsBytes += frameBytes
                        let now = Date().timeIntervalSince1970
                        if now - statsLastTime >= 1.0 {
                            let dt = now - statsLastTime
                            let fps = Double(statsFrames) / dt
                            let rps = Double(statsRects) / dt
                            let kbps = Double(statsBytes) / dt / 1024.0
                            let presentStr = presentStats?() ?? ""
                            let msg = "[player-stats] " + String(format: "FPS: %.1f, rects/s: %.1f, kB/s: %.1f (total frames: %d)",
                                                                 fps, rps, kbps, totalFrames) + presentStr
                            fputs("\(msg)\n", stderr)
                            trace?(msg)
                            statsFrames = 0
                            statsRects = 0
                            statsBytes = 0
                            statsLastTime = now
                        }
                    }

                    if let surfaceHandler = surfaceFrame {
                        surfaceHandler(surface)
                    } else if let frameHandler = frame {
                        if let img = ring.current.makeImage() {
                            frameHandler(img)
                        }
                    }
                }
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
    @discardableResult func execute(_ command: String, _ arguments: [String:Any] = [:]) throws -> Any? {
        let id = UUID().uuidString
        let data = try JSONSerialization.data(withJSONObject:["execute":command,"arguments":arguments,"id":id])
        try io.send(Array(data)+[10])
        while true {
            let response = try line()
            if response["id"] as? String != id { continue }
            if let error = response["error"] { throw NSError(domain:"QMP: \(error)",code:1) }
            return response["return"]
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
    func reset() {
        if io.fd >= 0 {close(io.fd);io.fd = -1}
        connected = false
    }
    func sendKey(_ qcode: String, _ down: Bool) {
        queue.async {
            do {
                if !self.connected { try self.connect() }
                try self.execute("input-send-event", [
                    "device": "a7-touch-display",
                    "events": [
                        ["type": "key", "data": ["down": down, "key": ["type": "qcode", "data": qcode]]]
                    ]
                ])
            } catch {
                if self.io.fd >= 0 { close(self.io.fd); self.io.fd = -1 }
                self.connected = false
                self.failed?(error.localizedDescription)
            }
        }
    }
    // Sleep/wake button: a click locks an awake device or wakes a sleeping one.
    func pressPower(_ completion: @escaping (String?)->Void) {
        queue.async {
            do {
                if !self.connected {try self.connect()}
                for down in [true,false] {
                    try self.execute("input-send-event",["device":"a7-touch-display","events":[["type":"key","data":["down":down,"key":["type":"qcode","data":"power"]]]]])
                }
                completion(nil)
            } catch {self.reset();completion(error.localizedDescription)}
        }
    }
    // nil when the backend cannot say (older build or QMP down).
    func displayOn(_ completion: @escaping (Bool?)->Void) {
        queue.async {
            do {
                if !self.connected {try self.connect()}
                completion(try self.execute("qom-get",["path":"/machine","property":"display-on"]) as? Bool)
            } catch {self.reset();completion(nil)}
        }
    }
    func prepareControlCenter(_ completion: @escaping (String?)->Void) {
        queue.async {
            do {
                if !self.connected {try self.connect()}
                var woke = false
                for _ in 0..<100 {
                    guard let ready = try self.execute("qom-get",["path":"/machine","property":"touch-reporting-enabled"]) as? Bool else {
                        throw NSError(domain:"Backend perlu dibangun ulang untuk memeriksa digitizer",code:1)
                    }
                    if ready {completion(nil);return}
                    if !woke {
                        // Only wake a gated digitizer. Do not leave an active app.
                        for down in [true,false] {
                            try self.execute("input-send-event",["device":"a7-touch-display","events":[["type":"key","data":["down":down,"key":["type":"qcode","data":"home"]]]]])
                        }
                        woke = true
                    }
                    usleep(100000)
                }
                completion("Digitizer belum siap. Tunggu boot selesai lalu coba lagi.")
            } catch {
                if self.io.fd >= 0 {close(self.io.fd);self.io.fd = -1}
                self.connected = false;completion(error.localizedDescription)
            }
        }
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
        fingers = [NSPoint(x:320,y:1135)];emit("begin");return true
    }
    func advanceControlCenter(_ progress:CGFloat) {
        guard kind == .controlCenter else {return}
        fingers = [NSPoint(x:320,y:1135-1035*max(0,min(1,progress)))];emit("update")
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

struct KeyAction: Equatable {
    let qcode: String
    let keysym: UInt32
    let down: Bool
}

enum KeyboardMapping {
    static let table: [UInt16: (String, UInt32)] = [
        // Letters
        0x00: ("a", 0x61), 0x0B: ("b", 0x62), 0x08: ("c", 0x63), 0x02: ("d", 0x64),
        0x0E: ("e", 0x65), 0x03: ("f", 0x66), 0x05: ("g", 0x67), 0x04: ("h", 0x68),
        0x22: ("i", 0x69), 0x26: ("j", 0x6a), 0x28: ("k", 0x6b), 0x25: ("l", 0x6c),
        0x2E: ("m", 0x6d), 0x2D: ("n", 0x6e), 0x1F: ("o", 0x6f), 0x23: ("p", 0x70),
        0x0C: ("q", 0x71), 0x0F: ("r", 0x72), 0x01: ("s", 0x73), 0x11: ("t", 0x74),
        0x20: ("u", 0x75), 0x09: ("v", 0x76), 0x0D: ("w", 0x77), 0x07: ("x", 0x78),
        0x10: ("y", 0x79), 0x06: ("z", 0x7a),

        // Numbers
        0x1D: ("0", 0x30), 0x12: ("1", 0x31), 0x13: ("2", 0x32), 0x14: ("3", 0x33),
        0x15: ("4", 0x34), 0x17: ("5", 0x35), 0x16: ("6", 0x36), 0x1A: ("7", 0x37),
        0x1C: ("8", 0x38), 0x19: ("9", 0x39),

        // Punctuation & symbols
        0x1B: ("minus", 0x2d), 0x18: ("equal", 0x3d),
        0x21: ("bracket_left", 0x5b), 0x1E: ("bracket_right", 0x5d),
        0x2A: ("backslash", 0x5c), 0x29: ("semicolon", 0x3b),
        0x27: ("apostrophe", 0x27), 0x32: ("grave_accent", 0x60),
        0x2B: ("comma", 0x2c), 0x2F: ("dot", 0x2e), 0x2C: ("slash", 0x2f),

        // Controls & Whitespace
        0x24: ("ret", 0xff0d), 0x30: ("tab", 0xff09), 0x31: ("spc", 0x20),
        0x33: ("backspace", 0xff08), 0x75: ("delete", 0xffff),
        0x35: ("esc", 0xff1b),

        // Navigation
        0x7B: ("left", 0xff51), 0x7C: ("right", 0xff53),
        0x7E: ("up", 0xff52), 0x7D: ("down", 0xff54),
        0x73: ("home", 0xff50), 0x77: ("end", 0xff57),
        0x74: ("pgup", 0xff55), 0x79: ("pgdn", 0xff56),

        // Modifiers
        0x38: ("shift", 0xffe1), 0x3C: ("shift_r", 0xffe2),
        0x3B: ("ctrl", 0xffe3), 0x3E: ("ctrl_r", 0xffe4),
        0x3A: ("alt", 0xffe9), 0x3D: ("alt_r", 0xfe03),
        0x39: ("caps_lock", 0xffe5),

        // Keypad
        0x41: ("kp_decimal", 0xffae), 0x43: ("kp_multiply", 0xffaa),
        0x45: ("kp_add", 0xffab), 0x4B: ("kp_divide", 0xffaf),
        0x4C: ("kp_enter", 0xff8d), 0x4E: ("kp_subtract", 0xffad),
        0x51: ("kp_equals", 0xffbd),
        0x52: ("kp_0", 0xffb0), 0x53: ("kp_1", 0xffb1), 0x54: ("kp_2", 0xffb2),
        0x55: ("kp_3", 0xffb3), 0x56: ("kp_4", 0xffb4), 0x57: ("kp_5", 0xffb5),
        0x58: ("kp_6", 0xffb6), 0x59: ("kp_7", 0xffb7), 0x5B: ("kp_8", 0xffb8),
        0x5C: ("kp_9", 0xffb9),

        // Function keys
        0x7A: ("f1", 0xffbe), 0x78: ("f2", 0xffbf), 0x63: ("f3", 0xffc0),
        0x76: ("f4", 0xffc1), 0x60: ("f5", 0xffc2), 0x61: ("f6", 0xffc3),
        0x62: ("f7", 0xffc4), 0x64: ("f8", 0xffc5), 0x65: ("f9", 0xffc6),
        0x6D: ("f10", 0xffc7), 0x67: ("f11", 0xffc8), 0x6F: ("f12", 0xffc9)
    ]

    static func shouldFilter(modifiers: NSEvent.ModifierFlags) -> Bool {
        modifiers.contains(.command)
    }

    static func map(keyCode: UInt16,
                    characters: String?,
                    charactersIgnoringModifiers: String?,
                    modifiers: NSEvent.ModifierFlags,
                    down: Bool) -> KeyAction? {
        if shouldFilter(modifiers: modifiers) {
            return nil
        }
        guard let (qcode, defaultSym) = table[keyCode] else {
            return nil
        }
        var keysym = defaultSym
        if defaultSym < 0xff00, defaultSym != 0x20, let chars = characters, let first = chars.unicodeScalars.first {
            let val = first.value
            if (val >= 0x20 && val <= 0x7e) || (val >= 0xa0 && val <= 0x10ffff) {
                keysym = val
            }
        }
        return KeyAction(qcode: qcode, keysym: keysym, down: down)
    }

    static func map(_ event: NSEvent, down: Bool) -> KeyAction? {
        map(keyCode: event.keyCode,
            characters: event.characters,
            charactersIgnoringModifiers: event.charactersIgnoringModifiers,
            modifiers: event.modifierFlags,
            down: down)
    }

    static func mapModifier(event: NSEvent, previous: NSEvent.ModifierFlags) -> KeyAction? {
        let code = event.keyCode
        let flags = event.modifierFlags
        let flag: NSEvent.ModifierFlags
        switch code {
        case 0x38, 0x3C: flag = .shift
        case 0x3B, 0x3E: flag = .control
        case 0x3A, 0x3D: flag = .option
        case 0x39: flag = .capsLock
        default: return nil
        }
        let isDown = flags.contains(flag)
        let wasDown = previous.contains(flag)
        guard isDown != wasDown, let (qcode, sym) = table[code] else { return nil }
        return KeyAction(qcode: qcode, keysym: sym, down: isDown)
    }
}

// Shown over the screen while the guest has its display off (sleep/locked).
final class SleepOverlay: NSView {
    var onWake: (() -> Void)?
    private let icon = NSImageView()
    private let label = NSTextField(labelWithString:"Perangkat terkunci")
    private let hint = NSTextField(labelWithString:"Klik untuk membangunkan")
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        icon.image = NSImage(systemSymbolName:"lock.fill",accessibilityDescription:"Terkunci")
        icon.symbolConfiguration = .init(pointSize:34,weight:.regular)
        icon.contentTintColor = NSColor(white:0.75,alpha:1)
        label.font = .systemFont(ofSize:15,weight:.semibold);label.textColor = NSColor(white:0.85,alpha:1)
        hint.font = .systemFont(ofSize:12);hint.textColor = NSColor(white:0.55,alpha:1)
        let stack = NSStackView(views:[icon,label,hint]);stack.orientation = .vertical;stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false;addSubview(stack)
        NSLayoutConstraint.activate([stack.centerXAnchor.constraint(equalTo:centerXAnchor),stack.centerYAnchor.constraint(equalTo:centerYAnchor)])
        setAccessibilityLabel("Perangkat terkunci. Klik untuk membangunkan.")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }
    override func mouseDown(with event: NSEvent) { onWake?() }
}

final class Screen: NSView {
    private var cachedImage: CGImage?
    var pixelBuffer: PixelBuffer?
    var pixelBufferProvider: (() -> PixelBuffer?)?
    var image: CGImage? {
        get {
            if let pb = pixelBufferProvider?() ?? pixelBuffer { return pb.makeImage() }
            return cachedImage
        }
        set {
            cachedImage = newValue
            if let newValue {
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                contentLayer.contents = newValue
                CATransaction.commit()
            }
        }
    }
    let contentLayer = CALayer()
    let overlayLayer = CALayer()
    let sleepOverlay = SleepOverlay()
    private(set) var sleeping = false
    var input: ((Int,Int,Bool)->Void)?
    var rfbKey: ((UInt32,Bool)->Void)?
    var qmpKey: ((String,Bool)->Void)?
    private var currentModifiers: NSEvent.ModifierFlags = []
    let gestures = TouchGestures()
    var wheelTimer: Timer?
    var controlCenterTimer: Timer?
    var tracking: NSTrackingArea?
    var touchEpoch = 0
    var held = false
    var lastPoint = (0,0)
    var trace: ((String)->Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        setupLayers()
    }
    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupLayers()
    }
    private func setupLayers() {
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        contentLayer.contentsGravity = .resizeAspect
        // Smooth scaling like the old NSImage.draw path; nearest made the
        // upscaled screen look blocky at non-integer window sizes.
        contentLayer.magnificationFilter = .linear
        contentLayer.minificationFilter = .trilinear
        layer?.addSublayer(contentLayer)
        layer?.addSublayer(overlayLayer)
        sleepOverlay.isHidden = true
        addSubview(sleepOverlay)
    }
    // Display off: hide the last rendered frame and block input until woken.
    func setSleeping(_ value: Bool) {
        guard value != sleeping else {return}
        sleeping = value
        if value {cancelTouch()}
        CATransaction.begin();CATransaction.setDisableActions(true)
        contentLayer.isHidden = value
        CATransaction.commit()
        sleepOverlay.isHidden = !value
        trace?("display \(value ? "off (sleep)" : "on")")
    }
    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        contentLayer.frame = displayRect
        overlayLayer.frame = bounds
        CATransaction.commit()
        sleepOverlay.frame = displayRect
        updateOverlay()
    }
    func present(surface: IOSurface) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        contentLayer.contents = surface
        CATransaction.commit()
    }
    func updateOverlay() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        overlayLayer.frame = bounds
        overlayLayer.sublayers?.forEach { $0.removeFromSuperlayer() }
        let r = displayRect, diameter = 56 * r.width / 640
        for p in gestures.overlay {
            let dot = CALayer()
            dot.frame = CGRect(x: r.minX + p.x * r.width / 640 - diameter / 2,
                               y: r.minY + p.y * r.height / 1136 - diameter / 2,
                               width: diameter, height: diameter)
            dot.cornerRadius = diameter / 2
            dot.backgroundColor = NSColor.gray.withAlphaComponent(0.45).cgColor
            dot.borderColor = NSColor.white.withAlphaComponent(0.85).cgColor
            dot.borderWidth = 1
            overlayLayer.addSublayer(dot)
        }
        CATransaction.commit()
    }
    func cancelTouch() {
        touchEpoch += 1
        currentModifiers = []
        wheelTimer?.invalidate();wheelTimer = nil
        controlCenterTimer?.invalidate();controlCenterTimer = nil
        gestures.end();gestures.hint(nil,option:false);updateOverlay()
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
        // Presentation is handled by GPU CALayer.
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
        trace?("pointer x=\(x) y=\(y) down=\(down)")
        input?(x,y,down)
    }
    func openControlCenter() {
        guard !held,gestures.beginControlCenter() else {return}
        var tick = 0
        // Begin moving promptly so this is an edge pan, not a long press.
        // Drag for 0.8s and settle briefly; QEMU adds its own release frames.
        let timer = Timer(timeInterval:1.0/30,repeats:true) {[weak self] timer in
            guard let self,self.gestures.kind == .controlCenter else {timer.invalidate();return}
            tick += 1
            if tick >= 1 && tick <= 25 {self.gestures.advanceControlCenter(CGFloat(tick-1)/24)}
            if tick >= 28 {self.gestures.end();timer.invalidate();self.controlCenterTimer = nil}
        }
        controlCenterTimer = timer;RunLoop.main.add(timer,forMode:.common)
    }
    override func mouseDown(with event:NSEvent) {
        touchEpoch += 1
        window?.makeFirstResponder(self)
        // A fresh physical press takes ownership. A lost scroll/magnify end
        // or a pending toolbar swipe must never swallow subsequent clicks.
        if gestures.kind != .none {cancelTouch()}
        if event.modifierFlags.contains(.option),let point = guestPoint(event) {
            gestures.pinch(point,begin:true);updateOverlay()
        } else {pointer(event,true)}
    }
    override func mouseDragged(with event:NSEvent) {
        if gestures.kind == .pinch {
            let p = convert(event.locationInWindow,from:nil),r = displayRect
            let point = gestures.clamp(NSPoint(x:(p.x-r.minX)/r.width*640,y:(p.y-r.minY)/r.height*1136))
            gestures.pinch(point,begin:false,pan:event.modifierFlags.contains(.shift));updateOverlay()
        } else if held {pointer(event,true)}
    }
    override func mouseUp(with event:NSEvent) {
        if gestures.kind == .pinch {gestures.end();gestures.hint(guestPoint(event),option:event.modifierFlags.contains(.option));updateOverlay()}
        else if held {pointer(event,false)}
    }
    override func keyDown(with event: NSEvent) {
        if let action = KeyboardMapping.map(event, down: true) {
            rfbKey?(action.keysym, true)
            qmpKey?(action.qcode, true)
            trace?("keyDown qcode=\(action.qcode) sym=0x\(String(action.keysym, radix: 16))")
        }
    }
    override func keyUp(with event: NSEvent) {
        if let action = KeyboardMapping.map(event, down: false) {
            rfbKey?(action.keysym, false)
            qmpKey?(action.qcode, false)
            trace?("keyUp qcode=\(action.qcode) sym=0x\(String(action.keysym, radix: 16))")
        }
    }
    override func flagsChanged(with event:NSEvent) {
        if let action = KeyboardMapping.mapModifier(event: event, previous: currentModifiers) {
            rfbKey?(action.keysym, action.down)
            qmpKey?(action.qcode, action.down)
            trace?("modifier \(action.down ? "down" : "up") qcode=\(action.qcode) sym=0x\(String(action.keysym, radix: 16))")
        }
        currentModifiers = event.modifierFlags
        let p = window.map {convert($0.mouseLocationOutsideOfEventStream,from:nil)}
        let point = p.flatMap {displayRect.contains($0) ? NSPoint(x:($0.x-displayRect.minX)/displayRect.width*640,y:($0.y-displayRect.minY)/displayRect.height*1136):nil}
        gestures.hint(point,option:event.modifierFlags.contains(.option));updateOverlay()
    }
    override func mouseMoved(with event:NSEvent) {gestures.hint(guestPoint(event),option:event.modifierFlags.contains(.option));updateOverlay()}
    override func mouseExited(with event:NSEvent) {gestures.hint(nil,option:false);updateOverlay()}
    override func scrollWheel(with event:NSEvent) {
        guard !held else {return}
        if !event.momentumPhase.isEmpty {
            if gestures.kind == .scroll {
                gestures.end()
                updateOverlay()
            }
            return
        }
        let wheel = event.phase.isEmpty
        let scale = event.hasPreciseScrollingDeltas ? 640/displayRect.width:40
        let moves = wheel || event.phase.contains(.changed) || event.phase.contains(.ended)
        let isEnd = event.phase.contains(.ended) || event.phase.contains(.cancelled)
        gestures.scroll(guestPoint(event),dx:moves ? event.scrollingDeltaX*scale:0,dy:moves ? event.scrollingDeltaY*scale:0,
            begin:wheel ? gestures.kind == .none:event.phase.contains(.began),
            end:isEnd,wheel:wheel)
        if wheel,gestures.kind == .wheel {
            wheelTimer?.invalidate()
            wheelTimer = Timer.scheduledTimer(withTimeInterval:0.15,repeats:false) {[weak self] _ in self?.gestures.end();self?.updateOverlay()}
        }
        updateOverlay()
    }
    override func magnify(with event:NSEvent) {
        guard !held else {return}
        gestures.magnify(guestPoint(event),delta:event.magnification,begin:event.phase.contains(.began),end:event.phase.contains(.ended) || event.phase.contains(.cancelled))
        updateOverlay()
    }
}

final class Player: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let rfb = RFB(), screen = Screen(), status = NSTextField(labelWithString:"Menunggu layar QEMU…")
    var touchInput: QMPInput?
    var activity: NSObjectProtocol?
    var inputFailed = false
    var gestureError: String?
    var controlCenterPending = false
    var inputLog: FileHandle?
    var window: NSWindow!
    var buttons: [NSButton] = []
    var displayTimer: Timer?
    var displayQuery = false
    private let presentLock = NSLock()
    private var pendingSurface: IOSurface?
    private var presentScheduled = false
    private var displayLink: CADisplayLink?
    private var buttonsConfigured = false
    private var lastPresentUptime: TimeInterval = 0
    private var presentIntervals: [Double] = []
    private var lastFrameTime: TimeInterval = 0
    private let statsEnabled = ProcessInfo.processInfo.environment["VPHONE_PLAYER_STATS"] != nil

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
        for (icon,title,action) in [("house","Home",#selector(home)),("slider.horizontal.3","Control Center",#selector(controlCenter)),("camera","Screen Capture",#selector(capture)),("lock","Lock",#selector(lock))] {
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
        let logURL = URL(fileURLWithPath:CommandLine.arguments[1]).deletingLastPathComponent().appendingPathComponent("player-input.log")
        _ = FileManager.default.createFile(atPath:logURL.path,contents:nil)
        inputLog = try? FileHandle(forWritingTo:logURL)
        screen.trace = {[weak self] text in self?.logInput(text)}
        rfb.trace = {[weak self] text in self?.logInput(text)}
        logInput("player started; VNC pointer and QMP gesture input configured")
        let qmp = QMPInput(CommandLine.arguments[2]);touchInput = qmp
        screen.gestures.send = {[weak self,weak qmp] type,points in
            if type != "update" {self?.logInput("mtt \(type) points=\(points)")}
            qmp?.touch(type,points)
        }
        qmp.failed = {[weak self] message in DispatchQueue.main.async {self?.handleGestureFailure(message)}}
        screen.pixelBufferProvider = { [weak self] in self?.rfb.pixelBuffer }
        screen.pixelBuffer = rfb.pixelBuffer
        screen.input = { [weak self] x,y,down in self?.rfb.pointer(x,y,down) }
        screen.rfbKey = { [weak self] sym, down in self?.rfb.key(sym, down) }
        screen.qmpKey = { [weak self] qcode, down in self?.touchInput?.sendKey(qcode, down) }
        rfb.presentStats = { [weak self] in self?.formatPresentStats() ?? "" }
        rfb.surfaceFrame = { [weak self] surface in
            self?.schedulePresent(surface: surface)
        }
        rfb.failed = { [weak self] message in
            DispatchQueue.main.async {
                guard let self else {return}
                self.inputFailed = true
                self.status.stringValue = "QEMU berhenti / koneksi gagal: \(message)"
                self.status.isHidden = false
                self.updateButtons()
                self.screen.input = nil
                self.screen.rfbKey = nil
                self.screen.qmpKey = nil
            }
        }
        window.center(); window.makeKeyAndOrderFront(nil);window.makeFirstResponder(screen); NSApp.activate(ignoringOtherApps:true)
        if let window {
            let link = window.displayLink(target: self, selector: #selector(vsyncFired(_:)))
            link.add(to: .main, forMode: .common)
            displayLink = link
        }
        screen.sleepOverlay.onWake = {[weak self] in self?.home()}
        // Poll the guest's display power so a sleeping device shows as locked
        // instead of a frozen frame that ignores touches.
        displayTimer = Timer.scheduledTimer(withTimeInterval:0.5,repeats:true) {[weak self] _ in self?.pollDisplay()}
        let path = CommandLine.arguments[1]
        DispatchQueue.global(qos:.userInteractive).async { self.rfb.run(path) }
    }

    @objc func vsyncFired(_ link: CADisplayLink) {
        flushPendingPresent()
    }

    func flushPendingPresent() {
        presentLock.lock()
        guard let surface = pendingSurface else {
            presentLock.unlock()
            return
        }
        pendingSurface = nil
        presentLock.unlock()

        screen.present(surface: surface)
        recordPresentInterval()

        if !inputFailed && gestureError == nil && !status.isHidden {
            status.isHidden = true
        }
        if !buttonsConfigured && !inputFailed {
            buttonsConfigured = true
            updateButtons()
        }
    }

    func schedulePresent(surface: IOSurface) {
        lastFrameTime = Date().timeIntervalSince1970
        presentLock.lock()
        pendingSurface = surface
        let hasLink = (displayLink != nil)
        let scheduled = presentScheduled
        if !hasLink && !scheduled {
            presentScheduled = true
        }
        presentLock.unlock()

        if !hasLink && !scheduled {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.presentLock.lock()
                self.presentScheduled = false
                self.presentLock.unlock()
                self.flushPendingPresent()
            }
        }
    }

    func recordPresentInterval() {
        guard statsEnabled else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if lastPresentUptime > 0 {
            let deltaMs = (now - lastPresentUptime) * 1000.0
            presentIntervals.append(deltaMs)
        }
        lastPresentUptime = now
    }

    func formatPresentStats() -> String {
        presentLock.lock()
        defer { presentLock.unlock() }
        guard !presentIntervals.isEmpty else {
            return ", present_ms(p50/p95/p99/max): 0.0/0.0/0.0/0.0"
        }
        presentIntervals.sort()
        let n = presentIntervals.count
        let p50 = presentIntervals[Int(Double(n - 1) * 0.50)]
        let p95 = presentIntervals[Int(Double(n - 1) * 0.95)]
        let p99 = presentIntervals[Int(Double(n - 1) * 0.99)]
        let maxMs = presentIntervals.last!
        presentIntervals.removeAll(keepingCapacity: true)
        return String(format: ", present_ms(p50/p95/p99/max): %.1f/%.1f/%.1f/%.1f",
                      p50, p95, p99, maxMs)
    }

    func updateButtons() {
        buttons.enumerated().forEach { i, button in
            button.isEnabled = !inputFailed && !(i == 1 && controlCenterPending)
        }
    }

    func handleGestureFailure(_ message:String) {
        // A gesture transport failure must not disable the independent pointer.
        logInput("QMP failure: \(message)")
        gestureError = message
        status.stringValue = "Gesture gagal: \(message). Klik/drag tetap tersedia."
        status.isHidden = false
        updateButtons()
        let send = screen.gestures.send
        screen.gestures.send = nil
        screen.cancelTouch()
        screen.gestures.send = send
    }
    func logInput(_ text:String) {
        try? inputLog?.write(contentsOf:Data("\(Date().timeIntervalSince1970) \(text)\n".utf8))
    }
    @objc func home() { screen.cancelTouch();rfb.home() }
    @objc func lock() {
        screen.cancelTouch()
        touchInput?.pressPower {[weak self] error in
            guard let error else {return}
            DispatchQueue.main.async {self?.handleGestureFailure(error)}
        }
    }
    func pollDisplay() {
        guard !displayQuery, let touchInput else {return}
        let now = Date().timeIntervalSince1970
        // When the guest is actively streaming frames, the display is definitely awake.
        if !screen.sleeping && (now - lastFrameTime < 1.0) {
            return
        }
        displayQuery = true
        touchInput.displayOn {[weak self] on in DispatchQueue.main.async {
            guard let self else {return}
            self.displayQuery = false
            if let on {self.screen.setSleeping(!on)}
        }}
    }
    @objc func controlCenter() {
        guard !controlCenterPending,!screen.held,screen.gestures.kind == .none,let touchInput else {return}
        controlCenterPending = true; updateButtons()
        let epoch = screen.touchEpoch
        touchInput.prepareControlCenter {[weak self] error in DispatchQueue.main.async {
            guard let self else {return}
            self.controlCenterPending = false; self.updateButtons()
            guard self.screen.touchEpoch == epoch else {return}
            if let error {self.handleGestureFailure(error);return}
            self.gestureError = nil;self.status.isHidden = true
            self.screen.openControlCenter()
        }}
    }
    @objc func capture() {
        guard let image = screen.image else {return}
        let png = NSBitmapImageRep(cgImage:image).representation(using:.png,properties:[:])
        let panel = NSSavePanel(); panel.allowedContentTypes = [.png]; panel.nameFieldStringValue = "vphone-\(Int(Date().timeIntervalSince1970)).png"
        panel.beginSheetModal(for:window) { result in
            guard result == .OK, let url = panel.url, let png else {return}
            do {try png.write(to:url,options:.atomic)} catch {NSAlert(error:error).runModal()}
        }
    }
    func windowDidResize(_ notification: Notification) {
        guard let window else {return}
        logInput("window frame=\(window.frame) screen=\(String(describing:window.screen?.frame))")
    }
    func windowDidResignKey(_ notification: Notification) { screen.cancelTouch() }
    func windowWillClose(_ notification:Notification) {
        displayLink?.invalidate()
        displayLink = nil
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
