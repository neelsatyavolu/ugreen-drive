import AppKit

/// A downloaded app opens straight from Downloads, where macOS runs it
/// from a read-only, randomized copy (App Translocation): it can't update itself,
/// rename itself or start at login from there. On the first open outside an
/// Applications folder, offer to move to /Applications (or ~/Applications when
/// that isn't writable, e.g. a non-admin account) and relaunch from there.
enum MoveToApplications {
    static let declinedKey = "moveToApplicationsDeclined"
    static let appName = "Ugreen Drive.app"
    /// Older copies in the destination folder that this one replaces.
    static let replacedNames = ["Ugreen Drive.app"]

    private static func applicationFolders(home: URL) -> [String] {
        ["/Applications", home.appendingPathComponent("Applications").standardizedFileURL.path]
    }

    /// Already in /Applications or ~/Applications (or a folder inside them).
    static func isInstalled(_ bundle: URL, home: URL) -> Bool {
        let path = bundle.standardizedFileURL.path
        return applicationFolders(home: home).contains { path.hasPrefix($0 + "/") }
    }

    static func shouldOffer(bundle: URL, home: URL, background: Bool, declined: Bool) -> Bool {
        bundle.pathExtension == "app" && !background && !declined && !isInstalled(bundle, home: home)
    }

    static func destinationFolder(home: URL, systemWritable: Bool) -> URL {
        systemWritable ? URL(fileURLWithPath: "/Applications") : home.appendingPathComponent("Applications")
    }

    /// Returns normally unless the app moved (then it relaunches and exits).
    @MainActor
    static func offerIfNeeded() {
        let bundle = Bundle.main.bundleURL
        let home = FileManager.default.homeDirectoryForCurrentUser
        let defaults = UserDefaults.standard
        guard shouldOffer(bundle: bundle, home: home,
                          background: CommandLine.arguments.contains("--background"),
                          declined: defaults.bool(forKey: declinedKey)) else { return }

        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Move Ugreen Drive to your Applications folder?"
        alert.informativeText = "Ugreen Drive needs to live in Applications to keep itself up to date and start when you log in."
        alert.addButton(withTitle: "Move to Applications")
        alert.addButton(withTitle: "Not Now")
        alert.showsSuppressionButton = true
        alert.suppressionButton?.title = "Don't ask again"
        let choice = alert.runModal()
        if alert.suppressionButton?.state == .on { defaults.set(true, forKey: declinedKey) }
        guard choice == .alertFirstButtonReturn else { return }

        do {
            let destination = try move(bundle, home: home)
            Updater.relaunch(destination, showWindow: true)
            exit(0) // nothing is running yet: no controller, no mount
        } catch {
            let failure = NSAlert()
            failure.messageText = "Ugreen Drive couldn't move itself"
            failure.informativeText = "Drag Ugreen Drive into your Applications folder in Finder, then open it from there.\n\n\(error.localizedDescription)"
            failure.runModal()
        }
    }

    private static func move(_ bundle: URL, home: URL) throws -> URL {
        let files = FileManager.default
        let folder = destinationFolder(home: home, systemWritable: files.isWritableFile(atPath: "/Applications"))
        try files.createDirectory(at: folder, withIntermediateDirectories: true)
        for name in replacedNames {
            let old = folder.appendingPathComponent(name)
            if files.fileExists(atPath: old.path) { try files.trashItem(at: old, resultingItemURL: nil) }
        }
        let destination = folder.appendingPathComponent(appName)
        try files.copyItem(at: bundle, to: destination)
        // Already notarized and opened once; don't make macOS ask again for the copy.
        let xattr = Process()
        xattr.executableURL = URL(fileURLWithPath: "/usr/bin/xattr")
        xattr.arguments = ["-dr", "com.apple.quarantine", destination.path]
        try? xattr.run()
        xattr.waitUntilExit()
        // Tidy up the download (best effort; a disk image is read-only).
        if let original = originalLocation(of: bundle), original.standardizedFileURL != destination.standardizedFileURL {
            try? files.trashItem(at: original, resultingItemURL: nil)
        }
        return destination
    }

    /// The real location of a translocated app (Security.framework exports these
    /// without a public header), or the bundle itself when it isn't translocated.
    private static func originalLocation(of bundle: URL) -> URL? {
        typealias IsTranslocated = @convention(c) (CFURL, UnsafeMutablePointer<DarwinBoolean>, UnsafeMutablePointer<Unmanaged<CFError>?>?) -> DarwinBoolean
        typealias OriginalPath = @convention(c) (CFURL, UnsafeMutablePointer<Unmanaged<CFError>?>?) -> Unmanaged<CFURL>?
        guard let handle = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_LAZY) else { return bundle }
        defer { dlclose(handle) }
        guard let isSymbol = dlsym(handle, "SecTranslocateIsTranslocatedURL"),
              let originalSymbol = dlsym(handle, "SecTranslocateCreateOriginalPathForURL") else { return bundle }
        let isTranslocated = unsafeBitCast(isSymbol, to: IsTranslocated.self)
        let originalPath = unsafeBitCast(originalSymbol, to: OriginalPath.self)
        var translocated: DarwinBoolean = false
        guard isTranslocated(bundle as CFURL, &translocated, nil).boolValue, translocated.boolValue else { return bundle }
        return originalPath(bundle as CFURL, nil)?.takeRetainedValue() as URL?
    }
}
