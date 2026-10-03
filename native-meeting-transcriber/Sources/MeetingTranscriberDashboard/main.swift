import AppKit
import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit
import SwiftUI

private let home = FileManager.default.homeDirectoryForCurrentUser
private let runtimeRoot = home.appendingPathComponent(".meeting-transcriber")
private let appRoot = runtimeRoot.appendingPathComponent("app")
private let outputRoot = runtimeRoot.appendingPathComponent("output")
private let statusURL = runtimeRoot.appendingPathComponent("status.json")
private let commandURL = runtimeRoot.appendingPathComponent("dashboard-command.json")
private let acknowledgementURL = runtimeRoot.appendingPathComponent("dashboard-ack.json")
private let suppressionURL = runtimeRoot.appendingPathComponent("auto-suppression.json")
private let configURL = appRoot.appendingPathComponent("config.json")
private let envURL = home.appendingPathComponent(".meeting-transcriber.env")
private let transcribePython = runtimeRoot.appendingPathComponent("venv/bin/python")
// Kytkimet ja punainen lamppu (docs/PAATOKSET.md D1-D4); sama muoto kuin meeting-transcriber/policy.py.
private let switchURL = runtimeRoot.appendingPathComponent("kytkimet.json")
private let lampURL = runtimeRoot.appendingPathComponent("lamppu.json")
let sijaintiGlobal = "maailmanlaajuinen"
let sijaintiEU = "eu"
let laatuPerus = "perus"
let laatuHuippu = "huippu"

/// Read switch positions; anything unknown falls back to the defaults (global, perustaso).
func readSwitches(from url: URL) -> (sijainti: String, laatu: String) {
    guard let data = try? Data(contentsOf: url),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return (sijaintiGlobal, laatuPerus)
    }
    let s = (object["sijainti"] as? String)?.lowercased() == sijaintiEU ? sijaintiEU : sijaintiGlobal
    let l = (object["laatu"] as? String)?.lowercased() == laatuHuippu ? laatuHuippu : laatuPerus
    return (s, l)
}

/// Lock the switch positions into the meeting folder (job.json). Stricter wins: EU and Huipputaso
/// stay once set, a later switch change can only tighten a meeting.
func lockJob(_ jobDir: URL, sijainti: String, laatu: String, moment: String) {
    let url = jobDir.appendingPathComponent("job.json")
    var job: [String: Any] = [:]
    if let data = try? Data(contentsOf: url),
       let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        job = object
    }
    let now = ISO8601DateFormatter().string(from: Date())
    let hadJob = !job.isEmpty
    let oldS = job["sijainti"] as? String
    let oldL = job["laatu"] as? String
    job["sijainti"] = (sijainti == sijaintiEU || (hadJob && oldS == sijaintiEU)) ? sijaintiEU : sijaintiGlobal
    job["laatu"] = (laatu == laatuHuippu || (hadJob && oldL == laatuHuippu)) ? laatuHuippu : laatuPerus
    var history = job["historia"] as? [[String: Any]] ?? []
    history.append(["hetki": moment, "aika": now, "sijainti": sijainti, "laatu": laatu])
    job["historia"] = history
    if job["luotu"] == nil { job["luotu"] = now }
    if let data = try? JSONSerialization.data(withJSONObject: job, options: [.prettyPrinted, .sortedKeys]) {
        try? data.write(to: url, options: .atomic)
    }
}
let statusStaleSeconds: TimeInterval = 10

struct RecorderStatus: Decodable {
    var recording: Bool
    var level: Double
    var systemLevel: Double
    var microphoneLevel: Double
    var outputPath: String
    var updatedAt: String
}

enum RecordingOrigin: Equatable {
    case manual
    case automatic(commandID: String)
    case external
}

enum RecordingState: Equatable {
    case idle
    case starting(RecordingOrigin)
    case recording(RecordingOrigin)
    case stopping(RecordingOrigin)

    var isRecording: Bool {
        switch self {
        case .idle:
            return false
        case .starting, .recording, .stopping:
            return true
        }
    }
}

/// ISO 8601 with or without fractional seconds (the watcher writes microseconds).
func parseISODate(_ value: String) -> Date? {
    let plain = ISO8601DateFormatter()
    if let date = plain.date(from: value) { return date }
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = fractional.date(from: value) { return date }
    // Microsecond precision (Python isoformat) is not always accepted: drop the fraction.
    let trimmed = value.replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)
    return plain.date(from: trimmed)
}

func statusIsFresh(_ status: RecorderStatus, now: Date = Date(), threshold: TimeInterval = statusStaleSeconds) -> Bool {
    guard let updatedAt = ISO8601DateFormatter().date(from: status.updatedAt) else { return false }
    return now.timeIntervalSince(updatedAt) <= threshold
}

func resolvedRecordingFlag(
    hasManualRecorder: Bool,
    hasAutoRecorder: Bool,
    status: RecorderStatus?,
    now: Date = Date(),
    staleAfter: TimeInterval = statusStaleSeconds
) -> Bool {
    if hasManualRecorder || hasAutoRecorder { return true }
    guard let status, statusIsFresh(status, now: now, threshold: staleAfter) else { return false }
    return status.recording
}

