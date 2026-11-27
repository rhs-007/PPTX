"""Read-only helpers over a deck: slides, layouts, masters, placeholders, style chains."""
import re

from lxml import etree

from .package import A, P, R, NS, EMU, RT_LAYOUT, RT_THEME
from .theme import ThemeContext

TITLE_TYPES = {"title", "ctrTitle"}


def local(el):
    return etree.QName(el).localname


def ph_el(shape):
    """The <p:ph> of a top-level shape (sp, pic, graphicFrame, cxnSp), or None."""
    for nv in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr", "p:nvGrpSpPr"):
        e = shape.find(nv + "/p:nvPr/p:ph", NS)
        if e is not None:
            return e
    return None


def ph_type(ph):
    return ph.get("type", "body") if ph is not None else None


def ph_key(ph):
    """Key used in profiles: 'title' for title types, else the idx as a string."""
    if ph is None:
        return None
    t = ph.get("type")
    if t in TITLE_TYPES:
        return "title"
    return ph.get("idx", t or "body")


def cnvpr(shape):
    for nv in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr", "p:nvGrpSpPr"):
        e = shape.find(nv + "/p:cNvPr", NS)
        if e is not None:
            return e
    if local(shape) == "AlternateContent":
        for ch in shape:
            if len(ch):
                return cnvpr(ch[0])
    return None


def xfrm_el(shape):
    """The transform element holding off/ext for a top-level shape."""
    tag = local(shape)
    if tag in ("sp", "pic", "cxnSp"):
        return shape.find("p:spPr/a:xfrm", NS)
    if tag == "grpSp":
        return shape.find("p:grpSpPr/a:xfrm", NS)
    if tag == "graphicFrame":
        return shape.find("p:xfrm", NS)
    if tag == "AlternateContent":
        for ch in shape:
            if len(ch):
                return xfrm_el(ch[0])
    return None


