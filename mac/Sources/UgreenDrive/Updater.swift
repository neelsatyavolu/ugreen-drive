import AppKit
import CryptoKit
import SwiftUI
import Security

/// Keeps the app up to date: every hour it checks the latest `v*` release
/// (the web redirect, not the rate-limited API), downloads Ugreen-Drive-mac.zip,
/// verifies its SHA-256, that it's Developer ID-signed by the same team as this
/// copy and notarized, replaces the app and relaunches — never during a copy.
/// The relaunched app remounts the drive by itself.
@MainActor
final class Updater: ObservableObject {
    enum State: Equatable {
        case idle
        case checking
        case upToDate
        case available(String)   // found; downloading/verifying or waiting to install
        case ready(String)       // verified, installs when no copy is running
        case installing(String)
        case failed(String)
    }

    nonisolated static let repo = URL(string: "https://github.com/neelsatyavolu/ugreen-drive")!
    static let interval: TimeInterval = 60 * 60
    nonisolated private static let asset = "Ugreen-Drive-mac.zip"

    @Published private(set) var state: State = .idle
    @Published private(set) var checkedAt: Date?
    @Published var automatic: Bool {
        didSet { UserDefaults.standard.set(automatic, forKey: "autoUpdate") }
    }

    private weak var drive: DriveController?
    private var timer: Timer?
    private var staged: URL? // verified new app, ready to install
    @Published private(set) var installWhenIdle = false

    init() {
        automatic = UserDefaults.standard.object(forKey: "autoUpdate") as? Bool ?? true
    }

    var current: String { DriveController.appVersion }

    #if DEBUG
    func preview(_ state: State) { self.state = state }
    #endif

    /// The version waiting to be installed, for the footer's Update button.
    var pendingVersion: String? {
        switch state {
        case .available(let v), .ready(let v), .installing(let v): return v
        default: return nil
        }
    }

