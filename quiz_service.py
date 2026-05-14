import os
import uuid
import json
import random
from google import genai
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime

def get_db():
    client = MongoClient(os.getenv('MONGODB_URI', 'mongodb://localhost:27017/'))
    db_name = os.getenv('MONGODB_DB_NAME', 'nexa_db')
    return client[db_name]


def _choose_gemini_model(client: genai.Client) -> str:
    """Select a Gemini model that supports generateContent for this API key.

    Preference order (if available):
    - gemini-2.5-flash
    - gemini-2.0-flash
    - gemini-1.5-flash
    - gemini-1.5-pro
    - gemini-pro
    Falls back to the first model that declares generateContent support.
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
        # If listing models fails, just use the latest recommended model id.
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

    capable = [m for m in models if supports_generate_content(m)]
    if not capable:
        return preferred_models[0]

    for pref in preferred_models:
        for m in capable:
            if base_id(m) == pref:
                return pref

    return base_id(capable[0]) or preferred_models[0]

def fetch_current_transcript(transcript_id):
    """Fetch transcript text from MongoDB."""
    db = get_db()
    transcripts = db['transcripts']
    transcript = transcripts.find_one({'_id': ObjectId(transcript_id)})
    if transcript:
        return transcript.get('raw_transcript', ''), transcript
    return None, None

def generate_quiz_with_gemini(transcript_text, n_questions, types=None):
    """Generate a quiz using the Google Gemini API (google.genai client)."""
    api_key = os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise ValueError("Google API key is not set in environment variables")

    client = genai.Client(api_key=api_key)
    
    # Determine what types of questions to include
    allowed_types = []
    if not types:
        allowed_types = ["mcq", "true_false", "fill_blank"]
    else:
        if 'mcq' in types:
            allowed_types.append("mcq")
        if 'true_false' in types:
            allowed_types.append("true_false")
        if 'fill_blank' in types:
            allowed_types.append("fill_blank")
    
    # Default to all types if none were selected
    if not allowed_types:
        allowed_types = ["mcq", "true_false", "fill_blank"]
    
    type_instructions = ", ".join(allowed_types)
    
    system_prompt = "You are generating a mixed quiz from the provided transcript. Only use the transcript. Return strict JSON per the provided schema—no markdown or prose."
    
    user_prompt = f"""
TRANSCRIPT:
{transcript_text}

REQUIREMENTS:
- Total questions: {n_questions}
- Allowed types: {type_instructions}
- Strict JSON, schema exactly as specified.
- Keep questions concise; explanations 1–2 lines.
- Do not include answers that aren't directly supported by the transcript.
- No duplicate questions; cover diverse parts of the transcript.

Generate a quiz in the following JSON format:
{{
  "quiz": [
    {{
      "id": "string-uuid",
      "type": "mcq" | "true_false" | "fill_blank",
      "question": "string",
      "options": ["A", "B", "C", "D"],   // present ONLY for type=mcq (exactly 4)
      "answer": "string",                 // for mcq = one of options; for true_false = "True"/"False"; for fill_blank = the exact expected string
      "explanation": "string"             // brief, 1-2 lines
    }}
  ]
}}

Return VALID JSON ONLY - no extra text, no markdown formatting.
"""
    
    # Pick a model that is actually available for this API key
    # to avoid 404 NOT_FOUND errors on deprecated or disabled models.
    model_name = _choose_gemini_model(client)

    # Call Gemini via the google.genai client
    response = client.models.generate_content(
        model=model_name,
        contents=system_prompt + "\n\n" + user_prompt,
    )

    response_text = getattr(response, "text", "") or str(response)
    
    # Try to extract JSON from the response
    try:
        # Remove potential markdown code blocks if present
        cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
        quiz_data = json.loads(cleaned_text)
        
        # Ensure all questions have a UUID
        for question in quiz_data.get("quiz", []):
            if "id" not in question or not question["id"]:
                question["id"] = str(uuid.uuid4())
        
        return quiz_data
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON response: {e}\nResponse text: {response_text[:1000]}")

def save_quiz(transcript_id, quiz_data):
    """Save generated quiz to MongoDB."""
    db = get_db()
    transcripts = db['transcripts']
    
    # Extract metadata
    quiz_content = quiz_data.get("quiz", [])
    types_used = set(q.get("type") for q in quiz_content)
    
    quiz_record = {
        'quiz': quiz_content,
        'createdAt': datetime.now(),
        'nQuestions': len(quiz_content),
        'types': list(types_used)
    }
    
    # Save to the transcript document
    transcripts.update_one(
        {'_id': ObjectId(transcript_id)},
        {'$set': {'quiz': quiz_record}}
    )
    
    return quiz_record

def grade_answer(quiz_data, question_id, user_answer):
    """Grade a user's answer for a specific question."""
    quiz_content = quiz_data.get("quiz", [])
    
    for question in quiz_content:
        if question.get("id") == question_id:
            correct_answer = question.get("answer")
            question_type = question.get("type")
            
            is_correct = False
            
            # Grade based on question type
            if question_type == "mcq":
                is_correct = user_answer == correct_answer
            elif question_type == "true_false":
                is_correct = user_answer.lower() == correct_answer.lower()
            elif question_type == "fill_blank":
                # Case-insensitive comparison for fill-in-the-blanks
                is_correct = user_answer.lower().strip() == correct_answer.lower().strip()
            
            return {
                "correct": is_correct,
                "correctAnswer": correct_answer,
                "explanation": question.get("explanation", "")
            }
    
    return None

def save_attempt(transcript_id, quiz_id, user_id, responses, score):
    """Save a quiz attempt to MongoDB."""
    db = get_db()
    attempts = db.get_collection('attempts')
    
    attempt = {
        'transcriptId': ObjectId(transcript_id),
        'quizId': quiz_id,
        'userId': user_id,
        'responses': responses,
        'score': score,
        'createdAt': datetime.now(),
        'completedAt': datetime.now()
    }
    
    attempt_id = attempts.insert_one(attempt).inserted_id
    return str(attempt_id)

def shuffle_options(quiz_data):
    """Shuffle the options for MCQ questions and track the original order."""
    
    quiz_content = quiz_data.get("quiz", [])
    
    for question in quiz_content:
        if question.get("type") == "mcq" and "options" in question:
            options = question["options"]
            correct_option = question["answer"]
            
            # Store the original index of each option
            option_mapping = {option: index for index, option in enumerate(options)}
            
            # Shuffle the options
            random.shuffle(options)
            
            # Update the answer to match the new position
            question["answer"] = correct_option
            question["originalOrder"] = option_mapping
    
    return quiz_data
