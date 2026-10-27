import Foundation
import Security
import SwiftUI

/// A speed test that matches using the drive in Finder: it reads and writes
/// files on the mounted volume itself (macOS WebDAV client → helper → tunnel →
/// Drive), in the helper's hidden speed-test folder, whose files are the
/// Drive's synthetic stream, so nothing is written to a share.
@MainActor
final class SpeedTest: ObservableObject {
    enum Phase: Equatable { case idle, running(String, Double), failed(String) }

    struct Result: Equatable {
        var download: Double // MB/s, reading a 256 MB file
        var upload: Double   // MB/s, writing a 128 MB file (until Finder would show it done)
        var smallFiles: Double // files/s, 40 × 256 KB files
        var at: Date
    }

    nonisolated static let downloadMB = 256
    nonisolated static let uploadMB = 128
    nonisolated static let smallFiles = 40
    nonisolated static let smallFileKB = 256

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var result: Result?

    var running: Bool {
        if case .running = phase { return true }
        return false
    }

    func run(volume: URL) {
        guard !running else { return }
        let dir = volume.appendingPathComponent(".Ugreen Drive Speed Test", isDirectory: true)
        phase = .running("Reading a \(Self.downloadMB) MB file…", 0)
        Task.detached(priority: .userInitiated) {
            do {
                let run = UUID().uuidString.prefix(8)
                let download = try Self.read(dir.appendingPathComponent("download-\(Self.downloadMB)-\(run).bin")) { p in
                    Task { @MainActor in self.phase = .running("Reading a \(Self.downloadMB) MB file…", p / 3) }
                }
                await self.step("Writing a \(Self.uploadMB) MB file…", 1.0 / 3)
                let upload = try Self.write(dir.appendingPathComponent("upload-\(run).bin"), bytes: Self.uploadMB << 20)
                await self.step("Copying \(Self.smallFiles) small files…", 2.0 / 3)
                let small = try Self.writeSmallFiles(dir, run: String(run))
                let result = Result(download: download, upload: upload, smallFiles: small, at: Date())
                await MainActor.run {
                    self.result = result
                    self.phase = .idle
                }
            } catch {
                await MainActor.run { self.phase = .failed(error.localizedDescription) }
            }
        }
    }

    private func step(_ text: String, _ progress: Double) {
        phase = .running(text, progress)
    }

    // MARK: File work (plain POSIX calls, like any app copying in Finder)

    struct TestError: LocalizedError {
        let errorDescription: String?
    }

    nonisolated private static func fail(_ what: String) -> TestError {
        TestError(errorDescription: "\(what) failed: \(String(cString: strerror(errno))). Is the drive connected?")
    }

    /// Reads a whole file; MB/s from open to the last byte.
    nonisolated static func read(_ url: URL, progress: @escaping (Double) -> Void) throws -> Double {
        let start = Date()
        let fd = open(url.path, O_RDONLY)
        guard fd >= 0 else { throw fail("Opening the test file") }
        defer { close(fd) }
        let total = Double(downloadMB << 20)
        var buffer = [UInt8](repeating: 0, count: 4 << 20)
        var got = 0
        while true {
            let n = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
            if n < 0 { throw fail("Reading") }
            if n == 0 { break }
            got += n
            progress(Double(got) / total)
        }
        return Double(got) / 1_000_000 / Date().timeIntervalSince(start)
    }

    /// Writes a file of random bytes; MB/s until close returns, which is when
    /// the upload has finished (the same moment a Finder copy completes).
    nonisolated static func write(_ url: URL, bytes: Int) throws -> Double {
        let chunk = randomBytes(min(bytes, 4 << 20))
        let start = Date()
        let fd = open(url.path, O_CREAT | O_WRONLY | O_TRUNC, 0o644)
        guard fd >= 0 else { throw fail("Creating the test file") }
        var written = 0
        while written < bytes {
            let n = chunk.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, min($0.count, bytes - written)) }
            if n <= 0 { close(fd); throw fail("Writing") }
            written += n
        }
        guard close(fd) == 0 else { throw fail("Saving") }
        let seconds = Date().timeIntervalSince(start)
        unlink(url.path)
        return Double(bytes) / 1_000_000 / seconds
    }

    /// Copies many small files one after another; files per second.
    nonisolated static func writeSmallFiles(_ dir: URL, run: String) throws -> Double {
        let start = Date()
        var paths: [String] = []
        for i in 0..<smallFiles {
            let url = dir.appendingPathComponent("small-\(run)-\(i).jpg")
            let data = randomBytes(smallFileKB << 10)
            let fd = open(url.path, O_CREAT | O_WRONLY | O_TRUNC, 0o644)
            guard fd >= 0 else { throw fail("Creating a small file") }
            let n = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
            guard n == data.count, close(fd) == 0 else { throw fail("Saving a small file") }
            paths.append(url.path)
        }
        let seconds = Date().timeIntervalSince(start)
        paths.forEach { unlink($0) }
        return Double(smallFiles) / seconds
    }

    nonisolated private static func randomBytes(_ count: Int) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: count)
        _ = SecRandomCopyBytes(kSecRandomDefault, count, &bytes)
        return bytes
    }
}

/// The speed test card in the main window.
struct SpeedTestSection: View {
    @ObservedObject var drive: DriveController
    @ObservedObject var test: SpeedTest

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                SectionLabel(text: "Speed test")
                Spacer()
                Button(test.result == nil ? "Run test" : "Run again") {
                    if let volume = drive.connectedVolume { test.run(volume: volume) }
                }
                .buttonStyle(LinkButtonStyle(tint: Brand.accent))
                .disabled(test.running || drive.connectedVolume == nil)
            }
            VStack(alignment: .leading, spacing: 10) {
                switch test.phase {
                case .running(let step, let progress):
                    Text(step).font(.sans(12))
                    ProgressView(value: progress).tint(Brand.fill)
                case .failed(let reason):
                    Text(reason).font(.sans(12)).foregroundStyle(Brand.danger)
                        .fixedSize(horizontal: false, vertical: true)
                case .idle:
                    EmptyView()
                }
                if let r = test.result {
                    HStack(spacing: 8) {
                        SpeedTile(title: "Download", value: String(format: "%.0f", r.download), unit: "MB/s")
                        SpeedTile(title: "Upload", value: String(format: "%.0f", r.upload), unit: "MB/s")
                        SpeedTile(title: "Small files", value: String(format: r.smallFiles < 10 ? "%.1f" : "%.0f", r.smallFiles), unit: "files/s")
                    }
                }
                Text(footnote)
                    .font(.sans(10.5))
                    .foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Brand.card)
            .overlay(Rectangle().strokeBorder(Brand.border))
        }
    }

    private var footnote: String {
        if drive.connectedVolume == nil { return "Connect to run the test." }
        var text = "Measured through \(drive.connectedVolume!.path), the same path a Finder copy takes: reads a \(SpeedTest.downloadMB) MB file, writes a \(SpeedTest.uploadMB) MB file and \(SpeedTest.smallFiles) small files (about 400 MB). Nothing is saved to your shares."
        if let at = test.result?.at { text += " Last run \(Formatting.time(at))." }
        return text
    }
}

private struct SpeedTile: View {
    let title: String
    let value: String
    let unit: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title.uppercased()).font(.sans(9.5, .medium)).tracking(0.9).foregroundStyle(Brand.muted)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(value).font(.mono(18, .medium)).lineLimit(1).fixedSize()
                Text(unit).font(.mono(10.5)).foregroundStyle(Brand.muted).lineLimit(1).minimumScaleFactor(0.7)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Brand.secondary, in: Rectangle())
    }
}
