import XCTest
@testable import UgreenDrive

final class AppConfigTests: XCTestCase {
    func testReadsTheDriveAddress() {
        let config = AppConfig(info: ["UgreenDriveURL": " https://drive.example.com/ "])
        XCTAssertEqual(config.driveURL?.absoluteString, "https://drive.example.com")
        XCTAssertNil(AppConfig(info: [:]).driveURL)
        XCTAssertNil(AppConfig(info: ["UgreenDriveURL": "__DRIVE_URL__"]).driveURL)
    }

    func testEmptyPlaceholderAndUnsafeValuesAreNil() {
        for raw in ["", "  ", "__DRIVE_URL__", "http://drive.example.com", "http://8.8.8.8:8790",
                    "http://172.32.0.1", "ftp://drive.example.com", "drive"] {
            XCTAssertNil(AppConfig.origin(raw), raw)
        }
    }

    func testStripsPathAndAllowsHomeNetworkHTTP() {
        XCTAssertEqual(AppConfig.origin("https://drive.example.com/folder?x=1#y")?.absoluteString,
                       "https://drive.example.com")
        for raw in ["http://localhost:8790", "http://192.168.1.20:8790", "http://10.0.0.5:8790", "http://172.16.4.2",
                    "http://127.0.0.1:8790", "http://169.254.10.1", "http://nas.local:8790", "http://[fd00::1]:8790",
                    "http://[fe80::1]:8790", "http://[::1]:8790"] {
            XCTAssertEqual(AppConfig.origin(raw)?.absoluteString, raw, raw)
        }
    }

    func testLocalHostRuleMatchesTheCLI() {
        XCTAssertTrue(AppConfig.isLocalHost("NAS.local"))
        XCTAssertTrue(AppConfig.isLocalHost("::ffff:192.168.0.2"))
        XCTAssertFalse(AppConfig.isLocalHost("192.169.0.1"))
        XCTAssertFalse(AppConfig.isLocalHost("2001:db8::1"))
        XCTAssertFalse(AppConfig.isLocalHost("local"))
        XCTAssertFalse(AppConfig.isLocalHost("evil.local.example.com"))
    }
}

final class DriveAddressTests: XCTestCase {
    func testTypedAddresses() {
        func parse(_ raw: String) -> String? { DriveController.parseServer(raw)?.absoluteString }
        XCTAssertEqual(parse("drive.example.com"), "https://drive.example.com")
        XCTAssertEqual(parse(" https://drive.example.com/ "), "https://drive.example.com")
        XCTAssertEqual(parse("192.168.1.20"), "http://192.168.1.20:8790")
        XCTAssertEqual(parse("192.168.1.20:9000"), "http://192.168.1.20:9000")
        XCTAssertEqual(parse("http://nas.local"), "http://nas.local:8790")
        XCTAssertEqual(parse("localhost:8787"), "http://localhost:8787")
        XCTAssertNil(parse("http://drive.example.com"))
        XCTAssertNil(parse("https://drive.example.com/sub"))
        XCTAssertNil(parse("https://drive.example.com/?x=1"))
        XCTAssertNil(parse(""))
    }

    func testVolumeNames() {
        XCTAssertEqual(DriveController.cleanName("Home Drive"), "Home Drive")
        XCTAssertEqual(DriveController.cleanName("a/b:c"), "a-b-c")
        XCTAssertEqual(DriveController.cleanName("  "), "Ugreen Drive")
        XCTAssertEqual(DriveController.cleanName(nil), "Ugreen Drive")
        XCTAssertEqual(DriveController.cleanName(String(repeating: "x", count: 100)).count, 60)
    }
}

final class MoveToApplicationsTests: XCTestCase {
    let home = URL(fileURLWithPath: "/Users/someone")

    func testInstalledOnlyInsideAnApplicationsFolder() {
        XCTAssertTrue(MoveToApplications.isInstalled(URL(fileURLWithPath: "/Applications/Ugreen Drive.app"), home: home))
        XCTAssertTrue(MoveToApplications.isInstalled(URL(fileURLWithPath: "/Applications/Utilities/Ugreen Drive.app"), home: home))
        XCTAssertTrue(MoveToApplications.isInstalled(URL(fileURLWithPath: "/Users/someone/Applications/Ugreen Drive.app"), home: home))
        XCTAssertFalse(MoveToApplications.isInstalled(URL(fileURLWithPath: "/Users/someone/Downloads/Ugreen Drive.app"), home: home))
        XCTAssertFalse(MoveToApplications.isInstalled(
            URL(fileURLWithPath: "/private/var/folders/ab/T/AppTranslocation/1234/d/Ugreen Drive.app"), home: home))
        XCTAssertFalse(MoveToApplications.isInstalled(URL(fileURLWithPath: "/ApplicationsBackup/Ugreen Drive.app"), home: home))
    }

