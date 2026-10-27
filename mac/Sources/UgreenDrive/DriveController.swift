import AppKit
import Network
import ServiceManagement

/// Signs in, runs the WebDAV helper and keeps the Finder volume mounted.
@MainActor
final class DriveController: ObservableObject {
    enum Account: Equatable { case unknown, signedOut, signingIn, signedIn(String) }
    enum Connection: Equatable { case disconnected, connecting, connected(URL), failed(String) }

    nonisolated static let defaultName = "Ugreen Drive"
    private static let davUser = "ugd"

    @Published private(set) var serverURL: String
    /// The Drive's name (Settings → General on the Drive), also the Finder volume's.
    @Published private(set) var driveName = DriveController.cleanName(UserDefaults.standard.string(forKey: "driveName"))
    @Published private(set) var account: Account = .unknown
    @Published private(set) var connection: Connection = .disconnected
    @Published private(set) var message: String?
    @Published private(set) var startsAtLogin = LoginItem.isEnabled
    @Published private(set) var status = DriveStatus()
    @Published private(set) var transfers: [Transfer] = []
    /// Files Finder has open for writing (a copy in progress before its upload).
    @Published private(set) var openWrites = 0
    let updater = Updater()
    let speedTest = SpeedTest()
    /// The encrypted personal folder the Unlock window is working on.
    @Published private(set) var unlocking: PersonalUnlock?

    private let defaults = UserDefaults.standard
    private let dav = DavServer()
    /// Secret between this app, the helper and NetFS; new for every helper
    /// start, so nothing that talked to an earlier helper can reuse it.
    private var davPassword = ""
    private var mountURL: URL?
    private var loginRun: CLIRun?
    private var working = false
    private var checking = false
    private var restartDelay: UInt64 = 1
    private let pathMonitor = NWPathMonitor()
    private var timer: Timer?

    private var wantsConnected: Bool {
        get { defaults.bool(forKey: "wantsConnected") }
        set { defaults.set(newValue, forKey: "wantsConnected") }
    }

    init() {
        // A saved address wins; otherwise the one this build was made for (if any).
        serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? AppConfig.shared.driveURL?.absoluteString ?? ""
        dav.onEvent = { [weak self] event in self?.helperEvent(event) }
        observeSystem()
        Task { await start() }
    }

    #if DEBUG
    /// Inert controller for PreviewRenderer: no helper, observers or timers.
    init(previewServer: String) {
        serverURL = previewServer
    }

    func applyPreview(account: Account, connection: Connection, status: DriveStatus, transfers: [Transfer]) {
        self.account = account
        self.connection = connection
        self.status = status
        self.transfers = transfers
    }
    #endif

    // MARK: Setup and account

    var hasServer: Bool { !serverURL.isEmpty }

    var serverHost: String { URL(string: serverURL)?.host ?? serverURL }

    /// Validates and saves the Drive address: https, or http on the home
    /// network (a NAS without a tunnel is http://<NAS-IP>:8790). No path.
    func setServer(_ raw: String) {
        guard let url = Self.parseServer(raw) else {
            message = "Enter your Drive address, like https://drive.example.com or 192.168.1.20:8790"
            return
        }
        serverURL = url.absoluteString
        defaults.set(serverURL, forKey: "serverURL")
        message = nil
        Task { await refreshAccount() }
    }

    func changeServer() {
        disconnect()
        serverURL = ""
        defaults.removeObject(forKey: "serverURL")
        account = .unknown
    }

    /// "drive.example.com" → https; "192.168.1.20" → http://192.168.1.20:8790
    /// (a home-network address with no port gets the Drive's default port).
    nonisolated static func parseServer(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        var candidate = trimmed
        if !trimmed.contains("://") {
            let host = URLComponents(string: "http://" + trimmed)?.host?.lowercased() ?? ""
            candidate = (AppConfig.isLocalHost(host) ? "http://" : "https://") + trimmed
        }
        guard var parts = URLComponents(string: candidate), parts.query == nil,
              ["", "/"].contains(parts.path) else { return nil }
        if parts.scheme == "http", parts.port == nil, parts.host != "localhost" { parts.port = 8790 }
        parts.path = ""
        return AppConfig.origin(parts.string)
    }

    /// A name that works as a Finder volume name.
    nonisolated static func cleanName(_ raw: String?) -> String {
        let name = (raw ?? "").replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
            .trimmingCharacters(in: .whitespacesAndNewlines.union(.controlCharacters))
        return name.isEmpty ? defaultName : String(name.prefix(60))
    }

