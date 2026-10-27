import SwiftUI

/// The menu-bar window: what's happening now, everything's status, shares,
/// uploads, and a way to Help.
struct MenuView: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        VStack(spacing: 0) {
            MenuHeader(drive: drive)
            VStack(alignment: .leading, spacing: 16) {
                if drive.hasServer {
                    HeroCard(drive: drive)
                    if let message = drive.message { Banner(text: message) }
                    if !drive.transfers.isEmpty { TransfersSection(transfers: drive.transfers) }
                    StatusSection(drive: drive)
                    if case .signedIn = drive.account, !drive.status.shares.isEmpty {
                        SharesSection(drive: drive)
                    }
                } else {
                    Onboarding(drive: drive)
                }
            }
            .padding(16)
            MenuFooter(drive: drive)
        }
        .frame(width: 360)
        .background(Brand.background)
        .foregroundStyle(Brand.foreground)
        .onAppear { drive.refreshIfStale() }
    }
}

private struct MenuHeader: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Wordmark(name: drive.driveName, size: 13)
            Spacer()
            StatePill(drive: drive)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Brand.card)
        .overlay(alignment: .bottom) { Rectangle().fill(Brand.border).frame(height: 1) }
    }
}

/// Short state label with a tone: green when connected, danger on problems.
struct StatePill: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        let (text, tone) = drive.summary
        HStack(spacing: 6) {
            Circle().fill(tone.color).frame(width: 6, height: 6)
            Text(text).font(.sans(11, .medium))
        }
        .foregroundStyle(tone == .idle ? Brand.muted : tone.color)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(tone.tint, in: Rectangle())
    }
}

enum Tone: Equatable {
    case ok, busy, problem, idle

    var color: Color {
        switch self {
        case .ok: return Brand.accent
        case .problem: return Brand.danger
        case .busy, .idle: return Brand.muted
        }
    }

    var tint: Color {
        switch self {
        case .ok: return Brand.accentTint
        case .problem: return Brand.dangerTint
        case .busy, .idle: return Brand.secondary
        }
    }
}

extension DriveController {
    /// One line for the header pill and the menu-bar icon.
    var summary: (String, Tone) {
        if !hasServer { return ("Set up", .idle) }
        switch (account, connection) {
        case (.signingIn, _): return ("Signing in", .busy)
        case (.signedOut, _): return ("Signed out", .problem)
        case (_, .connected): return ("Connected", .ok)
        case (_, .connecting): return ("Connecting", .busy)
        case (_, .failed): return ("Problem", .problem)
        default: return ("Not connected", .idle)
        }
    }
}

/// The big card: the one thing to do next.
struct HeroCard: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                ZStack {
                    Rectangle().fill(tone.tint)
                    if busy {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: symbol)
                            .font(.system(size: 17, weight: .medium))
                            .foregroundStyle(tone == .idle ? Brand.muted : tone.color)
                    }
                }
                .frame(width: 40, height: 40)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.sans(15, .semibold)).tracking(-0.2)
                    Text(subtitle)
                        .font(subtitleIsData ? .mono(11) : .sans(12))
                        .foregroundStyle(failed ? Brand.danger : Brand.muted)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            actions
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Brand.card)
        .overlay(Rectangle().strokeBorder(Brand.border))
    }

    private var tone: Tone { drive.summary.1 }
    private var failed: Bool {
        if case .failed = drive.connection { return true }
        return false
    }
    private var busy: Bool { tone == .busy }

    private var symbol: String {
        switch (drive.account, drive.connection) {
        case (.signedOut, _): return "person.crop.circle.badge.exclamationmark"
        case (_, .connected): return "externaldrive.fill.badge.checkmark"
        case (_, .failed): return "exclamationmark.triangle"
        default: return "externaldrive"
        }
    }

    private var title: String {
        switch (drive.account, drive.connection) {
        case (.signingIn, _): return "Finish in your browser"
        case (.signedOut, _): return "Sign in to use Finder"
        case (_, .connected): return "Your Drive is in Finder"
        case (_, .connecting): return "Connecting…"
        case (_, .failed): return "Couldn't connect"
        default: return "Not connected"
        }
    }

    private var subtitleIsData: Bool { drive.connectedVolume != nil }

    private var subtitle: String {
        switch (drive.account, drive.connection) {
        case (.signingIn, _): return "Sign in to \(drive.driveName) in your browser, then click Allow."
        case (.signedOut, _): return "Use your NAS account, the same one as on the Drive website."
        case (_, .connected(let volume)): return volume.path
        case (_, .connecting): return "Starting the helper and mounting the volume."
        case (_, .failed(let reason)): return reason
        default: return "Connect to see your shares under Locations in Finder."
        }
    }

    @ViewBuilder private var actions: some View {
        switch (drive.account, drive.connection) {
        case (.signingIn, _):
            Button("Cancel") { drive.cancelSignIn() }.buttonStyle(SecondaryButtonStyle())
        case (.signedOut, _):
            Button { drive.signIn() } label: {
                Label("Sign in", systemImage: "person.badge.key")
            }
            .buttonStyle(PrimaryButtonStyle())
        case (_, .connected):
            HStack(spacing: 8) {
                Button { drive.openInFinder() } label: { Label("Open in Finder", systemImage: "folder") }
                    .buttonStyle(PrimaryButtonStyle())
                Button("Disconnect") { drive.disconnect() }
                    .buttonStyle(SecondaryButtonStyle())
                    .frame(width: 112)
            }
        case (_, .connecting):
            EmptyView()
        case (_, .failed):
            Button("Try again") { drive.connect() }.buttonStyle(PrimaryButtonStyle())
        default:
            Button { drive.connect() } label: { Label("Connect", systemImage: "externaldrive.badge.plus") }
                .buttonStyle(PrimaryButtonStyle())
        }
    }
}

