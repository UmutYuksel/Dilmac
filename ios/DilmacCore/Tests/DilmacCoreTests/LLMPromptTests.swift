// LLMPrompt against pc/translation.py: the fixtures are made by ios/scripts/make-prompt-fixtures.py.
@testable import DilmacCore
import Foundation
import XCTest

final class LLMPromptTests: XCTestCase {
    private struct Fixtures: Decodable {
        struct Prompt: Decodable {
            let sentences: [String]
            let speaker: String?
            let history: [[String]]
            let names: [String]
            let game: String
            let glossary: Bool
            let pack: Bool
            let prompt: String
        }
        struct Reply: Decodable {
            let raw: String
            let n: Int
            let out: [String]?
            let error: Bool?
        }
        let system: String
        let prompt_version: Int
        let glossary_json: String
        let pack: [[String]]
        let prompts: [Prompt]
        let replies: [Reply]
    }

    private static let fixtures: Fixtures = {
        let url = Bundle.module.url(forResource: "prompt-fixtures", withExtension: "json")!
        return try! JSONDecoder().decode(Fixtures.self, from: Data(contentsOf: url))
    }()

    func testSystemMatchesPython() {
        XCTAssertEqual(LLMPrompt.system, Self.fixtures.system)
        XCTAssertEqual(LLMPrompt.version, Self.fixtures.prompt_version)
    }

    func testPromptMatchesPython() throws {
        let f = Self.fixtures
        let glossary = try Glossary(data: Data(f.glossary_json.utf8))
        let pack = Pack(gameTitle: "Test Game", lines: f.pack.map { ($0[0], $0[1]) })
        XCTAssertEqual(f.prompts.count, 8)
        for c in f.prompts {
            let prompt = LLMPrompt.user(
                sentences: c.sentences, speaker: c.speaker, history: c.history.map { ($0[0], $0[1]) }, game: c.game,
                glossary: c.glossary ? glossary : nil, learnedNames: c.names, pack: c.pack ? pack : nil)
            XCTAssertEqual(prompt, c.prompt, "sentences: \(c.sentences)")
        }
    }

    func testReplySeqCases() {
        XCTAssertEqual(Self.fixtures.replies.count, 15)
        for r in Self.fixtures.replies {
            if let out = r.out {
                XCTAssertEqual(try? LLMPrompt.replySeq(r.raw, count: r.n), out, "raw: \(r.raw)")
            } else {
                XCTAssertThrowsError(try LLMPrompt.replySeq(r.raw, count: r.n), "raw: \(r.raw)") {
                    XCTAssertTrue($0 is LLMPrompt.ReplyError)
                }
            }
        }
    }

    func testReplySeqWrongCountThrows() {
        XCTAssertThrowsError(try LLMPrompt.replySeq("[\"a\"]", count: 2))
    }
}
