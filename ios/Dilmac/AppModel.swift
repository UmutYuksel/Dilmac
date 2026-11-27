import AVFoundation
import Foundation
import SwiftUI
import UIKit

/// State and actions of the v0.1 test screen: PSN sign-in, finding and pairing the PS5, streaming frames.
@MainActor
final class AppModel: ObservableObject {
    enum StreamState: Equatable { case idle, connecting, connected }

    @Published var account: PSNAccount? = Keychain.load(PSNAccount.self, for: Keychain.psnAccount)
    @Published var pairing: PS5Pairing? = Keychain.load(PS5Pairing.self, for: Keychain.ps5Pairing)
    @Published var host = ""
    @Published var pin = ""
    @Published var status = ""
    @Published var busy = false
    @Published var showLogin = false
    @Published var streamState = StreamState.idle
    @Published var fps = 0
    @Published var decodedFrames = 0
    @Published var lastSaved: URL?
    @Published var log: [String] = []

    // Subtitles (OCR -> translation -> TV)
    @Published var subtitlesReady = false
    @Published var packInfo = ""
    @Published var translationInstalled: Bool?
    @Published var tvHost = UserDefaults.standard.string(forKey: "tv-host") ?? ""
    @Published var tvConnected: Bool?
    @Published var english = ""
    @Published var turkish = ""
    @Published var turkishInfo = ""
    @Published var deviceInfo = ""

    let videoView = VideoLayerView()

    private var pipeline: SubtitlePipeline?
    private var stream: ChiakiStream?
    private let latest = LatestFrame()
    private var ticker: Timer?
    private var lastCount = 0
    private var lastKeyframeRequest = Date.distantPast

    init() {
        host = pairing?.host ?? ""
        Chiaki.log = { [weak self] level, line in
            let tag = level == 1 ? "HATA" : level == 2 ? "UYARI" : "bilgi"
            Task { @MainActor in self?.append("chiaki \(tag): \(line)") }
        }
        ticker = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        UIDevice.current.isBatteryMonitoringEnabled = true
        loadSubtitles()
    }

    // MARK: subtitles

    /// Loads the glossary, the game pack and the English dictionary off the main thread, then builds the pipeline.
    private func loadSubtitles() {
        Task.detached(priority: .userInitiated) {
            let data = GameData.load()
            let pipeline = SubtitlePipeline(lexicon: data.lexicon, glossary: data.glossary, pack: data.pack)
            let installed = await Translator.languageInstalled()
            await MainActor.run {
                pipeline.onEvent = { [weak self] event in Task { @MainActor in self?.handle(event) } }
                self.pipeline = pipeline
                self.subtitlesReady = true
                self.translationInstalled = installed
                self.packInfo = "\(data.glossary?.gameTitle ?? "Oyun yok") · paket \(data.pack?.count ?? 0) satır"
                if !self.tvHost.isEmpty { self.useTV(self.tvHost) }
            }
        }
    }

    func refreshTranslationStatus() async {
        translationInstalled = await Translator.languageInstalled()
    }

    private func handle(_ event: SubtitlePipeline.Event) {
        switch event {
        case .english(let text, _):
            english = text
        case .turkish(let text, let source, let translateMs, let totalMs):
            turkish = text
            turkishInfo = "\(source) · çeviri \(Int(translateMs)) ms · ilk görülmeden \(Int(totalMs)) ms"
        case .cleared:
            break
        case .log(let line):
            append(line)
        }
    }

    func findTV() {
        let known = tvHost.trimmingCharacters(in: .whitespaces)
        run("TV aranıyor (8787)...") {
            var found = known.isEmpty ? nil : TVLink.find(in: [known])
            if found == nil { found = TVLink.find(in: PS5Discovery.subnetHosts()) }
            guard let found else {
                return "TV bulunamadı. TV'de Dilmaç uygulaması açık mı, aynı ağda mı?"
            }
            await MainActor.run { self.useTV(found) }
            return "TV bulundu: \(found)"
        }
    }

    private func useTV(_ host: String) {
        tvHost = host
        UserDefaults.standard.set(host, forKey: "tv-host") // not a secret: a LAN address
        pipeline?.tv?.close()
        let link = TVLink(host: host)
        link.onState = { [weak self] ok in
            Task { @MainActor in
                self?.tvConnected = ok
                self?.append(ok ? "TV'ye bağlandı: \(host)" : "TV'ye ulaşılamadı (\(host)). TV'de Dilmaç açık mı?")
            }
        }
        pipeline?.tv = link
    }

