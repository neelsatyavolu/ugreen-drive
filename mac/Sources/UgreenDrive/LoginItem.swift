import ServiceManagement

/// Start at login, in the background: a LaunchAgent in the app bundle
/// (Contents/Library/LaunchAgents) runs the app with `--background`, so it
/// mounts the drive without opening a window. Opening the app yourself shows
/// the window instead.
enum LoginItem {
    static let plistName = "io.github.neelsatyavolu.ugreen-drive.login.plist"
    private static var agent: SMAppService { SMAppService.agent(plistName: plistName) }

    static var isEnabled: Bool { agent.status == .enabled }

    static func set(_ on: Bool) throws {
        if on {
            try agent.register()
            if agent.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
        } else {
            try agent.unregister()
        }
    }
}
