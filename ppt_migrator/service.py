"""One call that does everything for one deck: detect -> pick template -> convert -> verify -> report."""
import json
import os
import re
import time
import traceback

from .classify import detect, load_model
from .templates import TemplateLibrary

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
PROFILES = os.path.join(HERE, "profiles")


def load_profile(name_or_path="default"):
    path = name_or_path if os.path.exists(name_or_path) else os.path.join(PROFILES, name_or_path + ".json")
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def settings_path():
    return os.path.join(APP_DIR, "settings.json")


def _defaults():
    return {"templates_dir": os.path.join(APP_DIR, "templates"), "output_dir": os.path.join(APP_DIR, "output"),
            "labels": {}}


def load_settings():
    s = _defaults()
    p = settings_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                s.update(json.load(f))
        except (OSError, ValueError):
            pass
    return s


def save_settings(s):
    """Only keep what differs from the defaults, so the app folder can be moved or copied."""
    d = _defaults()
    keep = {k: v for k, v in s.items() if d.get(k) != v}
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2, ensure_ascii=False)


def output_name(profile, original_name):
    stem = os.path.splitext(os.path.basename(original_name))[0]
    pattern = profile.get("output_name", "NEW_{stem}")
    name = pattern.format(stem=stem)
    return re.sub(r'[<>:"/\\|?*]', "_", name) + ".pptx"


class Engine:
    def __init__(self, templates_dir=None, profile="default"):
        self.settings = load_settings()
        self.templates_dir = templates_dir or self.settings["templates_dir"]
        self.profile, self.profile_path = load_profile(profile)
        self.model = load_model(os.path.join(PROFILES, "strand_model.json"))
        self.library = TemplateLibrary(self.templates_dir)

    def detect(self, path, display_name=None):
        return detect(path, self.model, display_name)

    def convert(self, src, out_dir, display_name=None, strand=None, band=None, lang=None, labels=None):
        """Returns a result dict; never raises (errors are reported in the dict)."""
        from .convert import Converter
        from .verify import verify
        display_name = display_name or os.path.basename(src)
        t0 = time.time()
        res = {"name": display_name, "ok": False}
        try:
            det = self.detect(src, display_name)
            strand = strand or det["strand"]
            band = band or det["band"]
            lang = lang or det["language"]
            res.update(detected=det, strand=strand, band=band, language=lang)
            tpl = self.library.get(strand, band, lang)
            if not tpl:
                res["error"] = ("No template found for %s / %s / %s. Put the template files in the "
                                "templates folder (%s)." % (strand, band, "Spanish" if lang == "es" else "English",
                                                           self.templates_dir))
                return res
            res["template"] = os.path.basename(tpl)
            os.makedirs(out_dir, exist_ok=True)
            out_name = output_name(self.profile, display_name)
            out_path = os.path.join(out_dir, out_name)
            lab = dict(self.settings.get("labels", {}).get(lang, {}))
            lab.update(labels or {})
            conv = Converter(src, tpl, self.profile, lang=lang, labels=lab)
            report = conv.run(out_path)
            vrep = verify(src, out_path, self.profile, ledger=conv.ledger)
            head = ["# %s" % out_name, "",
                    "- Source: %s" % display_name,
                    "- Detected: %s, %s, %s%s" % (strand, band, "Spanish" if lang == "es" else "English",
                                                 "" if det["strand_conf"] >= 0.6 else " (strand detection was not sure - please confirm)"),
                    "- Template: %s" % os.path.basename(tpl), ""]
            text = "\n".join(head) + report.text() + "\n" + vrep.text()
            rep_path = os.path.join(out_dir, os.path.splitext(out_name)[0] + "_report.md")
            with open(rep_path, "w", encoding="utf-8") as f:
                f.write(text)
            res.update(ok=True, verified=vrep.ok, output=out_path, output_name=out_name, report=rep_path,
                       report_text=text, problems=vrep.problems, to_check=report.warnings + vrep.notes,
                       moves=len(report.moves), slides=len(conv.old.slides), seconds=round(time.time() - t0, 1))
        except PermissionError as e:
            res["error"] = ("Could not save %s - it is probably open in PowerPoint. Close it and convert again."
                            % os.path.basename(getattr(e, "filename", "") or "the file"))
        except Exception as e:  # keep going with other decks
            res["error"] = "%s: %s" % (type(e).__name__, e)
            res["trace"] = traceback.format_exc()
        return res
