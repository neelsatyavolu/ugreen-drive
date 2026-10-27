import SwiftUI

/// "Status": one row per moving part, so it's clear what works and what doesn't.
struct StatusSection: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                SectionLabel(text: "Status")
                Spacer()
                if let checked = drive.status.checkedAt {
                    Text("Checked \(Formatting.time(checked))").font(.mono(10)).foregroundStyle(Brand.muted)
                }
                Button { Task { await drive.refreshAccount() } } label: {
                    Image(systemName: "arrow.clockwise").font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(LinkButtonStyle())
                .help("Check again")
            }
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 8) {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in row }
                StartAtLoginTile(drive: drive)
            }
        }
    }

    private var rows: [StatusRow] {
        let status = drive.status
        return [
            accountRow,
            StatusRow(symbol: "globe", title: "Drive", detail: drive.serverHost,
                      value: status.driveReachable == false ? "Can't reach"
                          : status.latencyMs.map { "\($0) ms" } ?? "Checking…",
                      valueIsData: status.latencyMs != nil,
                      tone: status.driveReachable == false ? .problem : status.driveReachable == true ? .ok : .busy),
            StatusRow(symbol: "externaldrive", title: "Finder",
                      detail: drive.connectedVolume?.path ?? "Not in Finder",
                      value: drive.connectedVolume != nil ? "Mounted" : "Not mounted",
                      tone: drive.connectedVolume != nil ? .ok : .idle),
            StatusRow(symbol: "gearshape.2", title: "Helper",
                      detail: status.helperSince.map { "Up \(Formatting.duration(since: $0))" + restarts } ?? "Starts when you connect",
                      value: status.helperSince != nil ? "Running" : "Stopped",
                      tone: status.helperSince != nil ? .ok : .idle),
            StatusRow(symbol: status.online ? "wifi" : "wifi.slash", title: "Network",
                      detail: !status.online ? "Waiting for a connection"
                          : status.viaLAN ? "Home network, direct" : "Over the internet",
                      value: status.online ? status.networkKind : "Offline",
                      tone: status.online ? .ok : .problem),
        ]
    }

    private var restarts: String {
        let count = drive.status.helperRestarts
        return count == 0 ? "" : " · restarted \(count)×"
    }

    private var accountRow: StatusRow {
        switch drive.account {
        case .signedIn(let user):
            return StatusRow(symbol: "person.crop.circle", title: "Account",
                             detail: drive.status.email.isEmpty ? "NAS account" : drive.status.email,
                             value: user, valueIsData: true, tone: .ok)
        case .signedOut:
            return StatusRow(symbol: "person.crop.circle", title: "Account", detail: "Sign in with your NAS account",
                             value: "Signed out", tone: .problem)
        case .signingIn:
            return StatusRow(symbol: "person.crop.circle", title: "Account", detail: "Waiting for your browser",
                             value: "Signing in", tone: .busy)
        case .unknown:
            return StatusRow(symbol: "person.crop.circle", title: "Account", detail: "Checking your sign-in",
                             value: "Checking…", tone: .busy)
        }
    }
}

/// One moving part: label, value and a tone dot.
struct StatusRow: View {
    let symbol: String
    let title: String
    let detail: String
    let value: String
    var valueIsData = false
    let tone: Tone

    var body: some View {
        StatusTile(symbol: symbol, title: title, tone: tone) {
            Text(value)
                .font(valueIsData ? .mono(12.5, .medium) : .sans(13, .medium))
                .foregroundStyle(tone == .problem ? Brand.danger : Brand.foreground)
                .lineLimit(1)
            Text(detail)
                .font(.sans(10.5))
                .foregroundStyle(Brand.muted)
                .lineLimit(2)
                .truncationMode(.middle)
                .fixedSize(horizontal: false, vertical: true)
        }
        .help("\(title): \(value) — \(detail)")
    }
}

struct StatusTile<Content: View>: View {
    let symbol: String
    let title: String
    let tone: Tone
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                Image(systemName: symbol).font(.system(size: 9.5, weight: .semibold))
                Text(title.uppercased()).font(.sans(9.5, .medium)).tracking(0.9)
                Spacer(minLength: 4)
                Circle().fill(tone.color).frame(width: 6, height: 6)
            }
            .foregroundStyle(Brand.muted)
            .padding(.bottom, 2)
            content
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, minHeight: 64, alignment: .topLeading)
        .background(Brand.card)
        .overlay(Rectangle().strokeBorder(Brand.border))
    }
}

private struct StartAtLoginTile: View {
    @ObservedObject var drive: DriveController

    var body: some View {
        StatusTile(symbol: "power", title: "Login", tone: drive.startsAtLogin ? .ok : .idle) {
            HStack {
                Text(drive.startsAtLogin ? "Starts at login" : "Manual start")
                    .font(.sans(13, .medium))
                    .lineLimit(1)
                Spacer(minLength: 4)
                Toggle("", isOn: Binding(get: { drive.startsAtLogin }, set: { drive.setStartsAtLogin($0) }))
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .labelsHidden()
                    .tint(Brand.fill)
            }
            Text("Reconnects on restart").font(.sans(10.5)).foregroundStyle(Brand.muted).lineLimit(1)
        }
    }
}

