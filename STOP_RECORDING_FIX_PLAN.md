# Stop Recording Fix Plan

## Purpose

Automatic recording already starts reliably enough to be useful. The critical
failure is that a user cannot stop an automatic recording when browser based
meeting detection remains active after the actual meeting has ended.

This plan keeps the current architecture:

```text
Python watcher detects a meeting
-> watcher writes dashboard-command.json
-> native dashboard owns ScreenCaptureKit recording
-> transcription worker processes the completed recording
```

The fix adds a reliable user stop path and a persistent suppression latch. It
does not replace ScreenCaptureKit, the watcher, the command file protocol, or
the transcription pipeline.

## Confirmed Current Behavior

### Automatic Stop Button Is Disabled

The dashboard button displays `Stop` whenever `isRecording` is true, but its
action only stops a manual recorder. During automatic recording,
`manualRecordingActive` is false, so the button is disabled.

Relevant code:

```text
native-meeting-transcriber/Sources/MeetingTranscriberDashboard/main.swift
DashboardView, lines around the primary Start and Stop button
```

### Watcher Does Not Know About User Intent

`DashboardCommandProcess.poll()` always returns `None`. The watcher therefore
has no acknowledgement channel from the dashboard and no state representing
"the user stopped this detected meeting".

If detection remains active, the watcher can eventually reach its maximum
recording duration, issue a stop, clear its state, and start another recording
for the same stale meeting page.

### Teams Join Pages Can Remain Detectable

The browser detector treats a Teams URL containing `meet` as a meeting. A join
or post-meeting page can retain such a URL and remain open after the call. The
watcher then sees a meeting even though no useful conversation is happening.

### Status Can Become Stale

`status.json` contains `updatedAt`, but the dashboard does not use it. A crash
can leave `recording: true` behind and make the next dashboard session display a
recording that no longer exists.

### Manual Start Does Not Guard Against Automatic Recording

`startManualRecording()` checks `manualRecorder` but not `autoRecorder`. A
transient status read failure can make the UI appear idle and allow a second
recorder to start.

## Required User Behavior

1. Automatic recording starts as it does now.
2. The main dashboard always offers an enabled Stop button during a real local
   recording, regardless of how recording started.
3. The floating HUD shows a stop-square control while recording.
4. Pressing Stop immediately asks ScreenCaptureKit to finalize the MP4.
5. The completed recording enters the normal transcription pipeline.
6. The watcher does not restart recording while the same stale meeting
   detection remains present.
7. Suppression survives a watcher restart.
8. Suppression clears after meeting detection has been absent for the configured
   number of consecutive checks.
9. A later meeting can start automatically as normal.

## Proposed State Model

Replace `manualRecordingActive` as the routing decision with explicit local
recording state.

Suggested Swift model:

```swift
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
}
```

`DashboardModel` remains the owner of the two existing `DashboardRecorder`
references. The enum records intent and gives the UI one reliable source for
button labels and availability.

## Dashboard Changes

### One Public Stop Method

Add one method:

```swift
func stopRecording(userInitiated: Bool = true)
```

Behavior:

1. If `manualRecorder` exists, set state to stopping and call its `stop()`.
2. If `autoRecorder` exists, write the persistent automatic suppression file,
   set state to stopping, and call its `stop()`.
3. If neither recorder exists, clear any stale local recording display rather
   than pretending a stream can be stopped.
4. Let the existing recorder completion callback launch transcription after the
   MP4 is finalized.

Do not route Stop through `manualRecordingActive`.

### Primary Button

The primary button should behave as follows:

```text
idle -> Start manual recording
starting -> disabled
recording -> Stop the active recorder
stopping -> disabled with "Stopping"
```

It must be enabled for both manual and automatic recording states.

### HUD Stop Control

When recording, add a compact button using the SF Symbol `stop.fill` to the
floating HUD. Give it the tooltip `Stop recording`. The HUD already accepts
mouse events, so no window architecture change is required.

### Strong Start Guards

Both start paths must require:

```swift
manualRecorder == nil && autoRecorder == nil
```

This prevents simultaneous ScreenCaptureKit streams.

### Stale Status Handling

Local recorder references are authoritative. The exact rule is:

```swift
if manualRecorder != nil || autoRecorder != nil {
    isRecording = true
} else if statusIsMissingOrMalformed || statusAge > staleThreshold {
    isRecording = false
} else {
    isRecording = status.recording
}
```

File age must never override an existing local recorder. `readStatus()` must
apply state in this order:

1. if `manualRecorder` exists, preserve the local manual state even when the
   status file is missing, stale, malformed, or temporarily says false,
2. if `autoRecorder` exists, preserve the local automatic state under the same
   conditions,
