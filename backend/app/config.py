from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    auth_token: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_thinking_level: str = "low"
    gemini_timeout_s: float = 4.0

    database_url: str = ""

    stt_engine: str = "parakeet"  # parakeet | none
    parakeet_threads: int = 3          # onnxruntime intra-op threads (CPU)
    parakeet_model_path: str = ""      # local folder with the int8 ONNX files; empty = download from Hugging Face
    stt_polish_only: bool = True       # drop clips Parakeet returns in English
    stt_batch_window_ms: int = 15
    stt_max_batch: int = 8

    # VAD / segmentation
    vad_engine: str = "silero"  # silero | energy
    vad_min_silence_ms: int = 450
    vad_max_utterance_s: float = 12.0
    vad_min_utterance_ms: int = 300

    embed_model: str = "intfloat/multilingual-e5-small"
    rag_top_k: int = 4
    rag_min_similarity: float = 0.80

    master_prompt_path: Path = BACKEND_DIR.parent / "prompts" / "master_prompt_pl.md"
    history_turns: int = 8
    sample_rate: int = 16000

    def resolved_prompt_path(self) -> Path:
        p = Path(self.master_prompt_path)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
