import AppKit
import SwiftUI

/// Unlocking an encrypted personal folder, the same steps as the web app:
/// the UGOS encryption password (or key file); if UGOS asks, first sign in
/// to the NAS as the folder owner (plus an authenticator code). Secrets go
/// to the bundled CLI on stdin and are dropped as soon as they're sent.
@MainActor
final class PersonalUnlock: ObservableObject {
    enum Step: Equatable { case key, nasPassword, code(pending: String) }

    let share: DriveStatus.Share
    @Published private(set) var step: Step
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published var keyFile: URL?
    @Published private(set) var done = false

    private weak var drive: DriveController?

    init(share: DriveStatus.Share, drive: DriveController) {
        self.share = share
        self.drive = drive
        step = share.needsOwnerSignIn ? .nasPassword : .key
    }

    var owner: String { String(share.id.dropFirst()) }

    /// Runs the current step with what was typed (password, NAS password or code).
    func submit(_ secret: String) async {
        guard !busy, let drive else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            switch step {
            case .key:
                if let keyFile {
                    _ = try await drive.runCLI(["unlock", "--key-file", keyFile.path])
                } else {
                    guard !secret.isEmpty else { throw CLIError.failed("Enter the encryption password or choose its key file.") }
                    guard secret.utf8.count <= 64 * 1024 else { throw CLIError.failed("The encryption password is too long.") }
                    _ = try await drive.runCLI(["unlock"], input: Data(secret.utf8))
                }
                done = true
                await drive.personalFolderUnlocked(share)
            case .nasPassword:
                guard !secret.isEmpty else { throw CLIError.failed("Enter your NAS account password.") }
                guard secret.utf8.count <= 256 else { throw CLIError.failed("That NAS password is too long.") }
                try await nasStep(["password": secret])
            case .code(let pending):
                guard !secret.isEmpty else { throw CLIError.failed("Enter your authenticator code.") }
                guard secret.count <= 12 else { throw CLIError.failed("Authenticator codes are at most 12 characters.") }
                try await nasStep(["pending": pending, "code": secret.trimmingCharacters(in: .whitespaces)])
            }
        } catch CLIError.needsNASSignIn(let message) {
            // UGOS wants the owner sign-in, or the code step expired: (re)start it.
            step = .nasPassword
            error = message
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func nasStep(_ body: [String: String]) async throws {
        guard let drive else { return }
        let input = try JSONSerialization.data(withJSONObject: body)
        let out = try await drive.runCLI(["unlock", "--nas-sign-in"], input: input)
        let result = (try? JSONSerialization.jsonObject(with: out)) as? [String: Any] ?? [:]
        if result["need_otp"] as? Bool == true, let pending = result["pending"] as? String {
            step = .code(pending: pending)
        } else {
            step = .key
        }
    }

    #if DEBUG
    func preview(step: Step, error: String?) {
        self.step = step
        self.error = error
    }
    #endif

    func chooseKeyFile() {
        let panel = NSOpenPanel()
        panel.title = "Choose your UGOS encryption key file"
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            if size > 64 * 1024 {
                error = "Key files must be under 64 KB."
            } else {
                keyFile = url
                error = nil
            }
        }
    }
}

extension DriveController {
    /// Runs the bundled CLI against this Drive (`--json --server …`).
    func runCLI(_ args: [String], input: Data? = nil) async throws -> Data {
        try await CLIRun(["--json", "--server", serverURL] + args).output(input: input)
    }
}

/// The Unlock window.
struct UnlockView: View {
    @ObservedObject var drive: DriveController
    @Environment(\.closeWindow) private var closeWindow

    var body: some View {
        Group {
            if let unlock = drive.unlocking {
                UnlockForm(unlock: unlock) { closeWindow() }
            } else {
                Text("Choose a locked folder in the Drive menu.")
                    .font(.sans(12.5))
                    .foregroundStyle(Brand.muted)
                    .padding(28)
            }
        }
        .frame(width: 420)
        .background(Brand.background)
        .foregroundStyle(Brand.foreground)
    }
}