struct RecordingItem: Identifiable {
    let id = UUID()
    let folderURL: URL
    let url: URL
    let transcriptURL: URL?
    let summaryURL: URL?
    let modifiedAt: Date
    var state: String = ""  // "", "virhe", "yhteenveto puuttuu", "Huipputaso jonossa", ...
    var locationEU: Bool = false
    var outsideEU: Bool = false

    var name: String { folderURL.lastPathComponent }
    var transcriptName: String { transcriptURL?.lastPathComponent ?? "None" }
    var summaryName: String { summaryURL?.lastPathComponent ?? "None" }
}

final class DashboardStatusWriter {
    private let lock = NSLock()
    private var systemLevel = 0.0
    private var microphoneLevel = 0.0
    private var outputPath = ""

    func setOutput(_ path: String) {
        lock.lock()
        outputPath = path
        lock.unlock()
    }

    func update(system: Double? = nil, microphone: Double? = nil, recording: Bool = true) {
        lock.lock()
        defer { lock.unlock() }
        if let system { systemLevel = min(1, max(0, systemLevel * 0.7 + system * 0.3)) }
        if let microphone { microphoneLevel = min(1, max(0, microphoneLevel * 0.7 + microphone * 0.3)) }
        let payload: [String: Any] = [
            "recording": recording,
            "systemLevel": recording ? systemLevel : 0,
            "microphoneLevel": recording ? microphoneLevel : 0,
            "level": recording ? max(systemLevel, microphoneLevel) : 0,
            "outputPath": recording ? outputPath : "",
            "updatedAt": ISO8601DateFormatter().string(from: Date())
        ]
        do {
            try FileManager.default.createDirectory(at: statusURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: statusURL, options: .atomic)
        } catch {
            print("status write failed: \(error.localizedDescription)")
        }
    }
}

final class DashboardRecorder: NSObject, @unchecked Sendable, SCRecordingOutputDelegate, SCStreamDelegate, SCStreamOutput {
    private var stream: SCStream?
    private var recordingOutput: SCRecordingOutput?
    private let statusWriter = DashboardStatusWriter()
    private var onFinish: (@Sendable (Int32, String?) -> Void)?
    private var stopping = false
    private let finishLock = NSLock()
    private var finished = false
    private var heartbeat: DispatchSourceTimer?

    func start(outputURL: URL, onFinish: @escaping @Sendable (Int32, String?) -> Void) async throws {
        self.onFinish = onFinish
        stopping = false
        finished = false
        statusWriter.setOutput(outputURL.path)

        if !CGPreflightScreenCaptureAccess() {
            guard CGRequestScreenCaptureAccess() else {
                throw NSError(domain: "MeetingTranscriber", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "Allow Screen & System Audio Recording for Meeting Transcriber Dashboard in System Settings, then quit and reopen the app."
                ])
            }
        }

        let micGranted = await withCheckedContinuation { continuation in
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                continuation.resume(returning: granted)
            }
        }
        guard micGranted else {
            throw NSError(domain: "MeetingTranscriber", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Allow Microphone access for Meeting Transcriber Dashboard."
            ])
        }

        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }

        let content = try await SCShareableContent.current
        guard let display = content.displays.first else {
            throw NSError(domain: "MeetingTranscriber", code: 3, userInfo: [NSLocalizedDescriptionKey: "No capturable display found."])
        }

        let currentPID = ProcessInfo.processInfo.processIdentifier
        let currentApp = content.applications.first { $0.processID == currentPID }
        let filter = SCContentFilter(display: display, excludingApplications: currentApp.map { [$0] } ?? [], exceptingWindows: [])

        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3
        configuration.showsCursor = false
        configuration.capturesAudio = true
        configuration.sampleRate = 16_000
        configuration.channelCount = 1
        configuration.excludesCurrentProcessAudio = true
        if #available(macOS 15.0, *) {
            configuration.captureMicrophone = true
        }

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        let recordingConfig = SCRecordingOutputConfiguration()
        recordingConfig.outputURL = outputURL
        recordingConfig.outputFileType = .mp4
        let recordingOutput = SCRecordingOutput(configuration: recordingConfig, delegate: self)

        try stream.addRecordingOutput(recordingOutput)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "dashboard.system-meter"))
        try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: DispatchQueue(label: "dashboard.microphone-meter"))
        self.stream = stream
        self.recordingOutput = recordingOutput
        try await stream.startCapture()
        statusWriter.update(recording: true)
        startHeartbeat()
        if stopping {
            stop()
        }
    }

    func stop() {
        stopping = true
        stream?.stopCapture { [weak self] error in
            if let error {
                self?.finish(status: 1, error: error.localizedDescription)
            }
        }
    }

    func cancelFailedStart() {
        heartbeat?.cancel()
        heartbeat = nil
        statusWriter.update(recording: false)
        stream = nil
        recordingOutput = nil
        onFinish = nil
    }

    func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        statusWriter.update(recording: true)
    }

    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        finish(status: 1, error: error.localizedDescription)
    }

    func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        finish(status: 0, error: nil)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        if !stopping {
            finish(status: 1, error: error.localizedDescription)
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard CMSampleBufferDataIsReady(sampleBuffer), let level = rmsLevel(sampleBuffer) else { return }
        if type == .microphone {
            statusWriter.update(microphone: level)
        } else if type == .audio {
            statusWriter.update(system: level)
        }
    }

    private func finish(status: Int32, error: String?) {
        finishLock.lock()
        guard !finished else {
            finishLock.unlock()
            return
        }
        finished = true
        finishLock.unlock()
        heartbeat?.cancel()
        heartbeat = nil
        statusWriter.update(recording: false)
        stream = nil
        recordingOutput = nil
        let callback = onFinish
        onFinish = nil
        callback?(status, error)
    }

    private func startHeartbeat() {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "dashboard.status-heartbeat"))
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler { [weak self] in
            self?.statusWriter.update(recording: true)
        }
        heartbeat = timer
        timer.resume()
    }

    private func rmsLevel(_ sampleBuffer: CMSampleBuffer) -> Double? {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) else { return nil }
        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(mNumberChannels: 0, mDataByteSize: 0, mData: nil))
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
        guard status == noErr, let data = audioBufferList.mBuffers.mData else { return nil }
        let flags = streamDescription.pointee.mFormatFlags
        let byteCount = Int(audioBufferList.mBuffers.mDataByteSize)
        if (flags & kAudioFormatFlagIsFloat) != 0 {
            let samples = data.bindMemory(to: Float.self, capacity: byteCount / MemoryLayout<Float>.size)
            return normalizedRMS(samples: samples, count: byteCount / MemoryLayout<Float>.size)
        }
        if (flags & kAudioFormatFlagIsSignedInteger) != 0 {
            let samples = data.bindMemory(to: Int16.self, capacity: byteCount / MemoryLayout<Int16>.size)
            return normalizedRMS(samples: samples, count: byteCount / MemoryLayout<Int16>.size)
        }
        return nil
    }

    private func normalizedRMS(samples: UnsafePointer<Float>, count: Int) -> Double? {
        guard count > 0 else { return nil }
        var sum = 0.0
        for index in 0..<count {
            let value = Double(samples[index])
            sum += value * value
        }
        return min(1, sqrt(sum / Double(count)) * 4)
    }

    private func normalizedRMS(samples: UnsafePointer<Int16>, count: Int) -> Double? {
        guard count > 0 else { return nil }
        var sum = 0.0
        for index in 0..<count {
            let value = Double(samples[index]) / Double(Int16.max)
            sum += value * value
        }
        return min(1, sqrt(sum / Double(count)) * 4)
    }
}

