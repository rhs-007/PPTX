"""PPT Template Migrator.

    py -m ppt_migrator                       start the web app and open it in the browser
    py -m ppt_migrator web [--port 8765] [--no-browser]
    py -m ppt_migrator convert DECK.pptx [DECK2.pptx ...] [--out FOLDER] [--strand Reading] [--grade GK-1] [--lang es]
    py -m ppt_migrator batch FOLDER [--out FOLDER]          convert every .pptx under FOLDER (subfolders too)
    py -m ppt_migrator detect DECK.pptx [...]               show the detected strand / grade / language
    py -m ppt_migrator templates                            show which of the 12 templates were found
    py -m ppt_migrator train --strand Writing DECK.pptx [...]   teach the strand detector with labelled decks
    py -m ppt_migrator verify --old OLD.pptx --new NEW.pptx
"""
import argparse
import os
import sys

STRAND_CHOICES = ["Phonics", "Reading", "Writing"]
GRADE_CHOICES = ["GK-1", "G2-5"]


def _engine(args):
    from .service import Engine
    return Engine(templates_dir=getattr(args, "templates", None))


def _missing(paths):
    bad = [p for p in paths if not os.path.exists(p)]
    for p in bad:
        print("Can't find: %s" % os.path.abspath(p))
    if bad:
        print("Tip: drag the file from File Explorer into this window to paste its exact path,")
        print("     or just run Start.bat and drop the files into the web page.")
    return bool(bad)


def _run(eng, files, out_dir, strand=None, grade=None, lang=None, rel_to=None):
    from .service import load_settings
    out_dir = out_dir or load_settings()["output_dir"]
    ok = 0
    for src in files:
        sub = ""
        if rel_to:
            sub = os.path.dirname(os.path.relpath(src, rel_to))
        name = os.path.relpath(src, rel_to) if rel_to else os.path.basename(src)
        print("Converting %s ..." % name, flush=True)
        res = eng.convert(src, os.path.join(out_dir, sub), display_name=name if rel_to else None,
                          strand=strand, band=grade, lang=lang)
        if not res.get("ok"):
            print("   FAILED: %s" % res.get("error"))
            continue
        ok += 1
        checks = len(res.get("to_check") or [])
        print("   -> %s   [%s, %s, %s]  %s%s" % (
            res["output"], res["strand"], res["band"], "Spanish" if res["language"] == "es" else "English",
            "verified" if res["verified"] else "CHECK REPORT",
            ("  (%d item(s) to look at in the report)" % checks) if checks else ""))
    print("\n%d of %d deck(s) converted. Output folder: %s" % (ok, len(files), os.path.abspath(out_dir)))
    return 0 if ok == len(files) else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["web"]
    ap = argparse.ArgumentParser(prog="ppt_migrator", description="Move lesson decks to the new templates.")
    ap.add_argument("--templates", help="folder holding the new template .pptx files")
    sub = ap.add_subparsers(dest="cmd")

    w = sub.add_parser("web", help="start the web app")
    w.add_argument("--port", type=int)
    w.add_argument("--no-browser", action="store_true")

    c = sub.add_parser("convert", help="convert one or more decks")
    c.add_argument("decks", nargs="+")
    c.add_argument("--out", help="output folder (default: the app's output folder)")
    c.add_argument("--strand", choices=STRAND_CHOICES)
    c.add_argument("--grade", choices=GRADE_CHOICES)
    c.add_argument("--lang", choices=["en", "es"])

    b = sub.add_parser("batch", help="convert every deck in a folder")
    b.add_argument("folder")
    b.add_argument("--out")
    b.add_argument("--strand", choices=STRAND_CHOICES)
    b.add_argument("--grade", choices=GRADE_CHOICES)
    b.add_argument("--lang", choices=["en", "es"])

    d = sub.add_parser("detect", help="show detected strand / grade / language")
    d.add_argument("decks", nargs="+")

    sub.add_parser("templates", help="list the templates that were found")

    t = sub.add_parser("train", help="teach the strand detector with decks of a known strand")
    t.add_argument("--strand", required=True, choices=STRAND_CHOICES)
    t.add_argument("decks", nargs="+")

    v = sub.add_parser("verify", help="compare a converted deck with its original")
    v.add_argument("--old", required=True)
    v.add_argument("--new", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "web":
        if args.templates:
            from .service import load_settings, save_settings
            s = load_settings()
            s["templates_dir"] = os.path.abspath(args.templates)
            save_settings(s)
        from .webapp import serve
        serve(args.port, open_browser=not args.no_browser)
        return 0

    if args.cmd == "convert":
        if _missing(args.decks):
            return 1
        return _run(_engine(args), args.decks, args.out, args.strand, args.grade, args.lang)

    if args.cmd == "batch":
        if _missing([args.folder]):
            return 1
        files = []
        for root, _dirs, names in os.walk(args.folder):
            for n in sorted(names):
                if n.lower().endswith(".pptx") and not n.startswith(("~$", "._", "NEW_")):
                    files.append(os.path.join(root, n))
        if not files:
            print("No .pptx files found in %s" % os.path.abspath(args.folder))
            return 1
        return _run(_engine(args), files, args.out, args.strand, args.grade, args.lang, rel_to=args.folder)

    if args.cmd == "detect":
        if _missing(args.decks):
            return 1
        eng = _engine(args)
        for p in args.decks:
            r = eng.detect(p)
            print("%s: %s (%.0f%%), %s%s, %s" % (os.path.basename(p), r["strand"], 100 * r["strand_conf"], r["band"],
                                                 "" if r["band_found"] else " (guessed)",
                                                 "Spanish" if r["language"] == "es" else "English"))
        return 0

    if args.cmd == "templates":
        eng = _engine(args)
        print("Templates folder: %s\n" % os.path.abspath(eng.templates_dir))
        for r in eng.library.status():
            print("  %-8s %-8s %-5s %s" % ("Spanish" if r["lang"] == "es" else "English", r["strand"], r["band"],
                                          r["file"] or "-- MISSING --"))
        return 0

    if args.cmd == "train":
        if _missing(args.decks):
            return 1
        from .classify import deck_text, save_model
        from .service import PROFILES
        eng = _engine(args)
        for p in args.decks:
            eng.model.add(args.strand, deck_text(p))
            print("learned %s as %s" % (os.path.basename(p), args.strand))
        save_model(eng.model, os.path.join(PROFILES, "strand_model.json"))
        return 0

    if args.cmd == "verify":
        if _missing([args.old, args.new]):
            return 1
        from .verify import verify
        eng = _engine(args)
        rep = verify(args.old, args.new, eng.profile)
        print(rep.text())
        return 0 if rep.ok else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
