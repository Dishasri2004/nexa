# Nexa

Nexa is a Flask-based AI learning assistant that turns study content into transcripts, searchable knowledge, quizzes, flashcards, diagrams, and snapshot-based Q&A.

It supports:

- user signup/login with session-based access
- YouTube, text, PDF, video, audio, and browser voice input
- transcript storage in MongoDB
- transcript chat with Gemini + LangChain + FAISS
- flashcard generation
- quiz generation and grading
- diagram generation
- snapshot extraction and image-aware Q&A
- dashboard gamification with XP, streaks, and recent activity

## Tech stack

- Flask
- MongoDB / PyMongo
- Google Gemini
- LangChain + FAISS
- OpenCV, Pillow, PyMuPDF, pytesseract
- ffmpeg and yt-dlp
- Tailwind-based templates

## Project layout

- [app.py](app.py): main Flask app and routes
- [chatbot.py](chatbot.py): RAG helpers and FAISS setup
- [vid.py](vid.py): video transcription blueprint
- [voice.py](voice.py): audio transcription helper
- [flashcards.py](flashcards.py): flashcard generation
- [quiz_service.py](quiz_service.py): quiz generation and grading
- [templates](templates): frontend pages

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env`.
4. Fill in:
   - `MONGODB_URI`
   - `MONGODB_DB_NAME`
   - `GOOGLE_API_KEY`
   - `FLASK_SECRET_KEY`
5. Run the app:

```bash
python app.py
```

6. Open `http://localhost:5000`.

## Environment variables

Core variables:

- `MONGODB_URI`
- `MONGODB_DB_NAME`
- `GOOGLE_API_KEY`
- `FLASK_SECRET_KEY`

Useful optional variables:

- `GEMINI_API_KEY`
- `GEMINI_CHAT_MODEL`
- `NEXA_DATA_DIR`
- `PORT`
- `FLASK_DEBUG`
- `FLASK_SECURE_COOKIES`
- `SESSION_LIFETIME_DAYS`
- `MAX_CONTENT_LENGTH_MB`
- `LOG_LEVEL`

## Deployment

Heroku files are already included:

- [Procfile](Procfile)
- [runtime.txt](runtime.txt)
- [wsgi.py](wsgi.py)
- [Aptfile](Aptfile)
- [README-deploy.md](README-deploy.md)

Recommended production config:

- `FLASK_SECURE_COOKIES=true`
- `NEXA_DATA_DIR=/tmp/nexa`

Health check endpoint:

- `GET /health`

## Important note about persistence

This app currently stores uploads and FAISS indexes on the local filesystem. That works locally, but on Heroku those files are temporary and disappear when the dyno restarts.

For a more scalable production setup, the next upgrade should be:

- move uploads and snapshots to S3 or Cloudinary
- move vector storage to a persistent service, or rebuild indexes on demand

## Security checklist before pushing

- rotate any previously exposed Google API keys
- keep `.env` out of git
- use a strong `FLASK_SECRET_KEY`
- use MongoDB Atlas in production
