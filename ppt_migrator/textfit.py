"""Approximate text layout, used to size auto-fit panels and to line up the
badge pictures (ACCESS / ELD / EXTEND ...) with their "See notes." line.

PowerPoint only recalculates <a:spAutoFit/> boxes when the text is edited, so
the converter writes the size PowerPoint would have produced.
"""
import os
import shutil
import subprocess

try:
    from PIL import ImageFont
except Exception:  # Pillow is optional
    ImageFont = None

_FONT_CACHE = {}


_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLED = os.path.join(_HERE, "fonts")

# Windows file names of Century Gothic; Office for Mac keeps its own copy inside each app.
_KNOWN_FILES = {
    "century gothic": (["GOTHIC.TTF", "Century Gothic.ttf", "CenturyGothic.ttf"],
                       ["GOTHICB.TTF", "Century Gothic Bold.ttf", "CenturyGothic-Bold.ttf"]),
}
# Same proportions as Century Gothic (both follow Avant Garde Gothic); shipped with the app
# so text is measured the same way on a computer without Office fonts.
_SUBSTITUTES = {
    "century gothic": ("texgyreadventor-regular.otf", "texgyreadventor-bold.otf"),
}
_OFFICE_APPS = ["Microsoft PowerPoint", "Microsoft Word", "Microsoft Excel", "Microsoft Outlook"]
_FONT_DIRS = ([os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
               os.path.expanduser("~/AppData/Local/Microsoft/Windows/Fonts")] +
              ["/Applications/%s.app/Contents/Resources/DFonts" % a for a in _OFFICE_APPS] +
              ["/Library/Fonts", os.path.expanduser("~/Library/Fonts"), "/System/Library/Fonts/Supplemental",
               "/Library/Application Support/Microsoft/Fonts", "/usr/share/fonts", os.path.expanduser("~/.fonts")])
_FOUND = {}


def _font_style(path):
    """(family, style) read from the font file itself, or None."""
    if ImageFont is None:
        return None
    try:
        return ImageFont.truetype(path, 12).getname()
    except Exception:
        return None


def _scan_dirs(family, bold):
    """Look through the font folders for a file whose own name says it is `family` (bold or not)."""
    want = family.lower()
    key = want.split()[-1]  # "gothic" - only files with this in the name are opened
    for d in _FONT_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if key not in n.lower() or not n.lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            p = os.path.join(d, n)
            fs = _font_style(p)
            if not fs or fs[0].lower() != want:
                continue
            style = (fs[1] or "").lower()
            if "italic" in style or "oblique" in style:
                continue
            if ("bold" in style) == bold:
                return p
    return None


def _find_font(family, bold=False):
    k = (family.lower(), bold)
    if k not in _FOUND:
        _FOUND[k] = _locate(family, bold)
    return _FOUND[k]


def _locate(family, bold):
    names = _KNOWN_FILES.get(family.lower())
    if names:
        for d in _FONT_DIRS:
            for n in names[1 if bold else 0]:
                p = os.path.join(d, n)
                if os.path.exists(p):
                    return p
    p = _scan_dirs(family, bold)
    if p:
        return p
    sub = _SUBSTITUTES.get(family.lower())
    if sub:
        p = os.path.join(_BUNDLED, sub[1 if bold else 0])
        if os.path.exists(p):
            return p
    if not shutil.which("fc-match"):
        return None
    pattern = family + (":bold" if bold else "")
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        return out or None
    except Exception:
        return None


class Measurer:
    """Measures text widths in inches; falls back to an average-width model."""

    def __init__(self, family="Century Gothic", line_factor=1.2):
        self.family = family
        self.line_factor = line_factor

    def _font(self, size_pt, bold):
        if ImageFont is None:
            return None
        key = (self.family, bold, round(size_pt * 4))
        if key not in _FONT_CACHE:
            path = _find_font(self.family, bold)
            f = None
            if path:
                try:
                    f = ImageFont.truetype(path, max(1, int(round(size_pt * 4))))  # 4x for precision
                except Exception:
                    f = None
            _FONT_CACHE[key] = f
        return _FONT_CACHE[key]

    def width(self, text, size_pt, bold=False):
        f = self._font(size_pt, bold)
        text = text.replace("​", "")
        if f is None:
            return len(text) * size_pt * 0.56 / 72.0
        try:
            w = f.getlength(text)
        except AttributeError:
            w = f.getsize(text)[0]
        return w / 4.0 / 72.0

    def line_height(self, size_pt):
        return size_pt * self.line_factor / 72.0

    def count_lines(self, segments, avail_in, size_pt, bold=False, tab_in=1.0):
        """segments: list of strings separated by hard line breaks."""
        total = 0
        for seg in segments:
            seg = seg.expandtabs(1).replace("\t", " ")
            words = seg.split(" ")
            lines, cur = 1, ""
            for w in words:
                cand = w if cur == "" else cur + " " + w
                if self.width(cand, size_pt, bold) <= avail_in or cur == "":
                    cur = cand
                else:
                    lines += 1
                    cur = w
            total += lines
        return max(1, total)


def paragraph_block(measurer, para, avail_in):
    """para: dict(text_segments, size, bold, spc_bef_pt, spc_aft_pt, marL_in, first)."""
    n = measurer.count_lines(para["segments"], max(0.2, avail_in - para.get("marL_in", 0.0)),
                             para["size"], para.get("bold", False))
    h = n * measurer.line_height(para["size"])
    before = 0.0 if para.get("first") else para.get("spc_bef_pt", 0) / 72.0
    after = para.get("spc_aft_pt", 0) / 72.0
    return before, h, after, n
