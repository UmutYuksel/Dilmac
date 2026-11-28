import AVFoundation
import CoreImage
import SwiftUI
#if os(macOS)
import AppKit

/// Shows decoded frames on an AVSampleBufferDisplayLayer (no extra copy, GPU composited).
final class VideoLayerView: NSView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        wantsLayer = true
        displayLayer.videoGravity = .resizeAspect
        displayLayer.backgroundColor = NSColor.black.cgColor
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func makeBackingLayer() -> CALayer { AVSampleBufferDisplayLayer() }
    var displayLayer: AVSampleBufferDisplayLayer { layer as! AVSampleBufferDisplayLayer }

    /// The layer's renderer; frames can be enqueued on it from any thread (see `FrameDisplay`).
    var renderer: AVSampleBufferVideoRenderer { displayLayer.sampleBufferRenderer }
}

struct VideoView: NSViewRepresentable {
    let view: VideoLayerView
    func makeNSView(context: Context) -> VideoLayerView { view }
    func updateNSView(_ nsView: VideoLayerView, context: Context) {}
}
#else
import UIKit

/// Shows decoded frames on an AVSampleBufferDisplayLayer (no extra copy, GPU composited).
final class VideoLayerView: UIView {
    override class var layerClass: AnyClass { AVSampleBufferDisplayLayer.self }
    var displayLayer: AVSampleBufferDisplayLayer { layer as! AVSampleBufferDisplayLayer }

    override init(frame: CGRect) {
        super.init(frame: frame)
        displayLayer.videoGravity = .resizeAspect
        backgroundColor = .black
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The layer's renderer; frames can be enqueued on it from any thread (see `FrameDisplay`).
    var renderer: AVSampleBufferVideoRenderer { displayLayer.sampleBufferRenderer }
}

struct VideoView: UIViewRepresentable {
    let view: VideoLayerView
    func makeUIView(context: Context) -> VideoLayerView { view }
    func updateUIView(_ uiView: VideoLayerView, context: Context) {}
}
#endif

enum FrameDisplay {
    /// Shows a decoded frame right away. Safe from any thread.
    static func show(_ pixelBuffer: CVPixelBuffer, on renderer: AVSampleBufferVideoRenderer) {
        var format: CMVideoFormatDescription?
        guard CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: pixelBuffer,
                                                           formatDescriptionOut: &format) == noErr,
              let format else { return }
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: .zero, decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        guard CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: pixelBuffer,
                                                       formatDescription: format, sampleTiming: &timing,
                                                       sampleBufferOut: &sample) == noErr,
              let sample else { return }
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: true),
           CFArrayGetCount(attachments) > 0 {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(attachments, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(dict, Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                                 Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }
        if renderer.status == .failed { renderer.flush() }
        renderer.enqueue(sample)
    }
}

/// Saves frames as PNG under Documents/frames (visible in the Files app: On My iPhone > Dilmaç); on the Mac under
/// ~/Library/Application Support/Dilmac/frames.
enum FrameStore {
    private static let context = CIContext()

    static var folder: URL {
        #if os(macOS)
        URL.applicationSupportDirectory.appendingPathComponent("Dilmac/frames")
        #else
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("frames")
        #endif
    }

    /// A saved frame back as a pixel buffer (BGRA), as the decoder would hand it over.
    static func load(_ url: URL) -> CVPixelBuffer? {
        guard let image = CIImage(contentsOf: url) else { return nil }
        let buffer = makeBuffer(Int(image.extent.width), Int(image.extent.height))
        if let buffer { context.render(image, to: buffer) }
        return buffer
    }

    /// A black frame of the same size (the subtitle is gone).
    static func blank(like frame: CVPixelBuffer) -> CVPixelBuffer? {
        let w = CVPixelBufferGetWidth(frame), h = CVPixelBufferGetHeight(frame)
        guard let buffer = makeBuffer(w, h) else { return nil }
        context.render(CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: w, height: h)), to: buffer)
        return buffer
    }

    private static func makeBuffer(_ w: Int, _ h: Int) -> CVPixelBuffer? {
        var buffer: CVPixelBuffer?
        let attrs: [CFString: Any] = [kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary]
        CVPixelBufferCreate(nil, w, h, kCVPixelFormatType_32BGRA, attrs as CFDictionary, &buffer)
        return buffer
    }

    static func save(_ pixelBuffer: CVPixelBuffer) throws -> URL {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let png = context.pngRepresentation(of: image, format: .RGBA8, colorSpace: space) else {
            throw CocoaError(.fileWriteUnknown)
        }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let stamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current,
                                                formatOptions: [.withFullDate, .withTime])
        let url = folder.appendingPathComponent("frame-\(stamp).png")
        try png.write(to: url, options: .completeFileProtection)
        return url
    }
}
