"""Theme handling: lock content colours/fonts/effects to the OLD deck's look.

When a slide moves to a new master, anything written as a *theme reference*
(schemeClr accent2, +mn-lt, <p:style> lnRef idx=2 ...) is re-evaluated against
the new theme.  If the new theme differs, the content silently changes colour.
PowerPoint's "Keep source formatting" avoids that by converting such references
into literal values - this module does the same.
"""
from copy import deepcopy

from lxml import etree

from .package import A, P, NS

SLOTS = ["dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4",
         "accent5", "accent6", "hlink", "folHlink"]
DEFAULT_CLRMAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}


def _c14n(el):
    if el is None:
        return None
    return etree.tostring(el, method="c14n")


class ThemeContext:
    """Theme + colour map in effect for one master."""

    def __init__(self, theme_root, master_root):
        self.colors = {}
        cs = theme_root.find(".//a:clrScheme", NS)
        if cs is not None:
            for c in cs:
                slot = etree.QName(c).localname
                if len(c):
                    v = c[0]
                    self.colors[slot] = (v.get("lastClr") or v.get("val") or "").upper()
        self.fonts = {}
        fs = theme_root.find(".//a:fontScheme", NS)
        if fs is not None:
            for kind, tag in (("mj", "majorFont"), ("mn", "minorFont")):
                f = fs.find("a:" + tag, NS)
                if f is None:
                    continue
                for script in ("latin", "ea", "cs"):
                    e = f.find("a:" + script, NS)
                    self.fonts["+%s-%s" % (kind, {"latin": "lt", "ea": "ea", "cs": "cs"}[script])] = (
                        e.get("typeface") if e is not None else "")
        fmt = theme_root.find(".//a:fmtScheme", NS)
        self.fill_styles = list(fmt.find("a:fillStyleLst", NS)) if fmt is not None else []
        self.bg_fill_styles = list(fmt.find("a:bgFillStyleLst", NS)) if fmt is not None else []
        self.ln_styles = list(fmt.find("a:lnStyleLst", NS)) if fmt is not None else []
        self.effect_styles = list(fmt.find("a:effectStyleLst", NS)) if fmt is not None else []
        self.clrmap = dict(DEFAULT_CLRMAP)
        cm = master_root.find("p:clrMap", NS) if master_root is not None else None
        if cm is not None:
            self.clrmap = dict(cm.attrib)

    def slot(self, val):
        return self.clrmap.get(val, val)

    def color(self, val):
        return self.colors.get(self.slot(val))

    def font(self, ref):
        return self.fonts.get(ref)

    def style_entry(self, kind, idx):
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return None
        if idx <= 0:
            return None
        if kind == "fill":
            if idx >= 1001:
                lst, i = self.bg_fill_styles, idx - 1001
            else:
                lst, i = self.fill_styles, idx - 1
        elif kind == "ln":
            lst, i = self.ln_styles, idx - 1
        else:
            lst, i = self.effect_styles, idx - 1
        return lst[i] if 0 <= i < len(lst) else None


def differs(old, new):
    """Which theme aspects differ between two contexts (for reporting)."""
    out = []
    for s in ["bg1", "tx1", "bg2", "tx2"] + SLOTS[4:]:
        if old.color(s) != new.color(s):
            out.append(s)
    return out


def lock_colors(el, old, new, phclr=None):
    """Replace schemeClr references whose value changes under `new` with srgbClr."""
    n = 0
    for sc in list(el.iter(A + "schemeClr")):
        val = sc.get("val")
        if val == "phClr":
            if phclr is None:
                continue
            hexv = phclr
        else:
            o, nw = old.color(val), new.color(val)
            if not o or o == nw:
                continue
            hexv = o
        sc.tag = A + "srgbClr"
        sc.set("val", hexv)
        n += 1
    return n


def lock_fonts(el, old, new):
    n = 0
    for tag in ("latin", "ea", "cs", "font"):
        for f in el.iter(A + tag):
            tf = f.get("typeface") or ""
            if tf.startswith("+m"):
                o, nw = old.font(tf), new.font(tf)
                if o and o != nw:
                    f.set("typeface", o)
                    for att in ("panose", "pitchFamily", "charset"):
                        if att in f.attrib:
                            del f.attrib[att]
                    n += 1
    return n


