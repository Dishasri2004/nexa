# Nexa - Transcript Assistant

## Quiz Feature

Nexa now includes an interactive quiz generator that creates mixed-type quizzes (MCQs, True/False, Fill-in-the-Blanks) from your transcript content.

### Features

- Generate quizzes from transcript content using Google Gemini 1.5 Flash
- Configure question count and types
- Interactive quiz UI with immediate feedback
- Supports three question types:
  - Multiple Choice (4 options)
  - True/False
  - Fill-in-the-Blanks
- View one question at a time or all at once
- Track your score and progress
- Results are saved to MongoDB for future reference

### Setup

1. Install dependencies:
```
pip install -r requirements.txt
```

2. Configure environment variables:
```
# Copy the example .env file
cp .env.example .env

# Edit the .env file with your API keys and configuration
nano .env  # or use any text editor
```

3. Ensure MongoDB is running:
```
# Start MongoDB if not running
mongod
```

4. Run the application:
```
python app.py
```

### Using the Quiz Feature

1. Navigate to a transcript page
2. Click on the "Quiz Generator" panel in the right sidebar
3. Configure your quiz settings:
   - Number of questions
   - Question types to include (MCQ, True/False, Fill-in-the-Blanks)
   - Display mode (one question at a time or all questions)
4. Click "Generate Quiz"
5. Answer the questions and receive immediate feedback
6. View your score and progress at the bottom of the quiz panel

### API Key

This feature requires a Google API key with access to the Gemini 1.5 Flash model. Set this in your .env file:

```
GOOGLE_API_KEY=your_api_key_here
```

### Database Schema

Quizzes are stored in the transcript document under the 'quiz' field with the following structure:

```json
{
  "quiz": [
    {
      "id": "string-uuid",
      "type": "mcq|true_false|fill_blank",
      "question": "string",
      "options": ["A", "B", "C", "D"],
      "answer": "string",
      "explanation": "string"
    }
  ],
  "createdAt": "timestamp",
  "nQuestions": "number",
  "types": ["array of question types used"],
  "responses": [
    {
      "question_id": "string-uuid",
      "user_answer": "string",
      "correct": "boolean",
      "timestamp": "number"
    }
  ]
}
```
