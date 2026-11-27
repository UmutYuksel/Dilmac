import Foundation

/// The PSN account used for Remote Play registration. Kept in the Keychain (`Keychain.psnAccount`).
struct PSNAccount: Codable, Equatable {
    var onlineID: String
    /// Numeric PSN account id; Remote Play wants it as 8 little-endian bytes.
    var accountID: UInt64
}

/// A PS5 this phone is registered with. Kept in the Keychain (`Keychain.ps5Pairing`).
struct PS5Pairing: Codable, Equatable {
    var host: String
    var name: String
    var mac: Data
    /// 16 bytes from registration (ASCII hex text, NUL padded), passed back to chiaki as is.
    var registKey: Data
    /// 16 bytes ("morning" in chiaki).
    var rpKey: Data
    var date: Date
}

extension Keychain {
    static let psnAccount = "psn-account"
    static let ps5Pairing = "ps5-pairing"
}
