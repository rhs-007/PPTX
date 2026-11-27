"""Proves the converted deck kept the content: text, pictures, animations, notes,
hidden flags, transitions and the fonts/colours of the carried-over content.
Also lists things a person should look at (lesson numbers, timings, designer notes).
"""
import collections
import hashlib
import re

from lxml import etree

from .package import A, P, R, NS, Package, RT_NOTES
from .deck import DeckInfo, ph_el, cnvpr, local, text_of

NUM_TOKEN = re.compile(r"^\d{1,2}[.)]$")


def _tokens(text):
    text = text.replace("​", " ").replace("\xa0", " ").replace("\v", " ")
    return [t for t in text.split() if not NUM_TOKEN.match(t)]


def _slide_text(root):
    tree = root.find("p:cSld/p:spTree", NS)
    return "\n".join(text_of(el) for el in tree)


def _images(pkg, slide, root):
    out = collections.Counter()
    for blip in root.iter(A + "blip"):
        rid = blip.get(R + "embed")
        if not rid:
            continue
        tgt = pkg.rel_target(slide, rid)
        if tgt and pkg.exists(tgt):
            out[hashlib.sha1(pkg.raw(tgt)).hexdigest()[:12]] += 1
    return out


def _notes_text(pkg, slide):
    for rel in pkg.rel_list(slide):
        if rel["Type"] == RT_NOTES:
            from .package import resolve_target
            nroot = pkg.xml(resolve_target(slide, rel["Target"]))
            body = []
            for sp in nroot.iter(P + "sp"):
                ph = ph_el(sp)
                if ph is not None and ph.get("type") == "body":
                    body.append(text_of(sp))
            return "\n".join(body)
    return ""


def _effects(root):
    t = root.find("p:timing", NS)
    if t is None:
        return 0, set()
    n = sum(1 for c in t.iter(P + "cTn") if c.get("presetClass"))
    tg = {s.get("spid") for s in t.iter(P + "spTgt")}
    return n, tg


from copy import deepcopy
from .theme import _materialise

_FILLS = ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill")


def _canon(el, ctx):
    """Canonical string for a colour/fill element with theme colours resolved."""
    if el is None:
        return None
    e = deepcopy(el)
    for sc in list(e.iter(A + "schemeClr")):
        v = ctx.color(sc.get("val"))
        if v:
            sc.tag = A + "srgbClr"
            sc.set("val", v)
    for sc in list(e.iter(A + "sysClr")):
        sc.tag = A + "srgbClr"
        sc.set("val", sc.get("lastClr", "000000"))
        for k in ("lastClr",):
            sc.attrib.pop(k, None)
    for x in e.iter():
        for k in list(x.attrib):
            if k in ("dirty", "smtClean", "err", "lang", "altLang", "noProof"):
                del x.attrib[k]
    return etree.tostring(e, method="c14n")


def _style_entry(sp, ctx, kind):
    st = sp.find("p:style", NS)
    if st is None:
        return None
    ref = st.find({"fill": "a:fillRef", "ln": "a:lnRef"}[kind], NS)
    if ref is None:
        return None
    ent = ctx.style_entry(kind, ref.get("idx"))
    if ent is None:
        return None
    return _materialise(ent, ref, ctx, ctx)


def _shape_look(sp, ctx):
    sppr = sp.find("p:spPr", NS)
    fill = None
    if sppr is not None:
        for c in sppr:
            if local(c) in _FILLS:
                fill = c
    if fill is None and local(sp) == "sp":
        fill = _style_entry(sp, ctx, "fill")
    ln = sppr.find("a:ln", NS) if sppr is not None else None
    sln = _style_entry(sp, ctx, "ln")
    lfill = lw = None
    if ln is not None:
        lw = ln.get("w")
        for c in ln:
            if local(c) in _FILLS:
                lfill = c
    if sln is not None:
        lw = lw or sln.get("w")
        if lfill is None:
            for c in sln:
                if local(c) in _FILLS:
                    lfill = c
    if local(sp) == "pic" and ln is None:
        lfill = lw = None
    return (_canon(fill, ctx), _canon(lfill, ctx), lw)


