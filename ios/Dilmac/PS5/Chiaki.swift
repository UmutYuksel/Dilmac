import ChiakiBridge
import Foundation

/// Swift face of the C bridge (pc/chiaki_bridge/bridge.h), port of the bridge parts of pc/ps5_direct.py.
/// All calls here block; call them off the main thread.
enum Chiaki {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// chiaki's log lines (error, warning, info). Set before `start()`; called on chiaki's threads.
    nonisolated(unsafe) static var log: ((Int32, String) -> Void)?

    private static let started: Int32 = {
        kp_init({ level, message in
            guard let message else { return }
            Chiaki.log?(level, String(cString: message))
        }, 1 | 2 | 4)
    }()

    static func start() throws {
        let error = started
        if error != 0 { throw Failure(message: "chiaki başlatılamadı: \(errorText(error))") }
    }

    static func errorText(_ code: Int32) -> String {
        kp_error_text(code).map { String(cString: $0) } ?? "hata \(code)"
    }

    /// Registers with the PS5 using the 8-digit Link Device code. The PS5 must be on (not in rest mode).
    static func register(host: String, account: PSNAccount, pin: UInt32) throws -> PS5Pairing {
        try start()
        let accountBytes = withUnsafeBytes(of: account.accountID.littleEndian) { Array($0) }
        var registKey = [CChar](repeating: 0, count: 16)
        var rpKey = [UInt8](repeating: 0, count: 16)
        var mac = [UInt8](repeating: 0, count: 6)
        var name = [CChar](repeating: 0, count: 64)
        let outcome = kp_register(host, accountBytes, pin, 1, 30_000, &registKey, &rpKey, &mac, &name, name.count)
        switch outcome {
        case 0:
            let nameBytes = name.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }
            let ps5Name = String(decoding: nameBytes, as: UTF8.self)
            return PS5Pairing(host: host, name: ps5Name.isEmpty ? "PS5" : ps5Name, mac: Data(mac),
                              registKey: Data(registKey.map { UInt8(bitPattern: $0) }), rpKey: Data(rpKey),
                              date: Date())
        case 2:
            throw Failure(message: "PS5 yanıt vermedi (zaman aşımı). Cihazı Bağla ekranı açık mı?")
        case 1000...:
            throw Failure(message: "Eşleme başlatılamadı: \(errorText(outcome - 1000))")
        default:
            throw Failure(message: "PS5 eşlemeyi kabul etmedi. Kod yanlış ya da süresi dolmuş olabilir; "
                          + "ekrandaki yeni kodu dene. Bu PSN hesabının PS5'te bir kullanıcısı olmalı.")
        }
    }

    /// Sends the wake-up packet. The PS5 then needs some seconds to boot.
    static func wake(_ pairing: PS5Pairing) throws {
        try start()
        let result = pairing.registKey.withUnsafeBytes { key in
            kp_wakeup(pairing.host, key.baseAddress!.assumingMemoryBound(to: CChar.self), 1)
        }
        if result != 0 { throw Failure(message: "Uyandırma paketi gönderilemedi: \(errorText(result))") }
    }
}

/// One Remote Play stream (video only, no audio, no controller). Keep a strong reference until `close()` returns.
final class ChiakiStream: @unchecked Sendable {
    enum Event {
        case connected
        case ended(String)
        case pinRequested
    }

    enum Resolution: Int32 { case p360 = 1, p540 = 2, p720 = 3, p1080 = 4 }

    /// One H.264 access unit (Annex B). Return false if it could not be decoded: a keyframe is then requested.
    /// Called on chiaki's video thread.
    var onFrame: ((UnsafeBufferPointer<UInt8>, Int32) -> Bool)?
    /// Called on chiaki's threads.
    var onEvent: ((Event) -> Void)?

    private var handle: UnsafeMutableRawPointer?
    private let lock = NSLock()

    func open(_ pairing: PS5Pairing, resolution: Resolution = .p720, fps: Int32 = 30) throws {
        try Chiaki.start()
        let user = Unmanaged.passUnretained(self).toOpaque()
        var error: Int32 = 0
        let ptr = pairing.registKey.withUnsafeBytes { registKey in
            pairing.rpKey.withUnsafeBytes { rpKey in
                kp_session_open(pairing.host, registKey.baseAddress!.assumingMemoryBound(to: CChar.self),
                                rpKey.baseAddress!.assumingMemoryBound(to: UInt8.self), 1, resolution.rawValue, fps,
                                0, chiakiFrame, chiakiEvent, nil, user, &error)
            }
        }
        guard let ptr else { throw Chiaki.Failure(message: "PS5 oturumu açılamadı: \(Chiaki.errorText(error))") }
        lock.withLock { handle = ptr }
    }

    /// Stops the stream; waits until chiaki's threads are done.
    func close() {
        let ptr = lock.withLock { () -> UnsafeMutableRawPointer? in
            defer { handle = nil }
            return handle
        }
        if let ptr { kp_session_close(ptr) }
    }

    func requestKeyframe() {
        lock.withLock { if let handle { _ = kp_request_keyframe(handle) } }
    }

    fileprivate func frame(_ buffer: UnsafeBufferPointer<UInt8>, lost: Int32) -> Bool {
        onFrame?(buffer, lost) ?? true
    }

    fileprivate func event(kind: Int32, code: Int32, text: String) {
        switch kind {
        case 1: onEvent?(.connected)
        case 2: onEvent?(.ended(text.isEmpty ? "kod \(code)" : text))
        case 3: onEvent?(.pinRequested)
        default: break
        }
    }
}

private let chiakiFrame: kp_frame_cb = { buffer, size, lost, user in
    guard let buffer, let user else { return 1 }
    let stream = Unmanaged<ChiakiStream>.fromOpaque(user).takeUnretainedValue()
    return stream.frame(UnsafeBufferPointer(start: buffer, count: size), lost: lost) ? 1 : 0
}

private let chiakiEvent: kp_event_cb = { kind, code, text, user in
    guard let user else { return }
    let stream = Unmanaged<ChiakiStream>.fromOpaque(user).takeUnretainedValue()
    stream.event(kind: kind, code: code, text: text.map { String(cString: $0) } ?? "")
}
