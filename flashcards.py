import os
from google import genai
from pymongo import MongoClient
from bson import ObjectId

def get_db():
    client = MongoClient(os.getenv('MONGODB_URI', 'mongodb://localhost:27017/'))
    db_name = os.getenv('MONGODB_DB_NAME', 'nexa_db')
    return client[db_name]

def get_transcript_text(transcript_id):
    """Fetch transcript text from MongoDB."""
    db = get_db()
    transcripts = db['transcripts']
    transcript = transcripts.find_one({'_id': ObjectId(transcript_id)})
    if transcript:
        return transcript.get('content', '').replace('<br>', '\n')
    return None


def _choose_gemini_model(client: genai.Client) -> str:
    """Pick a Gemini model that supports generateContent.

    Preference order (if available for your key/project):
    - gemini-2.5-flash
    - gemini-2.0-flash
    - gemini-1.5-flash
    - gemini-1.5-pro
    - gemini-pro
    Falls back to the first model that reports support for generateContent.
    """

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
        # If listing models fails (unlikely), fall back to the latest text model.
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

    # Prefer one of the known good model IDs.
    for pref in preferred_models:
        for m in capable_models:
            if base_id(m) == pref:
                return pref

    # Otherwise, just use the first capable model's base id.
    return base_id(capable_models[0]) or preferred_models[0]

def generate_flashcards(transcript_text, num_flashcards):
    """Generate flashcards using the Google Gemini API (google.genai client)."""
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise ValueError("Google API key is not set in environment variables")

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Based on the following transcript, generate {num_flashcards} educational flashcards.
    Each flashcard should have a concise question on one side and a clear answer on the other.
    The questions should cover key concepts, facts, and ideas from the transcript.
    Format the output as a list of JSON objects with 'question' and 'answer' fields.
    
    TRANSCRIPT:
    {transcript_text}
    
    FORMAT EXAMPLE:
    [
        {{"question": "What is...", "answer": "It is..."}},
        {{"question": "Define...", "answer": "The definition is..."}}
    ]
    """
    
    # Dynamically pick a Gemini model that this API key / project
    # is actually allowed to call. This avoids 404 NOT_FOUND errors
    # from hard-coded, deprecated, or unavailable model names.
    model_name = _choose_gemini_model(client)

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )
    response_text = getattr(response, "text", "") or str(response)
    
    # Parse the response to extract flashcards
    import json
    import re
    
    # Try to extract JSON from the response
    json_match = re.search(r'\[([\s\S]*)\]', response_text)
    if json_match:
        try:
            flashcards_json = json.loads('[' + json_match.group(1) + ']')
            return flashcards_json
        except json.JSONDecodeError:
            pass
    
    # If JSON extraction fails, create flashcards from text parsing
    flashcards = []
    parts = response_text.split("question")
    for i in range(1, min(num_flashcards + 1, len(parts))):
        try:
            question_part = parts[i].split("answer")
            if len(question_part) > 1:
                question = question_part[0].strip().strip('":').strip()
                answer = question_part[1].strip().strip('",').strip()
                flashcards.append({"question": question, "answer": answer})
        except Exception:
            continue
    
    return flashcards

def save_flashcards(transcript_id, flashcards):
    """Save generated flashcards back to MongoDB."""
    db = get_db()
    transcripts = db['transcripts']
    transcripts.update_one(
        {'_id': ObjectId(transcript_id)},
        {'$set': {'flashcards': flashcards}}
    )
