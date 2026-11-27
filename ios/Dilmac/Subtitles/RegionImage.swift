import CoreGraphics
import CoreImage
import DilmacCore
import Foundation

/// The subtitle region of one frame as RGBA8 pixels, plus the image preparations of pc/ocr.py that need pixels:
/// the "white text with a dark shadow" mask and the small signature used to tell whether text is still in place.
struct RegionImage {
    let width: Int
    let height: Int
    /// RGBA8, row after row (no padding).
    let pixels: [UInt8]

    /// Renders `rect` (top-left origin, in pixels of the frame) of the frame.
    static func render(_ frame: CIImage, rect: CGRect, context: CIContext) -> RegionImage? {
        let w = Int(rect.width), h = Int(rect.height)
        guard w > 4, h > 4 else { return nil }
        // Core Image's origin is bottom-left.
        let ciRect = CGRect(x: rect.minX, y: frame.extent.height - rect.maxY, width: rect.width, height: rect.height)
        let crop = frame.cropped(to: ciRect).transformed(by: CGAffineTransform(translationX: -ciRect.minX, y: -ciRect.minY))
        var pixels = [UInt8](repeating: 0, count: w * h * 4)
        context.render(crop, toBitmap: &pixels, rowBytes: w * 4, bounds: CGRect(x: 0, y: 0, width: w, height: h),
                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))
        return RegionImage(width: w, height: h, pixels: pixels)
    }

    /// The region as it is (Vision reads color well; this is ocr.py's "raw" read).
    func cgImage() -> CGImage? {
        let data = Data(pixels) as CFData
        guard let provider = CGDataProvider(data: data) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
                       space: CGColorSpace(name: CGColorSpace.sRGB)!,
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }

    /// ocr.py prepare_white: only "white text + a dark shadow to its lower right" pixels, as black text on white.
    /// Erases a bright background (sky, shirt, pavement) behind GTA-style subtitles.
    func whiteTextImage(frameHeight: Int) -> CGImage? {
        let k = max(1, Int((Double(frameHeight) / 680).rounded()))
        let mask = Self.whiteMask(autoLevel(), width: width, height: height, shadow: k)
        let gray = mask.map { $0 ? UInt8(0) : UInt8(255) }
        guard let provider = CGDataProvider(data: Data(gray) as CFData) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: width,
                       space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: 0),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }

    /// ocr.py auto_level: the brightest 0.5% is pulled to white (a white subtitle can sit at 150-200 in dark scenes).
    private func autoLevel() -> [UInt8] {
        var histogram = [Int](repeating: 0, count: 256)
        let n = width * height
        for i in 0..<n {
            let p = i * 4
            let l = (Int(pixels[p]) * 299 + Int(pixels[p + 1]) * 587 + Int(pixels[p + 2]) * 114) / 1000
            histogram[l] += 1
        }
        let target = Double(n) * 0.005
        var total = 0, peak = 255
        for v in stride(from: 255, through: 0, by: -1) {
            total += histogram[v]
            if Double(total) >= target { peak = v; break }
        }
        if peak >= 235 || peak < 100 { return pixels }
        let scale = 255.0 / Double(peak)
        return pixels.map { UInt8(min(255, Int(Double($0) * scale))) }
    }

    /// ocr.py white_mask: true = white text pixel. With shadow > 0 a white pixel needs a dark pixel in the window
    /// [x, x+2*shadow] x [y, y+2*shadow] (shadow searched only to the lower right).
    static func whiteMask(_ px: [UInt8], width w: Int, height h: Int, shadow k: Int,
                          bright: Int = 185, color: Int = 60, dark: Int = 90) -> [Bool] {
        var mask = [Bool](repeating: false, count: w * h)
        var darkPx = [Bool](repeating: false, count: w * h)
        for i in 0..<(w * h) {
            let r = Int(px[i * 4]), g = Int(px[i * 4 + 1]), b = Int(px[i * 4 + 2])
            let mn = min(r, g, b), mx = max(r, g, b)
            mask[i] = mn >= bright && mx - mn <= color
            darkPx[i] = mx <= dark
        }
        guard k > 0 else { return mask }
        for y in 0..<h {
            for x in 0..<w where mask[y * w + x] {
                var found = false
                search: for yy in y...min(h - 1, y + 2 * k) {
                    for xx in x...min(w - 1, x + 2 * k) where darkPx[yy * w + xx] { found = true; break search }
                }
                mask[y * w + x] = found
            }
        }
        return mask
    }

    /// ocr.py box_signature: white-text mask (no shadow test) of the box where the text was read, shrunk to 128x32.
    func signature(of box: Box, layout: Layout) -> [Double]? {
        let r = layout.toRegion(box)
        let px = 0.01, py = 0.25 * r.h
        let x0 = max(0, Int((r.x - px) * Double(width))), y0 = max(0, Int((r.y - py) * Double(height)))
        let x1 = min(width, Int((r.x + r.w + px) * Double(width)) + 1)
        let y1 = min(height, Int((r.y + r.h + py) * Double(height)) + 1)
        guard x1 - x0 >= 4, y1 - y0 >= 4 else { return nil }
        let cw = x1 - x0, ch = y1 - y0
        var crop = [UInt8](repeating: 0, count: cw * ch * 4)
        for y in 0..<ch {
            let src = ((y0 + y) * width + x0) * 4
            crop.replaceSubrange((y * cw * 4)..<((y + 1) * cw * 4), with: pixels[src..<(src + cw * 4)])
        }
        let mask = Self.whiteMask(crop, width: cw, height: ch, shadow: 0)
        // Box filter down to 128 x 32 (values 0-255 like PIL's "L" image).
        let tw = 128, th = 32
        var out = [Double](repeating: 0, count: tw * th)
        for ty in 0..<th {
            let sy0 = ty * ch / th, sy1 = max(sy0 + 1, (ty + 1) * ch / th)
            for tx in 0..<tw {
                let sx0 = tx * cw / tw, sx1 = max(sx0 + 1, (tx + 1) * cw / tw)
                var sum = 0, count = 0
                for sy in sy0..<min(sy1, ch) {
                    for sx in sx0..<min(sx1, cw) { sum += mask[sy * cw + sx] ? 255 : 0; count += 1 }
                }
                out[ty * tw + tx] = count > 0 ? Double(sum) / Double(count) : 0
            }
        }
        return out
    }

    /// ocr.py text_in_place: is at least `ratio` of the white text of the last read frame still there.
    static func textInPlace(_ now: [Double]?, _ last: [Double]?, ratio: Double = 0.6) -> Bool {
        guard let now, let last, now.count == last.count else { return false }
        let total = last.reduce(0, +)
        if total < 255 * 20 { return false }
        return zip(now, last).reduce(0) { $0 + min($1.0, $1.1) } / total >= ratio
    }
}
