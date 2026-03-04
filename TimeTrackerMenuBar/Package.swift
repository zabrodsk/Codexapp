// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TimeTrackerMenuBar",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "TimeTrackerMenuBar", targets: ["TimeTrackerMenuBar"])
    ],
    targets: [
        .executableTarget(
            name: "TimeTrackerMenuBar",
            path: "Sources/TimeTrackerMenuBar"
        )
    ]
)