def _inherited(sp, p, master_root, attr=None, child=None):
    """Value for a run property from the shape's own list style, then the master's otherStyle."""
    lvl = 0
    ppr = p.find("a:pPr", NS)
    if ppr is not None:
        try:
            lvl = int(ppr.get("lvl", "0"))
        except ValueError:
            lvl = 0
    name = "a:lvl%dpPr/a:defRPr" % (lvl + 1)
    holders = []
    ls = sp.find("p:txBody/a:lstStyle", NS)
    if ls is not None:
        holders.append(ls.find(name, NS))
    if ppr is not None:
        holders.append(ppr.find("a:defRPr", NS))
    st = sp.find("p:style", NS)
    if st is not None and st.find("a:fontRef", NS) is not None:
        fr = st.find("a:fontRef", NS)
        if child == "solidFill" and len(fr):
            return fr[0]
        if child == "latin":
            return etree.Element(A + "latin", typeface="+%s-lt" % ("mj" if fr.get("idx") == "major" else "mn"))
    if master_root is not None:
        holders.append(master_root.find("p:txStyles/p:otherStyle/" + name, NS))
    for h in holders:
        if h is None:
            continue
        if attr and h.get(attr) is not None:
            return h.get(attr)
        if child and h.find("a:" + child, NS) is not None:
            return h.find("a:" + child, NS)
    return None


def _run_looks(sp, ctx, master_root):
    out = []
    for p in sp.iter(A + "p"):
        for r in p.findall("a:r", NS):
            t = r.find("a:t", NS)
            rp = r.find("a:rPr", NS)
            col = rp.find("a:solidFill", NS) if rp is not None else None
            if col is None:
                col = _inherited(sp, p, master_root, child="solidFill")
            col = _canon(col[0] if col is not None and local(col) == "solidFill" and len(col) else col, ctx)
            lat = rp.find("a:latin", NS) if rp is not None else None
            if lat is None:
                lat = _inherited(sp, p, master_root, child="latin")
            font = lat.get("typeface") if lat is not None else ctx.font("+mn-lt")
            if font and font.startswith("+m"):
                font = ctx.font(font.replace("-ea", "-lt").replace("-cs", "-lt"))
            flags = []
            for k in ("b", "i", "u", "strike"):
                v = rp.get(k) if rp is not None else None
                if v is None:
                    v = _inherited(sp, p, master_root, attr=k)
                flags.append(v if v not in ("0", "none", "noStrike") else None)
            out.append((t.text if t is not None else None, col, font, tuple(flags)))
    return out


class VerifyReport:
    def __init__(self):
        self.rows = []
        self.problems = []
        self.notes = []

    @property
    def ok(self):
        return not self.problems

    def text(self):
        out = ["# Verification (old deck vs converted deck)", ""]
        out.append("| Slide | Text | Pictures | Animations | Notes | Hidden/transition | Content fonts & colours |")
        out.append("|---|---|---|---|---|---|---|")
        out += self.rows
        out.append("")
        if self.problems:
            out += ["## Problems", ""] + ["- " + p for p in self.problems] + [""]
        else:
            out += ["**All content checks passed.**", ""]
        out += ["## Things a person should look at (found in the OLD deck, not changed)", ""]
        out += ["- " + n for n in self.notes] if self.notes else ["- Nothing found."]
        return "\n".join(out) + "\n"


