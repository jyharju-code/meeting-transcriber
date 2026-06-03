# Publishing This To GitHub

This project lives best as a small two-folder repo:

```text
meeting-transcriber/
native-meeting-transcriber/
```

Do not push a parent folder that contains unrelated business files.

## Public Safety Check

Before publishing:

```bash
rg -n "sk-|OPENAI_API_KEY=|/Users/yourname|@yourdomain" meeting-transcriber native-meeting-transcriber
```

Expected:

- references to the string `OPENAI_API_KEY` in code/docs are fine
- no real API keys
- no personal email domains
- no private documents

Local files that should not be committed:

- `meeting-transcriber/config.json`
- `~/.meeting-transcriber.env`
- `~/.meeting-transcriber/output/`
- `native-meeting-transcriber/.build/`
- recordings, logs, transcripts from real meetings

## Create A Clean Repo Folder

From the folder that contains both project directories:

```bash
mkdir meeting-transcriber-public
rsync -a --exclude-from meeting-transcriber/.gitignore meeting-transcriber/ meeting-transcriber-public/meeting-transcriber/
rsync -a --exclude-from native-meeting-transcriber/.gitignore native-meeting-transcriber/ meeting-transcriber-public/native-meeting-transcriber/
cd meeting-transcriber-public
cp meeting-transcriber/README.md README.md
git init
git add README.md meeting-transcriber native-meeting-transcriber
git commit -m "Initial local-first meeting transcriber"
```

Then create an empty GitHub repo and push:

```bash
git remote add origin git@github.com:YOUR_USER/meeting-transcriber.git
git branch -M main
git push -u origin main
```

## Suggested Repository Description

```text
Local-first macOS Meet/Teams recorder: native ScreenCaptureKit dashboard, Python watcher, OpenAI transcription and action-item summaries.
```

## Suggested Topics

```text
macos screencapturekit openai transcription meetings google-meet microsoft-teams local-first swift python
```

## README Positioning

Keep the README boring and useful:

- what it does
- consent note
- setup commands
- macOS permission trap
- smoke test
- expected output files
- cost note

That saves other vibe coders tokens because they do not need to rediscover the
ScreenCaptureKit / LaunchAgent permission boundary.
