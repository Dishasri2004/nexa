import os
import asyncio
from dotenv import load_dotenv
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain.prompts import PromptTemplate

# --- Config ---
load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("NEXA_DATA_DIR", BASE_DIR)
FAISS_ROOT = os.path.join(DATA_DIR, "faiss_indexes")
os.makedirs(FAISS_ROOT, exist_ok=True)


def _ensure_event_loop() -> None:
    """Ensure an asyncio event loop exists in this (Flask worker) thread.

    Some Google / LangChain integrations expect an event loop even when used
    synchronously. Flask runs requests in worker threads, so we create a loop
    on-demand if one is missing to avoid "There is no current event loop".
    """

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

def get_api_key():
    """Get the Google API key from environment variables"""
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

def validate_api_key():
    """Validate that API key is set and not the default placeholder"""
    api_key = get_api_key()
    if not api_key or api_key == "YOUR_NEW_API_KEY_HERE":
        return False
    return True

# --- PDF Loader ---
def load_documents(file_path):
    loader = PyPDFLoader(file_path)
    return loader.load()

# --- Text File Loader ---
def load_text_documents(file_path):
    """Load text documents from a .txt file"""
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
        
        # Create a document object similar to what PyPDFLoader returns
        from langchain_core.documents import Document
        return [Document(page_content=content, metadata={"source": file_path})]
    except Exception as e:
        print(f"Error loading text file: {e}")
        return []

# --- Text Splitter ---
def split_documents(documents):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200
    )
    return splitter.split_documents(documents)

# --- Embeddings & Vectorstore ---
def create_faiss_vectorstore(chunks, index_name="faiss_index_cyber"):
    try:
        _ensure_event_loop()
        # Use the stable Gemini embedding model
        embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        vectorstore = FAISS.from_documents(chunks, embeddings)
        vectorstore.save_local(index_name)
        return vectorstore
    except Exception as e:
        print(f"Error creating FAISS vectorstore: {e}")
        raise e

def load_faiss_vectorstore(index_name="faiss_index_cyber"):
    _ensure_event_loop()
    embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    return FAISS.load_local(index_name, embeddings, allow_dangerous_deserialization=True)

# --- RAG Pipeline ---
def setup_rag_pipeline(vectorstore):
    try:
        _ensure_event_loop()
        prompt_template = """
You are a helpful AI assistant specialized in analyzing documents and answering questions about their content.
Use the document context below to provide accurate, informative answers.

When answering questions:
- Focus on the main topics, key points, and insights from the document
- Include specific examples, quotes, or details mentioned in the document when relevant
- If asked for a summary, provide a concise overview of the main points
- If the answer requires information not in the document, clearly state that the information isn't available in this document
- Be conversational and engaging

Context from document:
{context}

Question: {question}
Answer:"""

        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["context", "question"]
        )

        # Use a current Gemini chat model; allow override via env var
        chat_model = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash")
        llm = ChatGoogleGenerativeAI(model=chat_model, temperature=0.5)

        return RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",
            retriever=vectorstore.as_retriever(search_kwargs={"k": 3}),
            return_source_documents=False,
            chain_type_kwargs={"prompt": prompt}
        )
    except Exception as e:
        print(f"Error setting up RAG pipeline: {e}")
        raise e
