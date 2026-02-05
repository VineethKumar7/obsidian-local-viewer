# 📚 Obsidian Local Viewer

**View your Obsidian vault from any device on your local network** — tablets, phones, or other computers.

A lightweight Python server that renders your markdown files beautifully in any web browser. No cloud sync, no account needed — your notes stay on your machine.

![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## ✨ Features

- 📁 **Folder tree navigation** — Browse your entire vault structure
- 📄 **Beautiful markdown rendering** — Headers, code blocks, tables, lists, blockquotes
- 📕 **PDF support** — View PDFs directly in the browser
- 🖼️ **Image support** — PNG, JPG, GIF, WebP displayed inline
- 📱 **Mobile-friendly** — Responsive design works great on tablets
- 🔒 **Local only** — Your notes never leave your network
- ⚡ **Fast & lightweight** — No heavy dependencies

## 🎯 Who Is This For?

- **Students** who want to study their notes on a tablet while their laptop runs the server
- **Writers** who want to read their drafts on a different device
- **Anyone with an Obsidian vault** (or any markdown folder) who wants easy access from other devices on their WiFi
- **People who don't want to pay for Obsidian Sync** but still want multi-device access at home

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

### Manual Install

```bash
pip install flask markdown
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
```

### Using Environment Variable

```bash
export OBSIDIAN_VAULT_PATH=~/Documents/MyVault
python app.py
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
╚══════════════════════════════════════════════════════╝
```

## 🔧 Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `vault_path` | Path to your Obsidian vault or markdown folder | Required |
| `--port`, `-p` | Port to run the server on | 5000 |
| `--host`, `-H` | Host to bind to (`0.0.0.0` for network, `127.0.0.1` for local only) | 0.0.0.0 |

## 📁 Supported File Types

| Type | Extension | How it's displayed |
|------|-----------|-------------------|
| Markdown | `.md` | Rendered as formatted HTML |
| PDF | `.pdf` | Displayed in browser's PDF viewer |
| Images | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` | Displayed inline |
| Text | `.txt` | Shown as plain text |

## 🔒 Security Notes

- The server only serves files from the specified vault directory
- Directory traversal attacks are prevented
- By default, the server is accessible to anyone on your local network
- Use `--host 127.0.0.1` if you want localhost-only access

## 🛠️ Running as a Service (Optional)

### Linux (systemd)

Create `/etc/systemd/system/obsidian-viewer.service`:

```ini
[Unit]
Description=Obsidian Local Viewer
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/obsidian-local-viewer
ExecStart=/usr/bin/python3 app.py /path/to/your/vault
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl enable obsidian-viewer
sudo systemctl start obsidian-viewer
```

### macOS (launchd)

Create `~/Library/LaunchAgents/com.obsidian-viewer.plist` with appropriate configuration.

## 🤝 Contributing

Contributions are welcome! Feel free to:

- Report bugs
- Suggest features
- Submit pull requests

## 📄 License

MIT License — feel free to use this however you want.

## 🙏 Acknowledgments

- Built for the [Obsidian](https://obsidian.md/) community
- Uses [Flask](https://flask.palletsprojects.com/) for the web server
- Uses [Python-Markdown](https://python-markdown.github.io/) for rendering

---

**Made with ❤️ for note-takers everywhere**
