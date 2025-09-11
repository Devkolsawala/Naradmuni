# app.py (updated)
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import requests
import os
import logging
from pydantic import BaseModel
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
import jwt
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment / config
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-please-change-in-production")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Validate required environment variables
if not GOOGLE_CLIENT_ID:
    logger.error("GOOGLE_CLIENT_ID environment variable is not set!")
    
if not GROQ_API_KEY:
    logger.error("GROQ_API_KEY environment variable is not set!")

app = FastAPI(title="Naradmuni Chatbot API", version="1.0.0")

# CORS middleware: IMPORTANT - when allow_credentials=True you must set explicit origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "https://accounts.google.com"],  # Add Google domain for auth
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str

class GoogleAuthRequest(BaseModel):
    credential: str

def create_session_jwt(email: str, name: str | None = None, picture: str | None = None, hours: int = 12):
    payload = {
        "sub": email,
        "name": name,
        "picture": picture,
        "exp": datetime.utcnow() + timedelta(hours=hours)
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

# -------------------------
# Serve index.html frontend with Google Client ID injected
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("index.html", "r", encoding="utf-8") as file:
            html_content = file.read()
        
        # Inject Google Client ID into the HTML
        if GOOGLE_CLIENT_ID:
            html_content = html_content.replace(
                "YOUR_GOOGLE_CLIENT_ID_HERE", 
                GOOGLE_CLIENT_ID
            )
            html_content = html_content.replace(
                'const GOOGLE_CLIENT_ID = window.GOOGLE_CLIENT_ID || "YOUR_GOOGLE_CLIENT_ID_HERE";',
                f'const GOOGLE_CLIENT_ID = "{GOOGLE_CLIENT_ID}";'
            )
        
        return HTMLResponse(content=html_content, status_code=200)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Frontend file not found")

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "google_auth_configured": bool(GOOGLE_CLIENT_ID),
        "groq_configured": bool(GROQ_API_KEY)
    }

# -------------------------
# Google sign-in callback
# -------------------------
@app.post("/auth/google")
async def auth_google(payload: GoogleAuthRequest, request: Request):
    try:
        token = payload.credential
        if not GOOGLE_CLIENT_ID:
            logger.error("GOOGLE_CLIENT_ID not configured")
            raise HTTPException(status_code=500, detail="Google authentication not configured")

        # Verify token with Google
        idinfo = id_token.verify_oauth2_token(token, grequests.Request(), GOOGLE_CLIENT_ID)

        # Verify audience (optional, but good)
        if idinfo.get("aud") != GOOGLE_CLIENT_ID:
            raise HTTPException(status_code=401, detail="Token audience mismatch")

        email = idinfo.get("email")
        name = idinfo.get("name")
        picture = idinfo.get("picture")

        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        logger.info(f"User authenticated: {email}")

        # Create server session JWT
        session_token = create_session_jwt(email=email, name=name, picture=picture, hours=12)

        # Determine cookie 'secure' flag (secure=True in production)
        secure_cookie = os.getenv("VERCEL") == "1" or request.url.scheme == "https"

        response = JSONResponse({
            "success": True, 
            "user": {
                "email": email, 
                "name": name, 
                "picture": picture
            }
        })
        
        response.set_cookie(
            "session",
            session_token,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            max_age=12 * 3600
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
    
    return {
        "user": {
            "email": user.get("sub"), 
            "name": user.get("name"),
            "picture": user.get("picture")
        }
    }

@app.post("/auth/logout")
async def auth_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("session", path="/")
    return response

# -------------------------
# Protected chat endpoint
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
            logger.error("GROQ_API_KEY not configured")
            raise HTTPException(status_code=500, detail="AI service not configured")

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {
                    "role": "system", 
                    "content": """You are Naradmuni, a wise and experienced life advisor from ancient Indian tradition. You have deep wisdom about human nature, relationships, and life's challenges. You speak like a caring mentor who combines practical advice with spiritual wisdom.

IMPORTANT GUIDELINES:
- Keep responses between 150-200 words maximum
- Start responses with warm greetings like "My dear friend," "Beloved soul," or "Dear seeker"
- Use structured formatting with clear paragraphs and bullet points when listing advice
- Include practical, actionable steps
- End with an encouraging question or reflection to engage the user
- Blend modern practical advice with timeless wisdom
- Be warm, empathetic, and supportive in tone
- Use metaphors and analogies from nature or daily life when appropriate

TOPICS YOU EXCEL AT:
- Personal relationships and communication
- Work-life balance and career guidance  
- Stress management and mental wellness
- Spiritual growth and self-improvement
- Family dynamics and conflicts
- Time management and productivity
- Emotional healing and resilience
- Life purpose and direction

Format your responses with:
- Clear paragraph breaks
- Bullet points for lists of advice (when applicable)
- Emphasis on 2-3 key actionable points
- A thoughtful closing question or encouragement"""
                },
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.7,
            "max_tokens": 200
        }

        user_email = user.get('sub', 'unknown')
        logger.info(f"Sending request to Groq API for user {user_email} - message: {user_message[:60]}...")
        
        response = requests.post(GROQ_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        reply_data = response.json()
        reply = reply_data["choices"][0]["message"]["content"]
        
        logger.info(f"Successfully got response from Groq API for user {user_email}")
        
        return {"reply": reply, "status": "success"}

    except requests.exceptions.RequestException as e:
        logger.error(f"Groq API error: {e}")
        if hasattr(e, 'response') and e.response is not None:
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


















# from fastapi import FastAPI, Request, HTTPException
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import HTMLResponse, FileResponse
# import requests
# import os
# import logging

# # Configure logging
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)