    /// Shows a short test line on the TV.
    func testTV() {
        guard let tv = pipeline?.tv else { status = "Önce TV'yi bul."; return }
        tv.send(["type": "show", "id": "speech", "text": "Dilmaç: TV bağlantısı tamam.", "ttl": 4, "new": true,
                 "size": 30, "bottom": 60, "opacity": 0.97, "scale": 1, "max_width": 0.8, "old_secs": 3, "fade_ms": 180])
    }

    // MARK: PSN

    func finishLogin(_ result: Result<String, Error>) {
        showLogin = false
        switch result {
        case .failure(let error):
            status = error.localizedDescription
        case .success(let code):
            run("PSN hesabı alınıyor...") {
                let account = try await PSNLogin.account(forCode: code)
                try Keychain.save(account, for: Keychain.psnAccount)
                await MainActor.run { self.account = account }
                return "PSN hesabı: \(account.onlineID)"
            }
        }
    }

    func forgetAccount() {
        Keychain.delete(Keychain.psnAccount)
        account = nil
    }

    // MARK: PS5

    func findPS5() {
        let known = host.trimmingCharacters(in: .whitespaces)
        run("PS5 aranıyor...") {
            var found = known.isEmpty ? nil : PS5Discovery.probe([known])
            if found == nil { found = PS5Discovery.probe(PS5Discovery.subnetHosts(), timeout: 2) }
            guard let found else { return "PS5 bulunamadı. Açık mı, telefon aynı Wi-Fi ağında mı?" }
            await MainActor.run { self.host = found.ip }
            return Self.describe(found)
        }
    }

    func pair() {
        let code = pin.filter(\.isNumber)
        let target = host.trimmingCharacters(in: .whitespaces)
        guard let account else { status = "Önce PSN hesabıyla giriş yap."; return }
        guard code.count == 8, let number = UInt32(code) else {
            status = "Kod 8 haneli olmalı (PS5: Ayarlar > Sistem > Uzaktan Oynatma > Cihazı Bağla)."
            return
        }
        guard !target.isEmpty else { status = "Önce PS5'i bul ya da IP adresini yaz."; return }
        run("Eşleniyor...") {
            guard let found = PS5Discovery.probe([target]) else { return "PS5'e ulaşılamadı (\(target))." }
            guard found.state == .ready else {
                return "PS5 dinlenme modunda. Eşleme için PS5'i aç ve Cihazı Bağla ekranını göster."
            }
            let pairing = try Chiaki.register(host: found.ip, account: account, pin: number)
            try Keychain.save(pairing, for: Keychain.ps5Pairing)
            await MainActor.run { self.pairing = pairing; self.pin = "" }
            return "Eşlendi: \(pairing.name)"
        }
    }

    func forgetPairing() {
        disconnect()
        Keychain.delete(Keychain.ps5Pairing)
        pairing = nil
    }

    // MARK: stream

    func connect() {
        guard let saved = pairing, streamState == .idle else { return }
        streamState = .connecting
        latest.reset()
        decodedFrames = 0
        lastCount = 0

        let stream = ChiakiStream()
        let decoder = H264Decoder()
        let renderer = videoView.renderer
        let latest = self.latest
        let pipeline = self.pipeline
        decoder.onFrame = { pixelBuffer in
            latest.set(pixelBuffer)
            FrameDisplay.show(pixelBuffer, on: renderer)
            pipeline?.offer(pixelBuffer)
        }
        stream.onFrame = { accessUnit, _ in decoder.decode(accessUnit) }
        stream.onEvent = { [weak self] event in
            Task { @MainActor in self?.handle(event) }
        }
        self.stream = stream

        run("Bağlanılıyor...") {
            var pairing = saved
            // The router may have given the PS5 a new address: look for it on the subnet if it does not answer.
            var found = PS5Discovery.probe([pairing.host])
            if found == nil { found = PS5Discovery.probe(PS5Discovery.subnetHosts(), timeout: 2) }
            guard let current = found else { throw Chiaki.Failure(message: "PS5 bulunamadı. Açık ya da dinlenme modunda mı?") }
            if current.ip != pairing.host {
                pairing.host = current.ip
                try Keychain.save(pairing, for: Keychain.ps5Pairing)
                let updated = pairing
                await MainActor.run { self.pairing = updated; self.host = updated.host }
            }
            if current.state == .standby {
                await MainActor.run { self.status = "PS5 dinlenme modunda; uyandırılıyor..." }
                try Chiaki.wake(pairing)
                try await Self.waitUntilReady(pairing.host)
            }
            try stream.open(pairing, resolution: .p720, fps: 30)
            return "Oturum açıldı, ilk kare bekleniyor..."
        } failed: { [weak self] in
            self?.stream = nil
            self?.streamState = .idle
        }
    }

    func disconnect() {
        guard let stream else { return }
        self.stream = nil
        streamState = .idle
        pipeline?.stop()
        Task.detached { stream.close() }
    }

