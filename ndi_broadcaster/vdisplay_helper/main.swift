// vdisplay_helper.swift
//
// Standalone command-line helper that creates a fully virtual (no physical
// hardware) macOS display using the private CGVirtualDisplay* API surface,
// the same one used by DeskPad / BetterDisplay. Declarations come from
// CGVirtualDisplayPrivate.h.
//
// Usage:
//   ./vdisplay_helper <width> <height> [name]
//
// Prints one JSON line to stdout once the display is live:
//   {"displayID": 69732865, "x": 5000, "y": 0, "width": 3840, "height": 2160}
//
// Then blocks (RunLoop.main.run()) holding the display open until it
// receives SIGINT/SIGTERM, at which point it exits (deallocating the
// CGVirtualDisplay object, which tears the display down).

import Cocoa
import CoreGraphics
import Foundation

setbuf(stdout, nil)

let args = CommandLine.arguments
let width = args.count > 1 ? Int(args[1]) ?? 3840 : 3840
let height = args.count > 2 ? Int(args[2]) ?? 2160 : 2160
let displayName = args.count > 3 ? args[3] : "Layout Driver Virtual Display"

// Place the virtual display far to the right of any real display so its
// coordinate space can never overlap the physical desktop, and so the
// origin we hand to Chromium's --window-position is unambiguous.
let desiredOriginX: Int32 = 5000
let desiredOriginY: Int32 = 0

FileHandle.standardError.write("vdisplay_helper: creating \(width)x\(height) virtual display named \(displayName)\n".data(using: .utf8)!)

let descriptor = CGVirtualDisplayDescriptor()
descriptor.setDispatchQueue(DispatchQueue.main)
descriptor.name = displayName
descriptor.maxPixelsWide = UInt32(width)
descriptor.maxPixelsHigh = UInt32(height)
descriptor.sizeInMillimeters = CGSize(width: 1600, height: 900)
// Randomized identity fields avoid colliding with any cached WindowServer
// display-preference record keyed by vendor/product/serial from a prior run
// of this or another virtual-display tool on this machine.
descriptor.productID = UInt32.random(in: 0x1000...0xFFFE)
descriptor.vendorID = UInt32.random(in: 0x1000...0xFFFE)
descriptor.serialNum = UInt32(ProcessInfo.processInfo.processIdentifier)
descriptor.terminationHandler = { _, _ in
    FileHandle.standardError.write("vdisplay_helper: termination handler fired\n".data(using: .utf8)!)
}

let display = CGVirtualDisplay(descriptor: descriptor)

let mode = CGVirtualDisplayMode(width: UInt(width), height: UInt(height), refreshRate: 60.0)
let settings = CGVirtualDisplaySettings()
settings.hiDPI = 0
settings.modes = [mode]

let applied = display.apply(settings)
FileHandle.standardError.write("vdisplay_helper: applySettings returned \(applied), displayID=\(display.displayID)\n".data(using: .utf8)!)

if !applied {
    FileHandle.standardError.write("vdisplay_helper: FATAL applySettings failed\n".data(using: .utf8)!)
    exit(1)
}

// Explicitly select the display mode matching our requested resolution.
// GOTCHA: calling CGConfigureDisplayWithDisplayMode immediately (within
// ~0.5s) after the display is created reports success but is silently
// clobbered a moment later by WindowServer's async "new display attached"
// auto-arrange/default-mode logic. Reading CGDisplayBounds back afterward
// confirms the mode reverted (observed reverting to 1920x1080). The fix:
// wait for the display to settle (~1.5s) before touching mode/origin, then
// verify and retry a few times if it didn't stick.
func currentSize(_ displayID: CGDirectDisplayID) -> (Int, Int) {
    let mode = CGDisplayCopyDisplayMode(displayID)
    return (mode?.width ?? -1, mode?.height ?? -1)
}

func findMode(_ displayID: CGDirectDisplayID, w: Int, h: Int) -> CGDisplayMode? {
    guard let allModes = CGDisplayCopyAllDisplayModes(displayID, nil) as? [CGDisplayMode] else {
        return nil
    }
    return allModes.first { $0.width == w && $0.height == h }
}

Thread.sleep(forTimeInterval: 1.5)

if let allModes = CGDisplayCopyAllDisplayModes(display.displayID, nil) as? [CGDisplayMode] {
    FileHandle.standardError.write("vdisplay_helper: available modes: \(allModes.map { ($0.width, $0.height) })\n".data(using: .utf8)!)
}

var attempt = 0
let maxAttempts = 6
while attempt < maxAttempts {
    attempt += 1
    guard let targetMode = findMode(display.displayID, w: width, h: height) else {
        FileHandle.standardError.write("vdisplay_helper: WARNING no matching CGDisplayMode found for \(width)x\(height) (attempt \(attempt))\n".data(using: .utf8)!)
        Thread.sleep(forTimeInterval: 0.5)
        continue
    }

    var config: CGDisplayConfigRef?
    let beginResult = CGBeginDisplayConfiguration(&config)
    if beginResult == .success, let config = config {
        CGConfigureDisplayOrigin(config, display.displayID, desiredOriginX, desiredOriginY)
        let modeResult = CGConfigureDisplayWithDisplayMode(config, display.displayID, targetMode, nil)
        let completeResult = CGCompleteDisplayConfiguration(config, .permanently)
        FileHandle.standardError.write("vdisplay_helper: attempt \(attempt): configureMode=\(modeResult.rawValue) complete=\(completeResult.rawValue)\n".data(using: .utf8)!)
    } else {
        FileHandle.standardError.write("vdisplay_helper: CGBeginDisplayConfiguration failed -> \(beginResult.rawValue)\n".data(using: .utf8)!)
    }

    Thread.sleep(forTimeInterval: 0.6)
    let (w, h) = currentSize(display.displayID)
    FileHandle.standardError.write("vdisplay_helper: after attempt \(attempt), current size = \(w)x\(h)\n".data(using: .utf8)!)
    if w == width && h == height {
        break
    }
}

// Never trust the requested origin as applied -- always read back the real
// bounds WindowServer settled on.
let bounds = CGDisplayBounds(display.displayID)
FileHandle.standardError.write("vdisplay_helper: final bounds = \(bounds)\n".data(using: .utf8)!)

let payload: [String: Any] = [
    "displayID": display.displayID,
    "x": Int(bounds.origin.x),
    "y": Int(bounds.origin.y),
    "width": Int(bounds.size.width),
    "height": Int(bounds.size.height),
]
if let jsonData = try? JSONSerialization.data(withJSONObject: payload),
   let jsonString = String(data: jsonData, encoding: .utf8) {
    print(jsonString)
}

// Keep the display alive (and the process holding `display` retained) until
// killed.
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSource.setEventHandler {
    FileHandle.standardError.write("vdisplay_helper: SIGINT received, exiting\n".data(using: .utf8)!)
    exit(0)
}
sigintSource.resume()
signal(SIGINT, SIG_IGN)

let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler {
    FileHandle.standardError.write("vdisplay_helper: SIGTERM received, exiting\n".data(using: .utf8)!)
    exit(0)
}
sigtermSource.resume()
signal(SIGTERM, SIG_IGN)

withExtendedLifetime(display) {
    RunLoop.main.run()
}
