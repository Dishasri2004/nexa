from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, after_this_request
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import re
import html
import json
import shutil
import uuid
import hashlib
from bson import ObjectId
import time
import tempfile
from datetime import date, datetime, timedelta
from typing import Any, Dict
from dotenv import load_dotenv
from google import genai
import logging
from vid import vid_bp
from vid import speech_to_text as video_to_text  # Import with explicit alias
from Youtube_Transcript import get_video_id, get_transcript_from_url
from voice import speech_to_text as audio_to_text

from flashcards import get_transcript_text, generate_flashcards, save_flashcards
from quiz_service import (fetch_current_transcript, generate_quiz_with_gemini, 
                         save_quiz, grade_answer, save_attempt, shuffle_options)

# Import chatbot functions
from chatbot import (load_documents, load_text_documents, split_documents, 
                    create_faiss_vectorstore, load_faiss_vectorstore, 
                    setup_rag_pipeline, get_api_key, validate_api_key)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("NEXA_DATA_DIR", BASE_DIR)
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
FAISS_ROOT = os.path.join(DATA_DIR, "faiss_indexes")
CHATBOT_INDEX_NAME = "chatbot"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(FAISS_ROOT, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Use a stable secret key so sessions (and login state) survive
# code reloads and server restarts during development.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH_MB", "80")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_SECURE_COOKIES", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "7")))

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# Register the video blueprint
app.register_blueprint(vid_bp)

_mongo_client = None
_db = None


def _faiss_index_path(index_name: str) -> str:
    return os.path.join(FAISS_ROOT, index_name)

# MongoDB connection with retry logic
def get_db():
    global _mongo_client, _db
    if _db is not None:
        return _db

    max_tries = 3
    tries = 0
    mongodb_uri = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
    db_name = os.environ.get('MONGODB_DB_NAME', 'nexa_db')
    while tries < max_tries:
        try:
            _mongo_client = MongoClient(
                mongodb_uri,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=int(os.environ.get("MONGODB_MAX_POOL_SIZE", "20")),
                retryWrites=True,
            )
            # Test the connection
            _mongo_client.admin.command("ping")
            _db = _mongo_client[db_name]
            return _db
        except Exception as e:
            tries += 1
            if tries == max_tries:
                logger.exception("Could not connect to MongoDB")
                raise
            time.sleep(2)  # Wait before retrying

# Get database collections
def get_users_collection():
    return get_db()['users']

def get_transcripts_collection():
    return get_db()['transcripts']

XP_PER_ACTION = {
    'upload': 140,
    'link': 100,
    'record': 120
}

def default_analytics():
    return {
        'uploads': 0,
        'links': 0,
        'records': 0,
        'xp': 0,
        'streak': 1,
        'last_active_date': date.today().isoformat(),
        'recent_actions': []
    }

def get_user_analytics(user_id):
    users = get_users_collection()
    user = users.find_one({'_id': ObjectId(user_id)}, {'analytics': 1})

    if not user:
        return default_analytics()

    analytics = user.get('analytics')
    if not analytics:
        analytics = default_analytics()
        users.update_one({'_id': ObjectId(user_id)}, {'$set': {'analytics': analytics}})

    return analytics

def refresh_user_streak(user_id):
    users = get_users_collection()
    analytics = get_user_analytics(user_id)

    today = date.today()
    today_str = today.isoformat()
    last_active_str = analytics.get('last_active_date')

    if not last_active_str:
        analytics['last_active_date'] = today_str
    else:
        try:
            last_active = date.fromisoformat(last_active_str)
            day_diff = (today - last_active).days
            if day_diff == 1:
                analytics['streak'] = int(analytics.get('streak', 1)) + 1
            elif day_diff > 1:
                analytics['streak'] = 1
            analytics['last_active_date'] = today_str
        except Exception:
            analytics['streak'] = max(1, int(analytics.get('streak', 1)))
            analytics['last_active_date'] = today_str

    users.update_one({'_id': ObjectId(user_id)}, {'$set': {'analytics': analytics}})
    return analytics

def track_user_action(user_id, action_type, detail):
    if action_type not in XP_PER_ACTION:
        return

    users = get_users_collection()
    analytics = get_user_analytics(user_id)

    if action_type == 'upload':
        analytics['uploads'] = int(analytics.get('uploads', 0)) + 1
    elif action_type == 'link':
        analytics['links'] = int(analytics.get('links', 0)) + 1
    elif action_type == 'record':
        analytics['records'] = int(analytics.get('records', 0)) + 1

    analytics['xp'] = int(analytics.get('xp', 0)) + XP_PER_ACTION[action_type]

    recent_actions = analytics.get('recent_actions', [])
    action_label = f"{time.strftime('%Y-%m-%d %H:%M:%S')} — {detail} (+{XP_PER_ACTION[action_type]} XP)"
    recent_actions.insert(0, action_label)
    analytics['recent_actions'] = recent_actions[:10]

    users.update_one({'_id': ObjectId(user_id)}, {'$set': {'analytics': analytics}})

def build_analytics_summary(analytics):
    xp = int(analytics.get('xp', 0))
    level_size = 120
    return {
        'xp': xp,
        'level': (xp // level_size) + 1,
        'xp_into_level': xp % level_size,
        'xp_per_level': level_size,
        'uploads': int(analytics.get('uploads', 0)),
        'links': int(analytics.get('links', 0)),
        'records': int(analytics.get('records', 0)),
        'streak': int(analytics.get('streak', 1)),
        'last_active_date': analytics.get('last_active_date'),
        'recent_actions': analytics.get('recent_actions', [])[:10]
    }

def build_activity_history(user_id, days=14):
    transcripts = get_transcripts_collection()
    start_date = date.today() - timedelta(days=days - 1)
    start_epoch = datetime.combine(start_date, datetime.min.time()).timestamp()

    documents = transcripts.find(
        {
            'user_id': user_id,
            'created_at': {'$gte': start_epoch}
        },
        {
            'type': 1,
            'created_at': 1
        }
    )

    buckets = {}
    for offset in range(days):
        day_value = start_date + timedelta(days=offset)
        day_key = day_value.isoformat()
        buckets[day_key] = {
            'date': day_key,
            'uploads': 0,
            'links': 0,
            'records': 0,
            'xp': 0
        }

    for doc in documents:
        created_at = doc.get('created_at')
        transcript_type = doc.get('type')
        if created_at is None:
            continue

        day_key = datetime.fromtimestamp(created_at).date().isoformat()
        if day_key not in buckets:
            continue

        if transcript_type == 'video':
            buckets[day_key]['uploads'] += 1
            buckets[day_key]['xp'] += XP_PER_ACTION['upload']
        elif transcript_type == 'audio':
            buckets[day_key]['records'] += 1
            buckets[day_key]['xp'] += XP_PER_ACTION['record']
        elif transcript_type in ['youtube', 'text']:
            buckets[day_key]['links'] += 1
            buckets[day_key]['xp'] += XP_PER_ACTION['link']

    return [buckets[key] for key in sorted(buckets.keys())]

def _choose_gemini_model(client: genai.Client) -> str:
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

def _get_transcript_plain_text(transcript_doc: Dict[str, Any]) -> str:
    if not transcript_doc:
        return ''

    raw_transcript = transcript_doc.get('raw_transcript')
    if isinstance(raw_transcript, str) and raw_transcript.strip():
        text = raw_transcript
    else:
        content = transcript_doc.get('content')
        if not isinstance(content, str):
            return ''
        text = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
        text = re.sub(r'</p\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    return text.strip()

def _build_semantic_fallback_diagram(transcript_text: str) -> str:
    raw = re.sub(r'<br\s*/?>', '\n', transcript_text or '', flags=re.IGNORECASE)
    raw = raw.replace('\r', '\n')
    if not raw.strip():
        return "flowchart TD\nA[No content found in transcript]"

    candidates = [segment.strip() for segment in re.split(r'\n+', raw) if segment and segment.strip()]
    if not candidates:
        normalized = re.sub(r'\s+', ' ', raw).strip()
        candidates = [segment.strip() for segment in re.split(r'[.!?]+', normalized) if segment and segment.strip()]

    phrases = []
    seen = set()
    for segment in candidates:
        cleaned = re.sub(r'\s+', ' ', segment).strip(' ,;:-')
        cleaned = re.sub(r'[\[\]{}()<>`"\\|]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        words = cleaned.split()
        if len(words) < 3:
            continue
        phrase = ' '.join(words[:8])
        phrase = phrase[:42].strip(' ,;:-')
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        phrases.append(phrase)
        if len(phrases) >= 7:
            break

    if not phrases:
        flat_words = re.sub(r'\s+', ' ', raw).strip().split()
        if flat_words:
            phrases = [' '.join(flat_words[:8])]
        else:
            phrases = ['No content found in transcript']

    lines = ['flowchart TD']
    lines.append(f"A[{phrases[0]}]")

    previous = 'A'
    for index, phrase in enumerate(phrases[1:], start=1):
        node_id = f"N{index}"
        lines.append(f"{node_id}[{phrase}]")
        lines.append(f"{previous} --> {node_id}")
        previous = node_id

    return '\n'.join(lines)

def _build_mindmap_fallback_diagram(transcript_text: str) -> str:
    raw = re.sub(r'<br\s*/?>', '\n', transcript_text or '', flags=re.IGNORECASE)
    raw = raw.replace('\r', '\n')
    cleaned = re.sub(r'\s+', ' ', raw).strip()
    if not cleaned:
        return "mindmap\n  root((Transcript))\n    Key Idea"

    chunks = [segment.strip() for segment in re.split(r'[.!?\n]+', cleaned) if segment and segment.strip()]
    phrases = []
    seen = set()
    for chunk in chunks:
        text = re.sub(r'[\[\]{}()<>`"\\|]', ' ', chunk)
        text = re.sub(r'\s+', ' ', text).strip(' ,;:-')
        words = text.split()
        if len(words) < 3:
            continue
        phrase = ' '.join(words[:8])[:46].strip(' ,;:-')
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        phrases.append(phrase)
        if len(phrases) >= 8:
            break

    if not phrases:
        phrases = ['Transcript Overview']

    lines = ["mindmap", f"  root(({phrases[0]}))"]
    for phrase in phrases[1:]:
        lines.append(f"    {phrase}")
    return '\n'.join(lines)

def _build_topic_template_diagram(topic_text: str, diagram_type: str = 'flowchart') -> str:
    topic = (topic_text or '').strip()
    topic_lower = topic.lower()

    if not topic:
        topic = 'Learning Workflow'
        topic_lower = topic.lower()

    if 'reinforcement learning' in topic_lower or topic_lower.startswith('rl'):
        if diagram_type == 'mindmap':
            return "\n".join([
                "mindmap",
                "  root((Reinforcement Learning))",
                "    Environment",
                "      State",
                "      Reward",
                "    Agent",
                "      Policy",
                "      Action Selection",
                "    Learning Loop",
                "      Experience Collection",
                "      Policy Update",
                "    Evaluation",
                "      Performance Metrics",
            ])

        orientation = 'LR' if diagram_type == 'concept-map' else 'TD'
        return "\n".join([
            f"flowchart {orientation}",
            "A[Define Environment]",
            "B[Initialize Agent Policy]",
            "C[Observe Current State]",
            "D[Select Action from Policy]",
            "E[Execute Action in Environment]",
            "F[Receive Reward and Next State]",
            "G[Store Experience]",
            "H[Update Policy or Value Function]",
            "I[Repeat Until Convergence]",
            "J[Evaluate Trained Agent]",
            "A --> B",
            "B --> C",
            "C --> D",
            "D --> E",
            "E --> F",
            "F --> G",
            "G --> H",
            "H --> I",
            "I --> J",
        ])

    if diagram_type == 'mindmap':
        return "\n".join([
            "mindmap",
            f"  root(({topic[:45]}))",
            "    Core Concepts",
            "    Key Components",
            "    Process Steps",
            "    Practical Applications",
            "    Evaluation Metrics",
        ])

    orientation = 'LR' if diagram_type == 'concept-map' else 'TD'
    return "\n".join([
        f"flowchart {orientation}",
        f"A[Understand {topic[:35]}]",
        "B[Identify Core Components]",
        "C[Design Step-by-Step Process]",
        "D[Implement and Validate]",
        "E[Evaluate Outcomes]",
        "F[Iterate and Improve]",
        "A --> B",
        "B --> C",
        "C --> D",
        "D --> E",
        "E --> F",
    ])

def _extract_mermaid(text: str, transcript_text: str = '', diagram_type: str = 'flowchart') -> str:
    def clean_label(label: str) -> str:
        value = (label or '').replace('\n', ' ').replace('\r', ' ')
        value = re.sub(r'[\[\]{}()<>`"\\|]', ' ', value)
        value = re.sub(r'\s+', ' ', value).strip(' .-:;')
        return (value or 'Concept')[:60]

    def fallback_diagram() -> str:
        return ''

    fenced = re.search(r"```(?:mermaid)?\s*([\s\S]*?)```", text or "", re.IGNORECASE)
    if fenced:
        code = fenced.group(1).strip()
    else:
        code = (text or "").strip()

    if not code:
        return fallback_diagram()

    code = re.sub(r'\x1b\[[0-9;]*m', '', code)
    if diagram_type == 'mindmap':
        if code.strip().startswith('mindmap'):
            return code.strip()
        return ''

    code = code.replace('graph TD', 'flowchart TD').replace('graph LR', 'flowchart LR')

    body = re.sub(r'^(flowchart\s+(?:TD|LR)|graph\s+(?:TD|LR))\s*', '', code, flags=re.IGNORECASE).strip()
    statements = [segment.strip() for segment in re.split(r'[\n;]+', body) if segment.strip()]

    node_order = []
    node_labels: Dict[str, str] = {}
    edges = []

    edge_with_labels = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)\s*(?:\[(.*?)\])?\s*-->\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:\[(.*?)\])?$')
    edge_plain = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)\s*-->\s*([A-Za-z][A-Za-z0-9_]*)$')
    node_only = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)\s*\[(.*?)\]$')

    def ensure_node(node_id: str, label: str = ''):
        if node_id not in node_order:
            node_order.append(node_id)
        if label:
            node_labels[node_id] = clean_label(label)

    for statement in statements:
        cleaned_statement = re.sub(r'\s+', ' ', statement).strip()

        match_edge_label = edge_with_labels.match(cleaned_statement)
        if match_edge_label:
            source_id, source_label, target_id, target_label = match_edge_label.groups()
            ensure_node(source_id, source_label or source_id)
            ensure_node(target_id, target_label or target_id)
            edges.append((source_id, target_id))
            continue

        match_edge = edge_plain.match(cleaned_statement)
        if match_edge:
            source_id, target_id = match_edge.groups()
            ensure_node(source_id)
            ensure_node(target_id)
            edges.append((source_id, target_id))
            continue

        match_node = node_only.match(cleaned_statement)
        if match_node:
            node_id, node_label = match_node.groups()
            ensure_node(node_id, node_label or node_id)

    if not edges:
        return ''

    if diagram_type == 'flowchart':
        has_missing_labels = False
        for node_id in node_order:
            label = node_labels.get(node_id)
            if not label or label.lower() == node_id.lower():
                has_missing_labels = True
                break

        if has_missing_labels:
            return ''

    lines = ['flowchart TD']
    for index, node_id in enumerate(node_order[:20], start=1):
        label = node_labels.get(node_id)
        if not label or label.lower() in {node_id.lower(), node_id.lower().strip()}:
            return ''
        lines.append(f"{node_id}[{label}]")
    for source_id, target_id in edges[:30]:
        lines.append(f"{source_id} --> {target_id}")

    return '\n'.join(lines)

