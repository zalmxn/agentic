import re

# Common user spelling variations and shorthand normalized deterministically
COMMON_TYPO_MAP: dict[str, str] = {
    "plumer": "plumber",
    "plumbr": "plumber",
    "plumbng": "plumbing",
    "electrcian": "electrician",
    "electrition": "electrician",
    "electrcal": "electrical",
    "eletrician": "electrician",
    "carwash": "car wash",
    "carwah": "car wash",
    "acrepair": "ac repair",
    "tomorow": "tomorrow",
    "tommorrow": "tomorrow",
    "tmrw": "tomorrow",
    "tmr": "tomorrow",
    "tdy": "today",
    "cheepest": "cheapest",
    "cheapst": "cheapest",
    "chepest": "cheapest",
    "neerest": "nearest",
    "nearest": "nearest",
    "availble": "available",
    "availabl": "available",
    "avaliable": "available",
    "providr": "provider",
    "servce": "service",
    "bookng": "booking",
    "cancle": "cancel",
    "cncl": "cancel",
    "confrm": "confirm",
    "repar": "repair",
    "kochin": "kochi",
    "cochin": "kochi",
    "alwaye": "aluva",
    "ernakulam": "ernakulam",
    "kakkanad": "kakkanad",
    "edapally": "edappally",
}

CONTRACTIONS: dict[str, str] = {
    "i'd": "i would",
    "i'll": "i will",
    "i'm": "i am",
    "i've": "i have",
    "don't": "do not",
    "can't": "cannot",
    "won't": "will not",
    "what's": "what is",
    "that's": "that is",
    "it's": "it is",
}


def normalize_text(text: str) -> str:
    """
    Normalize natural language input while preserving times (14:00, 2:30 pm),
    dates (2026-11-20), booking codes (BK-20261120-AB12), and decimals.
    """
    cleaned = text.strip().lower()
    cleaned = cleaned.replace("₹", " rs ").replace("’", "'").replace("‘", "'")
    for contraction, expansion in CONTRACTIONS.items():
        cleaned = re.sub(rf"\b{re.escape(contraction)}\b", expansion, cleaned)

    # Remove noise punctuation while keeping : - / . inside tokens
    cleaned = re.sub(r"[?!,;()\"'#*]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Token-level typo normalization (skip booking codes & numeric tokens)
    tokens = cleaned.split(" ")
    normalized_tokens: list[str] = []
    for tok in tokens:
        stripped = tok.strip(".")
        if stripped in COMMON_TYPO_MAP:
            normalized_tokens.append(COMMON_TYPO_MAP[stripped])
        else:
            normalized_tokens.append(stripped if not re.match(r"^\d+\.\d+$", tok) else tok)

    return re.sub(r"\s+", " ", " ".join(normalized_tokens)).strip()
