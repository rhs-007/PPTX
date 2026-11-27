"""Finds the 12 new templates (3 strands x 2 grade bands x 2 languages) in a folder."""
import os
import re
import zipfile

from .classify import STRANDS, BANDS


def describe_template(path):
    """(strand, band, lang) for a template file, from its name and its opening-slide prompt."""
    base = os.path.basename(path)
    strand = next((s for s in STRANDS if re.search(s, base, re.I)), None)
    band = "GK-1" if re.search(r"GK\s*-\s*1", base, re.I) else ("G2-5" if re.search(r"G2\s*-\s*5", base, re.I) else None)
    lang = None
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if re.match(r"ppt/slideLayouts/slideLayout\d+\.xml$", n)]
            blob = " ".join(z.read(n).decode("utf-8", "ignore") for n in names[:4])
            if re.search(r">\s*Lecci[oó]n\b", blob):
                lang = "es"
            elif re.search(r">\s*Lesson\b", blob):
                lang = "en"
            if strand is None:
                m = re.search(r"Lesson Outcomes 1 (Phonics|Reading|Writing)", blob)
                strand = m.group(1) if m else None
    except (zipfile.BadZipFile, OSError):
        return None
    if lang is None:
        lang = "es" if re.search(r"spanish|espa|^DL_", base, re.I) else "en"
    if not (strand and band):
        return None
    return strand, band, lang


class TemplateLibrary:
    def __init__(self, folder):
        self.folder = folder
        self.index = {}
        self.scan()

    def scan(self):
        self.index = {}
        if not os.path.isdir(self.folder):
            return
        for root, _dirs, files in os.walk(self.folder):
            for f in sorted(files):
                if f.lower().endswith((".pptx", ".potx")) and not f.startswith(("~$", "._")):
                    p = os.path.join(root, f)
                    d = describe_template(p)
                    if d and d not in self.index:
                        self.index[d] = p

    def get(self, strand, band, lang):
        return self.index.get((strand, band, lang))

    def status(self):
        rows = []
        for lang in ("en", "es"):
            for strand in STRANDS:
                for band in BANDS:
                    p = self.index.get((strand, band, lang))
                    rows.append({"lang": lang, "strand": strand, "band": band,
                                 "file": os.path.basename(p) if p else None})
        return rows