    /// Reads the Drive's name from its public /api/config (no sign-in needed).
    private func refreshDriveName() async {
        guard let base = URL(string: serverURL) else { return }
        var request = URLRequest(url: base.appendingPathComponent("api/config"))
        request.timeoutInterval = 10
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let raw = obj["drive_name"] as? String else { return }
        let name = Self.cleanName(raw)
        if name != driveName {
            driveName = name
            defaults.set(name, forKey: "driveName")
        }
    }

    private func start() async {
        guard hasServer else { return }
        cleanUpStaleVolumes()
        await refreshAccount()
        if wantsConnected { await ensureConnected() }
    }

    /// Checks the sign-in and how the Drive responds (also fills the status
    /// dashboard: email, shares, latency).
    func refreshAccount() async {
        guard hasServer, account != .signingIn, !checking else { return }
        checking = true
        defer { checking = false }
        await refreshDriveName()
        let started = Date()
        // Only the fields this check owns: the helper and network fields can
        // change while whoami runs, so a stale copy of status must not win.
        let checkedAt = Date()
        var reachable: Bool?
        var latency: Int?
        var info: (email: String, shares: [DriveStatus.Share])?
        do {
            let out = try await CLIRun(["--json", "--server", serverURL, "whoami"]).output()
            let who = DriveStatus.account(fromWhoami: out)
            account = .signedIn(who.username)
            info = (who.email, who.shares)
            reachable = true
            latency = Int(Date().timeIntervalSince(started) * 1000)
            if message == CLIError.signedOut.errorDescription { message = nil }
        } catch CLIError.signedOut {
            account = .signedOut
            reachable = true
            latency = status.latencyMs
        } catch {
            // Offline or the Drive is down: keep what we knew, retry later.
            reachable = false
            if account == .unknown { message = error.localizedDescription }
        }
        status.checkedAt = checkedAt
        status.driveReachable = reachable
        // While the helper runs it reports the real round trip (a warm
        // connection); this timing includes starting a process and signing in.
        if status.helperSince == nil || reachable == false { status.latencyMs = latency }
        if let info {
            status.email = info.email
            status.shares = info.shares
        }
    }

    /// Re-checks the Drive if the last check is older than maxAge.
    func refreshIfStale(maxAge: TimeInterval = 20) {
        if let checked = status.checkedAt, Date().timeIntervalSince(checked) < maxAge { return }
        Task { await refreshAccount() }
    }

    func signIn() {
        guard hasServer else { return }
        let device = (Host.current().localizedName ?? "Mac") + " (Finder)"
        do {
            let run = try CLIRun(["--json", "--server", serverURL, "login", "--device", device])
            loginRun = run
            account = .signingIn
            message = nil
            Task {
                defer { loginRun = nil }
                do {
                    let out = try await run.output()
                    let obj = try? JSONSerialization.jsonObject(with: out) as? [String: Any]
                    account = .signedIn(obj?["username"] as? String ?? "")
                    connect()
                } catch {
                    account = .signedOut
                    message = error.localizedDescription
                }
            }
        } catch {
            message = error.localizedDescription
        }
    }

    func cancelSignIn() {
        loginRun?.cancel()
    }

    func signOut() {
        disconnect()
        Task {
            _ = try? await CLIRun(["--json", "--server", serverURL, "logout"]).output()
            account = .signedOut
        }
    }

    // MARK: Connection

    func connect() {
        wantsConnected = true
        message = nil
        Task { await ensureConnected(openFinder: true) }
    }

    func disconnect() {
        wantsConnected = false
        if let volume = connectedVolume {
            // Clear the state first: the unmount notification arrives later
            // and must not look like a Finder eject.
            connection = .disconnected
            do {
                try Mounter.unmount(volume, force: false)
            } catch {
                wantsConnected = true
                connection = .connected(volume)
                message = "Close files that are open on \(driveName), then try again."
                return
            }
        }
        stopHelper()
        connection = .disconnected
    }

    func openInFinder() {
        if let volume = connectedVolume { NSWorkspace.shared.open(volume) }
    }

    func openShare(_ share: DriveStatus.Share) {
        guard let volume = connectedVolume else { return }
        NSWorkspace.shared.open(volume.appendingPathComponent(share.name, isDirectory: true))
    }

    /// Starts unlocking an encrypted personal folder (shown in the Unlock window).
    func beginUnlock(_ share: DriveStatus.Share) {
        // Fresh each time (never a half-finished step), unless one is running.
        if unlocking?.busy != true {
            unlocking = PersonalUnlock(share: share, drive: self)
        }
    }