@MainActor
final class DashboardModel: ObservableObject {
    @Published private(set) var recordingState: RecordingState = .idle
    @Published var level = 0.0
    @Published var systemLevel = 0.0
    @Published var microphoneLevel = 0.0
    @Published var currentOutput = ""
    @Published var transcriptFormat = "md"
    @Published private(set) var sijainti = sijaintiGlobal
    @Published private(set) var laatu = laatuPerus
    @Published private(set) var lampRed = false
    @Published private(set) var lampText = ""
    @Published var summaryEnabled = true
    @Published var transcriptionProgress = 0.0
    @Published var transcriptionMessage = ""
    @Published var files: [RecordingItem] = []
    @Published var message = "Ready"
    @Published private(set) var autoSuppressed = false
    @Published var hudVisible = UserDefaults.standard.object(forKey: "hudVisible") as? Bool ?? true

    private var manualRecorder: DashboardRecorder?
    private var autoRecorder: DashboardRecorder?
    private var activeAutoCommandID: String?
    private var activeAutoOutputURL: URL?
    private var lastAutoCommandKey = ""
    private var timer: Timer?

    var isRecording: Bool { recordingState.isRecording }

    var canStopRecording: Bool {
        guard manualRecorder != nil || autoRecorder != nil else { return false }
        switch recordingState {
        case .recording(.manual), .recording(.automatic):
            return true
        case .idle, .starting, .stopping, .recording(.external):
            return false
        }
    }

    var primaryActionTitle: String {
        switch recordingState {
        case .idle:
            return "Start"
        case .starting:
            return "Starting"
        case .recording:
            return "Stop"
        case .stopping:
            return "Stopping"
        }
    }

    var primaryActionDisabled: Bool {
        switch recordingState {
        case .starting, .stopping:
            return true
        case .recording(.external):
            return true
        case .idle, .recording:
            return false
        }
    }

