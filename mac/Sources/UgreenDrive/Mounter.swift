import Foundation
import NetFS

/// Mounts the helper's WebDAV URL with Apple's own client (NetFS), so the
/// volume shows up in /Volumes and under Finder › Locations.
enum Mounter {
    struct MountError: LocalizedError {
        let status: Int32
        var errorDescription: String? {
            let reason = String(cString: strerror(status))
            return "macOS couldn't mount the drive (\(reason), code \(status))."
        }
    }

    static func mount(_ url: URL, user: String, password: String) async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            var requestID: AsyncRequestID?
            let openOptions: NSMutableDictionary = [kNAUIOptionKey: kNAUIOptionNoUI]
            // Soft mount: if the helper is gone, file calls fail instead of hanging Finder.
            let mountOptions: NSMutableDictionary = [kNetFSSoftMountKey: true]
            let started = NetFSMountURLAsync(
                url as CFURL, nil, user as CFString, password as CFString,
                openOptions, mountOptions, &requestID, .main
            ) { status, _, points in
                if status == 0, let path = (points as? [String])?.first {
                    continuation.resume(returning: URL(fileURLWithPath: path))
                } else {
                    continuation.resume(throwing: MountError(status: status == 0 ? EIO : status))
                }
            }
            if started != 0 {
                continuation.resume(throwing: MountError(status: started))
            }
        }
    }

    /// Mounted WebDAV volumes with the server URL each was mounted from.
    static func webdavVolumes() -> [(volume: URL, source: URL)] {
        let volumes = FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: nil) ?? []
        return volumes.compactMap { volume in
            var info = statfs()
            guard statfs(volume.path, &info) == 0 else { return nil }
            let type = withUnsafeBytes(of: info.f_fstypename) { String(cString: $0.bindMemory(to: CChar.self).baseAddress!) }
            let from = withUnsafeBytes(of: info.f_mntfromname) { String(cString: $0.bindMemory(to: CChar.self).baseAddress!) }
            guard type == "webdav", let source = URL(string: from) else { return nil }
            return (volume, source)
        }
    }

    /// Our volumes: mounted from a loopback helper at the given path.
    static func helperVolumes(path: String) -> [(volume: URL, source: URL)] {
        webdavVolumes().filter { $0.source.host == "127.0.0.1" && $0.source.path == path }
    }

    static func unmount(_ volume: URL, force: Bool) throws {
        if Darwin.unmount(volume.path, force ? MNT_FORCE : 0) != 0 {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }
}
