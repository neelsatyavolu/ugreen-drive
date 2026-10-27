import AppKit

/// "Keep Drive connected after Quit" (on by default): Quit from the keyboard
/// (Cmd+Q), the app menu or the Dock closes the app's windows and drops the
/// Dock icon, but the app keeps running in the menu bar so Drive stays mounted.
/// A full quit always unmounts and exits: Quit Completely (menu bar,
/// Drive window, Option+Cmd+Q), updates, log out / restart / shut down, and quit
/// requests from other programs (the installer script).
enum QuitPolicy {
    static let keepDriveKey = "keepDriveConnectedAfterQuit"

    enum Outcome: Equatable { case terminate, background }

    static func decide(keepDrive: Bool, fullQuitRequested: Bool, externalQuit: Bool) -> Outcome {
        keepDrive && !fullQuitRequested && !externalQuit ? .background : .terminate
    }

    static var keepDrive: Bool {
        UserDefaults.standard.object(forKey: keepDriveKey) as? Bool ?? true
    }

    /// Set right before an explicit full quit.
    @MainActor static var fullQuitRequested = false

    @MainActor static func quitCompletely() {
        fullQuitRequested = true
        NSApp.terminate(nil)
    }

    /// The quit came as an Apple event from macOS ending the session (it carries
    /// a reason) or from a program other than the Dock (e.g. the installer's
    /// osascript). Cmd+Q and the menus send no event.
    static var isExternalQuit: Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent,
              event.eventClass == AEEventClass(kCoreEventClass),
              event.eventID == AEEventID(kAEQuitApplication) else { return false }
        if event.attributeDescriptor(forKeyword: AEKeyword(kAEQuitReason)) != nil { return true }
        guard let pid = event.attributeDescriptor(forKeyword: AEKeyword(keySenderPIDAttr))?.int32Value,
              pid > 0 else { return true }
        return NSRunningApplication(processIdentifier: pid)?.bundleIdentifier != "com.apple.dock"
    }
}
