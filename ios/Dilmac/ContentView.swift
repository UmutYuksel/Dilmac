import SwiftUI
import Translation

/// Test screen: PSN account, PS5 pairing, live frames, subtitles (OCR -> Turkish -> TV).
struct ContentView: View {
    @StateObject private var model = AppModel()
    /// Set to ask iOS to download the English -> Turkish translation model (shows the system sheet).
    @State private var translationDownload: TranslationSession.Configuration?

    var body: some View {
        NavigationStack {
            Form {
                videoSection
                subtitleSection
                accountSection
                ps5Section
                if !model.status.isEmpty {
                    Section("Durum") { Text(model.status).font(.callout) }
                }
                Section("Günlük") {
                    ScrollView {
                        Text(model.log.suffix(80).joined(separator: "\n"))
                            .font(.caption2.monospaced())
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                    .frame(height: 180)
                }
            }
            .navigationTitle("Dilmaç")
            .disabled(model.busy && model.streamState == .idle)
            .overlay { if model.busy { ProgressView() } }
        }
        .translationTask(translationDownload) { session in
            try? await session.prepareTranslation()
            await model.refreshTranslationStatus()
        }
        .sheet(isPresented: $model.showLogin) {
            NavigationStack {
                PSNLoginView { model.finishLogin($0) }
                    .ignoresSafeArea(edges: .bottom)
                    .navigationTitle("PSN girişi")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Vazgeç") { model.showLogin = false }
                        }
                    }
            }
        }
    }

    private var videoSection: some View {
        Section {
            VideoView(view: model.videoView)
                .aspectRatio(16 / 9, contentMode: .fit)
                .listRowInsets(EdgeInsets())
            HStack {
                switch model.streamState {
                case .idle:
                    Button("Bağlan", systemImage: "play.fill") { model.connect() }
                        .disabled(model.pairing == nil)
                case .connecting:
                    ProgressView()
                    Text("Bağlanılıyor")
                    Spacer()
                    Button("Kes", role: .destructive) { model.disconnect() }
                case .connected:
                    Text("\(model.fps) fps · \(model.decodedFrames) kare").monospacedDigit()
                    Spacer()
                    Button("Kareyi kaydet", systemImage: "camera") { model.saveFrame() }
                        .disabled(model.decodedFrames == 0)
                    Button("Kes", systemImage: "stop.fill", role: .destructive) { model.disconnect() }
                        .labelStyle(.iconOnly)
                }
            }
            .buttonStyle(.borderless)
        }
    }

    private var subtitleSection: some View {
        Section("Altyazı") {
            if model.subtitlesReady {
                if !model.english.isEmpty {
                    Text(model.english).font(.callout).foregroundStyle(.secondary)
                }
                if !model.turkish.isEmpty {
                    Text(model.turkish).font(.body.weight(.semibold))
                    Text(model.turkishInfo).font(.caption).foregroundStyle(.secondary)
                }
                Text(model.packInfo).font(.caption).foregroundStyle(.secondary)
            } else {
                HStack { ProgressView(); Text("Oyun paketi ve sözlük yükleniyor") }
            }
            switch model.translationInstalled {
            case .some(false):
                Button("Türkçe çeviri dilini indir", systemImage: "arrow.down.circle") {
                    if translationDownload == nil {
                        translationDownload = .init(source: Translator.source, target: Translator.target)
                    } else {
                        translationDownload?.invalidate()
                    }
                }
                Text("Pakette olmayan satırlar Apple'ın cihaz üzerindeki çevirisiyle çevrilir; bunun için dil bir kez indirilir.")
                    .font(.footnote).foregroundStyle(.secondary)
            case .some(true):
                LabeledContent("Çeviri dili", value: "İngilizce → Türkçe hazır")
            case .none:
                EmptyView()
            }
            HStack {
                TextField("TV IP adresi (boşsa ağda aranır)", text: $model.tvHost)
                    .keyboardType(.decimalPad)
                Button("TV'yi bul") { model.findTV() }
            }
            if let connected = model.tvConnected {
                LabeledContent("TV", value: connected ? "Bağlı" : "Ulaşılamıyor")
            }
            Button("TV'de dene") { model.testTV() }
                .disabled(model.tvHost.isEmpty)
            if !model.deviceInfo.isEmpty {
                Text(model.deviceInfo).font(.caption).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.borderless)
    }

    private var accountSection: some View {
        Section("PSN hesabı") {
            if let account = model.account {
                LabeledContent("Hesap", value: account.onlineID)
                Button("Hesabı unut", role: .destructive) { model.forgetAccount() }
            } else {
                Button("PSN ile giriş yap") { model.showLogin = true }
                Text("Sony'nin kendi giriş sayfası açılır; şifre yalnızca Sony'ye gider. Uygulama sadece hesap "
                     + "kimliğini Anahtar Zinciri'nde saklar.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private var ps5Section: some View {
        Section("PS5") {
            if let pairing = model.pairing {
                LabeledContent("Eşli", value: pairing.name)
                LabeledContent("Adres", value: pairing.host)
                Button("Eşlemeyi sil", role: .destructive) { model.forgetPairing() }
            } else {
                HStack {
                    TextField("IP adresi (boşsa ağda aranır)", text: $model.host)
                        .keyboardType(.decimalPad)
                    Button("Bul") { model.findPS5() }
                }
                TextField("8 haneli kod (Ayarlar > Sistem > Uzaktan Oynatma > Cihazı Bağla)", text: $model.pin)
                    .keyboardType(.numberPad)
                Button("Eşle") { model.pair() }
                    .disabled(model.account == nil)
            }
        }
    }
}
