import CoreMedia
import Foundation
import VideoToolbox

/// Decodes the PS5's H.264 stream (Annex B access units from chiaki) with VideoToolbox into NV12 pixel buffers.
/// Not thread-safe: feed it from one thread (chiaki's video thread).
final class H264Decoder {
    /// Each decoded frame. Called on the thread that calls `decode`.
    var onFrame: ((CVPixelBuffer) -> Void)?
    private(set) var decodedCount = 0

    private var sps: [UInt8]?
    private var pps: [UInt8]?
    private var format: CMVideoFormatDescription?
    private var session: VTDecompressionSession?

    deinit { invalidate() }

    func invalidate() {
        if let session { VTDecompressionSessionInvalidate(session) }
        session = nil
        format = nil
    }

    /// Returns false if the access unit could not be decoded (the caller then asks the PS5 for a keyframe).
    func decode(_ accessUnit: UnsafeBufferPointer<UInt8>) -> Bool {
        var avcc = [UInt8]()
        var parametersChanged = false
        for nal in Self.nalUnits(accessUnit) {
            guard let header = nal.first else { continue }
            switch header & 0x1F {
            case 7:
                if sps != nal { sps = nal; parametersChanged = true }
            case 8:
                if pps != nal { pps = nal; parametersChanged = true }
            case 9: // access unit delimiter
                continue
            default:
                let n = UInt32(nal.count).bigEndian
                withUnsafeBytes(of: n) { avcc.append(contentsOf: $0) }
                avcc.append(contentsOf: nal)
            }
        }
        if parametersChanged || session == nil {
            guard makeSession() else { return avcc.isEmpty }
        }
        guard !avcc.isEmpty else { return true }
        guard let session, let format, let sample = Self.sampleBuffer(avcc, format: format) else { return false }

        var ok = true
        let status = VTDecompressionSessionDecodeFrame(session, sampleBuffer: sample, flags: [], infoFlagsOut: nil) {
            [weak self] status, _, image, _, _ in
            guard status == noErr, let image else { ok = false; return }
            self?.decodedCount += 1
            self?.onFrame?(image)
        }
        if status == kVTInvalidSessionErr { invalidate() }
        return status == noErr && ok
    }

    private func makeSession() -> Bool {
        invalidate()
        guard let sps, let pps else { return false }
        var desc: CMVideoFormatDescription?
        let status = sps.withUnsafeBufferPointer { s in
            pps.withUnsafeBufferPointer { p in
                let pointers = [s.baseAddress!, p.baseAddress!]
                let sizes = [s.count, p.count]
                return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: nil, parameterSetCount: 2, parameterSetPointers: pointers,
                    parameterSetSizes: sizes, nalUnitHeaderLength: 4, formatDescriptionOut: &desc)
            }
        }
        guard status == noErr, let desc else { return false }

        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        ]
        var created: VTDecompressionSession?
        let made = VTDecompressionSessionCreate(allocator: nil, formatDescription: desc, decoderSpecification: nil,
                                                imageBufferAttributes: attributes as CFDictionary,
                                                outputCallback: nil, decompressionSessionOut: &created)
        guard made == noErr, let created else { return false }
        VTSessionSetProperty(created, key: kVTDecompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        format = desc
        session = created
        return true
    }

    private static func sampleBuffer(_ data: [UInt8], format: CMVideoFormatDescription) -> CMSampleBuffer? {
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil,
                                                 blockLength: data.count, blockAllocator: kCFAllocatorDefault,
                                                 customBlockSource: nil, offsetToData: 0, dataLength: data.count,
                                                 flags: kCMBlockBufferAssureMemoryNowFlag,
                                                 blockBufferOut: &block) == noErr, let block else { return nil }
        guard data.withUnsafeBytes({
            CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: block, offsetIntoDestination: 0,
                                          dataLength: data.count)
        }) == noErr else { return nil }
        var sample: CMSampleBuffer?
        var size = data.count
        guard CMSampleBufferCreateReady(allocator: kCFAllocatorDefault, dataBuffer: block,
                                        formatDescription: format, sampleCount: 1, sampleTimingEntryCount: 0,
                                        sampleTimingArray: nil, sampleSizeEntryCount: 1, sampleSizeArray: &size,
                                        sampleBufferOut: &sample) == noErr else { return nil }
        return sample
    }

    /// Splits an Annex B byte stream at its 00 00 01 / 00 00 00 01 start codes.
    static func nalUnits(_ data: UnsafeBufferPointer<UInt8>) -> [[UInt8]] {
        var units = [[UInt8]]()
        var start: Int?
        var i = 0
        let n = data.count
        while i + 2 < n {
            if data[i] == 0, data[i + 1] == 0, data[i + 2] == 1 {
                if let s = start {
                    var end = i
                    while end > s, data[end - 1] == 0 { end -= 1 } // trailing zero of a 4-byte start code
                    if end > s { units.append(Array(data[s..<end])) }
                }
                i += 3
                start = i
            } else {
                i += 1
            }
        }
        if let s = start, s < n { units.append(Array(data[s..<n])) }
        return units
    }
}
