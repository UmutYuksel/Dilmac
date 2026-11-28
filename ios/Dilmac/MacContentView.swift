#if os(macOS)
import SwiftUI
import Translation

/// The Mac window: the PS5 picture with the subtitle region outlined on the left; status cards, start / stop and the
/// live subtitle list on the right. PSN sign-in, pairing and settings open as sheets.
struct MacContentView: View {
    @StateObject private var model = AppModel()
    @State private var showSettings = false
    @State private var showPairing = false
    @State private var trial = ""
    /// Set to ask macOS to download the English -> Turkish translation model (shows the system sheet).
    @State private var translationDownload: TranslationSession.Configuration?

    var body: some View {
        HStack(spacing: 0) {
            picture
                .frame(minWidth: 560, maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            sidebar
                .frame(width: 380)
        }
        .frame(minWidth: 980, minHeight: 620)
        .toolbar {
            ToolbarItemGroup {
                Button("PSN ve PS5", systemImage: "gamecontroller") { showPairing = true }
                Button("Ayarlar", systemImage: "gearshape") { showSettings = true }
            }
        }
        .navigationTitle("Dilmaç")
        .translationTask(translationDownload) { session in
            try? await session.prepareTranslation()
            await model.refreshTranslationStatus()
        }
        .sheet(isPresented: $showSettings) { MacSettingsView(model: model) }
        .sheet(isPresented: $showPairing) {
            VStack(alignment: .trailing) {
                Form {
                    AccountSection(model: model)
                    PS5Section(model: model)
                    if !model.status.isEmpty { Section("Durum") { Text(model.status).font(.callout) } }
                }
                .formStyle(.grouped)
                .disabled(model.busy)
                Button("Kapat") { showPairing = false }
                    .keyboardShortcut(.defaultAction)
                    .padding()
            }
            .frame(width: 520, height: 460)
            .sheet(isPresented: $model.showLogin) {
                VStack(spacing: 0) {
                    PSNLoginView { model.finishLogin($0) }
                    HStack {
                        Spacer()
                        Button("Vazgeç") { model.showLogin = false }.keyboardShortcut(.cancelAction).padding(8)
                    }
                }
                .frame(width: 520, height: 680)
            }
        }
    }

    // MARK: left

    private var picture: some View {
        VStack(spacing: 0) {
            VideoView(view: model.videoView)
                .aspectRatio(16 / 9, contentMode: .fit)
                .overlay { regionOutline }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(.black)
            HStack(spacing: 12) {
                switch model.streamState {
                case .idle:
                    Button("Bağlan", systemImage: "play.fill") { model.connect() }
                        .disabled(model.pairing == nil || model.busy)
                    if model.pairing == nil { Text("Önce PS5'i eşle (PSN ve PS5).").foregroundStyle(.secondary) }
                case .connecting:
                    ProgressView().controlSize(.small)
                    Text("Bağlanılıyor")
                    Button("Kes", role: .destructive) { model.disconnect() }
                case .connected:
                    Button("Durdur", systemImage: "stop.fill", role: .destructive) { model.disconnect() }
                    Text("\(model.fps) fps · \(model.decodedFrames) kare").monospacedDigit().foregroundStyle(.secondary)
                    Button("Kareyi kaydet", systemImage: "camera") { model.saveFrame() }
                        .disabled(model.decodedFrames == 0)
                }
                Spacer()
                if !model.status.isEmpty {
                    Text(model.status).font(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                }
            }
            .padding(10)
        }
    }

    /// The region the subtitles are read from, on top of the picture.
    private var regionOutline: some View {
        GeometryReader { geo in
            if let r = model.regionBox {
                Rectangle()
                    .stroke(.yellow.opacity(0.8), style: StrokeStyle(lineWidth: 1.5, dash: [6, 4]))
                    .frame(width: r.width * geo.size.width, height: r.height * geo.size.height)
                    .offset(x: r.minX * geo.size.width, y: r.minY * geo.size.height)
            }
        }
        .allowsHitTesting(false)
    }

    // MARK: right

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 10) {
            card("PS5", systemImage: "playstation.logo") {
                Text(ps5Text)
                if let pairing = model.pairing { Text("\(pairing.name) · \(pairing.host)").foregroundStyle(.secondary) }
            }
            card("TV", systemImage: "tv") {
                Text(tvText)
                HStack {
                    Button("TV'yi bul") { model.findTV() }
                    Button("TV'de dene") { model.testTV() }.disabled(model.tvHost.isEmpty)
                }
            }
            card("Çeviri", systemImage: "character.bubble") {
                Text(model.engineInfo.isEmpty ? "Hazırlanıyor..." : model.engineInfo)
                Text(model.packInfo).foregroundStyle(.secondary)
                if model.translationInstalled == false {
                    Button("Türkçe modeli indir", systemImage: "arrow.down.circle") {
                        if translationDownload == nil {
                            translationDownload = .init(source: Translator.source, target: Translator.target)
                        } else {
                            translationDownload?.invalidate()
                        }
                    }
                    Text("Apple Çeviri, LLM geciktiğinde ya da kapalıyken devreye girer; dil bir kez indirilir.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                HStack {
                    TextField("Çeviri dene (İngilizce bir cümle)", text: $trial)
                        .onSubmit { model.tryTranslate(trial) }
                    Button("Çevir") { model.tryTranslate(trial) }.disabled(trial.isEmpty || !model.subtitlesReady)
                }
                if !model.deviceInfo.isEmpty { Text(model.deviceInfo).font(.caption).foregroundStyle(.secondary) }
            }
            Text("Altyazılar").font(.headline).padding(.top, 4)
            if model.lines.isEmpty {
                Text("Henüz çevrilen satır yok.").foregroundStyle(.secondary)
                Spacer()
            } else {
                List(model.lines) { line in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(line.turkish).font(.body.weight(.semibold))
                        Text(line.english).font(.callout).foregroundStyle(.secondary)
                        Text("\(line.source) · \(line.translateMs) ms · "
                             + line.time.formatted(date: .omitted, time: .standard))
                            .font(.caption).foregroundStyle(.tertiary)
                    }
                    .textSelection(.enabled)
                    .padding(.vertical, 2)
                }
                .listStyle(.inset)
            }
        }
        .padding(12)
    }

