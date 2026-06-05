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
private let configURL = appRoot.appendingPathComponent("config.json")
private let envURL = home.appendingPathComponent(".meeting-transcriber.env")
private let transcribePython = runtimeRoot.appendingPathComponent("venv/bin/python")

struct RecorderStatus: Decodable {
    var recording: Bool
    var level: Double
    var systemLevel: Double
    var microphoneLevel: Double
    var outputPath: String
    var updatedAt: String
}

struct RecordingItem: Identifiable {
    let id = UUID()
    let folderURL: URL
    let url: URL
    let transcriptURL: URL?
    let summaryURL: URL?
    let modifiedAt: Date

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
        lock.unlock()

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

    func start(outputURL: URL, onFinish: @escaping @Sendable (Int32, String?) -> Void) async throws {
        self.onFinish = onFinish
        stopping = false
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
    }

    func stop() {
        stopping = true
        stream?.stopCapture { [weak self] error in
            if let error {
                self?.finish(status: 1, error: error.localizedDescription)
            }
        }
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
        statusWriter.update(recording: false)
        stream = nil
        recordingOutput = nil
        let callback = onFinish
        onFinish = nil
        callback?(status, error)
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
    @Published var isRecording = false
    @Published var level = 0.0
    @Published var systemLevel = 0.0
    @Published var microphoneLevel = 0.0
    @Published var currentOutput = ""
    @Published var transcriptFormat = "md"
    @Published var transcribeModel = "gpt-4o-mini-transcribe"
    @Published var summaryModel = "gpt-4o-mini"
    @Published var summaryEnabled = true
    @Published var transcriptionProgress = 0.0
    @Published var transcriptionMessage = ""
    @Published var files: [RecordingItem] = []
    @Published var message = "Ready"
    @Published var manualRecordingActive = false
    @Published var hudVisible = UserDefaults.standard.object(forKey: "hudVisible") as? Bool ?? true

    private var manualRecorder: DashboardRecorder?
    private var manualOutputURL: URL?
    private var autoRecorder: DashboardRecorder?
    private var autoOutputURL: URL?
    private var lastAutoCommandKey = ""
    private var timer: Timer?
    private let transcribeModels = [
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe",
        "gpt-4o-transcribe-diarize"
    ]
    // Verified OpenAI chat models only. Edit this list to expose newer ones once
    // you've confirmed they exist and work with the Responses API.
    private let summaryModels = [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1-mini",
        "gpt-4.1"
    ]

    init() {
        loadConfig()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.handleAutoCommand()
                self?.refresh()
            }
        }
    }

    func refresh() {
        readStatus()
        readFiles()
    }

    func startManualRecording() {
        guard manualRecorder == nil else { return }
        do {
            try FileManager.default.createDirectory(at: outputRoot, withIntermediateDirectories: true)
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyyMMdd-HHmmss"
            let jobDir = outputRoot.appendingPathComponent("\(formatter.string(from: Date()))-manual")
            try FileManager.default.createDirectory(at: jobDir, withIntermediateDirectories: true)
            let outputURL = jobDir.appendingPathComponent("recording.mp4")

            let recorder = DashboardRecorder()
            manualRecorder = recorder
            manualOutputURL = outputURL
            manualRecordingActive = true
            message = "Manual recording starting"
            Task {
                do {
                    try await recorder.start(outputURL: outputURL) { [weak self] status, error in
                        Task { @MainActor in
                            self?.manualRecorder = nil
                            self?.manualRecordingActive = false
                            self?.isRecording = false
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
                        self.isRecording = true
                        self.message = "Manual recording started"
                    }
                } catch {
                    await MainActor.run {
                        self.manualRecorder = nil
                        self.manualRecordingActive = false
                        self.isRecording = false
                        self.message = error.localizedDescription
                    }
                }
            }
        } catch {
            message = "Could not start recording: \(error.localizedDescription)"
        }
    }

    func stopManualRecording() {
        guard let manualRecorder else { return }
        manualRecorder.stop()
        manualRecordingActive = false
        message = "Stopping manual recording"
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
            startAutoRecording(outputURL: URL(fileURLWithPath: outputPath), detail: object["detail"] as? String)
        } else if command == "stop" {
            stopAutoRecording()
        }
    }

    private func startAutoRecording(outputURL: URL, detail: String?) {
        let recorder = DashboardRecorder()
        autoRecorder = recorder
        autoOutputURL = outputURL
        message = detail == nil ? "Automatic recording starting" : "Automatic recording starting: \(detail!)"
        Task {
            do {
                try await recorder.start(outputURL: outputURL) { [weak self] status, error in
                    Task { @MainActor in
                        self?.autoRecorder = nil
                        self?.isRecording = false
                        self?.refresh()
                        if status == 0 {
                            self?.message = "Automatic recording saved"
                            self?.transcribe(url: outputURL)
                        } else {
                            self?.message = error ?? "Automatic recording failed"
                        }
                    }
                }
                await MainActor.run {
                    self.isRecording = true
                    self.currentOutput = outputURL.path
                    self.message = "Automatic recording started"
                }
            } catch {
                await MainActor.run {
                    self.autoRecorder = nil
                    self.isRecording = false
                    self.message = error.localizedDescription
                }
            }
        }
    }

    private func stopAutoRecording() {
        guard let autoRecorder else { return }
        autoRecorder.stop()
        message = "Stopping automatic recording"
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

    func setTranscribeModel(_ value: String) {
        transcribeModel = value
        updateConfig(key: "transcribe_model", value: value)
    }

    func setSummaryModel(_ value: String) {
        summaryModel = value
        updateConfig(key: "summary_model", value: value)
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
        guard let data = try? Data(contentsOf: statusURL),
              let status = try? JSONDecoder().decode(RecorderStatus.self, from: data) else {
            if manualRecorder == nil {
                isRecording = false
                level = 0
                systemLevel = 0
                microphoneLevel = 0
            }
            return
        }

        isRecording = status.recording
        level = min(max(status.level, 0), 1)
        systemLevel = min(max(status.systemLevel, 0), 1)
        microphoneLevel = min(max(status.microphoneLevel, 0), 1)
        currentOutput = status.recording ? status.outputPath : ""
        readProgress(activeOutput: status.outputPath)
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
            return RecordingItem(
                folderURL: folder,
                url: recording,
                transcriptURL: transcript,
                summaryURL: FileManager.default.fileExists(atPath: summary.path) ? summary : nil,
                modifiedAt: modifiedAt
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
        transcribeModel = (object["transcribe_model"] as? String) ?? "gpt-4o-mini-transcribe"
        summaryModel = (object["summary_model"] as? String) ?? "gpt-4o-mini"
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
        // Forward every key from the env file so any configured provider works
        // (OpenAI, OpenRouter, ...). Local Whisper needs no key at all, so we do
        // not block transcription when no key is present.
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
            let key = String(trimmed[..<eq]).trimmingCharacters(in: .whitespaces)
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
        let progressURL = folder.appendingPathComponent("progress.json")
        guard let data = try? Data(contentsOf: progressURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        transcriptionProgress = min(max(object["progress"] as? Double ?? transcriptionProgress, 0), 1)
        transcriptionMessage = object["message"] as? String ?? transcriptionMessage
    }

    func transcribeModelOptions() -> [String] { transcribeModels }
    func summaryModelOptions() -> [String] { summaryModels }
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
                Button(model.isRecording ? "Stop" : "Start") {
                    model.manualRecordingActive ? model.stopManualRecording() : model.startManualRecording()
                }
                .disabled(model.isRecording && !model.manualRecordingActive)
                .keyboardShortcut(.defaultAction)
                Button("Folder") {
                    model.openOutputFolder()
                }
            }
            .padding()

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
                HStack {
                    Text("Transcribe")
                        .frame(width: 72, alignment: .leading)
                    Picker("", selection: Binding(
                        get: { model.transcribeModel },
                        set: { model.setTranscribeModel($0) }
                    )) {
                        ForEach(model.transcribeModelOptions(), id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                }
                HStack {
                    Text("Summary")
                        .frame(width: 72, alignment: .leading)
                    Picker("", selection: Binding(
                        get: { model.summaryModel },
                        set: { model.setSummaryModel($0) }
                    )) {
                        ForEach(model.summaryModelOptions(), id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
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
        .frame(minWidth: 640, minHeight: 480)
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
            }
            Spacer()
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
            contentRect: NSRect(x: 40, y: 40, width: 150, height: 38),
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
