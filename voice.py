import os
from dotenv import load_dotenv
import shutil
import subprocess
from pathlib import Path
import google.generativeai as genai
load_dotenv()
import time


def _configure_genai() -> None:
    gemini_api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise RuntimeError("Google API key not found. Set GOOGLE_API_KEY (or GEMINI_API_KEY) in your environment.")
    genai.configure(api_key=gemini_api_key)


def convert_to_wav(src_path: str) -> str:
    """Convert audio file to wav format using ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is required to transcode webm/ogg to wav. Install ffmpeg or record a wav/mp3 file.")

    src = Path(src_path)
    dst = src.with_suffix(".wav")

    cmd = [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.decode('utf-8', errors='ignore')}")

    return str(dst)


def speech_to_text(audio_path):
    """Converts speech to text using Gemini API."""
    _configure_genai()
    model_candidates = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-pro"
    ]
    model = None
    upload_path = audio_path
    try:
        ext = os.path.splitext(audio_path)[1].lower()
        if ext in (".webm", ".ogg", ".m4a"):
            upload_path = convert_to_wav(audio_path)
    except Exception:
        raise

    mime_type = None
    if upload_path.lower().endswith('.wav'):
        mime_type = 'audio/wav'
    elif upload_path.lower().endswith('.mp3'):
        mime_type = 'audio/mpeg'
    elif upload_path.lower().endswith('.ogg'):
        mime_type = 'audio/ogg'

    uploaded = genai.upload_file(upload_path, mime_type=mime_type)

    file_obj = uploaded
    for _ in range(30):
        try:
            file_obj = genai.get_file(uploaded.name)
        except Exception:
            time.sleep(1)
            continue

        err = getattr(file_obj, "error", None)
        has_actionable_error = False
        if err is not None:
            try:
                code = getattr(err, "code", 0)
                message = getattr(err, "message", "") or ""
                if code != 0 or message.strip():
                    has_actionable_error = True
            except Exception:
                has_actionable_error = True

        if has_actionable_error:
            try:
                info = file_obj.to_dict()
            except Exception:
                info = str(file_obj.error)
            import json
            try:
                info_json = json.dumps(info, default=str, indent=2)
            except Exception:
                info_json = str(info)
            print("Uploaded file error details:\n", info_json)
            raise RuntimeError(f"Uploaded file error: {info_json}")

        try:
            state = file_obj.state
            state_name = getattr(state, "name", None) or str(state)
        except Exception:
            state_name = None

        if state_name and "ACTIVE" in state_name:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"Uploaded file did not become ACTIVE within timeout: {uploaded}")

    last_error = None
    for model_name in model_candidates:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(["Transcribe this audio:", file_obj])
            return response.text
        except Exception as e:
            last_error = e
            message = str(e)
            if "not found" in message.lower() or "not supported" in message.lower() or "404" in message:
                continue
            raise

    raise RuntimeError(f"No supported Gemini transcription model available: {last_error}")
