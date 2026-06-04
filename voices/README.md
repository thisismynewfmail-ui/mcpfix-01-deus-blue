# voices/ — Piper TTS voice models

This folder holds the neural voice models used by the **PIPER TTS** speech
engine (Settings → **§ 7 SPEECH / VOICE OUTPUT** → *TTS Engine → PIPER TTS*).

It is optional: the default **VILLAGER TALK** blip engine needs nothing here,
and the terminal runs fine with this folder empty.

## Installing a voice

1. Install the engine once:

   ```bash
   pip install piper-tts
   ```

2. Each voice is **two files that share the same base name** (the standard
   Piper layout):

   ```
   voices/
     en_US-lessac-medium.onnx          ← the model
     en_US-lessac-medium.onnx.json     ← its config (sample rate, phonemes…)
   ```

   The part before `.onnx` (here `en_US-lessac-medium`) is the *voice key*
   shown in the UI. A bare `<key>.json` config is also accepted.

3. Download voices from the official catalogue:
   <https://huggingface.co/rhasspy/piper-voices>
   (grab both the `.onnx` and the matching `.onnx.json`).

4. In the UI open Settings → **§ 7 SPEECH**, switch the engine to **PIPER TTS**,
   click **RESCAN VOICES**, pick a voice, then **GENERATE MISSING PREVIEWS** to
   create a short sample next to each voice so you can audition them.

## previews/

Generated `<key>.wav` audition clips live in `previews/`. They are created on
demand by the **GENERATE MISSING PREVIEWS** button (only voices that don't yet
have one are synthesised) and served at `/api/tts/preview?voice=<key>`.

## Note on version control

The `.onnx` / `.onnx.json` models (tens of MB each) and the generated preview
WAVs are intentionally **git-ignored** (see `.gitignore`) — they are large and
machine-local. Only this README and the folder structure are tracked.