3. only when neither local recorder exists may `status.json` supply external
   recording state,
4. an external status is accepted only when `updatedAt` is recent.

The current missing-file branch has the inverse bug:

```swift
if manualRecorder == nil {
    isRecording = false
}
```

It clears recording state without checking `autoRecorder`. Replace that branch
with a check for both references. Missing, malformed, stale, or temporarily
contradictory file state may show idle only when:

```swift
manualRecorder == nil && autoRecorder == nil
```

The current implementation has no independent heartbeat. It writes status at
capture start and from audio sample callbacks. Do not assume silent capture
always produces timely meter callbacks.

Add a recorder heartbeat that writes current status every two seconds while a
local recorder exists. Use a ten-second staleness threshold. Stop the heartbeat
when the recorder finishes or fails.

If status is stale and neither local recorder reference exists:

1. show idle,
2. clear audio levels,
3. clear the active output path,
4. do not disable Start or Stop based on the stale file.

## Persistent User Stop Protocol

Do not reuse `dashboard-command.json` bidirectionally. The watcher currently
writes that file and the dashboard reads it. Having both processes overwrite
the same file introduces avoidable races.

Add a separate file:

```text
~/.meeting-transcriber/auto-suppression.json
```

The dashboard writes it atomically before stopping an automatic recording:

```json
{
  "suppressed": true,
  "commandID": "20260922-090111-microsoft-teams",
  "createdAt": "2026-09-22T07:37:57Z",
  "reason": "user_stop"
}
```

The file is persistent so a watcher crash or restart cannot immediately restart
the unwanted recording.

## Watcher Suppression Logic

At startup and on every poll, the watcher reads the suppression file.

When suppression is active:

1. do not start a new automatic recording,
2. continue checking meeting detection,
3. count consecutive polls with no meeting detected,
4. clear suppression only after `stop_after_consecutive_misses` is reached,
5. reset the miss count if the stale meeting detection reappears.

This intentionally suppresses all automatic starts until the stale detection
has disappeared. That is safer than trying to infer whether two unstable Teams
titles represent the same call.

The dashboard must offer a `Resume automatic recording` control whenever
suppression is active. This is required because suppression intentionally blocks
all automatic starts, including a different meeting joined while a stale tab is
still open.

Resume removes the suppression file atomically and returns the watcher to normal
detection. It does not start a recording directly. The normal consecutive-hit
rule still applies.

## Teams Detection Hardening

Suppression is the safety mechanism. Detection should still be improved to
reduce false starts.

Add configurable ignored titles for known prejoin and post-meeting pages, for
example:

```text
Liity keskusteluun
Join the conversation
Join now
Pre-join
```

Do not rely only on translated title text. Log the URL and title for false
positives, then add stable URL or DOM signals when available. Keep the current
detection as a fallback so valid Teams meetings do not stop auto-starting.

## Required Command Acknowledgement

Add a lightweight acknowledgement file written by the dashboard:

```text
~/.meeting-transcriber/dashboard-ack.json
```

It should contain the command ID, state, output path, timestamp, and an optional
error. Suggested states are `starting`, `recording`, `stopping`, `stopped`, and
`failed`.

Example:

```json
{
  "commandID": "20260922-090111-microsoft-teams",
  "state": "stopped",
  "outputPath": "~/.meeting-transcriber/output/example/recording.mp4",
  "updatedAt": "2026-09-22T07:37:58Z",
  "error": null
}
```

This lets the watcher distinguish these cases:

1. command written but dashboard never opened,
2. dashboard opened but permission was denied,
3. recording started successfully,
4. user stopped recording,
5. ScreenCaptureKit failed.

Acknowledgement is a release blocker, not an optional follow-up.

On every watcher poll, read the acknowledgement after reading suppression. When
both of these conditions are true:

1. the acknowledgement command ID matches the active
   `DashboardCommandProcess.command_id`,
2. acknowledgement state is `stopped` or `failed`,

the watcher must immediately clear all active recording state:

```text
recorder = None
audio_path = None
started_at = None
active_detection = None
hits = 0
misses = 0
```

This cleanup must use one helper so timeout stop, detection-loss stop, failed
start, and user stop cannot drift into different partial reset behavior.

Handle cleanup is independent of suppression so a fast click on Resume cannot
leave a completed recorder handle alive. After cleanup, suppression remains
active until detection has been absent for
`stop_after_consecutive_misses` polls or the user explicitly resumes automatic
recording. This prevents the stale watcher handle from surviving until
`max_recording_minutes` and prevents a restart for the same stale page.

Ignore acknowledgements with a different command ID. Treat malformed or stale
acknowledgements as diagnostic errors, not as permission to clear a live handle.

