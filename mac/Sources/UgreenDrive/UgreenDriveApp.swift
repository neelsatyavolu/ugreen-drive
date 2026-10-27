import SwiftUI

/// UserDefaults key: show the Drive icon in the menu bar.
let showInMenuBarKey = "showInMenuBar"

@main
struct UgreenDriveApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @AppStorage(showInMenuBarKey) private var showInMenuBar = true

    init() {
        // Writing to a helper that already exited must not kill the app.
        signal(SIGPIPE, SIG_IGN)
        Brand.registerFonts()
        #if DEBUG
        PreviewRenderer.runIfRequested()
        #endif
        // Before any scene (and so the controller) exists.
        AppDelegate.handOffToRunningCopy()
    }

    var body: some Scene {
        MenuBarExtra(isInserted: $showInMenuBar) {
            MenuView(drive: delegate.drive)
        } label: {
            MenuBarIcon(drive: delegate.drive)
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, ObservableObject {
    lazy var drive = DriveController()
    /// Another copy asks the running one to show its window through this.
    nonisolated static let showWindowNote = Notification.Name("io.github.neelsatyavolu.ugreen-drive.show-window")

    func applicationDidFinishLaunching(_ notification: Notification) {
        DistributedNotificationCenter.default().addObserver(
            forName: Self.showWindowNote, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.showMainWindow() }
        }
        // A fresh download offers to move itself first (relaunches from Applications).
        MoveToApplications.offerIfNeeded()
        _ = drive // start connecting now, whether or not any UI is visible
        drive.updater.start(drive: drive)
        MenuActions.shared.drive = drive
        // Start at login passes --background: stay quiet. Opening the app
        // yourself (Finder, Spotlight, Launchpad) shows the window.
        let background = CommandLine.arguments.contains("--background")
        if !background || !drive.hasServer {
            showMainWindow()
        }
    }

    /// Opening the app again while it runs (e.g. from Finder) shows the window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false // keep the Finder volume mounted in the background
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        switch QuitPolicy.decide(keepDrive: QuitPolicy.keepDrive,
                                 fullQuitRequested: QuitPolicy.fullQuitRequested,
                                 externalQuit: QuitPolicy.isExternalQuit) {
        case .background:
            Windows.shared.closeAllForQuit() // Drive stays mounted; menu bar icon stays
            return .terminateCancel
        case .terminate:
            drive.shutdown()
            return .terminateNow
        }
    }

    func showMainWindow() {
        Windows.shared.showMain(drive)
    }

    /// One copy per account: a second launch asks the running one to show
    /// its window (unless it's a background login launch) and quits.
    nonisolated static func handOffToRunningCopy() {
        guard let id = Bundle.main.bundleIdentifier else { return }
        let me = ProcessInfo.processInfo.processIdentifier
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: id)
            .filter { $0.processIdentifier != me }
        guard !others.isEmpty else { return }
        if !CommandLine.arguments.contains("--background") {
            DistributedNotificationCenter.default().postNotificationName(
                Self.showWindowNote, object: nil, userInfo: nil, deliverImmediately: true)
        }
        // exit, not terminate: this copy must never start its own controller
        // (it would clean up the running copy's volume as "stale").
        exit(0)
    }
}

struct MenuBarIcon: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        Image(systemName: symbol)
    }

    private var symbol: String {
        if drive.transfers.contains(where: { $0.state == .active }) { return "arrow.up.circle" }
        switch drive.summary.1 {
        case .ok: return "externaldrive.fill"
        case .problem: return "externaldrive.badge.exclamationmark"
        default: return "externaldrive"
        }
    }
}