struct Banner: View {
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(Brand.danger)
            Text(text).font(.sans(12)).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(Brand.dangerTint, in: Rectangle())
    }
}

/// First run: three steps, the first one is entering the Drive address.
struct Onboarding: View {
    @ObservedObject var drive: DriveController
    @State private var address = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            StepMarkers(current: 0)
            VStack(alignment: .leading, spacing: 6) {
                Text("YOUR DRIVE, IN FINDER.")
                    .font(.mono(17, .semibold))
                    .tracking(0.6)
                Text("Browse, open and save files on your Ugreen NAS like any folder on your Mac. You sign in with your NAS account.")
                    .font(.sans(12.5))
                    .foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: 6) {
                SectionLabel(text: "Drive address")
                TextField("drive.example.com or 192.168.1.20:8790", text: $address)
                    .textFieldStyle(.plain)
                    .font(.mono(12.5))
                    .padding(.horizontal, 10)
                    .frame(height: 34)
                    .background(Brand.card, in: Rectangle())
                    .overlay(Rectangle().strokeBorder(Brand.border))
                    .onSubmit { drive.setServer(address) }
            }
            Button("Continue") { drive.setServer(address) }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
            if let message = drive.message { Banner(text: message) }
        }
    }
}

private struct StepMarkers: View {
    let current: Int
    private let steps = ["Address", "Sign in", "Finder"]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                HStack(spacing: 6) {
                    Text("\(index + 1)")
                        .font(.mono(10, .medium))
                        .foregroundStyle(index == current ? Brand.onBrand : Brand.muted)
                        .frame(width: 18, height: 18)
                        .background(index == current ? Brand.fill : Brand.secondary, in: Rectangle())
                    Text(step)
                        .font(.sans(11, index == current ? .semibold : .regular))
                        .lineLimit(1)
                        .fixedSize()
                        .foregroundStyle(index == current ? Brand.foreground : Brand.muted)
                }
                if index < steps.count - 1 {
                    Rectangle().fill(Brand.border).frame(height: 1).frame(maxWidth: .infinity)
                }
            }
        }
    }
}

private struct MenuFooter: View {
    @ObservedObject var drive: DriveController
    @AppStorage(showInMenuBarKey) private var showInMenuBar = true

    var body: some View {
        HStack(spacing: 12) {
            Button { Windows.shared.showMain(drive) } label: { Image(systemName: "externaldrive") }
                .buttonStyle(LinkButtonStyle())
                .help("Open the Drive window")
            Button { Windows.shared.showHelp(drive) } label: { Image(systemName: "questionmark.circle") }
                .buttonStyle(LinkButtonStyle())
                .help("Help")
            if drive.hasServer {
                Menu {
                    if case .signedIn = drive.account {
                        Button("Sign out") { drive.signOut() }
                    }
                    if drive.hasServer {
                        Button("Open Drive in browser") { drive.openDriveWebsite() }
                    }
                    Button("Copy diagnostics") { drive.copyDiagnostics() }
                    Button("Show helper log") { NSWorkspace.shared.open(HelperLog.url) }
                    Divider()
                    Button("Hide menu bar icon") {
                        // Keeps running in the background; opening the app shows its window.
                        Windows.shared.showMain(drive)
                        showInMenuBar = false
                    }
                    if drive.hasServer {
                        Button("Change Drive address…") { drive.changeServer() }
                    }
                } label: {
                    Label("Account", systemImage: "person.crop.circle")
                        .font(.sans(12, .medium))
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .foregroundStyle(Brand.muted)
            }
            UpdateButton(updater: drive.updater)
            Spacer(minLength: 4)
            VersionLabel(updater: drive.updater)
            Button("Quit Completely") { QuitPolicy.quitCompletely() }
                .buttonStyle(LinkButtonStyle()).fixedSize()
                .help("Disconnects Drive and closes the app")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Brand.card)
        .overlay(alignment: .top) { Rectangle().fill(Brand.border).frame(height: 1) }
    }
}