def rect_of_xfrm(x):
    if x is None:
        return None
    off, ext = x.find("a:off", NS), x.find("a:ext", NS)
    if off is None or ext is None:
        return None
    return Rect(int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy")))


class Rect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    @property
    def r(self):
        return self.x + self.w

    @property
    def b(self):
        return self.y + self.h

    @property
    def cx(self):
        return self.x + self.w / 2.0

    @property
    def cy(self):
        return self.y + self.h / 2.0

    def union(self, o):
        x, y = min(self.x, o.x), min(self.y, o.y)
        return Rect(x, y, max(self.r, o.r) - x, max(self.b, o.b) - y)

    def __repr__(self):
        return "Rect(%.2f,%.2f,%.2f,%.2f)in" % (self.x / EMU, self.y / EMU, self.w / EMU, self.h / EMU)


def text_of(el):
    """Plain text of a shape/txBody: paragraphs joined by newlines, <a:br> as \\v."""
    out = []
    for p in el.iter(A + "p"):
        s = []
        for c in p:
            t = local(c)
            if t in ("r", "fld"):
                tt = c.find("a:t", NS)
                s.append(tt.text or "" if tt is not None else "")
            elif t == "br":
                s.append("\v")
        out.append("".join(s))
    return "\n".join(out)


def para_text(p):
    s = []
    for c in p:
        t = local(c)
        if t in ("r", "fld"):
            tt = c.find("a:t", NS)
            s.append((tt.text or "") if tt is not None else "")
        elif t == "br":
            s.append("\v")
    return "".join(s)


def spacer(text):
    return text.replace("​", "").replace("\xa0", " ").strip() == ""


class DeckInfo:
    def __init__(self, pkg):
        self.pkg = pkg
        pres = "ppt/presentation.xml"
        root = pkg.xml(pres)
        self.slides = []
        lst = root.find("p:sldIdLst", NS)
        for sid in (lst if lst is not None else []):
            self.slides.append(pkg.rel_target(pres, sid.get(R + "id")))
        self.masters = []
        for m in root.find("p:sldMasterIdLst", NS):
            self.masters.append(pkg.rel_target(pres, m.get(R + "id")))
        self.layouts = []
        self.layout_name = {}
        self.master_of = {}
        self.theme_of = {}
        for m in self.masters:
            mroot = pkg.xml(m)
            ids = mroot.find("p:sldLayoutIdLst", NS)
            for lid in (ids if ids is not None else []):
                lay = pkg.rel_target(m, lid.get(R + "id"))
                if lay is None:
                    continue
                self.layouts.append(lay)
                self.master_of[lay] = m
                self.layout_name[lay] = pkg.xml(lay).find("p:cSld", NS).get("name", "")
            th = pkg.rels_of_type(m, RT_THEME)
            self.theme_of[m] = th[0][1] if th else None
        self.ctx = {m: ThemeContext(pkg.xml(self.theme_of[m]), pkg.xml(m)) for m in self.masters
                    if self.theme_of[m]}

    def layout_of(self, slide):
        r = self.pkg.rels_of_type(slide, RT_LAYOUT)
        return r[0][1] if r else None

    def layout_by_name(self, name):
        for lay in self.layouts:
            if self.layout_name[lay] == name:
                return lay
        for lay in self.layouts:  # tolerant match
            if self.layout_name[lay].strip().lower() == name.strip().lower():
                return lay
        return None

    # ------------- placeholders in layouts / masters -------------
    def layout_ph(self, layout, key):
        """Find the placeholder shape in a layout by profile key ('title' or idx)."""
        root = self.pkg.xml(layout)
        for sp in root.find("p:cSld/p:spTree", NS):
            ph = ph_el(sp)
            if ph is None:
                continue
            if ph_key(ph) == str(key):
                return sp
        return None

    def master_ph(self, master, ptype):
        root = self.pkg.xml(master)
        want = "title" if ptype in TITLE_TYPES else ptype
        if want in ("obj", None):
            want = "body"
        for sp in root.find("p:cSld/p:spTree", NS):
            ph = ph_el(sp)
            if ph is None:
                continue
            t = ph.get("type", "body")
            if t in TITLE_TYPES:
                t = "title"
            if t == want:
                return sp
        return None

    def inherited_rect(self, shape, layout):
        """Geometry of a (placeholder) shape, following layout -> master inheritance."""
        r = rect_of_xfrm(xfrm_el(shape))
        if r is not None:
            return r
        ph = ph_el(shape)
        if ph is None:
            return None
        lsp = self.layout_ph(layout, ph_key(ph))
        if lsp is not None:
            r = rect_of_xfrm(xfrm_el(lsp))
            if r is not None:
                return r
        msp = self.master_ph(self.master_of[layout], ph_type(ph))
        if msp is not None:
            return rect_of_xfrm(xfrm_el(msp))
        return None

    # ------------- style chains -------------
    def chain(self, shape, layout, lvl):
        """List of <a:lvlNpPr> elements (highest priority first) for a paragraph."""
        name = "a:lvl%dpPr" % (lvl + 1)
        out = []
        tb = shape.find("p:txBody", NS)
        if tb is not None:
            ls = tb.find("a:lstStyle", NS)
            if ls is not None and ls.find(name, NS) is not None:
                out.append(ls.find(name, NS))
        master = self.master_of[layout]
        mroot = self.pkg.xml(master)
        ph = ph_el(shape)
        if ph is None:
            o = mroot.find("p:txStyles/p:otherStyle/" + name, NS)
            if o is not None:
                out.append(o)
            return out
        for holder in (self.layout_ph(layout, ph_key(ph)), self.master_ph(master, ph_type(ph))):
            if holder is None:
                continue
            ls = holder.find("p:txBody/a:lstStyle", NS)
            if ls is not None and ls.find(name, NS) is not None:
                out.append(ls.find(name, NS))
        style = "p:titleStyle" if ph_type(ph) in TITLE_TYPES else "p:bodyStyle"
        o = mroot.find("p:txStyles/%s/%s" % (style, name), NS)
        if o is not None:
            out.append(o)
        return out

    def body_pr(self, shape, layout):
        """Effective bodyPr attributes (shape -> layout ph -> master ph)."""
        res = {}
        holders = [shape]
        ph = ph_el(shape)
        if ph is not None:
            holders += [self.layout_ph(layout, ph_key(ph)),
                        self.master_ph(self.master_of[layout], ph_type(ph))]
        for h in reversed([h for h in holders if h is not None]):
            bp = h.find("p:txBody/a:bodyPr", NS)
            if bp is not None:
                res.update(bp.attrib)
        return res


def para_level(p):
    ppr = p.find("a:pPr", NS)
    try:
        return int(ppr.get("lvl", "0")) if ppr is not None else 0
    except ValueError:
        return 0


def effective_bullet(p, chain):
    """'none' | 'bullet' | 'num' for a paragraph given its style chain."""
    holders = [p.find("a:pPr", NS)] + list(chain)
    for h in holders:
        if h is None:
            continue
        for c in h:
            t = local(c)
            if t == "buNone":
                return "none", c
            if t == "buAutoNum":
                return "num", c
            if t in ("buChar", "buBlip"):
                return "bullet", c
    return "none", None


def chain_attr(chain, attr, default=None):
    for h in chain:
        if h is not None and attr in h.attrib:
            return h.get(attr)
    return default


def chain_defrpr(chain, attr, default=None):
    for h in chain:
        if h is None:
            continue
        d = h.find("a:defRPr", NS)
        if d is not None and attr in d.attrib:
            return d.get(attr)
    return default


def chain_spacing(chain, tag):
    """spcBef/spcAft/lnSpc from a chain -> ('pts', value_pt) or ('pct', fraction) or None."""
    for h in chain:
        if h is None:
            continue
        e = h.find("a:" + tag, NS)
        if e is not None:
            pts = e.find("a:spcPts", NS)
            if pts is not None:
                return ("pts", int(pts.get("val")) / 100.0)
            pct = e.find("a:spcPct", NS)
            if pct is not None:
                return ("pct", int(pct.get("val")) / 100000.0)
    return None


_NUM_RE = re.compile(r"^\s*\d{1,2}[.)]\s+")


def manual_number(text):
    return _NUM_RE.match(text)
