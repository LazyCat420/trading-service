import os
import io
import wave
from typing import Dict
import time

try:
    from piper import PiperVoice
    HAS_PIPER = True
except ImportError:
    HAS_PIPER = False
    print("WARNING: piper-tts is not installed. TTS generation will fail.")

class PiperTTSManager:
    def __init__(self, models_dir: str = None):
        if models_dir is None:
            models_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "piper_models")
        self.models_dir = os.path.abspath(models_dir)
        self.voices: Dict[str, PiperVoice] = {}
        
        # We define a few fallback mapping aliases for the downloaded voices
        self.aliases = {
            "en-US": "en_US-lessac-medium",
            "en-US-MALE": "en_US-ryan-high",
            "en-GB": "en_GB-alan-medium",
            "default": "en_US-lessac-medium"
        }

    def _get_model_path(self, voice_name: str) -> str:
        """Resolves alias and returns path to the ONNX model"""
        name = self.aliases.get(voice_name, voice_name)
        # If the requested voice_name doesn't exist locally, fallback to default
        onnx_path = os.path.join(self.models_dir, f"{name}.onnx")
        if not os.path.exists(onnx_path):
            name = self.aliases["default"]
            onnx_path = os.path.join(self.models_dir, f"{name}.onnx")
        return onnx_path, name

    def load_voice(self, voice_name: str) -> "PiperVoice":
        """Loads a voice into memory if not already loaded"""
        if not HAS_PIPER:
            raise RuntimeError("piper-tts is not installed.")
            
        onnx_path, resolved_name = self._get_model_path(voice_name)
        
        if resolved_name in self.voices:
            return self.voices[resolved_name]
            
        json_path = f"{onnx_path}.json"
        if not os.path.exists(onnx_path) or not os.path.exists(json_path):
            raise FileNotFoundError(f"Missing ONNX or JSON file for {resolved_name} at {self.models_dir}")
            
        print(f"[PiperTTS] Loading voice into memory: {resolved_name}...")
        t0 = time.time()
        voice = PiperVoice(load_path=onnx_path, config_path=json_path)
        self.voices[resolved_name] = voice
        print(f"[PiperTTS] Loaded {resolved_name} in {time.time() - t0:.2f}s")
        return voice

    def synthesize(self, text: str, voice_name: str = "default") -> bytes:
        """Synthesizes text to a WAV byte string"""
        voice = self.load_voice(voice_name)
        
        # Piper synthesize outputs directly to a WAV file object
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav_file:
            voice.synthesize(text, wav_file)
            
        return wav_io.getvalue()

# Singleton instance
tts_manager = PiperTTSManager()
