import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit

private struct Options {
    var outputURL: URL
    var statusURL: URL?
    var maxSeconds: TimeInterval = 10_800
    var sampleRate: Int = 16_000
    var channelCount: Int = 1
    var includeMicrophone = true
    var requestPermission = false
}

private final class StatusWriter {
    private let url: URL?
    private let outputPath: String
    private let lock = NSLock()
    private var systemLevel = 0.0
    private var microphoneLevel = 0.0

    init(url: URL?, outputPath: String) {
        self.url = url
        self.outputPath = outputPath
    }

    func markRecording() {
        write(recording: true)
    }

    func markStopped() {
        update(system: 0, microphone: 0)
        write(recording: false)
    }

    func update(system: Double? = nil, microphone: Double? = nil) {
        lock.lock()
        if let system {
            systemLevel = smooth(previous: systemLevel, next: system)
        }
        if let microphone {
            microphoneLevel = smooth(previous: microphoneLevel, next: microphone)
        }
        lock.unlock()
        write(recording: true)
    }

    private func smooth(previous: Double, next: Double) -> Double {
        max(0, min(1, previous * 0.7 + next * 0.3))
    }

    private func write(recording: Bool) {
        guard let url else {
            return
        }

        lock.lock()
        let systemLevel = self.systemLevel
        let microphoneLevel = self.microphoneLevel
        lock.unlock()

        let payload: [String: Any] = [
            "recording": recording,
            "systemLevel": systemLevel,
            "microphoneLevel": microphoneLevel,
            "level": max(systemLevel, microphoneLevel),
            "outputPath": outputPath,
            "updatedAt": ISO8601DateFormatter().string(from: Date())
        ]

        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: url, options: .atomic)
        } catch {
            log("status write failed: \(error.localizedDescription)")
        }
    }
}

private final class AudioMeterOutput: NSObject, SCStreamOutput {
    private let statusWriter: StatusWriter

    init(statusWriter: StatusWriter) {
        self.statusWriter = statusWriter
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard CMSampleBufferDataIsReady(sampleBuffer), let level = rmsLevel(sampleBuffer) else {
            return
        }

        if type == .microphone {
            statusWriter.update(microphone: level)
        } else if type == .audio {
            statusWriter.update(system: level)
        }
    }

    private func rmsLevel(_ sampleBuffer: CMSampleBuffer) -> Double? {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) else {
            return nil
        }

        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(mNumberChannels: 0, mDataByteSize: 0, mData: nil)
        )

        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )

        guard status == noErr else {
            return nil
        }

        let flags = streamDescription.pointee.mFormatFlags
        let isFloat = (flags & kAudioFormatFlagIsFloat) != 0
        let isSignedInteger = (flags & kAudioFormatFlagIsSignedInteger) != 0
        let bytesPerSample = Int(streamDescription.pointee.mBitsPerChannel / 8)
        guard let data = audioBufferList.mBuffers.mData, audioBufferList.mBuffers.mDataByteSize > 0 else {
            return nil
        }

        let byteCount = Int(audioBufferList.mBuffers.mDataByteSize)
        if isFloat && bytesPerSample == 4 {
            let samples = data.bindMemory(to: Float.self, capacity: byteCount / MemoryLayout<Float>.size)
            return normalizedRMS(samples: samples, count: byteCount / MemoryLayout<Float>.size)
        }

        if isSignedInteger && bytesPerSample == 2 {
            let samples = data.bindMemory(to: Int16.self, capacity: byteCount / MemoryLayout<Int16>.size)
            return normalizedRMS(samples: samples, count: byteCount / MemoryLayout<Int16>.size)
        }

        return nil
    }

    private func normalizedRMS(samples: UnsafePointer<Float>, count: Int) -> Double? {
        guard count > 0 else {
            return nil
        }
        var sum = 0.0
        for index in 0..<count {
            let value = Double(samples[index])
            sum += value * value
        }
        return min(1, sqrt(sum / Double(count)) * 4)
    }

    private func normalizedRMS(samples: UnsafePointer<Int16>, count: Int) -> Double? {
        guard count > 0 else {
            return nil
        }
        var sum = 0.0
        for index in 0..<count {
            let value = Double(samples[index]) / Double(Int16.max)
            sum += value * value
        }
        return min(1, sqrt(sum / Double(count)) * 4)
    }
}