    func start(drive: DriveController) {
        self.drive = drive
        // First check shortly after launch, then hourly; every 5 minutes we
        // also retry an install that was waiting for a copy to finish.
        Task {
            try? await Task.sleep(nanoseconds: 90 * 1_000_000_000)
            await check()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                if let checked = self.checkedAt, Date().timeIntervalSince(checked) < Self.interval {
                    self.installIfReady()
                } else {
                    await self.check()
                }
            }
        }
    }

    /// "Check for updates" / the Update button: installs as soon as it's safe.
    func updateNow() async {
        installWhenIdle = true
        if case .ready = state { installIfReady(); return }
        await check(manual: true)
    }

    func check(manual: Bool = false) async {
        switch state {
        case .checking, .available, .installing: return // one at a time
        case .ready: installIfReady(); return
        default: break
        }
        guard Self.parse(current) != nil else {
            if manual { state = .failed("Development builds don't update.") }
            return
        }
        guard let team = Self.teamID(of: Bundle.main.bundleURL) else {
            if manual { state = .failed("This copy isn't signed. Reinstall it from the Drive to get updates.") }
            return
        }
        state = .checking
        do {
            let latest = try await Self.latestVersion()
            checkedAt = Date()
            guard let latest, Self.isNewer(latest, than: current) else {
                state = .upToDate
                return
            }
            state = .available(latest)
            staged = try await Self.download(latest, team: team)
            state = .ready(latest)
            if automatic || manual { installWhenIdle = true }
            installIfReady()
        } catch {
            state = .failed("Update check failed: \(error.localizedDescription)")
        }
    }

    /// Called when the last copy/upload finishes: install a waiting update now.
    func transfersIdle() {
        installIfReady()
    }

    /// Installs wait for Drive copies and a folder unlock.
    nonisolated static func mayInstall(transferring: Bool, unlocking: Bool) -> Bool {
        !transferring && !unlocking
    }

    /// True when an install is waiting only for a copy to finish.
    var waitingForCopy: Bool {
        if case .ready = state, installWhenIdle, drive?.isTransferring == true { return true }
        return false
    }

    /// Installs the verified update once it's safe (see `mayInstall`).
    private func installIfReady() {
        guard case .ready(let version) = state, installWhenIdle, let staged, let drive else { return }
        guard Self.mayInstall(transferring: drive.isTransferring, unlocking: drive.unlocking?.busy == true)
        else { return } // retried every 5 min and after copies
        state = .installing(version)
        do {
            try Self.replaceApp(with: staged)
            Self.relaunch(showWindow: Windows.shared.hasOpenWindows)
            QuitPolicy.quitCompletely() // unmounts; the new copy remounts
        } catch {
            state = .failed("Couldn't install \(version): \(error.localizedDescription) Run the install command from the Drive's Mac app window.")
        }
    }

    // MARK: Steps

    /// The newest release version, from the /releases/latest redirect.
    nonisolated static func latestVersion() async throws -> String? {
        var request = URLRequest(url: repo.appendingPathComponent("releases/latest"))
        request.httpMethod = "HEAD"
        request.timeoutInterval = 30
        let (_, response) = try await URLSession.shared.data(for: request)
        let tag = response.url?.lastPathComponent ?? ""
        return Self.version(fromTag: tag)
    }

    /// "v1.2.3" → "1.2.3"; anything else (not a release tag) → nil.
    nonisolated static func version(fromTag tag: String) -> String? {
        guard tag.hasPrefix("v"), parse(String(tag.dropFirst())) != nil else { return nil }
        return String(tag.dropFirst())
    }

    /// Downloads and verifies the release; returns the unpacked app.
    nonisolated static func download(_ version: String, team: String) async throws -> URL {
        let base = repo.appendingPathComponent("releases/download/v\(version)")
        let work = FileManager.default.temporaryDirectory.appendingPathComponent("ugreen-drive-update-\(version)")
        try? FileManager.default.removeItem(at: work)
        try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)

        let (sums, _) = try await URLSession.shared.data(from: base.appendingPathComponent("SHA256SUMS"))
        guard let expected = String(decoding: sums, as: UTF8.self).split(separator: "\n")
            .first(where: { $0.hasSuffix(" \(asset)") })?.split(separator: " ").first.map(String.init) else {
            throw UpdateError("the release has no checksum for \(asset)")
        }
        let (file, _) = try await URLSession.shared.download(from: base.appendingPathComponent(asset))
        let zip = work.appendingPathComponent(asset)
        try FileManager.default.moveItem(at: file, to: zip)
        let digest = SHA256.hash(data: try Data(contentsOf: zip)).map { String(format: "%02x", $0) }.joined()
        guard digest == expected else { throw UpdateError("checksum mismatch") }

        try run("/usr/bin/ditto", ["-x", "-k", zip.path, work.path])
        let app = work.appendingPathComponent(MoveToApplications.appName)
        guard FileManager.default.fileExists(atPath: app.path) else {
            throw UpdateError("the download doesn't contain the app")
        }
        try verify(app, version: version, team: team)
        return app
    }

    /// Same bundle id and version, Developer ID-signed by our team, notarized.
    nonisolated static func verify(_ app: URL, version: String, team: String) throws {
        guard let info = Bundle(url: app)?.infoDictionary,
              info["CFBundleIdentifier"] as? String == Bundle.main.bundleIdentifier,
              info["CFBundleShortVersionString"] as? String == version else {
            throw UpdateError("the download isn't Ugreen Drive \(version)")
        }
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code else {
            throw UpdateError("can't read the app's signature")
        }
        let id = Bundle.main.bundleIdentifier ?? ""
        let text = "anchor apple generic and identifier \"\(id)\" and certificate leaf[subject.OU] = \"\(team)\""
        var requirement: SecRequirement?
        guard SecRequirementCreateWithString(text as CFString, [], &requirement) == errSecSuccess else {
            throw UpdateError("bad signing requirement")
        }
        let flags = SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSStrictValidate | kSecCSCheckNestedCode)
        guard SecStaticCodeCheckValidity(code, flags, requirement) == errSecSuccess else {
            throw UpdateError("the download isn't signed by the same developer as this copy")
        }
        // Gatekeeper: Developer ID and notarized.
        try run("/usr/sbin/spctl", ["--assess", "--type", "execute", app.path])
    }

    /// Swaps the running app's bundle for the new one (same volume, atomic).
    static func replaceApp(with new: URL) throws {
        let target = Bundle.main.bundleURL
        let staging = target.deletingLastPathComponent().appendingPathComponent(".\(target.lastPathComponent).update")
        try? FileManager.default.removeItem(at: staging)
        try run("/usr/bin/ditto", [new.path, staging.path])
        _ = try FileManager.default.replaceItemAt(target, withItemAt: staging)
    }

    /// Starts the new copy once this one has quit (only one copy may run).
    nonisolated static func relaunch(_ app: URL = Bundle.main.bundleURL, showWindow: Bool) {
        let pid = ProcessInfo.processInfo.processIdentifier
        let open = showWindow ? #"/usr/bin/open "$0""# : #"/usr/bin/open "$0" --args --background"#
        let script = "while kill -0 \(pid) 2>/dev/null; do sleep 0.2; done; " + open
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", script, app.path]
        try? process.run()
    }

    // MARK: Helpers

    nonisolated static func teamID(of app: URL) -> String? {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code else { return nil }
        var info: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
              let dict = info as? [String: Any] else { return nil }
        return dict[kSecCodeInfoTeamIdentifier as String] as? String
    }

    /// "1.2.3" → comparable parts; nil for dev builds ("0.0.0-dev", "0.4.1-local").
    nonisolated static func parse(_ version: String) -> [Int]? {
        let parts = version.split(separator: ".").map { Int($0) }
        guard parts.count == 3, parts.allSatisfy({ $0 != nil }) else { return nil }
        return parts.compactMap { $0 }
    }

    nonisolated static func isNewer(_ candidate: String, than installed: String) -> Bool {
        guard let new = parse(candidate), let have = parse(installed) else { return false }
        return have.lexicographicallyPrecedes(new)
    }

    nonisolated private static func run(_ tool: String, _ args: [String]) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = args
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw UpdateError("\((tool as NSString).lastPathComponent) failed (\(process.terminationStatus))")
        }
    }
}

