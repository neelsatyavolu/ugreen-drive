// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "UgreenDrive",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "UgreenDrive",
            path: "Sources/UgreenDrive",
            linkerSettings: [.linkedFramework("NetFS")]
        ),
        .testTarget(
            name: "UgreenDriveTests",
            dependencies: ["UgreenDrive"],
            path: "Tests/UgreenDriveTests"
        ),
    ]
)
