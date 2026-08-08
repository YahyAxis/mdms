"""
Text Normalization & String Utility Library
Centralizes title cleaning, edition/remaster noise stripping, artist name sanitization,
path-seed cleaning, slugification, and string similarity scoring across MDMS modules.
"""

import os
import re
import unicodedata
import difflib
from typing import Optional

EDITION_NOISE_PATTERN = re.compile(
    r"[\(\[\-]\s*(20\d\d|19\d\d)?\s*(remastered|remaster|deluxe edition|bonus track|anniversary edition|single version|album version|5\.1 mix|stereo|mono|digital remaster|expanded edition)\s*[\)\]]?",
    re.IGNORECASE
)

EDITION_SUFFIX_PATTERN = re.compile(
    r"\b(remastered|remaster|deluxe|edition|anniversary|expanded)\b",
    re.IGNORECASE
)

FEATURED_ARTIST_PATTERN = re.compile(
    r"\s+[\(\[]?\s*(feat\.?|ft\.?|featuring|with|presents|pres\.?)\s+.*$",
    re.IGNORECASE
)

PATH_SEED_PREFIX_PATTERN = re.compile(
    r"^(\d+[\s\._\-]+|\b(cd|disc|vol|volume)\s*\d+[\s\._\-]+|\(\d{4}\)[\s\._\-]+)",
    re.IGNORECASE
)

def unicodedata_normalize(text: str) -> str:
    """Normalizes Unicode strings to NFC form."""
    if not text:
        return ""
    return unicodedata.normalize("NFC", str(text)).strip()

def strip_edition_noise(text: str, casefold: bool = True) -> str:
    """Strips remaster/edition noise brackets and suffixes from track and album titles."""
    if not text:
        return ""

    norm = unicodedata_normalize(text)
    if casefold:
        norm = norm.casefold()

    cleaned = EDITION_NOISE_PATTERN.sub("", norm)
    cleaned = re.sub(r"\s+", " ", cleaned.strip('"' + "'" + '“”‘’')).strip()
    return cleaned

def sanitize_artist_name(name: str) -> str:
    """Strips featured artist suffixes ('feat.', 'ft.', 'featuring') and normalizes whitespace."""
    if not name:
        return ""
    norm = unicodedata_normalize(name)
    cleaned = FEATURED_ARTIST_PATTERN.sub("", norm)
    return re.sub(r"\s+", " ", cleaned.strip('"' + "'" + '“”‘’')).strip()

def normalize_artist_name(name: str) -> str:
    """
    Artist name normalization for directory deduplication.
    Standardizes apostrophes, handles '&' vs 'and', and removes leading 'The' only.
    """
    if not name:
        return ""
    clean = sanitize_artist_name(name).casefold()
    clean = clean.replace("’", "'").replace("`", "'")
    clean = clean.replace("&", " and ")
    # Only strip 'the' if it is a leading article (e.g. at start of string), preserving internal entries like 'The The'
    clean = re.sub(r"^the\s+", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean

def clean_path_seed(filepath: str) -> str:
    """
    Cleans filename path seeds before pushing to meta_evidence.
    Strips leading track/disc/year prefixes and edition noise.
    """
    if not filepath:
        return ""
    stem = os.path.splitext(os.path.basename(filepath))[0]
    cleaned = unicodedata_normalize(stem)
    cleaned = PATH_SEED_PREFIX_PATTERN.sub("", cleaned)
    cleaned = EDITION_NOISE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[\._\-]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()

def is_canonical_title_form(text: str) -> bool:
    """Evaluates whether a title string is in short canonical form without edition suffixes."""
    if not text:
        return True
    return not bool(EDITION_SUFFIX_PATTERN.search(text))

def calculate_string_similarity(str1: Optional[str], str2: Optional[str]) -> float:
    """Calculates alphanumeric SequenceMatcher ratio similarity between two strings [0.0 - 1.0]."""
    if not str1 or not str2:
        return 0.0
    c1 = "".join(c.lower() for c in str(str1) if c.isalnum())
    c2 = "".join(c.lower() for c in str(str2) if c.isalnum())
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 1.0
    return difflib.SequenceMatcher(None, c1, c2).ratio()

def slugify_text(text: str) -> str:
    """Converts arbitrary text into a safe ASCII URL or filename slug."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    norm = norm.lower()
    norm = re.sub(r"[^\w\s-]", "", norm)
    return re.sub(r"[-\s]+", "_", norm).strip("_")