import Foundation

/// Everything the status dashboard shows besides account and connection.
struct DriveStatus: Equatable {
    struct Share: Equatable, Identifiable {
        let id: String
        let name: String // folder name at the volume root
        let canWrite: Bool
        var encrypted = false
        var locked = false
        var needsOwnerSignIn = false
        var relocksAt: Date?

        var isPersonal: Bool { id.hasPrefix("~") }
    }

    var email = ""
    var shares: [Share] = []
    /// nil = not checked yet.
    var driveReachable: Bool?
    var latencyMs: Int?
    var checkedAt: Date?
    var helperSince: Date?
    var helperRestarts = 0
    var online = true
    var networkKind = ""
    /// The helper is talking to the NAS directly on the home network.
    var viaLAN = false

    /// Folder name a share gets at the volume root (same rule as the helper,
    /// cli/internal/davfs shareName).
    static func folderName(id: String, name: String) -> String {
        let trimmed = name.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "/", with: "-")
        return trimmed.isEmpty ? id.replacingOccurrences(of: "/", with: "-") : trimmed
    }

    /// Reads `ugd --json whoami` output.
    static func account(fromWhoami data: Data) -> (username: String, email: String, shares: [Share]) {
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let shares = (obj["shares"] as? [[String: Any]] ?? []).compactMap { raw -> Share? in
            guard let id = raw["id"] as? String else { return nil }
            let name = raw["name"] as? String ?? id
            return Share(id: id, name: folderName(id: id, name: name), canWrite: raw["can_write"] as? Bool ?? false,
                         encrypted: raw["encrypted"] as? Bool ?? false,
                         locked: raw["locked"] as? Bool ?? false,
                         needsOwnerSignIn: raw["needs_owner_signin"] as? Bool ?? false,
                         relocksAt: (raw["expires_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) })
        }
        // Personal folders first: that's where Unlock lives.
        let ordered = shares.filter(\.isPersonal) + shares.filter { !$0.isPersonal }
        return (obj["nas_username"] as? String ?? "", obj["email"] as? String ?? "", ordered)
    }
}

/// One upload reported by the helper ({"event":"upload",…}).
struct Transfer: Equatable, Identifiable {
    enum State: String { case active, done, failed }

    let id: Int
    let path: String
    var size: Int64
    var sent: Int64
    var state: State
    var error: String
    var updatedAt: Date
    var bytesPerSecond: Double = 0

    var name: String { (path as NSString).lastPathComponent }
    var folder: String { (path as NSString).deletingLastPathComponent }
    var fraction: Double { size > 0 ? min(1, Double(sent) / Double(size)) : 1 }

    init?(event: [String: Any], now: Date) {
        guard let id = (event["id"] as? NSNumber)?.intValue,
              let path = event["path"] as? String,
              let state = State(rawValue: event["state"] as? String ?? "") else { return nil }
        self.id = id
        self.path = path
        size = (event["size"] as? NSNumber)?.int64Value ?? 0
        sent = (event["sent"] as? NSNumber)?.int64Value ?? 0
        self.state = state
        error = event["error"] as? String ?? ""
        updatedAt = now
    }

    /// A newer report for the same upload, with a smoothed speed.
    func updated(with next: Transfer) -> Transfer {
        var merged = next
        let seconds = next.updatedAt.timeIntervalSince(updatedAt)
        if seconds > 0.2, next.sent >= sent {
            let instant = Double(next.sent - sent) / seconds
            merged.bytesPerSecond = bytesPerSecond == 0 ? instant : bytesPerSecond * 0.6 + instant * 0.4
        } else {
            merged.bytesPerSecond = bytesPerSecond
        }
        return merged
    }
}

/// Merges one upload event into the list: newest first, finished ones kept
/// for a while so people see what just happened.
func mergeTransfer(_ incoming: Transfer, into list: [Transfer], now: Date) -> [Transfer] {
    var others = list.filter { $0.id != incoming.id }
    let merged = list.first(where: { $0.id == incoming.id }).map { $0.updated(with: incoming) } ?? incoming
    others.insert(merged, at: 0)
    return pruneTransfers(others, now: now)
}

func pruneTransfers(_ list: [Transfer], now: Date) -> [Transfer] {
    let active = list.filter { $0.state == .active }
    let finished = list.filter { $0.state != .active && now.timeIntervalSince($0.updatedAt) < 15 * 60 }
    return active + finished.prefix(4)
}

enum Formatting {
    static func bytes(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }

    static func speed(_ bytesPerSecond: Double) -> String {
        bytes(Int64(bytesPerSecond)) + "/s"
    }

    static func time(_ date: Date) -> String {
        date.formatted(date: .omitted, time: .shortened)
    }

    static func duration(since date: Date, now: Date = Date()) -> String {
        let seconds = Int(now.timeIntervalSince(date))
        switch seconds {
        case ..<60: return "just now"
        case ..<3600: return "\(seconds / 60) min"
        case ..<86400: return "\(seconds / 3600) h \(seconds % 3600 / 60) min"
        default: return "\(seconds / 86400) d"
        }
    }
}