def verify(old_path, new_path, profile, ledger=None):
    old, new = Package(old_path), Package(new_path)
    oi, ni = DeckInfo(old), DeckInfo(new)
    rep = VerifyReport()
    led = ledger.slides if ledger is not None else {}
    if len(oi.slides) != len(ni.slides):
        rep.problems.append("slide count changed: %d -> %d" % (len(oi.slides), len(ni.slides)))
    titles = []
    for n, (os_, ns_) in enumerate(zip(oi.slides, ni.slides), 1):
        oroot, nroot = old.xml(os_), new.xml(ns_)
        cells = []
        # --- text
        L = led.get(n, {})
        ot = collections.Counter(_tokens(_slide_text(oroot)))
        nt = collections.Counter(_tokens(_slide_text(nroot)))
        for lab in L.get("added_text", []):
            nt.subtract(collections.Counter(_tokens(lab)))
        for t in L.get("dropped_text", []):
            ot.subtract(collections.Counter(_tokens(t)))
        nt, ot = +nt, +ot
        missing, added = ot - nt, nt - ot
        if missing or added:
            cells.append("**changed**")
            if missing:
                rep.problems.append("Slide %d: words missing: %s" % (n, " ".join(sorted(missing.elements()))[:200]))
            if added:
                rep.problems.append("Slide %d: words added: %s" % (n, " ".join(sorted(added.elements()))[:200]))
        else:
            cells.append("same (%d words)" % sum(ot.values()))
        # --- pictures
        oim, nim = _images(old, os_, oroot), _images(new, ns_, nroot)
        oim.subtract(collections.Counter(L.get("dropped_images", [])))
        oim = +oim
        if oim == nim:
            cells.append("same (%d)" % sum(oim.values()))
        else:
            cells.append("**changed**")
            rep.problems.append("Slide %d: pictures differ (%d -> %d)" % (n, sum(oim.values()), sum(nim.values())))
        # --- animations
        oe, _ = _effects(oroot)
        oe -= L.get("dropped_effects", 0)
        ne, ntg = _effects(nroot)
        ids = {c.get("id") for c in nroot.iter(P + "cNvPr")}
        dangling = [t for t in ntg if t not in ids]
        if oe == ne and not dangling:
            cells.append("same (%d)" % oe if oe else "none")
        else:
            cells.append("**changed**")
            rep.problems.append("Slide %d: animations %d -> %d%s" % (
                n, oe, ne, (", targets missing: %s" % dangling) if dangling else ""))
        # --- notes
        if _notes_text(old, os_) == _notes_text(new, ns_):
            cells.append("same" if _notes_text(old, os_).strip() else "none")
        else:
            cells.append("**changed**")
            rep.problems.append("Slide %d: speaker notes differ" % n)
        # --- hidden flag + transition
        otr = oroot.find("p:transition", NS)
        ntr = nroot.find("p:transition", NS)
        same_tr = (otr is None and ntr is None) or (otr is not None and ntr is not None and
                                                    etree.tostring(otr, method="c14n") == etree.tostring(ntr, method="c14n"))
        if oroot.get("show") == nroot.get("show") and same_tr:
            cells.append("hidden" if oroot.get("show") == "0" else "same")
        else:
            cells.append("**changed**")
            rep.problems.append("Slide %d: hidden flag or transition changed" % n)
        # --- content look (shapes carried over with the same id)
        octx = oi.ctx[oi.master_of[oi.layout_of(os_)]]
        nctx = ni.ctx[ni.master_of[ni.layout_of(ns_)]]
        omaster = old.xml(oi.master_of[oi.layout_of(os_)])
        nmaster = new.xml(ni.master_of[ni.layout_of(ns_)])
        oshapes = {cnvpr(s).get("id"): s for s in oroot.iter(P + "sp", P + "pic", P + "cxnSp")
                   if ph_el(s) is None and cnvpr(s) is not None}
        nshapes = {cnvpr(s).get("id"): s for s in nroot.iter(P + "sp", P + "pic", P + "cxnSp")
                   if ph_el(s) is None and cnvpr(s) is not None}
        diffs = 0
        checked = 0
        for sid, osp in oshapes.items():
            nsp = nshapes.get(sid)
            if nsp is None:
                continue
            checked += 1
            if _shape_look(osp, octx) != _shape_look(nsp, nctx):
                diffs += 1
                rep.problems.append("Slide %d: fill/line colour of shape '%s' changed" % (n, cnvpr(osp).get("name")))
            oruns = _run_looks(osp, octx, omaster)
            nruns = _run_looks(nsp, nctx, nmaster)
            if oruns != nruns:
                diffs += 1
                rep.problems.append("Slide %d: text look of shape '%s' changed" % (n, cnvpr(osp).get("name")))
        cells.append("same (%d shapes)" % checked if not diffs else "**%d changed**" % diffs)
        rep.rows.append("| %d | %s |" % (n, " | ".join(cells)))
        # --- lint data
        hdr = None
        for s in oroot.iter(P + "sp"):
            ph = ph_el(s)
            nm = (cnvpr(s).get("name") or "") if cnvpr(s) is not None else ""
            if (ph is not None and ph.get("idx") == "10") or nm.startswith("top_bar_box"):
                hdr = text_of(s).strip()
        if hdr:
            titles.append((n, hdr, _notes_text(old, os_), oroot.get("show") == "0"))
    # lesson number consistency
    nums = collections.Counter()
    for n, h, _notes, _hid in titles:
        m = re.search(r"(?:Lesson|Lecci[oó]n)\s+(\d+)", h)
        if m:
            nums[m.group(1)] += 1
    if nums:
        main = nums.most_common(1)[0][0]
        for n, h, _notes, _hid in titles:
            m = re.search(r"(?:Lesson|Lecci[oó]n)\s+(\d+)", h)
            if m and m.group(1) != main:
                rep.notes.append("Slide %d: header says lesson %s but the rest of the deck is lesson %s"
                                 % (n, m.group(1), main))
    # timing in header vs notes
    for n, h, notes, hid in titles:
        m = re.search(r"\((\d+)\s*min", h, re.I)
        k = re.search(r"\((\d+)\s*min", notes.split("\n")[0] if notes else "", re.I)
        if m and k and m.group(1) != k.group(1):
            rep.notes.append("Slide %d: header says %s min but the notes say %s min" % (n, m.group(1), k.group(1)))
    return rep
