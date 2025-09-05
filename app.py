from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Naradmuni Chatbot API", version="1.0.0")

# CORS middleware to allow frontend requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend domain
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

@app.get("/")
async def root():
    return {"message": "Naradmuni Chatbot API is running"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.post("/chat")
async def chat(request: Request):
    try:
        body = await request.json()
        user_message = body.get("message", "")
        
        if not user_message:
            raise HTTPException(status_code=400, detail="Message is required")
        
        if not GROQ_API_KEY:
            raise HTTPException(status_code=500, detail="API key not configured")

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        data = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {
                    "role": "system", 
                    "content": """You are Naradmuni, a wise and friendly AI assistant who helps people with their daily life problems. 
                    You provide practical, empathetic, and helpful advice on topics like:
                    - Personal relationships and communication
                    - Work and career guidance
                    - Health and wellness tips
                    - Time management and productivity
                    - Emotional support and stress management
                    - General life advice and problem-solving
                    
                    Keep your responses concise, warm, and actionable. Always be respectful and supportive."""
                },
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.7,
            "max_tokens": 1000
        }

        logger.info(f"Sending request to Groq API for message: {user_message[:50]}...")
        
        response = requests.post(GROQ_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        
        reply_data = response.json()
        reply = reply_data["choices"][0]["message"]["content"]
        
        logger.info("Successfully got response from Groq API")
        
        return {"reply": reply, "status": "success"}
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Groq API error: {e}")
        raise HTTPException(status_code=500, detail="AI service temporarily unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)