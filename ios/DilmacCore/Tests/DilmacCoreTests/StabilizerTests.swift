// Port of pc/tests/test_stabilizer.py (same cases, same expectations).
@testable import DilmacCore
import XCTest

private let lexicon = Lexicon(["Lucia", "Jason"])

final class StabilizerTests: XCTestCase {
    private var ms = 0.0
    private var d: Stabilizer!

    override func setUp() {
        ms = 0
        d = Stabilizer(lexicon: lexicon, clock: { [unowned self] in self.ms / 1000 })
    }

    @discardableResult
    private func frame(_ text: String, _ step: Double = 100, unchanged: Bool = false) -> Stabilizer.Decision {
        ms += step
        return d.feed(text, unchanged: unchanged)
    }

    func testCleanPunctuatedSentFirstFrame() {
        let k = frame("Hmm, who's this?")
        XCTAssertEqual(k.kind, .send)
        XCTAssertEqual(k.text, "Hmm, who's this?")
        for t in ["Hmm, who's this?", "Hmm, whos this?", "Hmm, who's"] { XCTAssertEqual(frame(t).kind, .none) }
    }

    func testGarbledFadeInNotSent() {
        XCTAssertEqual(frame("Ooo wee, I sure can't wait till veghayectaervffeoacgv").kind, .none)
        XCTAssertEqual(frame("oooweeisure940119aittillvoehaveourbunkerready").kind, .none)
        let k = frame("Ooo wee, I sure can't wait till we have our bunker ready.")
        XCTAssertEqual(k.kind, .send)
        XCTAssertTrue(k.text.contains("bunker ready"))
    }

    func testCleanLineWithoutPunctWaits() {
        XCTAssertEqual(frame("Hey, what are you", 150).kind, .none)
        XCTAssertEqual(frame("Hey, what are you", 150).kind, .none)
        XCTAssertEqual(frame("Hey, what are you", 150).kind, .send)
    }

    func testContinuationIsNewLine() {
        XCTAssertEqual(frame("Look, it's my fault!").kind, .send)
        let k = frame("Look, it's my fault! I should have called you.")
        XCTAssertEqual(k.kind, .send)
        XCTAssertTrue(k.text.hasSuffix("called you."))
    }

    func testEmptyRegionClears() {
        frame("Just gimme a second.")
        XCTAssertEqual(frame("", 300).kind, .none)
        XCTAssertEqual(frame("", 300).kind, .none)
        XCTAssertEqual(frame("", 300).kind, .clear)
    }

    func testHoldsIfRegionUnchanged() {
        frame("Just gimme a second.")
        for _ in 0..<10 { XCTAssertEqual(frame("", 300, unchanged: true).kind, .none) }
        XCTAssertEqual(frame("", 1200, unchanged: true).kind, .clear)
    }

    func testReturningLineNotRetranslated() {
        frame("Hi. Yes, I'm here.")
        frame("", 900)
        XCTAssertNil(d.sentText)
        let k = frame("Hi. Yes, I'm here.", 300)
        XCTAssertEqual(k.kind, .back)
        XCTAssertEqual(k.text, "Hi. Yes, I'm here.")
    }

    func testUnnamedRereadIsSameLine() {
        XCTAssertEqual(frame("JASON: Let's go, now.").kind, .send)
        XCTAssertEqual(frame("Let's go, now.").kind, .none)
    }

    func testCorrection() {
        let kinds = (0..<3).map { _ in frame("Take the eedboat to the docks.", 150).kind }
        XCTAssertEqual(kinds, [.none, .none, .send])
        XCTAssertEqual(frame("Take the speedboat to the docks.").kind, .none)
        let k = frame("Take the speedboat to the docks.")
        XCTAssertEqual(k.kind, .correct)
        XCTAssertEqual(k.text, "Take the speedboat to the docks.")
        XCTAssertEqual(frame("Take the speedboat to the docks.").kind, .none)
    }

    func testFailedTranslationRetriedAfterASecond() {
        frame("Look, it's my fault!")
        d.forget()
        XCTAssertEqual(frame("Look, it's my fault!").kind, .none)
        XCTAssertEqual(frame("Look, it's my fault!", 1000).kind, .send)
    }
}
