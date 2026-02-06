#!/usr/bin/env python3
"""
Obsidian Local Viewer - View your Obsidian vault from any device on your network
"""

from flask import Flask, render_template_string, send_file, abort, redirect, url_for, Response
import os
import sys
import argparse
import socket
import markdown
import tempfile
import subprocess

app = Flask(__name__)

# Will be set via command line or environment variable
VAULT_PATH = None

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }} - Obsidian Viewer</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; height: 100vh; background: #f5f5f5; }
        
        /* Sidebar */
        .sidebar { 
            width: 300px; 
            background: #1e1e1e; 
            color: #ccc; 
            padding: 20px; 
            overflow-y: auto; 
            flex-shrink: 0;
            box-shadow: 2px 0 10px rgba(0,0,0,0.3);
            transition: transform 0.3s ease, width 0.3s ease;
            position: relative;
            z-index: 100;
        }
        .sidebar.hidden { 
            transform: translateX(-100%);
            width: 0;
            padding: 0;
            overflow: hidden;
        }
        .sidebar h2 { 
            color: #fff; 
            margin-bottom: 20px; 
            font-size: 18px; 
            padding-bottom: 15px;
            border-bottom: 1px solid #333;
        }
        .sidebar ul { list-style: none; }
        .sidebar li { margin: 3px 0; }
        .sidebar a { 
            color: #7eb8da; 
            text-decoration: none; 
            font-size: 14px; 
            display: block; 
            padding: 8px 12px; 
            border-radius: 6px;
            transition: background 0.2s;
        }
        .sidebar a:hover { background: #2d2d2d; }
        .sidebar a.active { background: #0066cc; color: white; }
        
        /* Toggle button */
        .toggle-btn {
            position: fixed;
            top: 15px;
            left: 15px;
            z-index: 200;
            background: #1e1e1e;
            color: #fff;
            border: none;
            padding: 12px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 18px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            transition: left 0.3s ease, background 0.2s;
        }
        .toggle-btn:hover { background: #333; }
        .toggle-btn.sidebar-open { left: 315px; }
        
        /* File icons */
        .folder { color: #f0c674 !important; font-weight: 600; }
        .folder::before { content: "📁 "; }
        .file-md::before { content: "📄 "; }
        .file-pdf::before { content: "📕 "; }
        .file-img::before { content: "🖼️ "; }
        
        /* Content area */
        .content { 
            flex: 1; 
            padding: 40px 60px; 
            overflow-y: auto; 
            background: #fff;
            max-width: 1200px;
            margin: 0 auto;
            transition: max-width 0.3s ease;
        }
        .content.fullscreen {
            max-width: 100%;
            padding: 40px 80px;
        }
        .content-wrapper {
            flex: 1;
            overflow-y: auto;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            background: #f0f0f0;
        }
        
        /* Toolbar */
        .toolbar {
            position: fixed;
            top: 15px;
            right: 20px;
            z-index: 200;
            display: flex;
            gap: 10px;
        }
        .toolbar button {
            background: #0066cc;
            color: #fff;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            box-shadow: 0 2px 10px rgba(0,0,0,0.2);
            transition: background 0.2s, transform 0.1s;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .toolbar button:hover { background: #0055aa; transform: translateY(-1px); }
        .toolbar button:active { transform: translateY(0); }
        .toolbar button.secondary { background: #555; }
        .toolbar button.secondary:hover { background: #444; }
        
        /* Typography */
        .content h1 { 
            color: #1a1a1a; 
            margin: 30px 0 20px; 
            font-size: 2em;
            border-bottom: 3px solid #0066cc; 
            padding-bottom: 15px; 
        }
        .content h2 { color: #333; margin: 25px 0 15px; font-size: 1.5em; }
        .content h3 { color: #444; margin: 20px 0 10px; font-size: 1.25em; }
        .content p { line-height: 1.8; margin: 15px 0; color: #444; font-size: 16px; }
        .content ul, .content ol { margin: 15px 0 15px 30px; }
        .content li { margin: 10px 0; line-height: 1.7; }
        
        /* Code */
        .content code { 
            background: #f4f4f4; 
            padding: 3px 8px; 
            border-radius: 4px; 
            font-family: 'SF Mono', Monaco, 'Courier New', monospace;
            font-size: 0.9em;
            color: #e83e8c;
        }
        .content pre { 
            background: #1e1e1e; 
            color: #ddd; 
            padding: 20px; 
            border-radius: 10px; 
            overflow-x: auto; 
            margin: 20px 0;
            line-height: 1.5;
        }
        .content pre code { background: none; padding: 0; color: #ddd; }
        
        /* Other elements */
        .content img { max-width: 100%; height: auto; border-radius: 10px; margin: 20px 0; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .content table { border-collapse: collapse; width: 100%; margin: 20px 0; }
        .content th, .content td { border: 1px solid #ddd; padding: 12px 15px; text-align: left; }
        .content th { background: #f8f8f8; font-weight: 600; }
        .content tr:hover { background: #f9f9f9; }
        .content blockquote { 
            border-left: 4px solid #0066cc; 
            padding: 15px 20px; 
            margin: 20px 0; 
            background: #f9f9f9;
            border-radius: 0 8px 8px 0;
            color: #555;
        }
        .content a { color: #0066cc; text-decoration: none; }
        .content a:hover { text-decoration: underline; }
        .content strong { color: #1a1a1a; }
        .content hr { border: none; border-top: 2px solid #eee; margin: 30px 0; }
        
        /* Nested folders */
        .nested { margin-left: 15px; border-left: 1px solid #333; padding-left: 10px; }
        
        /* Welcome page */
        .welcome { text-align: center; padding: 60px 40px; }
        .welcome h1 { border: none; font-size: 2.5em; margin-bottom: 20px; }
        .welcome p { font-size: 18px; color: #666; max-width: 500px; margin: 0 auto; }
        
        /* Fullscreen mode adjustments */
        .fullscreen-mode .sidebar { display: none; }
        .fullscreen-mode .toggle-btn { left: 15px; }
        .fullscreen-mode .content { max-width: 100%; }
        
        /* Mobile responsive */
        @media (max-width: 768px) {
            body { flex-direction: column; }
            .sidebar { width: 100%; max-height: 35vh; position: fixed; top: 0; left: 0; right: 0; }
            .sidebar.hidden { transform: translateY(-100%); max-height: 0; }
            .toggle-btn { top: auto; bottom: 20px; left: 50%; transform: translateX(-50%); }
            .toggle-btn.sidebar-open { left: 50%; transform: translateX(-50%); }
            .content { padding: 25px; margin-top: 0; }
            .content h1 { font-size: 1.6em; }
            .toolbar { top: auto; bottom: 70px; right: 15px; flex-direction: column; }
        }
        
        /* Print styles for PDF */
        @media print {
            .sidebar, .toggle-btn, .toolbar { display: none !important; }
            .content { max-width: 100% !important; padding: 20px !important; }
        }
    </style>
</head>
<body>
    <button class="toggle-btn sidebar-open" onclick="toggleSidebar()" title="Toggle Sidebar">☰</button>
    
    <div class="toolbar">
        {% if is_markdown %}
        <button onclick="downloadPDF()" title="Download as PDF">📥 PDF</button>
        {% endif %}
        <button class="secondary" onclick="toggleFullscreen()" title="Toggle Fullscreen">⛶ Fullscreen</button>
    </div>
    
    <div class="sidebar">
        <h2>📚 {{ vault_name }}</h2>
        {{ tree|safe }}
    </div>
    <div class="content-wrapper">
        <div class="content">
            {{ content|safe }}
        </div>
    </div>
    
    <script>
        let sidebarOpen = true;
        
        function toggleSidebar() {
            const sidebar = document.querySelector('.sidebar');
            const toggleBtn = document.querySelector('.toggle-btn');
            const content = document.querySelector('.content');
            
            sidebarOpen = !sidebarOpen;
            
            if (sidebarOpen) {
                sidebar.classList.remove('hidden');
                toggleBtn.classList.add('sidebar-open');
                content.classList.remove('fullscreen');
            } else {
                sidebar.classList.add('hidden');
                toggleBtn.classList.remove('sidebar-open');
                content.classList.add('fullscreen');
            }
        }
        
        function toggleFullscreen() {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen();
            } else {
                document.exitFullscreen();
            }
        }
        
        function downloadPDF() {
            const currentPath = window.location.pathname;
            if (currentPath.startsWith('/view/')) {
                const filepath = currentPath.replace('/view/', '');
                window.location.href = '/pdf/' + filepath;
            }
        }
        
        // Keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            // Ctrl+B or Cmd+B to toggle sidebar
            if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
                e.preventDefault();
                toggleSidebar();
            }
            // F11 or Ctrl+Shift+F for fullscreen
            if (e.key === 'F11' || ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'F')) {
                e.preventDefault();
                toggleFullscreen();
            }
            // Escape to show sidebar
            if (e.key === 'Escape' && !sidebarOpen) {
                toggleSidebar();
            }
        });
    </script>
</body>
</html>
'''

def get_file_tree(path, base_path, current_file=""):
    """Recursively build HTML file tree"""
    items = []
    try:
        entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        
        for entry in entries:
            if entry.startswith('.'):
                continue
                
            full_path = os.path.join(path, entry)
            rel_path = os.path.relpath(full_path, base_path)
            
            if os.path.isdir(full_path):
                subtree = get_file_tree(full_path, base_path, current_file)
                if subtree:  # Only show folders that have content
                    items.append(f'<li><span class="folder">{entry}</span><ul class="nested">{subtree}</ul></li>')
            else:
                ext = entry.lower().rsplit('.', 1)[-1] if '.' in entry else ''
                if ext in ['md', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'txt']:
                    css_class = 'file-md' if ext == 'md' else 'file-pdf' if ext == 'pdf' else 'file-img' if ext in ['png','jpg','jpeg','gif','webp'] else ''
                    active = 'active' if rel_path == current_file else ''
                    items.append(f'<li><a href="/view/{rel_path}" class="{css_class} {active}">{entry}</a></li>')
    except PermissionError:
        pass
    except Exception as e:
        items.append(f'<li style="color:#ff6b6b">Error: {e}</li>')
    
    return ''.join(items)

def get_vault_name():
    """Get the vault folder name"""
    return os.path.basename(VAULT_PATH.rstrip('/'))

@app.route('/')
def index():
    """Home page"""
    tree = f'<ul>{get_file_tree(VAULT_PATH, VAULT_PATH)}</ul>'
    content = f'''
    <div class="welcome">
        <h1>Welcome to {get_vault_name()}</h1>
        <p>Select a file from the sidebar to view it. Markdown files will be beautifully rendered, and PDFs/images will display directly.</p>
        <p style="margin-top: 20px; font-size: 14px; color: #888;">
            <strong>Keyboard shortcuts:</strong><br>
            Ctrl+B — Toggle sidebar<br>
            F11 — Fullscreen<br>
            Esc — Show sidebar
        </p>
    </div>
    '''
    return render_template_string(HTML_TEMPLATE, title="Home", tree=tree, content=content, vault_name=get_vault_name(), is_markdown=False)

@app.route('/view/<path:filepath>')
def view_file(filepath):
    """View a specific file"""
    full_path = os.path.join(VAULT_PATH, filepath)
    
    # Security check - prevent directory traversal
    if not os.path.abspath(full_path).startswith(os.path.abspath(VAULT_PATH)):
        abort(403)
    
    if not os.path.exists(full_path):
        abort(404)
    
    ext = filepath.lower().rsplit('.', 1)[-1] if '.' in filepath else ''
    tree = f'<ul>{get_file_tree(VAULT_PATH, VAULT_PATH, filepath)}</ul>'
    filename = os.path.basename(filepath)
    
    if ext == 'md':
        with open(full_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        
        # Remove YAML frontmatter if present
        if md_content.startswith('---'):
            parts = md_content.split('---', 2)
            if len(parts) >= 3:
                md_content = parts[2]
        
        # Convert markdown to HTML
        html_content = markdown.markdown(
            md_content, 
            extensions=['tables', 'fenced_code', 'toc', 'nl2br', 'sane_lists']
        )
        
        # Add title
        title_html = f'<h1>{filename.replace(".md", "")}</h1>'
        
        return render_template_string(
            HTML_TEMPLATE, 
            title=filename, 
            tree=tree, 
            content=title_html + html_content,
            vault_name=get_vault_name(),
            is_markdown=True
        )
    
    elif ext == 'pdf':
        return send_file(full_path, mimetype='application/pdf')
    
    elif ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
        return send_file(full_path)
    
    else:
        # Plain text files
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f'<h1>{filename}</h1><pre>{f.read()}</pre>'
        except:
            content = '<p>Unable to read this file.</p>'
        
        return render_template_string(
            HTML_TEMPLATE, 
            title=filename, 
            tree=tree, 
            content=content,
            vault_name=get_vault_name(),
            is_markdown=False
        )

@app.route('/pdf/<path:filepath>')
def download_pdf(filepath):
    """Convert markdown file to PDF and download"""
    full_path = os.path.join(VAULT_PATH, filepath)
    
    # Security check
    if not os.path.abspath(full_path).startswith(os.path.abspath(VAULT_PATH)):
        abort(403)
    
    if not os.path.exists(full_path):
        abort(404)
    
    ext = filepath.lower().rsplit('.', 1)[-1] if '.' in filepath else ''
    if ext != 'md':
        abort(400, "Only markdown files can be converted to PDF")
    
    filename = os.path.basename(filepath).replace('.md', '.pdf')
    
    with open(full_path, 'r', encoding='utf-8') as f:
        md_content = f.read()
    
    # Remove YAML frontmatter
    if md_content.startswith('---'):
        parts = md_content.split('---', 2)
        if len(parts) >= 3:
            md_content = parts[2]
    
    # Convert to HTML
    html_content = markdown.markdown(
        md_content,
        extensions=['tables', 'fenced_code', 'toc', 'nl2br', 'sane_lists']
    )
    
    # Full HTML document for PDF
    title = os.path.basename(filepath).replace('.md', '')
    full_html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{title}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; padding: 40px; max-width: 800px; margin: 0 auto; }}
            h1 {{ color: #1a1a1a; border-bottom: 2px solid #0066cc; padding-bottom: 10px; }}
            h2 {{ color: #333; margin-top: 30px; }}
            h3 {{ color: #444; }}
            p {{ margin: 15px 0; }}
            code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-family: monospace; }}
            pre {{ background: #1e1e1e; color: #ddd; padding: 15px; border-radius: 8px; overflow-x: auto; }}
            pre code {{ background: none; padding: 0; color: inherit; }}
            table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
            th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
            th {{ background: #f8f8f8; }}
            blockquote {{ border-left: 4px solid #0066cc; padding: 10px 20px; margin: 20px 0; background: #f9f9f9; }}
            ul, ol {{ margin: 15px 0 15px 25px; }}
            li {{ margin: 8px 0; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        {html_content}
    </body>
    </html>
    '''
    
    # Try to convert using wkhtmltopdf or weasyprint
    try:
        # Try wkhtmltopdf first
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as html_file:
            html_file.write(full_html.encode('utf-8'))
            html_path = html_file.name
        
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as pdf_file:
            pdf_path = pdf_file.name
        
        # Try wkhtmltopdf
        result = subprocess.run(
            ['wkhtmltopdf', '--quiet', '--enable-local-file-access', html_path, pdf_path],
            capture_output=True,
            timeout=30
        )
        
        if result.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            response = send_file(
                pdf_path,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
            # Clean up temp files after sending
            os.unlink(html_path)
            return response
        
        # Cleanup failed attempt
        os.unlink(html_path)
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
            
    except FileNotFoundError:
        pass  # wkhtmltopdf not installed
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        print(f"PDF conversion error: {e}")
    
    # Fallback: Try weasyprint
    try:
        from weasyprint import HTML
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as pdf_file:
            HTML(string=full_html).write_pdf(pdf_file.name)
            return send_file(
                pdf_file.name,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
    except ImportError:
        pass
    except Exception as e:
        print(f"WeasyPrint error: {e}")
    
    # If all else fails, return the HTML as a downloadable file
    return Response(
        full_html,
        mimetype='text/html',
        headers={'Content-Disposition': f'attachment; filename="{title}.html"'}
    )

@app.route('/raw/<path:filepath>')
def raw_file(filepath):
    """Serve raw file (for embedding images in markdown, etc.)"""
    full_path = os.path.join(VAULT_PATH, filepath)
    
    if not os.path.abspath(full_path).startswith(os.path.abspath(VAULT_PATH)):
        abort(403)
    
    if os.path.exists(full_path):
        return send_file(full_path)
    abort(404)

def get_local_ip():
    """Get the local network IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return '127.0.0.1'

def main():
    global VAULT_PATH
    
    parser = argparse.ArgumentParser(
        description='View your Obsidian vault from any device on your local network',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python app.py ~/Documents/MyVault
  python app.py /path/to/vault --port 8080
  python app.py ~/notes --host 127.0.0.1  # localhost only
        '''
    )
    parser.add_argument('vault_path', nargs='?', help='Path to your Obsidian vault or any markdown folder')
    parser.add_argument('--port', '-p', type=int, default=5000, help='Port to run on (default: 5000)')
    parser.add_argument('--host', '-H', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0 for network access)')
    
    args = parser.parse_args()
    
    # Get vault path from argument or environment variable
    VAULT_PATH = args.vault_path or os.environ.get('OBSIDIAN_VAULT_PATH')
    
    if not VAULT_PATH:
        print("❌ Error: Please provide a vault path")
        print("   Usage: python app.py /path/to/your/vault")
        print("   Or set OBSIDIAN_VAULT_PATH environment variable")
        sys.exit(1)
    
    VAULT_PATH = os.path.expanduser(VAULT_PATH)
    
    if not os.path.isdir(VAULT_PATH):
        print(f"❌ Error: '{VAULT_PATH}' is not a valid directory")
        sys.exit(1)
    
    ip = get_local_ip()
    
    print(f"""
╔══════════════════════════════════════════════════════╗
║           📚 Obsidian Local Viewer                   ║
╠══════════════════════════════════════════════════════╣
║  Vault: {get_vault_name():<43} ║
║  Local:   http://localhost:{args.port:<24} ║
║  Network: http://{ip}:{args.port:<27} ║
╠══════════════════════════════════════════════════════╣
║  Keyboard Shortcuts:                                 ║
║    Ctrl+B  — Toggle sidebar                          ║
║    F11     — Fullscreen                              ║
║    Esc     — Show sidebar                            ║
╚══════════════════════════════════════════════════════╝
    
Open the Network URL on your tablet/phone to view your vault!
Press Ctrl+C to stop the server.
""")
    
    app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__ == '__main__':
    main()
