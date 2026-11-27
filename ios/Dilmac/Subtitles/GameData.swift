import DilmacCore
import Foundation

/// The game data shipped in the app: pc/glossary/*.json and pc/packs/*.json, copied into the bundle as the folders
/// "glossary" and "packs" (folder references in the Xcode project, so there is one copy in the repo).
enum GameData {
    struct Loaded {
        let glossary: Glossary?
        let pack: Pack?
        let lexicon: Lexicon
    }

    /// v0.2 plays GTA VI only (the only game with a glossary and a pack).
    static func load(game: String = "Grand Theft Auto VI") -> Loaded {
        let bundle = Bundle.main
        let glossaries = (bundle.urls(forResourcesWithExtension: "json", subdirectory: "glossary") ?? [])
            .compactMap { try? Glossary(data: Data(contentsOf: $0)) }
        let glossary = glossaries.first { g in g.names.map(TextUtil.textKey).contains(TextUtil.textKey(game)) }
        let titles = Set((glossary?.names ?? [game]).map(TextUtil.textKey))

        let pack = Pack(gameTitle: glossary?.gameTitle ?? game)
        // Sorted by file name like pc/pack.py (the first file with a line wins).
        let files = (bundle.urls(forResourcesWithExtension: "json", subdirectory: "packs") ?? [])
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        for url in files {
            guard let data = try? Data(contentsOf: url), let title = Pack.gameTitle(ofFile: data),
                  titles.contains(TextUtil.textKey(title)) else { continue }
            try? pack.add(file: data)
        }
        return Loaded(glossary: glossary, pack: pack.count > 0 ? pack : nil,
                      lexicon: Lexicon((glossary?.lexiconExtras ?? []) + [game]))
    }
}
