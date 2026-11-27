// Port of SplitTest in pc/tests/test_ocr.py (GTA VI layout).
@testable import DilmacCore
import XCTest

final class LayoutTests: XCTestCase {
    private let d = Layout(left: 0.15, top: 0.75, width: 0.68, height: 0.22, centerTol: 0.02, objectiveY: 0.925)

    /// A line given relative to the picture, converted to the region like OCR returns it.
    private func line(_ t: String, _ x: Double, _ y: Double, _ w: Double, _ h: Double = 0.029) -> OcrLine {
        let b = d.toRegion(Box(x, y, w, h))
        return OcrLine(t, b.x, b.y, b.w, b.h)
    }

    func testSpeechObjectivePhoneSplit() {
        let a = d.classify([
            line("Girl. Stop. Rent is coming.", 0.624, 0.759, 0.125),
            line("You know, you really didn't have to go this far, man.", 0.287, 0.885, 0.426),
            line("Go to the docks", 0.447, 0.950, 0.107, 0.0175),
        ])
        XCTAssertEqual(Layout.joined(a.speech), "You know, you really didn't have to go this far, man.")
        XCTAssertEqual(Layout.joined(a.objective), "Go to the docks")
        XCTAssertEqual(a.dropped.map(\.text), ["Girl. Stop. Rent is coming."])
    }

    func testColorSplitLineMerges() {
        let a = d.classify([line("Pick up", 0.45, 0.949, 0.045, 0.02), line("Dr. Bill", 0.498, 0.949, 0.052, 0.02)])
        XCTAssertEqual(Layout.joined(a.objective), "Pick up Dr. Bill")
    }

    func testAdjacentSignDropped() {
        let a = d.classify([
            line("You, give me the money.", 0.399, 0.833, 0.204),
            line("PlßWASSER", 0.629, 0.830, 0.066, 0.031),
            line("Yeah, yeah, whatever you want!", 0.372, 0.886, 0.256),
        ])
        XCTAssertEqual(Layout.joined(a.speech), "You, give me the money. Yeah, yeah, whatever you want!")
        let k = Layout.bounding(a.speech)!
        XCTAssertEqual(k.y, 0.833, accuracy: 0.0005)
        XCTAssertEqual(k.y + k.h, 0.915, accuracy: 0.0005)
    }

    func testJunkOnBothSidesKeepsCenter() {
        let a = d.classify([
            line("ollision damage.", 0.10, 0.884, 0.10),
            line("What's the plan?", 0.432, 0.884, 0.136),
            line("GRANGER 36", 0.80, 0.884, 0.10),
        ])
        XCTAssertEqual(Layout.joined(a.speech), "What's the plan?")
    }

    func testOldRuleWithoutLayout() {
        let a = Layout().classify([OcrLine("SELECT BACK", 0.80, 0.9, 0.15, 0.1), OcrLine("Hello there.", 0.40, 0.5, 0.2, 0.1)])
        XCTAssertEqual(Layout.joined(a.speech), "Hello there.")
    }
}