struct UpdateError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

extension Updater {
    /// One line for Settings and diagnostics.
    var summary: String {
        let checked = checkedAt.map { " · checked \(Formatting.time($0))" } ?? ""
        switch state {
        case .idle: return "Version \(current)\(checked)"
        case .checking: return "Checking for updates…"
        case .upToDate: return "Up to date (\(current))\(checked)"
        case .available(let v): return "Downloading \(v)…"
        case .ready(let v): return "\(v) is ready — installs when no copy is running"
        case .installing(let v): return "Installing \(v)…"
        case .failed(let reason): return reason
        }
    }
}

/// The "Update" button that appears next to Account when an update is
/// available (it also installs by itself within the hour).
struct UpdateButton: View {
    @ObservedObject var updater: Updater

    var body: some View {
        if let version = updater.pendingVersion {
            Button { Task { await updater.updateNow() } } label: {
                HStack(spacing: 5) {
                    if case .installing = updater.state {
                        ProgressView().controlSize(.mini).tint(Brand.onBrand)
                    } else {
                        Image(systemName: "arrow.down.circle.fill").font(.system(size: 11, weight: .semibold))
                    }
                    Text(label).font(.sans(11.5, .semibold))
                }
                .foregroundStyle(Brand.onBrand)
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(Brand.fill, in: Rectangle())
            }
            .buttonStyle(.plain)
            .fixedSize()
            .help("Update to \(version) now. The drive remounts by itself after the restart.")
        }
    }

    private var label: String {
        if case .installing = updater.state { return "Updating…" }
        return updater.waitingForCopy ? "Update after copy" : "Update"
    }
}

/// The version in the menu footer; the Update button takes its place when an
/// update is pending (the footer is narrow).
struct VersionLabel: View {
    @ObservedObject var updater: Updater

    var body: some View {
        if updater.pendingVersion == nil {
            Text("v\(updater.current)").font(.mono(10.5)).foregroundStyle(Brand.muted).fixedSize()
        }
    }
}

/// Settings row: automatic updates, version and "Check now".
struct UpdateSetting: View {
    @ObservedObject var updater: Updater

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "arrow.triangle.2.circlepath")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Brand.muted)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text("Update automatically").font(.sans(12.5, .medium))
                Text(updater.summary).font(.sans(11)).foregroundStyle(stateColor)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Check now") { Task { await updater.check(manual: true) } }
                    .buttonStyle(LinkButtonStyle(tint: Brand.accent))
            }
            Spacer(minLength: 8)
            Toggle("", isOn: $updater.automatic)
                .toggleStyle(.switch)
                .controlSize(.mini)
                .labelsHidden()
                .tint(Brand.accent)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private var stateColor: Color {
        if case .failed = updater.state { return Brand.danger }
        return Brand.muted
    }
}
