import AppKit

/// The menu bar while a window is open. Built in AppKit because the app has no
/// SwiftUI window scene, and text fields need a real Edit menu for Copy/Paste
/// shortcuts.
@MainActor
enum MainMenu {
    static let identifier = NSUserInterfaceItemIdentifier("UgreenDriveMainMenu")

    /// Installs the menu unless it's already the current one.
    static func install() {
        guard NSApp.mainMenu?.identifier != identifier else { return }
        let menu = NSMenu()
        menu.identifier = identifier
        [appMenu(), fileMenu(), editMenu(), windowMenu(), helpMenu()]
            .forEach { submenu in
                let item = NSMenuItem(title: submenu.title, action: nil, keyEquivalent: "")
                item.submenu = submenu
                menu.addItem(item)
            }
        NSApp.mainMenu = menu
    }

    private static var appName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String ?? "Ugreen Drive"
    }

    private static func appMenu() -> NSMenu {
        let menu = NSMenu(title: appName)
        menu.addItem(withTitle: "About \(appName)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Hide \(appName)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let others = menu.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        others.keyEquivalentModifierMask = [.command, .option]
        menu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        menu.addItem(.separator())
        // With "Keep Drive connected after Quit" on, Quit leaves Drive mounted; hold Option for a full quit.
        menu.addItem(withTitle: "Quit \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let quitAll = action("Quit \(appName) Completely", #selector(MenuActions.quitCompletely(_:)), "q")
        quitAll.keyEquivalentModifierMask = [.command, .option]
        quitAll.isAlternate = true
        menu.addItem(quitAll)
        return menu
    }

    private static func fileMenu() -> NSMenu {
        let menu = NSMenu(title: "File")
        menu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        menu.addItem(.separator())
        menu.addItem(action("Sign Out", #selector(MenuActions.signOut(_:)), ""))
        return menu
    }

    private static func editMenu() -> NSMenu {
        let menu = NSMenu(title: "Edit")
        menu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        menu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        menu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        menu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        menu.addItem(withTitle: "Delete", action: #selector(NSText.delete(_:)), keyEquivalent: "")
        menu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        return menu
    }

    private static func windowMenu() -> NSMenu {
        let menu = NSMenu(title: "Window")
        menu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        menu.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(action("Drive", #selector(MenuActions.showDrive(_:)), "D"))
        menu.addItem(.separator())
        menu.addItem(withTitle: "Bring All to Front", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
        NSApp.windowsMenu = menu
        return menu
    }

    private static func helpMenu() -> NSMenu {
        let menu = NSMenu(title: "Help")
        menu.addItem(action("\(appName) Help", #selector(MenuActions.showHelp(_:)), ""))
        NSApp.helpMenu = menu
        return menu
    }

    private static func action(_ title: String, _ selector: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = MenuActions.shared
        return item
    }
}

/// App-level menu commands (no window needed).
@MainActor
final class MenuActions: NSObject {
    static let shared = MenuActions()
    weak var drive: DriveController?

    @objc func quitCompletely(_ sender: Any?) {
        QuitPolicy.quitCompletely()
    }

    @objc func showDrive(_ sender: Any?) {
        if let drive { Windows.shared.showMain(drive) }
    }

    @objc func showHelp(_ sender: Any?) {
        if let drive { Windows.shared.showHelp(drive) }
    }

    @objc func signOut(_ sender: Any?) {
        drive?.signOut()
    }
}
