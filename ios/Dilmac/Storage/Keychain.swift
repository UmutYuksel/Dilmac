import Foundation
import Security

/// Small Codable store on top of the iOS Keychain (generic passwords).
///
/// Everything secret (PSN account id, PS5 registration keys) lives here, never in files or UserDefaults.
/// Items are `AfterFirstUnlockThisDeviceOnly`: not synced to iCloud and not restored onto another device.
enum Keychain {
    private static let service = "dilmac"

    struct Failure: LocalizedError {
        let status: OSStatus
        var errorDescription: String? {
            let text = SecCopyErrorMessageString(status, nil) as String? ?? "\(status)"
            return "Anahtar Zinciri hatası: \(text)"
        }
    }

    private static func query(_ key: String) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: key]
        #if os(macOS)
        if dataProtection { q[kSecUseDataProtectionKeychain as String] = true }
        #endif
        return q
    }

    #if os(macOS)
    /// The iOS-style keychain on the Mac (it knows ThisDeviceOnly and is never synced). A build signed without a
    /// provisioning profile is refused it (errSecMissingEntitlement); then the login keychain is used.
    private static let dataProtection: Bool = {
        let probe: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service + ".probe",
                                    kSecAttrAccount as String: "probe",
                                    kSecUseDataProtectionKeychain as String: true]
        SecItemDelete(probe as CFDictionary)
        var item = probe
        item[kSecValueData as String] = Data()
        let status = SecItemAdd(item as CFDictionary, nil)
        SecItemDelete(probe as CFDictionary)
        return status == errSecSuccess
    }()

    /// For the log: which keychain is in use.
    static var kind: String { dataProtection ? "veri korumalı anahtar zinciri" : "oturum anahtar zinciri" }
    #endif

    static func save<T: Encodable>(_ value: T, for key: String) throws {
        let data = try JSONEncoder().encode(value)
        SecItemDelete(query(key) as CFDictionary)
        var item = query(key)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw Failure(status: status) }
    }

    static func load<T: Decodable>(_ type: T.Type, for key: String) -> T? {
        var q = query(key)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else {
            return nil
        }
        return try? JSONDecoder().decode(type, from: data)
    }

    static func delete(_ key: String) {
        SecItemDelete(query(key) as CFDictionary)
    }
}
