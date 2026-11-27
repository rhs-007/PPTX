"""Low-level OPC (zip) package handling for .pptx files.

Everything is done on raw XML with lxml so that formatting, animations,
extensions and unknown markup survive untouched.
"""
import posixpath
import re
import zipfile
from copy import deepcopy

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
A = "{%s}" % NS["a"]
P = "{%s}" % NS["p"]
R = "{%s}" % NS["r"]
PR = "{%s}" % NS["pr"]
CT = "{%s}" % NS["ct"]

RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RT_SLIDE = RT + "slide"
RT_LAYOUT = RT + "slideLayout"
RT_MASTER = RT + "slideMaster"
RT_THEME = RT + "theme"
RT_NOTES = RT + "notesSlide"
RT_NOTES_MASTER = RT + "notesMaster"
RT_IMAGE = RT + "image"

EMU = 914400

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True,
                          remove_blank_text=False)


def parse_xml(data):
    return etree.fromstring(data, _PARSER)


def to_bytes(root):
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


class Package:
    """An in-memory, editable OPC package."""

    def __init__(self, path):
        self.path = path
        self.parts = {}
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                self.parts[info.filename] = z.read(info.filename)
        self._xml = {}
        self.ct = parse_xml(self.parts["[Content_Types].xml"])

    # ---------------- generic part access -----------------
    def exists(self, name):
        return name in self.parts or name in self._xml

    def xml(self, name):
        if name not in self._xml:
            self._xml[name] = parse_xml(self.parts[name])
        return self._xml[name]

    def set_xml(self, name, root):
        self._xml[name] = root
        self.parts[name] = b""  # placeholder; serialised on save

    def raw(self, name):
        if name in self._xml:
            return to_bytes(self._xml[name])
        return self.parts[name]

    def set_raw(self, name, data):
        self._xml.pop(name, None)
        self.parts[name] = data

    def delete(self, name):
        self.parts.pop(name, None)
        self._xml.pop(name, None)
        rp = rels_path(name)
        self.parts.pop(rp, None)
        self._xml.pop(rp, None)
        self.remove_override(name)

    def names(self):
        return list(self.parts.keys())

    # ---------------- relationships -----------------
    def rels(self, part):
        """Return the <Relationships> root for a part (created if missing)."""
        rp = rels_path(part)
        if rp not in self.parts and rp not in self._xml:
            root = etree.Element(PR + "Relationships", nsmap={None: NS["pr"]})
            self.set_xml(rp, root)
        return self.xml(rp)

    def has_rels(self, part):
        rp = rels_path(part)
        return rp in self.parts or rp in self._xml

    def rel_list(self, part):
        if not self.has_rels(part):
            return []
        out = []
        for r in self.rels(part):
            out.append(dict(Id=r.get("Id"), Type=r.get("Type"), Target=r.get("Target"),
                            TargetMode=r.get("TargetMode")))
        return out

    def rel_target(self, part, rid):
        for r in self.rel_list(part):
            if r["Id"] == rid:
                if r["TargetMode"] == "External":
                    return None
                return resolve_target(part, r["Target"])
        return None

    def rels_of_type(self, part, rtype):
        return [(r["Id"], resolve_target(part, r["Target"])) for r in self.rel_list(part)
                if r["Type"] == rtype and r["TargetMode"] != "External"]

    def add_rel(self, part, rtype, target_part, rid=None):
        root = self.rels(part)
        existing = {r.get("Id") for r in root}
        if rid is None:
            n = 1
            while "rId%d" % n in existing:
                n += 1
            rid = "rId%d" % n
        el = etree.SubElement(root, PR + "Relationship")
        el.set("Id", rid)
        el.set("Type", rtype)
        el.set("Target", relative_target(part, target_part))
        return rid

    def set_rel_target(self, part, rid, target_part):
        for r in self.rels(part):
            if r.get("Id") == rid:
                r.set("Target", relative_target(part, target_part))
                return
        raise KeyError(rid)

    def remove_rel(self, part, rid):
        root = self.rels(part)
        for r in list(root):
            if r.get("Id") == rid:
                root.remove(r)

    # ---------------- content types -----------------
    def content_type(self, name):
        pn = "/" + name
        for o in self.ct.findall(CT + "Override"):
            if o.get("PartName") == pn:
                return o.get("ContentType")
        ext = name.rsplit(".", 1)[-1].lower()
        for d in self.ct.findall(CT + "Default"):
            if d.get("Extension").lower() == ext:
                return d.get("ContentType")
        return None

    def override(self, name):
        pn = "/" + name
        for o in self.ct.findall(CT + "Override"):
            if o.get("PartName") == pn:
                return o.get("ContentType")
        return None

    def add_override(self, name, ctype):
        self.remove_override(name)
        el = etree.SubElement(self.ct, CT + "Override")
        el.set("PartName", "/" + name)
        el.set("ContentType", ctype)

    def remove_override(self, name):
        pn = "/" + name
        for o in list(self.ct.findall(CT + "Override")):
            if o.get("PartName") == pn:
                self.ct.remove(o)

    def ensure_default(self, ext, ctype):
        for d in self.ct.findall(CT + "Default"):
            if d.get("Extension").lower() == ext.lower():
                return
        el = etree.Element(CT + "Default")
        el.set("Extension", ext)
        el.set("ContentType", ctype)
        # Defaults must precede Overrides
        first_override = self.ct.find(CT + "Override")
        if first_override is not None:
            first_override.addprevious(el)
        else:
            self.ct.append(el)

    def defaults(self):
        return {d.get("Extension").lower(): d.get("ContentType") for d in self.ct.findall(CT + "Default")}

    # ---------------- naming -----------------
    def free_name(self, wanted):
        """Return `wanted` or the next free name with the same stem/number pattern."""
        if not self.exists(wanted):
            return wanted
        d, base = posixpath.split(wanted)
        m = re.match(r"^(.*?)(\d*)(\.[^.]+)$", base)
        stem, _num, ext = m.group(1), m.group(2), m.group(3)
        n = 1
        while True:
            cand = posixpath.join(d, "%s%d%s" % (stem, n, ext))
            if not self.exists(cand):
                return cand
            n += 1

    # ---------------- reachability / cleanup -----------------
    def reachable(self):
        seen = set()
        stack = []
        for r in self.rel_list(""):
            if r["TargetMode"] != "External":
                stack.append(resolve_target("", r["Target"]))
        while stack:
            part = stack.pop()
            if part in seen or not self.exists(part):
                continue
            seen.add(part)
            for r in self.rel_list(part):
                if r["TargetMode"] == "External":
                    continue
                stack.append(resolve_target(part, r["Target"]))
        return seen

    def remove_unreachable(self):
        keep = self.reachable()
        removed = []
        for name in list(self.parts.keys()):
            if name == "[Content_Types].xml" or name.endswith(".rels"):
                continue
            if name not in keep:
                self.delete(name)
                removed.append(name)
        # drop rels files whose source part disappeared
        for name in list(self.parts.keys()):
            if name.endswith(".rels") and name != "_rels/.rels":
                src = source_of_rels(name)
                if not self.exists(src):
                    self.parts.pop(name, None)
                    self._xml.pop(name, None)
        # drop overrides pointing at nothing
        for o in list(self.ct.findall(CT + "Override")):
            if not self.exists(o.get("PartName").lstrip("/")):
                self.ct.remove(o)
        return removed

    # ---------------- save -----------------
    def save(self, path):
        self.set_xml("[Content_Types].xml", self.ct)
        names = sorted(self.parts.keys(), key=lambda n: (n != "[Content_Types].xml", n != "_rels/.rels", n))
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                z.writestr(n, self.raw(n))


