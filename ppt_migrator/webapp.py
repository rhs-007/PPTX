"""Local web app: drag decks in, check the detected strand / grade / language, convert,
download the renamed decks and read the reports. Standard library only."""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .classify import deck_text, save_model, STRANDS
from .service import Engine, APP_DIR, PROFILES, load_settings, save_settings, output_name

WORK = os.path.join(APP_DIR, "work")
STATE = {"engine": None, "items": {}, "batch": None, "lock": threading.Lock()}
MAX_UPLOAD = 600 * 1024 * 1024


def engine():
    if STATE["engine"] is None:
        STATE["engine"] = Engine()
    return STATE["engine"]


def safe_name(name):
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "deck.pptx"


def add_deck(path, display):
    eng = engine()
    iid = uuid.uuid4().hex[:10]
    try:
        det = eng.detect(path, display)
        err = None
    except Exception as e:  # unreadable file
        det, err = None, "Could not read this file: %s" % e
    item = {"id": iid, "name": os.path.basename(display), "display": display, "path": path,
            "detected": det, "error": err, "result": None}
    STATE["items"][iid] = item
    return item


def public(item):
    r = item.get("result") or {}
    out = {k: item[k] for k in ("id", "name", "display", "detected", "error")}
    if r:
        out["result"] = {k: r.get(k) for k in ("ok", "verified", "output_name", "problems", "to_check",
                                                "moves", "slides", "seconds", "error", "template",
                                                "strand", "band", "language")}
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "PPTMigrator/2"

    def log_message(self, fmt, *args):  # quiet console
        pass

    # ---------------- helpers ----------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("file too large")
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(min(1 << 20, n - len(data)))
            if not chunk:
                break
            data += chunk
        return data

    def _json(self):
        try:
            return json.loads(self._body() or b"{}")
        except ValueError:
            return {}

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    # ---------------- routes ----------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/api/state":
            eng = engine()
            labels = {}
            for lang in ("en", "es"):
                d = dict(eng.profile.get("labels", {}).get(lang, {}))
                d.update(eng.settings.get("labels", {}).get(lang, {}))
                labels[lang] = d
            return self._send(200, {"templates": eng.library.status(), "templates_dir": eng.templates_dir,
                                    "output_dir": eng.settings["output_dir"], "labels": labels,
                                    "items": [public(i) for i in STATE["items"].values()],
                                    "learn": eng.settings.get("learn_from_corrections", True)})
        m = re.match(r"^/api/download/(\w+)$", path)
        if m:
            item = STATE["items"].get(m.group(1))
            r = (item or {}).get("result") or {}
            if not r.get("output") or not os.path.exists(r["output"]):
                return self._send(404, {"error": "not converted yet"})
            with open(r["output"], "rb") as f:
                data = f.read()
            return self._send(200, data, "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                              {"Content-Disposition": "attachment; filename*=UTF-8''%s" %
                               urllib.parse.quote(r["output_name"])})
        m = re.match(r"^/api/report/(\w+)$", path)
        if m:
            item = STATE["items"].get(m.group(1))
            r = (item or {}).get("result") or {}
            return self._send(200, {"text": r.get("report_text") or r.get("error") or ""})
        if path == "/api/download_all":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for item in STATE["items"].values():
                    r = item.get("result") or {}
                    if r.get("output") and os.path.exists(r["output"]):
                        folder = r.get("strand") or ""
                        z.write(r["output"], os.path.join(folder, r["output_name"]))
                        if r.get("report") and os.path.exists(r["report"]):
                            z.write(r["report"], os.path.join(folder, "reports", os.path.basename(r["report"])))
            return self._send(200, buf.getvalue(), "application/zip",
                              {"Content-Disposition": "attachment; filename=converted_decks_%s.zip"
                               % time.strftime("%Y%m%d_%H%M")})
        if path == "/api/open_output":
            d = engine().settings["output_dir"]
            os.makedirs(d, exist_ok=True)
            open_folder(d)
            return self._send(200, {"ok": True, "dir": d})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        q = self._query()
        try:
            if path == "/api/upload":
                name = safe_name((q.get("name") or ["deck.pptx"])[0])
                rel = (q.get("rel") or [name])[0]
                data = self._body()
                sess = os.path.join(WORK, "uploads", uuid.uuid4().hex[:8])
                os.makedirs(sess, exist_ok=True)
                added = []
                if name.lower().endswith(".zip"):
                    try:
                        z = zipfile.ZipFile(io.BytesIO(data))
                    except zipfile.BadZipFile:
                        return self._send(400, {"error": "This .zip file could not be opened"})
                    with z:
                        for info in z.infolist():
                            if info.is_dir() or not info.filename.lower().endswith(".pptx"):
                                continue
                            base = safe_name(info.filename)
                            if base.startswith(("~$", "._")) or "__MACOSX" in info.filename:
                                continue
                            dst = os.path.join(sess, uuid.uuid4().hex[:6] + "_" + base)
                            with open(dst, "wb") as f:
                                f.write(z.read(info))
                            inner = info.filename.replace("\\", "/")
                            # the zip's own name ("Reading.zip") is a useful strand hint
                            zstem = os.path.splitext(name)[0]
                            display = inner if inner.lower().startswith(zstem.lower() + "/") else zstem + "/" + inner
                            added.append(add_deck(dst, display))
                elif name.startswith(("._", "~$")):
                    return self._send(400, {"error": "This is a hidden helper file, not a deck"})
                elif name.lower().endswith(".pptx"):
                    dst = os.path.join(sess, name)
                    with open(dst, "wb") as f:
                        f.write(data)
                    added.append(add_deck(dst, rel))
                else:
                    return self._send(400, {"error": "Only .pptx files or a .zip of .pptx files"})
                return self._send(200, {"items": [public(i) for i in added]})

            if path == "/api/remove":
                body = self._json()
                STATE["items"].pop(body.get("id"), None)
                return self._send(200, {"ok": True})

            if path == "/api/clear":
                STATE["items"].clear()
                return self._send(200, {"ok": True})

            if path == "/api/convert":
                body = self._json()
                item = STATE["items"].get(body.get("id"))
                if not item:
                    return self._send(404, {"error": "unknown deck"})
                eng = engine()
                strand = body.get("strand") or (item.get("detected") or {}).get("strand") or "Other"
                out_dir = os.path.join(eng.settings["output_dir"], body.get("batch") or time.strftime("%Y%m%d"), strand)
                name = item["name"]
                taken = {os.path.normcase(os.path.abspath((i.get("result") or {}).get("output") or ""))
                         for i in STATE["items"].values() if i is not item}
                stem, ext = os.path.splitext(name)
                n = 2
                while os.path.normcase(os.path.abspath(os.path.join(out_dir, output_name(eng.profile, name)))) in taken:
                    name = "%s (%d)%s" % (stem, n, ext)
                    n += 1
                with STATE["lock"]:
                    res = eng.convert(item["path"], out_dir, display_name=name,
                                      strand=body.get("strand"), band=body.get("band"), lang=body.get("lang"))
                    item["result"] = res
                    self._learn(item, body)
                return self._send(200, public(item))

            if path == "/api/templates/upload":
                eng = engine()
                name = safe_name((q.get("name") or ["template.pptx"])[0])
                if not name.lower().endswith((".pptx", ".potx")):
                    return self._send(400, {"error": "Templates must be .pptx files"})
                os.makedirs(eng.templates_dir, exist_ok=True)
                with open(os.path.join(eng.templates_dir, name), "wb") as f:
                    f.write(self._body())
                eng.library.scan()
                return self._send(200, {"templates": eng.library.status()})

            if path == "/api/settings":
                body = self._json()
                eng = engine()
                s = load_settings()
                if "labels" in body:
                    s["labels"] = body["labels"]
                if "learn" in body:
                    s["learn_from_corrections"] = bool(body["learn"])
                if body.get("templates_dir"):
                    s["templates_dir"] = body["templates_dir"]
                if body.get("output_dir"):
                    s["output_dir"] = body["output_dir"]
                save_settings(s)
                STATE["engine"] = Engine()
                return self._send(200, {"ok": True})
        except Exception as e:
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        return self._send(404, {"error": "not found"})

    def _learn(self, item, body):
        """If a person corrected the strand, remember it so the next deck is detected better."""
        eng = engine()
        det = item.get("detected") or {}
        chosen = body.get("strand")
        if not eng.settings.get("learn_from_corrections", True) or not chosen or chosen not in STRANDS:
            return
        if chosen != det.get("strand") or det.get("strand_conf", 1) < 0.6:
            try:
                eng.model.add(chosen, deck_text(item["path"]))
                save_model(eng.model, os.path.join(PROFILES, "strand_model.json"))
                item["result"]["learned"] = True
            except Exception:
                pass


