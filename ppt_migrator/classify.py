"""Works out strand (Phonics / Reading / Writing), grade band (GK-1 / G2-5) and
language (en / es) for an old deck.

* language  - common-word counts (Spanish vs English)
* grade     - the file name (..._GK_..., ..._G3_...), then the slide text
* strand    - a small word model trained on the old decks you gave
              (python -m ppt_migrator train ... adds more examples)
"""
import json
import math
import os
import re
import unicodedata
import zipfile

from lxml import etree

STRANDS = ["Phonics", "Reading", "Writing"]
BANDS = ["GK-1", "G2-5"]

_ES = set("el la los las de del que y en un una por para con se su sus es son al lo como más pero sus "
          "este esta estos estas qué cómo cuál quién dónde lección unidad semana estudiantes palabra "
          "palabras pida diga muestre lean escuchen".split())
_EN = set("the and of to a in is that for with on as are this be it you your they their students "
          "lesson unit week word words have will what how say read".split())

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def tokens(text):
    text = _strip_accents(text.lower())
    return [t for t in re.findall(r"[a-z]{3,}", text)]


def deck_text(path, with_notes=True):
    """All slide (and notes) text of a .pptx, without parsing it fully."""
    out = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if re.match(r"ppt/slides/slide\d+\.xml$", n) or (
                    with_notes and re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n)):
                try:
                    root = etree.fromstring(z.read(n), etree.XMLParser(resolve_entities=False, no_network=True,
                                                                         huge_tree=True))
                except etree.XMLSyntaxError:
                    continue
                out.append(" ".join(t.text or "" for t in root.iter(_A_NS + "t")))
    return "\n".join(out)


def first_slide_text(path):
    with zipfile.ZipFile(path) as z:
        try:
            root = etree.fromstring(z.read("ppt/slides/slide1.xml"),
                                    etree.XMLParser(resolve_entities=False, no_network=True))
        except (KeyError, etree.XMLSyntaxError):
            return ""
    return " ".join(t.text or "" for t in root.iter(_A_NS + "t"))


_LETTERS = re.compile(r"(\b[a-z\u00f1]{1,2}(?:\s*,\s*[a-z\u00f1]{1,2}){1,}\b)|(\b([A-Z\u00d1])([a-z\u00f1])\b)")


def letter_title(title):
    for m in _LETTERS.finditer(title):
        if m.group(1) or (m.group(3) and m.group(3).lower() == m.group(4)):
            return True
    return False


def detect_language(text):
    toks = re.findall(r"[a-záéíóúñü]+", text.lower())
    es = sum(1 for t in toks if t in _ES)
    en = sum(1 for t in toks if t in _EN)
    if es + en == 0:
        return "en", 0.0
    lang = "es" if es > en else "en"
    return lang, abs(es - en) / float(es + en)


def detect_grade(filename, text=""):
    base = os.path.basename(filename)
    m = re.search(r"(?:^|[_\-\s.])G(K|\d{1,2})(?:-\d)?(?=[_\-\s.]|$)", base, re.I)
    if not m:
        m = re.search(r"(?:grade|grado)\s*(K|\d)\b", base + " \n " + (text or ""), re.I)
    if not m:
        if re.search(r"\bkinder(garten)?\b", text, re.I):
            return "K", "GK-1", True
        return None, None, False
    g = m.group(1).upper()
    band = "GK-1" if g in ("K", "0", "1") else ("G2-5" if g.isdigit() and 2 <= int(g) <= 5 else None)
    return g, band, band is not None


class StrandModel:
    """Multinomial naive Bayes over words; tiny, transparent and retrainable."""

    def __init__(self, data=None):
        data = data or {}
        self.counts = data.get("counts", {s: {} for s in STRANDS})
        self.docs = data.get("docs", {s: 0 for s in STRANDS})
        self.hints = data.get("hints", {
            "Phonics": ["phonics", "fonetica", "fonica", "sound spelling", "articulation", "articulacion",
                        "high frequency", "uso frecuente", "handwriting", "caligrafia", "decodable",
                        "phonemic", "fonologica", "blend", "silabas", "decodificable", "decodificar",
                        "sonido inicial", "la letra", "the letter", "letter sound"],
            "Reading": ["read aloud", "lectura en voz alta", "lectura atenta", "close read",
                        "escuchar y observar", "texto principal", "comprehension", "comprension"],
            "Writing": ["writing", "escritura", "borrador", "draft", "opinion", "publish", "publicar",
                        "pauta de escritura", "revise", "revisar", "editar"],
        })

    def to_json(self):
        return {"counts": self.counts, "docs": self.docs, "hints": self.hints}

    def add(self, strand, text, weight=1):
        c = self.counts.setdefault(strand, {})
        for t in tokens(text):
            c[t] = c.get(t, 0) + weight
        self.docs[strand] = self.docs.get(strand, 0) + 1

    def predict(self, text, title=""):
        toks = tokens(text)
        vocab = set()
        for c in self.counts.values():
            vocab.update(c)
        V = len(vocab) + 1
        scores = {}
        total_docs = sum(self.docs.values()) or 1
        for s in STRANDS:
            c = self.counts.get(s, {})
            tot = sum(c.values())
            lp = math.log((self.docs.get(s, 0) + 1) / float(total_docs + len(STRANDS)))
            for t in toks:
                lp += math.log((c.get(t, 0) + 1) / float(tot + V))
            scores[s] = lp
        low = _strip_accents(text.lower())
        for s, words in self.hints.items():          # small nudge from strand words
            scores[s] = scores.get(s, 0) + 2.0 * sum(low.count(w) for w in words)
        # letter sets ("ca, co, cu") and letter pairs ("Cc", "Ii") are typical phonics titles
        letters = len(re.findall(r"\b[a-z\u00f1]{1,2}(?:\s*,\s*[a-z\u00f1]{1,2}){2,}\b", text)) + \
            sum(1 for a, b in re.findall(r"\b([A-Z])([a-z])\b", text) if a.lower() == b)
        scores["Phonics"] = scores.get("Phonics", 0) + 6.0 * letters
        if title and letter_title(title):            # "Introduce: Cc", "Ampliar: ca, co, cu"
            return "Phonics", 0.95, scores
        best = max(scores, key=scores.get)
        ordered = sorted(scores.values(), reverse=True)
        margin = ordered[0] - ordered[1] if len(ordered) > 1 else 99
        conf = 1.0 - math.exp(-max(0.0, margin) / 60.0)
        return best, conf, scores


def load_model(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return StrandModel(json.load(f))
    return StrandModel()


def save_model(model, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model.to_json(), f, ensure_ascii=False)


def strand_from_path(path):
    low = path.lower()
    for s in STRANDS:
        if re.search(r"(^|[\\/_\-\s])%s([\\/_\-\s.]|$)" % s.lower(), low):
            return s
    for s, alias in (("Phonics", "fonetica"), ("Reading", "lectura"), ("Writing", "escritura")):
        if alias in _strip_accents(low):
            return s
    return None


def detect(path, model, display_name=None):
    """Everything the web page shows for one uploaded deck."""
    name = display_name or os.path.basename(path)
    text = deck_text(path)
    lang, lconf = detect_language(text)
    grade, band, gok = detect_grade(name, text)
    hint = strand_from_path(display_name or path)
    strand, sconf, _ = model.predict(text, first_slide_text(path))
    if hint:
        strand, sconf = hint, 1.0
    return {
        "language": lang, "language_conf": round(lconf, 2),
        "grade": grade, "band": band or "GK-1", "band_found": gok,
        "strand": strand, "strand_conf": round(sconf, 2), "strand_from": "name" if hint else "content",
    }