# ---------------- path helpers -----------------
def rels_path(part):
    if part == "":
        return "_rels/.rels"
    d, b = posixpath.split(part)
    return posixpath.join(d, "_rels", b + ".rels")


def source_of_rels(rels_name):
    d, b = posixpath.split(rels_name)
    parent = posixpath.dirname(d)
    return posixpath.join(parent, b[:-len(".rels")])


def resolve_target(source_part, target):
    if target.startswith("/"):
        return target.lstrip("/")
    base = posixpath.dirname(source_part)
    return posixpath.normpath(posixpath.join(base, target))


def relative_target(source_part, target_part):
    base = posixpath.dirname(source_part) or "."
    return posixpath.relpath(target_part, base)


def copy_part_tree(src, src_part, dst, name_map, skip_types=(), retarget=None):
    """Copy `src_part` (and everything it references) from package `src` into `dst`.

    name_map: dict src_name -> dst_name (filled in as parts are copied; pre-seed it
              to force names or to re-point a reference at an existing dst part).
    skip_types: relationship types that are not followed (the rel is dropped).
    retarget: optional callable(rel_type, src_target) -> dst part name or None, used
              to point references (e.g. layout -> master) at already-copied parts.
    """
    if src_part in name_map:
        return name_map[src_part]
    dst_name = dst.free_name(src_part)
    name_map[src_part] = dst_name
    if src_part in src._xml:
        dst.set_xml(dst_name, deepcopy(src._xml[src_part]))
    else:
        dst.set_raw(dst_name, src.parts[src_part])
    ov = src.override(src_part)
    if ov:
        dst.add_override(dst_name, ov)
    else:
        ext = dst_name.rsplit(".", 1)[-1]
        ctype = src.content_type(src_part)
        if ctype and ext.lower() not in dst.defaults():
            dst.ensure_default(ext, ctype)
    if src.has_rels(src_part):
        new_rels = etree.Element(PR + "Relationships", nsmap={None: NS["pr"]})
        for r in src.rel_list(src_part):
            el = etree.SubElement(new_rels, PR + "Relationship")
            el.set("Id", r["Id"])
            el.set("Type", r["Type"])
            if r["TargetMode"] == "External":
                el.set("Target", r["Target"])
                el.set("TargetMode", "External")
                continue
            if r["Type"] in skip_types:
                new_rels.remove(el)
                continue
            tgt = resolve_target(src_part, r["Target"])
            mapped = retarget(r["Type"], tgt) if retarget else None
            if mapped is None:
                if not src.exists(tgt):
                    new_rels.remove(el)
                    continue
                mapped = copy_part_tree(src, tgt, dst, name_map, skip_types, retarget)
            el.set("Target", relative_target(dst_name, mapped))
        dst.set_xml(rels_path(dst_name), new_rels)
    return dst_name
