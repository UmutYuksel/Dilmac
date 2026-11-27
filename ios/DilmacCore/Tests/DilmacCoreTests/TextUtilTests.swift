// Port of pc/tests/test_textutil.py (same cases, same expectations).
@testable import DilmacCore
import XCTest

private let lexicon = Lexicon(["Lucia", "Jason", "Vice City", "Leonida"])

final class KeyTests: XCTestCase {
    func testTurkishLettersKept() {
        XCTAssertEqual(TextUtil.textKey("Güç Tasarrufu Görünümü"), "güçtasarrufugörünümü")
    }

    func testPunctuationAndQuotes() {
        XCTAssertEqual(TextUtil.textKey("I’m here!"), TextUtil.textKey("I'm here"))
    }

    func testOcrSimilarity() {
        XCTAssertGreaterThanOrEqual(TextUtil.similarity("Well, take care of yourself.", "Well, take care of yourse1f."), 0.9)
        XCTAssertLessThan(TextUtil.similarity("Well, take care of yourself.", "Hmm, who's this?"), 0.5)
    }
}

final class LexiconTests: XCTestCase {
    func testTurkishMenu() {
        XCTAssertTrue(lexicon.isTurkish("Güç Tasarrufu Görünümü"))
        XCTAssertTrue(lexicon.isTurkish("Guq Tasarrufu Gorunum"))
        XCTAssertTrue(lexicon.isTurkish("Diger Baglantllar"))
        XCTAssertFalse(lexicon.isTurkish("Can you please back up out my grill? Damn."))
        XCTAssertFalse(lexicon.isTurkish("Lucia, get in the car."))
        XCTAssertTrue(lexicon.isTurkish("Lutfen bekleyin"))
        XCTAssertTrue(lexicon.isTurkish("Arkadas Etkinligi Ana Ekran"))
        XCTAssertTrue(lexicon.isTurkish("Iptal"))
        XCTAssertTrue(lexicon.isTurkish("Arkadas Etkinlioi Grand Theft Auto VI Konsol Depolama Alant BEACH"))
        XCTAssertFalse(lexicon.isTurkish("Tell Mendoza Ramirez to call me."))
        XCTAssertFalse(lexicon.isTurkish("Shit, neiurcanii you evrl think about how much noise we can make then"))
        XCTAssertFalse(lexicon.isTurkish("Hrnrn, whds this?"))
        XCTAssertFalse(lexicon.isTurkish("000 wee, J sure waft if}} we hgye our bunker ready."))
    }

    func testGarbled() {
        XCTAssertTrue(lexicon.isGarbled("Ooo wee, I sure can't wait till veghayectaervffeoacgv"))
        XCTAssertTrue(lexicon.isGarbled("06 wei sure canlt bunker ragy"))
        XCTAssertTrue(lexicon.isGarbled("f fe & ie want it."))
        XCTAssertFalse(lexicon.isGarbled("Ooo wee, I sure can't wait till we have our bunker ready."))
        XCTAssertFalse(lexicon.isGarbled("I ain't even bout to play with you."))
    }

    func testClean() {
        XCTAssertTrue(lexicon.isClean("Hmm, who's this?"))
        XCTAssertTrue(lexicon.isClean("Jason, we gotta go."))
        XCTAssertFalse(lexicon.isClean("Hmm, whos1s this?"))
    }

    func testScorePicksBetterReading() {
        XCTAssertGreaterThan(lexicon.score("Well, take care of yourself."), lexicon.score("Well, akecare ofelf."))
    }
}

final class SpeakerTests: XCTestCase {
    private func split(_ s: String) -> [String?] {
        let r = TextUtil.splitSpeaker(s)
        return [r.name, r.body]
    }

    func testUppercaseName() { XCTAssertEqual(split("JASON: Let's go."), ["JASON", "Let's go."]) }
    func testTwoWordName() { XCTAssertEqual(split("Cal Hampton: Hey."), ["Cal Hampton", "Hey."]) }

    func testNoName() {
        XCTAssertEqual(split("We need to talk."), [nil, "We need to talk."])
        XCTAssertEqual(split("12:30 at the docks."), [nil, "12:30 at the docks."])
        XCTAssertEqual(split("Listen to me, this is it: we go now."), [nil, "Listen to me, this is it: we go now."])
    }
}

final class SentenceTests: XCTestCase {
    func testSplit() {
        XCTAssertEqual(TextUtil.splitSentences("Look, it's my fault! I'm sorry. Okay?"),
                       ["Look, it's my fault!", "I'm sorry.", "Okay?"])
    }

    func testAbbreviation() {
        XCTAssertEqual(TextUtil.splitSentences("Talk to Mr. Hampton first."), ["Talk to Mr. Hampton first."])
    }

    func testEllipsisLowercase() {
        XCTAssertEqual(TextUtil.splitSentences("I was gonna... you know what, forget it."),
                       ["I was gonna... you know what, forget it."])
    }

    func testDialogueDash() {
        XCTAssertEqual(TextUtil.splitSentences("- Where are you? - Home."), ["- Where are you?", "- Home."])
    }

    func testPunctuation() {
        XCTAssertTrue(TextUtil.endsWithPunct("He said \"go.\""))
        XCTAssertFalse(TextUtil.endsWithPunct("and then we"))
    }
}

final class ChunkTests: XCTestCase {
    func testShortSentencesMerge() {
        XCTAssertEqual(TextUtil.chunkSentences(TextUtil.splitSentences("Girl. Stop. Rent is coming.")),
                       ["Girl. Stop. Rent is coming."])
    }

    func testLongSentencesStaySeparate() {
        let c = ["You don't wanna do this!", "I'm likable and shit!", "I don't know...", "Feel like you got more somewhere."]
        XCTAssertEqual(TextUtil.chunkSentences(c), c)
        XCTAssertEqual(TextUtil.chunkSentences(["Hey.", "It's me.", "Let's see what these new folks are up to..."]),
                       ["Hey. It's me.", "Let's see what these new folks are up to..."])
    }

    func testShortTailJoinsPrevious() {
        XCTAssertEqual(TextUtil.chunkSentences(["Get on the floor, right now!", "Now!"]),
                       ["Get on the floor, right now! Now!"])
    }

    func testEmpty() { XCTAssertEqual(TextUtil.chunkSentences([]), []) }
}

final class NewPartTests: XCTestCase {
    func testAppendedBelow() {
        XCTAssertEqual(TextUtil.newPart(old: "Hey. It's me.", new: "Hey. It's me. Let's see what these new folks are up to..."),
                       "Let's see what these new folks are up to...")
    }

    func testTopLineShifts() {
        XCTAssertEqual(TextUtil.newPart(old: "Shit. Act natural.", new: "Act natural. I am acting natural."),
                       "I am acting natural.")
    }

    func testOcrDifferenceTolerated() {
        XCTAssertEqual(TextUtil.newPart(old: "Whoa, dude!", new: "Wh0a, dude! And don't get cute."), "And don't get cute.")
    }

    func testNoneWithoutOverlap() {
        XCTAssertNil(TextUtil.newPart(old: "Where's the doctor?", new: "You first. Where's the stuff?"))
        XCTAssertNil(TextUtil.newPart(old: "A lot of guys here.", new: "A lot of guys here."))
    }
}
