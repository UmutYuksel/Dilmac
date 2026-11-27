import Darwin
import Foundation

/// PS5 discovery protocol (UDP 9302), port of pc/checks.py find_ps5.
///
/// iOS needs Apple's multicast entitlement for broadcast, so this only sends unicast: to a known address, or to
/// every address of the phone's Wi-Fi subnet (at most a /22) when the PS5's address is not known or has changed.
/// Unicast to the local network only needs the Local Network permission (NSLocalNetworkUsageDescription).
enum PS5Discovery {
    struct Status: Equatable {
        enum State: Equatable { case ready, standby, other(String) }
        let ip: String
        let name: String
        let state: State
        let runningApp: String
    }

    private static let port: UInt16 = 9302
    private static let search = Array("SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n".utf8)

    /// Asks the given addresses; returns the first PS5 that answers. Blocking: call off the main thread.
    static func probe(_ hosts: [String], timeout: TimeInterval = 1.2) -> Status? {
        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var tv = timeval(tv_sec: 0, tv_usec: 150_000)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        // Twice: the first packet to an address the phone has not talked to yet can be lost while ARP resolves.
        for round in 0..<2 {
            for host in hosts { send(fd, to: host) }
            if round == 0 { usleep(150_000) }
        }

        let end = Date().addingTimeInterval(timeout)
        var buffer = [UInt8](repeating: 0, count: 2048)
        while Date() < end {
            var from = sockaddr_in()
            var fromLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let n = withUnsafeMutablePointer(to: &from) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    recvfrom(fd, &buffer, buffer.count, 0, $0, &fromLen)
                }
            }
            guard n > 0 else { continue }
            if let status = parse(String(decoding: buffer[0..<n], as: UTF8.self), from: ipString(from.sin_addr)) {
                return status
            }
        }
        return nil
    }

    /// Every host address of the Wi-Fi interface's IPv4 subnet (capped to a /22), without the phone's own.
    static func subnetHosts() -> [String] {
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return [] }
        defer { freeifaddrs(list) }
        for entry in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let ifa = entry.pointee
            guard String(cString: ifa.ifa_name) == "en0", let addr = ifa.ifa_addr, let mask = ifa.ifa_netmask,
                  addr.pointee.sa_family == sa_family_t(AF_INET) else { continue }
            let ip = addr.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
            var netmask = mask.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
            netmask |= 0xFFFF_FC00 // never scan more than a /22 (1022 hosts)
            let network = ip & netmask
            let broadcast = network | ~netmask
            guard broadcast > network + 1 else { return [] }
            return (network + 1..<broadcast).filter { $0 != ip }.map { ipString(in_addr(s_addr: $0.bigEndian)) }
        }
        return []
    }

    private static func send(_ fd: Int32, to host: String) {
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        guard inet_pton(AF_INET, host, &addr.sin_addr) == 1 else { return }
        _ = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                sendto(fd, search, search.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
    }

    private static func parse(_ reply: String, from ip: String) -> Status? {
        let lines = reply.split(whereSeparator: \.isNewline).map(String.init)
        guard let head = lines.first, head.hasPrefix("HTTP/1.1") else { return nil }
        var fields: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            fields[String(line[..<colon])] = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        }
        let type = fields["host-type"] ?? "PS5"
        guard type == "PS5" || type.isEmpty else { return nil }
        let code = head.split(separator: " ", maxSplits: 2).dropFirst().first.map(String.init) ?? ""
        let state: Status.State = code == "200" ? .ready : code == "620" ? .standby : .other(head)
        return Status(ip: ip, name: fields["host-name"] ?? "PS5", state: state,
                      runningApp: fields["running-app-name"] ?? "")
    }

    private static func ipString(_ addr: in_addr) -> String {
        var a = addr
        var text = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
        inet_ntop(AF_INET, &a, &text, socklen_t(INET_ADDRSTRLEN))
        return String(cString: text)
    }
}