    /// After an unlock: refresh the shares and open the folder in Finder.
    func personalFolderUnlocked(_ share: DriveStatus.Share) async {
        await refreshAccount()
        if let fresh = status.shares.first(where: { $0.id == share.id }), !fresh.locked {
            openShare(fresh)
        }
    }

    /// True while Finder is copying to or from the Drive (don't restart now).
    var isTransferring: Bool {
        openWrites > 0 || transfers.contains { $0.state == .active }
    }

    func openDriveWebsite() {
        if let url = URL(string: serverURL) { NSWorkspace.shared.open(url) }
    }

    static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
    }

    private var connectionText: String {
        switch connection {
        case .connected(let volume): return "mounted at \(volume.path)"
        case .connecting: return "connecting"
        case .disconnected: return wantsConnected ? "not connected (will retry)" : "disconnected"
        case .failed(let reason): return "failed — \(reason)"
        }
    }

    func copyDiagnostics() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(diagnostics(), forType: .string)
    }

    /// A plain-text summary for support. No tokens or passwords.
    func diagnostics() -> String {
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        let who: String
        switch account {
        case .signedIn(let user): who = "signed in as \(user)"
        case .signedOut: who = "signed out"
        case .signingIn: who = "signing in"
        case .unknown: who = "unknown"
        }
        let drive: String
        switch status.driveReachable {
        case true?: drive = "reachable" + (status.latencyMs.map { " (\($0) ms)" } ?? "")
        case false?: drive = "unreachable"
        case nil: drive = "not checked"
        }
        let lines = [
            "Ugreen Drive for Mac \(Self.appVersion) on macOS \(os)",
            "Drive: \(serverHost) — \(drive)",
            "Account: \(who)",
            "Connection: " + connectionText,
            "Helper: " + (status.helperSince.map { "running since \(Formatting.time($0))" } ?? "stopped")
                + " (restarts: \(status.helperRestarts))",
            "Network: " + (status.online ? "online \(status.networkKind), " + (status.viaLAN ? "direct to the NAS (LAN)" : "over the internet") : "offline"),
            "Shares: \(status.shares.count)",
            "Transfers: " + transfers.map { "\($0.name) \($0.state.rawValue)\($0.error.isEmpty ? "" : ": " + $0.error)" }
                .joined(separator: "; "),
            "Updates: " + updater.summary,
            "Last message: \(message ?? "none")",
        ]
        return lines.joined(separator: "\n")
    }

    var connectedVolume: URL? {
        if case .connected(let volume) = connection { return volume }
        return nil
    }

    /// Makes sure the helper runs and the volume is mounted. Safe to call
    /// often: on launch, wake, network changes and a timer.
    func ensureConnected(openFinder: Bool = false) async {
        guard wantsConnected, hasServer, !working else { return }
        switch account {
        case .signedOut, .signingIn: return
        default: break
        }
        working = true
        defer { working = false }
        if connectedVolume == nil { connection = .connecting }
        do {
            let url = try await runningHelper()
            let volume = try await mountedVolume(for: url)
            guard wantsConnected else {
                // Disconnect was clicked while the mount was in progress.
                try? Mounter.unmount(volume, force: true)
                return
            }
            connection = .connected(volume)
            message = nil
            if openFinder { NSWorkspace.shared.open(volume) }
        } catch CLIError.signedOut {
            handleSignedOut()
        } catch {
            connection = .failed(error.localizedDescription)
        }
    }

    private func runningHelper() async throws -> URL {
        if dav.isRunning, let mountURL { return mountURL }
        // A volume still pointing at an old helper's port must go before a
        // new helper starts: another account could take that port over.
        cleanUpStaleVolumes()
        davPassword = Self.randomPassword()
        let url = try await dav.start(server: serverURL, port: 0, password: davPassword, name: driveName)
        defaults.set(driveName, forKey: "mountedName")
        mountURL = url
        restartDelay = 1
        status.helperSince = Date()
        return url
    }

    private func mountedVolume(for url: URL) async throws -> URL {
        let ours = ourVolumes()
        if let current = ours.first(where: { $0.source.port == url.port }) {
            return current.volume
        }
        connection = .connecting
        cleanUpStaleVolumes()
        return try await Mounter.mount(url, user: Self.davUser, password: davPassword)
    }

    /// Unmounts volumes left by a helper that no longer runs (e.g. a crash).
    private func cleanUpStaleVolumes() {
        let live = dav.isRunning ? mountURL?.port : nil
        for stale in ourVolumes() where stale.source.port != live {
            try? Mounter.unmount(stale.volume, force: true)
        }
    }

    /// Volumes this app mounted: a loopback helper under the Drive's name, or
    /// the name it had when the last helper started (it may have been renamed).
    private func ourVolumes() -> [(volume: URL, source: URL)] {
        let names = Set([driveName, defaults.string(forKey: "mountedName") ?? driveName])
        return names.flatMap { Mounter.helperVolumes(path: "/" + $0) }
    }

    private func handleSignedOut() {
        let volume = connectedVolume
        connection = .disconnected
        if let volume { try? Mounter.unmount(volume, force: true) }
        stopHelper()
        account = .signedOut
        message = CLIError.signedOut.errorDescription
    }

    /// Stops the helper; uploads it hadn't finished didn't reach the Drive.
    private func stopHelper() {
        dav.stop()
        helperStopped()
    }

    private func helperStopped() {
        status.helperSince = nil
        status.viaLAN = false
        openWrites = 0
        transfers = transfers.map { transfer in
            guard transfer.state == .active else { return transfer }
            var interrupted = transfer
            interrupted.state = .failed
            interrupted.error = "Interrupted when the connection stopped. Copy the file again."
            interrupted.updatedAt = Date()
            return interrupted
        }
    }

    private func helperEvent(_ event: DavServer.Event) {
        switch event {
        case .route(let viaLAN):
            status.viaLAN = viaLAN
        case .latency(let ms, let viaLAN):
            status.latencyMs = ms
            status.viaLAN = viaLAN
            status.driveReachable = true
        case .writing(let open):
            openWrites = open
            if !isTransferring { updater.transfersIdle() }
        case .upload(let raw):
            if let transfer = Transfer(event: raw, now: Date()) {
                transfers = mergeTransfer(transfer, into: transfers, now: Date())
            }
            if !isTransferring { updater.transfersIdle() }
        case .signedOut, .exited(CLIError.exitAuth):
            helperStopped()
            handleSignedOut()
        case .exited:
            helperStopped()
            status.helperRestarts += 1
            // Crashed: drop the volume now (nothing may keep talking to a
            // port that is free again), then start a new helper and remount.
            if let volume = connectedVolume {
                connection = .connecting
                try? Mounter.unmount(volume, force: true)
            }
            guard wantsConnected else { return }
            let delay = restartDelay
            restartDelay = min(restartDelay * 2, 30)
            Task {
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                await ensureConnected()
            }
        }
    }

    /// Quit: unmount first so Finder doesn't keep a dead volume.
    func shutdown() {
        if let volume = connectedVolume {
            connection = .disconnected
            if (try? Mounter.unmount(volume, force: false)) == nil {
                try? Mounter.unmount(volume, force: true)
            }
        }
        stopHelper()
    }

    // MARK: Start at login

    func setStartsAtLogin(_ on: Bool) {
        do {
            try LoginItem.set(on)
        } catch {
            message = "Couldn't change Start at login: \(error.localizedDescription)"
        }
        startsAtLogin = LoginItem.isEnabled
    }

    // MARK: System events

    private func observeSystem() {
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await self?.refreshAccount()
                await self?.ensureConnected()
            }
        }
        center.addObserver(forName: NSWorkspace.didUnmountNotification, object: nil, queue: .main) { [weak self] note in
            let path = (note.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL)?.path
            Task { @MainActor in self?.volumeUnmounted(path: path) }
        }
        pathMonitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            let kind = path.usesInterfaceType(.wifi) ? "Wi-Fi"
                : path.usesInterfaceType(.wiredEthernet) ? "Ethernet"
                : path.usesInterfaceType(.cellular) ? "Cellular" : "Online"
            Task { @MainActor in
                guard let self else { return }
                let cameBack = online && !self.status.online
                self.status.online = online
                self.status.networkKind = online ? kind : ""
                self.dav.probeNetwork() // switch to/from the home network right away
                guard online else { return }
                if cameBack || self.account == .unknown { await self.refreshAccount() }
                await self.ensureConnected()
            }
        }
        pathMonitor.start(queue: .main)
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.transfers = pruneTransfers(self.transfers, now: Date())
                self.refreshIfStale(maxAge: 120)
                await self.ensureConnected()
            }
        }
    }

    /// Ejecting the volume in Finder means "disconnect"; the helper is stopped
    /// and it isn't remounted until Connect is clicked again.
    private func volumeUnmounted(path: String?) {
        guard let volume = connectedVolume, volume.path == path else { return }
        connection = .disconnected
        if dav.isRunning {
            wantsConnected = false
            stopHelper()
        }
    }

    private static func randomPassword() -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}
