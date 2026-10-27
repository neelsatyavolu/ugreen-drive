import SwiftUI

/// The main window: everything the menu shows, plus settings. It's what you
/// get when you open the app, so the menu bar icon is optional.
struct MainWindowView: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        VStack(spacing: 0) {
            header
            if drive.hasServer {
                HStack(alignment: .top, spacing: 20) {
                    VStack(alignment: .leading, spacing: 16) {
                        HeroCard(drive: drive)
                        if let message = drive.message { Banner(text: message) }
                        if !drive.transfers.isEmpty { TransfersSection(transfers: drive.transfers) }
                        if case .signedIn = drive.account, !drive.status.shares.isEmpty {
                            SharesSection(drive: drive, visibleRows: 7)
                        }
                    }
                    .frame(width: 360)
                    VStack(alignment: .leading, spacing: 16) {
                        StatusSection(drive: drive)
                        SettingsSection(drive: drive)
                        SpeedTestSection(drive: drive, test: drive.speedTest)
                    }
                    .frame(width: 360)
                }
                .padding(20)
            } else {
                Onboarding(drive: drive)
                    .frame(width: 380)
                    .padding(32)
            }
        }
        .background(Brand.background)
        .foregroundStyle(Brand.foreground)
        .onAppear { drive.refreshIfStale() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Wordmark(name: drive.driveName, size: 15)
                Text(drive.hasServer ? drive.serverHost : "Your Drive, in Finder")
                    .font(.mono(11))
                    .foregroundStyle(Brand.muted)
            }
            Spacer()
            UpdateButton(updater: drive.updater)
            StatePill(drive: drive)
            Button { Windows.shared.showHelp(drive) } label: {
                Label("Help", systemImage: "questionmark.circle")
            }
            .buttonStyle(LinkButtonStyle())
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(Brand.card)
        .overlay(alignment: .bottom) { Rectangle().fill(Brand.border).frame(height: 1) }
    }
}

/// Settings: menu bar icon, start at login, quit behavior, account actions.
struct SettingsSection: View {
    @ObservedObject var drive: DriveController
    @AppStorage(showInMenuBarKey) private var showInMenuBar = true
    @AppStorage(QuitPolicy.keepDriveKey) private var keepDriveAfterQuit = true

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Settings")
            VStack(spacing: 0) {
                SettingToggle(symbol: "menubar.rectangle", title: "Show in menu bar",
                              detail: showInMenuBar ? "Status and shares one click away"
                                  : "Runs in the background. Open the app to see this window.",
                              isOn: $showInMenuBar)
                Rectangle().fill(Brand.border).frame(height: 1)
                SettingToggle(symbol: "power", title: "Start at login",
                              detail: "Mounts the drive in the background when you log in",
                              isOn: Binding(get: { drive.startsAtLogin }, set: { drive.setStartsAtLogin($0) }))
                Rectangle().fill(Brand.border).frame(height: 1)
                SettingToggle(symbol: "externaldrive.badge.checkmark", title: "Keep Drive connected after Quit",
                              detail: keepDriveAfterQuit ? "Quit closes the window; Drive stays in Finder"
                                  : "Quit disconnects Drive and closes the app",
                              isOn: $keepDriveAfterQuit)
                Rectangle().fill(Brand.border).frame(height: 1)
                UpdateSetting(updater: drive.updater)
            }
            .background(Brand.card)
            .overlay(Rectangle().strokeBorder(Brand.border))
            HStack(spacing: 14) {
                Button("Open Drive in browser") { drive.openDriveWebsite() }
                Button("Copy diagnostics") { drive.copyDiagnostics() }
                Menu("More") {
                    Button("Show helper log") { NSWorkspace.shared.open(HelperLog.url) }
                    if case .signedIn = drive.account {
                        Button("Sign out") { drive.signOut() }
                    }
                    Button("Change Drive address…") { drive.changeServer() }
                    Divider()
                    Button("Quit Completely") { QuitPolicy.quitCompletely() }
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
            }
            .buttonStyle(LinkButtonStyle())
            .font(.sans(12, .medium))
            .padding(.top, 2)
        }
    }
}

private struct SettingToggle: View {
    let symbol: String
    let title: String
    let detail: String
    @Binding var isOn: Bool

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Brand.muted)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.sans(12.5, .medium))
                Text(detail).font(.sans(11)).foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Toggle("", isOn: $isOn)
                .toggleStyle(.switch)
                .controlSize(.mini)
                .labelsHidden()
                .tint(Brand.accent)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }
}