/// Shares as they appear at the top of the volume; click to open one.
struct SharesSection: View {
    @ObservedObject var drive: DriveController
    var visibleRows = 4

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                SectionLabel(text: "Shares")
                Spacer()
                Text("\(drive.status.shares.count)").font(.mono(10)).foregroundStyle(Brand.muted)
            }
            ScrollView {
                VStack(spacing: 0) {
                    ForEach(Array(drive.status.shares.enumerated()), id: \.element.id) { index, share in
                        if index > 0 { Rectangle().fill(Brand.border).frame(height: 1) }
                        ShareRow(share: share) {
                            if share.locked {
                                Windows.shared.showUnlock(drive, share: share)
                            } else if drive.connectedVolume != nil {
                                drive.openShare(share)
                            } else {
                                drive.connect()
                            }
                        }
                    }
                }
            }
            // An explicit height: a ScrollView with only a max height collapses
            // to nothing in the menu-bar window. Rows are 32pt plus a 1pt divider.
            .frame(height: CGFloat(min(drive.status.shares.count, visibleRows)) * 33 - 1)
            .background(Brand.card)
            .overlay(Rectangle().strokeBorder(Brand.border))
        }
    }
}

private struct ShareRow: View {
    let share: DriveStatus.Share
    let open: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: open) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(share.locked ? Brand.muted : Brand.accent)
                    .frame(width: 18)
                Text(share.name).font(.sans(12.5)).lineLimit(1)
                Spacer()
                if share.locked {
                    Text("Unlock").font(.sans(11.5, .semibold)).foregroundStyle(Brand.accent)
                } else if share.encrypted, let relocks = share.relocksAt {
                    Text("until \(Formatting.time(relocks))").font(.mono(10.5)).foregroundStyle(Brand.muted)
                } else if !share.canWrite {
                    Tag(text: "Read only")
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Brand.muted)
            }
            .padding(.horizontal, 12)
            .frame(height: 32)
            .background(hovering ? Brand.secondary : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(help)
    }

    private var icon: String {
        if share.locked { return "lock.fill" }
        if share.encrypted { return "lock.open" }
        return share.isPersonal ? "person.crop.square" : "folder"
    }

    private var help: String {
        if share.locked { return "\(share.name) is encrypted and locked. Click to unlock it for 24 hours." }
        if share.encrypted, let relocks = share.relocksAt {
            return "Unlocked until \(Formatting.time(relocks)). Click to open in Finder."
        }
        return "Open \(share.name) in Finder"
    }
}

private struct Tag: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(.sans(9, .medium))
            .tracking(0.8)
            .foregroundStyle(Brand.muted)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(Brand.secondary, in: Rectangle())
    }
}

/// Uploads the helper is sending (and ones that just finished or failed).
struct TransfersSection: View {
    let transfers: [Transfer]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                SectionLabel(text: "Uploads")
                Spacer()
                let active = transfers.filter { $0.state == .active }.count
                if active > 0 {
                    Text("\(active) active").font(.mono(10)).foregroundStyle(Brand.muted)
                }
            }
            VStack(spacing: 0) {
                ForEach(Array(transfers.enumerated()), id: \.element.id) { index, transfer in
                    if index > 0 { Rectangle().fill(Brand.border).frame(height: 1) }
                    TransferRow(transfer: transfer)
                }
            }
            .background(Brand.card)
            .overlay(Rectangle().strokeBorder(Brand.border))
        }
    }
}

private struct TransferRow: View {
    let transfer: Transfer

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(tint)
                Text(transfer.name).font(.sans(12.5, .medium)).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 6)
                Text(stateText).font(.mono(11, .medium)).foregroundStyle(tint)
            }
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Rectangle().fill(Brand.secondary)
                    Rectangle().fill(barColor).frame(width: proxy.size.width * transfer.fraction)
                }
            }
            .frame(height: 3)
            Text(detail)
                .font(transfer.state == .failed ? .sans(11) : .mono(10.5))
                .foregroundStyle(transfer.state == .failed ? Brand.danger : Brand.muted)
                .lineLimit(2)
                .truncationMode(.middle)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    private var icon: String {
        switch transfer.state {
        case .active: return "arrow.up"
        case .done: return "checkmark"
        case .failed: return "xmark"
        }
    }

    private var tint: Color {
        switch transfer.state {
        case .active: return Brand.foreground
        case .done: return Brand.accent
        case .failed: return Brand.danger
        }
    }

    private var barColor: Color {
        switch transfer.state {
        case .active: return Brand.fill
        case .done: return Brand.accent
        case .failed: return Brand.danger
        }
    }

    private var stateText: String {
        switch transfer.state {
        case .active: return "\(Int(transfer.fraction * 100))%"
        case .done: return "Done"
        case .failed: return "Failed"
        }
    }

    private var detail: String {
        switch transfer.state {
        case .active:
            let progress = "\(Formatting.bytes(transfer.sent)) of \(Formatting.bytes(transfer.size))"
            return transfer.bytesPerSecond > 0 ? progress + " · " + Formatting.speed(transfer.bytesPerSecond) : progress
        case .done:
            return "\(Formatting.bytes(transfer.size)) · \(transfer.folder) · \(Formatting.time(transfer.updatedAt))"
        case .failed:
            return transfer.error.isEmpty ? "Not saved to the Drive." : "Not saved: \(transfer.error)"
        }
    }
}
