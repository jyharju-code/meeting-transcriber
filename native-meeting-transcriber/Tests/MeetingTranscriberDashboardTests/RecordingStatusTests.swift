import Foundation
import XCTest
@testable import MeetingTranscriberDashboard

final class RecordingStatusTests: XCTestCase {
    private func status(recording: Bool, updatedAt: Date) -> RecorderStatus {
        RecorderStatus(
            recording: recording,
            level: 0,
            systemLevel: 0,
            microphoneLevel: 0,
            outputPath: "/tmp/recording.mp4",
            updatedAt: ISO8601DateFormatter().string(from: updatedAt)
        )
    }

    func testManualRecorderWinsOverMissingStatus() {
        XCTAssertTrue(resolvedRecordingFlag(
            hasManualRecorder: true,
            hasAutoRecorder: false,
            status: nil
        ))
    }

    func testAutoRecorderWinsOverStaleIdleStatus() {
        let now = Date()
        XCTAssertTrue(resolvedRecordingFlag(
            hasManualRecorder: false,
            hasAutoRecorder: true,
            status: status(recording: false, updatedAt: now.addingTimeInterval(-60)),
            now: now
        ))
    }

    func testStaleStatusIsIdleWithoutLocalRecorder() {
        let now = Date()
        XCTAssertFalse(resolvedRecordingFlag(
            hasManualRecorder: false,
            hasAutoRecorder: false,
            status: status(recording: true, updatedAt: now.addingTimeInterval(-11)),
            now: now
        ))
    }

    func testFreshRecordingStatusIsUsedWithoutLocalRecorder() {
        let now = Date()
        XCTAssertTrue(resolvedRecordingFlag(
            hasManualRecorder: false,
            hasAutoRecorder: false,
            status: status(recording: true, updatedAt: now.addingTimeInterval(-2)),
            now: now
        ))
    }

    func testMalformedTimestampIsStale() {
        var malformed = status(recording: true, updatedAt: Date())
        malformed.updatedAt = "not-a-date"
        XCTAssertFalse(resolvedRecordingFlag(
            hasManualRecorder: false,
            hasAutoRecorder: false,
            status: malformed
        ))
    }
}
