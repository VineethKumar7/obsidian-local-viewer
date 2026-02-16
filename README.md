# 📚 Obsidian Local Viewer

**View and annotate your Obsidian vault from any device on your local network** — tablets, phones, or other computers.

A lightweight Python server that renders your markdown files beautifully in any web browser. Supports **Apple Pencil annotations** on iPad! No cloud sync, no account needed — your notes stay on your machine.

![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## ✨ Features

### 📁 Navigation & Viewing
- **Folder tree navigation** — Browse your entire vault structure with collapsible folders
- **Beautiful markdown rendering** — Headers, code blocks, tables, lists, blockquotes, LaTeX math
- **Wiki-link support** — `[[links]]` rendered as clickable navigation
- **Dark theme** — Easy on the eyes for extended study sessions

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
- **Video playback** — Plyr.js player (YouTube-style controls)
- **Images** — PNG, JPG, GIF, WebP displayed inline

## 🎯 Who Is This For?

- **Students** studying for exams who want to annotate notes on iPad
- **Learners** tracking study progress with completion checkboxes
- **Anyone with an Obsidian vault** wanting easy multi-device access
- **People who need offline access** to their notes (train, plane, etc.)
- **iPad users** who want to annotate PDFs with Apple Pencil

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
| ✏️ **Annotate** | Enter annotation mode (Apple Pencil) |
| ℹ️ **Info** | View/edit file metadata |
| 🔄 **Sync** | Sync metadata to index tables |
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
