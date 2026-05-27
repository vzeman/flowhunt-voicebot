# Whisper to Supertonic 3 Local Echo Demo

This example listens to the local microphone, transcribes each spoken phrase with
OpenAI Whisper, and repeats the recognized text using Supertone Supertonic 3.

## Setup

Install `ffmpeg` first. On macOS with Homebrew:

```bash
brew install ffmpeg
```

Create a Python environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the demo:

```bash
python listen_transcribe_repeat.py --whisper-model base --language en
```

For Slovak, use:

```bash
python listen_transcribe_repeat.py --whisper-model base --language sk
```

The first run downloads Whisper weights and Supertonic 3 model files. macOS may
ask for microphone permission for the terminal app.

## Useful Options

List audio devices:

```bash
python -m sounddevice
```

Use specific devices:

```bash
python listen_transcribe_repeat.py --input-device 1 --output-device 3
```

Tune silence detection if recording starts too easily or stops too early:

```bash
python listen_transcribe_repeat.py --start-threshold 0.025 --stop-threshold 0.012 --silence-ms 1200
```

If the microphone hears the generated speaker audio and starts repeating itself,
raise the barge-in threshold or use headphones:

```bash
python listen_transcribe_repeat.py --barge-in-threshold 0.10 --echo-tail-ms 800
```

Use `tiny` for lower latency, `base` or `small` for better quality, and `turbo`
if your machine can run it fast enough.
