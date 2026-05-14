from flask import Blueprint, render_template, request, jsonify
import os
import tempfile
from google import genai
from dotenv import load_dotenv
import time
from pathlib import Path

# Load environment variables and configure Gemini client
load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None

# Create blueprint for video handling
vid_bp = Blueprint('vid', __name__, template_folder='templates')

# Export speech_to_text function at module level
__all__ = ['vid_bp', 'speech_to_text']

def choose_transcription_model(client: genai.Client) -> str:
    preferred_models = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-pro",
    ]

    try:
        models = list(client.models.list())
    except Exception:
        return preferred_models[0]

    def supports_generate_content(model) -> bool:
        for attr in ("supported_actions", "supportedGenerationMethods", "supported_generation_methods"):
            methods = getattr(model, attr, None) or []
            if "generateContent" in methods or "generate_content" in methods:
                return True
        return False

    def base_id(model) -> str:
        for attr in ("base_model_id", "baseModelId"):
            value = getattr(model, attr, None)
            if value:
                return value
        name = getattr(model, "name", "")
        if name.startswith("models/"):
            return name.split("/", 1)[-1]
        return name

    capable_models = [m for m in models if supports_generate_content(m)]
    if not capable_models:
        return preferred_models[0]

    for preferred in preferred_models:
        for model in capable_models:
            if base_id(model) == preferred:
                return preferred

    return base_id(capable_models[0]) or preferred_models[0]


def _get_client() -> genai.Client:
    global client
    if client is not None:
        return client
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Google API key not found. Set GOOGLE_API_KEY (or GEMINI_API_KEY) in your environment.")
    client = genai.Client(api_key=api_key)
    return client

def convert_media_to_wav(src_path: str) -> str:
    """
    Extracts audio from video files or converts audio files to wav format.
    Supports video formats (mp4, avi, mkv, etc.) and audio formats (webm, ogg, m4a, etc.)
    """
    import shutil
    import subprocess
    from pathlib import Path

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is required to extract audio and convert to wav. Install ffmpeg first.")

    src = Path(src_path)
    dst = src.with_suffix(".wav")

    # -vn flag tells ffmpeg to ignore video stream
    cmd = [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-vn", str(dst)]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.decode('utf-8', errors='ignore')}")

    return str(dst)

def speech_to_text(filepath):
    """
    Convert speech from video to text using Gemini API.
    First extracts audio from video, then transcribes.
    """
    audio_path = None
    try:
        print(f"Processing file: {filepath}")
        
        # First convert video to wav format
        print("Converting to WAV format...")
        audio_path = convert_media_to_wav(filepath)
        print(f"Audio extracted successfully: {audio_path}")

        # Create a structured prompt for better transcription
        prompt = [
            "Please transcribe this audio file accurately.",
            "Include proper punctuation and formatting.",
            "Make sure to capture all spoken content faithfully.",
            "Format the output as clean, readable text."
        ]
        
        print("Uploading to Gemini...")
        # Upload audio file to Gemini via google.genai client.
        # Different google-genai versions support different upload signatures,
        # so we try the stable form first and fall back when needed.
        active_client = _get_client()
        try:
            uploaded_file = active_client.files.upload(file=audio_path)
        except TypeError:
            try:
                uploaded_file = active_client.files.upload(file=audio_path, config={"mime_type": "audio/wav"})
            except TypeError:
                uploaded_file = active_client.files.upload(audio_path)

        print("Generating transcript...")
        model_name = choose_transcription_model(active_client)
        # Generate transcript with context
        response = active_client.models.generate_content(
            model=model_name,
            contents=[*prompt, uploaded_file],
        )
        
        if not getattr(response, "text", None):
            raise RuntimeError("No transcript generated")
            
        # Clean up the transcript
        transcript = response.text.strip()
        print("Transcription completed successfully")
        
        return transcript
        
    except Exception as e:
        error_text = str(e)
        if "API_KEY_INVALID" in error_text or "API key expired" in error_text or "invalid api key" in error_text.lower():
            error_msg = "Transcription failed: Gemini API key is invalid or expired. Please update GOOGLE_API_KEY (or GEMINI_API_KEY)."
        else:
            error_msg = f"Transcription failed: {error_text}"
        print(error_msg)
        return error_msg
        
    finally:
        # Cleanup temporary audio file
        if audio_path and audio_path != filepath:
            try:
                os.remove(audio_path)
                print(f"Cleaned up temporary audio file: {audio_path}")
            except Exception as e:
                print(f"Error cleaning up audio file: {e}")

# Video processing routes
@vid_bp.route('/process-video', methods=['POST'])
def process_video():
    """
    Process video files and generate transcripts.
    Returns the transcript in JSON format.
    """
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'No video file uploaded'}), 400
            
        video = request.files['video']
        if not video.filename:
            return jsonify({'error': 'No file selected'}), 400
            
        # Check file extension
        ext = os.path.splitext(video.filename)[1].lower()
        if ext not in ['.mp4', '.avi', '.mkv', '.mov', '.flv', '.webm']:
            return jsonify({
                'error': 'Invalid file type. Please upload a video file (MP4, AVI, MKV, MOV, FLV, WEBM)'
            }), 400

        # Process video
        print(f"Processing video: {video.filename}")
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp:
            try:
                # Save uploaded file
                video.save(temp.name)
                print(f"Saved to temporary file: {temp.name}")
                
                # Generate transcript
                transcript = speech_to_text(temp.name)
                
                if transcript.startswith("Transcription failed:"):
                    return jsonify({'error': transcript}), 500
                    
                return jsonify({
                    'success': True,
                    'transcript': transcript
                })
                
            finally:
                # Cleanup
                try:
                    os.unlink(temp.name)
                    print(f"Cleaned up temporary file: {temp.name}")
                except Exception as e:
                    print(f"Error cleaning up temp file: {e}")
                    
    except Exception as e:
        error_msg = f"Error processing video: {str(e)}"
        print(error_msg)
        return jsonify({'error': error_msg}), 500