private final class RecordingDelegate: NSObject, SCRecordingOutputDelegate, SCStreamDelegate, @unchecked Sendable {
    private let finishSemaphore: DispatchSemaphore
    private let statusWriter: StatusWriter
    private(set) var didStart = false
    private(set) var failedError: Error?
    private var isStopping = false

    init(finishSemaphore: DispatchSemaphore, statusWriter: StatusWriter) {
        self.finishSemaphore = finishSemaphore
        self.statusWriter = statusWriter
    }

    func markStopping() {
        isStopping = true
    }

    func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        didStart = true
        statusWriter.markRecording()
        log("recording started")
    }

    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        failedError = error
        statusWriter.markStopped()
        log("recording failed: \(error.localizedDescription)")
        finishSemaphore.signal()
    }

    func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        statusWriter.markStopped()
        log("recording finished")
        finishSemaphore.signal()
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        if !isStopping {
            failedError = error
        }
        log("stream stopped with error: \(error.localizedDescription)")
        finishSemaphore.signal()
    }
}

@main
struct NativeMeetingRecorder {
    static func main() async {
        do {
            let options = try parseOptions()
            try await record(options: options)
        } catch {
            fputs("native-meeting-recorder: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }

    private static func record(options: Options) async throws {
        try await requestPermissions(includeMicrophone: options.includeMicrophone, requestPermission: options.requestPermission)

        try FileManager.default.createDirectory(
            at: options.outputURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if FileManager.default.fileExists(atPath: options.outputURL.path) {
            try FileManager.default.removeItem(at: options.outputURL)
        }

        let content = try await SCShareableContent.current
        guard let display = content.displays.first else {
            throw RecorderError("no capturable display found")
        }

        let currentPID = ProcessInfo.processInfo.processIdentifier
        let currentApp = content.applications.first { $0.processID == currentPID }
        let excludedApps = currentApp.map { [$0] } ?? []
        let filter = SCContentFilter(
            display: display,
            excludingApplications: excludedApps,
            exceptingWindows: []
        )

        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3
        configuration.showsCursor = false
        configuration.capturesAudio = true
        configuration.sampleRate = options.sampleRate
        configuration.channelCount = options.channelCount
        configuration.excludesCurrentProcessAudio = true
        if #available(macOS 15.0, *) {
            configuration.captureMicrophone = options.includeMicrophone
        }

        let finishSemaphore = DispatchSemaphore(value: 0)
        let statusWriter = StatusWriter(url: options.statusURL, outputPath: options.outputURL.path)
        let delegate = RecordingDelegate(finishSemaphore: finishSemaphore, statusWriter: statusWriter)
        let meterOutput = AudioMeterOutput(statusWriter: statusWriter)
        let stream = SCStream(filter: filter, configuration: configuration, delegate: delegate)

        let recordingConfig = SCRecordingOutputConfiguration()
        recordingConfig.outputURL = options.outputURL
        recordingConfig.outputFileType = .mp4

        let recordingOutput = SCRecordingOutput(configuration: recordingConfig, delegate: delegate)
        try stream.addRecordingOutput(recordingOutput)
        try stream.addStreamOutput(meterOutput, type: .audio, sampleHandlerQueue: DispatchQueue(label: "recorder.system-meter"))
        if options.includeMicrophone {
            try stream.addStreamOutput(meterOutput, type: .microphone, sampleHandlerQueue: DispatchQueue(label: "recorder.microphone-meter"))
        }

        let stopSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        signal(SIGINT, SIG_IGN)
        stopSource.setEventHandler {
            log("stop requested")
            delegate.markStopping()
            stream.stopCapture { error in
                if let error {
                    log("stop failed: \(error.localizedDescription)")
                    finishSemaphore.signal()
                }
            }
        }
        stopSource.resume()

        let terminateSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        signal(SIGTERM, SIG_IGN)
        terminateSource.setEventHandler {
            log("terminate requested")
            delegate.markStopping()
            stream.stopCapture { error in
                if let error {
                    log("stop failed: \(error.localizedDescription)")
                    finishSemaphore.signal()
                }
            }
        }
        terminateSource.resume()

        try await stream.startCapture()
        log("capturing system audio\(options.includeMicrophone ? " and microphone" : "") to \(options.outputURL.path)")

        DispatchQueue.global().asyncAfter(deadline: .now() + options.maxSeconds) {
            log("max duration reached")
            delegate.markStopping()
            stream.stopCapture { error in
                if let error {
                    log("stop failed: \(error.localizedDescription)")
                    finishSemaphore.signal()
                }
            }
        }

        await withCheckedContinuation { continuation in
            DispatchQueue.global().async {
                finishSemaphore.wait()
                continuation.resume()
            }
        }
        stopSource.cancel()
        terminateSource.cancel()

        if let failedError = delegate.failedError {
            throw failedError
        }
    }

    private static func parseOptions() throws -> Options {
        var args = Array(CommandLine.arguments.dropFirst())
        guard !args.isEmpty else {
            throw RecorderError("usage: native-meeting-recorder --output /path/meeting.mp4 [--status-file /path/status.json] [--max-seconds 10800] [--no-mic]")
        }

        var output: URL?
        var statusURL: URL?
        var maxSeconds: TimeInterval = 10_800
        var includeMicrophone = true
        var requestPermission = false

        while !args.isEmpty {
            let arg = args.removeFirst()
            switch arg {
            case "--output", "-o":
                guard let value = args.first else { throw RecorderError("missing value for \(arg)") }
                args.removeFirst()
                output = URL(fileURLWithPath: NSString(string: value).expandingTildeInPath)
            case "--status-file":
                guard let value = args.first else { throw RecorderError("missing value for \(arg)") }
                args.removeFirst()
                statusURL = URL(fileURLWithPath: NSString(string: value).expandingTildeInPath)
            case "--max-seconds":
                guard let value = args.first, let parsed = TimeInterval(value) else {
                    throw RecorderError("missing numeric value for --max-seconds")
                }
                args.removeFirst()
                maxSeconds = parsed
            case "--no-mic":
                includeMicrophone = false
            case "--request-permission":
                requestPermission = true
            case "--help", "-h":
                throw RecorderError("usage: native-meeting-recorder --output /path/meeting.mp4 [--status-file /path/status.json] [--max-seconds 10800] [--no-mic]")
            default:
                if output == nil {
                    output = URL(fileURLWithPath: NSString(string: arg).expandingTildeInPath)
                } else {
                    throw RecorderError("unknown argument: \(arg)")
                }
            }
        }

        guard let output else {
            throw RecorderError("missing --output path")
        }

        return Options(outputURL: output, statusURL: statusURL, maxSeconds: maxSeconds, includeMicrophone: includeMicrophone, requestPermission: requestPermission)
    }

    private static func requestPermissions(includeMicrophone: Bool, requestPermission: Bool) async throws {
        if !CGPreflightScreenCaptureAccess() {
            if requestPermission {
                log("requesting Screen/System Audio Recording permission")
                guard CGRequestScreenCaptureAccess() else {
                    throw RecorderError("Screen/System Audio Recording permission was not granted")
                }
            } else {
                throw RecorderError("Screen/System Audio Recording permission was not granted")
            }
        }

        guard includeMicrophone else {
            return
        }

        let microphoneGranted = await withCheckedContinuation { continuation in
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                continuation.resume(returning: granted)
            }
        }

        guard microphoneGranted else {
            throw RecorderError("Microphone permission was not granted")
        }
    }
}

private struct RecorderError: LocalizedError {
    let message: String

    init(_ message: String) {
        self.message = message
    }

    var errorDescription: String? {
        message
    }
}

private func log(_ message: String) {
    let formatter = ISO8601DateFormatter()
    print("[\(formatter.string(from: Date()))] \(message)")
    fflush(stdout)
}
