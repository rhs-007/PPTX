# PPT Template Migrator

Moves old lesson decks onto the new Phonics, Reading and Writing templates without changing the content.
Text, pictures, animations, speaker notes, hidden slides, transitions, fonts, text colours and slide types all
stay the same. Each converted deck is checked against its original, and you get a report for every deck.

## Start it on Windows

1. Unzip this folder anywhere, for example `Downloads\ppt_template_migrator`.
2. Double-click **`Start.bat`**.
   - On the first run it installs two helper packages (lxml, Pillow). This happens only once.
   - A black window opens and your browser shows the app at `http://127.0.0.1:8765/`.
   - Keep the black window open while you work. Close it when you're done.

If double-clicking doesn't work, open PowerShell in this folder and run `py -m ppt_migrator`.

## Start it on a Mac

**Once per Mac:** you need Python 3. If you don't have it, install it from
[python.org/downloads/macos](https://www.python.org/downloads/macos/) (the "macOS 64-bit universal2 installer").
`Start.command` opens that page for you if Python is missing.

1. Double-click the zip to unzip it, and move the `ppt_template_migrator` folder where you want it, for example
   into Documents.
2. Open the folder, then **Control-click (right-click) `Start.command` → Open → Open**.
   - macOS asks this only the first time, because the file came from the internet.
   - If there is no **Open** button, click **Done**. Then go to **System Settings → Privacy & Security**,
     scroll down, and click **Open Anyway** next to "Start.command".
3. A Terminal window opens.
   - On the first run it sets itself up, which takes about a minute and needs internet.
   - Then Safari (or your default browser) shows the app at `http://127.0.0.1:8765/`.
   - Keep the Terminal window open while you work. Close it when you're done.

After the first time, just double-click `Start.command`.

**If macOS still won't open it**, open Terminal (Applications → Utilities), type `bash ` (with a space), drag
`Start.command` into the Terminal window, and press Return.

The app runs only on your own computer, on Windows and Mac alike. Nothing is uploaded to the internet.

## Use it

1. **Drop the old decks** onto the page. You can drop several `.pptx` files, or a `.zip` of whole folders.
2. Check the **Strand / Grade / Language** columns.
   - The app reads each deck and fills these in itself.
   - An amber box means it wasn't sure, so please check that one.
   - If you change a strand, the app learns from it for next time.
3. Click **Convert all**.
4. Click **Download** for each deck, or **Download all (.zip)**.
   - Each converted deck is named `NEW_` + the old name, for example
     `PRES_DL_GK_U4_W1_D1_L1_Slides.pptx` → `NEW_PRES_DL_GK_U4_W1_D1_L1_Slides.pptx`.
   - The decks are also saved in the `output` folder. **Open output folder** takes you there.
5. Click **Report** to see what changed on each slide and the verification table.

## How it picks the template

| | Grades K–1 | Grades 2–5 |
|---|---|---|
| English | `New_Final_GK-1 <Strand> Template.pptx` | `New_Final_G2-5 <Strand> Template.pptx` |
| Spanish | `DL_Final_GK-1 <Strand> Template.pptx` | `DL_Final_G2-5 <Strand> Template.pptx` |

- **Grade** comes from the file name (`_GK_`, `_G1_` → K–1; `_G2_` … `_G5_` → 2–5).
- **Strand** comes from the deck's own words, with folder names as a hint.
- **Language** comes from the slide text.

All 12 templates are already in the `templates` folder. To replace one, use **Add template files…** on the page,
or copy the new file into `templates`. The app recognises a template by the grade band and strand in its file
name (for example "GK-1" and "Reading"). It tells English from Spanish by the template's own text.

## What the converter does on purpose

- **Slide size** always comes from the template (13.333 × 7.5 in for all 12 templates). This holds even when
  the old deck has a different size, as some English Phonics decks do (13.330 in wide).
- **Comments** (the review comments in PowerPoint's Comments pane) are not copied to the converted deck. The
  report says how many were left out. Speaker notes are always kept.
- **Opening and Learning Outcome slides** use the template's own design. The text is moved into the matching
  places, and the old pasted design pictures are left out.
- **Teaching slides**: the title and time go to the new header bar. Objectives go in the Learning Outcomes / Teach
  panel, the student text and icons go in the Collaborate panel, and the routine box keeps its place. Slide content
  is scaled to fit the new content area. Animations still point at the same pictures and text.
- **Designer notes** (red art specs, "please insert / crop…", notes to DSN, FOR PRINT TRS) go on the slide,
  in the left column under the Collaborate box and above the routine. They keep their red colour and
  highlights, and the text is made smaller only if needed to fit. If they still don't fit at a readable size,
  they go in the right column under the Teach box. On opening and Learning Outcome slides they stay where
  they were.
- **Anything outside the old slide** (notes or pictures parked in the grey area around the slide) is not
  copied to the new deck. The report lists each item that was left out.
- The old template's unfilled **"X MIN."** box is removed, because the new header holds the time.
- If a long panel doesn't fit, its text is made a little smaller. The report says by how much (for example
  "88% size").

The **Please check** section of each report lists anything worth a quick look. For example, the lesson number in
the header might not match the title, or the header time might not match the notes.

## Panel headings

The panels are headed *Learning Outcomes / Teach / Collaborate*. In Spanish they are *Objetivos de aprendizaje /
Enseñar / Colaborar*. You can change these under **Settings – panel headings** on the page.

## Without the web page

```
py -m ppt_migrator convert "C:\decks\PRES_DL_GK_U4_W1_D1_L1_Slides.pptx"
py -m ppt_migrator batch "C:\decks\Week 1"           # every deck in the folder and its subfolders
py -m ppt_migrator convert deck.pptx --strand Reading --grade G2-5 --lang es   # override detection
py -m ppt_migrator detect deck.pptx                  # just show strand / grade / language
py -m ppt_migrator templates                         # which templates were found
py -m ppt_migrator train --strand Writing deck1.pptx deck2.pptx   # teach the detector
```

On Windows you can also drag a folder of old decks onto **`Convert_Folder.bat`**.
On a Mac, double-click **`Convert_Folder.command`** and choose the folder.

On a Mac, use the Python inside the app folder for these commands: `cd` into the folder, then
`.venv/bin/python3 -m ppt_migrator …` (after `Start.command` has run once).

## Troubleshooting

- **"pip.exe failed to run"**: use `py -m pip install -r requirements.txt` instead of `pip …`.
- **"Could not save"**: close the converted deck in PowerPoint, then convert again.
- **A deck shows "No template found"**: one of the 12 templates is missing or renamed. The Templates section on
  the page shows which one.
- **Port already in use**: the app picks the next free port by itself. Use the address shown in the black
  (Terminal) window.
- **Mac: "Python 3 is not installed"**: install Python from python.org, then double-click `Start.command` again.
- **Mac: "Could not download the two helper packages"**: the first run needs internet. Connect and try again.
