"""Medicine-name normalisation and scoring for the catalogue layer (pure functions, no database).

``split_name("Tab. Montina-FX 10 mg")`` -> ``("montina fx", (10.0,))``: the brand words (dosage form
words, strengths and punctuation removed) and the strengths as numbers. Catalogue names are split
the same way when they are loaded, so an extracted name and a product meet on the same key.

``score(query, candidate)`` compares two brand keys the way OCR / VLM mistakes look: after folding
glyphs a reader confuses (``rn``/``m``, ``0``/``o``, ``1``/``l``, ...) it blends character-trigram
overlap with an edit-similarity ratio. Strength is compared separately (``strength_agrees``); a
brand is identified by its words, a product (e.g. Dolo 500 vs Dolo 650) by its strength.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence

# dosage forms and pack words that are not part of a brand ("Dolo 650 Tablet", "Tab. Pan 40")
FORM_WORDS = frozenset({
    "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules", "syp", "syr",
    "syrup", "susp", "suspension", "inj", "injection", "oint", "ointment", "cream", "gel",
    "lotion", "drop", "drops", "sachet", "sachets", "powder", "granules", "spray", "inhaler",
    "rotacap", "rotacaps", "respules", "solution", "soln", "liquid", "strip", "of", "vial",
    "ampoule", "amp", "kit", "infusion", "pen", "patch", "mouthwash", "gargle", "paste",
    "shampoo", "soap", "chewable", "dt", "md", "dispersible", "effervescent",
})  # fmt: skip
UNIT_WORDS = frozenset({"mg", "mcg", "g", "gm", "ml", "iu", "unit", "units", "w", "v", "ww", "wv"})
# a number that stands alone ("650", "40mg", "60K" = 60 000 IU), not a digit of a name ("D3")
_STRENGTH = re.compile(r"(?<![a-z\d.])(\d+(?:\.\d+)?)\s*(mg|mcg|gm|g|ml|iu|u|k|%)?(?![a-z\d])")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")  # "60,000U"
# glyphs a print OCR or a VLM swaps on handwriting and small print, folded before comparing
_FOLD = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("0", "o"), ("1", "l"), ("5", "s"), ("8", "b"))


def split_name(text: str | None) -> tuple[str, tuple[float, ...]]:
    """(brand key, strengths) of a medicine name as written or as catalogued."""
    t = _THOUSANDS.sub("", (text or "").lower())
    t = re.sub(r"#\s*\d+", " ", t)  # "(#5)": a quantity to dispense, not a strength
    t = re.sub(r"[^a-z0-9.%]+", " ", t)
    strengths = tuple(float(m.group(1)) * (1000 if m.group(2) == "k" else 1)
                      for m in _STRENGTH.finditer(t))  # fmt: skip
    words: list[str] = []
    for w in t.replace(".", " ").split():
        if w in FORM_WORDS:
            if words:  # "Sompraz D 40 Capsule SR": what follows the form qualifies the form
                break
            continue  # "Tab. Dolo 650": a leading form word
        if w in UNIT_WORDS or re.fullmatch(r"[\d%]+|\d+(mg|mcg|gm|g|ml|iu|u|k)", w):
            continue
        words.append(w)
    return " ".join(words), strengths


# the form word written fused to the name, as a VLM reads a handwritten "Cap Uprise": "CapUprise"
_FUSED_FORM = re.compile(r"^\s*(Tab|TAB|Cap|CAP|Caps|Syp|Syr|Inj|INJ)\.?(?=[A-Z][a-z])")
# a prescription marker read into the name ("Ad. Sompraz", "Rx Dolo"); dropped only when the name
# as written finds nothing, because a few brands begin with these letters ("AD Para")
_RX_MARKER = re.compile(r"^\s*(rx|adv|ad)\b[.:]?\s*", re.I)


def query_keys(text: str | None) -> list[tuple[str, tuple[float, ...]]]:
    """The (brand key, strengths) a written medicine name is looked up by, in order: as written,
    then without a leading prescription marker. Catalogue names are never split this way."""
    t = standard_form_prefix(_FUSED_FORM.sub(r"\1 ", text or ""))
    keys = [split_name(t)]
    bare = _RX_MARKER.sub("", t, count=1)
    if bare != t:
        alt = split_name(bare)
        if alt[0] and alt[0] != keys[0][0]:
            keys.append(alt)
    return keys


# Every written dosage-form word or abbreviation -> the catalogue's own form word (the forms the
# Tata 1mg product names use: Tablet, Capsule, Syrup, Injection, Suspension, Cream, ...),
# lower-cased. The ONE table the extraction, the catalogue and the terminology lookup share
# (owner, 2026-09-28: "map abbreviations correctly ... consistently to all medicines").
DOSAGE_FORMS = {
    "tab": "tablet", "tabs": "tablet", "tablet": "tablet", "tablets": "tablet",
    "cap": "capsule", "caps": "capsule", "capsule": "capsule", "capsules": "capsule",
    "syp": "syrup", "syr": "syrup", "syrup": "syrup",
    "inj": "injection", "injection": "injection",
    "susp": "suspension", "suspension": "suspension",
    "crm": "cream", "cream": "cream",
    "oint": "ointment", "ointment": "ointment",
    "drop": "drops", "drops": "drops", "gtt": "drops", "gtts": "drops",
    "gel": "gel",
    "lot": "lotion", "lotion": "lotion",
    "sol": "solution", "soln": "solution", "solution": "solution",
    "pwd": "powder", "powder": "powder",
    "sach": "sachet", "sachet": "sachet", "sachets": "sachet",
    "inh": "inhaler", "inhaler": "inhaler",
    "resp": "respules", "respules": "respules",
    "rotacap": "rotacap", "rotacaps": "rotacap",
    "spray": "spray",
    "supp": "suppository", "suppository": "suppository",
    "liq": "liquid", "liquid": "liquid",
    "loz": "lozenges", "lozenges": "lozenges",
    "gran": "granules", "granules": "granules",
    "pen": "pen",
    # the rest of the catalogue's form words (Tata 1mg product names), so a product of another
    # form is recognised as one ("Moxicip Infusion" is not the "Gtt Moxicip" eye drop)
    "infusion": "infusion", "gargle": "gargle", "mouthwash": "mouthwash", "soap": "soap",
    "shampoo": "shampoo", "patch": "patch", "paste": "paste", "kit": "kit",
    "softgel": "softgel", "jelly": "jelly", "emulsion": "emulsion", "elixir": "elixir",
}  # fmt: skip
# abbreviations that are a dosage form ONLY as the first word of a medicine line ("T Dytor",
# "C Pan-D", "Cr Candid"); anywhere else they are part of a name ("Vitamin C", "Neurobion T") or
# a release form ("Glycomet CR" = controlled release)
LEADING_FORMS = {"t": "tablet", "c": "capsule", "cr": "cream"}
_FORM_OF = DOSAGE_FORMS


def form_word(word: str, *, leading: bool = False) -> str | None:
    """The catalogue form a single written word stands for ("Tab." -> tablet), or None."""
    w = word.lower().strip(" .,:;-")
    return DOSAGE_FORMS.get(w) or (LEADING_FORMS.get(w) if leading else None)


def dosage_form(text: str | None) -> str | None:
    """The dosage form a name states, from its first form word: "Cap. Sompraz" -> capsule,
    "Sompraz 40 Tablet" -> tablet, "CapUprise" -> capsule, "T Dytor" -> tablet; None when it
    states none."""
    words = re.sub(r"[^a-z]+", " ", _FUSED_FORM.sub(r"\1 ", text or "").lower()).split()
    if words and (lead := form_word(words[0], leading=True)):
        return lead
    return next((DOSAGE_FORMS[w] for w in words if w in DOSAGE_FORMS), None)


_LEADING_WORD = re.compile(r"^\s*([A-Za-z]+)\b\.?\s+(?=\S)")


def standard_form_prefix(text: str) -> str:
    """The line with a leading dosage-form abbreviation written as the catalogue's word, so the
    rest is read as the name: "T Dytor 10" -> "tablet Dytor 10", "Cr. Candid" -> "cream Candid".
    Only a form word the brand splitter already knows is written (a catalogue key never changes)."""
    m = _LEADING_WORD.match(text)
    if m and (form := form_word(m.group(1), leading=True)) and form in FORM_WORDS:
        return f"{form} {text[m.end() :]}"
    return text


def ingredients(composition: str | None) -> list[tuple[str, str]]:
    """(generic key, generic name) of each ingredient of a catalogue composition:
    "Amoxycillin (500mg) + Clavulanic Acid (125mg)" -> amoxycillin, clavulanic acid."""
    out: list[tuple[str, str]] = []
    for part in (composition or "").split("+"):
        name = re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", part)).strip()
        key = split_name(name)[0]
        if letters(key) >= MIN_KEY_LETTERS:
            out.append((key, name))
    return out


_COMP_STRENGTH = re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*(mg|mcg|gm|g|ml|iu|%)?", re.I)


def composition_strengths(composition: str | None) -> tuple[float, ...]:
    """The strength of each ingredient of a composition: "Sulfasalazine (500mg)" -> (500,),
    "Vitamin D3 (60000IU)" -> (60000,); "(NA)" states none."""
    return tuple(float(m.group(1)) for m in _COMP_STRENGTH.finditer(composition or ""))


def nearness(query: str, candidate: str) -> float:
    """How near two keys look, for ranking the closest products a reviewer is shown (never for
    accepting a match): ``score``, or the share of matching characters after folding, which
    also ranks short names ("shaz" / "saaz")."""
    ratio = difflib.SequenceMatcher(None, fold(query), fold(candidate)).ratio()
    return round(max(score(query, candidate), ratio), 4)


def letters(key: str) -> int:
    return sum(ch.isalpha() for ch in key)


def fold(key: str) -> str:
    """The key with OCR-confusable glyphs folded together, spaces removed."""
    k = key.replace(" ", "")
    for a, b in _FOLD:
        k = k.replace(a, b)
    return k


# a brand key this short is matched exactly or not at all: short look-alike names (Dolo / Dola)
# are where one misread letter is another medicine
MIN_FUZZY_LEN = 5
# a key with fewer letters than this is not a medicine name at all ("T", "CAP", "D3")
MIN_KEY_LETTERS = 3


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (insert / delete / substitute, each 1)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score(query: str, candidate: str) -> float:
    """0..1 similarity of two brand keys, tolerant of OCR / handwriting misreads: one minus the
    edit distance per character, after folding confusable glyphs. One wrong letter in an
    eight-letter brand scores 0.875; keys shorter than ``MIN_FUZZY_LEN`` score 0 unless equal."""
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    dq, dc = re.sub(r"\D", "", query), re.sub(r"\D", "", candidate)
    if dq and dc and dq != dc:
        return 0.0  # a different number in the name is a different product (Uprise D2 / D3)
    q, c = fold(query), fold(candidate)
    if q == c:
        return 0.97  # the same once confusable glyphs are folded ("Rn" vs "m")
    if min(len(q), len(c)) < MIN_FUZZY_LEN:
        return 0.0
    sim = 1 - edit_distance(q, c) / max(len(q), len(c))
    # a tie-break between equal edit distances: the longer common run wins
    run = difflib.SequenceMatcher(None, q, c).find_longest_match(0, len(q), 0, len(c)).size
    return round(max(0.0, sim) + 0.01 * run / max(len(q), len(c)), 4)


def strength_agrees(query: Sequence[float], product: Sequence[float]) -> bool | None:
    """True / False when both sides state a strength, None when either does not."""
    if not query or not product:
        return None
    return bool(set(query) & set(product))


def best_brand(scored: Iterable[tuple[str, float]]) -> tuple[str, float, float]:
    """(best brand key, its score, the best score of any OTHER brand) from (brand, score) pairs."""
    per_brand: dict[str, float] = {}
    for brand, s in scored:
        per_brand[brand] = max(s, per_brand.get(brand, 0.0))
    if not per_brand:
        return "", 0.0, 0.0
    ranked = sorted(per_brand.items(), key=lambda kv: -kv[1])
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    return ranked[0][0], ranked[0][1], runner