def generate_diagram_mermaid(transcript_text: str, diagram_type: str = 'flowchart', topic_text: str = '') -> str:
    diagram_type = (diagram_type or 'flowchart').strip().lower()
    if diagram_type not in ['flowchart', 'mindmap', 'concept-map']:
        diagram_type = 'flowchart'

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Gemini API key is missing. Set GOOGLE_API_KEY or GEMINI_API_KEY.")

    topic_text = (topic_text or '').strip()
    transcript_text = (transcript_text or '').strip()
    context_text = topic_text if topic_text else transcript_text[:12000]

    if not context_text:
        raise RuntimeError("No context provided. Add topic or transcript text.")

    client = genai.Client(api_key=api_key)
    model_name = _choose_gemini_model(client)

    def parse_json_payload(raw_text: str):
        if not raw_text:
            return None
        cleaned = raw_text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                return None
        return None

    def build_mermaid_from_json(payload):
        if not isinstance(payload, dict):
            return ''

        if diagram_type == 'mindmap':
            root = str(payload.get('root') or '').strip()
            branches = payload.get('branches') or []
            if not root or not isinstance(branches, list) or not branches:
                return ''

            lines = ['mindmap', f"  root(({root[:50]}))"]
            for item in branches[:10]:
                if isinstance(item, dict):
                    label = str(item.get('label') or '').strip()
                    children = item.get('children') if isinstance(item.get('children'), list) else []
                    if label:
                        lines.append(f"    {label[:50]}")
                        for child in children[:4]:
                            child_label = str(child).strip()
                            if child_label:
                                lines.append(f"      {child_label[:50]}")
                else:
                    label = str(item).strip()
                    if label:
                        lines.append(f"    {label[:50]}")
            return '\n'.join(lines)

        nodes = payload.get('nodes') or []
        edges = payload.get('edges') or []
        if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes or not edges:
            return ''

        node_map = {}
        for item in nodes[:16]:
            if not isinstance(item, dict):
                continue
            node_id = re.sub(r'[^A-Za-z0-9_]', '', str(item.get('id') or '').strip())
            label = str(item.get('label') or '').strip()
            label = re.sub(r'[\[\]{}()<>`"\\|]', ' ', label)
            label = re.sub(r'\s+', ' ', label).strip()[:60]
            if node_id and label:
                node_map[node_id] = label

        if len(node_map) < 3:
            return ''

        orientation = 'LR' if diagram_type == 'concept-map' else 'TD'
        lines = [f'flowchart {orientation}']
        for node_id, node_label in node_map.items():
            lines.append(f"{node_id}[{node_label}]")

        edge_count = 0
        for edge in edges[:24]:
            if not isinstance(edge, dict):
                continue
            src = re.sub(r'[^A-Za-z0-9_]', '', str(edge.get('from') or '').strip())
            dst = re.sub(r'[^A-Za-z0-9_]', '', str(edge.get('to') or '').strip())
            if src in node_map and dst in node_map:
                lines.append(f"{src} --> {dst}")
                edge_count += 1

        if edge_count < 2:
            return ''
        return '\n'.join(lines)

    if diagram_type == 'mindmap':
        schema_text = """
Return STRICT JSON only:
{
  "root": "main topic",
  "branches": [
    {"label": "branch one", "children": ["detail 1", "detail 2"]},
    {"label": "branch two", "children": ["detail 1", "detail 2"]}
  ]
}
Rules: 5-8 branches, short labels, no markdown.
"""
    else:
        schema_text = """
Return STRICT JSON only:
{
  "nodes": [
    {"id": "A", "label": "Start"},
    {"id": "B", "label": "Next step"}
  ],
  "edges": [
    {"from": "A", "to": "B"}
  ]
}
Rules: 7-12 nodes, 7-14 edges, one connected diagram, no markdown.
"""

    prompts = [
        f"Create a {diagram_type} diagram from this context. {schema_text}\n\nContext:\n{context_text}",
        f"Retry with higher quality. Ensure domain-specific labels and clear structure. {schema_text}\n\nContext:\n{context_text}",
        f"Final retry: output VALID JSON only, nothing else. {schema_text}\n\nContext:\n{context_text}",
    ]

    for prompt in prompts:
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            response_text = getattr(response, "text", "") or str(response)
            payload = parse_json_payload(response_text)
            mermaid = build_mermaid_from_json(payload)
            if mermaid:
                return mermaid
        except Exception:
            continue

    def build_mermaid_from_lines(raw_text: str):
        if not raw_text:
            return ''
        lines = []
        for line in (raw_text or '').splitlines():
            cleaned = re.sub(r'^\s*[-*\d\.\)\(\s]+', '', line).strip()
            cleaned = re.sub(r'[\[\]{}()<>`"\\|]', ' ', cleaned)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if cleaned:
                lines.append(cleaned[:60])

        unique_lines = []
        seen = set()
        for item in lines:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_lines.append(item)
            if len(unique_lines) >= 10:
                break

        if len(unique_lines) < 3:
            return ''

        if diagram_type == 'mindmap':
            output = ['mindmap', f"  root(({unique_lines[0]}))"]
            for branch in unique_lines[1:]:
                output.append(f"    {branch}")
            return '\n'.join(output)

        orientation = 'LR' if diagram_type == 'concept-map' else 'TD'
        output = [f'flowchart {orientation}']
        output.append(f"A[{unique_lines[0]}]")
        prev = 'A'
        for index, label in enumerate(unique_lines[1:], start=1):
            node_id = f"N{index}"
            output.append(f"{node_id}[{label}]")
            output.append(f"{prev} --> {node_id}")
            prev = node_id
        return '\n'.join(output)

    line_prompt = f"""
Generate a concise list of diagram points for this {diagram_type}.
Return only plain lines, one point per line, 6-10 lines, no numbering.
Context:
{context_text}
"""

    try:
        line_response = client.models.generate_content(model=model_name, contents=line_prompt)
        line_text = getattr(line_response, "text", "") or str(line_response)
        mermaid_from_lines = build_mermaid_from_lines(line_text)
        if mermaid_from_lines:
            return mermaid_from_lines
    except Exception:
        pass

    return _build_topic_template_diagram(context_text, diagram_type=diagram_type)