    init() {
        loadConfig()
        // A command file left over from before this launch must never be replayed: on 3.10.2026 a
        // relaunch re-ran yesterday's "start" and recorded over that meeting's recording.
        lastAutoCommandKey = currentCommandKey() ?? ""
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.handleAutoCommand()
                self?.refresh()
            }
        }
    }

    func refresh() {
        autoSuppressed = FileManager.default.fileExists(atPath: suppressionURL.path)
        let sw = readSwitches(from: switchURL)
        if sw.sijainti != sijainti { sijainti = sw.sijainti }
        if sw.laatu != laatu { laatu = sw.laatu }
        readLamp()
        readStatus()
        readFiles()
    }

    var isEU: Bool { sijainti == sijaintiEU }
    var isHuippu: Bool { laatu == laatuHuippu }

    func setSijainti(_ value: String) { writeSwitches(sijainti: value, laatu: laatu) }
    func setLaatu(_ value: String) { writeSwitches(sijainti: sijainti, laatu: value) }

    private func writeSwitches(sijainti s: String, laatu l: String) {
        do {
            try writeJSON(["sijainti": s, "laatu": l], to: switchURL)
            sijainti = s
            laatu = l
            message = s == sijaintiEU ? "Käsittelysijainti: EU" : "Käsittelysijainti: maailmanlaajuinen"
        } catch {
            message = "Kytkintä ei voitu tallentaa: \(error.localizedDescription)"
        }
    }

    private func readLamp() {
        guard let data = try? Data(contentsOf: lampURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            lampRed = false
            lampText = ""
            return
        }
        let violations = object["rikkeet"] as? [[String: Any]] ?? []
        let login = object["eu_kirjautuminen"] as? [String: Any]
        let loginOK = (login?["ok"] as? Bool) ?? true
        if !violations.isEmpty {
            let names = violations.compactMap { $0["palaveri"] as? String }.joined(separator: ", ")
            lampRed = true
            lampText = "EU-asennossa käsiteltyä EU:n ulkopuolella: \(names)"
        } else if isEU && !loginOK {
            lampRed = true
            lampText = "EU-kirjautuminen ei toimi (gcloud auth application-default login)"
        } else {
            lampRed = false
            lampText = loginOK ? "" : "EU-kirjautuminen ei toimi"
        }
    }

    func acknowledgeLamp() {
        guard let data = try? Data(contentsOf: lampURL),
              var object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        object["rikkeet"] = []
        try? writeJSON(object, to: lampURL)
        readLamp()
    }

    func startManualRecording() {
        guard manualRecorder == nil && autoRecorder == nil else { return }
        do {
            try FileManager.default.createDirectory(at: outputRoot, withIntermediateDirectories: true)
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyyMMdd-HHmmss"
            let jobDir = outputRoot.appendingPathComponent("\(formatter.string(from: Date()))-manual")
            try FileManager.default.createDirectory(at: jobDir, withIntermediateDirectories: true)
            let outputURL = jobDir.appendingPathComponent("recording.mp4")
            let sw = readSwitches(from: switchURL)
            lockJob(jobDir, sijainti: sw.sijainti, laatu: sw.laatu, moment: "nauhoitus")

            let recorder = DashboardRecorder()
            manualRecorder = recorder
            recordingState = .starting(.manual)
            currentOutput = outputURL.path
            level = 0
            systemLevel = 0
            microphoneLevel = 0
            message = "Manual recording starting"
            Task { [weak self] in
                do {
                    try await recorder.start(outputURL: outputURL) { [weak self] status, error in
                        Task { @MainActor in
                            self?.manualRecorder = nil
                            self?.recordingState = .idle
                            self?.refresh()
                            if status == 0 {
                                self?.message = "Manual recording saved"
                                self?.transcribe(url: outputURL)
                            } else {
                                self?.message = error ?? "Manual recording failed"
                            }
                        }
                    }
                    await MainActor.run {
                        guard self?.recordingState == .starting(.manual) else { return }
                        self?.recordingState = .recording(.manual)
                        self?.message = "Manual recording started"
                    }
                } catch {
                    await MainActor.run {
                        recorder.cancelFailedStart()
                        self?.manualRecorder = nil
                        self?.recordingState = .idle
                        self?.message = error.localizedDescription
                    }
                }
            }
        } catch {
            message = "Could not start recording: \(error.localizedDescription)"
        }
    }

    func stopRecording(userInitiated: Bool = true) {
        if let manualRecorder {
            recordingState = .stopping(.manual)
            message = "Stopping manual recording"
            manualRecorder.stop()
            return
        }

        guard let autoRecorder, let commandID = activeAutoCommandID else { return }
        if userInitiated {
            guard writeSuppression(commandID: commandID) else {
                message = "Could not suppress automatic restart; recording was not stopped"
                return
            }
        }
        writeAcknowledgement(commandID: commandID, state: "stopping", outputURL: activeAutoOutputURL)
        recordingState = .stopping(.automatic(commandID: commandID))
        message = "Stopping automatic recording"
        autoRecorder.stop()
    }

    func resumeAutomaticRecording() {
        do {
            try FileManager.default.removeItem(at: suppressionURL)
        } catch CocoaError.fileNoSuchFile {
        } catch {
            message = "Could not resume automatic recording: \(error.localizedDescription)"
            return
        }
        autoSuppressed = false
        message = "Automatic recording resumed"
    }

    private func currentCommandKey() -> String? {
        guard let data = try? Data(contentsOf: commandURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let command = object["command"] as? String,
              let id = object["id"] as? String else { return nil }
        return "\(command):\(id):\(object["createdAt"] as? String ?? "")"
    }

    private func handleAutoCommand() {
        guard let data = try? Data(contentsOf: commandURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let command = object["command"] as? String,
              let id = object["id"] as? String else {
            return
        }
        let key = "\(command):\(id):\(object["createdAt"] as? String ?? "")"
        guard key != lastAutoCommandKey else { return }
        lastAutoCommandKey = key

        if command == "start" {
            guard autoRecorder == nil && manualRecorder == nil else {
                message = "Automatic meeting detected, but recording is already active"
                return
            }
            guard let outputPath = object["outputPath"] as? String, !outputPath.isEmpty else {
                message = "Automatic recording command was missing output path"
                return
            }
            if let created = object["createdAt"] as? String, let date = parseISODate(created),
               Date().timeIntervalSince(date) > 120 {
                message = "Ignored a stale automatic recording command (\(id))"
                return
            }
            let attrs = try? FileManager.default.attributesOfItem(atPath: outputPath)
            if let size = attrs?[.size] as? NSNumber, size.intValue > 0 {
                message = "Refused to record over an existing recording (\(id))"
                return
            }
            startAutoRecording(
                outputURL: URL(fileURLWithPath: outputPath),
                detail: object["detail"] as? String,
                commandID: id
            )
        } else if command == "stop" {
            guard activeAutoCommandID == id else { return }
            stopRecording(userInitiated: false)
        }
    }

    private func startAutoRecording(outputURL: URL, detail: String?, commandID: String) {
        let sw = readSwitches(from: switchURL)
        try? FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        lockJob(outputURL.deletingLastPathComponent(), sijainti: sw.sijainti, laatu: sw.laatu, moment: "nauhoitus")
        let recorder = DashboardRecorder()
        autoRecorder = recorder
        activeAutoCommandID = commandID
        activeAutoOutputURL = outputURL
        recordingState = .starting(.automatic(commandID: commandID))
        currentOutput = outputURL.path
        level = 0
        systemLevel = 0
        microphoneLevel = 0
        guard writeAcknowledgement(commandID: commandID, state: "starting", outputURL: outputURL) else {
            autoRecorder = nil
            activeAutoCommandID = nil
            activeAutoOutputURL = nil
            recordingState = .idle
            currentOutput = ""
            return
        }
        message = detail == nil ? "Automatic recording starting" : "Automatic recording starting: \(detail!)"
        Task { [weak self] in
            do {
                try await recorder.start(outputURL: outputURL) { [weak self] status, error in
                    Task { @MainActor in
                        self?.autoRecorder = nil
                        self?.activeAutoCommandID = nil
                        self?.activeAutoOutputURL = nil
                        self?.recordingState = .idle
                        let acknowledged = self?.writeAcknowledgement(
                            commandID: commandID,
                            state: status == 0 ? "stopped" : "failed",
                            outputURL: outputURL,
                            error: error
                        ) ?? false
                        self?.refresh()
                        if status == 0 {
                            self?.message = acknowledged
                                ? "Automatic recording saved"
                                : "Automatic recording saved; watcher acknowledgement failed"
                            self?.transcribe(url: outputURL)
                        } else {
                            let failure = error ?? "Automatic recording failed"
                            self?.message = acknowledged ? failure : "\(failure); watcher acknowledgement failed"
                        }
                    }
                }
                await MainActor.run {
                    guard self?.recordingState == .starting(.automatic(commandID: commandID)) else { return }
                    self?.recordingState = .recording(.automatic(commandID: commandID))
                    self?.currentOutput = outputURL.path
                    self?.message = "Automatic recording started"
                    self?.writeAcknowledgement(commandID: commandID, state: "recording", outputURL: outputURL)
                }
            } catch {
                await MainActor.run {
                    recorder.cancelFailedStart()
                    self?.autoRecorder = nil
                    self?.activeAutoCommandID = nil
                    self?.activeAutoOutputURL = nil
                    self?.recordingState = .idle
                    self?.message = error.localizedDescription
                    self?.writeAcknowledgement(
                        commandID: commandID,
                        state: "failed",
                        outputURL: outputURL,
                        error: error.localizedDescription
                    )
                }
            }
        }
    }

    @discardableResult
    private func writeSuppression(commandID: String) -> Bool {
        let payload: [String: Any] = [
            "suppressed": true,
            "commandID": commandID,
            "createdAt": ISO8601DateFormatter().string(from: Date()),
            "reason": "user_stop"
        ]
        do {
            try writeJSON(payload, to: suppressionURL)
            autoSuppressed = true
            return true
        } catch {
            return false
        }
    }

    @discardableResult
    private func writeAcknowledgement(commandID: String, state: String, outputURL: URL?, error: String? = nil) -> Bool {
        var payload: [String: Any] = [
            "commandID": commandID,
            "state": state,
            "outputPath": outputURL?.path ?? "",
            "updatedAt": ISO8601DateFormatter().string(from: Date())
        ]
        if let error { payload["error"] = error }
        do {
            try writeJSON(payload, to: acknowledgementURL)
            return true
        } catch {
            message = "Dashboard acknowledgement failed: \(error.localizedDescription)"
            return false
        }
    }

    private func writeJSON(_ object: [String: Any], to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
    }

    func openOutputFolder() {
        NSWorkspace.shared.open(outputRoot)
    }

    func reveal(_ item: RecordingItem) {
        NSWorkspace.shared.activateFileViewerSelecting([item.url])
    }

    func setTranscriptFormat(_ value: String) {
        transcriptFormat = value
        updateConfig(key: "transcribe_output_format", value: value)
    }

    func setSummaryEnabled(_ value: Bool) {
        summaryEnabled = value
        updateConfig(key: "summary", value: value ? "on" : "off")
    }

    func setHUDVisible(_ value: Bool) {
        hudVisible = value
        UserDefaults.standard.set(value, forKey: "hudVisible")
        message = value ? "Floating HUD shown" : "Floating HUD hidden"
    }

    private func readStatus() {
        let status: RecorderStatus?
        if let data = try? Data(contentsOf: statusURL) {
            status = try? JSONDecoder().decode(RecorderStatus.self, from: data)
        } else {
            status = nil
        }
        let hasLocalRecorder = manualRecorder != nil || autoRecorder != nil

        if hasLocalRecorder {
            if let status, statusIsFresh(status) {
                updateMeters(from: status)
                if !status.outputPath.isEmpty {
                    currentOutput = status.outputPath
                }
                readProgress(activeOutput: status.outputPath)
            }
            return
        }

        guard let status, statusIsFresh(status) else {
            recordingState = .idle
            level = 0
            systemLevel = 0
            microphoneLevel = 0
            currentOutput = ""
            return
        }

        recordingState = status.recording ? .recording(.external) : .idle
        updateMeters(from: status)
        currentOutput = status.recording ? status.outputPath : ""
        readProgress(activeOutput: status.outputPath)
    }

    private func updateMeters(from status: RecorderStatus) {
        level = min(max(status.level, 0), 1)
        systemLevel = min(max(status.systemLevel, 0), 1)
        microphoneLevel = min(max(status.microphoneLevel, 0), 1)
    }

    private func readFiles() {
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: outputRoot,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        )) ?? []

        files = urls.compactMap { folder in
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: folder.path, isDirectory: &isDir), isDir.boolValue else {
                return nil
            }
            let recording = ["recording.mp4", "recording.m4a", "recording.wav"]
                .map { folder.appendingPathComponent($0) }
                .first { FileManager.default.fileExists(atPath: $0.path) }
            guard let recording else { return nil }
            let modifiedAt = ((try? recording.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate) ?? .distantPast
            let transcript = ["transcript.md", "transcript.txt", "transcript.json", "transcript.diarized.json"]
                .map { folder.appendingPathComponent($0) }
                .first { FileManager.default.fileExists(atPath: $0.path) }
            let summary = folder.appendingPathComponent("summary.md")
            func stage(_ name: String) -> String? {
                guard let data = try? Data(contentsOf: folder.appendingPathComponent(name)),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
                return object["stage"] as? String
            }
            var state = ""
            switch stage("progress.json") {
            case "error": state = "litterointi odottaa uusintaa"
            case "summary_error": state = "yhteenveto odottaa uusintaa"
            default: break
            }
            if state.isEmpty, FileManager.default.fileExists(atPath: folder.appendingPathComponent("huipputaso-jono.json").path) {
                state = stage("progress-huipputaso.json") == "error" ? "Huipputaso odottaa uusintaa" : "Huipputaso jonossa"
            }
            var locationEU = false
            if let data = try? Data(contentsOf: folder.appendingPathComponent("job.json")),
               let job = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                locationEU = (job["sijainti"] as? String) == sijaintiEU
            }
            var outsideEU = false
            if locationEU, let log = try? String(contentsOf: folder.appendingPathComponent("kutsut.jsonl"), encoding: .utf8) {
                outsideEU = log.split(separator: "\n").contains { line in
                    line.contains("\"host\"") && !line.contains("aiplatform.eu.rep.googleapis.com")
                }
            }
            return RecordingItem(
                folderURL: folder,
                url: recording,
                transcriptURL: transcript,
                summaryURL: FileManager.default.fileExists(atPath: summary.path) ? summary : nil,
                modifiedAt: modifiedAt,
                state: state,
                locationEU: locationEU,
                outsideEU: outsideEU
            )
        }
        .sorted { $0.modifiedAt > $1.modifiedAt }

        if currentOutput.isEmpty, let latest = files.first {
            readProgress(activeOutput: latest.url.path)
        }
    }

    private func loadConfig() {
        guard let data = try? Data(contentsOf: configURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        transcriptFormat = (object["transcribe_output_format"] as? String) ?? "md"
        summaryEnabled = ((object["summary"] as? String) ?? "on") != "off"
    }

    private func updateConfig(key: String, value: Any) {
        do {
            let data = try Data(contentsOf: configURL)
            var object = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
            object[key] = value
            let updated = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
            try updated.write(to: configURL, options: .atomic)
            message = "Setting saved"
        } catch {
            message = "Could not update config: \(error.localizedDescription)"
        }
    }

    private func transcribe(url: URL) {
        guard FileManager.default.fileExists(atPath: transcribePython.path) else {
            message = "Recording saved; Python runtime missing (run install_launch_agent.sh)"
            return
        }

        let worker = appRoot.appendingPathComponent("transcribe_recording.py")
        guard FileManager.default.fileExists(atPath: worker.path) else {
            message = "Recording saved; transcription worker missing"
            return
        }

        transcriptionProgress = 0.01
        transcriptionMessage = "Starting transcription"
        let process = Process()
        process.executableURL = transcribePython
        process.arguments = [
            worker.path,
            "--recording", url.path,
            "--config", configURL.path,
        ]
        // Forward the keys from the env file (Gemini API key for the global position; the EU
        // service uses gcloud login). The worker reads the switches itself.
        process.environment = ProcessInfo.processInfo.environment.merging(loadEnv()) { _, new in new }
        process.terminationHandler = { [weak self] finished in
            Task { @MainActor in
                self?.message = finished.terminationStatus == 0 ? "Transcript and summary saved" : "Transcription failed"
                self?.refresh()
            }
        }

        do {
            try process.run()
            message = "Transcribing manual recording"
        } catch {
            message = "Could not transcribe: \(error.localizedDescription)"
        }
    }

    private func loadEnv() -> [String: String] {
        guard let text = try? String(contentsOf: envURL, encoding: .utf8) else { return [:] }
        var env: [String: String] = [:]
        for line in text.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            guard let eq = trimmed.firstIndex(of: "=") else { continue }
            var key = String(trimmed[..<eq]).trimmingCharacters(in: .whitespaces)
            // Tolerate a leading `export ` (shell-style env files use it; the
            // watcher sources the file so it works there, but this parser must
            // strip it or the variable name ends up as "export NAME").
            if key.hasPrefix("export ") {
                key = String(key.dropFirst("export ".count)).trimmingCharacters(in: .whitespaces)
            }
            var value = String(trimmed[trimmed.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if value.count >= 2,
               (value.hasPrefix("\"") && value.hasSuffix("\"")) || (value.hasPrefix("'") && value.hasSuffix("'")) {
                value = String(value.dropFirst().dropLast())
            }
            if !key.isEmpty { env[key] = value }
        }
        return env
    }

    private func readProgress(activeOutput: String) {
        let folder: URL
        if !activeOutput.isEmpty {
            folder = URL(fileURLWithPath: activeOutput).deletingLastPathComponent()
        } else if let latest = files.first {
            folder = latest.folderURL
        } else {
            return
        }
        var progressURL = folder.appendingPathComponent("progress.json")
        let upgradeURL = folder.appendingPathComponent("progress-huipputaso.json")
        func mtime(_ u: URL) -> Date {
            ((try? u.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate) ?? .distantPast
        }
        if mtime(upgradeURL) > mtime(progressURL) { progressURL = upgradeURL }
        guard let data = try? Data(contentsOf: progressURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        transcriptionProgress = min(max(object["progress"] as? Double ?? transcriptionProgress, 0), 1)
        transcriptionMessage = object["message"] as? String ?? transcriptionMessage
    }

}

struct LevelBar: View {
    var value: Double
    var color: Color

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                Capsule()
                    .fill(color)
                    .frame(width: max(4, proxy.size.width * CGFloat(min(max(value, 0), 1))))
            }
        }
        .frame(height: 7)
    }
}

