// Port of the PackTest part of pc/tests/test_pack.py, plus a check against the real GTA VI pack and glossary.
@testable import DilmacCore
import Foundation
import XCTest

private let packLines: [(String, String)] = [
    ("A lot of fucking guys here, Bill.", "Burada bir sürü adam var lan, Bill."),
    ("Don't get cute.", "Akıllılık etme."),
    ("Meet me at the trailer.", "Karavanda buluşalım."),
    ("Go to the docks", "Rıhtıma git"),
    ("Okay.", "Tamam."),
]

final class PackTests: XCTestCase {
    private let p = Pack(gameTitle: "Grand Theft Auto VI", lines: packLines)

    func testExactMatchAndPunctuation() {
        XCTAssertEqual(p.lookup("Meet me at the trailer."), "Karavanda buluşalım.")
        XCTAssertEqual(p.lookup("meet me at the trailer"), "Karavanda buluşalım.")
        XCTAssertEqual(p.lookup("Go to the docks!"), "Rıhtıma git!")
    }

    func testOcrError() {
        XCTAssertEqual(p.lookup("A lot of fucking guys here, Bills ̄"), "Burada bir sürü adam var lan, Bill.")
        XCTAssertEqual(p.lookup("A Iot of fucking guys here, BiII."), "Burada bir sürü adam var lan, Bill.")
    }

    func testShortDifferentLineNotFound() {
        XCTAssertNil(p.lookup("Okey"))
        XCTAssertNil(p.lookup("A lot of cops here, Bill."))
    }

    func testSpeaker() {
        let q = Pack(gameTitle: "x", lines: packLines, speakers: [
            "A lot of fucking guys here, Bill.": "Dunk", "Meet me at the trailer.": "Lucia", "Satır pakette yok.": "Jason",
        ])
        XCTAssertEqual(q.speaker("A Iot of fucking guys here, BiII."), "Dunk")
        XCTAssertEqual(q.speaker("meet me at the trailer"), "Lucia")
        XCTAssertNil(q.speaker("Don't get cute."))
        XCTAssertNil(q.speaker("Satır pakette yok."))
        q.add(lines: [], speakers: ["Don't get cute.": "pedestrian"])
        XCTAssertEqual(q.speaker("Don't get cute."), "pedestrian")
        q.add(lines: [], speakers: ["Don't get cute.": "Jason"])
        XCTAssertEqual(q.speaker("Don't get cute."), "pedestrian")
    }

    func testSpeakerBySentence() {
        let q = Pack(gameTitle: "x", lines: packLines, speakers: ["Meet me at the trailer.": "Lucia"])
        XCTAssertNil(q.speaker("Don't get cute. Meet me at the trailer."))
        XCTAssertEqual(q.speakerBySentence("Don't get cute. Meet me at the trailer."), "Lucia")
        XCTAssertEqual(q.speakerBySentence("LUCIA: Meet me at the trailer."), "Lucia")
        XCTAssertNil(q.speakerBySentence("Nothing anybody knows about at all."))
    }
}

/// The parts of the glossary and the pack that the LLM prompt uses (pc/translation.py Glossary, pc/pack.py similar).
final class PromptDataTests: XCTestCase {
    private func glossary(_ json: String) throws -> Glossary { try Glossary(data: Data(json.utf8)) }
    private func pairs(_ p: [(en: String, tr: String)]) -> [String] { p.map { "\($0.en)=\($0.tr)" } }

