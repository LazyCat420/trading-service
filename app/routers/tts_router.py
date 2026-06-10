from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
import traceback

from app.services.piper_manager import tts_manager

router = APIRouter(prefix="/api/v1/tts", tags=["TTS"])

class TTSRequest(BaseModel):
    text: str
    voice_accent: str = "default"

@router.post("/synthesize")
async def synthesize_speech(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
        
    try:
        # Synchronous CPU-bound call, might block the event loop temporarily
        # For a full prod deployment we'd run this in a threadpool, but for small agent quotes it's fine.
        wav_bytes = tts_manager.synthesize(text=req.text, voice_name=req.voice_accent)
        return Response(content=wav_bytes, media_type="audio/wav")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"TTS synthesis failed: {str(e)}")
