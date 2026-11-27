"""The migration engine: old deck + new template + profile -> new deck.

Every top-level shape on an old slide is given a role, from its placeholder,
its name, its picture/text content, and finally its position on the slide:

  header / objective / student / teacher / routine / body / subheader / annos
  content      - the lesson material itself (kept, re-centred, scaled only if needed)
  dnote        - designer / production notes ("Art spec", "FOR PRINT TRS") -> pasteboard
  decor        - old design pasted on the slide (frames, backgrounds, top bar, layout art)
  offslide     - already on the pasteboard: left untouched
  empty        - invisible or empty boxes

Nothing with text is ever dropped silently: every move/drop is written to the report
and to a ledger that the verifier uses.
"""
import hashlib
import re
from copy import deepcopy

from lxml import etree

from .package import (A, P, R, NS, EMU, RT_LAYOUT, RT_MASTER, RT_THEME, RT_IMAGE, RT_NOTES_MASTER,
                      RT_SLIDE, Package, copy_part_tree, resolve_target)
from .deck import (DeckInfo, Rect, ph_el, ph_key, ph_type, cnvpr, xfrm_el, rect_of_xfrm, local,
                   text_of, para_text, spacer, para_level, effective_bullet, chain_defrpr,
                   chain_attr, chain_spacing, manual_number, TITLE_TYPES)
from .theme import lock_colors, lock_fonts, bake_style_refs
from .textfit import Measurer

TOP_SHAPES = {"sp", "pic", "grpSp", "graphicFrame", "cxnSp", "contentPart", "AlternateContent"}
_RPR_ORDER = ["ln", "FILL", "effectLst", "effectDag", "highlight", "uLnTx", "uLn", "uFillTx", "uFill",
              "latin", "ea", "cs", "sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst"]
_PPR_ORDER = ["lnSpc", "spcBef", "spcAft", "buClrTx", "buClr", "buSzTx", "buSzPct", "buSzPts",
              "buFontTx", "buFont", "BULLET", "tabLst", "defRPr", "extLst"]
_FILLS = {"noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill"}
_BULLETS = {"buNone", "buAutoNum", "buChar", "buBlip"}


def E(tag, attrib=None, *children):
    pfx, name = tag.split(":")
    el = etree.Element("{%s}%s" % (NS[pfx], name))
    for k, v in (attrib or {}).items():
        el.set(k, str(v))
    for c in children:
        el.append(c)
    return el


def _order_key(tag, order):
    ln = etree.QName(tag).localname
    if ln in _FILLS and "FILL" in order:
        ln = "FILL"
    if ln in _BULLETS and "BULLET" in order:
        ln = "BULLET"
    return order.index(ln) if ln in order else len(order)


def insert_ordered(parent, child, order):
    k = _order_key(child.tag, order)
    for i, c in enumerate(parent):
        if _order_key(c.tag, order) > k:
            parent.insert(i, child)
            return child
    parent.append(child)
    return child


def emu(inches):
    return int(round(inches * EMU))


def inch(v):
    return v / float(EMU)


def overlap(a, b):
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.r, b.r), min(a.b, b.b)
    return max(0, x1 - x0) * max(0, y1 - y0)


def coverage(a, b):
    """Fraction of rect a covered by rect b."""
    area = float(a.w * a.h) or 1.0
    return overlap(a, b) / area


def iou(a, b):
    i = overlap(a, b)
    u = a.w * a.h + b.w * b.h - i
    return i / float(u) if u else 0.0


def _norm_text(t):
    t = t.replace("​", " ").replace("\xa0", " ").replace("\v", " ").lower()
    return re.findall(r"\w+", t)


# ---------------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.deck = []
        self.lines = []
        self.warnings = []
        self.moves = []

    def slide(self, n, msg):
        self.lines.append("Slide %d: %s" % (n, msg))

    def warn(self, n, msg):
        self.warnings.append("Slide %d: %s" % (n, msg) if n else msg)

    def move(self, n, msg):
        self.moves.append("Slide %d: %s" % (n, msg))

    def text(self):
        out = ["# Conversion report", ""]
        if self.deck:
            out += ["## Whole deck", ""] + ["- " + l for l in self.deck] + [""]
        out += ["## Slides", ""] + ["- " + l for l in self.lines] + [""]
        if self.moves:
            out += ["## Moved, removed or replaced by the template", ""]
            out += ["- " + m for m in self.moves] + [""]
        out += ["## Please check", ""]
        out += ["- " + w for w in self.warnings] if self.warnings else ["- Nothing flagged."]
        return "\n".join(out) + "\n"


class Ledger:
    """What the converter changed on purpose, so the verifier can tell it from loss."""

    def __init__(self):
        self.slides = {}

    def s(self, n):
        return self.slides.setdefault(n, {"added_text": [], "dropped_text": [], "dropped_images": [],
                                          "dropped_effects": 0})


class ShapeInfo:
    def __init__(self, el, conv, layout):
        self.el = el
        self.kind = local(el)
        self.ph = ph_el(el)
        self.key = ph_key(self.ph) if self.ph is not None else None
        c = cnvpr(el)
        self.id = c.get("id") if c is not None else None
        self.name = c.get("name", "") if c is not None else ""
        self.rect = conv.old.inherited_rect(el, layout) if self.ph is not None else rect_of_xfrm(xfrm_el(el))
        self.text = text_of(el)
        self.has_text = not spacer(self.text)
        self.has_pic = any(True for _ in el.iter(P + "pic")) or el.find(".//a:blipFill", NS) is not None
        self.role = None
        sw, sh = conv.slide_w, conv.slide_h
        r = self.rect
        self.offslide = False
        if r is not None:
            if r.r <= 0 or r.x >= sw or r.b <= 0 or r.y >= sh:
                self.offslide = True
            elif not (0 <= r.cx <= sw and 0 <= r.cy <= sh):
                self.offslide = True          # mostly outside the slide
            elif r.w > 0 and r.h > 0:
                vis = (min(r.r, sw) - max(r.x, 0)) * (min(r.b, sh) - max(r.y, 0)) / float(r.w * r.h)
                self.offslide = vis < 0.25