def get_snapshots_collection():
    return get_db()['snapshots']

def _ensure_snapshot_dirs():
    snapshots_dir = os.path.join(UPLOADS_DIR, 'snapshots')
    tmp_dir = os.path.join(UPLOADS_DIR, 'tmp_snapshots')
    os.makedirs(snapshots_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    return snapshots_dir, tmp_dir

def _format_timestamp(seconds: float) -> str:
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def _analyze_snapshot_image(image_path: str):
    labels = set()
    extracted_text = ''

    try:
        import pytesseract
        from PIL import Image
        extracted_text = (pytesseract.image_to_string(Image.open(image_path)) or '').strip()
    except Exception:
        extracted_text = ''

    try:
        import cv2
        frame = cv2.imread(image_path)
        if frame is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 75, 180)
            lines = cv2.HoughLinesP(edges, 1, 3.14 / 180, threshold=90, minLineLength=45, maxLineGap=10)
            line_count = len(lines) if lines is not None else 0

            if line_count >= 85:
                labels.add('Flowchart')
            elif line_count >= 45:
                labels.add('Diagram')

            h, w = gray.shape[:2]
            if h > 0 and w > 0:
                bright = (gray > 235).sum() / float(h * w)
                if bright > 0.66:
                    labels.add('Figure')
    except Exception:
        pass

    lower = extracted_text.lower()
    if any(k in lower for k in ['chart', 'bar', 'line graph', 'x-axis', 'y-axis', 'pie']):
        labels.add('Chart')
    if any(k in lower for k in ['flowchart', 'workflow', 'decision', 'process']):
        labels.add('Flowchart')
    if any(k in lower for k in ['figure', 'fig.', 'diagram', 'architecture', 'block']):
        labels.add('Diagram')

    if not labels:
        labels.add('Figure')

    return sorted(labels), extracted_text[:3000]

def _extract_snapshots_from_video(video_path: str, source_type: str, interval_seconds: int = 1, max_snapshots: int = 120):
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError("OpenCV is required for video snapshot extraction. Install opencv-python.") from exc

    snapshots_dir, _ = _ensure_snapshot_dirs()
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise RuntimeError('Unable to open video source for Snapshot Extractor')

    fps = capture.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 25.0

    max_snapshots = max(1, min(int(max_snapshots or 120), 400))
    interval_frames = max(1, int(interval_seconds * fps))
    frame_index = 0
    extracted = []

    while len(extracted) < max_snapshots:
        success, frame = capture.read()
        if not success:
            break

        if frame_index % interval_frames == 0:
            file_name = f"snapshot_{uuid.uuid4().hex[:10]}_{len(extracted)}.jpg"
            image_path = os.path.join(snapshots_dir, file_name)
            cv2.imwrite(image_path, frame)

            labels, ocr = _analyze_snapshot_image(image_path)
            extracted.append({
                'source_type': source_type,
                'reference': _format_timestamp(frame_index / fps),
                'reference_seconds': round(frame_index / fps, 2),
                'image_path': image_path,
                'labels': labels,
                'ocr_text': ocr
            })

        frame_index += 1

    capture.release()
    return extracted

def _extract_snapshots_from_pdf(file_path: str, max_pages: int = 12):
    try:
        import fitz
    except Exception as exc:
        raise RuntimeError('PyMuPDF is required for PDF snapshot extraction. Install pymupdf.') from exc

    snapshots_dir, _ = _ensure_snapshot_dirs()
    extracted = []
    doc = fitz.open(file_path)

    for page_index, page in enumerate(doc[:max_pages], start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        file_name = f"snapshot_{uuid.uuid4().hex[:10]}_p{page_index}.png"
        image_path = os.path.join(snapshots_dir, file_name)
        pix.save(image_path)

        labels, ocr = _analyze_snapshot_image(image_path)
        extracted.append({
            'source_type': 'pdf',
            'reference': f"Page {page_index}",
            'reference_seconds': None,
            'image_path': image_path,
            'labels': labels,
            'ocr_text': ocr
        })

    doc.close()
    return extracted

def _extract_snapshots_from_image(file_path: str, source_type: str = 'book'):
    snapshots_dir, _ = _ensure_snapshot_dirs()
    extension = os.path.splitext(file_path)[1].lower() or '.png'
    file_name = f"snapshot_{uuid.uuid4().hex[:10]}{extension}"
    image_path = os.path.join(snapshots_dir, file_name)
    shutil.copy(file_path, image_path)

    labels, ocr = _analyze_snapshot_image(image_path)
    return [{
        'source_type': source_type,
        'reference': 'Uploaded image',
        'reference_seconds': None,
        'image_path': image_path,
        'labels': labels,
        'ocr_text': ocr
    }]

def _create_text_snapshot_image(text: str, title: str) -> str:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        raise RuntimeError('Pillow is required for text snapshot generation') from exc

    snapshots_dir, _ = _ensure_snapshot_dirs()
    file_name = f"snapshot_{uuid.uuid4().hex[:10]}.png"
    output_path = os.path.join(snapshots_dir, file_name)

    image = Image.new('RGB', (1280, 720), color=(31, 41, 55))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1280, 90), fill=(17, 24, 39))
    draw.text((24, 30), title[:90], fill=(255, 255, 255))

    content = (text or '').replace('\r', '\n')
    lines = []
    for paragraph in content.split('\n'):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > 90:
            lines.append(paragraph[:90])
            paragraph = paragraph[90:]
        lines.append(paragraph)

    y = 120
    for line in lines[:24]:
        draw.text((24, y), line, fill=(209, 213, 219))
        y += 24

    image.save(output_path)
    return output_path

