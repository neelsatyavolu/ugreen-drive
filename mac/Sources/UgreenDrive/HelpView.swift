import SwiftUI

/// The Help window: your setup at a glance, how to use it, fixes for common
/// problems, and how it keeps your files safe.
struct HelpView: View {
    @ObservedObject var drive: DriveController
    @State private var copied = false

    static let docsURL = URL(string: "https://github.com/neelsatyavolu/ugreen-drive/blob/main/docs/MAC-APP.md")!

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                header
                setupCard
                HelpSection(title: "Getting started") {
                    NumberedStep(1, "Open **Ugreen Drive** from Applications (or its drive icon in the menu bar).")
                    NumberedStep(2, "Enter your Drive address and click **Continue**.")
                    NumberedStep(3, "Click **Sign in**. Your browser opens the Drive: sign in with your NAS account and click **Allow**.")
                    NumberedStep(4, "**\(drive.driveName)** appears in Finder under **Locations**. Turn on **Start at login** to keep it there.")
                }
                HelpSection(title: "Using the drive") {
                    Bullet("folder", "Each share you can open is a folder at the top of the drive. Your personal folder is there too.")
                    Bullet("lock", "Shares marked **Read only** can be opened and copied from, but not changed.")
                    Bullet("lock.open", "An encrypted personal folder shows a lock until you click **Unlock** and enter your UGOS encryption password or key file. It stays unlocked everywhere — Finder and the website — for 24 hours, then relocks.")
                    Bullet("arrow.up.doc", "Saving or copying a file uploads it when Finder finishes writing it. Watch **Uploads** in the menu for progress.")
                    Bullet("trash", "Deleting moves items to the share's **Recycle bin** on the Drive, so they can be recovered.")
                    Bullet("eject", "Ejecting the drive in Finder disconnects it until you click **Connect** again.")
                    Bullet("menubar.rectangle", "The menu bar icon is optional (**Settings → Show in menu bar**). Without it, the app keeps the drive mounted in the background; open the app from Applications or Spotlight to see this window.")
                }
                HelpSection(title: "Fix a problem") {
                    FAQItem("The drive isn't in Finder",
                            "Open the menu and click **Connect**. If **Status** shows *Signed out*, sign in again. If the network or Drive row is red, the Drive can't be reached right now. The app reconnects by itself when it can.")
                    FAQItem("A file won't save",
                            "Check that the share isn't **Read only**. File names can't contain a backslash (\\\\). A failed upload shows in **Uploads** with the reason. Big files are copied to your Mac first, so make sure there is free disk space.")
                    FAQItem("My personal folder won't open",
                            "It's encrypted and locked. In the menu, click it under **Shares** and enter your encryption password or key file. If UGOS asks, sign in to the NAS as yourself first (and enter your authenticator code). The folder relocks after 24 hours.")
                    FAQItem("It says I'm signed out",
                            "Sign-ins end after 30 days without use, or when they're revoked on the Drive (sidebar → **Mac app**). Click **Sign in** to sign in again. Your files aren't affected.")
                    FAQItem("Finder says the server connection was interrupted",
                            "Wait a few seconds: the app restarts its helper and mounts the drive again. If it keeps happening, quit and reopen the app, then use **Copy diagnostics** below.")
                    FAQItem("macOS says the app can't be opened",
                            "Release builds are signed and notarized by Apple, so macOS only asks once whether to open an app downloaded from the internet. If it still refuses, delete the app and install it again with the command from the Drive's **Mac app** window.")
                    FAQItem("Changes from the website don't show up",
                            "Finder updates folders every few seconds. Close and reopen the folder, or press ⌘R in some apps to reload.")
                }
                HelpSection(title: "Privacy and security") {
                    Bullet("key", "You type your NAS password on the Drive's own page in your browser. The app only keeps a Drive token in your login Keychain, never the password.")
                    Bullet("lock.shield", "Finder talks to a helper that only listens on this Mac and needs a new random password every time it starts.")
                    Bullet("eye.slash", "Finder's hidden files (.DS_Store, ._ files) stay on your Mac and are never uploaded.")
                    Bullet("person.2", "You see exactly what the Drive website shows you, with the same permissions.")
                }
                footerLinks
            }
            .padding(28)
        }
        .frame(width: 560, height: 680)
        .background(Brand.background)
        .foregroundStyle(Brand.foreground)
        .onAppear { drive.refreshIfStale() }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 6) {
                Wordmark(name: drive.driveName, size: 15)
                Text("Help · version \(DriveController.appVersion)").font(.mono(11)).foregroundStyle(Brand.muted)
            }
            Spacer()
            StatePill(drive: drive)
        }
    }

    private var setupCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Your setup")
            Text(drive.diagnostics())
                .font(.mono(11))
                .textSelection(.enabled)
                .foregroundStyle(Brand.muted)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(Brand.card)
                .overlay(Rectangle().strokeBorder(Brand.border))
            HStack(spacing: 8) {
                Button(copied ? "Copied" : "Copy diagnostics") {
                    drive.copyDiagnostics()
                    copied = true
                }
                .buttonStyle(SecondaryButtonStyle())
                Button("Show helper log") { NSWorkspace.shared.open(HelperLog.url) }
                    .buttonStyle(SecondaryButtonStyle())
            }
            Text("Diagnostics never include your sign-in token or passwords. Paste them when you ask for help.")
                .font(.sans(11))
                .foregroundStyle(Brand.muted)
        }
    }

    private var footerLinks: some View {
        HStack(spacing: 8) {
            Button("Open Drive in browser") { drive.openDriveWebsite() }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(!drive.hasServer)
            Button("Full documentation") { NSWorkspace.shared.open(Self.docsURL) }
                .buttonStyle(SecondaryButtonStyle())
        }
    }
}

private struct HelpSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title.uppercased()).font(.mono(13, .semibold)).tracking(0.8)
            VStack(alignment: .leading, spacing: 10) { content }
        }
    }
}

private struct NumberedStep: View {
    let number: Int
    let text: LocalizedStringKey

    init(_ number: Int, _ text: LocalizedStringKey) {
        self.number = number
        self.text = text
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("\(number)")
                .font(.mono(10.5, .medium))
                .foregroundStyle(Brand.onBrand)
                .frame(width: 20, height: 20)
                .background(Brand.fill, in: Rectangle())
                .alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
            Text(text).font(.sans(12.5)).fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct Bullet: View {
    let symbol: String
    let text: LocalizedStringKey

    init(_ symbol: String, _ text: LocalizedStringKey) {
        self.symbol = symbol
        self.text = text
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Brand.accent)
                .frame(width: 18)
            Text(text).font(.sans(12.5)).fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct FAQItem: View {
    let question: String
    let answer: LocalizedStringKey
    @State private var open = false

    init(_ question: String, _ answer: LocalizedStringKey) {
        self.question = question
        self.answer = answer
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } } label: {
                HStack {
                    Text(question).font(.sans(12.5, .medium))
                    Spacer()
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Brand.muted)
                        .rotationEffect(.degrees(open ? 180 : 0))
                }
                .padding(.horizontal, 12)
                .frame(height: 36)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                Text(answer)
                    .font(.sans(12))
                    .foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
            }
        }
        .background(Brand.card)
        .overlay(Rectangle().strokeBorder(Brand.border))
    }
}