# ---------------------------------------------------------------------------------
class Converter:
    def __init__(self, old_path, template_path, profile, lang="en", labels=None, measurer=None):
        self.src = Package(old_path)          # untouched copy of the old deck
        self.out = Package(old_path)          # becomes the converted deck
        self.tpl = Package(template_path)
        self.profile = profile
        self.lang = lang
        all_labels = profile.get("labels", {})
        self.labels = dict(all_labels.get(lang) or all_labels.get("en") or {})
        if labels:
            self.labels.update({k: v for k, v in labels.items() if v})
        self.report = Report()
        self.ledger = Ledger()
        m = profile.get("metrics", {})
        self.measurer = measurer or Measurer(m.get("font_family", "Century Gothic"), m.get("line_factor", 1.2))
        sz = self.src.xml("ppt/presentation.xml").find("p:sldSz", NS)
        self.slide_w, self.slide_h = int(sz.get("cx")), int(sz.get("cy"))      # old deck (old shapes)
        tsz = self.tpl.xml("ppt/presentation.xml").find("p:sldSz", NS)
        self.new_w, self.new_h = int(tsz.get("cx")), int(tsz.get("cy"))        # template (converted deck)

    # =================================================================
    def run(self, out_path):
        self.old = DeckInfo(self.src)
        self.design_art = self._index_design_art()
        self.import_template()
        self.new = DeckInfo(self.out)
        self.families = self.classify_slides()
        for n, slide in enumerate(self.old.slides, 1):
            self.convert_slide(n, slide)
        if not self.profile.get("copy_comments", False):
            self.drop_comments()
        self.out.remove_unreachable()
        self.out.save(out_path)
        return self.report

    # =================================================================
    # 1. Replace the old design (masters/layouts/themes) with the template's
    def import_template(self):
        out, tpl = self.out, self.tpl
        pres = "ppt/presentation.xml"
        proot = out.xml(pres)
        old = self.old
        keep_themes = set()
        for rel in out.rel_list(pres):
            if rel["Type"] == RT_NOTES_MASTER or rel["Type"].endswith("/handoutMaster"):
                nm = resolve_target(pres, rel["Target"])
                for _rid, th in out.rels_of_type(nm, RT_THEME):
                    keep_themes.add(th)
        for lay in old.layouts:
            out.delete(lay)
        for m in old.masters:
            th = old.theme_of.get(m)
            out.delete(m)
            if th and th not in keep_themes:
                out.delete(th)
        lst = proot.find("p:sldMasterIdLst", NS)
        for m in list(lst):
            out.remove_rel(pres, m.get(R + "id"))
            lst.remove(m)
        troot = tpl.xml(pres)
        name_map = {}
        for m in troot.find("p:sldMasterIdLst", NS):
            src_master = tpl.rel_target(pres, m.get(R + "id"))
            dst_master = copy_part_tree(tpl, src_master, out, name_map, skip_types=(RT_SLIDE,))
            rid = out.add_rel(pres, RT_MASTER, dst_master)
            el = E("p:sldMasterId", {"id": m.get("id")})
            el.set(R + "id", rid)
            lst.append(el)
        tthemes = tpl.rels_of_type(pres, RT_THEME)
        for rid, _th in out.rels_of_type(pres, RT_THEME):
            if tthemes and tthemes[0][1] in name_map:
                out.set_rel_target(pres, rid, name_map[tthemes[0][1]])
        self.use_template_size(proot, troot)

    def use_template_size(self, proot, troot):
        """The converted deck gets exactly the template's slide width and height."""
        osz, tsz = proot.find("p:sldSz", NS), troot.find("p:sldSz", NS)
        before = (int(osz.get("cx")), int(osz.get("cy")))
        for k in ("cx", "cy", "type"):
            if tsz.get(k) is not None:
                osz.set(k, tsz.get(k))
            elif k in osz.attrib:
                del osz.attrib[k]
        after = (self.new_w, self.new_h)
        size = lambda wh: "%.3f x %.3f in" % (wh[0] / 914400.0, wh[1] / 914400.0)
        if before != after:
            self.report.deck.append("Slide size set to the template's %s (the old deck was %s)." % (size(after), size(before)))
        else:
            self.report.deck.append("Slide size: %s, same as the template." % size(after))

    # =================================================================
    # Comments are not carried over to the converted deck
    RT_COMMENTS = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
                   "http://schemas.microsoft.com/office/2018/10/relationships/comments")
    RT_AUTHORS = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors",
                  "http://schemas.microsoft.com/office/2018/10/relationships/authors")

    def drop_comments(self):
        out = self.out
        total, slides = 0, 0
        for n, slide in enumerate(self.old.slides, 1):
            gone = []
            for rel in out.rel_list(slide):
                if rel["Type"] in self.RT_COMMENTS:
                    part = resolve_target(slide, rel["Target"])
                    if out.exists(part):
                        try:
                            croot = out.xml(part)
                            total += sum(1 for el in croot.iter() if isinstance(el.tag, str) and
                                         el.tag.rsplit("}", 1)[-1] == "cm")
                        except Exception:
                            pass
                        out.delete(part)
                    out.remove_rel(slide, rel["Id"])
                    gone.append(rel["Id"])
            if gone:
                slides += 1
                root = out.xml(slide)
                # modern comments are also linked from the slide itself (<p188:commentRel r:id=...>)
                for el in list(root.iter()):
                    if not isinstance(el.tag, str) or el.get(R + "id") not in gone:
                        continue
                    ext = el
                    while ext is not None and local(ext) != "ext":
                        ext = ext.getparent()
                    target = ext if ext is not None else el
                    parent = target.getparent()
                    if parent is None:
                        continue
                    parent.remove(target)
                    if local(parent) == "extLst" and len(parent) == 0 and parent.getparent() is not None:
                        parent.getparent().remove(parent)
                out.set_xml(slide, root)
        pres = "ppt/presentation.xml"
        for rel in out.rel_list(pres):
            if rel["Type"] in self.RT_AUTHORS:
                part = resolve_target(pres, rel["Target"])
                if out.exists(part):
                    out.delete(part)
                out.remove_rel(pres, rel["Id"])
        if slides:
            self.report.deck.append("Comments not copied: %d comment(s) on %d slide(s) were left out of the new deck."
                                    % (total, slides))

    # =================================================================
    # 2. Which kind of slide is each old slide?
    def classify_slides(self):
        fp = self.profile["families"]
        fams = []
        lo_seen = 0
        for n, slide in enumerate(self.old.slides, 1):
            lay = self.old.layout_of(slide)
            name = self.old.layout_name.get(lay, "")
            root = self.src.xml(slide)
            texts = [text_of(s).strip() for s in root.find("p:cSld/p:spTree", NS)]
            fam = "panel"
            if re.search(fp["opening_layout"], name, re.I):
                fam = "opening"
            elif re.search(fp["outcomes1_layout"], name, re.I):
                fam = "outcomes1"
            elif re.search(fp["outcomes2_layout"], name, re.I):
                fam = "outcomes2"
            elif any(re.match(fp["outcomes_text"], t, re.I) for t in texts):
                fam = "outcomes2" if (lo_seen or any(re.search(fp["outcomes2_text"], t, re.I) for t in texts)) \
                    else "outcomes1"
            elif n == 1 and not any(ph_el(s) is not None and ph_key(ph_el(s)) in ("10", "11")
                                    for s in root.find("p:cSld/p:spTree", NS)):
                fam = "opening"
            if fam.startswith("outcomes"):
                lo_seen += 1
            fams.append(fam)
        return fams

    def new_layout_for(self, family):
        pats = self.profile["families"][family + "_new"]
        for pat in pats:
            if pat == "@first":
                return self.new.layouts[0]
            for lay in self.new.layouts:
                if re.search(pat, self.new.layout_name[lay], re.I):
                    return lay
        return self.new.layouts[0]

    # =================================================================
    def convert_slide(self, n, slide):
        old_layout = self.old.layout_of(slide)
        old_name = self.old.layout_name.get(old_layout, "")
        old_master = self.old.master_of[old_layout]
        root = deepcopy(self.src.xml(slide))
        self.ctx_old = self.old.ctx[old_master]
        self.n = n
        fam = self.families[n - 1]
        self._timing_before = self._effect_count(root)
        if fam == "panel":
            new_layout = self.convert_panel(n, root, old_layout, old_name)
        else:
            new_layout = self.new_layout_for(fam)
            self.new_master = self.new.master_of[new_layout]
            self.ctx_new = self.new.ctx[self.new_master]
            self.convert_map(n, root, fam, old_layout, new_layout)
        if root.get("showMasterSp") == "0" and self.profile.get("show_template_graphics", True):
            del root.attrib["showMasterSp"]
            self.report.slide(n, "the old slide hid the design graphics; the new template's design is shown")
        if self.profile.get("drop_slide_background", False):
            bg = root.find("p:cSld/p:bg", NS)
            if bg is not None:
                bg.getparent().remove(bg)
        self.prune_timing(n, root)
        self.out.set_xml(slide, root)
        for rel in self.out.rel_list(slide):
            if rel["Type"] == RT_LAYOUT:
                self.out.set_rel_target(slide, rel["Id"], new_layout)
        self.prune_image_rels(slide, root)
        self.report.slide(n, "'%s' -> '%s'" % (old_name, self.new.layout_name[new_layout]))

    # ---------------------------------------------------------------
    # design-art detection: pictures that are copies of old layout/master artwork
    def _index_design_art(self):
        idx = {}
        for part in self.old.layouts + self.old.masters:
            root = self.src.xml(part)
            for pic in root.iter(P + "pic"):
                h = self._pic_hash(self.src, part, pic)
                r = rect_of_xfrm(pic.find("p:spPr/a:xfrm", NS))
                if h and r is not None:
                    idx.setdefault(h, []).append(r)
        return idx

    @staticmethod
    def _pic_hash(pkg, part, pic):
        b = pic.find(".//a:blip", NS)
        if b is None or not b.get(R + "embed"):
            return None
        tgt = pkg.rel_target(part, b.get(R + "embed"))
        if not tgt or not pkg.exists(tgt):
            return None
        return hashlib.sha1(pkg.raw(tgt)).hexdigest()

    def _is_design_art(self, si, slide):
        if si.kind != "pic" or si.rect is None:
            return False
        h = self._pic_hash(self.src, slide, si.el)
        if h in self.design_art:
            tol = emu(0.3)
            for r in self.design_art[h]:
                if abs(r.x - si.rect.x) < tol and abs(r.y - si.rect.y) < tol and abs(r.w - si.rect.w) < tol:
                    return True
        return False

    def _is_background(self, si):
        r = si.rect
        return r is not None and r.w * r.h >= 0.8 * self.slide_w * self.slide_h

    def _is_top_bar(self, si):
        r = si.rect
        return (r is not None and not si.has_text and not si.has_pic and r.w >= 0.9 * self.slide_w
                and r.h <= emu(0.35) and r.y <= emu(0.2))

    def _visible(self, el):
        tag = local(el)
        if tag in ("pic", "graphicFrame", "contentPart"):
            return True
        if tag in ("grpSp", "AlternateContent"):
            return any(self._visible(c) for c in el if local(c) in TOP_SHAPES)
        if not spacer(text_of(el)):
            return True
        sppr = el.find("p:spPr", NS)
        style = el.find("p:style", NS)
        fill = None
        if sppr is not None:
            for c in sppr:
                if local(c) in _FILLS:
                    fill = local(c)
        if fill and fill != "noFill":
            return True
        if fill is None and style is not None:
            fr = style.find("a:fillRef", NS)
            if fr is not None and fr.get("idx", "0") != "0":
                return True
        ln = sppr.find("a:ln", NS) if sppr is not None else None
        if ln is not None:
            if ln.find("a:noFill", NS) is not None:
                return False
            if any(local(c) in _FILLS for c in ln):
                return True
        if style is not None:
            lr = style.find("a:lnRef", NS)
            if lr is not None and lr.get("idx", "0") != "0" and (ln is None or ln.find("a:noFill", NS) is None):
                return True
        return False

    def _is_frame(self, si):
        """Empty, outline-or-white boxes (or groups of them) - the old design's frames."""
        if si.has_text or si.has_pic or si.rect is None:
            return False
        r = si.rect
        if r.w < emu(1.0) or r.h < emu(0.5):
            return False
        shapes = [si.el] if si.kind == "sp" else list(si.el.iter(P + "sp"))
        if not shapes:
            return False
        for sp in shapes:
            g = sp.find("p:spPr/a:prstGeom", NS)
            if g is None or g.get("prst") not in ("rect", "roundRect", "snip1Rect", "round2SameRect"):
                return False
            f = sp.find("p:spPr/a:solidFill", NS)
            if f is not None and len(f):
                c = f[0]
                v = c.get("val", "")
                if local(c) == "schemeClr" and v not in ("bg1", "lt1"):
                    return False
                if local(c) == "srgbClr" and v.upper() not in ("FFFFFF",):
                    return False
            for tag in ("gradFill", "blipFill", "pattFill"):
                if sp.find("p:spPr/a:" + tag, NS) is not None:
                    return False
        return True

    def _is_dnote(self, si):
        if not si.has_text:
            return False
        pf = self.profile
        if re.search(pf.get("designer_note_regex", r"$^"), si.text):
            return True
        # mostly red text outside the lesson content area
        red = total = 0
        for r in si.el.iter(A + "r"):
            t = (r.find("a:t", NS).text or "") if r.find("a:t", NS) is not None else ""
            n = len(t.strip())
            if not n:
                continue
            total += n
            f = r.find("a:rPr/a:solidFill/a:srgbClr", NS)
            if f is not None:
                v = f.get("val", "000000")
                try:
                    rr, gg, bb = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
                except ValueError:
                    continue
                if rr >= 0xB0 and gg <= 0x70 and bb <= 0x70:
                    red += n
        frame = Rect(*[emu(v) for v in pf["panel"]["old_content_frame_in"]])
        if not (total > 0 and red >= 0.6 * total) or si.rect is None:
            return False
        if coverage(si.rect, frame) < 0.5:
            return True
        return bool(re.search(pf.get("designer_note_red_regex", r"$^"), si.text))

    # =================================================================
    # MAP MODE: opening / lesson-outcome slides -> placeholders matched by position
    def convert_map(self, n, root, fam, old_layout, new_layout):
        spTree = root.find("p:cSld/p:spTree", NS)
        slide = self.old.slides[n - 1]
        shapes = [ShapeInfo(c, self, old_layout) for c in spTree if local(c) in TOP_SHAPES]
        led = self.ledger.s(n)
        # targets: text placeholders of the new layout
        targets = []
        for sp in self.new.pkg.xml(new_layout).find("p:cSld/p:spTree", NS):
            ph = ph_el(sp)
            if ph is None or ph.get("type") == "pic":
                continue
            r = self.new.inherited_rect(sp, new_layout)
            if r is not None:
                targets.append((ph_key(ph), r))
        static = self._static_text_shapes(new_layout)
        sources, kept = [], []
        kept_off = self._drop_offslide(n, root, [si for si in shapes if si.offslide])
        for si in shapes:
            if si.offslide:
                if si in kept_off:
                    self.lock_content(si.el, old_layout)
                else:
                    spTree.remove(si.el)
                continue
            if not si.has_text:
                if not self._visible(si.el):
                    spTree.remove(si.el)
                elif self.profile.get("map_drop_non_text", True):
                    self._drop(n, si, "old design art on a title/outcome slide (the template supplies its own)")
                    spTree.remove(si.el)
                else:
                    self.lock_content(si.el, old_layout)
                continue
            if self._is_dnote(si):
                if self.profile.get("designer_notes", "under_collaborate") == "pasteboard":
                    self._to_pasteboard(n, si)
                else:
                    self._keep_note_inside(n, si)
                self.lock_content(si.el, old_layout)
                continue
            dup = self._dup_static(si, static)
            if dup is not None:
                self._drop(n, si, "replaced by the template's own text '%s'" % dup[:60], text=True)
                spTree.remove(si.el)
                continue
            sources.append(si)
        # one-to-one matching by overlap (+ small bonus for same placeholder number)
        cand = []
        for si in sources:
            if si.rect is None:
                continue
            for key, tr in targets:
                score = coverage(si.rect, tr) + (0.25 if si.key == key else 0.0)
                d = ((si.rect.cx - tr.cx) ** 2 + (si.rect.cy - tr.cy) ** 2) ** 0.5
                if score <= 0 and d > emu(1.5):
                    continue
                cand.append((score, -d, id(si), si, key))
        cand.sort(key=lambda c: (c[0], c[1]), reverse=True)
        used_t, used_s = set(), set()
        assign = {}
        for score, _d, sid, si, key in cand:
            if sid in used_s or key in used_t:
                continue
            assign[sid] = key
            used_s.add(sid)
            used_t.add(key)
        for si in sources:
            key = assign.get(id(si))
            if key is None:
                self.keep_as_textbox(n, si.el, old_layout, "text has no matching box in the new layout")
                continue
            if key == "title":
                new_el, time_text = self.build_title(si.el, int(si.id), None, new_layout)
                si.el.addprevious(new_el)
                spTree.remove(si.el)
                if time_text:
                    self.report.warn(n, "time '%s' kept in the title (this layout has no time box)" % time_text)
                    self._title_append(new_el, " " + time_text)
                continue
            if si.ph is not None:
                self.retarget_placeholder(si.el, new_layout, key)
                self.lock_content(si.el, old_layout)
                new_el = si.el
            else:
                paras = self.framework_paras(si.el, old_layout, 0, drop_empty=True, role="map")
                new_el = self.make_ph(new_layout, key, int(si.id), paras)
                si.el.addprevious(new_el)
                spTree.remove(si.el)
            if len(si.text.split("\n")) > 2:
                self._fit_in_box(n, new_el, new_layout, key)
            if len(si.text.split()) == 1:
                # one word / number (e.g. lesson "12"): never break it inside a narrow box
                bp = new_el.find("p:txBody/a:bodyPr", NS)
                if bp is not None:
                    bp.set("wrap", "none")
                self._widen_for_word(new_el, new_layout, key, si.text.strip())

    def _widen_for_word(self, el, layout, key, word):
        lsp = self.new.layout_ph(layout, key)
        r = self.new.inherited_rect(lsp, layout)
        if r is None:
            return
        chain = self.new.chain(lsp, layout, 0)
        size = int(chain_defrpr(chain, "sz", "1800")) / 100.0
        bold = chain_defrpr(chain, "b", "0") == "1"
        bp = self.new.body_pr(lsp, layout)
        need = self.measurer.width(word, size, bold) * 1.15 + (int(bp.get("lIns", 91440)) +
                                                               int(bp.get("rIns", 91440))) / EMU
        if need <= inch(r.w):
            return
        sppr = el.find("p:spPr", NS)
        for c in list(sppr):
            if local(c) in ("xfrm", "prstGeom"):
                sppr.remove(c)
        sppr.insert(0, E("a:xfrm", {}, E("a:off", {"x": r.x, "y": r.y}), E("a:ext", {"cx": emu(need), "cy": r.h})))
        sppr.insert(1, E("a:prstGeom", {"prst": "rect"}, E("a:avLst")))

    def _static_text_shapes(self, layout):
        out = []
        for part in (layout, self.new.master_of[layout]):
            for sp in self.new.pkg.xml(part).find("p:cSld/p:spTree", NS).iter(P + "sp"):
                if ph_el(sp) is None and not spacer(text_of(sp)):
                    r = rect_of_xfrm(xfrm_el(sp))
                    if r is not None:
                        out.append((text_of(sp).strip(), r))
        return out

    def _dup_static(self, si, static):
        if si.rect is None:
            return None
        mine = set(_norm_text(si.text))
        for txt, r in static:
            theirs = set(_norm_text(txt))
            j = len(mine & theirs) / float(len(mine | theirs) or 1)
            if j >= 0.85 or (coverage(si.rect, r) >= 0.4 and j >= 0.3) or coverage(r, si.rect) >= 0.6:
                return txt.replace("\n", " ")
        return None

    def _title_append(self, el, text):
        p = el.find("p:txBody/a:p", NS)
        r = E("a:r", {}, E("a:rPr", {"lang": "en-US", "b": "0"}), E("a:t"))
        r.find("a:t", NS).text = text
        _preserve_space(r.find("a:t", NS))
        p.append(r)

    # =================================================================
    # PANEL MODE: teaching slides
    def convert_panel(self, n, root, old_layout, old_name):
        pf = self.profile["panel"]
        roles_by_idx = pf["old_roles"]
        roles_by_name = pf.get("roles_by_name", {})
        spTree = root.find("p:cSld/p:spTree", NS)
        slide = self.old.slides[n - 1]
        shapes = [ShapeInfo(c, self, old_layout) for c in spTree if local(c) in TOP_SHAPES]
        led = self.ledger.s(n)
        frame = Rect(*[emu(v) for v in pf["old_content_frame_in"]])
        old_frames = [Rect(*[emu(v) for v in f]) for f in pf.get("old_frames_in", [])] + [frame]

        # --- 1st pass: placeholders, names, obvious kinds ----------------------------------
        for si in shapes:
            if si.offslide:
                si.role = "offslide"
            elif si.ph is not None:
                si.role = roles_by_idx.get(si.key, "unknown")
                if si.role == "unknown" and si.has_text and self._is_dnote(si):
                    si.role = "dnote"
            else:
                for pat, role in roles_by_name.items():
                    if re.search(pat, si.name, re.I):
                        si.role = role
                        break
                if si.role is None:
                    if self._is_design_art(si, slide) or self._is_top_bar(si):
                        si.role = "decor"
                    elif self._is_dnote(si):
                        si.role = "dnote"
                    elif not self._visible(si.el):
                        si.role = "empty"
            if si.role is None and si.has_text and si.ph is None and re.match(
                    r"(?i)^\s*\(?\s*(x|\d+)\s*min\.?\s*\)?\s*$", si.text.strip()):
                si.role = "time_alt"
            if si.role in ("header", "objective", "teacher", "routine") and not si.has_text and si.ph is None:
                si.role = "decor" if self._visible(si.el) else "empty"
            if si.role in ("dnote", "time_alt") and not si.has_text:
                si.role = "empty"
            if si.role == "time_alt" and not re.match(r"(?i)^\s*\(?\s*(x|\d+)\s*(min|minutos?|minutes?)\.?\s*\)?\s*$",
                                                      si.text.strip()):
                si.role = "dnote" if self._is_dnote(si) else None     # real text typed into the time box

        def ref(role, idx):
            for si in shapes:
                if si.role == role and si.rect is not None and (si.ph is not None or si.has_text):
                    return si.rect
            lsp = self.old.layout_ph(old_layout, idx)
            return rect_of_xfrm(xfrm_el(lsp)) if lsp is not None else None

        g = pf["regions"]
        obj = ref("objective", g["objective"]) or Rect(*[emu(v) for v in pf["old_objective_in"]])
        left_edge, right_edge = frame.x, frame.r
        # student box: its placeholder / named box / the outline drawn around it
        stu_rects = [si.rect for si in shapes if si.role == "student" and si.rect is not None
                     and (si.ph is not None or si.kind == "sp")]
        for si in shapes:
            r = si.rect
            if si.role in (None, "decor", "empty") and r is not None and r.cx > right_edge and r.y < emu(2.2) \
                    and not si.has_pic and self._visible(si.el) and r.w >= emu(1.5) and r.h >= emu(0.4) \
                    and (si.kind == "sp" or self._is_frame(si)) and not si.has_text:
                stu_rects.append(r)
        stu = self._union(stu_rects) if stu_rects else ref("student", g["student"])
        tea = ref("teacher", g["teacher"])
        tea_present = any(si.role == "teacher" and si.has_text for si in shapes)
        split_y = emu(pf.get("right_column_split_in", 4.9))
        routine_top = emu(pf.get("routine_min_top_in", 3.0))

        # --- 2nd pass: geometry for everything still unknown ------------------------------
        for si in shapes:
            if si.role is not None:
                continue
            r = si.rect
            if r is None:
                si.role = "content"
                continue
            if self._is_frame(si) and (any(iou(r, f) >= 0.6 for f in old_frames) or
                                       (r.cx > right_edge and r.y < emu(2.2)) or coverage(r, obj) >= 0.6):
                si.role = "decor"
            elif si.has_text and r.y < emu(0.4) and r.h < emu(0.5) and r.x < emu(1.0):
                si.role = "header"
            elif si.has_text and iou(r, obj) >= 0.5:
                si.role = "objective"
            elif r.cx > right_edge:
                if stu is not None and (coverage(r, stu) >= 0.5 or r.y < stu.b + emu(0.1)) and \
                        (tea is None or r.cy < tea.y or (coverage(r, stu) >= 0.8 and not tea_present)):
                    si.role = "student"
                elif tea is not None and r.cy >= tea.y - emu(0.3):
                    si.role = "teacher"
                elif r.cy < split_y and not (si.kind == "pic" and r.h <= emu(0.45) and r.y > emu(4.5)):
                    si.role = "student"
                else:
                    si.role = "teacher"
            elif r.cx < left_edge:
                if si.has_text:
                    si.role = "dnote" if self._is_dnote(si) else "routine"
                else:
                    si.role = "content"
            else:
                si.role = "content"

        buckets = {}
        for si in shapes:
            buckets.setdefault(si.role, []).append(si)
        B = lambda k: buckets.get(k, [])

        def has_txt(k):
            return any(si.has_text for si in B(k))

        # --- choose the new layout --------------------------------------------------------------
        new_layout, rule = None, None
        for rl in pf["layouts"]:
            if not re.search(rl["old_layout"], old_name, re.I):
                continue
            need = rl.get("needs")
            if need and not any(has_txt(k) for k in need.split("|")):
                continue
            lay = self.new.layout_by_name(rl["new_layout"])
            if lay is not None:
                new_layout, rule = lay, rl
                break
        if new_layout is None:
            new_layout = self.new.layout_by_name("Image only") or self.new.layouts[-1]
            rule = {"new_layout": "Image only"}
        self.new_master = self.new.master_of[new_layout]
        self.ctx_new = self.new.ctx[self.new_master]
        nl = new_layout
        newp = pf["new_placeholders"]
        levels = pf["levels"]
        targets = self._new_targets(nl)

        ids = [int(c.get("id")) for c in root.iter(P + "cNvPr") if c.get("id", "").isdigit()]
        next_id = [max(ids + [1]) + 1]

        def new_id():
            next_id[0] += 1
            return next_id[0] - 1

        built = []

        # ---- title (+ time) --------------------------------------------------------------------
        hdr = [si for si in B("header") if si.has_text]
        time_alt = [si for si in B("time_alt") if si.has_text]
        if not hdr:
            self.report.warn(n, "this slide has no header text in the old deck, so its title bar is empty")
        if hdr and self.new.layout_ph(nl, "title") is not None:
            src = hdr[0]
            title_el, time_text = self.build_title(src.el, int(src.id), None, nl)
            built.append(title_el)
            if not time_text:
                for si in time_alt:
                    m = re.search(self.profile.get("time_pattern", "$^"), " " + si.text.strip(), re.I)
                    if m:
                        time_text = m.group(1).strip()
                        time_alt.remove(si)
                        break
            if time_text and self.new.layout_ph(nl, newp["time"]) is not None:
                built.append(self.make_ph(nl, newp["time"], new_id(), [self.simple_para(time_text)]))
            for extra in hdr[1:]:
                buckets.setdefault("dnote", []).append(extra)
                self.report.warn(n, "a second header was found: '%s' (placed with the designer notes)"
                                 % extra.text.strip()[:60])
        header_time = None
        for el in built:
            for t in el.iter(A + "t"):
                m = re.search(r"(\d+)\s*min", t.text or "", re.I)
                if m:
                    header_time = m.group(1)
        for si in time_alt:
            m = re.search(r"(?i)(x|\d+)\s*min", si.text)
            if m and m.group(1).lower() == "x":
                self._drop(n, si, "the old template's unfilled time box (the new header has the time)", text=True)
            elif m and header_time is not None:
                self._drop(n, si, "same time as in the new header", text=True)
            else:
                buckets.setdefault("dnote", []).append(si)

        # ---- body / header / annotation placeholders -----------------------------------------------
        for role in ("subheader", "body", "annos"):
            for si in B(role):
                if not si.has_text:
                    continue
                tkey = targets.get(role)
                if tkey and si.ph is not None:
                    self.retarget_placeholder(si.el, nl, tkey)
                    self.lock_content(si.el, old_layout)
                    built.append(si.el)
                elif tkey:
                    paras = self.framework_paras(si.el, old_layout, 0, drop_empty=False, role="body")
                    built.append(self.make_ph(nl, tkey, int(si.id), paras))
                else:
                    buckets.setdefault("unknown", []).append(si)

        # ---- right panel: Learning Outcomes + Teach ---------------------------------------------------
        obj_paras = []
        for si in B("objective"):
            obj_paras += self.framework_paras(si.el, old_layout, levels["objective"], drop_empty=True,
                                              role="objective")
        teach_sources = sorted(B("teacher"), key=lambda s: (s.rect.y if s.rect is not None else 0))
        teach_paras, badges = [], []
        for si in teach_sources:
            if si.kind == "sp" and si.el.find("p:txBody", NS) is not None and (si.has_text or si.ph is not None):
                teach_paras += self.framework_paras(si.el, old_layout, levels["teacher"], drop_empty=True,
                                                    role="teacher")
            elif si.ph is None and self._visible(si.el):
                badges.append(si)
        out_paras = []
        teach_start = None
        right_top = None
        if obj_paras:
            out_paras.append(self.simple_para(self.labels["outcomes"], levels["outcomes_heading"]))
            led["added_text"].append(self.labels["outcomes"])
            out_paras += obj_paras
        if teach_paras or badges:
            out_paras.append(self.simple_para(self.labels["teach"], levels["teach_heading"]))
            led["added_text"].append(self.labels["teach"])
            if not teach_paras:
                teach_paras = [self.simple_para("      ", lvl=levels["teacher"])]
            teach_start = len(out_paras)
            out_paras += teach_paras
        if out_paras and self.new.layout_ph(nl, newp["outcomes"]) is not None:
            srcs = [si for si in B("objective") if si.id] or [si for si in B("teacher") if si.ph is not None]
            sid = int(srcs[0].id) if srcs else new_id()
            outcomes_el = self.make_ph(nl, newp["outcomes"], sid, out_paras)
            box, blocks = self.autofit(outcomes_el, nl, newp["outcomes"], max_bottom=inch(self.new_h) - 0.1, n=n)
            built.append(outcomes_el)
            if badges:
                tgt = self._badge_paragraph(out_paras, teach_start)
                self._place_badges([b.el for b in badges], box, blocks[tgt], nl, newp["outcomes"])
            right_top = max([inch(box.b)] + [inch(rect_of_xfrm(xfrm_el(b.el)).b) for b in badges
                                             if rect_of_xfrm(xfrm_el(b.el)) is not None]) + 0.12

        # ---- routine ---------------------------------------------------------------------------------
        routine_top = inch(self.new_h) - 0.1
        rsrc = [si for si in B("routine") if si.has_text]
        if rsrc and self.new.layout_ph(nl, newp["routine"]) is not None:
            rparas = []
            for si in sorted(rsrc, key=lambda s: s.rect.y if s.rect is not None else 0):
                rparas += self.routine_paras(si.el, old_layout, levels)
            if rparas:
                r_el = self.make_ph(nl, newp["routine"], int(rsrc[0].id), rparas)
                rbox, _ = self.autofit(r_el, nl, newp["routine"], anchor_bottom=True,
                                       max_bottom=inch(self.new_h) * 0.45, n=n)
                routine_top = inch(rbox.y)
                built.append(r_el)

        # ---- left panel: student box ("Collaborate") -------------------------------------------------
        stu_ph = [si for si in B("student") if si.ph is not None]
        stu_tb = sorted([si for si in B("student") if si.ph is None and si.kind == "sp" and si.has_text],
                        key=lambda s: s.rect.y if s.rect is not None else 0)
        stu_tb_ids = {id(s) for s in stu_tb}
        icons = [si for si in B("student") if si.ph is None and id(si) not in stu_tb_ids
                 and self._visible(si.el) and not (si.kind == "sp" and not si.has_pic and self._is_frame(si))]
        stu_txt_sources = stu_ph + [si for si in B("student") if si.ph is None and si.kind == "sp"
                                    and not si.has_text and si.el.find("p:txBody", NS) is not None
                                    and id(si) not in {id(i) for i in icons}]
        notes_top = None
        has_student = any(si.has_text for si in B("student")) or icons
        if has_student and self.new.layout_ph(nl, newp["student"]) is not None:
            sparas = []
            for si in stu_txt_sources:
                sparas += self.framework_paras(si.el, old_layout, levels["student"], drop_empty=False,
                                               role="student")
            if stu_tb:
                while len(sparas) > 1 and spacer(para_text(sparas[-1])):
                    sparas.pop()
                for si in stu_tb:
                    ps = self.framework_paras(si.el, old_layout, levels["student"], drop_empty=False,
                                              role="student")
                    while ps and spacer(para_text(ps[0])):
                        ps.pop(0)
                    sparas += ps
            if not icons:
                while sparas and spacer(para_text(sparas[-1])):
                    sparas.pop()
            else:
                # blank lines only made room for the icons; the icons now get their own room
                while len(sparas) > 1 and spacer(para_text(sparas[0])) and not spacer(
                        "".join(para_text(p) for p in sparas)):
                    sparas.pop(0)
                squeezed = []
                for p in sparas:
                    if squeezed and spacer(para_text(p)) and spacer(para_text(squeezed[-1])):
                        continue
                    squeezed.append(p)
                while len(squeezed) > 1 and spacer(para_text(squeezed[-1])):
                    squeezed.pop()
                sparas = squeezed
            if icons and not sparas:
                sparas = [self._spacer_para(levels["student"])]
            paras = [self.simple_para(self.labels["student"], levels["student_heading"])] + sparas
            led["added_text"].append(self.labels["student"])
            sid = int(stu_ph[0].id) if stu_ph else new_id()
            stu_el = self.make_ph(nl, newp["student"], sid, paras)
            box, blocks = self.autofit(stu_el, nl, newp["student"], max_bottom=routine_top - 0.1, n=n)
            built.append(stu_el)
            if icons:
                old_box = stu or self._union([si.rect for si in icons if si.rect is not None])
                text_top = blocks[1]["top"] if len(blocks) > 1 else inch(box.b)
                self._place_icons([si.el for si in icons], old_box, box, text_top, routine_top - 0.1)
                box, blocks = self.autofit(stu_el, nl, newp["student"],
                                           avoid=[rect_of_xfrm(xfrm_el(si.el)) for si in icons],
                                           max_bottom=routine_top - 0.1, n=n)
            bottoms = [inch(box.b)] + [inch(rect_of_xfrm(xfrm_el(si.el)).b) for si in icons
                                       if rect_of_xfrm(xfrm_el(si.el)) is not None]
            notes_top = max(bottoms) + 0.12
        else:
            icons = []


        # ---- anything with text that has no home: never lose it -------------------------------------------
        for role in ("drop", "unknown"):
            for si in B(role):
                if si.has_text:
                    if si.ph is not None and si.key in pf.get("dnote_placeholders", []):
                        buckets.setdefault("dnote", []).append(si)
                    else:
                        self.keep_as_textbox(n, si.el, old_layout, "text with no matching box in the new layout")
                        buckets.setdefault("content", []).append(si)
        dnotes = [si for si in B("dnote")]
        for si in dnotes:
            if si.ph is not None:
                self.keep_as_textbox(n, si.el, old_layout, None)
                si.rect = rect_of_xfrm(xfrm_el(si.el))

        # ---- decorations: report what was removed ------------------------------------------------------
        for si in B("decor"):
            self._drop(n, si, "old design element (frame / background / top bar / layout art)")

        # ---- content: fit + centre into the new content area -------------------------------------------
        content = [si for si in B("content")]
        area = targets.get("area")
        frame_rect = None
        if rule and rule.get("needs"):
            olds = [si.rect for k in ("body", "subheader", "annos") for si in B(k) if si.rect is not None]
            if olds:
                frame_rect = self._union(olds)
        if content and area is not None:
            self._fit_content(n, [si.el for si in content], area, old_layout, pf, frame_rect)
        offslide = self._drop_offslide(n, root, B("offslide"))
        for si in content + icons + badges + dnotes + offslide:
            self.lock_content(si.el, old_layout)
        if self.profile.get("designer_notes", "under_collaborate") == "pasteboard":
            self._stack_pasteboard(n, dnotes)
        else:
            self._place_notes(n, dnotes, nl, (notes_top, routine_top - 0.1), (right_top, inch(self.new_h) - 0.1))

        # ---- rebuild the shape tree --------------------------------------------------------------------
        keep_old = set(map(id, [si.el for si in content + icons + badges + dnotes + offslide]))
        for c in list(spTree):
            if local(c) in TOP_SHAPES:
                spTree.remove(c)
        for el in built:
            spTree.append(el)
        for si in shapes:
            if id(si.el) in keep_old:
                spTree.append(si.el)
        return new_layout

    # ------------------------------------------------------------------
    def _new_targets(self, layout):
        """Which placeholder of a new layout is the body / sub-header / annotations / content area."""
        pf = self.profile["panel"]
        panel = set(pf["new_placeholders"].values()) | {"title"}
        phs = []
        for sp in self.new.pkg.xml(layout).find("p:cSld/p:spTree", NS):
            ph = ph_el(sp)
            if ph is None:
                continue
            r = self.new.inherited_rect(sp, layout)
            if r is None:
                continue
            phs.append((ph_key(ph), ph.get("type", "body"), r))
        time_r = next((r for k, t, r in phs if k == pf["new_placeholders"]["time"]), None)
        others = [(k, t, r) for k, t, r in phs if k not in panel and t != "pic"
                  and not (time_r is not None and iou(r, time_r) > 0.8)]
        pics = [(k, t, r) for k, t, r in phs if t == "pic"]
        out = {}
        if others:
            body = max(others, key=lambda x: x[2].w * x[2].h)
            out["body"] = body[0]
            rest = [o for o in others if o is not body]
            for k, t, r in rest:
                if r.h < emu(0.8) and r.b <= body[2].y + emu(0.15) and "subheader" not in out:
                    out["subheader"] = k
                elif r.y >= body[2].b - emu(0.2) and "annos" not in out:
                    out["annos"] = k
            rects = [r for k, t, r in others]
            out["area"] = self._union(rects)
        if pics:
            out["area"] = pics[0][2]
        return out

    def _drop(self, n, si, why, text=False):
        led = self.ledger.s(n)
        for pic in si.el.iter(P + "pic"):          # includes si.el itself when it is a picture
            h = self._pic_hash(self.src, self.old.slides[n - 1], pic)
            if h:
                led["dropped_images"].append(h[:12])
        if si.has_text:
            led["dropped_text"].append(si.text)
        what = ("'%s'" % si.text.strip().replace("\n", " ")[:60]) if si.has_text else ("'%s'" % si.name)
        self.report.move(n, "removed %s - %s" % (what, why))

    def _drop_offslide(self, n, root, items):
        """Items that sat outside the old slide are not carried over. Returns the ones kept
        because an animation brings them onto the slide during the show."""
        animated = {t.get("spid") for t in root.iter(P + "spTgt")}
        kept = []
        for si in items:
            ids = {c.get("id") for c in si.el.iter(P + "cNvPr")}
            if ids & animated:
                kept.append(si)
                self.report.warn(n, "an item outside the slide is animated, so it was kept: '%s'"
                                 % (si.text.strip()[:60] or si.name))
                continue
            self._drop(n, si, "it was outside the slide in the old deck", text=True)
        return kept

    def _keep_note_inside(self, n, si):
        """Opening / outcome slides have no Collaborate box: the note stays where it was, inside the slide."""
        r = rect_of_xfrm(xfrm_el(si.el)) or si.rect
        if r is None:
            return
        x = min(max(r.x, emu(0.05)), max(emu(0.05), self.new_w - r.w - emu(0.05)))
        y = min(max(r.y, emu(0.05)), max(emu(0.05), self.new_h - r.h - emu(0.05)))
        if (x, y) != (r.x, r.y):
            _move(si.el, x, y)
        self.report.move(n, "designer note kept on the slide: '%s'" % si.text.strip().replace("\n", " ")[:70])

    # ---------------------------------------------------------------
    # Designer notes go in the left column, under the Collaborate box (above the routine box)
    def _place_notes(self, n, items, layout, left, right):
        """Under the Collaborate box (left column, above the routine). If the notes don't fit there at a
        readable size, they go in the right column under Learning Outcomes / Teach instead."""
        if not items:
            return
        newp = self.profile["panel"]["new_placeholders"]
        regions = []
        for key, (top, bottom), where in ((newp["student"], left, "under the Collaborate box"),
                                          (newp["outcomes"], right, "in the right column, under the Teach box")):
            sp = self.new.layout_ph(layout, key)
            col = self.new.inherited_rect(sp, layout) if sp is not None else None
            if col is None:
                continue
            if top is None:
                top = inch(col.y)
                if key == newp["student"]:
                    where = "in the left column (this slide has no Collaborate box)"
            regions.append(dict(x=col.x, w=col.w, top=top, avail=max(0.2, bottom - top), where=where))
        if not regions:
            self._stack_pasteboard(n, items)
            return
        items = sorted(items, key=lambda si: ((si.rect.y, si.rect.x) if si.rect is not None else (0, 0)))
        gap = 0.08
        for si in items:
            self._prep_note(si.el)
        bases = [self._note_sizes(si.el) for si in items]
        smallest = min([v for b in bases for runs, end in b for v in runs + [end]] or [1000])
        readable = max(0.6, min(1.0, 700.0 / smallest))       # never below 7 pt unless it must

        def fit(region):
            scale = 1.0
            for _ in range(16):
                hs = [self._note_height(si.el, inch(region["w"]), b, scale) for si, b in zip(items, bases)]
                total = sum(hs) + gap * (len(hs) - 1)
                if total <= region["avail"] + 0.005 or scale <= 0.35:
                    return scale, hs, total
                scale = max(0.35, scale * max(0.7, (region["avail"] / total) ** 0.5) - 0.01)
            return scale, hs, total

        fits = [(r,) + fit(r) for r in regions]
        good = [f for f in fits if f[1] >= readable and f[3] <= f[0]["avail"] + 0.005]
        region, scale, heights, total = good[0] if good else max(fits, key=lambda f: (f[3] <= f[0]["avail"] + 0.005,
                                                                                         f[1]))
        for si, b in zip(items, bases):
            self._apply_note_sizes(si.el, b, scale)
        y = region["top"]
        for si, h in zip(items, heights):
            _set_rect(si.el, region["x"], emu(y), region["w"], emu(h))
            y += h + gap
        size_note = "" if scale >= 0.999 else " (text at %d%% size to fit)" % round(scale * 100)
        why = "" if region is regions[0] else " - no room in the left column"
        for si in items:
            self.report.move(n, "designer note placed %s%s%s: '%s'"
                             % (region["where"], size_note, why, si.text.strip().replace("\n", " ")[:70]))
        if total > region["avail"] + 0.05:
            self.report.warn(n, "the designer notes are longer than the free space %s, so they overlap "
                                "other text there" % region["where"])

    @staticmethod
    def _prep_note(el):
        """Plain wrapped text box with small margins; sizes are set explicitly afterwards."""
        tb = el.find("p:txBody", NS)
        if tb is None:
            return
        bp = tb.find("a:bodyPr", NS)
        if bp is None:
            bp = E("a:bodyPr")
            tb.insert(0, bp)
        bp.set("wrap", "square")
        for k in ("rot", "vert", "numCol"):
            bp.attrib.pop(k, None)
        bp.set("lIns", "45720")
        bp.set("rIns", "45720")
        bp.set("tIns", "27432")
        bp.set("bIns", "27432")
        bp.set("anchor", "t")
        for c in list(bp):
            if local(c) in ("normAutofit", "spAutoFit", "noAutofit"):
                bp.remove(c)
        bp.append(E("a:noAutofit"))
        x_ = el.find("p:spPr/a:xfrm", NS)
        if x_ is not None:
            for k in ("rot", "flipH", "flipV"):
                x_.attrib.pop(k, None)

    @staticmethod
    def _note_sizes(el):
        """Font size (hundredths of a point) of each paragraph's runs, as written or 18 pt."""
        default = "1800"
        lst = el.find("p:txBody/a:lstStyle/a:lvl1pPr/a:defRPr", NS)
        if lst is not None and lst.get("sz"):
            default = lst.get("sz")
        out = []
        for p in el.findall("p:txBody/a:p", NS):
            sizes = []
            for r in p:
                if local(r) in ("r", "fld", "br"):
                    rp = r.find("a:rPr", NS)
                    sizes.append(int(rp.get("sz")) if rp is not None and rp.get("sz") else None)
            epr = p.find("a:endParaRPr", NS)
            esz = int(epr.get("sz")) if epr is not None and epr.get("sz") else None
            known = [v for v in sizes if v] + ([esz] if esz else [])
            fallback = max(known) if known else int(default)
            out.append(([v or fallback for v in sizes], esz or fallback))
        return out

    @staticmethod
    def _scaled_sz(v, scale):
        return max(600, int(round(v * scale / 50.0)) * 50)

    def _apply_note_sizes(self, el, bases, scale):
        for p, (runs, end) in zip(el.findall("p:txBody/a:p", NS), bases):
            i = 0
            for r in p:
                if local(r) in ("r", "fld", "br"):
                    rp = r.find("a:rPr", NS)
                    if rp is None:
                        rp = E("a:rPr", {"lang": "en-US"})
                        r.insert(0, rp)
                    rp.set("sz", str(self._scaled_sz(runs[i], scale)))
                    i += 1
            epr = p.find("a:endParaRPr", NS)
            if epr is None:
                epr = E("a:endParaRPr", {"lang": "en-US"})
                p.append(epr)
            epr.set("sz", str(self._scaled_sz(end, scale)))

    def _note_height(self, el, width_in, bases, scale):
        bp = el.find("p:txBody/a:bodyPr", NS)
        ins = lambda k, d: int(bp.get(k, d)) / EMU if bp is not None else d / EMU
        avail = width_in - ins("lIns", 91440) - ins("rIns", 91440)
        h = ins("tIns", 45720) + ins("bIns", 45720)
        for i, (p, (runs, end)) in enumerate(zip(el.findall("p:txBody/a:p", NS), bases)):
            size = self._scaled_sz(max(runs + [end]) if runs else end, scale) / 100.0
            bold = any(r.find("a:rPr", NS) is not None and r.find("a:rPr", NS).get("b") == "1"
                       for r in p if local(r) == "r")
            ppr = p.find("a:pPr", NS)
            chain = [ppr] if ppr is not None else []
            marl = int(ppr.get("marL", "0")) / EMU if ppr is not None else 0.0
            lh = self.measurer.line_height(size)
            ls = chain_spacing(chain, "lnSpc")
            if ls and ls[0] == "pct":
                lh *= ls[1]
            elif ls and ls[0] == "pts":
                lh = ls[1] * scale / 72.0
            sb, sa = chain_spacing(chain, "spcBef"), chain_spacing(chain, "spcAft")
            sbv = (sb[1] if sb[0] == "pts" else sb[1] * size * 1.2) if sb else 0.0
            sav = (sa[1] if sa[0] == "pts" else sa[1] * size * 1.2) if sa else 0.0
            lines = self.measurer.count_lines(para_text(p).split("\v"), max(0.3, avail - marl), size, bold)
            h += (sbv / 72.0 if i else 0.0) + lines * lh + sav / 72.0
        return h

    def _to_pasteboard(self, n, si):
        self._stack_pasteboard(n, [si])

    def _stack_pasteboard(self, n, items):
        if not items:
            return
        mode = self.profile.get("designer_notes", "pasteboard")
        y = emu(0.2)
        for si in items:
            r = rect_of_xfrm(xfrm_el(si.el)) or si.rect
            if r is None:
                continue
            if mode == "pasteboard":
                x = -r.w - emu(0.25)
                _move(si.el, x, y)
                y += r.h + emu(0.15)
            self.report.move(n, "designer note moved next to the slide (visible when editing, not in the show): '%s'"
                             % si.text.strip().replace("\n", " ")[:70])

    # ---------------------------------------------------------------
    def lock_content(self, el, old_layout):
        old_master = self.old.master_of[old_layout]
        for sp in el.iter(P + "sp", P + "cxnSp"):
            bake_style_refs(sp, self.ctx_old, self.ctx_new)
        for sp in el.iter(P + "sp"):
            if ph_el(sp) is None:
                tb = sp.find("p:txBody", NS)
                if tb is not None:
                    self.bake_otherstyle(tb, old_master)
        lock_colors(el, self.ctx_old, self.ctx_new)
        lock_fonts(el, self.ctx_old, self.ctx_new)

    def bake_otherstyle(self, tb, old_master):
        om = self.src.xml(old_master)
        nm = self.out.xml(self.new_master)
        lst = tb.find("a:lstStyle", NS)
        for p in tb.findall("a:p", NS):
            L = para_level(p)
            name = "a:lvl%dpPr" % (L + 1)
            o = om.find("p:txStyles/p:otherStyle/" + name, NS)
            n = nm.find("p:txStyles/p:otherStyle/" + name, NS)
            if o is None:
                continue
            shp = lst.find(name, NS) if lst is not None else None
            ppr = p.find("a:pPr", NS)
            for att, v in o.attrib.items():
                if n is not None and n.get(att) == v:
                    continue
                if ppr is not None and att in ppr.attrib:
                    continue
                if shp is not None and att in shp.attrib:
                    continue
                if ppr is None:
                    ppr = E("a:pPr")
                    p.insert(0, ppr)
                ppr.set(att, v)
            od = o.find("a:defRPr", NS)
            nd = n.find("a:defRPr", NS) if n is not None else None
            if od is None:
                continue
            sdef = shp.find("a:defRPr", NS) if shp is not None else None
            rprs = []
            for r in [r for r in p if local(r) in ("r", "fld", "br")]:
                rp = r.find("a:rPr", NS)
                if rp is None:
                    rp = E("a:rPr", {"lang": "en-US"})
                    r.insert(0, rp)
                rprs.append(rp)
            epr = p.find("a:endParaRPr", NS)
            if epr is not None:
                rprs.append(epr)
            for rp in rprs:
                for att, v in od.attrib.items():
                    if (nd is not None and nd.get(att) == v) or att in rp.attrib or (
                            sdef is not None and att in sdef.attrib):
                        continue
                    rp.set(att, v)
                for child in od:
                    cn = local(child)
                    group = _FILLS if cn in _FILLS else {cn}
                    nchild = None
                    if nd is not None:
                        for c in nd:
                            if local(c) in group:
                                nchild = c
                    if self._same_resolved(child, nchild):
                        continue
                    if any(local(c) in group for c in rp):
                        continue
                    if sdef is not None and any(local(c) in group for c in sdef):
                        continue
                    cp = deepcopy(child)
                    lock_colors(cp, self.ctx_old, self.ctx_new)
                    insert_ordered(rp, cp, _RPR_ORDER)

    def _same_resolved(self, o, n):
        if n is None:
            return False
        a, b = deepcopy(o), deepcopy(n)
        for el, ctx in ((a, self.ctx_old), (b, self.ctx_new)):
            for sc in list(el.iter(A + "schemeClr")):
                v = ctx.color(sc.get("val"))
                if v:
                    sc.tag = A + "srgbClr"
                    sc.set("val", v)
            for f in el.iter(A + "latin", A + "ea", A + "cs"):
                tf = f.get("typeface", "")
                if tf.startswith("+m") and ctx.font(tf):
                    f.set("typeface", ctx.font(tf))
        return etree.tostring(a, method="c14n") == etree.tostring(b, method="c14n")

    # ---------------------------------------------------------------
    def retarget_placeholder(self, el, new_layout, target):
        ph = ph_el(el)
        lph = ph_el(self.new.layout_ph(new_layout, target))
        for att in list(ph.attrib):
            del ph.attrib[att]
        for att in ("type", "orient", "sz", "idx"):
            if lph.get(att) is not None:
                ph.set(att, lph.get(att))
        sppr = el.find("p:spPr", NS)
        if sppr is not None:
            for c in list(sppr):
                sppr.remove(c)
        tb = el.find("p:txBody", NS)
        if tb is not None:
            bp = tb.find("a:bodyPr", NS)
            if bp is not None:
                for att in list(bp.attrib):
                    del bp.attrib[att]
                for c in list(bp):
                    bp.remove(c)
            ls = tb.find("a:lstStyle", NS)
            if ls is not None:
                for c in list(ls):
                    ls.remove(c)

    def keep_as_textbox(self, n, el, old_layout, why):
        """Never lose text: turn a placeholder into a positioned text box with its old look."""
        ph = ph_el(el)
        if ph is None:
            if why:
                self.report.warn(n, "%s - kept as it was: '%s'" % (why, text_of(el).strip()[:60]))
            return
        rect = self.old.inherited_rect(el, old_layout)
        chain0 = self.old.chain(el, old_layout, 0)
        size = chain_defrpr(chain0, "sz", "1800")
        ph.getparent().remove(ph)
        csp = el.find("p:nvSpPr/p:cNvSpPr", NS)
        if csp is not None:
            csp.set("txBox", "1")
            for c in list(csp):
                csp.remove(c)
        sppr = el.find("p:spPr", NS)
        if sppr is not None and sppr.find("a:xfrm", NS) is None and rect is not None:
            x = E("a:xfrm", {}, E("a:off", {"x": rect.x, "y": rect.y}), E("a:ext", {"cx": rect.w, "cy": rect.h}))
            sppr.insert(0, x)
            if sppr.find("a:prstGeom", NS) is None:
                sppr.insert(1, E("a:prstGeom", {"prst": "rect"}, E("a:avLst")))
        tb = el.find("p:txBody", NS)
        if tb is not None and tb.find("a:bodyPr", NS) is not None:
            tb.find("a:bodyPr", NS).set("wrap", "square")
        for rp in el.iter(A + "rPr", A + "endParaRPr"):
            if "sz" not in rp.attrib:
                rp.set("sz", size)
        if why:
            self.report.warn(n, "%s - kept as a text box: '%s'" % (why, text_of(el).strip()[:60]))

    # ------------------------------------------------------------------
    @staticmethod
    def _union(rects):
        rects = [r for r in rects if r is not None]
        u = rects[0]
        for r in rects[1:]:
            u = u.union(r)
        return u

    def make_ph(self, layout, key, sid, paras):
        lph = ph_el(self.new.layout_ph(layout, key))
        ph = E("p:ph")
        for att in ("type", "orient", "sz", "idx"):
            if lph.get(att) is not None:
                ph.set(att, lph.get(att))
        kind = "Title" if lph.get("type") in TITLE_TYPES else "Text Placeholder"
        return E("p:sp", {},
                 E("p:nvSpPr", {},
                   E("p:cNvPr", {"id": sid, "name": "%s %d" % (kind, max(1, sid - 1))}),
                   E("p:cNvSpPr", {}, E("a:spLocks", {"noGrp": "1"})),
                   E("p:nvPr", {}, ph)),
                 E("p:spPr"),
                 E("p:txBody", {}, E("a:bodyPr"), E("a:lstStyle"), *paras))

    def _spacer_para(self, lvl):
        p = self.simple_para("", lvl=lvl)
        r = p.find("a:r", NS)
        if r is not None:
            p.remove(r)
        p.append(E("a:endParaRPr", {"lang": "en-US", "dirty": "0"}))
        ppr = p.find("a:pPr", NS)
        if ppr is None:
            ppr = E("a:pPr")
            p.insert(0, ppr)
        ppr.set("marL", "0")
        ppr.set("indent", "0")
        ppr.append(E("a:buNone"))
        return p

    def simple_para(self, text, lvl=None):
        p = E("a:p")
        if lvl:
            p.append(E("a:pPr", {"lvl": lvl}))
        r = E("a:r", {}, E("a:rPr", {"lang": "es-ES" if self.lang == "es" else "en-US", "dirty": "0"}), E("a:t"))
        r.find("a:t", NS).text = text
        _preserve_space(r.find("a:t", NS))
        p.append(r)
        return p

    def build_title(self, src, sid, name, layout):
        ts = self.profile.get("title_style", {})
        txt = " ".join(t for t in (para_text(p).replace("\v", " ") for p in src.iter(A + "p")) if not spacer(t))
        txt = re.sub(r"\s{2,}", " ", txt)
        time_text = None
        pat = self.profile.get("time_pattern")
        if pat:
            m = re.search(pat, txt, re.I)
            if m:
                time_text = m.group(1).strip()
                txt = txt[:m.start()] + txt[m.end():]
        txt = txt.rstrip().rstrip("​").rstrip()
        p = E("a:p")
        split = ts.get("split_at")
        i = txt.find(split) if split else -1
        parts = [(txt[:i], {}), (txt[i:], ts.get("after_split_rpr", {}))] if i > 0 else [(txt, {})]
        for t, extra in parts:
            r = E("a:r", {}, E("a:rPr", dict({"lang": "es-ES" if self.lang == "es" else "en-US"}, **extra)),
                  E("a:t"))
            r.find("a:t", NS).text = t
            _preserve_space(r.find("a:t", NS))
            p.append(r)
        el = self.make_ph(layout, "title", sid, [p])
        return el, time_text

    # --- framework text: takes the template look, keeps emphasis --------------------------
    def _base_fonts(self):
        fonts = set(self.profile.get("base_fonts", []))
        fonts |= {v for v in self.ctx_old.fonts.values() if v}
        return fonts

    def clean_rpr(self, rpr, default_hex):
        if rpr is None:
            return
        for att in ("sz", "kern", "noProof"):
            rpr.attrib.pop(att, None)
        if rpr.get("baseline") == "0":
            del rpr.attrib["baseline"]
        base = self._base_fonts()
        for c in list(rpr):
            t = local(c)
            if t == "solidFill":
                col = c[0] if len(c) else None
                hexv, has_mods = None, False
                if col is not None:
                    ct = local(col)
                    if ct == "srgbClr":
                        hexv = col.get("val", "").upper()
                    elif ct == "schemeClr":
                        hexv = self.ctx_old.color(col.get("val"))
                    elif ct == "sysClr":
                        hexv = (col.get("lastClr") or "").upper()
                    has_mods = len(col) > 0
                if hexv and not has_mods and (hexv == "000000" or hexv == default_hex):
                    rpr.remove(c)
            elif t == "latin":
                tf = c.get("typeface", "")
                if tf in base or tf.startswith("+m"):
                    rpr.remove(c)
            elif t in ("ea", "cs"):
                rpr.remove(c)

    def framework_paras(self, shape, layout, lvl, drop_empty=True, role=None):
        tb = shape.find("p:txBody", NS)
        if tb is None:
            return []
        out = []
        respect = self.profile["panel"].get("respect_old_no_bullet", {}).get(role, True)
        for p in tb.findall("a:p", NS):
            txt = para_text(p)
            if drop_empty and spacer(txt):
                continue
            chain = self.old.chain(shape, layout, para_level(p))
            dflt = self._chain_color(chain)
            np_ = E("a:p")
            ppr = E("a:pPr", {"lvl": lvl}) if lvl else E("a:pPr")
            kind, bu = effective_bullet(p, chain)
            old_ppr = p.find("a:pPr", NS)
            if kind == "none" and respect:
                ppr.set("marL", "0")
                ppr.set("indent", "0")
                ppr.append(E("a:buNone"))
            elif kind == "num" and bu is not None:
                ppr.append(deepcopy(bu))
            algn = old_ppr.get("algn") if old_ppr is not None else None
            if algn in ("r", "ctr", "just", "dist") and role not in ("map",):
                ppr.set("algn", algn)
            if lvl or len(ppr) or ppr.attrib:
                np_.append(ppr)
            for c in p:
                if local(c) in ("r", "fld", "br"):
                    cc = deepcopy(c)
                    self.clean_rpr(cc.find("a:rPr", NS), dflt)
                    lock_colors(cc, self.ctx_old, self.ctx_new)
                    np_.append(cc)
            out.append(np_)
        self._strip_uniform(out)
        return out

    @staticmethod
    def _strip_uniform(paras):
        """A colour or font used on EVERY run of a box is the old box's design, not emphasis."""
        runs = [r for p in paras for r in p if local(r) == "r" and (r.find("a:t", NS).text or "").strip()]
        if len(runs) < 1:
            return
        for kind in ("solidFill", "latin"):
            vals = []
            for r in runs:
                rp = r.find("a:rPr", NS)
                c = rp.find("a:" + kind, NS) if rp is not None else None
                vals.append(etree.tostring(c, method="c14n") if c is not None else None)
            if vals and vals[0] is not None and all(v == vals[0] for v in vals) and len(runs) > 1:
                for p in paras:
                    for r in p:
                        rp = r.find("a:rPr", NS) if local(r) in ("r", "fld", "br") else None
                        if rp is not None and rp.find("a:" + kind, NS) is not None:
                            rp.remove(rp.find("a:" + kind, NS))

    def _chain_color(self, chain):
        for h in chain:
            d = h.find("a:defRPr", NS)
            if d is not None:
                f = d.find("a:solidFill", NS)
                if f is not None and len(f):
                    c = f[0]
                    if local(c) == "srgbClr":
                        return c.get("val").upper()
                    if local(c) == "schemeClr":
                        return self.ctx_old.color(c.get("val"))
        return None

    def routine_paras(self, shape, layout, levels):
        tb = shape.find("p:txBody", NS)
        out = []
        first = True
        for p in tb.findall("a:p", NS):
            txt = para_text(p)
            if spacer(txt):
                continue
            chain = self.old.chain(shape, layout, para_level(p))
            kind, _bu = effective_bullet(p, chain)
            numbered = kind == "num" or bool(manual_number(txt))
            lvl = levels["routine_heading"] if (first and not numbered) else levels["routine"]
            np_ = E("a:p")
            ppr = E("a:pPr", {"lvl": lvl}) if lvl else E("a:pPr")
            if lvl and not numbered:
                ppr.set("marL", "0")
                ppr.set("indent", "0")
                ppr.append(E("a:buNone"))
            if lvl or len(ppr):
                np_.append(ppr)
            dflt = self._chain_color(chain)
            for c in p:
                if local(c) in ("r", "fld", "br"):
                    cc = deepcopy(c)
                    self.clean_rpr(cc.find("a:rPr", NS), dflt)
                    lock_colors(cc, self.ctx_old, self.ctx_new)
                    np_.append(cc)
            if manual_number(txt) and kind != "num":
                _strip_prefix(np_, manual_number(txt).end())
            first = False
            out.append(np_)
        self._strip_uniform(out)
        return out

    # --- auto-fit estimation -------------------------------------------------------------
    def _new_chain(self, layout, key, lvl):
        return self.new.chain(self.new.layout_ph(layout, key), layout, lvl)

    def estimate(self, el, layout, key):
        lsp = self.new.layout_ph(layout, key)
        lrect = rect_of_xfrm(xfrm_el(lsp)) or self.new.inherited_rect(lsp, layout)
        bp = self.new.body_pr(lsp, layout)
        lins = int(bp.get("lIns", 91440)) / EMU
        rins = int(bp.get("rIns", 91440)) / EMU
        tins = int(bp.get("tIns", 45720)) / EMU
        bins = int(bp.get("bIns", 45720)) / EMU
        avail = lrect.w / EMU - lins - rins
        y = lrect.y / EMU + tins
        blocks = []
        for i, p in enumerate(el.findall("p:txBody/a:p", NS)):
            lvl = para_level(p)
            ppr = p.find("a:pPr", NS)
            base = self._new_chain(layout, key, lvl)
            chain = ([ppr] + base) if ppr is not None else base
            size = int(chain_defrpr(base, "sz", "1800")) / 100.0
            rsz = [r.find("a:rPr", NS).get("sz") for r in p if local(r) in ("r", "fld")
                   and r.find("a:rPr", NS) is not None and r.find("a:rPr", NS).get("sz")]
            if not rsz and p.find("a:endParaRPr", NS) is not None and p.find("a:endParaRPr", NS).get("sz"):
                rsz = [p.find("a:endParaRPr", NS).get("sz")]
            if rsz:
                size = max(int(v) for v in rsz) / 100.0
            bold = chain_defrpr(base, "b", "0") == "1"
            marl = int(chain_attr(chain, "marL", "0")) / EMU
            algn = chain_attr(chain, "algn", "l")
            sb = chain_spacing(chain, "spcBef")
            sa = chain_spacing(chain, "spcAft")
            lh = self.measurer.line_height(size)
            ls = chain_spacing(chain, "lnSpc")
            if ls and ls[0] == "pct":
                lh *= ls[1]
            elif ls and ls[0] == "pts":
                lh = ls[1] / 72.0
            sbv = (sb[1] if sb and sb[0] == "pts" else (sb[1] * size * 1.2 if sb else 0))
            sav = (sa[1] if sa and sa[0] == "pts" else (sa[1] * size * 1.2 if sa else 0))
            txt = para_text(p)
            segs = txt.split("\v")
            usable = max(0.2, avail - marl)
            nlines = self.measurer.count_lines(segs, usable, size, bold)
            before = 0 if i == 0 else sbv / 72.0
            top = y + before
            h = nlines * lh
            tw = 0.0 if spacer(txt) else min(usable, max(self.measurer.width(sg.strip(), size, bold) for sg in segs))
            if algn == "r":
                x0 = lrect.r / EMU - rins - tw
            elif algn == "ctr":
                x0 = lrect.x / EMU + lins + marl + (usable - tw) / 2.0
            else:
                x0 = lrect.x / EMU + lins + marl
            blocks.append(dict(top=top, lh=lh, h=h, size=size, marl=marl, x0=x0, x1=x0 + tw,
                               spacer=spacer(txt), before=before, after=sav / 72.0))
            y = top + h + sav / 72.0
        height = max(0.3, y - lrect.y / EMU + bins)
        return lrect, height, blocks

    def autofit(self, el, layout, key, anchor_bottom=False, avoid=(), max_bottom=None, n=None):
        for _ in range(12):
            lrect, height, blocks = self.estimate(el, layout, key)
            hit = None
            for i, b in enumerate(blocks):
                if b["spacer"] or i == 0:
                    continue
                for r in avoid:
                    if r is None:
                        continue
                    rx0, ry0, rx1, ry1 = inch(r.x), inch(r.y), inch(r.r), inch(r.b)
                    if b["x0"] < rx1 - 0.02 and b["x1"] > rx0 + 0.02 and b["top"] < ry1 - 0.02 \
                            and b["top"] + b["h"] > ry0 + 0.02:
                        # margin covers renderers that merge "space after" with "space before"
                        hit = (i, ry1 + 0.1 + blocks[i - 1]["after"] - b["top"])
                        break
                if hit:
                    break
            if not hit:
                break
            i, extra = hit
            # blank lines render the same in PowerPoint and other viewers (large spacing does not)
            paras = el.findall("p:txBody/a:p", NS)
            p = paras[i]
            lvl = para_level(p)
            step = blocks[i]["lh"] + blocks[i]["after"]
            k = max(1, int(extra / step + 0.999))
            for _ in range(k):
                p.addprevious(self._spacer_para(lvl))
        new_y = inch(lrect.y)
        if anchor_bottom:
            new_y = inch(lrect.b) - height
            if max_bottom is not None and new_y < max_bottom:
                new_y = max_bottom
            shift = new_y - inch(lrect.y)
            for b in blocks:
                b["top"] += shift
        limit = (inch(lrect.b) - new_y) if anchor_bottom and max_bottom is not None else (
            (max_bottom - new_y) if max_bottom is not None else None)
        if limit is not None and height > limit + 0.01 and not getattr(self, "_shrinking", False):
            # too much text: set an exact smaller size (renderers' own auto-shrink differs and
            # would move lines under the icons); headings keep their size
            self._shrinking = True
            try:
                pct = 100
                for _ in range(8):
                    pct = max(50, int(pct * max(0.6, limit / height)) - 2)
                    self._set_text_scale(el, layout, key, pct)
                    box, blocks = self.autofit(el, layout, key, anchor_bottom, avoid, None, None)
                    height = inch(box.h)
                    if height <= limit + 0.01 or pct <= 50:
                        break
                new_y = inch(box.y) if not anchor_bottom else max(inch(lrect.b) - height, max_bottom or 0)
                if n:
                    self.report.slide(n, "a side panel had more text than fits; its text was set to %d%% size" % pct)
            finally:
                self._shrinking = False
            if anchor_bottom:
                shift = new_y - inch(box.y)
                for b in blocks:
                    b["top"] += shift
        box = Rect(lrect.x, emu(new_y), lrect.w, emu(height))
        sppr = el.find("p:spPr", NS)
        for c in list(sppr):
            sppr.remove(c)
        sppr.append(E("a:xfrm", {}, E("a:off", {"x": box.x, "y": box.y}), E("a:ext", {"cx": box.w, "cy": box.h})))
        sppr.append(E("a:prstGeom", {"prst": "rect"}, E("a:avLst")))
        return box, blocks

    def _fit_in_box(self, n, el, layout, key):
        """Lists longer than their box (e.g. 8 lesson outcomes) get an exact smaller size."""
        lsp = self.new.layout_ph(layout, key)
        r = self.new.inherited_rect(lsp, layout)
        if r is None:
            return
        pct = 100
        for _ in range(8):
            _lr, height, _b = self.estimate(el, layout, key)
            if height <= inch(r.h) + 0.02 or pct <= 60:
                break
            pct = max(60, int(pct * max(0.7, inch(r.h) / height)) - 2)
            self._set_text_scale(el, layout, key, pct, all_levels=True)
        if pct < 100:
            self.report.slide(n, "the list was longer than its box; text set to %d%% size" % pct)

    def _set_text_scale(self, el, layout, key, pct, all_levels=False):
        """Give every non-heading run an explicit size = template size x pct."""
        labels = set(self.labels.values())
        for i, p in enumerate(el.findall("p:txBody/a:p", NS)):
            lvl = para_level(p)
            if not all_levels and (lvl == 0 or para_text(p).strip() in labels):
                continue
            base = int(chain_defrpr(self._new_chain(layout, key, lvl), "sz", "1800"))
            sz = str(max(600, int(round(base * pct / 100.0 / 50.0)) * 50))
            for r in p:
                if local(r) in ("r", "fld", "br"):
                    rp = r.find("a:rPr", NS)
                    if rp is None:
                        rp = E("a:rPr", {"lang": "en-US"})
                        r.insert(0, rp)
                    rp.set("sz", sz)
            epr = p.find("a:endParaRPr", NS)
            if epr is None:
                epr = E("a:endParaRPr", {"lang": "en-US"})
                p.append(epr)
            epr.set("sz", sz)

    def _badge_paragraph(self, paras, start):
        idx = None
        for i in range(start or 0, len(paras)):
            t = para_text(paras[i])
            if re.match(r"^[\s  \xa0\t]{2,}", t) or re.search(r"(?i)see notes|consulte las notas|ver notas", t):
                idx = i
        return idx if idx is not None else len(paras) - 1

    def _place_badges(self, badges, box, block, layout, key):
        top, lh, marl = block["top"], block["lh"], block["marl"]
        lins = int(self.new.body_pr(self.new.layout_ph(layout, key), layout).get("lIns", 91440)) / EMU
        x = inch(box.x) + lins + marl
        for b in sorted(badges, key=lambda s: rect_of_xfrm(xfrm_el(s)).x):
            r = rect_of_xfrm(xfrm_el(b))
            _move(b, emu(x), emu(top + (lh - inch(r.h)) / 2.0))
            x += inch(r.w) + 0.05

    def _place_icons(self, icons, old_box, new_box, text_top_in, bottom_in=None):
        """Flow the student-box icons into rows under the panel heading, in reading order
        (top-to-bottom, left-to-right), wrapping only when a row is full."""
        items = []
        for s in icons:
            r = rect_of_xfrm(xfrm_el(s))
            if r is not None:
                items.append((s, r))
        if not items:
            return
        # reading order: group icons that share a line in the old box, then left to right
        items.sort(key=lambda it: it[1].y)
        lines = []
        for s, r in items:
            if lines and r.y < lines[-1][-1][1].b - emu(0.1):
                lines[-1].append((s, r))
            else:
                lines.append([(s, r)])
        order = [it for line in lines for it in sorted(line, key=lambda it: it[1].x)]
        gap = emu(0.08)
        left = new_box.x + emu(0.05)
        right = new_box.r - emu(0.05)
        x, y = left, emu(text_top_in) - emu(0.05)
        row_h = 0
        for s, r in order:
            if x > left and x + r.w > right:
                x, y = left, y + row_h + gap
                row_h = 0
            ny = y
            if bottom_in is not None:
                ny = min(ny, emu(bottom_in) - r.h)
            _move(s, int(x), int(ny))
            x += r.w + gap
            row_h = max(row_h, r.h)

    # --- content area -------------------------------------------------------------------
    def _fit_content(self, n, content, area, old_layout, pf, frame=None):
        pairs = [(s, rect_of_xfrm(xfrm_el(s))) for s in content]
        pairs = [(s, r) for s, r in pairs if r is not None]
        if not pairs:
            return
        bb = self._union([r for _, r in pairs])
        if frame is not None:
            bb = frame.union(bb)
        x0, y0 = max(bb.x, 0), max(bb.y, 0)
        x1, y1 = min(bb.r, self.slide_w), min(bb.b, self.slide_h)
        if x1 > x0 and y1 > y0:
            bb = Rect(x0, y0, x1 - x0, y1 - y0)
        inset = emu(pf.get("content_inset_in", 0.1))
        aw, ah = area.w - 2 * inset, area.h - 2 * inset
        s = min(aw / float(bb.w) if bb.w else 1, ah / float(bb.h) if bb.h else 1)
        if not pf.get("allow_upscale", False):
            s = min(1.0, s)
        for el, r in pairs:
            ncx = area.cx + (r.cx - bb.cx) * s
            ncy = area.cy + (r.cy - bb.cy) * s
            nw, nh = r.w * s, r.h * s
            _set_rect(el, int(round(ncx - nw / 2)), int(round(ncy - nh / 2)), int(round(nw)), int(round(nh)))
            if s < 0.999:
                self._scale_inside(el, s, old_layout)
        if s < 0.999:
            self.report.slide(n, "content scaled to %d%% to fit the new content area" % round(s * 100))

    def _scale_inside(self, el, s, old_layout):
        for tbl in el.iter(A + "tbl"):
            for gc in tbl.iter(A + "gridCol"):
                gc.set("w", str(int(round(int(gc.get("w")) * s))))
            for tr in tbl.iter(A + "tr"):
                tr.set("h", str(int(round(int(tr.get("h")) * s))))
            for tc in tbl.iter(A + "tcPr"):
                for att in ("marL", "marR", "marT", "marB"):
                    if tc.get(att):
                        tc.set(att, str(int(round(int(tc.get(att)) * s))))
        for tb in list(el.iter(P + "txBody")) + list(el.iter(A + "txBody")):
            shape = tb.getparent()
            lst = tb.find("a:lstStyle", NS)
            for lvl in (lst if lst is not None else []):
                d = lvl.find("a:defRPr", NS)
                if d is not None and d.get("sz"):
                    d.set("sz", _scaled(d.get("sz"), s))
            for p in tb.findall("a:p", NS):
                if local(shape) == "sp":
                    base = chain_defrpr(self.old.chain(shape, old_layout, para_level(p)), "sz", "1800")
                else:
                    base = "1800"
                for r in p:
                    if local(r) in ("r", "fld", "br"):
                        rp = r.find("a:rPr", NS)
                        if rp is None:
                            rp = E("a:rPr", {"lang": "en-US"})
                            r.insert(0, rp)
                        rp.set("sz", _scaled(rp.get("sz", base), s))
                epr = p.find("a:endParaRPr", NS)
                if epr is not None:
                    epr.set("sz", _scaled(epr.get("sz", base), s))

    # =================================================================
    @staticmethod
    def _effect_count(root):
        t = root.find("p:timing", NS)
        return 0 if t is None else sum(1 for c in t.iter(P + "cTn") if c.get("presetClass"))

    def prune_timing(self, n, root):
        timing = root.find("p:timing", NS)
        if timing is None:
            return
        valid = {c.get("id") for c in root.iter(P + "cNvPr")}
        removed = 0
        for tgt in list(timing.iter(P + "spTgt")):
            if tgt.get("spid") in valid:
                continue
            node = tgt
            while node is not None and not (local(node) == "par" and node.find("p:cTn", NS) is not None
                                            and node.find("p:cTn", NS).get("presetClass")):
                node = node.getparent()
            if node is None or node.getparent() is None:
                continue
            node.getparent().remove(node)
            removed += 1
        for b in list(timing.iter(P + "bldP")):
            if b.get("spid") not in valid:
                b.getparent().remove(b)
        changed = True
        while changed:
            changed = False
            for ctn in list(timing.iter(P + "cTn")):
                kids = ctn.find("p:childTnLst", NS)
                if kids is not None and len(kids) == 0 and ctn.get("nodeType") not in ("mainSeq", "tmRoot"):
                    par = ctn.getparent()
                    if local(par) == "par" and par.getparent() is not None:
                        par.getparent().remove(par)
                        changed = True
        if removed:
            self.ledger.s(n)["dropped_effects"] += removed
            self.report.warn(n, "%d animation(s) belonged to removed old-design shapes and were removed" % removed)

    def prune_image_rels(self, slide, root):
        used = set()
        for el in root.iter():
            for k, v in el.attrib.items():
                if k.startswith(R):
                    used.add(v)
        for rel in self.out.rel_list(slide):
            if rel["Type"] == RT_IMAGE and rel["Id"] not in used:
                self.out.remove_rel(slide, rel["Id"])