    func saveFrame() {
        guard let frame = latest.get() else { status = "Henüz kare yok."; return }
        do {
            lastSaved = try FrameStore.save(frame)
            status = "Kare kaydedildi (Dosyalar > iPhone'umda > Dilmaç > frames)."
        } catch {
            status = "Kare kaydedilemedi: \(error.localizedDescription)"
        }
    }

    // MARK: private

    private func handle(_ event: ChiakiStream.Event) {
        switch event {
        case .connected:
            streamState = .connected
            status = "Bağlandı."
            append("PS5'e bağlandı.")
        case .ended(let reason):
            stream = nil
            streamState = .idle
            var text = "Akış bitti: \(reason)"
            if reason.lowercased().contains("remote"), reason.lowercased().contains("use") {
                text += " (başka bir Uzaktan Oynatma bağlantısı açıksa kapat)"
            }
            status = text
            append(text)
        case .pinRequested:
            status = "PS5 bir giriş PIN'i istiyor (bu sürümde desteklenmiyor)."
            append(status)
        }
    }

    private func tick() {
        let count = latest.count
        fps = count - lastCount
        lastCount = count
        decodedFrames = count
        deviceInfo = Self.deviceText()
        // Connected but nothing decodes yet (the first keyframe was missed): ask for one now and then.
        if streamState == .connected, count == 0, Date().timeIntervalSince(lastKeyframeRequest) > 3 {
            lastKeyframeRequest = Date()
            stream?.requestKeyframe()
        }
    }

    /// Phone heat and battery, to judge whether it can run for a whole session.
    private static func deviceText() -> String {
        let heat: String
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: heat = "normal"
        case .fair: heat = "ılık"
        case .serious: heat = "sıcak"
        case .critical: heat = "çok sıcak"
        @unknown default: heat = "?"
        }
        let device = UIDevice.current
        let level = device.batteryLevel >= 0 ? "%\(Int(device.batteryLevel * 100))" : "?"
        let charging = device.batteryState == .charging || device.batteryState == .full ? " (şarjda)" : ""
        return "Isı: \(heat) · Pil: \(level)\(charging)"
    }

    private func append(_ line: String) {
        let stamp = Date().formatted(date: .omitted, time: .standard)
        log.append("\(stamp) \(line)")
        if log.count > 300 { log.removeFirst(log.count - 300) }
    }

    /// Runs blocking work off the main thread, with a busy flag and the result or error in `status`.
    /// `work` is @Sendable so it never inherits the main actor: the blocking calls in it stay off the main thread.
    private func run(_ message: String, _ work: @escaping @Sendable () async throws -> String,
                     failed: (@MainActor () -> Void)? = nil) {
        busy = true
        status = message
        append(message)
        Task.detached {
            do {
                let result = try await work()
                await MainActor.run { self.busy = false; self.status = result; self.append(result) }
            } catch {
                let text = error.localizedDescription
                await MainActor.run { self.busy = false; self.status = text; self.append(text); failed?() }
            }
        }
    }

    nonisolated private static func waitUntilReady(_ host: String) async throws {
        let end = Date().addingTimeInterval(60)
        while Date() < end {
            try await Task.sleep(for: .seconds(2))
            if PS5Discovery.probe([host])?.state == .ready {
                try await Task.sleep(for: .seconds(3)) // let the boot screen come up
                return
            }
        }
        throw Chiaki.Failure(message: "PS5 uyanmadı. PS5'te Ayarlar > Sistem > Güç Tasarrufu > Dinlenme Modunda "
                             + "Kullanılabilen Özellikler altında 'İnternete bağlı kal' ve 'PS5'i ağ üzerinden "
                             + "açmayı etkinleştir' açık mı?")
    }

    nonisolated private static func describe(_ s: PS5Discovery.Status) -> String {
        switch s.state {
        case .ready: return "Açık · \(s.name) (\(s.ip))" + (s.runningApp.isEmpty ? "" : " · \(s.runningApp)")
        case .standby: return "Dinlenme modunda · \(s.name) (\(s.ip))"
        case .other(let head): return "\(s.name) (\(s.ip)) yanıt verdi: \(head)"
        }
    }
}

/// The latest decoded frame and a frame counter, shared between chiaki's video thread and the UI.
final class LatestFrame: @unchecked Sendable {
    private let lock = NSLock()
    private var frame: CVPixelBuffer?
    private var frames = 0

    func set(_ pixelBuffer: CVPixelBuffer) { lock.withLock { frame = pixelBuffer; frames += 1 } }
    func get() -> CVPixelBuffer? { lock.withLock { frame } }
    var count: Int { lock.withLock { frames } }
    func reset() { lock.withLock { frame = nil; frames = 0 } }
}