struct UnlockForm: View {
    @ObservedObject var unlock: PersonalUnlock
    let close: () -> Void
    @State private var secret = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                ZStack {
                    Rectangle().fill(Brand.accentTint)
                    Image(systemName: unlock.done ? "lock.open.fill" : "lock.fill")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(Brand.accent)
                }
                .frame(width: 40, height: 40)
                VStack(alignment: .leading, spacing: 3) {
                    Text(unlock.done ? "\(unlock.owner) is unlocked" : "Unlock \(unlock.owner)")
                        .font(.sans(17, .semibold)).tracking(-0.3)
                    Text(subtitle).font(.sans(12)).foregroundStyle(Brand.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if unlock.done {
                Button("Done", action: close).buttonStyle(PrimaryButtonStyle())
            } else {
                fields
                if let error = unlock.error { Banner(text: error) }
                HStack(spacing: 8) {
                    Button("Cancel", action: close)
                        .buttonStyle(SecondaryButtonStyle())
                        .frame(width: 110)
                        .keyboardShortcut(.cancelAction)
                    Button(action: submit) {
                        if unlock.busy { ProgressView().controlSize(.small).tint(Brand.onBrand) } else { Text(actionTitle) }
                    }
                    .buttonStyle(PrimaryButtonStyle())
                    .keyboardShortcut(.defaultAction)
                    .disabled(unlock.busy)
                }
            }
        }
        .padding(24)
        .onAppear { focused = true }
        .onChange(of: unlock.step) { _ in
            secret = ""
            focused = true
        }
    }

    private var subtitle: String {
        if unlock.done {
            return "Open it from the menu or in Finder. It relocks everywhere after 24 hours."
        }
        switch unlock.step {
        case .key:
            return "Use your UGOS encryption password or key file. The folder relocks everywhere, including Finder, after 24 hours."
        case .nasPassword:
            return "Your folder stays private. Sign in to the NAS as \(unlock.owner) to authorize unlocking and daily relocking. Your NAS password is not saved."
        case .code:
            return "Enter the code from your authenticator app for \(unlock.owner)."
        }
    }

    private var actionTitle: String {
        switch unlock.step {
        case .key: return "Unlock"
        case .nasPassword: return "Sign in"
        case .code: return "Verify"
        }
    }

    @ViewBuilder private var fields: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch unlock.step {
            case .key:
                SectionLabel(text: "Encryption password")
                if let file = unlock.keyFile {
                    HStack {
                        Image(systemName: "key.fill").foregroundStyle(Brand.accent)
                        Text(file.lastPathComponent).font(.mono(12)).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("Remove") { unlock.keyFile = nil }.buttonStyle(LinkButtonStyle())
                    }
                    .padding(.horizontal, 10)
                    .frame(height: 34)
                    .background(Brand.card, in: Rectangle())
                    .overlay(Rectangle().strokeBorder(Brand.border))
                } else {
                    secretField("Encryption password")
                }
                Button { unlock.chooseKeyFile() } label: {
                    Label("Use a key file instead…", systemImage: "doc.badge.key")
                }
                .buttonStyle(LinkButtonStyle(tint: Brand.accent))
            case .nasPassword:
                SectionLabel(text: "NAS password for \(unlock.owner)")
                secretField("NAS account password")
            case .code:
                SectionLabel(text: "Authenticator code")
                TextField("123456", text: $secret)
                    .textFieldStyle(.plain)
                    .font(.mono(15, .medium))
                    .modifier(FieldChrome())
                    .focused($focused)
                    .onSubmit(submit)
            }
        }
    }

    private func secretField(_ placeholder: String) -> some View {
        SecureField(placeholder, text: $secret)
            .textFieldStyle(.plain)
            .font(.sans(13))
            .modifier(FieldChrome())
            .focused($focused)
            .onSubmit(submit)
    }

    private func submit() {
        let value = secret
        secret = "" // never keep a secret in the field after sending it
        Task { await unlock.submit(value) }
    }
}

private struct FieldChrome: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 10)
            .frame(height: 34)
            .background(Brand.card, in: Rectangle())
            .overlay(Rectangle().strokeBorder(Brand.border))
    }
}
