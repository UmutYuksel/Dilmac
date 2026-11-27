// swift-tools-version: 6.2
// DilmacCore: the platform-free logic of Dilmaç (ports of pc/textutil.py, stabilizer.py, pack.py and the glossary),
// shared by the iOS app and tested on the Mac with `swift test` (no simulator needed).
import PackageDescription

let package = Package(
    name: "DilmacCore",
    platforms: [.iOS(.v26), .macOS(.v15)],
    products: [.library(name: "DilmacCore", targets: ["DilmacCore"])],
    targets: [
        .target(
            name: "DilmacCore",
            resources: [.copy("Resources/en-words.deflate")],
            swiftSettings: [.swiftLanguageMode(.v5)]),
        .testTarget(
            name: "DilmacCoreTests",
            dependencies: ["DilmacCore"],
            swiftSettings: [.swiftLanguageMode(.v5)]),
    ])
