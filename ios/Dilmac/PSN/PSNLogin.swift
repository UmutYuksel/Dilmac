import Foundation
import SwiftUI
import WebKit

/// PSN sign-in for Remote Play registration (port of pc/ps5_direct.py: login_url, code_from_redirect, account_info).
///
/// Sony's own sign-in page opens in a WKWebView with a non-persistent data store, so no cookies or site data are
/// kept. When Sony redirects to remoteplay.dl.playstation.net/remoteplay/redirect?code=..., the navigation is
/// stopped and the one-time code is exchanged for the account id. The password goes to Sony only; the app keeps
/// neither the code nor the access token.
enum PSNLogin {
    // Remote Play's public client credentials (chiaki-ng uses them too).
    private static let clientID = "ba495a24-818c-472b-b12d-ff231c1b5745"
    private static let clientSecret = "mvaiZkRsAsI1IBkY"
    static let redirect = "https://remoteplay.dl.playstation.net/remoteplay/redirect"
    private static let scope = "psn:clientapp referenceDataService:countryConfig.read "
        + "pushNotification:webSocket.desktop.connect sessionManager:remotePlaySession.system.update"
    private static let authorizeURL = "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/authorize"
    private static let tokenURL = "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/token"

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static var loginURL: URL {
        let q: [(String, String)] = [
            ("service_entity", "urn:service-entity:psn"), ("response_type", "code"), ("client_id", clientID),
            ("redirect_uri", redirect), ("scope", scope), ("request_locale", "en_US"), ("ui", "pr"),
            ("service_logo", "ps"), ("layout_type", "popup"), ("smcid", "remoteplay"), ("prompt", "always"),
            ("PlatformPrivacyWs1", "minimal"),
        ]
        return URL(string: authorizeURL + "?" + formEncode(q))!
    }

    /// The one-time code from Sony's redirect address; nil if `url` is not the redirect.
    static func code(fromRedirect url: URL) throws -> String? {
        guard url.absoluteString.hasPrefix(redirect) else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        if let code = items.first(where: { $0.name == "code" })?.value, code.count > 1 {
            return code
        }
        if let error = items.first(where: { $0.name == "error" })?.value {
            let description = items.first(where: { $0.name == "error_description" })?.value ?? ""
            throw Failure(message: "Sony girişi tamamlamadı: \(error) \(description)")
        }
        throw Failure(message: "Adreste giriş kodu yok. Girişi yeniden yap.")
    }

    /// Exchanges the code for the PSN account (name and numeric id). The token is used once and dropped.
    static func account(forCode code: String) async throws -> PSNAccount {
        let auth = "Basic " + Data("\(clientID):\(clientSecret)".utf8).base64EncodedString()

        var post = URLRequest(url: URL(string: tokenURL)!)
        post.httpMethod = "POST"
        post.setValue(auth, forHTTPHeaderField: "Authorization")
        post.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        post.httpBody = Data(formEncode([("grant_type", "authorization_code"), ("code", code), ("scope", scope),
                                         ("redirect_uri", redirect)]).utf8)
        post.timeoutInterval = 15
        let (tokenData, tokenResponse) = try await session.data(for: post)
        guard (tokenResponse as? HTTPURLResponse)?.statusCode == 200 else {
            let status = (tokenResponse as? HTTPURLResponse)?.statusCode ?? 0
            throw Failure(message: "Sony kodu kabul etmedi (HTTP \(status)). Girişi yeniden dene.")
        }
        guard let tokenJSON = try JSONSerialization.jsonObject(with: tokenData) as? [String: Any],
              let token = tokenJSON["access_token"] as? String else {
            throw Failure(message: "Sony'nin yanıtında erişim anahtarı yok.")
        }

        let escaped = token.addingPercentEncoding(withAllowedCharacters: .urlUnreserved) ?? token
        var get = URLRequest(url: URL(string: tokenURL + "/" + escaped)!)
        get.setValue(auth, forHTTPHeaderField: "Authorization")
        get.timeoutInterval = 15
        let (infoData, infoResponse) = try await session.data(for: get)
        guard (infoResponse as? HTTPURLResponse)?.statusCode == 200 else {
            let status = (infoResponse as? HTTPURLResponse)?.statusCode ?? 0
            throw Failure(message: "Hesap bilgisi alınamadı (HTTP \(status)).")
        }
        guard let info = try JSONSerialization.jsonObject(with: infoData) as? [String: Any],
              let userID = info["user_id"].flatMap({ "\($0)" }), let number = UInt64(userID) else {
            throw Failure(message: "Sony'nin yanıtında hesap kimliği yok.")
        }
        let name = (info["online_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "psn-\(number)"
        return PSNAccount(onlineID: name, accountID: number)
    }

    /// No cookies, no cache: nothing from the sign-in outlives the request.
    private static let session = URLSession(configuration: .ephemeral)

    private static func formEncode(_ pairs: [(String, String)]) -> String {
        pairs.map { k, v in
            "\(k.addingPercentEncoding(withAllowedCharacters: .urlUnreserved)!)="
                + v.addingPercentEncoding(withAllowedCharacters: .urlUnreserved)!
        }.joined(separator: "&")
    }
}

private extension CharacterSet {
    /// RFC 3986 unreserved characters; everything else is percent-encoded.
    static let urlUnreserved = CharacterSet(charactersIn:
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
}

/// Sony's sign-in page in a throwaway web view. Calls `onResult` once with the one-time code (or an error).
struct PSNLoginView {
    let onResult: (Result<String, Error>) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onResult: onResult) }

    private func makeWebView(_ coordinator: Coordinator) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = coordinator
        view.load(URLRequest(url: PSNLogin.loginURL))
        return view
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        private let onResult: (Result<String, Error>) -> Void
        private var done = false

        init(onResult: @escaping (Result<String, Error>) -> Void) { self.onResult = onResult }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url, url.absoluteString.hasPrefix(PSNLogin.redirect) else {
                decisionHandler(.allow)
                return
            }
            decisionHandler(.cancel)
            guard !done else { return }
            done = true
            do {
                if let code = try PSNLogin.code(fromRedirect: url) { onResult(.success(code)) }
            } catch {
                onResult(.failure(error))
            }
        }
    }
}

#if os(macOS)
extension PSNLoginView: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView { makeWebView(context.coordinator) }
    func updateNSView(_ view: WKWebView, context: Context) {}
}
#else
extension PSNLoginView: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView { makeWebView(context.coordinator) }
    func updateUIView(_ view: WKWebView, context: Context) {}
}
#endif