## Adjacent Bug To Fix In The Same Release

The Gemini model pickers currently write top-level keys while the explicit
`providers` configuration block supplies the actual models. The selected model
therefore does not affect transcription or summarization.

Fix either side consistently:

1. update the nested `providers.gemini.transcribe.model` and
   `providers.gemini.summarize.model` values from Swift, or
2. make provider construction apply supported top-level overrides after reading
   the explicit provider registry.

The second option is smaller and preserves the current dashboard config writer.

## Tests

### Python Unit Tests

Add tests for:

1. suppression prevents automatic start while detection remains active,
2. suppression survives watcher construction and restart,
3. suppression clears after the configured number of misses,
4. a new meeting can start after suppression clears,
5. malformed suppression JSON fails safe and logs an error,
6. matching `stopped` acknowledgement clears `recorder`, `audio_path`,
   `started_at`, and `active_detection` immediately,
7. matching `failed` acknowledgement performs the same cleanup,
8. acknowledgement for another command ID cannot clear the active handle,
9. suppression remains active after acknowledgement-driven handle cleanup,
10. Resume removes suppression and permits normal consecutive-hit detection,
11. known Teams prejoin titles are ignored,
12. Gemini top-level model overrides affect an explicit provider registry.

### Swift State Tests

Extract recording state transitions into pure methods and test:

1. manual start to recording to stopping to idle,
2. automatic start to recording to user stop to idle,
3. automatic user stop writes suppression before stopping,
4. manual start is rejected while auto recording exists,
5. automatic start is rejected while manual recording exists,
6. missing status cannot override a local manual recorder,
7. missing status cannot override a local automatic recorder,
8. malformed status cannot override either local recorder,
9. stale false status cannot override either local recorder,
10. stale true status cannot force recording state when both local references
    are nil,
11. fresh status is accepted when both local references are nil,
12. heartbeat continues to update status during silent capture,
13. Resume removes suppression without directly starting a recorder,
14. duplicate completion callbacks do not launch transcription twice.

### Manual Acceptance Test

1. Start a real Teams or Meet session and confirm automatic recording begins.
2. Keep the meeting tab open.
3. Press Stop in the main dashboard.
4. Confirm the MP4 becomes readable by `ffprobe`.
5. Confirm transcription starts once.
6. Wait at least two watcher start windows and confirm no new folder appears.
7. Confirm the watcher clears its active handle immediately after the stopped
   acknowledgement rather than waiting for the maximum recording time.
8. Restart the watcher while the stale tab remains and confirm no restart.
9. Use Resume while the stale tab remains, then confirm normal detection is
   restored.
10. Stop again, close the stale tab, and wait for suppression to clear.
11. Join a new meeting and confirm automatic recording starts.
12. Repeat using the stop control in the floating HUD.

## Deployment Order

1. Patch and test the Python watcher.
2. Patch and build the Swift dashboard.
3. Copy watcher and worker files to `~/.meeting-transcriber/app`.
4. Install the rebuilt dashboard app.
5. Keep the watcher disabled and verify manual Start, Stop, MP4 finalization,
   transcription, heartbeat, and stale-status recovery.
6. Enable the watcher for the automatic acceptance phase.
7. Verify automatic start, dashboard Stop, HUD Stop, stopped acknowledgement,
   immediate watcher-handle cleanup, suppression, watcher restart, Resume, and
   suppression auto-clear.
8. Disable the watcher again if any automatic acceptance step fails.
9. Push the reviewed commit to GitHub only after both acceptance phases pass.

## Rollback

If the new dashboard fails:

1. unload the watcher LaunchAgent,
2. reinstall the previous dashboard build,
3. remove only `auto-suppression.json` and `dashboard-ack.json`,
4. leave recordings, transcripts, configuration, and API keys untouched,
5. reload the watcher only after confirming the previous dashboard records and
   stops manually.

## Definition Of Done

The fix is complete only when all of these are true:

1. Stop works during automatic recording.
2. Stop works from both the dashboard and floating HUD.
3. The MP4 finalizes correctly.
4. Transcription launches exactly once.
5. The stale meeting page cannot restart recording.
6. Watcher restart cannot bypass suppression.
7. A stopped or failed acknowledgement clears the watcher handle immediately.
8. Resume automatic recording is available whenever suppression is active.
9. Suppression clears when meeting detection disappears.
10. A later meeting starts automatically.
11. Local recorder references override missing, stale, or contradictory file
    status.
12. Silent recording produces an independent status heartbeat.
13. Stale status cannot lock the dashboard.
14. Automated tests and both manual acceptance phases pass.
