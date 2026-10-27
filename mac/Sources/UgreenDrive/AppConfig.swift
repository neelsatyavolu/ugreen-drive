import Foundation

/// The Drive address baked in at build time: build.sh writes DRIVE_URL into
/// Info.plist (`UgreenDriveURL`), so the source has no hostnames. Empty means
/// the "enter the address" onboarding (or an address the installer saved).
struct AppConfig: Equatable {
    let driveURL: URL?

    static let shared = AppConfig(info: Bundle.main.infoDictionary ?? [:])

    init(info: [String: Any]) {
        driveURL = Self.origin(info["UgreenDriveURL"] as? String)
    }

    /// A Drive origin (scheme + host + port, no path). https anywhere; plain
    /// http only for a home-network address (localhost, `*.local`, a private
    /// IPv4 address), since a NAS without a tunnel is `http://<NAS-IP>:8790`.
    /// Empty values and unfilled build placeholders ("__DRIVE_URL__") are nil.
    static func origin(_ raw: String?) -> URL? {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty, !raw.hasPrefix("__"),
              var parts = URLComponents(string: raw),
              let host = parts.host?.lowercased(), !host.isEmpty,
              parts.scheme == "https" || (parts.scheme == "http" && isLocalHost(host)) else {
            return nil
        }
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    /// Same rule as the CLI (config.LocalHost): localhost, a `.local` (Bonjour)
    /// name, or a loopback, private or link-local IP address.
    static func isLocalHost(_ raw: String) -> Bool {
        var host = raw.lowercased()
        if host == "localhost" || host.hasSuffix(".local") { return true }
        if host.hasPrefix("["), host.hasSuffix("]") { host = String(host.dropFirst().dropLast()) }
        var v4 = in_addr()
        if inet_pton(AF_INET, host, &v4) == 1 {
            let b = withUnsafeBytes(of: v4) { Array($0) }
            return b[0] == 127 || b[0] == 10 || (b[0] == 172 && (16...31).contains(b[1]))
                || (b[0] == 192 && b[1] == 168) || (b[0] == 169 && b[1] == 254)
        }
        var v6 = in6_addr()
        guard inet_pton(AF_INET6, host, &v6) == 1 else { return false }
        let b = withUnsafeBytes(of: v6) { Array($0) }
        let loopback = b[0..<15].allSatisfy { $0 == 0 } && b[15] == 1
        // An IPv4-mapped address (::ffff:a.b.c.d) counts as its IPv4 address, as in Go.
        if b[0..<10].allSatisfy({ $0 == 0 }) && b[10] == 0xff && b[11] == 0xff {
            return isLocalHost(b[12..<16].map(String.init).joined(separator: "."))
        }
        return loopback || (b[0] & 0xfe) == 0xfc || (b[0] == 0xfe && (b[1] & 0xc0) == 0x80)
    }
}
