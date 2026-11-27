import Darwin
import Foundation

/// Link to the Android TV overlay app (tv/, OverlayService): TCP 8787, one JSON message per line (port of
/// pc/subtitler.py TvLink). Sends on its own serial queue; reconnects once on failure.
final class TVLink: @unchecked Sendable {
    static let port: UInt16 = 8787

    let host: String
    /// Connection state changes (true = connected). Called on the link's queue.
    var onState: ((Bool) -> Void)?

    private let queue = DispatchQueue(label: "dilmac.tv")
    private var fd: Int32 = -1
    private var warned = false

    init(host: String) { self.host = host }

    deinit { if fd >= 0 { Darwin.close(fd) } }

    func send(_ message: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: message, options: [.withoutEscapingSlashes]) else { return }
        data.append(0x0A)
        queue.async { self.write(data) }
    }

    func close() {
        queue.async { self.disconnect() }
    }

    private func write(_ data: Data) {
        for _ in 0..<2 {
            if fd < 0 {
                fd = Self.connect(host, timeoutMs: 2000)
                if fd < 0 {
                    if !warned { warned = true; onState?(false) }
                    return
                }
                warned = false
                onState?(true)
            }
            let ok = data.withUnsafeBytes { buffer -> Bool in
                var sent = 0
                while sent < buffer.count {
                    let n = Darwin.send(fd, buffer.baseAddress! + sent, buffer.count - sent, 0)
                    if n <= 0 { return false }
                    sent += n
                }
                return true
            }
            if ok { return }
            disconnect()
        }
        if !warned { warned = true; onState?(false) }
    }

    private func disconnect() {
        if fd >= 0 { Darwin.close(fd) }
        fd = -1
    }

    // MARK: sockets

    private static func address(_ host: String) -> sockaddr_in? {
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        return inet_pton(AF_INET, host, &addr.sin_addr) == 1 ? addr : nil
    }

    /// Non-blocking socket with a connect already started; -1 on error.
    private static func startConnect(_ host: String) -> Int32 {
        guard var addr = address(host) else { return -1 }
        let s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
        guard s >= 0 else { return -1 }
        var one: Int32 = 1
        setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size)) // no SIGPIPE on a dead TV
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size)) // small messages go at once
        _ = fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK)
        let r = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if r != 0 && errno != EINPROGRESS { Darwin.close(s); return -1 }
        return s
    }

    private static func connected(_ s: Int32) -> Bool {
        var err: Int32 = 0
        var len = socklen_t(MemoryLayout<Int32>.size)
        return getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len) == 0 && err == 0
    }

    /// A blocking connected socket, or -1.
    static func connect(_ host: String, timeoutMs: Int32) -> Int32 {
        let s = startConnect(host)
        guard s >= 0 else { return -1 }
        var p = pollfd(fd: s, events: Int16(POLLOUT), revents: 0)
        guard poll(&p, 1, timeoutMs) == 1, connected(s) else { Darwin.close(s); return -1 }
        _ = fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) & ~O_NONBLOCK)
        var tv = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        return s
    }

    /// The first address with the TV app's port open. The TV's address changes with DHCP, so the subnet is
    /// scanned in batches. Blocking: call off the main thread.
    static func find(in hosts: [String], timeoutMs: Int32 = 500) -> String? {
        for start in stride(from: 0, to: hosts.count, by: 100) {
            let batch = hosts[start..<min(hosts.count, start + 100)]
            let open: [(String, Int32)] = batch.compactMap { h in
                let s = startConnect(h)
                return s >= 0 ? (h, s) : nil
            }
            defer { for (_, s) in open { Darwin.close(s) } }
            var fds = open.map { pollfd(fd: $0.1, events: Int16(POLLOUT), revents: 0) }
            let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000)
            while Date() < deadline {
                let left = Int32(max(1, deadline.timeIntervalSinceNow * 1000))
                guard poll(&fds, nfds_t(fds.count), left) > 0 else { break }
                for (i, p) in fds.enumerated() where p.revents != 0 {
                    if connected(p.fd) { return open[i].0 }
                    fds[i].fd = -1 // refused: stop watching it (poll skips negative fds)
                }
            }
        }
        return nil
    }
}
