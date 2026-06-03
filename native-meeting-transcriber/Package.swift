// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "NativeMeetingTranscriber",
    platforms: [
        .macOS(.v15)
    ],
    products: [
        .executable(name: "native-meeting-recorder", targets: ["NativeMeetingRecorder"]),
        .executable(name: "meeting-transcriber-dashboard", targets: ["MeetingTranscriberDashboard"])
    ],
    targets: [
        .executableTarget(
            name: "NativeMeetingRecorder",
            path: "Sources/NativeMeetingRecorder",
            linkerSettings: [
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("ScreenCaptureKit")
            ]
        ),
        .executableTarget(
            name: "MeetingTranscriberDashboard",
            path: "Sources/MeetingTranscriberDashboard",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("SwiftUI")
            ]
        )
    ]
)
