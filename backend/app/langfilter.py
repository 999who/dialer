"""Drop English phrases Parakeet v3 sometimes returns on quiet or unclear Polish audio.

Ported from 999who/recorder_fork (recorder/core/langfilter.py). A clip is rejected only
when English function words dominate and there is no Polish trace (letters or words).
"""
import re
from typing import Optional

_PL_CHARS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")

# Słowa polskie, które nie występują jako angielskie (pomijamy słowa jednoliterowe i wspólne: do, no, on, my, was)
_PL_WORDS = {
    "nie", "to", "się", "jest", "że", "tak", "ale", "co", "jak", "czy", "ja", "ty", "mnie", "tu", "tam",
    "już", "też", "tylko", "bardzo", "dobrze", "proszę", "dzięki", "dzień", "dobry", "bo", "za", "po", "od",
    "ten", "ta", "te", "tego", "tym", "jeszcze", "będzie", "było", "są", "mam", "masz", "ma", "mamy", "możesz",
    "można", "trzeba", "teraz", "potem", "zaraz", "wiesz", "więc", "czyli", "dla", "przez", "przy", "nad", "pod",
    "kto", "gdzie", "kiedy", "dlaczego", "który", "która", "które", "razem", "firmy", "klient", "faktura",
}

# Typowe angielskie słowa funkcyjne i wtrącenia (bez słów wspólnych z polskim)
_EN_WORDS = {
    "the", "you", "and", "is", "are", "that", "this", "it", "what", "well", "got", "yeah", "yes", "okay", "oh",
    "good", "like", "of", "in", "for", "with", "have", "has", "not", "but", "just", "know", "right", "thank",
    "thanks", "please", "hello", "hi", "hey", "fuck", "shit", "really", "will", "would", "your", "we", "they",
    "he", "she", "there", "here", "go", "see", "now", "about", "from", "did", "how", "why", "when", "who", "if",
    "im", "dont", "its", "thats", "let", "lets", "gonna", "wanna", "sorry", "wow", "nice", "great", "cool", "bye",
    "alright", "sure", "maybe", "come", "want", "need", "think", "say", "said", "look", "been", "were", "them",
    "been", "our", "his", "her", "out", "up", "down", "over", "some", "any", "very", "much", "more", "than", "then",
}

_TOKEN_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def is_foreign_language(text: str, min_english_ratio: float = 0.5) -> bool:
    """True, gdy tekst wygląda na angielski (przeważają angielskie słowa) i nie ma w nim polskich śladów."""
    if not text:
        return False
    if any(ch in _PL_CHARS for ch in text):
        return False
    tokens = [t.lower().replace("'", "") for t in _TOKEN_RE.findall(text)]
    tokens = [t for t in tokens if len(t) >= 2 or t == "i"]
    if not tokens:
        return False
    if any(t in _PL_WORDS for t in tokens):
        return False
    en_hits = sum(1 for t in tokens if t in _EN_WORDS)
    return en_hits / len(tokens) >= min_english_ratio