# ---- <p:style> baking --------------------------------------------------------
_FILL_TAGS = {A + t for t in ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill")}
_SPPR_ORDER = ["xfrm", "custGeom", "prstGeom", "FILL", "ln", "effectLst", "effectDag", "scene3d",
               "sp3d", "extLst"]
_LN_ORDER = ["FILL", "prstDash", "custDash", "round", "bevel", "miter", "headEnd", "tailEnd", "extLst"]


def _key(tag, order):
    ln = etree.QName(tag).localname
    if A + ln in _FILL_TAGS:
        ln = "FILL"
    return order.index(ln) if ln in order else len(order)


def _insert_ordered(parent, child, order):
    k = _key(child.tag, order)
    for i, c in enumerate(parent):
        if _key(c.tag, order) > k:
            parent.insert(i, child)
            return
    parent.append(child)


def _ref_color(ref, old, new):
    """Resolve the colour carried by a lnRef/fillRef/effectRef in the old context."""
    for c in ref:
        tag = etree.QName(c).localname
        if tag == "schemeClr":
            base = old.color(c.get("val"))
            return base, list(c)
        if tag in ("srgbClr",):
            return c.get("val"), list(c)
        if tag == "sysClr":
            return c.get("lastClr"), list(c)
    return None, []


def _materialise(style_el, ref, old, new):
    """Copy a theme style entry, replacing phClr by the ref's colour (with its mods)."""
    el = deepcopy(style_el)
    hexv, mods = _ref_color(ref, old, new)
    for sc in list(el.iter(A + "schemeClr")):
        if sc.get("val") == "phClr" and hexv:
            sc.tag = A + "srgbClr"
            sc.set("val", hexv)
            # ref modifiers apply before the style's own modifiers
            for i, m in enumerate(mods):
                sc.insert(i, deepcopy(m))
    lock_colors(el, old, new)
    return el


def bake_style_refs(shape, old, new):
    """If a shape's <p:style> would resolve differently in the new theme, write the
    old effective fill / line / effect into spPr explicitly."""
    style = shape.find("p:style", NS)
    sppr = shape.find("p:spPr", NS)
    if style is None or sppr is None:
        return 0
    changed = 0
    ln_ref = style.find("a:lnRef", NS)
    fill_ref = style.find("a:fillRef", NS)
    eff_ref = style.find("a:effectRef", NS)
    # fill
    if fill_ref is not None and not any(c.tag in _FILL_TAGS for c in sppr):
        o = old.style_entry("fill", fill_ref.get("idx"))
        n = new.style_entry("fill", fill_ref.get("idx"))
        if o is not None and _c14n(o) != _c14n(n):
            _insert_ordered(sppr, _materialise(o, fill_ref, old, new), _SPPR_ORDER)
            changed += 1
    # line
    if ln_ref is not None:
        o = old.style_entry("ln", ln_ref.get("idx"))
        n = new.style_entry("ln", ln_ref.get("idx"))
        if o is not None and _c14n(o) != _c14n(n):
            src = _materialise(o, ln_ref, old, new)
            ln = sppr.find("a:ln", NS)
            if ln is None:
                _insert_ordered(sppr, src, _SPPR_ORDER)
            else:
                for att, v in src.attrib.items():
                    if att not in ln.attrib:
                        ln.set(att, v)
                have = {_key(c.tag, _LN_ORDER) for c in ln}
                for c in src:
                    if _key(c.tag, _LN_ORDER) not in have:
                        _insert_ordered(ln, deepcopy(c), _LN_ORDER)
            changed += 1
    # effects
    if eff_ref is not None and sppr.find("a:effectLst", NS) is None and sppr.find("a:effectDag", NS) is None:
        o = old.style_entry("effect", eff_ref.get("idx"))
        n = new.style_entry("effect", eff_ref.get("idx"))
        if o is not None and _c14n(o) != _c14n(n):
            eff = o.find("a:effectLst", NS)
            if eff is not None:
                _insert_ordered(sppr, _materialise(eff, eff_ref, old, new), _SPPR_ORDER)
                changed += 1
    return changed
