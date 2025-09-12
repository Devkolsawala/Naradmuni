# app.py (with chat history support and proper database initialization)
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import requests
import os
import logging
from pydantic import BaseModel
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
import jwt
from datetime import datetime, timedelta
from databases import Database
import asyncpg

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment / config
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-please-change-in-production")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Try different database URL environment variables that might be set by Neon/Vercel
DATABASE_URL = (
    os.getenv("DATABASE_URL") or 
    os.getenv("POSTGRES_URL") or 
    os.getenv("DATABASE_URL_UNPOOLED") or
    os.getenv("POSTGRES_URL_NON_POOLING")
)

# Initialize database connection
database = None
if DATABASE_URL:
    database = Database(DATABASE_URL)

# Validate required environment variables
if not GOOGLE_CLIENT_ID:
    logger.error("GOOGLE_CLIENT_ID environment variable is not set!")

if not GROQ_API_KEY:
    logger.error("GROQ_API_KEY environment variable is not set!")

if not DATABASE_URL:
    logger.warning("DATABASE_URL environment variable is not set! Chat history will not be saved.")

app = FastAPI(title="Naradmuni Chatbot API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "https://accounts.google.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -------------------------
# Models
# -------------------------
class ChatRequest(BaseModel):
    message: str

class GoogleAuthRequest(BaseModel):
    credential: str

# -------------------------
# Helpers
# -------------------------
def create_session_jwt(email: str, name: str | None = None, picture: str | None = None, hours: int = 12):
    payload = {
        "sub": email,
        "name": name,
        "picture": picture,
        "exp": datetime.utcnow() + timedelta(hours=hours),
    }
    token = jwt.encode(payload, SESSION_SECRET, algorithm="HS256")
    return token

def get_authenticated_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.info("Session token expired")
        return None
    except Exception as e:
        logger.error(f"Session token decode error: {e}")
        return None

async def create_tables_if_not_exist():
    """Create the chat_history table if it doesn't exist"""
    if not database:
        return
    
    create_table_query = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id SERIAL PRIMARY KEY,
        user_email VARCHAR(255) NOT NULL,
        message_type VARCHAR(10) NOT NULL CHECK (message_type IN ('user', 'bot')),
        content TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    
    CREATE INDEX IF NOT EXISTS idx_chat_history_user_email ON chat_history(user_email);
    CREATE INDEX IF NOT EXISTS idx_chat_history_created_at ON chat_history(created_at);
    """
    
    try:
        await database.execute(create_table_query)
        logger.info("Database tables created/verified successfully")
    except Exception as e:
        logger.error(f"Error creating database tables: {e}")

# -------------------------
# Startup / Shutdown events
# -------------------------
@app.on_event("startup")
async def startup():
    if database:
        try:
            await database.connect()
            await create_tables_if_not_exist()
            logger.info("Database connected and initialized successfully")
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            # Don't fail the entire app if DB connection fails
    else:
        logger.warning("No database configured - running without chat history")

@app.on_event("shutdown")
async def shutdown():
    if database:
        try:
            await database.disconnect()
            logger.info("Database disconnected")
        except Exception as e:
            logger.error(f"Error disconnecting database: {e}")

# -------------------------
# Serve index.html frontend
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("index.html", "r", encoding="utf-8") as file:
            html_content = file.read()

        if GOOGLE_CLIENT_ID:
            html_content = html_content.replace(
                "YOUR_GOOGLE_CLIENT_ID_HERE",
                GOOGLE_CLIENT_ID,
            )
            html_content = html_content.replace(
                'const GOOGLE_CLIENT_ID = window.GOOGLE_CLIENT_ID || "YOUR_GOOGLE_CLIENT_ID_HERE";',
                f'const GOOGLE_CLIENT_ID = "{GOOGLE_CLIENT_ID}";',
            )

        return HTMLResponse(content=html_content, status_code=200)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Frontend file not found")

@app.get("/health")
async def health_check():
    db_status = "connected" if database else "not configured"
    if database:
        try:
            await database.fetch_one("SELECT 1")
            db_status = "connected"
        except:
            db_status = "connection failed"
    
    return {
        "status": "healthy",
        "google_auth_configured": bool(GOOGLE_CLIENT_ID),
        "groq_configured": bool(GROQ_API_KEY),
        "database_status": db_status,
    }

# -------------------------
# Google auth
# -------------------------
@app.post("/auth/google")
async def auth_google(payload: GoogleAuthRequest, request: Request):
    try:
        token = payload.credential
        if not GOOGLE_CLIENT_ID:
            raise HTTPException(status_code=500, detail="Google authentication not configured")

        idinfo = id_token.verify_oauth2_token(token, grequests.Request(), GOOGLE_CLIENT_ID)

        if idinfo.get("aud") != GOOGLE_CLIENT_ID:
            raise HTTPException(status_code=401, detail="Token audience mismatch")

        email = idinfo.get("email")
        name = idinfo.get("name")
        picture = idinfo.get("picture")

        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        logger.info(f"User authenticated: {email}")

        session_token = create_session_jwt(email=email, name=name, picture=picture, hours=12)
        secure_cookie = os.getenv("VERCEL") == "1" or request.url.scheme == "https"

        response = JSONResponse(
            {
                "success": True,
                "user": {"email": email, "name": name, "picture": picture},
            }
        )

        response.set_cookie(
            "session",
            session_token,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            max_age=12 * 3600,
        )

        return response

    except ValueError as e:
        logger.error(f"Google token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid Google token")
    except Exception as e:
        logger.error(f"Error in /auth/google: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")

@app.get("/auth/me")
async def auth_me(request: Request):
    user = get_authenticated_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return {"user": {"email": user.get("sub"), "name": user.get("name"), "picture": user.get("picture")}}

@app.post("/auth/logout")
async def auth_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("session", path="/")
    return response

# -------------------------
# Chat + history
# -------------------------
@app.post("/chat")
async def chat(chat_request: ChatRequest, request: Request):
    user = get_authenticated_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized - please login")

    try:
        user_message = chat_request.message.strip()
        if not user_message:
            raise HTTPException(status_code=400, detail="Message is required")
        if len(user_message) > 100:
            raise HTTPException(status_code=400, detail="Message too long (max 100 characters)")

        if not GROQ_API_KEY:
            raise HTTPException(status_code=500, detail="AI service not configured")

        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {
                    "role": "system",
                    "content": "You are Naradmuni, a wise mentor offering life guidance.",
                },
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.7,
            "max_tokens": 200,
        }

        user_email = user.get("sub", "unknown")
        logger.info(f"Sending request to Groq API for user {user_email} - message: {user_message[:60]}...")

        response = requests.post(GROQ_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        reply_data = response.json()
        reply = reply_data["choices"][0]["message"]["content"]

        # --- Save messages into DB (only if database is available) ---
        if database:
            try:
                await database.execute(
                    "INSERT INTO chat_history (user_email, message_type, content) VALUES (:email, 'user', :content)",
                    {"email": user_email, "content": user_message},
                )
                await database.execute(
                    "INSERT INTO chat_history (user_email, message_type, content) VALUES (:email, 'bot', :content)",
                    {"email": user_email, "content": reply},
                )
                logger.info(f"Chat history saved for user {user_email}")
            except Exception as e:
                logger.error(f"Error saving chat history: {e}")
                # Don't fail the entire request if history saving fails
        else:
            logger.info("Database not configured - chat history not saved")

        return {"reply": reply, "status": "success"}

    except requests.exceptions.RequestException as e:
        logger.error(f"Groq API error: {e}")
        if hasattr(e, "response") and e.response is not None:
            if e.response.status_code == 429:
                raise HTTPException(status_code=429, detail="AI service rate limit exceeded. Please try again later.")
            elif e.response.status_code >= 500:
                raise HTTPException(status_code=500, detail="AI service temporarily unavailable")
        raise HTTPException(status_code=500, detail="AI service temporarily unavailable")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in /chat: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/history")
async def get_history(request: Request):
    user = get_authenticated_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not database:
        return {"history": []}

    try:
        rows = await database.fetch_all(
            "SELECT message_type, content, created_at FROM chat_history WHERE user_email = :email ORDER BY created_at ASC LIMIT 100",
            {"email": user.get("sub")},
        )
        return {"history": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error fetching chat history: {e}")
        return {"history": []}

# -------------------------
# Run locally
# -------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)