    private var ps5Text: String {
        switch model.streamState {
        case .idle: return model.pairing == nil ? "Eşli PS5 yok" : "Bağlı değil"
        case .connecting: return "Bağlanılıyor"
        case .connected: return "Bağlı · \(model.fps) fps"
        }
    }

    private var tvText: String {
        if model.tvHost.isEmpty { return "TV seçilmedi" }
        switch model.tvConnected {
        case .some(true): return "Bağlı · \(model.tvHost)"
        case .some(false): return "Ulaşılamıyor · \(model.tvHost)"
        case .none: return model.tvHost
        }
    }

    private func card<Content: View>(_ title: String, systemImage: String,
                                     @ViewBuilder _ content: () -> Content) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label(title, systemImage: systemImage).font(.headline)
        }
    }
}

/// TV address, translation engine, model and Ollama address; the log.
struct MacSettingsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    // Edited here, applied on Kaydet.
    @State private var engine = Translator.Engine.llm
    @State private var localModel = ""
    @State private var localURL = ""

    var body: some View {
        VStack(alignment: .trailing, spacing: 0) {
            Form {
                Section("TV") {
                    HStack {
                        TextField("TV IP adresi (boşsa ağda aranır)", text: $model.tvHost)
                        Button("TV'yi bul") { model.findTV() }
                    }
                }
                Section("Çeviri") {
                    Picker("Motor", selection: $engine) {
                        Text("Yerel LLM (Ollama)").tag(Translator.Engine.llm)
                        Text("Apple Çeviri").tag(Translator.Engine.apple)
                    }
                    TextField("Model", text: $localModel)
                        .disabled(engine != .llm)
                    TextField("Ollama adresi", text: $localURL)
                        .disabled(engine != .llm)
                    Text("LLM 1,5 sn içinde cevap vermezse Apple Çeviri de denenir, önce gelen gösterilir; LLM'in geç "
                         + "gelen çevirisi bir sonraki sefer için saklanır.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("Günlük") {
                    ScrollView {
                        Text(model.log.suffix(120).joined(separator: "\n"))
                            .font(.caption.monospaced())
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                    .frame(height: 200)
                }
            }
            .formStyle(.grouped)
            HStack {
                Button("Vazgeç") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Kaydet") {
                    model.engine = engine
                    model.localModel = localModel
                    model.localURL = localURL
                    model.applyEngine()
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }
            .padding()
        }
        .frame(width: 560, height: 600)
        .onAppear {
            engine = model.engine
            localModel = model.localModel
            localURL = model.localURL
        }
    }
}
#endif