struct DashboardView: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Circle()
                    .fill(model.isRecording ? Color.red : Color.gray)
                    .frame(width: 12, height: 12)
                Text(model.isRecording ? "Recording" : "Idle")
                    .font(.headline)
                Spacer()
                Button(model.primaryActionTitle) {
                    model.isRecording ? model.stopRecording() : model.startManualRecording()
                }
                .disabled(model.primaryActionDisabled)
                .keyboardShortcut(.defaultAction)
                Button("Folder") {
                    model.openOutputFolder()
                }
            }
            .padding()

            SwitchPanel(model: model)
                .padding(.horizontal)
                .padding(.bottom, 8)

            VStack(alignment: .leading, spacing: 8) {
                Text(model.message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Text(model.currentOutput.isEmpty ? "No active output file" : model.currentOutput)
                    .font(.caption)
                    .lineLimit(1)
                    .truncationMode(.middle)
                HStack {
                    Text("System")
                        .frame(width: 72, alignment: .leading)
                    LevelBar(value: model.systemLevel, color: .blue)
                }
                HStack {
                    Text("Mic")
                        .frame(width: 72, alignment: .leading)
                    LevelBar(value: model.microphoneLevel, color: .green)
                }
                Picker("Transcript", selection: Binding(
                    get: { model.transcriptFormat },
                    set: { model.setTranscriptFormat($0) }
                )) {
                    Text("TXT").tag("txt")
                    Text("MD").tag("md")
                    Text("JSON").tag("json")
                    Text("Diarized JSON").tag("diarized_json")
                }
                .pickerStyle(.segmented)
                Toggle("Summary", isOn: Binding(
                    get: { model.summaryEnabled },
                    set: { model.setSummaryEnabled($0) }
                ))
                Toggle("Floating HUD", isOn: Binding(
                    get: { model.hudVisible },
                    set: { model.setHUDVisible($0) }
                ))
                if model.autoSuppressed {
                    Button("Resume automatic recording") {
                        model.resumeAutomaticRecording()
                    }
                }
                if !model.transcriptionMessage.isEmpty {
                    HStack {
                        Text("Progress")
                            .frame(width: 72, alignment: .leading)
                        ProgressView(value: model.transcriptionProgress)
                        Text(model.transcriptionMessage)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
            .padding(.horizontal)
            .padding(.bottom)

            Divider()

            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(model.files.indices, id: \.self) { index in
                        FileRow(item: model.files[index], model: model)
                    }
                }
            }
        }
        .frame(minWidth: 640, minHeight: 560)
    }
}