    func testOffersOnlyForAForegroundAppBundleOutsideApplications() {
        let download = URL(fileURLWithPath: "/Users/someone/Downloads/Ugreen Drive.app")
        XCTAssertTrue(MoveToApplications.shouldOffer(bundle: download, home: home, background: false, declined: false))
        XCTAssertFalse(MoveToApplications.shouldOffer(bundle: download, home: home, background: true, declined: false))
        XCTAssertFalse(MoveToApplications.shouldOffer(bundle: download, home: home, background: false, declined: true))
        XCTAssertFalse(MoveToApplications.shouldOffer(bundle: URL(fileURLWithPath: "/Applications/Ugreen Drive.app"),
                                                      home: home, background: false, declined: false))
        XCTAssertFalse(MoveToApplications.shouldOffer(bundle: URL(fileURLWithPath: "/tmp/.build/debug"),
                                                      home: home, background: false, declined: false))
    }

    func testNonAdminsGetTheirOwnApplicationsFolder() {
        XCTAssertEqual(MoveToApplications.destinationFolder(home: home, systemWritable: true).path, "/Applications")
        XCTAssertEqual(MoveToApplications.destinationFolder(home: home, systemWritable: false).path, "/Users/someone/Applications")
    }
}

final class QuitPolicyTests: XCTestCase {
    func testQuitKeepsDriveUnlessAFullQuitIsNeeded() {
        XCTAssertEqual(QuitPolicy.decide(keepDrive: true, fullQuitRequested: false, externalQuit: false), .background)
        XCTAssertEqual(QuitPolicy.decide(keepDrive: false, fullQuitRequested: false, externalQuit: false), .terminate)
        XCTAssertEqual(QuitPolicy.decide(keepDrive: true, fullQuitRequested: true, externalQuit: false), .terminate)
        XCTAssertEqual(QuitPolicy.decide(keepDrive: true, fullQuitRequested: false, externalQuit: true), .terminate)
    }

    func testOnByDefault() {
        let defaults = UserDefaults.standard
        let saved = defaults.object(forKey: QuitPolicy.keepDriveKey)
        defaults.removeObject(forKey: QuitPolicy.keepDriveKey)
        XCTAssertTrue(QuitPolicy.keepDrive)
        defaults.set(false, forKey: QuitPolicy.keepDriveKey)
        XCTAssertFalse(QuitPolicy.keepDrive)
        if let saved { defaults.set(saved, forKey: QuitPolicy.keepDriveKey) } else { defaults.removeObject(forKey: QuitPolicy.keepDriveKey) }
    }

    func testCommandQHasNoAppleEventSoItIsNotExternal() {
        XCTAssertFalse(QuitPolicy.isExternalQuit)
    }
}

final class UpdaterTests: XCTestCase {
    func testInstallWaitsForCopiesAndUnlocks() {
        XCTAssertTrue(Updater.mayInstall(transferring: false, unlocking: false))
        XCTAssertFalse(Updater.mayInstall(transferring: true, unlocking: false))
        XCTAssertFalse(Updater.mayInstall(transferring: false, unlocking: true))
    }

    func testReleaseTags() {
        XCTAssertEqual(Updater.version(fromTag: "v1.2.3"), "1.2.3")
        XCTAssertNil(Updater.version(fromTag: "cli-v1.2.3"))
        XCTAssertNil(Updater.version(fromTag: "v1.2"))
        XCTAssertNil(Updater.version(fromTag: "latest"))
        XCTAssertTrue(Updater.isNewer("0.2.0", than: "0.1.9"))
        XCTAssertFalse(Updater.isNewer("0.1.0", than: "0.1.0"))
        XCTAssertFalse(Updater.isNewer("1.0.0", than: "0.0.0-dev"))
    }
}