    func testRelevantNamesAndTerms() throws {
        let g = try glossary(#"{"game": "x", "keep": ["Cal", "Cal Hampton", "Lucia"], "terms": {"homie": "kanka"}}"#)
        let r = g.relevant("Cal Hampton told the homie")
        XCTAssertEqual(r.names, ["Cal Hampton"])
        XCTAssertEqual(pairs(r.terms), ["homie=kanka"])
        let none = g.relevant("calhampton")
        XCTAssertEqual(none.names, [])
        XCTAssertTrue(none.terms.isEmpty)
    }

    func testTermsKeepFileOrderAndTermsWin() throws {
        let g = try glossary(#"{"game": "x", "style": "Argo kalsın.", "address_terms": {"man": "lan", "bro": "kardeş"}, "terms": {"zebra": "z", "bro": "kanka", "apple": "a"}}"#)
        XCTAssertEqual(g.style, "Argo kalsın.")
        XCTAssertEqual(g.terms, ["man": "lan", "bro": "kanka", "zebra": "z", "apple": "a"])
        XCTAssertEqual(pairs(g.relevant("apple zebra bro man").terms), ["man=lan", "bro=kanka", "zebra=z", "apple=a"])
    }

    func testNicknameMapIsCaseSensitive() throws {
        let g = try glossary(#"{"game": "x", "nicknames": {"Lu": "Lucia", "Jay": "Jason"}}"#)
        XCTAssertEqual(g.nicknameMap("Jay and Lu, wait").map { "\($0.nick)=\($0.name)" }, ["Lu=Lucia", "Jay=Jason"])
        XCTAssertTrue(g.nicknameMap("lu wait").isEmpty)
        XCTAssertTrue(Glossary.occurs("lu", in: "Lu, wait"))
        XCTAssertFalse(Glossary.occurs("Lu", in: "Lu's"))
    }

    func testSimilar() {
        let p = Pack(gameTitle: "x", lines: packLines)
        let s = p.similar("A lot of fucking cops here, Bill.")
        XCTAssertEqual(s.first?.en, "A lot of fucking guys here, Bill.")
        XCTAssertEqual(s.first?.tr, "Burada bir sürü adam var lan, Bill.")
        XCTAssertFalse(p.similar("A lot of fucking guys here, Bill.").contains { $0.en == "A lot of fucking guys here, Bill." })
        XCTAssertTrue(p.similar("zzz").isEmpty)
    }

    func testSimilarOrderAndThreshold() {
        let p = Pack(gameTitle: "x", lines: [
            ("Drive the boat now.", "a"), ("Drive the car now.", "b"), ("Drive fast.", "c"), ("Boat party tonight.", "d"),
        ])
        // Scores: 1.0, 0.5, 0.5 (a tie: the earlier line first), 0.41.
        XCTAssertEqual(pairs(p.similar("Drive the boat!")), ["Drive the boat now.=a", "Drive the car now.=b", "Drive fast.=c"])
        XCTAssertEqual(pairs(p.similar("Drive the boat!", n: 4)).last, "Boat party tonight.=d")
        XCTAssertEqual(pairs(p.similar("Drive the boat!", n: 1)), ["Drive the boat now.=a"])
        XCTAssertEqual(pairs(p.similar("Drive the boat!", n: 4, threshold: 0.45)).count, 3)
        XCTAssertEqual(pairs(p.similar("Drive the boat!", threshold: 0.6)), ["Drive the boat now.=a"])
    }

    func testSimilarOnHashedPackIsEmpty() throws {
        let p = Pack(gameTitle: "x")
        try p.add(file: Data(#"{"game": "x", "format": "hashed-v1", "lines": {}, "h": {"0123456789abcdef0123": "Karavanda buluşalım."}}"#.utf8))
        XCTAssertEqual(p.similar("Meet me at the trailer.").count, 0)
    }
}

/// The real files the app ships (pc/packs, pc/glossary).
final class GameDataTests: XCTestCase {
    private static let repo = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    func testGta6PackLoads() throws {
        let data = try Data(contentsOf: Self.repo.appendingPathComponent("pc/packs/gta6.json"))
        XCTAssertEqual(Pack.gameTitle(ofFile: data), "Grand Theft Auto VI")
        let pack = Pack(gameTitle: "Grand Theft Auto VI")
        try pack.add(file: data)
        XCTAssertGreaterThan(pack.count, 5000)
    }

    func testGta6Glossary() throws {
        let g = try Glossary(data: Data(contentsOf: Self.repo.appendingPathComponent("pc/glossary/gta6.json")))
        XCTAssertEqual(g.screen["center_tol"], 0.02)
        XCTAssertEqual(g.screen["objective_y"], 0.925)
        XCTAssertEqual(g.colors["Lucia"], "#FF9AD5")
        XCTAssertTrue(g.lexiconExtras.contains("Vice City"))
    }
}
