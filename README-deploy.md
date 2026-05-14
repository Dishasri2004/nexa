# Nexa Deployment Notes

## Local setup

1. Create a virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env`.
3. Set `MONGODB_URI`, `GOOGLE_API_KEY`, and `FLASK_SECRET_KEY`.
4. Run `python app.py`.

## Heroku setup

1. Create the app and attach MongoDB Atlas.
2. Set config vars:
   - `MONGODB_URI`
   - `MONGODB_DB_NAME`
   - `GOOGLE_API_KEY`
   - `FLASK_SECRET_KEY`
   - `FLASK_SECURE_COOKIES=true`
   - `NEXA_DATA_DIR=/tmp/nexa`
3. Add the `heroku-buildpack-apt` buildpack before the Python buildpack so `Aptfile` installs `ffmpeg` and `tesseract-ocr`.
4. Deploy this folder as the Heroku app root.
5. Verify `GET /health`.

## Important production note

Heroku filesystem storage is ephemeral. Uploaded files and FAISS indexes stored in `/tmp` are temporary and will be cleared on dyno restart. For long-term scalability, move uploads and vector indexes to persistent services such as S3/Cloudinary and a managed vector store or rebuildable cache workflow.
