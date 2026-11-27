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