/// The two switches (D1, D2) and the red lamp (D4), shown so they cannot be missed.
struct SwitchPanel: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                Text(model.isEU ? "🇪🇺  EU" : "🌍  MAAILMANLAAJUINEN")
                    .font(.title2.bold())
                    .foregroundStyle(.white)
                Spacer()
                Picker("Käsittelysijainti", selection: Binding(
                    get: { model.sijainti },
                    set: { model.setSijainti($0) }
                )) {
                    Text("🌍 Maailmanlaajuinen").tag(sijaintiGlobal)
                    Text("🇪🇺 EU").tag(sijaintiEU)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 280)
            }
            .padding(12)
            .background(model.isEU ? Color(red: 0.0, green: 0.55, blue: 0.3) : Color(red: 0.1, green: 0.35, blue: 0.85))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .help(model.isEU
                  ? "Ääni ja teksti käsitellään vain Googlen EU-palvelussa. Jos EU ei vastaa, käsittely voi jatkua muualla ja punainen lamppu syttyy."
                  : "Paras laatu: ääni käsitellään Google AI Studiossa (ei rajattu EU:hun).")

            HStack(spacing: 12) {
                Text(model.isHuippu ? "★  HUIPPUTASO" : "PERUSTASO")
                    .font(.title3.bold())
                    .foregroundStyle(.white)
                Spacer()
                Picker("Laatu", selection: Binding(
                    get: { model.laatu },
                    set: { model.setLaatu($0) }
                )) {
                    Text("Perustaso").tag(laatuPerus)
                    Text("★ Huipputaso").tag(laatuHuippu)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 280)
            }
            .padding(10)
            .background(model.isHuippu ? Color(red: 0.55, green: 0.25, blue: 0.75) : Color.gray)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .help(model.isHuippu
                  ? "Nopea versio heti, Huipputaso korvaa sen taustalla (kaksi ajoa + vertaava malli)."
                  : "Yksi litterointiajo.")

            if model.lampRed {
                HStack(spacing: 10) {
                    Circle().fill(Color.red).frame(width: 16, height: 16)
                        .shadow(color: .red, radius: 6)
                    Text(model.lampText)
                        .font(.callout.bold())
                        .foregroundStyle(.red)
                        .lineLimit(2)
                    Spacer()
                    Button("Kuittaa") { model.acknowledgeLamp() }
                }
                .padding(10)
                .background(Color.red.opacity(0.12))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            } else if !model.lampText.isEmpty {
                Text(model.lampText).font(.caption).foregroundStyle(.orange)
            }
        }
    }
}