def _extract_snapshots_from_text(text_content: str, chunk_size: int = 1400, max_snapshots: int = 24):
    cleaned = re.sub(r'\s+', ' ', (text_content or '')).strip()
    if not cleaned:
        cleaned = 'No textual visual content was available.'

    chunks = []
    start = 0
    while start < len(cleaned) and len(chunks) < max_snapshots:
        chunks.append(cleaned[start:start + chunk_size])
        start += chunk_size

    extracted = []
    for index, chunk in enumerate(chunks, start=1):
        image_path = _create_text_snapshot_image(chunk, f"Text Snapshot {index}")
        labels, ocr = _analyze_snapshot_image(image_path)
        extracted.append({
            'source_type': 'text',
            'reference': f"Section {index}",
            'reference_seconds': None,
            'image_path': image_path,
            'labels': sorted(set(labels + ['Diagram']))[:5],
            'ocr_text': ocr or chunk[:2800]
        })

    return extracted

def _download_youtube_video(url: str) -> str:
    try:
        import yt_dlp
    except Exception as exc:
        raise RuntimeError('yt-dlp is required for YouTube Snapshot Extractor support. Install yt-dlp.') from exc

    _, tmp_dir = _ensure_snapshot_dirs()
    download_profiles = [
        {
            'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
            'merge_output_format': 'mp4'
        },
        {
            'format': 'best[ext=mp4][height<=480]/best[height<=480]/best',
            'merge_output_format': 'mp4'
        },
        {
            'format': 'worst[ext=mp4]/worst',
            'merge_output_format': 'mp4'
        }
    ]

    last_error = None
    for profile in download_profiles:
        out_tmpl = os.path.join(tmp_dir, f"yt_{uuid.uuid4().hex}.%(ext)s")
        ydl_opts: Any = {
            'outtmpl': out_tmpl,
            'quiet': True,
            'noplaylist': True,
            'retries': 6,
            'fragment_retries': 6,
            'socket_timeout': 60,
            'geo_bypass': True,
            **profile,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                info = ydl.extract_info(url, download=True)
                downloaded = ydl.prepare_filename(info)
                base, ext = os.path.splitext(downloaded)
                if ext.lower() != '.mp4' and os.path.exists(base + '.mp4'):
                    return base + '.mp4'
                if os.path.exists(downloaded):
                    return downloaded
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(f"YouTube download failed after retries: {last_error}")

def _extract_snapshots_from_youtube_storyboard(url: str, max_snapshots: int = 60):
    try:
        import yt_dlp
    except Exception as exc:
        raise RuntimeError('yt-dlp is required for YouTube storyboard extraction.') from exc

    try:
        import urllib.request
    except Exception as exc:
        raise RuntimeError('urllib is required for YouTube storyboard extraction.') from exc

    snapshots_dir, _ = _ensure_snapshot_dirs()
    max_snapshots = max(1, min(int(max_snapshots or 60), 200))

    ydl_opts: Any = {
        'quiet': True,
        'noplaylist': True,
        'skip_download': True,
        'extract_flat': False,
        'socket_timeout': 30,
        'geo_bypass': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(url, download=False)

    thumbnails = info.get('thumbnails') if isinstance(info, dict) else None
    if not thumbnails:
        return []

    valid_urls = []
    for thumb in thumbnails:
        thumb_url = (thumb or {}).get('url')
        if thumb_url and isinstance(thumb_url, str):
            valid_urls.append((thumb_url, thumb.get('id') or thumb.get('preference') or 'thumb'))

    if not valid_urls:
        return []

    total = len(valid_urls)
    target = min(max_snapshots, total)
    if target <= 0:
        return []

    if target == total:
        selected = valid_urls
    else:
        step = max(1, total // target)
        selected = [valid_urls[index] for index in range(0, total, step)][:target]

    extracted = []
    for index, (thumb_url, marker) in enumerate(selected, start=1):
        file_name = f"snapshot_{uuid.uuid4().hex[:10]}_yt_{index}.jpg"
        image_path = os.path.join(snapshots_dir, file_name)

        try:
            with urllib.request.urlopen(thumb_url, timeout=20) as response:
                content = response.read()
            with open(image_path, 'wb') as handle:
                handle.write(content)
        except Exception:
            continue

        labels, ocr = _analyze_snapshot_image(image_path)
        extracted.append({
            'source_type': 'youtube',
            'reference': f"Storyboard {marker}",
            'reference_seconds': None,
            'image_path': image_path,
            'labels': labels,
            'ocr_text': ocr
        })

    return extracted

def _extract_snapshots_from_source(file_path: str, source_type: str = '', interval_seconds: int = 1, max_snapshots: int = 120):
    extension = os.path.splitext(file_path)[1].lower()
    source_type = (source_type or '').lower()

    if extension in ['.mp4', '.avi', '.mkv', '.mov', '.flv', '.webm']:
        resolved_type = source_type if source_type in ['video', 'youtube'] else 'video'
        return _extract_snapshots_from_video(
            file_path,
            source_type=resolved_type,
            interval_seconds=interval_seconds,
            max_snapshots=max_snapshots
        )
    if extension == '.pdf':
        return _extract_snapshots_from_pdf(file_path)
    if extension in ['.png', '.jpg', '.jpeg', '.bmp', '.webp']:
        return _extract_snapshots_from_image(file_path, source_type='book')

    if source_type == 'youtube':
        return _extract_snapshots_from_video(
            file_path,
            source_type='youtube',
            interval_seconds=interval_seconds,
            max_snapshots=max_snapshots
        )
    if source_type == 'text':
        return _extract_snapshots_from_text('')

    raise RuntimeError('Unsupported file type for Snapshot Extractor')

def _compute_snapshot_fingerprint(image_path: str, ocr_text: str = '', labels=None) -> str:
    visual_bits = ''

    try:
        from PIL import Image
        with Image.open(image_path) as image:
            gray = image.convert('L').resize((32, 32))
            visual_bits = hashlib.sha256(gray.tobytes()).hexdigest()  # type: ignore[arg-type]
    except Exception:
        visual_bits = ''

    text_key = re.sub(r'\s+', ' ', (ocr_text or '')[:800].lower()).strip()
    label_key = '|'.join(sorted([str(label).strip().lower() for label in (labels or []) if str(label).strip()]))

    if not visual_bits:
        try:
            md5 = hashlib.md5()
            with open(image_path, 'rb') as handle:
                md5.update(handle.read(2 * 1024 * 1024))
            visual_bits = md5.hexdigest()
        except Exception:
            visual_bits = str(uuid.uuid4())

    combined = f"{visual_bits}|{text_key}|{label_key}"
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()

def _snapshot_dedupe_key(snapshot_payload: Dict[str, Any]) -> str:
    fingerprint = (snapshot_payload.get('fingerprint') or '').strip()
    if fingerprint:
        return f"fp:{fingerprint}"

    source = str(snapshot_payload.get('source_type', '')).strip().lower()
    reference = str(snapshot_payload.get('reference', '')).strip().lower()
    text_key = re.sub(r'\s+', ' ', (snapshot_payload.get('ocr_text', '') or '')[:300].lower()).strip()
    labels = '|'.join(sorted([str(label).strip().lower() for label in (snapshot_payload.get('labels') or []) if str(label).strip()]))
    return f"src:{source}|ref:{reference}|txt:{text_key}|lbl:{labels}"

def _dedupe_serialized_snapshots(snapshot_items):
    seen = set()
    unique_items = []

    for item in snapshot_items or []:
        key = _snapshot_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    return unique_items

def _serialize_snapshot(snapshot):
    return {
        'snapshot_id': str(snapshot.get('_id')),
        'transcript_id': str(snapshot.get('transcript_id')) if snapshot.get('transcript_id') else None,
        'source_type': snapshot.get('source_type', ''),
        'reference': snapshot.get('reference', ''),
        'reference_seconds': snapshot.get('reference_seconds'),
        'labels': snapshot.get('labels', []),
        'ocr_text': snapshot.get('ocr_text', ''),
        'image_url': f"/snapshot/image/{snapshot.get('_id')}",
        'download_url': f"/snapshot/download/{snapshot.get('_id')}",
        'fingerprint': snapshot.get('fingerprint', '')
    }

# Route for chatbot page
@app.route('/chatbot')
def chatbot_page():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    return render_template('chatbot.html')

@app.route('/upload_chatbot_pdf', methods=['POST'])
def upload_chatbot_pdf():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in first'})
    
    file = request.files.get('file')
    if not file:
        return jsonify({'success': False, 'message': 'No file provided'})
    
    # Check file extension
    filename = file.filename
    if not filename or not (filename.lower().endswith('.pdf') or filename.lower().endswith('.txt')):
        return jsonify({'success': False, 'message': 'Please upload a PDF or TXT file'})
    
    try:
        # Check if API key is valid
        if not validate_api_key():
            return jsonify({'success': False, 'message': 'Google API key not configured. Please check your .env file.'})
            
        # Create uploads directory if it doesn't exist
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        
        file_path = os.path.join(UPLOADS_DIR, secure_filename(filename))
        file.save(file_path)
        
        # Load document based on file type
        if filename.lower().endswith('.pdf'):
            documents = load_documents(file_path)
        else:  # For TXT files
            documents = load_text_documents(file_path)
        
        if not documents:
            return jsonify({'success': False, 'message': 'Failed to extract content from the file'})
        
        # Process documents
        chunks = split_documents(documents)
        
        # Create vector store for this session
        vectorstore = create_faiss_vectorstore(chunks, index_name=_faiss_index_path(CHATBOT_INDEX_NAME))
        
        # Save as latest transcript for easy access
        try:
            with open(os.path.join(UPLOADS_DIR, 'latest_transcript.txt'), 'w', encoding='utf-8') as f:
                for doc in documents:
                    f.write(doc.page_content)
        except Exception as e:
            print(f"Error saving latest transcript: {e}")
        
        return jsonify({
            'success': True, 
            'message': 'Document uploaded and processed successfully. You can now start chatting!'
        })
        
    except Exception as e:
        print(f"Error in document upload: {e}")
        return jsonify({'success': False, 'message': f'Error processing document: {str(e)}'})

@app.route('/chat_with_document', methods=['POST'])
def chat_with_document():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Debug what data is coming in
    print(f"Request JSON: {request.json}")
    
    question = request.json.get('question')
    if not question:
        print(f"No question found in request: {request.json}")
        return jsonify({'error': 'No question provided'}), 400
        
    # Load the vectorstore for chatbot
    try:
        vectorstore = load_faiss_vectorstore(_faiss_index_path(CHATBOT_INDEX_NAME))
    except Exception as e:
        print(f"Error loading vectorstore: {e}")
        return jsonify({'error': 'No document has been uploaded yet'}), 404
        
    # Setup RAG pipeline and get response
    try:
        qa_chain = setup_rag_pipeline(vectorstore)
        response = qa_chain.invoke(question)
        
        return jsonify({
            'answer': response['result'] if isinstance(response, dict) else str(response)
        })
    except Exception as e:
        print(f"Error getting response: {e}")
        return jsonify({'error': f'Error processing question: {str(e)}'}), 500

@app.route('/chat_with_image', methods=['POST'])
def chat_with_image():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    temp_path = None
    try:
        question = (request.form.get('question') or '').strip()
        snapshot_id = (request.form.get('snapshot_id') or '').strip()
        image_file = request.files.get('image')
        upload_file = image_file if (image_file and image_file.filename) else None

        if not question:
            return jsonify({'error': 'No question provided'}), 400
        if not snapshot_id and not upload_file:
            return jsonify({'error': 'Upload an image or provide a snapshot ID'}), 400

        image_path = None
        image_source = 'uploaded image'

        if snapshot_id:
            snapshot = get_snapshots_collection().find_one({
                '_id': ObjectId(snapshot_id),
                'user_id': session['user_id']
            })
            if not snapshot:
                return jsonify({'error': 'Snapshot not found'}), 404
            image_path = snapshot.get('image_path')
            image_source = f"snapshot {snapshot_id}"
        else:
            _, tmp_dir = _ensure_snapshot_dirs()
            safe_name = secure_filename(upload_file.filename)
            temp_path = os.path.join(tmp_dir, f"chat_image_{uuid.uuid4().hex}_{safe_name}")
            upload_file.save(temp_path)
            image_path = temp_path

        if not image_path or not os.path.exists(image_path):
            return jsonify({'error': 'Image file not found'}), 404

        context_text = ''
        try:
            vectorstore = load_faiss_vectorstore(_faiss_index_path(CHATBOT_INDEX_NAME))
            qa_chain = setup_rag_pipeline(vectorstore)
            context_response = qa_chain.invoke(f"Provide key context for answering this question: {question}")
            if isinstance(context_response, dict):
                context_text = str(context_response.get('result') or '')[:3000]
            elif context_response is not None:
                context_text = str(context_response)[:3000]
            else:
                context_text = ''
        except Exception:
            context_text = ''

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            return jsonify({'error': 'Google API key not configured'}), 500

        client = genai.Client(api_key=api_key)
        model_name = _choose_gemini_model(client)

        try:
            uploaded_image = client.files.upload(file=image_path)
        except TypeError:
            uploaded_image = client.files.upload(file=image_path, config={"mime_type": "image/png"})

        prompt = f"""
You are an educational visual assistant.
Answer the user question by combining image understanding and document context (if provided).

Image source: {image_source}

Document context (optional):
{context_text}

Question:
{question}
"""

        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, uploaded_image],
        )

        answer = (getattr(response, 'text', '') or str(response)).strip()
        return jsonify({'answer': answer})
    except Exception as e:
        print(f"Error in chat_with_image: {e}")
        return jsonify({'error': f'Error processing image query: {str(e)}'}), 500
    finally:
        try:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            pass

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/health')
def health():
    status = {"status": "ok", "storage": DATA_DIR}
    try:
        get_db().command("ping")
        status["database"] = "ok"
    except Exception as exc:
        status["database"] = "unavailable"
        status["database_error"] = str(exc)
        return jsonify(status), 503
    return jsonify(status)

@app.route('/signup', methods=['POST'])
def signup():
    try:
        data = request.form
        email = data.get('email')
        password = data.get('password')
        
        users = get_users_collection()
        if users.find_one({'email': email}):
            return jsonify({'error': 'Email already exists'}), 400
        
        hashed_password = generate_password_hash(password)
        user_id = users.insert_one({
            'email': email,
            'password': hashed_password,
            'analytics': default_analytics()
        }).inserted_id
        
        session['user_id'] = str(user_id)
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error in signup: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.form
        email = data.get('email')
        password = data.get('password')
        
        users = get_users_collection()
        user = users.find_one({'email': email})
        if user and check_password_hash(user['password'], password):
            session['user_id'] = str(user['_id'])
            return jsonify({'success': True})
        
        return jsonify({'error': 'Invalid credentials'}), 401
    except Exception as e:
        print(f"Error in login: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    analytics = refresh_user_streak(session['user_id'])
    return render_template('dashboard.html', analytics=analytics)

@app.route('/api/analytics', methods=['GET'])
def analytics_api():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        user_id = session['user_id']
        analytics = refresh_user_streak(user_id)

        return jsonify({
            'success': True,
            'analytics': build_analytics_summary(analytics),
            'history_14d': build_activity_history(user_id, days=14)
        })
    except Exception as e:
        print(f"Error in analytics API: {e}")
        return jsonify({'error': 'Failed to load analytics'}), 500

@app.route('/transcript/<transcript_id>')
def transcript(transcript_id):
    try:
        # Get the transcript from the database
        transcripts = get_transcripts_collection()
        transcript_doc = transcripts.find_one({'_id': ObjectId(transcript_id)})
        
        if not transcript_doc:
            return "Transcript not found", 404
            
        # Create chat index for this transcript if it doesn't exist
        transcript_text = transcript_doc.get('content', '')
        
        # Get flashcards if they exist
        flashcards = transcript_doc.get('flashcards', [])
        
        return render_template('transcript.html', transcript=transcript_doc, flashcards=flashcards)
    except Exception as e:
        print(f"Error displaying transcript: {e}")
        return "Error loading transcript", 500
        
@app.route('/generate_flashcards', methods=['POST'])
def generate_flashcards_route():
    try:
        transcript_id = request.form.get('transcript_id')
        num_flashcards = int(request.form.get('num_flashcards', 5))

        # Fetch transcript text
        transcripts = get_transcripts_collection()
        transcript_doc = transcripts.find_one({'_id': ObjectId(transcript_id)})
        if not transcript_doc:
            return jsonify({'success': False, 'message': 'Transcript not found.'}), 404
        
        transcript_text = transcript_doc.get('content', '')
        
        # Generate flashcards
        flashcards = generate_flashcards(transcript_text, num_flashcards)

        # Save flashcards to MongoDB
        save_flashcards(ObjectId(transcript_id), flashcards)

        return jsonify({'success': True, 'message': 'Flashcards generated successfully.'})
    except Exception as e:
        print(f"Error generating flashcards: {e}")
        return jsonify({'success': False, 'message': f'Failed to generate flashcards: {str(e)}'}), 500

@app.route('/chat/<transcript_id>', methods=['POST'])
def chat(transcript_id):
    try:
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
            
        question = request.json.get('question')
        if not question:
            return jsonify({'error': 'No question provided'}), 400
        
        index_name = _faiss_index_path(f"transcript_{transcript_id}")
        
        # Check if vectorstore exists for this transcript
        try:
            # Try to load existing vectorstore
            vectorstore = load_faiss_vectorstore(index_name)
        except Exception as e:
            print(f"Creating new vectorstore for transcript {transcript_id}: {e}")
            # If not found, create it from the transcript
            try:
                # Get transcript content
                transcripts = get_transcripts_collection()
                transcript_doc = transcripts.find_one({'_id': ObjectId(transcript_id)})
                
                if not transcript_doc:
                    return jsonify({'error': 'Transcript not found'}), 404
                
                # Get raw transcript text
                transcript_text = _get_transcript_plain_text(transcript_doc)
                
                if not transcript_text:
                    return jsonify({'error': 'No content found in transcript'}), 404
                
                # Create document from transcript
                from langchain_core.documents import Document
                documents = [Document(page_content=transcript_text, metadata={"source": f"transcript_{transcript_id}"})]
                
                # Split into chunks
                chunks = split_documents(documents)
                
                # Create vectorstore
                vectorstore = create_faiss_vectorstore(chunks, index_name=index_name)
                
            except Exception as create_error:
                print(f"Error creating vectorstore: {create_error}")
                return jsonify({'error': 'Failed to process transcript for chatbot'}), 500
            
        # Setup RAG pipeline and get response
        qa_chain = setup_rag_pipeline(vectorstore)
        response = qa_chain.invoke(question)
        
        return jsonify({
            'answer': response['result'] if isinstance(response, dict) else str(response)
        })
    except Exception as e:
        # For API-style chat calls, always return JSON so the frontend
        # can display a clear error instead of a generic connection error.
        print(f"Error in chat endpoint for transcript {transcript_id}: {e}")
        return jsonify({'error': f'Error processing question: {str(e)}'}), 500

@app.route('/process_content', methods=['POST'])
def process_content():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        url = request.form.get('url', '').strip()
        text = request.form.get('text', '').strip()

        if not url and not text:
            return jsonify({'error': 'No content provided'}), 400

        if url:
            # Check if it's a YouTube URL
            if 'youtube.com' in url or 'youtu.be' in url:
                video_id = get_video_id(url)
                if not video_id:
                    return jsonify({'error': 'Invalid YouTube URL'}), 400
                
                # Get transcript from YouTube
                transcript = get_transcript_from_url(url)
                if transcript.startswith('Error'):
                    return jsonify({'error': transcript}), 500
                
                # Format transcript for display
                content = transcript.replace('\n', '<br>')
                
                # Store in MongoDB
                transcripts = get_transcripts_collection()
                transcript_id = transcripts.insert_one({
                    'user_id': session['user_id'],
                    'type': 'youtube',
                    'video_id': video_id,
                    'url': url,
                    'content': content,
                    'raw_transcript': transcript,
                    'created_at': time.time()
                }).inserted_id

                track_user_action(session['user_id'], 'link', 'Link processed successfully')
                return jsonify({
                    'success': True,
                    'transcript_id': str(transcript_id)
                })
            else:
                return jsonify({'error': 'Only YouTube URLs are supported at this time'}), 400
        else:
            # Process the text content
            content = text.replace('\n', '<br>')
            transcripts = get_transcripts_collection()
            transcript_id = transcripts.insert_one({
                'user_id': session['user_id'],
                'type': 'text',
                'content': content,
                'raw_transcript': text,
                'created_at': time.time()
            }).inserted_id

            track_user_action(session['user_id'], 'link', 'Text processed successfully')
            return jsonify({
                'success': True,
                'transcript_id': str(transcript_id)
            })

    except Exception as e:
        print(f"Error processing content: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file provided'}), 400

    # Get file type from form data
    file_type = request.form.get('type', 'video')
    filename = file.filename
    
    # Validate file type and extension
    if file_type == 'audio':
        if not filename or not filename.lower().endswith(('.mp3', '.wav', '.ogg', '.m4a', '.webm')):
            return jsonify({'error': 'Please upload a valid audio file (MP3, WAV, OGG, M4A, WEBM)'}), 400
    else:  # video type
        if not filename or not filename.lower().endswith(('.mp4', '.avi', '.mkv', '.mov', '.flv', '.webm')):
            return jsonify({'error': 'Please upload a valid video file (MP4, AVI, MKV, MOV, FLV, WEBM)'}), 400

    temp_file = None
    try:
        # Create temporary file to process the video
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1])
        file.save(temp_file.name)
        
        # Save the video file
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        video_filename = f"{timestamp}_{filename}"
        video_path = os.path.join(UPLOADS_DIR, 'videos', video_filename)
        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        file.seek(0)  # Reset file pointer
        file.save(video_path)

        # Process the file based on type
        if file_type == 'audio':
            # Use voice.py's speech_to_text for audio
            transcript = audio_to_text(temp_file.name)
        else:
            # Use vid.py's speech_to_text for video
            transcript = video_to_text(temp_file.name)

        # Format transcript for display
        content = transcript.replace('\n', '<br>')
        
        transcripts = get_transcripts_collection()
        transcript_id = transcripts.insert_one({
            'user_id': session['user_id'],
            'type': file_type,
            'filename': filename,
            'file_path': video_path,  # store as file_path for both audio and video
            'content': content,  # Store formatted content
            'raw_transcript': transcript,  # Store original for download
            'created_at': time.time()
        }).inserted_id

        if file_type == 'audio':
            track_user_action(session['user_id'], 'record', 'Audio upload completed')
        else:
            track_user_action(session['user_id'], 'upload', 'Video upload completed')

        return jsonify({
            'success': True,
            'transcript_id': str(transcript_id),
            'transcript': transcript
        })

    except Exception as e:
        print(f"Error in upload: {e}")
        return jsonify({'error': str(e)}), 500

    finally:
        # Clean up temporary file
        if temp_file:
            try:
                os.unlink(temp_file.name)
            except:
                pass

@app.route('/file/<transcript_id>')
def serve_file(transcript_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        transcripts = get_transcripts_collection()
        transcript_data = transcripts.find_one({'_id': ObjectId(transcript_id)})
        
        if not transcript_data or transcript_data['user_id'] != session['user_id']:
            return jsonify({'error': 'File not found'}), 404
            
        file_path = transcript_data.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404
            
        # Set mimetype based on file type
        if transcript_data['type'] == 'video':
            mimetype = 'video/mp4'
        elif transcript_data['type'] == 'audio':
            # Determine audio mimetype based on file extension
            ext = os.path.splitext(file_path)[1].lower()
            mimetypes = {
                '.mp3': 'audio/mpeg',
                '.wav': 'audio/wav',
                '.ogg': 'audio/ogg',
                '.m4a': 'audio/mp4',
                '.webm': 'audio/webm'
            }
            mimetype = mimetypes.get(ext, 'audio/mpeg')
        else:
            mimetype = 'application/octet-stream'
            
        return send_file(
            file_path,
            mimetype=mimetype
        )
        
    except Exception as e:
        print(f"Error serving video: {e}")
        return jsonify({'error': 'Error serving video'}), 500

@app.route('/download_transcript/<transcript_id>')
def download_transcript(transcript_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        transcripts = get_transcripts_collection()
        transcript_data = transcripts.find_one({'_id': ObjectId(transcript_id)})
        
        if not transcript_data or transcript_data['user_id'] != session['user_id']:
            return jsonify({'error': 'Transcript not found'}), 404
            
        # Get raw transcript text from MongoDB
        raw_transcript = transcript_data.get('raw_transcript')
        if not raw_transcript:
            return jsonify({'error': 'Transcript content not found'}), 404
            
        # Create a temporary file with the transcript content
        temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt')
        try:
            temp_file.write(raw_transcript)
            temp_file.close()
            
            return send_file(
                temp_file.name,
                as_attachment=True,
                download_name=f"transcript_{transcript_data['filename']}.txt"
            )
        finally:
            # Clean up temp file in a background thread after sending
            @after_this_request
            def cleanup(response):
                try:
                    os.unlink(temp_file.name)
                except:
                    pass
                return response
        
    except Exception as e:
        print(f"Error downloading transcript: {e}")
        return jsonify({'error': 'Error downloading transcript'}), 500

@app.route('/flashcards/<transcript_id>')
def view_flashcards(transcript_id):
    try:
        transcripts = get_transcripts_collection()
        transcript = transcripts.find_one({'_id': ObjectId(transcript_id)})
        flashcards = transcript.get('flashcards', [])
        return render_template('flashcards.html', flashcards=flashcards)
    except Exception as e:
        print(f"Error fetching flashcards: {e}")
        return "Failed to load flashcards", 500

@app.route('/generate_quiz', methods=['POST'])
def generate_quiz():
    try:
        transcript_id = request.form.get('transcript_id')
        num_questions = int(request.form.get('num_questions', 5))
        
        # Get selected question types
        question_types = []
        if request.form.get('include_mcq') == 'true':
            question_types.append('mcq')
        if request.form.get('include_tf') == 'true':
            question_types.append('true_false')
        if request.form.get('include_fb') == 'true':
            question_types.append('fill_blank')
        
        # Fetch transcript text
        transcript_text, transcript_doc = fetch_current_transcript(transcript_id)
        if not transcript_text:
            return jsonify({'success': False, 'message': 'Transcript not found.'}), 404
        
        # Generate quiz
        quiz_data = generate_quiz_with_gemini(transcript_text, num_questions, question_types)
        
        # Shuffle options for MCQs
        quiz_data = shuffle_options(quiz_data)
        
        # Save quiz to MongoDB
        quiz_record = save_quiz(transcript_id, quiz_data)

        return jsonify({
            'success': True, 
            'message': 'Quiz generated successfully.',
            'quiz': quiz_record
        })
    except Exception as e:
        print(f"Error generating quiz: {e}")
        return jsonify({'success': False, 'message': f'Failed to generate quiz: {str(e)}'}), 500

@app.route('/generate_diagram', methods=['POST'])
def generate_diagram():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        data = request.get_json(silent=True) or request.form
        transcript_id = data.get('transcript_id') if data else None
        diagram_type = (data.get('diagram_type') or 'flowchart') if data else 'flowchart'
        topic_text = (data.get('topic') or '').strip() if data else ''

        if not transcript_id:
            return jsonify({'success': False, 'message': 'Transcript ID is required'}), 400

        if not topic_text:
            return jsonify({'success': False, 'message': 'Topic/context is required for diagram generation.'}), 400

        transcripts = get_transcripts_collection()
        transcript_doc = transcripts.find_one({'_id': ObjectId(transcript_id)})
        if not transcript_doc:
            return jsonify({'success': False, 'message': 'Transcript not found'}), 404

        transcript_text = _get_transcript_plain_text(transcript_doc)
        if not transcript_text and not topic_text:
            return jsonify({'success': False, 'message': 'Transcript text is empty. Please regenerate transcript first.'}), 400

        mermaid_code = generate_diagram_mermaid(transcript_text, diagram_type=diagram_type, topic_text=topic_text)

        return jsonify({
            'success': True,
            'mermaid': mermaid_code
        })
    except Exception as e:
        print(f"Error generating diagram: {e}")
        return jsonify({'success': False, 'message': f'Failed to generate diagram: {str(e)}'}), 500

@app.route('/snapshot/extract', methods=['POST'])
def snapshot_extract():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    temp_paths = []
    try:
        transcript_id = request.form.get('transcript_id')
        transcript_doc = None

        if transcript_id:
            transcript_doc = get_transcripts_collection().find_one({'_id': ObjectId(transcript_id)})
            if not transcript_doc:
                return jsonify({'success': False, 'message': 'Transcript not found'}), 404
            if transcript_doc.get('user_id') != session['user_id']:
                return jsonify({'success': False, 'message': 'Forbidden'}), 403

        uploaded_file = request.files.get('file')
        interval_seconds = request.form.get('interval_seconds', '1')
        max_snapshots = request.form.get('max_snapshots', '120')
        replace_existing = str(request.form.get('replace_existing', 'false')).strip().lower() in ['1', 'true', 'yes', 'on']

        try:
            interval_seconds = max(1, min(int(interval_seconds), 10))
        except Exception:
            interval_seconds = 1

        try:
            max_snapshots = max(1, min(int(max_snapshots), 400))
        except Exception:
            max_snapshots = 120

        if replace_existing and transcript_id:
            get_snapshots_collection().delete_many({
                'user_id': session['user_id'],
                'transcript_id': ObjectId(transcript_id)
            })

        extracted_items = []
        warning_message = ''

        if uploaded_file and uploaded_file.filename:
            _, tmp_dir = _ensure_snapshot_dirs()
            safe_name = secure_filename(uploaded_file.filename)
            temp_path = os.path.join(tmp_dir, f"snapshot_src_{uuid.uuid4().hex}_{safe_name}")
            uploaded_file.save(temp_path)
            temp_paths.append(temp_path)
            extracted_items = _extract_snapshots_from_source(
                temp_path,
                interval_seconds=interval_seconds,
                max_snapshots=max_snapshots
            )
        elif transcript_doc:
            source_type = transcript_doc.get('type', '')
            if source_type == 'youtube' and transcript_doc.get('url'):
                try:
                    youtube_path = _download_youtube_video(transcript_doc.get('url'))
                    temp_paths.append(youtube_path)
                    extracted_items = _extract_snapshots_from_video(
                        youtube_path,
                        source_type='youtube',
                        interval_seconds=interval_seconds,
                        max_snapshots=max_snapshots
                    )
                except Exception as download_error:
                    ansi_clean_error = re.sub(r'\x1b\[[0-9;]*m', '', str(download_error))
                    try:
                        extracted_items = _extract_snapshots_from_youtube_storyboard(
                            transcript_doc.get('url'),
                            max_snapshots=min(max_snapshots, 120)
                        )
                    except Exception:
                        extracted_items = []

                    if extracted_items:
                        warning_message = f"Video stream download timed out. Used YouTube storyboard snapshots instead. Details: {ansi_clean_error[:160]}"
                    else:
                        transcript_text = (transcript_doc.get('raw_transcript') or transcript_doc.get('content') or '')
                        transcript_text = transcript_text.replace('<br>', '\n')
                        extracted_items = _extract_snapshots_from_text(
                            transcript_text,
                            max_snapshots=min(max_snapshots, 24)
                        )
                        warning_message = f"Video download timed out. Generated text snapshots instead. Details: {ansi_clean_error[:160]}"
            elif source_type == 'text':
                transcript_text = (transcript_doc.get('raw_transcript') or transcript_doc.get('content') or '')
                transcript_text = transcript_text.replace('<br>', '\n')
                extracted_items = _extract_snapshots_from_text(
                    transcript_text,
                    max_snapshots=max_snapshots
                )
            else:
                file_path = transcript_doc.get('file_path')
                if file_path and os.path.exists(file_path):
                    extracted_items = _extract_snapshots_from_source(
                        file_path,
                        source_type=source_type,
                        interval_seconds=interval_seconds,
                        max_snapshots=max_snapshots
                    )
                else:
                    transcript_text = (transcript_doc.get('raw_transcript') or transcript_doc.get('content') or '')
                    transcript_text = transcript_text.replace('<br>', '\n')
                    extracted_items = _extract_snapshots_from_text(
                        transcript_text,
                        max_snapshots=max_snapshots
                    )
        else:
            return jsonify({'success': False, 'message': 'Provide transcript_id or upload media'}), 400

        if not extracted_items:
            return jsonify({'success': False, 'message': 'No snapshots extracted'}), 400

        snapshots_collection = get_snapshots_collection()
        inserted = []
        batch_seen = set()
        for item in extracted_items:
            fingerprint = _compute_snapshot_fingerprint(
                item.get('image_path', ''),
                item.get('ocr_text', ''),
                item.get('labels', [])
            )

            if fingerprint in batch_seen:
                continue

            duplicate_filter = {
                'user_id': session['user_id'],
                'transcript_id': ObjectId(transcript_id) if transcript_id else None,
                'fingerprint': fingerprint
            }
            already_exists = snapshots_collection.find_one(duplicate_filter, {'_id': 1})
            if already_exists:
                continue

            record = {
                'user_id': session['user_id'],
                'transcript_id': ObjectId(transcript_id) if transcript_id else None,
                'source_type': item.get('source_type', 'unknown'),
                'reference': item.get('reference', ''),
                'reference_seconds': item.get('reference_seconds'),
                'image_path': item.get('image_path'),
                'labels': item.get('labels', []),
                'ocr_text': item.get('ocr_text', ''),
                'fingerprint': fingerprint,
                'created_at': time.time()
            }
            inserted_id = snapshots_collection.insert_one(record).inserted_id
            record['_id'] = inserted_id
            inserted.append(_serialize_snapshot(record))
            batch_seen.add(fingerprint)

        response_payload = {'success': True, 'count': len(inserted), 'snapshots': inserted}
        if warning_message:
            response_payload['message'] = warning_message
        return jsonify(response_payload)
    except Exception as e:
        print(f"Error extracting snapshots: {e}")
        return jsonify({'success': False, 'message': f'Failed to extract snapshots: {str(e)}'}), 500
    finally:
        for path in temp_paths:
            try:
                if path and os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass

@app.route('/snapshot/list/<transcript_id>', methods=['GET'])
def snapshot_list(transcript_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        records = get_snapshots_collection().find({
            'user_id': session['user_id'],
            'transcript_id': ObjectId(transcript_id)
        }).sort('created_at', -1)

        snapshots = [_serialize_snapshot(record) for record in records]
        snapshots = _dedupe_serialized_snapshots(snapshots)
        return jsonify({'success': True, 'snapshots': snapshots})
    except Exception as e:
        print(f"Error loading snapshots: {e}")
        return jsonify({'success': False, 'message': 'Failed to load snapshots'}), 500

@app.route('/snapshot/search', methods=['POST'])
def snapshot_search():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        data = request.get_json(silent=True) or request.form
        transcript_id = data.get('transcript_id') if data else None
        query = (data.get('query') or '').strip() if data else ''

        if not transcript_id:
            return jsonify({'success': False, 'message': 'Transcript ID is required'}), 400

        filters: Dict[str, Any] = {
            'user_id': session['user_id'],
            'transcript_id': ObjectId(transcript_id)
        }
        if query:
            filters['$or'] = [
                {'ocr_text': {'$regex': query, '$options': 'i'}},
                {'labels': {'$elemMatch': {'$regex': query, '$options': 'i'}}},
                {'reference': {'$regex': query, '$options': 'i'}},
                {'source_type': {'$regex': query, '$options': 'i'}}
            ]

        records = get_snapshots_collection().find(filters).sort('created_at', -1)
        snapshots = [_serialize_snapshot(record) for record in records]
        snapshots = _dedupe_serialized_snapshots(snapshots)
        return jsonify({'success': True, 'snapshots': snapshots})
    except Exception as e:
        print(f"Error searching snapshots: {e}")
        return jsonify({'success': False, 'message': 'Failed to search snapshots'}), 500

@app.route('/snapshot/image/<snapshot_id>', methods=['GET'])
def snapshot_image(snapshot_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        snapshot = get_snapshots_collection().find_one({
            '_id': ObjectId(snapshot_id),
            'user_id': session['user_id']
        })
        if not snapshot:
            return jsonify({'success': False, 'message': 'Snapshot not found'}), 404

        image_path = snapshot.get('image_path')
        if not image_path or not os.path.exists(image_path):
            return jsonify({'success': False, 'message': 'Image not found'}), 404

        extension = os.path.splitext(image_path)[1].lower()
        if extension == '.png':
            mime = 'image/png'
        elif extension == '.webp':
            mime = 'image/webp'
        else:
            mime = 'image/jpeg'
        return send_file(image_path, mimetype=mime)
    except Exception as e:
        print(f"Error serving snapshot image: {e}")
        return jsonify({'success': False, 'message': 'Failed to serve snapshot image'}), 500

@app.route('/snapshot/download/<snapshot_id>', methods=['GET'])
def snapshot_download(snapshot_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        snapshot = get_snapshots_collection().find_one({
            '_id': ObjectId(snapshot_id),
            'user_id': session['user_id']
        })
        if not snapshot:
            return jsonify({'success': False, 'message': 'Snapshot not found'}), 404

        image_path = snapshot.get('image_path')
        if not image_path or not os.path.exists(image_path):
            return jsonify({'success': False, 'message': 'Image not found'}), 404

        extension = os.path.splitext(image_path)[1].lower()
        if extension == '.png':
            mime = 'image/png'
        elif extension == '.webp':
            mime = 'image/webp'
        else:
            mime = 'image/jpeg'

        reference = str(snapshot.get('reference') or 'snapshot').replace(' ', '_').replace(':', '-')
        file_name = f"snapshot_{reference}_{snapshot_id}{extension or '.jpg'}"
        return send_file(image_path, mimetype=mime, as_attachment=True, download_name=file_name)
    except Exception as e:
        print(f"Error downloading snapshot image: {e}")
        return jsonify({'success': False, 'message': 'Failed to download snapshot image'}), 500

@app.route('/snapshot/qa', methods=['POST'])
def snapshot_qa():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        data = request.get_json(silent=True) or {}
        snapshot_id = data.get('snapshot_id')
        question = (data.get('question') or '').strip()

        if not snapshot_id or not question:
            return jsonify({'success': False, 'message': 'Snapshot ID and question are required'}), 400

        snapshot = get_snapshots_collection().find_one({
            '_id': ObjectId(snapshot_id),
            'user_id': session['user_id']
        })
        if not snapshot:
            return jsonify({'success': False, 'message': 'Snapshot not found'}), 404

        transcript_context = ''
        if snapshot.get('transcript_id'):
            transcript_doc = get_transcripts_collection().find_one({'_id': snapshot.get('transcript_id')})
            if transcript_doc:
                transcript_context = (transcript_doc.get('raw_transcript') or transcript_doc.get('content') or '')[:4000]

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            return jsonify({'success': False, 'message': 'Google API key not configured'}), 500

        image_path = snapshot.get('image_path')
        if not image_path or not os.path.exists(image_path):
            return jsonify({'success': False, 'message': 'Snapshot image not found'}), 404

        client = genai.Client(api_key=api_key)
        model_name = _choose_gemini_model(client)

        try:
            uploaded_image = client.files.upload(file=image_path)
        except TypeError:
            uploaded_image = client.files.upload(file=image_path, config={"mime_type": "image/png"})

        prompt = f"""
You are a visual study assistant.
Answer using the selected snapshot and the transcript context.

Snapshot metadata:
- Source type: {snapshot.get('source_type', 'unknown')}
- Reference: {snapshot.get('reference', 'N/A')}
- Labels: {', '.join(snapshot.get('labels', []))}

OCR text from snapshot:
{(snapshot.get('ocr_text') or '')[:2400]}

Transcript context:
{transcript_context}

Question:
{question}
"""

        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, uploaded_image]
        )

        answer = (getattr(response, 'text', '') or str(response)).strip()
        return jsonify({'success': True, 'answer': answer})
    except Exception as e:
        print(f"Error in snapshot QA: {e}")
        return jsonify({'success': False, 'message': f'Failed to answer question: {str(e)}'}), 500

@app.route('/quiz/answer', methods=['POST'])
def submit_answer():
    try:
        data = request.json
        transcript_id = data.get('transcript_id')
        question_id = data.get('question_id')
        user_answer = data.get('user_answer')
        
        # Fetch the quiz
        transcripts = get_transcripts_collection()
        transcript = transcripts.find_one({'_id': ObjectId(transcript_id)})
        quiz_data = transcript.get('quiz', {})
        
        if not quiz_data:
            return jsonify({'success': False, 'message': 'Quiz not found'}), 404
        
        # Grade the answer
        result = grade_answer(quiz_data, question_id, user_answer)
        if not result:
            return jsonify({'success': False, 'message': 'Question not found'}), 404
        
        # Update the stored responses for this question
        if 'responses' not in quiz_data:
            quiz_data['responses'] = []
        
        # Check if this question has already been answered
        existing_response = next((r for r in quiz_data.get('responses', []) 
                                if r.get('question_id') == question_id), None)
        
        if existing_response:
            # Update existing response
            existing_response.update({
                'user_answer': user_answer,
                'correct': result['correct'],
                'timestamp': time.time()
            })
        else:
            # Add new response
            quiz_data['responses'].append({
                'question_id': question_id,
                'user_answer': user_answer,
                'correct': result['correct'],
                'timestamp': time.time()
            })
        
        # Calculate current score
        correct_answers = sum(1 for r in quiz_data.get('responses', []) if r.get('correct'))
        total_answers = len(quiz_data.get('responses', []))
        total_questions = quiz_data.get('nQuestions', 0)
        
        # Update quiz data in MongoDB
        transcripts.update_one(
            {'_id': ObjectId(transcript_id)},
            {'$set': {'quiz': quiz_data}}
        )
        
        return jsonify({
            'success': True,
            'result': result,
            'score': {
                'correct': correct_answers,
                'answered': total_answers,
                'total': total_questions
            }
        })
        
    except Exception as e:
        print(f"Error submitting answer: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true',
    )