def open_folder(path):
    """Show a folder in Explorer (Windows), Finder (Mac) or the file manager (Linux)."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def free_port(preferred=8765):
    for port in [preferred] + list(range(8766, 8800)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def serve(port=None, open_browser=True):
    port = port or free_port()
    os.makedirs(WORK, exist_ok=True)
    shutil.rmtree(os.path.join(WORK, "uploads"), ignore_errors=True)
    engine()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("PPT Template Migrator is running at %s" % url)
    print("Keep this window open while you use it. Close it (or press Ctrl+C) to stop.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PPT Template Migrator</title>
<style>
  :root{--ink:#1d2a33;--muted:#5d6b75;--line:#dde3e7;--bg:#f4f6f7;--card:#fff;--brand:#1f8f86;--brand-2:#e8f5f3;
        --warn:#a85d00;--warn-bg:#fff4e0;--bad:#b3261e;--bad-bg:#fdecea;--good:#18794e;--good-bg:#e6f4ec;}
  *{box-sizing:border-box}
  body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;color:var(--ink);background:var(--bg);font-size:14px}
  header{background:#163a44;color:#fff;padding:18px 28px;display:flex;align-items:baseline;gap:16px}
  header h1{margin:0;font-size:20px;font-weight:600;letter-spacing:.2px}
  header p{margin:0;color:#b9d3d6;font-size:13px}
  main{max-width:1180px;margin:22px auto;padding:0 20px;display:grid;gap:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
  .card h2{margin:0 0 12px;font-size:15px;font-weight:600;display:flex;justify-content:space-between;align-items:center}
  .muted{color:var(--muted)}
  .drop{border:2px dashed #b8c7cc;border-radius:10px;padding:34px;text-align:center;cursor:pointer;background:#fafcfc;transition:.15s}
  .drop.over{border-color:var(--brand);background:var(--brand-2)}
  .drop b{font-size:15px}
  table{width:100%;border-collapse:collapse}
  th{font-weight:600;text-align:left;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.4px;padding:8px 6px;border-bottom:1px solid var(--line)}
  td{padding:9px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
  td.name{max-width:330px;word-break:break-all}
  select,input[type=text]{font:inherit;padding:5px 7px;border:1px solid #c5d0d5;border-radius:6px;background:#fff}
  select.unsure{border-color:var(--warn);background:var(--warn-bg)}
  button{font:inherit;border:1px solid #c5d0d5;background:#fff;border-radius:7px;padding:7px 13px;cursor:pointer}
  button.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
  button:disabled{opacity:.5;cursor:default}
  button.link{border:none;background:none;color:var(--brand);padding:2px 4px;text-decoration:underline}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .ok{background:var(--good-bg);color:var(--good)} .chk{background:var(--warn-bg);color:var(--warn)}
  .err{background:var(--bad-bg);color:var(--bad)} .run{background:#e7eef9;color:#2853a3}
  .tgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
  .tgrid table td{font-size:13px;padding:6px}
  .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
  .small{font-size:12px}
  dialog{border:none;border-radius:12px;max-width:980px;width:92vw;max-height:86vh;padding:0;box-shadow:0 20px 60px rgba(0,0,0,.3)}
  dialog .head{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#fff}
  dialog .body{padding:6px 24px 22px;overflow:auto;max-height:calc(86vh - 60px);line-height:1.5}
  dialog .body table{font-size:12.5px} dialog .body td,dialog .body th{padding:4px 6px}
  .labels{display:grid;grid-template-columns:150px 1fr 1fr;gap:8px 12px;align-items:center;max-width:720px}
  details summary{cursor:pointer;font-weight:600}
  .hint{font-size:12px;color:var(--muted);margin-top:6px}
</style>
</head>
<body>
<header><h1>PPT Template Migrator</h1><p>Old lesson decks &rarr; new Phonics / Reading / Writing templates, content unchanged</p></header>
<main>
  <section class="card">
    <h2>1. Add old decks <span class="muted small">.pptx files, or a .zip of folders (Phonics / Reading / Writing)</span></h2>
    <div id="drop" class="drop"><b>Drop decks here</b><br><span class="muted">or click to choose files</span>
      <input id="files" type="file" accept=".pptx,.zip" multiple hidden></div>
    <div id="deckwrap" style="display:none;margin-top:14px">
      <table><thead><tr><th>Deck</th><th>Strand</th><th>Grade</th><th>Language</th><th>Status</th><th></th></tr></thead>
      <tbody id="decks"></tbody></table>
      <div class="bar">
        <button class="primary" id="convert">Convert all</button>
        <button id="dlall" disabled>Download all (.zip)</button>
        <button id="openout">Open output folder</button>
        <button id="clear" class="link">Clear list</button>
        <span id="summary" class="muted"></span>
      </div>
      <div class="hint">Amber boxes mean the detection was not sure &ndash; please check them. Changing a strand teaches the detector for next time.</div>
    </div>
  </section>

  <section class="card">
    <h2>2. Templates <button id="addtpl">Add template files&hellip;</button><input id="tplfiles" type="file" accept=".pptx,.potx" multiple hidden></h2>
    <div class="tgrid" id="tgrid"></div>
    <div class="hint" id="tpldir"></div>
  </section>

  <section class="card">
    <details><summary>Settings &ndash; panel headings</summary>
      <p class="muted small">Headings the converter writes above the moved text in the side panels.</p>
      <div class="labels">
        <span></span><b>English</b><b>Spanish</b>
        <span>Learning outcomes</span><input type="text" id="l_en_outcomes"><input type="text" id="l_es_outcomes">
        <span>Teacher notes</span><input type="text" id="l_en_teach"><input type="text" id="l_es_teach">
        <span>Student box</span><input type="text" id="l_en_student"><input type="text" id="l_es_student">
      </div>
      <div class="bar"><label><input type="checkbox" id="learn"> Learn from my strand corrections</label>
        <button id="savelabels">Save settings</button><span id="saved" class="muted"></span></div>
    </details>
  </section>
</main>

<dialog id="dlg"><div class="head"><b id="dlgtitle">Report</b><button id="dlgclose">Close</button></div><div class="body" id="dlgbody"></div></dialog>

<script>
const $ = s => document.querySelector(s);
let items = [];
const STRANDS = ["Phonics","Reading","Writing"], BANDS = ["GK-1","G2-5"], LANGS = [["en","English"],["es","Spanish"]];

async function api(path, opts={}) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}

async function loadState(){
  const st = await api("/api/state");
  renderTemplates(st.templates, st.templates_dir);
  for (const lang of ["en","es"]) for (const k of ["outcomes","teach","student"]) $(`#l_${lang}_${k}`).value = (st.labels[lang]||{})[k]||"";
  $("#learn").checked = st.learn;
  items = st.items || []; renderDecks();
}
function renderTemplates(rows, dir){
  const by = {en:[], es:[]}; rows.forEach(r=>by[r.lang].push(r));
  $("#tgrid").innerHTML = ["en","es"].map(l=>`<div><b>${l==="en"?"English":"Spanish"}</b><table>${
    by[l].map(r=>`<tr><td>${r.strand} ${r.band}</td><td>${r.file?`<span class="pill ok">found</span> <span class="muted small">${esc(r.file)}</span>`:`<span class="pill err">missing</span>`}</td></tr>`).join("")}</table></div>`).join("");
  $("#tpldir").textContent = "Templates folder: " + dir;
}
function sel(id, field, options, value, unsure){
  return `<select data-id="${id}" data-f="${field}" class="${unsure?"unsure":""}">${options.map(o=>{const [v,t]=Array.isArray(o)?o:[o,o];return `<option value="${v}" ${v===value?"selected":""}>${t}</option>`}).join("")}</select>`;
}
function status(it){
  if (it.error) return `<span class="pill err" title="${esc(it.error)}">cannot read</span>`;
  if (it.busy) return `<span class="pill run">converting&hellip;</span>`;
  const r = it.result; if (!r) return `<span class="muted">ready</span>`;
  if (r.error) return `<span class="pill err" title="${esc(r.error)}">failed</span> <span class="small">${esc(r.error).slice(0,120)}</span>`;
  const checks = (r.to_check||[]).length;
  return r.verified ? `<span class="pill ok">done &#10003;</span>${checks?` <span class="pill chk">${checks} to look at</span>`:""}`
                    : `<span class="pill chk">check report</span>`;
}
function renderDecks(){
  $("#deckwrap").style.display = items.length ? "" : "none";
  $("#decks").innerHTML = items.map(it=>{
    const d = it.detected || {}; const r = it.result;
    const strand = it.strand || d.strand || "Phonics", band = it.band || d.band || "GK-1", lang = it.lang || d.language || "en";
    return `<tr><td class="name"><b>${esc(it.name)}</b>${r&&r.output_name?`<div class="small muted">&rarr; ${esc(r.output_name)}</div>`:""}</td>
      <td>${sel(it.id,"strand",STRANDS,strand,d.strand_conf!==undefined && d.strand_conf<0.6)}</td>
      <td>${sel(it.id,"band",BANDS,band,d.band_found===false)}</td>
      <td>${sel(it.id,"lang",LANGS,lang,d.language_conf!==undefined && d.language_conf<0.3)}</td>
      <td>${status(it)}</td>
      <td>${r&&r.ok?`<a href="/api/download/${it.id}"><button>Download</button></a> <button class="link" data-rep="${it.id}">Report</button>`:""}
          <button class="link" data-rm="${it.id}" title="Remove">&times;</button></td></tr>`;
  }).join("");
  const done = items.filter(i=>i.result&&i.result.ok).length;
  $("#dlall").disabled = !done;
  $("#summary").textContent = done ? `${done} of ${items.length} converted` : `${items.length} deck(s) ready`;
}
$("#decks").addEventListener("change", e=>{
  const s = e.target; if (s.tagName!=="SELECT") return;
  const it = items.find(i=>i.id===s.dataset.id); it[s.dataset.f] = s.value; s.classList.remove("unsure");
});
$("#decks").addEventListener("click", async e=>{
  const rep = e.target.dataset.rep, rm = e.target.dataset.rm;
  if (rep) { const t = await api("/api/report/"+rep); showReport(items.find(i=>i.id===rep).name, t.text); }
  if (rm) { await api("/api/remove",{method:"POST",body:JSON.stringify({id:rm})}); items = items.filter(i=>i.id!==rm); renderDecks(); }
});

async function upload(files){
  for (const f of files){
    const tmp = {id:"tmp"+Math.random(), name:f.name, busy:false, detected:null};
    $("#summary").textContent = "Reading " + f.name + "…"; $("#deckwrap").style.display="";
    try{
      const rel = f.webkitRelativePath || f.name;
      const res = await api(`/api/upload?name=${encodeURIComponent(f.name)}&rel=${encodeURIComponent(rel)}`, {method:"POST", body:f});
      items.push(...res.items); renderDecks();
    }catch(err){ alert(f.name + ": " + err.message); }
  }
  renderDecks();
}
const drop = $("#drop");
drop.addEventListener("click", ()=>$("#files").click());
$("#files").addEventListener("change", e=>{upload([...e.target.files]); e.target.value="";});
["dragenter","dragover"].forEach(ev=>drop.addEventListener(ev, e=>{e.preventDefault(); drop.classList.add("over");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev, e=>{e.preventDefault(); drop.classList.remove("over");}));
drop.addEventListener("drop", e=>upload([...e.dataTransfer.files].filter(f=>/\.(pptx|zip)$/i.test(f.name))));

$("#convert").addEventListener("click", async ()=>{
  const btn = $("#convert"); btn.disabled = true;
  const batch = new Date().toISOString().slice(0,16).replace(/[-:T]/g,"");
  for (const it of items){
    if (it.error) continue;
    it.busy = true; renderDecks();
    const d = it.detected||{};
    try{
      const res = await api("/api/convert",{method:"POST",body:JSON.stringify({id:it.id, batch,
        strand: it.strand||d.strand, band: it.band||d.band, lang: it.lang||d.language})});
      Object.assign(it, res);
    }catch(err){ it.result = {error: err.message}; }
    it.busy = false; renderDecks();
  }
  btn.disabled = false;
});
$("#dlall").addEventListener("click", ()=>{ location.href = "/api/download_all"; });
$("#openout").addEventListener("click", async ()=>{ const r = await api("/api/open_output"); $("#summary").textContent = "Output folder: " + r.dir; });
$("#clear").addEventListener("click", async ()=>{ await api("/api/clear",{method:"POST"}); items=[]; renderDecks(); });

$("#addtpl").addEventListener("click", ()=>$("#tplfiles").click());
$("#tplfiles").addEventListener("change", async e=>{
  for (const f of e.target.files){
    try{ const r = await api(`/api/templates/upload?name=${encodeURIComponent(f.name)}`,{method:"POST",body:f}); renderTemplates(r.templates, $("#tpldir").textContent.replace("Templates folder: ","")); }
    catch(err){ alert(f.name+": "+err.message); }
  }
  e.target.value="";
});
$("#savelabels").addEventListener("click", async ()=>{
  const labels = {}; for (const lang of ["en","es"]) { labels[lang]={}; for (const k of ["outcomes","teach","student"]) labels[lang][k]=$(`#l_${lang}_${k}`).value.trim(); }
  await api("/api/settings",{method:"POST",body:JSON.stringify({labels, learn: $("#learn").checked})});
  $("#saved").textContent = "Saved."; setTimeout(()=>$("#saved").textContent="",2500);
});

function md(text){
  const lines = text.split("\n"); let html = "", inList = false, inTable = false;
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/`(.+?)`/g,"<code>$1</code>");
  for (const ln of lines){
    if (/^\|/.test(ln)){ if (/^\|[-| ]+\|$/.test(ln)) continue;
      const cells = ln.split("|").slice(1,-1).map(c=>inline(c.trim()));
      if (!inTable){ html += "<table><tr>"+cells.map(c=>`<th>${c}</th>`).join("")+"</tr>"; inTable = true; }
      else html += "<tr>"+cells.map(c=>`<td>${c}</td>`).join("")+"</tr>"; continue; }
    if (inTable){ html += "</table>"; inTable=false; }
    if (/^- /.test(ln)){ if(!inList){html+="<ul>";inList=true;} html += `<li>${inline(ln.slice(2))}</li>`; continue; }
    if (inList){ html+="</ul>"; inList=false; }
    const h = ln.match(/^(#{1,3}) (.*)/);
    if (h) html += `<h${h[1].length+1}>${inline(h[2])}</h${h[1].length+1}>`;
    else if (ln.trim()) html += `<p>${inline(ln)}</p>`;
  }
  return html + (inList?"</ul>":"") + (inTable?"</table>":"");
}
function showReport(name, text){ $("#dlgtitle").textContent = "Report – " + name; $("#dlgbody").innerHTML = md(text); $("#dlg").showModal(); }
$("#dlgclose").addEventListener("click", ()=>$("#dlg").close());
loadState();
</script>
</body>
</html>
"""