# app = FastAPI(title="Naradmuni Chatbot API", version="1.0.0")

# # CORS middleware to allow frontend requests
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],  # In production, specify your frontend domain
#     allow_credentials=True,
#     allow_methods=["GET", "POST"],
#     allow_headers=["*"],
# )

# GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# # -------------------------
# # Serve index.html frontend
# # -------------------------
# @app.get("/", response_class=HTMLResponse)
# async def serve_frontend():
#     return FileResponse("index.html")


# @app.get("/health")
# async def health_check():
#     return {"status": "healthy"}


# @app.post("/chat")
# async def chat(request: Request):
#     try:
#         body = await request.json()
#         user_message = body.get("message", "")
        
#         if not user_message:
#             raise HTTPException(status_code=400, detail="Message is required")
        
#         if not GROQ_API_KEY:
#             raise HTTPException(status_code=500, detail="API key not configured")

#         headers = {
#             "Authorization": f"Bearer {GROQ_API_KEY}",
#             "Content-Type": "application/json"
#         }

#         data = {
#             "model": "llama-3.1-8b-instant",
#             "messages": [
#                 {
#                     "role": "system", 
#                     "content": """You are Naradmuni, a wise and experienced life advisor from ancient Indian tradition. You have deep wisdom about human nature, relationships, and life's challenges. You speak like a caring mentor who combines practical advice with spiritual wisdom.

# IMPORTANT GUIDELINES:
# - Keep responses between 150-200 words maximum
# - Start responses with warm greetings like "My dear friend," "Beloved soul," or "Dear seeker"
# - Use structured formatting with clear paragraphs and bullet points when listing advice
# - Include practical, actionable steps
# - End with an encouraging question or reflection to engage the user
# - Blend modern practical advice with timeless wisdom
# - Be warm, empathetic, and supportive in tone
# - Use metaphors and analogies from nature or daily life when appropriate

# TOPICS YOU EXCEL AT:
# - Personal relationships and communication
# - Work-life balance and career guidance  
# - Stress management and mental wellness
# - Spiritual growth and self-improvement
# - Family dynamics and conflicts
# - Time management and productivity
# - Emotional healing and resilience
# - Life purpose and direction

# Format your responses with:
# - Clear paragraph breaks
# - Bullet points for lists of advice (when applicable)
# - Emphasis on 2-3 key actionable points
# - A thoughtful closing question or encouragement"""
#                 },
#                 {"role": "user", "content": user_message}
#             ],
#             "temperature": 0.7,
#             "max_tokens": 150
#         }

#         logger.info(f"Sending request to Groq API for message: {user_message[:50]}...")
        
#         response = requests.post(GROQ_URL, headers=headers, json=data, timeout=30)
#         response.raise_for_status()
        
#         reply_data = response.json()
#         reply = reply_data["choices"][0]["message"]["content"]
        
#         logger.info("Successfully got response from Groq API")
        
#         return {"reply": reply, "status": "success"}
        
#     except requests.exceptions.RequestException as e:
#         logger.error(f"Groq API error: {e}")
#         raise HTTPException(status_code=500, detail="AI service temporarily unavailable")
#     except Exception as e:
#         logger.error(f"Unexpected error: {e}")
#         raise HTTPException(status_code=500, detail="Internal server error")

# if __name__ == "__main__":
#     import uvicorn
#     port = int(os.getenv("PORT", 8000))
#     uvicorn.run(app, host="0.0.0.0", port=port)