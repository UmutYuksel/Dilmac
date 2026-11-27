import AVFoundation
import CoreImage
import SwiftUI
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

struct VideoView: UIViewRepresentable {
    let view: VideoLayerView
    func makeUIView(context: Context) -> VideoLayerView { view }
    func updateUIView(_ uiView: VideoLayerView, context: Context) {}
}

/// Saves frames as PNG under Documents/frames (visible in the Files app: On My iPhone > Dilmaç).
enum FrameStore {
    private static let context = CIContext()

    static var folder: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("frames")
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