struct FileRow: View {
    let item: RecordingItem
    @ObservedObject var model: DashboardModel

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(item.name)
                    .font(.system(.body, design: .monospaced))
                    .lineLimit(1)
                Text("Transcript: \(item.transcriptName) | Summary: \(item.summaryName)")
                    .font(.caption)
                    .foregroundColor(item.transcriptURL == nil ? Color.secondary : Color.green)
                    .lineLimit(1)
                if !item.state.isEmpty {
                    Text(item.state)
                        .font(.caption.bold())
                        .foregroundColor(item.state.contains("jonossa") ? .purple : .red)
                }
            }
            Spacer()
            if item.outsideEU {
                Circle().fill(Color.red).frame(width: 10, height: 10).help("EU-palaveri käsiteltiin osin EU:n ulkopuolella")
            }
            if item.locationEU {
                Text("EU").font(.caption.bold()).foregroundStyle(.green)
            }
            Button("Show") { model.reveal(item) }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        Divider()
    }
}

struct HUDView: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(model.isRecording ? Color.red : Color.gray)
                .frame(width: 8, height: 8)
            Text(model.isRecording ? "REC" : "IDLE")
                .font(.caption.bold())
                .frame(width: 34, alignment: .leading)
            LevelBar(value: model.level, color: model.isRecording ? .red : .gray)
                .frame(width: 76)
            Text(model.isEU ? "🇪🇺 EU" : "🌍")
                .font(.caption.bold())
                .foregroundStyle(model.isEU ? Color.green : Color.blue)
            if model.isHuippu {
                Text("★").font(.caption.bold()).foregroundStyle(.purple)
            }
            if model.lampRed {
                Circle().fill(Color.red).frame(width: 8, height: 8).help(model.lampText)
            }
            if model.canStopRecording {
                Button {
                    model.stopRecording()
                } label: {
                    Image(systemName: "stop.fill")
                        .foregroundStyle(.red)
                }
                .buttonStyle(.plain)
                .help("Stop recording")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = DashboardModel()
    var hudWindow: NSWindow?
    var hudTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        createHUD()
        hudTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.syncHUDVisibility()
            }
        }
    }

    private func createHUD() {
        let view = HUDView(model: model)
        let window = NSWindow(
            contentRect: NSRect(x: 40, y: 40, width: 250, height: 38),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.contentView = NSHostingView(rootView: view)
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.backgroundColor = .clear
        window.isOpaque = false
        window.hasShadow = true
        window.ignoresMouseEvents = false
        hudWindow = window
        syncHUDVisibility()
    }

    private func syncHUDVisibility() {
        guard let hudWindow else { return }
        if model.hudVisible {
            if !hudWindow.isVisible {
                hudWindow.orderFrontRegardless()
            }
        } else {
            hudWindow.orderOut(nil)
        }
    }
}

@main
struct MeetingTranscriberDashboardApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup("Meeting Transcriber") {
            DashboardView(model: appDelegate.model)
        }
        .windowStyle(.titleBar)
    }
}