# ---------------------------------------------------------------------------------------
def _scaled(sz, s):
    return str(max(100, int(round(int(sz) * s / 100.0)) * 100))


def _preserve_space(t):
    if t.text and (t.text != t.text.strip() or "  " in t.text):
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _strip_prefix(p, nchars):
    for r in p:
        if nchars <= 0:
            break
        if local(r) != "r":
            continue
        t = r.find("a:t", NS)
        if t is None or not t.text:
            continue
        take = min(nchars, len(t.text))
        t.text = t.text[take:]
        nchars -= take
    for r in list(p):
        if local(r) == "r":
            t = r.find("a:t", NS)
            if t is not None and t.text == "":
                p.remove(r)
            else:
                break


def _branches(el):
    if local(el) == "AlternateContent":
        return [ch[0] for ch in el if len(ch)]
    return [el]


def _move(el, x, y):
    for b in _branches(el):
        x_ = xfrm_el(b)
        if x_ is None:
            continue
        off = x_.find("a:off", NS)
        off.set("x", str(int(x)))
        off.set("y", str(int(y)))


def _set_rect(el, x, y, w, h):
    for b in _branches(el):
        x_ = xfrm_el(b)
        if x_ is None:
            continue
        x_.find("a:off", NS).set("x", str(x))
        x_.find("a:off", NS).set("y", str(y))
        x_.find("a:ext", NS).set("cx", str(max(1, w)))
        x_.find("a:ext", NS).set("cy", str(max(1, h)))
