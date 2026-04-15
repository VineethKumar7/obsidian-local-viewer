# 📚 Obsidian Local Viewer

**View and annotate your Obsidian vault from any device on your local network** — tablets, phones, or other computers.

A lightweight Python server that renders your markdown files beautifully in any web browser. Supports **Apple Pencil annotations** on iPad! No cloud sync, no account needed — your notes stay on your machine.

![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## ✨ Features

### 📁 Navigation & Viewing
- **Folder tree navigation** — Browse your entire vault structure with collapsible folders
- **Beautiful markdown rendering** — Headers, code blocks, tables, lists, blockquotes, LaTeX math (including interval notation)
- **Wiki-link support** — `[[links]]` rendered as clickable navigation
- **Image embeds** — `![[image.png]]` Obsidian-style embeds supported
- **Image lightbox** — Click any image to open a zoomable full-screen view
- **Automatic page navigation** — Parent, sibling, child, and backlink pages linked at the bottom of every note
- **Collapsible callouts** — Toggle all callouts open/closed from the three-dot menu
- **Dark theme + toggle** — Easy on the eyes; toggle light/dark with preference persisted in localStorage

### ✏️ Annotations (iPad/Apple Pencil)
- **Pen tool** — Write notes with pressure-sensitive strokes
- **Highlighter** — Semi-transparent highlighting
- **Eraser** — Remove mistakes
- **Color palette** — Multiple colors to choose from
- **Auto-save** — Annotations saved automatically

### ℹ️ File Metadata
- **Info button** — Track study progress per file
- **Completed checkbox** — Mark files as done ✅
- **Revision count** — Track how many times you've reviewed
- **Summary fields** — Add quick notes and detailed summaries
- **Stored in JSON** — Easy to backup, git-trackable

### 🔄 Sync to Index
- **Sync button** — Update index tables with metadata
- **Frontmatter sync** — Metadata synced to file YAML frontmatter
- **Index table updates** — Done/🔄 columns updated automatically

### 📥 Download for Offline
- **One-click ZIP download** — All markdown files as HTML
- **Same folder structure** — Navigate offline just like online
- **CSS embedded** — Dark theme rendered offline
- **Wiki links converted** — Internal links work in offline HTML
- **Auto-generated index.html** — Master navigation page
- **No HTTPS required** — Works on any network

### 📕 Media Support
- **PDF viewer** — Full PDF.js with zoom controls
- **PDF++ split-pane** — Click a `.pdf-plus-embed` thumbnail in a note to slide in a right-side PDF panel that scrolls to the page and highlights the target rectangle
- **PDF region thumbnails** — `/pdf-crop` renders and caches cropped page regions as inline thumbnails (requires `pymupdf`)
- **Video playback** — Plyr.js player (YouTube-style controls)
- **Images** — PNG, JPG, GIF, WebP displayed inline, click to zoom

### 🔗 Graph View
- **Local graph sidebar** — Right-side panel showing the current note's connections (links + backlinks)
- **Interactive** — Draggable resize handle on desktop, touch gestures on mobile, wide zoom range for large graphs
- **Size controls** — Cycle through panel sizes, collapse/expand
- **Mobile-friendly** — Slide-in overlay on phones/tablets, top-right toggle to avoid toolbar overlap
- **Smart filtering** — Bottom-navigation links excluded so the graph stays focused on real content connections

### 🎨 Cheatsheets
Auto-generated one-page summaries from your notes:
- **Quick Definitions** — All `> [!definition]` callouts aggregated into a scannable reference, linked back via wiki-links
- **Memory Tips** — All `> [!tip]` / `> [!mnemonic]` callouts gathered into a quick-recall sheet
- **Relevance controls** — Mark any cheatsheet item as high/medium/low relevance; state persists per vault
- **Smart grouping** — H2 section headers stay bundled with their callouts

---

## 🧠 Study Features

Transform your notes into an active learning system with flashcards, quizzes, and spaced repetition.

### 🎴 Flashcards

Add a `## Flashcards` section to any markdown file:

```markdown
## Flashcards

Q: What is the capital of France?
A: Paris

Q: What does HTTP stand for?
A: HyperText Transfer Protocol

Q: Name the 4 TCP/IP layers
A: LITA - Link, Internet, Transport, Application
```

Or use callouts anywhere in your notes:

```markdown
> [!flashcard] What is DNS?
> Domain Name System - translates domain names to IP addresses
```

### ✅ Multiple Choice Questions (MCQ)

```markdown
## MCQ

Q: Which protocol is connection-oriented?
- [ ] UDP
- [x] TCP
- [ ] ICMP
- [ ] ARP

Q: What port does HTTPS use?
- [ ] 80
- [x] 443
- [ ] 22
- [ ] 21
```

### 📝 Cloze Deletions (Fill-in-the-blank)

```markdown
## Cloze

TCP uses {{sequence numbers}} to track packets.
The OSI model has {{c1::7}} layers.
HTTP is a {{stateless|hint: no memory}} protocol.
DNS operates on port {{53}}.
```

**Syntax options:**
| Syntax | Description |
|--------|-------------|
| `{{text}}` | Simple blank |
| `{{c1::text}}` | Numbered cloze (same number = revealed together) |
| `{{text\|hint}}` | Blank with hint shown |
| `==text==` | Highlight-based cloze |

### 📋 Summary View

Auto-generates a quick summary from your notes. Add summary callouts:

```markdown
> [!summary]
> - TCP is connection-oriented
> - Uses 3-way handshake
> - Guarantees delivery

> [!tip] Remember
> TCP = Reliable, UDP = Fast
```

Or use a dedicated section:

```markdown
## Summary
- Key point 1
- Key point 2
- Key point 3

## TL;DR
- Quick overview here
```

**Bold text** is automatically extracted as key terms.

### 🎲 Mix Mode

Combines all card types (flashcards + MCQ + cloze) into one shuffled study session. Great for variety and comprehensive review.

### 📊 Study Dashboard

Track your learning progress. Available on the **homepage** (vault-wide stats) and inside any **folder view** (scoped to that folder):
- 🔥 **Day streak** — Consecutive days meeting your goal
- **Cards due** — Cards ready for review
- **Mastery %** — Percentage of cards in advanced boxes
- **Weak cards** — Cards you've missed 2+ times, plus a one-click "Weak Cards Mix" study session
- **Heatmap** — 4-week activity calendar with per-day hover tooltips (date + card count)
- **Daily / Weekly / Monthly / Hourly tabs** — Different time-scale views of your review activity
- **Progress bar + chart** — At-a-glance progress on the homepage
- **Review buttons** — Jump straight from the dashboard into due-cards for each folder

### 🎯 Focus Mode

Review weak cards from ALL files in one session. Cards are marked "weak" when:
- Missed 2+ times (lapses ≥ 2)
- Ease factor drops below 2.0

Focus mode shows the source filename on each card.

### ⏱️ Timed Mode

30-second countdown per question:
- Green → Yellow at 15s → Red pulse at 10s
- Auto-submits when timer expires

### 🔄 Spaced Repetition (SRS)

Built-in SM-2 algorithm tracks optimal review intervals:

| Rating | Button | Effect |
|--------|--------|--------|
| 1 | Again | Reset to 10 minutes |
| 2 | Hard | Interval × 1.2 |
| 3 | Good | Interval × ease factor |
| 4 | Easy | Interval × ease factor × 1.3 |

**Data storage:** SRS data saved alongside each file as `filename.md.srs.json`

### 📦 Leitner Box System

Simpler alternative to SRS with 5 boxes:

| Box | Review Interval |
|-----|-----------------|
| 1 | Daily |
| 2 | Every 2 days |
| 3 | Every 4 days |
| 4 | Weekly |
| 5 | Bi-weekly (mastered) |

Correct → move up. Wrong → back to Box 1.

### 📝 Exam Mode

Full exam simulation with mixed question types — MCQ, flashcards, and cloze deletions in a single timed session. Supports different question types in one paper and gives you a final score breakdown.

### ⚙️ Settings Panel

Tune your study experience — daily goal, timer length, default study mode, and more. Settings persist per vault.

### 📂 JSON Study File Support

In addition to writing cards inline in markdown, you can drop a `filename.json` next to any `filename.md` to define flashcards, MCQ, and cloze cards in structured form. Useful when migrating from Anki or generating cards programmatically. SRS data is stored alongside as `filename.md.srs.json`.

---

## 🔧 Study Toolbar Buttons

| Button | Function |
|--------|----------|
| 🎴 **Flashcards** | Study flashcards from current file |
| ✅ **MCQ** | Multiple choice quiz |
| 📝 **Cloze** | Fill-in-the-blank practice |
| 🎲 **Mix Mode** | Shuffled mix of all card types |
| 📊 **Dashboard** | View study progress & stats |
| 📋 **Summary** | Quick summary of current file |
| 🎓 **Exam Mode** | Full timed exam simulation (mixed question types) |
| 🎨 **Cheatsheet** | Open Quick Definitions / Memory Tips for current section |
| 🔗 **Graph** | Toggle local graph sidebar |
| ⚙️ **Settings** | Study preferences panel |

---

## 🎯 Who Is This For?

- **Students** studying for exams with flashcards, quizzes, and spaced repetition
- **Learners** tracking study progress with completion checkboxes and streaks
- **iPad users** who want to annotate notes and PDFs with Apple Pencil
- **Anyone with an Obsidian vault** wanting easy multi-device access
- **People who need offline access** to their notes (train, plane, etc.)
- **Active learners** who want to turn notes into testable knowledge

## 📦 Installation

### Prerequisites

- Python 3.7 or higher
- pip (Python package manager)

### Quick Install

```bash
# Clone the repository
git clone https://github.com/VineethKumar7/obsidian-local-viewer.git
cd obsidian-local-viewer

# Install dependencies
pip install -r requirements.txt
```

> `pymupdf` (installed via `requirements.txt`) is required for PDF++ region thumbnails. If it isn't available, the rest of the viewer still works — only the `/pdf-crop` endpoint is disabled.

## 🚀 Usage

### Basic Usage

```bash
python app.py /path/to/your/vault
```

### Examples

```bash
# View your Obsidian vault
python app.py ~/Documents/MyVault

# Use a custom port
python app.py ~/Documents/MyVault --port 8080

# Restrict to localhost only (no network access)
python app.py ~/Documents/MyVault --host 127.0.0.1

# Enable HTTPS (for PWA features)
python app.py ~/Documents/MyVault --ssl
```

## 📱 Accessing from Other Devices

1. Start the server on your computer
2. Note the **Network URL** shown (e.g., `http://192.168.1.100:5000`)
3. Open that URL on your tablet/phone (must be on the same WiFi network)
4. Browse and read your notes!

```
╔══════════════════════════════════════════════════════╗
║           📚 Obsidian Local Viewer                   ║
╠══════════════════════════════════════════════════════╣
║  Vault: MyVault                                      ║
║  Local:   http://localhost:5000                      ║
║  Network: http://192.168.1.100:5000                  ║
╠══════════════════════════════════════════════════════╣
║  Keyboard Shortcuts:                                 ║
║    Ctrl+B  — Toggle sidebar                          ║
║    F11     — Fullscreen                              ║
║    Esc     — Show sidebar                            ║
╚══════════════════════════════════════════════════════╝
```

## 🔧 Toolbar Buttons

| Button | Function |
|--------|----------|
| 📥 **PDF** | Download current page as PDF |
| 📦 **Topic** | Download page + linked subpages as ZIP (with inlined images + MathJax) |
| 🎴 **Flashcards** | Study flashcards from current file |
| ✅ **MCQ** | Multiple choice quiz |
| 📝 **Cloze** | Fill-in-the-blank practice |
| 🎲 **Mix Mode** | Shuffled mix of all card types |
| 🎓 **Exam Mode** | Full timed exam simulation |
| 📊 **Dashboard** | View study progress & stats |
| 📋 **Summary** | Quick summary of current file |
| 🎨 **Cheatsheet** | Quick Definitions / Memory Tips view |
| 🔗 **Graph** | Toggle local graph sidebar |
| ⚙️ **Settings** | Study preferences panel |
| 🌓 **Theme** | Toggle light / dark mode |
| ✏️ **Annotate** | Enter annotation mode (Apple Pencil) |
| ℹ️ **Info** | View/edit file metadata |
| 🔄 **Sync** | Sync metadata to index tables |
| ⋯ | Three-dot menu (collapse callouts, and more) |
| ⛶ | Toggle fullscreen |

## 📥 Offline Download

Click **"Download for Offline"** in the sidebar to get a ZIP file with:

```
EWADIS_Offline.zip
├── index.html              ← Master navigation page
├── Diagrams/
│   ├── Domain_Model.html
│   └── Subtopics/
│       └── Association_vs_Aggregation.html
├── Lecture_Notes/
│   ├── Lecture_1.html
│   └── Lecture_2.html
└── ...
```

- All `.md` files converted to `.html`
- Dark theme CSS embedded
- Wiki links (`[[]]`) converted to relative HTML links
- Works completely offline in any browser

## 📁 File Metadata Storage

Metadata is stored in `obsidian-viewer-meta.json` at vault root:

```json
{
  "Notes/lecture1.md": {
    "completed": true,
    "revision_count": 3,
    "summary": "Key concepts covered",
    "created_date": "2024-01-15"
  }
}
```

## 🔧 Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `vault_path` | Path to your Obsidian vault | Required |
| `--port`, `-p` | Port to run the server on | 5000 |
| `--host`, `-H` | Host to bind to | 0.0.0.0 |
| `--ssl` | Enable HTTPS with self-signed cert | Off |

## 📁 Supported File Types

| Type | Extension | How it's displayed |
|------|-----------|-------------------|
| Markdown | `.md` | Rendered as formatted HTML |
| PDF | `.pdf` | PDF.js viewer with zoom |
| Video | `.mp4`, `.mkv`, `.avi`, `.mov`, `.webm` | Plyr.js player |
| Images | `.png`, `.jpg`, `.gif`, `.webp` | Displayed inline |

## ⌨️ Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+B` | Toggle sidebar |
| `F11` | Toggle fullscreen |
| `Esc` | Show sidebar |
| `P` | Pen tool (annotation mode) |
| `H` | Highlighter (annotation mode) |
| `E` | Eraser (annotation mode) |
| `Ctrl+Z` | Undo |
| `Ctrl+Y` | Redo |

## 🔒 Security Notes

- Server only serves files from the specified vault directory
- Directory traversal attacks are prevented
- By default, accessible to anyone on your local network
- Use `--host 127.0.0.1` for localhost-only access

## ⚠️ Known Limitations

- **Long PDFs**: Annotation canvas capped at ~16,000px height due to browser limits
- **No cloud sync**: Annotations are local to the server machine

## 🤝 Contributing

Contributions welcome! Feel free to report bugs, suggest features, or submit PRs.

## 📄 License

MIT License — feel free to use this however you want.

## 🙏 Acknowledgments

- Built for the [Obsidian](https://obsidian.md/) community
- [Flask](https://flask.palletsprojects.com/) for the web server
- [Python-Markdown](https://python-markdown.github.io/) for rendering
- [PDF.js](https://mozilla.github.io/pdf.js/) for PDF viewing
- [Plyr](https://plyr.io/) for video playback
- [MathJax](https://www.mathjax.org/) for LaTeX rendering

---

**Made with ❤️ for note-takers everywhere**
