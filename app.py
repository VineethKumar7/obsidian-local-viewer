#!/usr/bin/env python3
"""
Obsidian Local Viewer - View your Obsidian vault from any device on your network
"""

from flask import Flask, render_template_string, send_file, abort, redirect, url_for, Response, request, jsonify
from datetime import datetime, timedelta
import os
import sys
import re
import json
import argparse
import socket
import markdown
import tempfile
import subprocess

app = Flask(__name__)

# Will be set via command line or environment variable
VAULT_PATH = None

# ===== IP ACCESS LOGGING =====
ACCESS_LOG_FILE = os.path.expanduser('~/clawd/obsidian-viewer/access_log.json')
_access_log = {'ips': {}, 'recent': []}

def load_access_log():
    """Load access log from file"""
    global _access_log
    try:
        if os.path.exists(ACCESS_LOG_FILE):
            with open(ACCESS_LOG_FILE, 'r') as f:
                _access_log = json.load(f)
    except:
        _access_log = {'ips': {}, 'recent': []}

def save_access_log():
    """Save access log to file"""
    try:
        with open(ACCESS_LOG_FILE, 'w') as f:
            json.dump(_access_log, f, indent=2)
    except:
        pass

@app.before_request
def log_request_ip():
    """Log IP address for each request"""
    # Skip static files and API calls for cleaner logs
    if request.path.startswith('/static') or request.path == '/favicon.ico':
        return
    
    ip = request.remote_addr
    # Handle proxy headers
    if request.headers.get('X-Forwarded-For'):
        ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Update IP stats
    if ip not in _access_log['ips']:
        _access_log['ips'][ip] = {'first_seen': timestamp, 'last_seen': timestamp, 'count': 0, 'paths': []}
    
    _access_log['ips'][ip]['last_seen'] = timestamp
    _access_log['ips'][ip]['count'] += 1
    
    # Track recent unique paths per IP (last 10)
    if request.path not in _access_log['ips'][ip]['paths']:
        _access_log['ips'][ip]['paths'].append(request.path)
        if len(_access_log['ips'][ip]['paths']) > 10:
            _access_log['ips'][ip]['paths'] = _access_log['ips'][ip]['paths'][-10:]
    
    # Add to recent log (keep last 100)
    _access_log['recent'].append({'ip': ip, 'path': request.path, 'time': timestamp})
    if len(_access_log['recent']) > 100:
        _access_log['recent'] = _access_log['recent'][-100:]
    
    # Save periodically (every 10 requests)
    if sum(d['count'] for d in _access_log['ips'].values()) % 10 == 0:
        save_access_log()

# Cache for file lookups (populated on first request)
_file_cache = {}

def build_file_cache():
    """Build a cache of all files in the vault for quick lookups"""
    global _file_cache
    _file_cache = {}
    
    for root, dirs, files in os.walk(VAULT_PATH):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for file in files:
            if file.startswith('.'):
                continue
            
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, VAULT_PATH)
            
            # Store by filename (without extension for .md files)
            name_without_ext = file.rsplit('.', 1)[0] if '.' in file else file
            
            # Store multiple mappings for flexible lookup
            _file_cache[file.lower()] = rel_path
            _file_cache[name_without_ext.lower()] = rel_path
            
            # Also store with path for disambiguation
            _file_cache[rel_path.lower()] = rel_path

def find_file_in_vault(link_text):
    """Find a file in the vault matching the Obsidian link text"""
    if not _file_cache:
        build_file_cache()
    
    # Clean up the link text
    link_text = link_text.strip()
    
    # Try exact match first (case-insensitive)
    lookup = link_text.lower()
    if lookup in _file_cache:
        return _file_cache[lookup]
    
    # Try with .md extension
    if lookup + '.md' in _file_cache:
        return _file_cache[lookup + '.md']
    
    # Try with .pdf extension
    if lookup + '.pdf' in _file_cache:
        return _file_cache[lookup + '.pdf']
    
    return None

def convert_obsidian_links(html_content, current_file_dir=""):
    """Convert Obsidian [[wiki-links]] and ![[embeds]] to HTML"""
    
    # First handle image/file embeds: ![[filename]]
    def replace_embed(match):
        inner = match.group(1)
        
        # Handle display text: ![[image.png|alt text]]
        if '|' in inner:
            link_part, alt_text = inner.split('|', 1)
        else:
            link_part = inner
            alt_text = inner
        
        # Find the file
        file_path = find_file_in_vault(link_part)
        
        # Try relative path if not found
        if not file_path and current_file_dir:
            relative_path = os.path.join(current_file_dir, link_part)
            relative_path = os.path.normpath(relative_path)
            file_path = find_file_in_vault(relative_path)
        
        if file_path:
            ext = link_part.lower().rsplit('.', 1)[-1] if '.' in link_part else ''
            
            # Image embeds
            if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp']:
                return f'<img src="/raw/{file_path}" alt="{alt_text}" style="max-width: 100%; cursor: zoom-in;" onclick="openLightbox(this.src)">'
            
            # PDF embeds
            elif ext == 'pdf':
                return f'<a href="/view/{file_path}" class="internal-link">📄 {alt_text}</a>'
            
            # Other files
            else:
                return f'<a href="/view/{file_path}" class="internal-link">{alt_text}</a>'
        else:
            return f'<span class="broken-link" title="File not found: {link_part}">![[{inner}]]</span>'
    
    # Handle ![[...]] embed patterns
    html_content = re.sub(r'!\[\[([^\]]+)\]\]', replace_embed, html_content)
    
    def replace_link(match):
        full_match = match.group(0)
        inner = match.group(1)
        
        # Handle [[link|display text]] format
        if '|' in inner:
            link_part, display_text = inner.split('|', 1)
        else:
            link_part = inner
            display_text = inner
        
        # Handle heading links like [[file#heading]]
        heading = ""
        if '#' in link_part:
            link_part, heading = link_part.split('#', 1)
            heading = '#' + heading.lower().replace(' ', '-')
        
        # Find the file - try multiple strategies
        file_path = None
        
        # Strategy 1: Direct lookup (works for absolute paths from vault root or filenames)
        file_path = find_file_in_vault(link_part)
        
        # Strategy 2: Try relative path from current file's directory
        if not file_path and current_file_dir:
            relative_path = os.path.join(current_file_dir, link_part)
            # Normalize the path (resolve .. and .)
            relative_path = os.path.normpath(relative_path)
            file_path = find_file_in_vault(relative_path)
            
            # Also try with .md extension
            if not file_path:
                file_path = find_file_in_vault(relative_path + '.md')
        
        if file_path:
            return f'<a href="/view/{file_path}{heading}" class="internal-link">{display_text}</a>'
        else:
            # Return as broken link (styled differently)
            return f'<span class="broken-link" title="File not found: {link_part}">{display_text}</span>'
    
    # Match [[...]] patterns (but not inside code blocks)
    # This regex handles [[link]] and [[link|text]]
    pattern = r'\[\[([^\]]+)\]\]'
    
    return re.sub(pattern, replace_link, html_content)

def convert_obsidian_callouts(content):
    """Convert Obsidian callouts to HTML before markdown processing.
    
    Supports:
    - > [!note] Title
    - > [!warning]+ Expanded by default
    - > [!tip]- Collapsed by default
    
    Note: Callout content is processed through markdown to support tables, 
    code blocks, and other formatting inside callouts.
    """
    lines = content.split('\n')
    result = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Check for callout start: > [!type] or > [!type]+ or > [!type]-
        callout_match = re.match(r'^>\s*\[!(\w+)\]([+-])?\s*(.*)?$', line)
        
        if callout_match:
            callout_type = callout_match.group(1).lower()
            collapse_char = callout_match.group(2)  # + or - or None
            title = callout_match.group(3) or callout_type.capitalize()
            
            # HTML-escape special characters in title first (before markdown processing)
            # This prevents & from being interpreted as HTML entity start
            title = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            
            # Process basic markdown in title (bold, italic, inline code)
            title = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', title)
            title = re.sub(r'\*(.+?)\*', r'<em>\1</em>', title)
            title = re.sub(r'`([^`]+)`', r'<code>\1</code>', title)
            
            # Collect callout content (subsequent lines starting with >)
            content_lines = []
            i += 1
            while i < len(lines) and lines[i].startswith('>'):
                # Remove the > prefix and one optional space
                content_line = re.sub(r'^>\s?', '', lines[i])
                content_lines.append(content_line)
                i += 1
            
            # Process callout content through markdown BEFORE wrapping in HTML
            # This ensures tables, code blocks, etc. inside callouts are rendered
            callout_md = '\n'.join(content_lines)
            
            # Fix lists that follow paragraphs without blank lines
            callout_md = fix_lists_after_paragraphs(callout_md)
            
            # Convert task lists (- [ ] and - [x]) to checkboxes
            callout_md = convert_task_lists(callout_md)
            
            # Preserve line breaks for equation continuations (lines starting with =)
            callout_md = fix_equation_line_breaks(callout_md)
            
            # Convert common math notations like e^(...) to LaTeX
            callout_md = convert_inline_math_notation(callout_md)
            
            # Protect math expressions inside callouts before markdown processing
            callout_md, callout_math_placeholders = protect_math_expressions(callout_md)
            
            callout_html = markdown.markdown(
                callout_md,
                extensions=['tables', 'fenced_code', 'sane_lists']
            )
            
            # Restore math expressions after markdown processing
            callout_html = restore_math_expressions(callout_html, callout_math_placeholders)
            
            if collapse_char:
                # Collapsible callout
                open_attr = ' open' if collapse_char == '+' else ''
                result.append(f'<details class="callout callout-{callout_type}"{open_attr}>')
                result.append(f'<summary><div class="callout-title">{title}</div></summary>')
                result.append(f'<div class="callout-content">{callout_html}</div>')
                result.append('</details>')
            else:
                # Non-collapsible callout
                result.append(f'<div class="callout callout-{callout_type}">')
                result.append(f'<div class="callout-title">{title}</div>')
                result.append(f'<div class="callout-content">{callout_html}</div>')
                result.append('</div>')
            
            result.append('')  # Add blank line after callout
        else:
            # Convert horizontal rules and headings to HTML directly
            # (markdown parser struggles with these after HTML blocks)
            if re.match(r'^-{3,}$', line.strip()):
                result.append('<hr class="section-divider">')
            elif re.match(r'^#{1,6}\s+', line):
                heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
                if heading_match:
                    level = len(heading_match.group(1))
                    text = process_inline_markdown(heading_match.group(2))
                    result.append(f'<h{level}>{text}</h{level}>')
                else:
                    result.append(line)
            # Handle tables (markdown parser struggles with tables after HTML)
            elif line.strip().startswith('|') and '|' in line[1:]:
                # Collect all table lines
                table_lines = [line]
                i += 1
                while i < len(lines) and lines[i].strip().startswith('|'):
                    table_lines.append(lines[i])
                    i += 1
                # Convert table to HTML
                table_html = convert_markdown_table(table_lines)
                result.append(table_html)
                continue  # Skip the i += 1 at end since we already incremented
            else:
                # Process inline markdown (bold, italic, code) on regular lines
                result.append(process_inline_markdown(line))
            i += 1
    
    return '\n'.join(result)


def process_inline_markdown(text):
    """Process inline markdown: bold, italic, code, links."""
    # Code (must be before bold/italic to avoid conflicts)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    return text


def convert_markdown_table(lines):
    """Convert markdown table lines to HTML table."""
    if len(lines) < 2:
        return '\n'.join(lines)
    
    html = ['<table>']
    
    # First line is header
    header_cells = [cell.strip() for cell in lines[0].split('|')[1:-1]]
    html.append('<thead><tr>')
    for cell in header_cells:
        html.append(f'<th>{process_inline_markdown(cell)}</th>')
    html.append('</tr></thead>')
    
    # Skip separator line (line with |---|---|)
    # Process data rows
    html.append('<tbody>')
    for line in lines[2:]:  # Skip header and separator
        if line.strip():
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            html.append('<tr>')
            for cell in cells:
                html.append(f'<td>{process_inline_markdown(cell)}</td>')
            html.append('</tr>')
    html.append('</tbody>')
    html.append('</table>')
    
    return '\n'.join(html)

def fix_list_continuation(content):
    """
    Fix markdown list parsing issues where numbered items following 
    nested bullets aren't recognized as continuing the parent list.
    
    Pattern: indented bullet (    - ...) followed by numbered item (N. ...)
    without a blank line causes the numbered item to be absorbed into the bullet.
    
    Solution: Insert blank line before numbered items that follow indented content.
    """
    import re
    # Match: line ending with content (indented list item), followed by numbered list item at column 0
    # Pattern: (indented line with content)\n(numbered item without preceding blank line)
    pattern = r'(^[ \t]+[-*+].*\S.*$)\n(^\d+\.\s)'
    replacement = r'\1\n\n\2'
    return re.sub(pattern, replacement, content, flags=re.MULTILINE)

def fix_lists_after_paragraphs(content):
    """
    Fix markdown list parsing by adding blank lines before lists that follow paragraphs.
    
    Markdown requires a blank line before lists for proper parsing.
    This adds a blank line when a list item (- or * or + or 1.) directly follows
    a line of text that doesn't end with a colon followed by newline.
    """
    import re
    # Pattern: non-empty line (not a list item) followed directly by a list item
    # (?![-*+\d]) ensures the previous line isn't already a list item
    # This handles: "some text:\n- item" -> "some text:\n\n- item"
    pattern = r'(^[^\n]*[^\s])\n([-*+] |\d+\. )'
    replacement = r'\1\n\n\2'
    return re.sub(pattern, replacement, content, flags=re.MULTILINE)

def fix_equation_line_breaks(content):
    """
    Preserve line breaks for equation-like content.
    
    Lines starting with = or mathematical operators that follow other content
    should have <br> tags to preserve the multi-line formatting.
    This handles step-by-step equation solutions.
    """
    import re
    # Add two trailing spaces (markdown line break) before lines starting with =
    # Pattern: end of line followed by line starting with = (equation continuation)
    pattern = r'(\S)\n(= )'
    replacement = r'\1  \n\2'
    return re.sub(pattern, replacement, content, flags=re.MULTILINE)

def convert_task_lists(content):
    """
    Convert GitHub-style task lists to HTML checkboxes.
    
    Patterns:
    - [ ] unchecked item -> checkbox unchecked
    - [x] checked item -> checkbox checked
    """
    # Convert unchecked: - [ ] text
    # Add no-mathjax class to prevent MathJax from processing task text
    content = re.sub(
        r'^(\s*)- \[ \] (.+)$',
        r'\1<label class="task-item no-mathjax"><input type="checkbox" disabled> <span class="no-mathjax">\2</span></label>',
        content,
        flags=re.MULTILINE
    )
    # Convert checked: - [x] text or - [X] text
    content = re.sub(
        r'^(\s*)- \[[xX]\] (.+)$',
        r'\1<label class="task-item task-done no-mathjax"><input type="checkbox" checked disabled> <span class="no-mathjax">\2</span></label>',
        content,
        flags=re.MULTILINE
    )
    return content


def convert_inline_math_notation(content):
    """
    Convert common mathematical notations to LaTeX for MathJax rendering.
    
    Patterns converted:
    - e^(...) -> $e^{...}$  (exponential)
    - e^-... -> $e^{-...}$  (negative exponential without parens)
    - x ∈ [a, b] -> $x \in [a, b]$  (element of interval)
    - [0, 1] -> $[0, 1]$  (interval notation)
    """
    import re
    # Convert e^(...) to $e^{...}$
    # Handles e^(-0.542), e^(x), e^(-S_KL), etc.
    content = re.sub(r'\be\^[\(]([^\)]+)[\)]', r'$e^{\1}$', content)
    
    # Convert e^-number (without parens) to $e^{-number}$
    # Handles e^-0.542, e^-x, etc.
    content = re.sub(r'\be\^(-?[\d\.]+)\b', r'$e^{\1}$', content)
    
    # Convert standalone interval notation [a, b] to math mode FIRST
    # Handles: [0, 1], [a, b], [-1, 1], [0.0, 1.0], etc.
    # Only match if it looks like an interval (has comma, numbers/variables)
    # Negative lookbehind/lookahead to avoid already-in-math, links, or double-conversion
    # (?!\$) at end prevents matching [0,1]$ which would be inside existing $...$
    content = re.sub(
        r'(?<!\$)\[(-?[\d\w\.]+),\s*(-?[\d\w\.]+)\](?!\()(?!\$)',
        r'$[\1, \2]$',
        content
    )
    
    # Convert "x ∈ $[a, b]$" to "$x \in [a, b]$" (merge with already-converted interval)
    # Handles: r ∈ [0, 1], x ∈ [a, b], etc.
    content = re.sub(
        r'(\w+)\s*∈\s*\$\[([^\]]+)\]\$',
        r'$\1 \\in [\2]$',
        content
    )
    
    # Also handle case where interval wasn't converted (fallback)
    content = re.sub(
        r'(\w+)\s*∈\s*\[([^\]]+)\](?!\$)',
        r'$\1 \\in [\2]$',
        content
    )
    
    return content

def protect_array_notation(content):
    """
    Protect array/matrix notation like A[i][j] from being parsed as markdown links.
    
    The pattern [i] and [j] get interpreted as link references in markdown,
    causing text like "A[i][j] = 1" to render incorrectly.
    
    This function wraps such patterns in backticks to render them as inline code.
    """
    # Pattern matches: word/letter followed by [single_char_or_word][...]
    # Examples: A[i][j], matrix[row][col], arr[0], list[index]
    # But NOT: [link text](url) or [[wiki links]]
    
    # Pattern 1: Variable followed by multiple bracket pairs like A[i][j]
    # Matches: A[i][j], A[i][j][k], matrix[0][1], etc.
    # Negative lookbehind for backtick to avoid double-wrapping
    multi_bracket_pattern = re.compile(r'(?<!`)(?<!\[)\b([A-Za-z_][A-Za-z0-9_]*)((?:\[[^\]]+\]){2,})(?!`)')
    content = re.sub(multi_bracket_pattern, r'`\1\2`', content)
    
    # Pattern 2: Single bracket with single letter/short index like A[i], arr[0]
    # Only if it looks like array notation (not a markdown link)
    # Matches: A[i], A[j], arr[0], list[n], but NOT [link text]
    single_bracket_pattern = re.compile(r'(?<!`)(?<!\])\b([A-Za-z_][A-Za-z0-9_]*)\[([a-zA-Z0-9_]{1,3})\](?![`\(\]])')
    content = re.sub(single_bracket_pattern, r'`\1[\2]`', content)
    
    return content


def protect_css_selectors(content):
    """
    Protect CSS selector patterns like (#name) and (.class) from MathJax processing.
    Also protects trailing # like "Apartment#" which causes LaTeX errors.
    
    The # symbol is a LaTeX macro parameter character and causes MathJax errors
    when it appears in text like "ID (#name)" or "#id" or "word#".
    
    Wraps such patterns in backticks or escapes them.
    """
    # Pattern 1: CSS ID selector in parentheses like (#name) or (#id)
    # This prevents MathJax from trying to process # as LaTeX
    content = re.sub(r'(?<!`)\(#([a-zA-Z_][a-zA-Z0-9_-]*)\)(?!`)', r'(`#\1`)', content)
    
    # Pattern 2: CSS class selector in parentheses like (.class) or (.name)
    content = re.sub(r'(?<!`)\(\.([a-zA-Z_][a-zA-Z0-9_-]*)\)(?!`)', r'(`.\1`)', content)
    
    # Pattern 3: Standalone #id or .class patterns (common in CSS discussions)
    # Match #word that's not in a heading context (not at start of line after optional >)
    content = re.sub(r'(?<!`)(?<!^)(?<!^> )(?<![#`])#([a-zA-Z_][a-zA-Z0-9_-]*)(?![`\w])', r'`#\1`', content)
    
    # Pattern 4: Word ending with # like "Apartment#" or "Room#"
    # Replace trailing # with HTML entity to avoid LaTeX macro parameter errors
    content = re.sub(r'(\w+)#(?=\W|$)', r'\1&#35;', content)
    
    # Pattern 5: Standalone # not in heading or code
    # Replace with HTML entity if not already protected
    content = re.sub(r'(?<!`)(?<!\d)#(?!\w)(?!`)', r'&#35;', content)
    
    return content


def protect_math_expressions(content):
    """Protect LaTeX math expressions from markdown processing"""
    # First, convert common math notations like e^(...) to LaTeX
    content = convert_inline_math_notation(content)
    
    # Protect array notation from being parsed as links
    content = protect_array_notation(content)
    
    # Protect CSS selectors (#id, .class) from MathJax errors
    content = protect_css_selectors(content)
    
    placeholders = {}
    counter = [0]  # Use list to allow modification in nested function
    
    def replace_math(match):
        placeholder = f"MATH_PLACEHOLDER_{counter[0]}_END"
        placeholders[placeholder] = match.group(0)
        counter[0] += 1
        return placeholder
    
    # Protect display math ($$...$$) first - multiline
    content = re.sub(r'\$\$[\s\S]*?\$\$', replace_math, content)
    
    # Protect inline math ($...$) - but not double dollars
    # More permissive regex to handle math inside tables and complex expressions
    # Match $ followed by content (can start/end with backslash for LaTeX), $
    # Allow expressions like $\tilde{P}_{D_i}(w)$ inside tables
    content = re.sub(r'(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)', replace_math, content)
    
    return content, placeholders

def restore_math_expressions(content, placeholders):
    """Restore protected math expressions after markdown processing.
    
    Wraps inline math in <span class="inline-math"> for explicit MathJax targeting.
    This helps MathJax reliably identify short expressions like $R$.
    """
    for placeholder, original in placeholders.items():
        if original.startswith('$$') and original.endswith('$$'):
            # Display math - keep as-is, MathJax handles these well
            content = content.replace(placeholder, original)
        elif original.startswith('$') and original.endswith('$'):
            # Inline math - keep original $...$ format for MathJax to process
            # MathJax natively handles $...$ delimiters
            content = content.replace(placeholder, original)
        else:
            content = content.replace(placeholder, original)
    return content


# ============================================
# FILE METADATA SYSTEM
# ============================================

def get_metadata_path():
    """Get the path to the metadata JSON file"""
    return os.path.join(VAULT_PATH, 'obsidian-viewer-meta.json')

def load_all_metadata():
    """Load all file metadata from JSON file"""
    meta_path = get_metadata_path()
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_all_metadata(metadata):
    """Save all metadata to JSON file"""
    meta_path = get_metadata_path()
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

def get_file_metadata(filepath):
    """Get metadata for a specific file"""
    all_meta = load_all_metadata()
    default_meta = {
        'completed': False,
        'created_date': '',
        'source': '',
        'revision_count': 0,
        'summary': '',
        'one_para_summary': ''
    }
    return all_meta.get(filepath, default_meta)

def set_file_metadata(filepath, metadata):
    """Set metadata for a specific file"""
    all_meta = load_all_metadata()
    all_meta[filepath] = metadata
    save_all_metadata(all_meta)


# ============================================
# SRS (SPACED REPETITION) STORAGE
# ============================================

def get_srs_filepath(filepath):
    """Get SRS data file path - stored alongside the markdown file"""
    # filepath is relative to VAULT_PATH, e.g. "Week 1/Ethernet.md"
    full_path = os.path.join(VAULT_PATH, filepath)
    # Store as Ethernet.md.srs.json in same directory
    return full_path + '.srs.json'

def scan_all_srs_files(folder_path=None):
    """Scan vault for all .srs.json files and return their data
    
    Args:
        folder_path: Optional relative path to filter by folder (e.g., "1 Week - Introduction")
    """
    srs_files = []
    search_path = VAULT_PATH
    if folder_path:
        search_path = os.path.join(VAULT_PATH, folder_path)
        if not os.path.exists(search_path):
            return []
    
    for root, dirs, files in os.walk(search_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith('.srs.json'):
                srs_path = os.path.join(root, file)
                try:
                    with open(srs_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        # Add relative path for context
                        data['_relativePath'] = os.path.relpath(srs_path, VAULT_PATH)
                        srs_files.append(data)
                except (json.JSONDecodeError, IOError):
                    pass
    return srs_files

def load_srs_data(filepath):
    """Load SRS data for a specific file"""
    srs_path = get_srs_filepath(filepath)
    if os.path.exists(srs_path):
        try:
            with open(srs_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        'filePath': filepath,
        'mode': 'srs',  # 'srs' or 'leitner'
        'cards': {},
        'lastReview': None
    }

def save_srs_data(filepath, data):
    """Save SRS data for a specific file"""
    srs_path = get_srs_filepath(filepath)
    data['filePath'] = filepath
    with open(srs_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_study_sessions_path():
    """Get path to study sessions log"""
    sessions_dir = os.path.join(VAULT_PATH, '.obsidian-viewer')
    os.makedirs(sessions_dir, exist_ok=True)
    return os.path.join(sessions_dir, 'sessions.json')

def load_study_sessions():
    """Load study session history"""
    path = get_study_sessions_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}

def save_study_sessions(data):
    """Save study session history"""
    with open(get_study_sessions_path(), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_study_settings_path():
    """Get path to study settings"""
    settings_dir = os.path.join(VAULT_PATH, '.obsidian-viewer')
    os.makedirs(settings_dir, exist_ok=True)
    return os.path.join(settings_dir, 'settings.json')

def load_study_settings():
    """Load study settings"""
    path = get_study_settings_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        'dailyGoal': 50,
        'timerSeconds': 30,
        'srsMode': 'srs',  # 'srs' or 'leitner'
        'timedMode': False,
        'confidenceRating': False,
        'notifications': False
    }

def save_study_settings(data):
    """Save study settings"""
    with open(get_study_settings_path(), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def calculate_srs_interval(card_data, rating):
    """
    Calculate next review interval using SM-2 algorithm variant.
    rating: 1=again, 2=hard, 3=good, 4=easy
    """
    interval = card_data.get('interval', 1)  # days
    ease_factor = card_data.get('easeFactor', 2.5)
    reps = card_data.get('reps', 0)
    lapses = card_data.get('lapses', 0)
    
    if rating == 1:  # Again - reset
        interval = 0.007  # ~10 minutes in days
        ease_factor = max(1.3, ease_factor - 0.2)
        lapses += 1
        reps = 0
    elif rating == 2:  # Hard
        interval = interval * 1.2
        ease_factor = max(1.3, ease_factor - 0.15)
        reps += 1
    elif rating == 3:  # Good
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 6
        else:
            interval = interval * ease_factor
        reps += 1
    elif rating == 4:  # Easy
        if reps == 0:
            interval = 4
        else:
            interval = interval * ease_factor * 1.3
        ease_factor = min(3.0, ease_factor + 0.15)
        reps += 1
    
    next_review = (datetime.utcnow() + timedelta(days=interval)).isoformat() + 'Z'
    
    return {
        'interval': round(interval, 3),
        'easeFactor': round(ease_factor, 2),
        'reps': reps,
        'lapses': lapses,
        'nextReview': next_review,
        'lastReview': datetime.utcnow().isoformat() + 'Z'
    }

def calculate_leitner_box(card_data, correct):
    """
    Calculate Leitner box movement.
    correct: True = move up, False = back to box 1
    """
    current_box = card_data.get('box', 1)
    
    # Box intervals in days
    box_intervals = {1: 1, 2: 2, 3: 4, 4: 7, 5: 14}
    
    if correct:
        new_box = min(5, current_box + 1)
    else:
        new_box = 1
    
    interval = box_intervals[new_box]
    next_review = (datetime.utcnow() + timedelta(days=interval)).isoformat() + 'Z'
    
    return {
        'box': new_box,
        'nextReview': next_review,
        'lastReview': datetime.utcnow().isoformat() + 'Z'
    }


# ============================================
# CLOZE DELETION PARSER
# ============================================

def parse_cloze(content):
    """
    Parse cloze deletions from markdown content.
    
    Supports formats:
    1. {{c1::text}} - Anki-style numbered cloze
    2. {{text}} - Unnumbered cloze
    3. {{text|hint}} - Cloze with hint
    4. ==text== - Highlight-based cloze (simpler)
    
    Returns list of cloze cards with their blanks.
    """
    cloze_cards = []
    
    # Look for ## Cloze section
    cloze_section = re.search(r'##\s*Cloze\s*\n([\s\S]*?)(?=\n##\s|\Z)', content, re.IGNORECASE)
    section_content = cloze_section.group(1) if cloze_section else ''
    
    # Also check for > [!cloze] callouts
    callout_pattern = re.compile(r'>\s*\[!cloze\][+-]?\s*(.*?)\n((?:>.*\n)*)', re.IGNORECASE)
    for match in callout_pattern.finditer(content):
        title = match.group(1).strip()
        body_lines = match.group(2).strip()
        body = '\n'.join(line.lstrip('>').strip() for line in body_lines.split('\n') if line.strip())
        full_text = (title + '\n' + body) if title else body
        if full_text:
            section_content += '\n' + full_text
    
    if not section_content.strip():
        return cloze_cards
    
    # Split into sentences/lines for individual cards
    lines = [l.strip() for l in section_content.split('\n') if l.strip() and not l.strip().startswith('#')]
    
    for line in lines:
        # Skip lines without any cloze markers
        if not (re.search(r'\{\{.*?\}\}', line) or re.search(r'==.+?==', line)):
            continue
        
        # Find all cloze deletions in this line
        cloze_groups = {}  # group by cloze number
        
        # Pattern 1: {{c1::text}} or {{c1::text|hint}}
        numbered_pattern = re.compile(r'\{\{c(\d+)::([^}|]+)(?:\|([^}]+))?\}\}')
        for match in numbered_pattern.finditer(line):
            num = int(match.group(1))
            text = match.group(2).strip()
            hint = match.group(3).strip() if match.group(3) else None
            if num not in cloze_groups:
                cloze_groups[num] = []
            cloze_groups[num].append({'text': text, 'hint': hint, 'full': match.group(0)})
        
        # Pattern 2: {{text}} or {{text|hint}} (unnumbered)
        unnumbered_pattern = re.compile(r'\{\{([^}:]+?)(?:\|([^}]+))?\}\}')
        unnumbered_idx = 100  # Start at 100 to not conflict with numbered
        for match in unnumbered_pattern.finditer(line):
            # Skip if this was already matched as numbered
            if '::' in match.group(0):
                continue
            text = match.group(1).strip()
            hint = match.group(2).strip() if match.group(2) else None
            cloze_groups[unnumbered_idx] = [{'text': text, 'hint': hint, 'full': match.group(0)}]
            unnumbered_idx += 1
        
        # Pattern 3: ==text== (highlight-based)
        highlight_pattern = re.compile(r'==([^=]+)==')
        highlight_idx = 200  # Start at 200
        for match in highlight_pattern.finditer(line):
            text = match.group(1).strip()
            cloze_groups[highlight_idx] = [{'text': text, 'hint': None, 'full': match.group(0)}]
            highlight_idx += 1
        
        # Create cards - one per cloze group
        for group_num, clozes in cloze_groups.items():
            # Build the question with this group blanked out
            question = line
            answers = []
            
            for cloze in clozes:
                hint_text = f'[{cloze["hint"]}]' if cloze['hint'] else '[...]'
                question = question.replace(cloze['full'], f'<span class="cloze-blank">{hint_text}</span>')
                answers.append(cloze['text'])
            
            # Clean up other cloze markers (show them revealed)
            question = re.sub(r'\{\{c?\d*::([^}|]+)(?:\|[^}]+)?\}\}', r'\1', question)
            question = re.sub(r'\{\{([^}|]+)(?:\|[^}]+)?\}\}', r'\1', question)
            question = re.sub(r'==([^=]+)==', r'\1', question)
            
            cloze_cards.append({
                'type': 'cloze',
                'question': question,
                'answer': ', '.join(answers),
                'answers': answers,
                'originalLine': line
            })
    
    return cloze_cards


def parse_summary(content):
    """
    Extract summary content from markdown.
    
    Looks for:
    1. > [!summary] callouts
    2. ## Summary / ## TL;DR sections
    3. **bold terms** as key terms
    4. > [!tip] and > [!important] callouts
    """
    summary = {
        'sections': [],
        'keyTerms': []
    }
    
    # Extract key terms (bold text)
    bold_pattern = re.compile(r'\*\*([^*]+)\*\*')
    key_terms = set()
    for match in bold_pattern.finditer(content):
        term = match.group(1).strip()
        if len(term) < 50 and not term.startswith('http'):  # Skip long text and URLs
            key_terms.add(term)
    summary['keyTerms'] = list(key_terms)[:15]  # Limit to 15 terms
    
    # Extract [!summary] callouts
    summary_callout = re.compile(r'>\s*\[!(summary|tldr|abstract)\][+-]?\s*(.*?)\n((?:>.*\n)*)', re.IGNORECASE)
    for match in summary_callout.finditer(content):
        title = match.group(2).strip() or 'Summary'
        body = match.group(3)
        points = []
        for line in body.split('\n'):
            line = line.lstrip('>').strip()
            if line.startswith('- ') or line.startswith('* '):
                points.append(line[2:].strip())
            elif line and not line.startswith('#'):
                points.append(line)
        if points:
            summary['sections'].append({'title': title, 'points': points[:10]})
    
    # Extract [!tip] and [!important] callouts
    tip_callout = re.compile(r'>\s*\[!(tip|important|warning|note)\][+-]?\s*(.*?)\n((?:>.*\n)*)', re.IGNORECASE)
    for match in tip_callout.finditer(content):
        callout_type = match.group(1).capitalize()
        title = match.group(2).strip() or callout_type
        body = match.group(3)
        points = []
        for line in body.split('\n'):
            line = line.lstrip('>').strip()
            if line:
                points.append(line)
        if points:
            summary['sections'].append({'title': f"💡 {title}", 'points': points[:5]})
    
    # Extract ## Summary or ## TL;DR sections
    summary_section = re.search(r'##\s*(Summary|TL;?DR|Key\s*Points?|Overview)\s*\n([\s\S]*?)(?=\n##\s|\Z)', content, re.IGNORECASE)
    if summary_section:
        title = summary_section.group(1).strip()
        body = summary_section.group(2)
        points = []
        for line in body.split('\n'):
            line = line.strip()
            if line.startswith('- ') or line.startswith('* '):
                points.append(line[2:].strip())
            elif line.startswith(('1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.')):
                points.append(line[2:].strip())
        if points:
            summary['sections'].insert(0, {'title': title, 'points': points[:10]})
    
    # If no sections found, extract headings as overview
    if not summary['sections']:
        headings = re.findall(r'^##\s+(.+)$', content, re.MULTILINE)
        if headings:
            summary['sections'].append({
                'title': 'Topics Covered',
                'points': headings[:10]
            })
    
    return summary if summary['sections'] or summary['keyTerms'] else None


def find_backlinks(filepath):
    """
    Find all MD files that link to the given file (backlinks).
    Searches for [[filename]] or [[path/to/filename]] patterns.
    """
    current_filename = os.path.basename(filepath)
    current_name = current_filename.replace('.md', '')
    backlinks = []
    
    # Patterns to search for
    # [[filename]] or [[filename|alias]] or [[path/filename]]
    search_patterns = [
        f'[[{current_name}]]',
        f'[[{current_name}|',
        f'[[{current_name}#',
        f'/{current_name}]]',
        f'/{current_name}|',
        f'/{current_name}#',
    ]
    
    # Walk through all MD files in vault
    for root, dirs, files in os.walk(VAULT_PATH):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for filename in files:
            if not filename.endswith('.md'):
                continue
            
            file_path = os.path.join(root, filename)
            rel_path = os.path.relpath(file_path, VAULT_PATH)
            
            # Don't include self
            if rel_path == filepath:
                continue
            
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                # Check if any pattern matches
                for pattern in search_patterns:
                    if pattern in content:
                        backlinks.append({
                            'name': filename.replace('.md', ''),
                            'path': rel_path
                        })
                        break  # Found a match, no need to check other patterns
            except Exception:
                pass
    
    return backlinks


def get_page_navigation(filepath):
    """
    Get navigation links for a page:
    - parents: MD files that link TO this page (backlinks)
    - siblings: Other MD files in the same folder
    - children: MD files in a 'subpages' subfolder or any immediate subfolder
    """
    full_path = os.path.join(VAULT_PATH, filepath)
    current_dir = os.path.dirname(full_path)
    current_filename = os.path.basename(filepath)
    current_name = current_filename.replace('.md', '')
    
    nav = {
        'parents': [],  # Changed from 'parent' to 'parents' (multiple backlinks)
        'siblings': [],
        'children': []
    }
    
    # Get parents (backlinks - files that reference this page)
    nav['parents'] = find_backlinks(filepath)
    
    # Get siblings (other MD files in the same directory)
    try:
        for entry in sorted(os.listdir(current_dir)):
            if entry.endswith('.md') and entry != current_filename:
                entry_path = os.path.relpath(os.path.join(current_dir, entry), VAULT_PATH)
                nav['siblings'].append({
                    'name': entry.replace('.md', ''),
                    'path': entry_path
                })
    except Exception:
        pass
    
    # Get children (MD files in 'subpages' subfolder or any subfolder)
    subpages_dir = os.path.join(current_dir, 'subpages')
    if os.path.isdir(subpages_dir):
        try:
            for entry in sorted(os.listdir(subpages_dir)):
                if entry.endswith('.md'):
                    entry_path = os.path.relpath(os.path.join(subpages_dir, entry), VAULT_PATH)
                    nav['children'].append({
                        'name': entry.replace('.md', ''),
                        'path': entry_path
                    })
        except Exception:
            pass
    
    # Also check for a subfolder with the same name as the current file
    same_name_dir = os.path.join(current_dir, current_name)
    if os.path.isdir(same_name_dir):
        try:
            for entry in sorted(os.listdir(same_name_dir)):
                if entry.endswith('.md'):
                    entry_path = os.path.relpath(os.path.join(same_name_dir, entry), VAULT_PATH)
                    # Avoid duplicates
                    if not any(c['path'] == entry_path for c in nav['children']):
                        nav['children'].append({
                            'name': entry.replace('.md', ''),
                            'path': entry_path
                        })
        except Exception:
            pass
    
    return nav


def render_page_navigation(nav, current_dir):
    """Render the navigation HTML"""
    if not nav['parents'] and not nav['siblings'] and not nav['children']:
        return ''
    
    html_parts = ['<div class="page-navigation">']
    html_parts.append('<h3>📍 Navigation</h3>')
    
    # Parent links (backlinks - pages that reference this page)
    if nav['parents']:
        html_parts.append('<div class="nav-section">')
        count = len(nav['parents'])
        html_parts.append(f'<h4>⬆️ Referenced By ({count} {"page" if count == 1 else "pages"})</h4>')
        html_parts.append('<ul class="parents-list">')
        for parent in nav['parents'][:10]:  # Limit to 10
            html_parts.append(f'<li><a href="/view/{parent["path"]}" class="nav-link parent-link">📄 {parent["name"]}</a></li>')
        if len(nav['parents']) > 10:
            html_parts.append(f'<li class="more-items">+{len(nav["parents"]) - 10} more...</li>')
        html_parts.append('</ul>')
        html_parts.append('</div>')
    
    # Siblings (Related pages)
    if nav['siblings']:
        html_parts.append('<div class="nav-section">')
        html_parts.append(f'<h4>📄 Related ({len(nav["siblings"])} siblings)</h4>')
        html_parts.append('<ul class="siblings-list">')
        for sibling in nav['siblings'][:15]:  # Limit to 15
            html_parts.append(f'<li><a href="/view/{sibling["path"]}" class="nav-link sibling-link">{sibling["name"]}</a></li>')
        if len(nav['siblings']) > 15:
            html_parts.append(f'<li class="more-items">+{len(nav["siblings"]) - 15} more...</li>')
        html_parts.append('</ul>')
        html_parts.append('</div>')
    
    # Children (Subpages)
    if nav['children']:
        html_parts.append('<div class="nav-section">')
        html_parts.append(f'<h4>📂 Subpages ({len(nav["children"])})</h4>')
        html_parts.append('<ul class="children-list">')
        for child in nav['children'][:20]:  # Limit to 20
            html_parts.append(f'<li><a href="/view/{child["path"]}" class="nav-link child-link">{child["name"]}</a></li>')
        if len(nav['children']) > 20:
            html_parts.append(f'<li class="more-items">+{len(nav["children"]) - 20} more...</li>')
        html_parts.append('</ul>')
        html_parts.append('</div>')
    
    html_parts.append('</div>')
    return '\n'.join(html_parts)


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <title>{{ title }} - Obsidian Viewer</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>">
    <link rel="manifest" href="/manifest.json">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        /* Prevent double-tap zoom on touch devices */
        html, body {
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
        }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
            display: flex; 
            height: 100vh; 
            background: #f5f5f5;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            text-rendering: optimizeLegibility;
        }
        
        /* Sidebar */
        .sidebar { 
            width: 280px; 
            min-width: 280px;
            background: #252526; 
            color: #cccccc; 
            overflow-y: auto; 
            flex-shrink: 0;
            box-shadow: 1px 0 3px rgba(0,0,0,0.3);
            transition: width 0.25s ease, min-width 0.25s ease, transform 0.25s ease;
            position: relative;
            z-index: 100;
            font-size: 13px;
        }
        .sidebar.collapsed { 
            width: 0 !important;
            min-width: 0 !important;
            overflow: hidden !important;
            padding: 0 !important;
        }
        .sidebar.collapsed .sidebar-content,
        .sidebar.collapsed .sidebar-header {
            opacity: 0;
            pointer-events: none;
        }
        
        /* Dock toggle button - always visible */
        .dock-toggle {
            position: fixed;
            top: 50%;
            left: 280px;
            transform: translateY(-50%);
            z-index: 150;
            background: #1e1e1e;
            color: #ccc;
            border: none;
            padding: 12px 6px;
            border-radius: 0 6px 6px 0;
            cursor: pointer;
            font-size: 14px;
            box-shadow: 2px 0 8px rgba(0,0,0,0.3);
            transition: left 0.25s ease, background 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 24px;
            height: 60px;
        }
        .dock-toggle:hover {
            background: #333;
            color: #fff;
        }
        .dock-toggle.collapsed {
            left: 0;
            border-radius: 0 6px 6px 0;
        }
        .dock-toggle .arrow {
            transition: transform 0.25s ease;
        }
        .dock-toggle.collapsed .arrow {
            transform: rotate(180deg);
        }
        .sidebar-header {
            padding: 12px 16px;
            background: #1e1e1e;
            border-bottom: 1px solid #3c3c3c;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        .sidebar h2 { 
            color: #cccccc; 
            font-size: 11px; 
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin: 0;
        }
        .sidebar-content {
            padding: 8px 0;
        }
        /* Hide tree until JS restores state (prevents flicker) */
        .sidebar-content.tree-loading {
            visibility: hidden;
        }
        
        /* Right Sidebar - Graph Panel */
        .graph-sidebar {
            width: 320px;
            min-width: 320px;
            background: #252526;
            color: #cccccc;
            flex-shrink: 0;
            box-shadow: -1px 0 3px rgba(0,0,0,0.3);
            transition: width 0.25s ease, min-width 0.25s ease;
            position: relative;
            z-index: 100;
            display: flex;
            flex-direction: column;
        }
        .graph-sidebar.collapsed {
            width: 0 !important;
            min-width: 0 !important;
            overflow: hidden !important;
        }
        .graph-sidebar-header {
            padding: 12px 16px;
            background: #1e1e1e;
            border-bottom: 1px solid #3c3c3c;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .graph-sidebar-header h3 {
            color: #cccccc;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin: 0;
        }
        .graph-sidebar-close {
            background: #3c3c3c;
            border: none;
            color: #ccc;
            padding: 4px 8px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }
        .graph-sidebar-close:hover {
            background: #cc3333;
            color: white;
        }
        .graph-container {
            flex: 1;
            position: relative;
            overflow: hidden;
        }
        #graphCanvas {
            width: 100%;
            height: 100%;
            display: block;
        }
        .graph-controls {
            padding: 8px 12px;
            background: #1e1e1e;
            border-top: 1px solid #3c3c3c;
            display: flex;
            gap: 8px;
            justify-content: center;
        }
        .graph-controls button {
            background: #3c3c3c;
            border: none;
            color: #ccc;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }
        .graph-controls button:hover {
            background: #4c4c4c;
        }
        /* Size buttons - mobile only */
        .graph-controls .size-buttons {
            display: none;
        }
        @media (max-width: 768px) {
            .graph-controls .size-buttons {
                display: flex;
                gap: 8px;
                align-items: center;
            }
        }
        /* Resize handle - desktop only */
        .graph-resize-handle {
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 6px;
            background: transparent;
            cursor: ew-resize;
            z-index: 20;
            transition: background 0.2s;
        }
        .graph-resize-handle:hover,
        .graph-resize-handle.dragging {
            background: #0066cc;
        }
        .graph-resize-handle::after {
            content: '⋮';
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            color: #666;
            font-size: 14px;
            pointer-events: none;
        }
        .graph-resize-handle:hover::after {
            color: #fff;
        }
        @media (max-width: 768px) {
            .graph-resize-handle {
                display: none;
            }
        }
        .graph-sidebar.size-small {
            width: 280px !important;
            min-width: 280px !important;
        }
        .graph-sidebar.size-medium {
            width: 400px !important;
            min-width: 400px !important;
        }
        .graph-sidebar.size-large {
            width: 550px !important;
            min-width: 550px !important;
        }
        .graph-sidebar.size-full {
            width: 100vw !important;
            min-width: 100vw !important;
            position: fixed !important;
            top: 0 !important;
            right: 0 !important;
            bottom: 0 !important;
            z-index: 500 !important;
        }
        /* Collapsed state must override size classes */
        .graph-sidebar.collapsed {
            width: 0 !important;
            min-width: 0 !important;
            overflow: hidden !important;
            padding: 0 !important;
        }
        /* Toggle button position for different sizes */
        .graph-dock-toggle.open.size-small { right: 280px; }
        .graph-dock-toggle.open.size-medium { right: 400px; }
        .graph-dock-toggle.open.size-large { right: 550px; }
        .graph-dock-toggle.open.size-full { display: none; }
        .graph-dock-toggle {
            position: fixed;
            right: 0;
            top: 50%;
            transform: translateY(-50%);
            z-index: 150;
            background: #3c3c3c;
            border: none;
            color: #ccc;
            padding: 12px 6px;
            cursor: pointer;
            border-radius: 6px 0 0 6px;
            transition: right 0.25s ease;
        }
        .graph-dock-toggle:hover {
            background: #4c4c4c;
        }
        .graph-dock-toggle .arrow {
            transition: transform 0.25s ease;
        }
        .graph-dock-toggle.open .arrow {
            transform: rotate(180deg);
        }
        .graph-dock-toggle.open {
            right: 320px;
        }
        
        /* Search Box Styles */
        .search-container {
            padding: 12px 0;
            border-bottom: 1px solid #3c3c3c;
        }
        .search-input-wrapper {
            position: relative;
            display: flex;
            align-items: center;
        }
        .search-input {
            width: 100%;
            padding: 8px 12px 8px 32px;
            font-size: 13px;
            background: #3c3c3c;
            border: 1px solid #4a4a4a;
            border-radius: 6px;
            color: #e0e0e0;
            outline: none;
            transition: border-color 0.2s, background 0.2s;
        }
        .search-input::placeholder {
            color: #888;
        }
        .search-input:focus {
            border-color: #007acc;
            background: #2d2d2d;
        }
        .search-icon {
            position: absolute;
            left: 10px;
            color: #888;
            font-size: 14px;
            pointer-events: none;
        }
        .search-clear {
            position: absolute;
            right: 8px;
            background: none;
            border: none;
            color: #888;
            cursor: pointer;
            font-size: 14px;
            padding: 4px;
            display: none;
        }
        .search-clear:hover {
            color: #ccc;
        }
        .search-input:not(:placeholder-shown) + .search-clear {
            display: block;
        }
        .search-options {
            display: flex;
            gap: 8px;
            margin-top: 8px;
            align-items: center;
        }
        .search-options label {
            font-size: 11px;
            color: #888;
            display: flex;
            align-items: center;
            gap: 4px;
            cursor: pointer;
        }
        .search-options input[type="checkbox"] {
            width: 12px;
            height: 12px;
            accent-color: #007acc;
        }
        .search-results {
            max-height: 400px;
            overflow-y: auto;
            margin-top: 8px;
            display: none;
        }
        .search-results.visible {
            display: block;
        }
        .search-result-item {
            padding: 8px 10px;
            border-radius: 4px;
            cursor: pointer;
            transition: background 0.15s;
            border-bottom: 1px solid #333;
        }
        .search-result-item:hover {
            background: #2a2d2e;
        }
        .search-result-item:last-child {
            border-bottom: none;
        }
        .search-result-filename {
            font-size: 13px;
            color: #e0e0e0;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .search-result-filename::before {
            content: "📄";
            font-size: 12px;
        }
        .search-result-path {
            font-size: 10px;
            color: #666;
            margin-top: 2px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .search-result-snippet {
            font-size: 11px;
            color: #888;
            margin-top: 4px;
            line-height: 1.4;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .search-result-snippet mark {
            background: #5a4a00;
            color: #ffd700;
            padding: 0 2px;
            border-radius: 2px;
        }
        .search-no-results {
            padding: 16px;
            text-align: center;
            color: #666;
            font-size: 12px;
        }
        .search-loading {
            padding: 16px;
            text-align: center;
            color: #888;
            font-size: 12px;
        }
        
        .sidebar ul { list-style: none; margin: 0; padding: 0; }
        .sidebar > .sidebar-content > ul { padding: 0 8px; }
        
        /* Folder styles */
        .folder-item {
            user-select: none;
        }
        .folder-header {
            display: flex;
            align-items: center;
            padding: 4px 8px;
            cursor: pointer;
            border-radius: 4px;
            transition: background 0.15s;
            color: #cccccc;
        }
        .folder-header:hover {
            background: #2a2d2e;
        }
        .folder-icon {
            width: 20px;
            height: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 4px;
            font-size: 10px;
            transition: transform 0.2s;
            color: #888;
        }
        .folder-icon.collapsed {
            transform: rotate(-90deg);
        }
        .folder-name {
            color: #cccccc;
            font-weight: 500;
        }
        .folder-name::before {
            content: "📁 ";
            font-size: 14px;
            margin-right: 4px;
        }
        .folder-item.open > .folder-header .folder-name::before {
            content: "📂 ";
        }
        
        /* Nested content - Tree structure with lines */
        .folder-children {
            overflow: hidden;
            max-height: 0;
            transition: max-height 0.25s ease-out;
            margin-left: 8px;
            padding-left: 16px;
            position: relative;
        }
        .folder-item.open > .folder-children {
            max-height: 5000px;
            transition: max-height 0.4s ease-in;
        }
        
        /* Vertical line running down the tree */
        .folder-children::before {
            content: '';
            position: absolute;
            left: 6px;
            top: 0;
            bottom: 8px;
            width: 1px;
            background: #3c3c3c;
        }
        
        /* Horizontal connector for each item */
        .folder-children > li {
            position: relative;
        }
        .folder-children > li::before {
            content: '';
            position: absolute;
            left: -10px;
            top: 14px;
            width: 10px;
            height: 1px;
            background: #3c3c3c;
        }
        
        /* Last item - L-shaped connector (hide line below) */
        .folder-children > li:last-child::after {
            content: '';
            position: absolute;
            left: -10px;
            top: 14px;
            bottom: 0;
            width: 1px;
            background: #252526; /* same as sidebar background to hide vertical line */
        }
        
        /* File styles */
        .sidebar a { 
            color: #cccccc; 
            text-decoration: none; 
            font-size: 13px; 
            display: flex;
            align-items: center;
            padding: 4px 8px; 
            border-radius: 4px;
            transition: background 0.15s;
            margin: 1px 0;
            margin-left: 4px;
        }
        .sidebar a:hover { background: #2a2d2e; }
        .sidebar a.active { background: #094771; color: #fff; }
        .sidebar a::before { margin-right: 6px; font-size: 14px; }
        
        /* Adjust folder header alignment in nested contexts */
        .folder-children .folder-header {
            margin-left: 4px;
        }
        
        /* Toggle button - hidden on desktop, shown on mobile */
        .toggle-btn {
            display: none;
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
        .file-md::before { content: "📄"; }
        .file-pdf::before { content: "📕"; }
        .file-img::before { content: "🖼️"; }
        .file-video::before { content: "🎬"; }
        .file-txt::before { content: "📝"; }
        
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
        
        /* Dark Theme */
        body.dark-theme {
            background: #1a1a1a;
        }
        body.dark-theme .content-wrapper {
            background: #1e1e1e;
        }
        body.dark-theme .content {
            background: #252526;
            color: #d4d4d4;
        }
        body.dark-theme .content h1,
        body.dark-theme .content h2,
        body.dark-theme .content h3,
        body.dark-theme .content h4,
        body.dark-theme .content h5,
        body.dark-theme .content h6 {
            color: #e0e0e0;
        }
        body.dark-theme .content a {
            color: #6cb6ff;
        }
        body.dark-theme .content a:hover {
            color: #8fcdff;
        }
        body.dark-theme .content code {
            background: #363636;
            color: #ce9178;
        }
        body.dark-theme .content pre {
            background: #1e1e1e;
            border-color: #3c3c3c;
        }
        body.dark-theme .content pre code {
            background: transparent;
        }
        body.dark-theme .content blockquote {
            border-left-color: #555;
            background: #2d2d2d;
            color: #b0b0b0;
        }
        body.dark-theme .content table {
            border-color: #3c3c3c;
        }
        body.dark-theme .content th {
            background: #2d2d2d;
            border-color: #3c3c3c;
            color: #e0e0e0;
        }
        body.dark-theme .content td {
            border-color: #3c3c3c;
        }
        body.dark-theme .content tr:hover {
            background: #3a3a3a;
        }
        body.dark-theme .content tr:hover td {
            color: #e0e0e0;
        }
        body.dark-theme .content hr {
            border-color: #3c3c3c;
        }
        body.dark-theme .file-path-bar {
            background: #2d2d2d;
            border-color: #3c3c3c;
            color: #b0b0b0;
        }
        body.dark-theme .file-path-bar .copy-btn {
            background: #3c3c3c;
            color: #d4d4d4;
        }
        body.dark-theme .file-path-bar .copy-btn:hover {
            background: #4a4a4a;
        }
        body.dark-theme .metadata-modal-content {
            background: #252526;
            color: #d4d4d4;
        }
        body.dark-theme .metadata-header {
            background: #1e1e1e;
            border-color: #3c3c3c;
        }
        body.dark-theme .metadata-footer {
            background: #1e1e1e;
            border-color: #3c3c3c;
        }
        body.dark-theme .metadata-field input,
        body.dark-theme .metadata-field textarea {
            background: #1e1e1e;
            border-color: #3c3c3c;
            color: #d4d4d4;
        }
        body.dark-theme .metadata-field input:focus,
        body.dark-theme .metadata-field textarea:focus {
            border-color: #0066cc;
        }
        /* Dark theme - ensure all text is visible */
        body.dark-theme .content,
        body.dark-theme .content p,
        body.dark-theme .content li,
        body.dark-theme .content span,
        body.dark-theme .content div,
        body.dark-theme .content em,
        body.dark-theme .content strong,
        body.dark-theme .content small {
            color: #d4d4d4;
        }
        body.dark-theme .content strong {
            color: #e8e8e8;
        }
        body.dark-theme .content em {
            color: #c5c5c5;
        }
        /* Muted/secondary text */
        body.dark-theme .content .muted,
        body.dark-theme .content .text-muted,
        body.dark-theme .content .secondary,
        body.dark-theme .content small,
        body.dark-theme .content .caption {
            color: #a0a0a0 !important;
        }
        /* SVG and inline styles override */
        body.dark-theme .content svg text,
        body.dark-theme .content [style*="color: #"] {
            fill: #d4d4d4;
        }
        /* Lists */
        body.dark-theme .content ul,
        body.dark-theme .content ol {
            color: #d4d4d4;
        }
        /* Images with dark backgrounds */
        body.dark-theme .content img {
            background: #2d2d2d;
        }
        /* Definition lists and terms */
        body.dark-theme .content dt,
        body.dark-theme .content dd {
            color: #d4d4d4;
        }
        /* Ensure inherited colors are overridden */
        body.dark-theme .content * {
            border-color: #3c3c3c;
        }
        body.dark-theme .content mark {
            background: #5a4a00;
            color: #ffeb3b;
        }
        /* Dark theme - Callouts */
        body.dark-theme .callout {
            background: #2d2d2d;
            border-color: #555;
            color: #d4d4d4;
        }
        body.dark-theme .callout-title {
            color: #e0e0e0;
        }
        body.dark-theme .callout-content {
            color: #d4d4d4;
        }
        body.dark-theme .callout-note, 
        body.dark-theme .callout-info { 
            background: #1a2d3d; 
            border-color: #2d6da8; 
        }
        body.dark-theme .callout-note .callout-title, 
        body.dark-theme .callout-info .callout-title { 
            color: #6cb6ff; 
        }
        body.dark-theme .callout-tip, 
        body.dark-theme .callout-hint { 
            background: #1a2d24; 
            border-color: #2d8a5e; 
        }
        body.dark-theme .callout-tip .callout-title, 
        body.dark-theme .callout-hint .callout-title { 
            color: #4ade80; 
        }
        body.dark-theme .callout-warning, 
        body.dark-theme .callout-caution { 
            background: #2d2618; 
            border-color: #b8860b; 
        }
        body.dark-theme .callout-warning .callout-title, 
        body.dark-theme .callout-caution .callout-title { 
            color: #fbbf24; 
        }
        body.dark-theme .callout-danger, 
        body.dark-theme .callout-error { 
            background: #2d1a1a; 
            border-color: #a53030; 
        }
        body.dark-theme .callout-danger .callout-title, 
        body.dark-theme .callout-error .callout-title { 
            color: #f87171; 
        }
        body.dark-theme .callout-question, 
        body.dark-theme .callout-help, 
        body.dark-theme .callout-faq { 
            background: #251a2d; 
            border-color: #7c3aed; 
        }
        body.dark-theme .callout-question .callout-title, 
        body.dark-theme .callout-help .callout-title, 
        body.dark-theme .callout-faq .callout-title { 
            color: #a78bfa; 
        }
        body.dark-theme .callout-example { 
            background: #1a1a2d; 
            border-color: #4f46e5; 
        }
        body.dark-theme .callout-example .callout-title { 
            color: #818cf8; 
        }
        body.dark-theme .callout-quote { 
            background: #262626; 
            border-color: #525252; 
        }
        body.dark-theme .callout-quote .callout-title { 
            color: #a3a3a3; 
        }
        /* Force all text in content to be readable - catch inline styles */
        body.dark-theme .content [style*="color:"] {
            color: #d4d4d4 !important;
        }
        body.dark-theme .content [style*="color: rgb(1"],
        body.dark-theme .content [style*="color: rgb(2"],
        body.dark-theme .content [style*="color: rgb(3"],
        body.dark-theme .content [style*="color: rgb(4"],
        body.dark-theme .content [style*="color: rgb(5"],
        body.dark-theme .content [style*="color: rgb(6"],
        body.dark-theme .content [style*="color: rgb(7"],
        body.dark-theme .content [style*="color: rgb(8"],
        body.dark-theme .content [style*="color: rgb(9"],
        body.dark-theme .content [style*="color:#"],
        body.dark-theme .content [style*="color: #"] {
            color: #d4d4d4 !important;
        }
        /* Theme toggle button */
        .theme-toggle {
            font-size: 16px !important;
            padding: 10px 14px !important;
        }
        
        /* Toolbar dropdown menu */
        .toolbar-menu {
            position: relative;
        }
        .toolbar-menu-btn {
            background: #0066cc !important;
            min-width: 44px;
            justify-content: center;
        }
        .toolbar-dropdown {
            display: none;
            position: absolute;
            top: 100%;
            right: 0;
            margin-top: 8px;
            background: #2a2a2a;
            border-radius: 10px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.4);
            overflow-x: hidden;
            overflow-y: auto;
            min-width: 160px;
            max-height: calc(100vh - 100px);
            z-index: 300;
        }
        .toolbar-dropdown.show {
            display: flex;
            flex-direction: column;
        }
        .toolbar-dropdown button {
            background: transparent;
            border-radius: 0;
            box-shadow: none;
            padding: 12px 16px;
            justify-content: flex-start;
            font-size: 14px;
            border-bottom: 1px solid #3a3a3a;
        }
        .toolbar-dropdown button:last-child {
            border-bottom: none;
        }
        .toolbar-dropdown button:hover {
            background: #3a3a3a;
            transform: none;
        }
        
        /* Mobile: dropdown appears ABOVE the button, fixed to viewport */
        @media screen and (max-width: 768px) {
            .toolbar-dropdown {
                position: fixed !important;
                top: auto !important;
                bottom: calc(20px + 48px + 8px) !important;
                right: 15px !important;
                left: auto !important;
                margin-top: 0 !important;
                margin-bottom: 0 !important;
                background: #1e1e1e !important;
                border: 1px solid #444 !important;
                border-radius: 10px !important;
                min-width: 140px !important;
                max-width: calc(100vw - 30px) !important;
                max-height: calc(100vh - 120px) !important;
                overflow-y: auto !important;
                overflow-x: hidden !important;
                box-sizing: border-box !important;
            }
            .toolbar-menu .toolbar-dropdown button,
            .toolbar-dropdown button {
                background: #1e1e1e !important;
                color: #fff !important;
                padding: 10px 14px !important;
                font-size: 14px !important;
                border-bottom: 1px solid #333 !important;
                box-shadow: none !important;
                display: block !important;
                text-align: left !important;
                white-space: normal !important;
                line-height: 1.4 !important;
                box-sizing: border-box !important;
                width: 100% !important;
            }
            .toolbar-menu .toolbar-dropdown button:last-child {
                border-bottom: none !important;
            }
            .toolbar-menu .toolbar-dropdown button:hover,
            .toolbar-menu .toolbar-dropdown button:active {
                background: #333 !important;
            }
        }
        
        /* Sidebar close button (mobile only) */
        .sidebar-close {
            display: none;
            position: absolute;
            top: 15px;
            right: 15px;
            background: #ff4444;
            color: #fff;
            border: none;
            padding: 10px 16px;
            border-radius: 6px;
            font-size: 14px;
            cursor: pointer;
            font-weight: 500;
        }
        .sidebar-close:hover { background: #cc3333; }
        
        /* Typography */
        .content h1 { 
            color: #1a1a1a; 
            margin: 30px 0 20px; 
            font-size: 2em;
            border-bottom: 3px solid #0066cc; 
            padding-bottom: 15px; 
        }
        .content h2 { color: #333; margin: 25px 0 15px; font-size: 1.5em; }
        
        /* Title with metadata badges */
        .title-with-meta {
            margin-bottom: 20px;
        }
        .title-with-meta h1 {
            margin-bottom: 10px;
        }
        .meta-badges {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 15px;
        }
        .meta-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 500;
        }
        .meta-badge.completed {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .meta-badge.incomplete {
            background: #fff3cd;
            color: #856404;
            border: 1px solid #ffeaa7;
        }
        .meta-badge.revision {
            background: #e7f1ff;
            color: #004085;
            border: 1px solid #b8daff;
        }
        body.dark-theme .meta-badge.completed {
            background: #1e3a2f;
            color: #75d9a0;
            border-color: #2d5a45;
        }
        body.dark-theme .meta-badge.incomplete {
            background: #3d3520;
            color: #ffc107;
            border-color: #5a4f2a;
        }
        body.dark-theme .meta-badge.revision {
            background: #1a2d4a;
            color: #6cb6ff;
            border-color: #2a4a6a;
        }
        
        /* Dashboard Dark Mode */
        body.dark-theme .dashboard-container {
            background: #1e1e1e;
            color: #e0e0e0;
        }
        body.dark-theme .dashboard-header h2 {
            color: #e0e0e0;
        }
        body.dark-theme .dashboard-stat {
            background: #2d2d2d;
        }
        body.dark-theme .dashboard-stat-value {
            color: #e0e0e0;
        }
        body.dark-theme .dashboard-stat-label {
            color: #9ca3af;
        }
        body.dark-theme .dashboard-progress h3 {
            color: #9ca3af;
        }
        body.dark-theme .progress-bar-container {
            background: #374151;
        }
        body.dark-theme .progress-label {
            color: #9ca3af;
        }
        body.dark-theme .heatmap-container h3 {
            color: #9ca3af;
        }
        body.dark-theme .heatmap-day {
            background: #374151;
        }
        body.dark-theme .heatmap-day.level-1 { background: #064e3b; }
        body.dark-theme .heatmap-day.level-2 { background: #047857; }
        body.dark-theme .heatmap-day.level-3 { background: #059669; }
        body.dark-theme .heatmap-day.level-4 { background: #10b981; }
        body.dark-theme .flashcard-empty {
            background: #2d2d2d;
            color: #e0e0e0;
        }
        body.dark-theme .flashcard-empty h3 {
            color: #e0e0e0;
        }
        body.dark-theme .flashcard-empty p {
            color: #9ca3af;
        }
        body.dark-theme .flashcard-empty pre {
            background: #1a1a1a;
            color: #a5d6ff;
        }
        
        .content h3 { color: #444; margin: 20px 0 10px; font-size: 1.25em; }
        .content p { line-height: 1.8; margin: 15px 0; color: #444; font-size: 16px; }
        .content ul, .content ol { margin: 15px 0 15px 30px; }
        .content li { margin: 10px 0; line-height: 1.7; }
        /* Task list (checkbox) styling */
        .task-item { 
            display: flex; 
            align-items: flex-start; 
            gap: 8px; 
            margin: 8px 0;
            cursor: default;
        }
        .task-item input[type="checkbox"] { 
            margin-top: 4px;
            width: 16px;
            height: 16px;
            accent-color: #4CAF50;
        }
        .task-done { 
            text-decoration: line-through; 
            opacity: 0.7; 
        }
        /* Nested lists styling */
        .content li > ul, .content li > ol { margin: 10px 0 10px 20px; }
        /* Ensure numbered lists inside list items reset to proper indentation */
        .content li > ol { 
            margin-left: -10px;  /* Pull back nested numbered lists */
            margin-top: 15px;
        }
        .content li > ol > li {
            font-weight: 600;  /* Make nested numbered items stand out */
        }
        /* Sub-bullet styling */
        .content ul ul, .content ol ul {
            list-style-type: circle;
        }
        
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
        .content img { 
            max-width: 100%; 
            height: auto; 
            border-radius: 10px; 
            margin: 20px 0; 
            box-shadow: 0 4px 15px rgba(0,0,0,0.1); 
            cursor: zoom-in;
            transition: transform 0.2s ease;
        }
        .content img:hover {
            transform: scale(1.02);
        }
        
        /* Image Lightbox Modal */
        .image-lightbox {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.9);
            z-index: 10000;
            justify-content: center;
            align-items: center;
            cursor: zoom-out;
        }
        .image-lightbox.active {
            display: flex;
        }
        .image-lightbox img {
            max-width: 95%;
            max-height: 95%;
            object-fit: contain;
            border-radius: 8px;
            box-shadow: 0 0 50px rgba(0,0,0,0.5);
            cursor: grab;
            transition: transform 0.3s ease;
        }
        .image-lightbox img.zoomed {
            cursor: grabbing;
            max-width: none;
            max-height: none;
        }
        .lightbox-controls {
            position: fixed;
            top: 20px;
            right: 20px;
            display: flex;
            gap: 10px;
            z-index: 10001;
        }
        .lightbox-btn {
            background: rgba(255,255,255,0.2);
            border: none;
            color: white;
            width: 44px;
            height: 44px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: background 0.2s;
        }
        .lightbox-btn:hover {
            background: rgba(255,255,255,0.3);
        }
        .lightbox-zoom-info {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(0,0,0,0.7);
            color: white;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            z-index: 10001;
        }
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
        /* Obsidian Callouts */
        .callout {
            border-radius: 8px;
            margin: 16px 0;
            padding: 12px 16px;
            border-left: 4px solid;
        }
        .callout-title {
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
        }
        .callout-content { margin-top: 8px; }
        .callout-content p:first-child { margin-top: 0; }
        .callout-content p:last-child { margin-bottom: 0; }
        /* Callout types */
        .callout-note, .callout-info { background: #e7f3ff; border-color: #0066cc; }
        .callout-note .callout-title, .callout-info .callout-title { color: #0066cc; }
        .callout-tip, .callout-hint { background: #e6f9ed; border-color: #10b981; }
        .callout-tip .callout-title, .callout-hint .callout-title { color: #10b981; }
        .callout-warning, .callout-caution { background: #fff7e6; border-color: #f59e0b; }
        .callout-warning .callout-title, .callout-caution .callout-title { color: #f59e0b; }
        .callout-danger, .callout-error { background: #fee2e2; border-color: #dc2626; }
        .callout-danger .callout-title, .callout-error .callout-title { color: #dc2626; }
        .callout-question, .callout-help, .callout-faq { background: #faf5ff; border-color: #8b5cf6; }
        .callout-question .callout-title, .callout-help .callout-title, .callout-faq .callout-title { color: #8b5cf6; }
        .callout-example { background: #f0f9ff; border-color: #6366f1; }
        .callout-example .callout-title { color: #6366f1; }
        .callout-quote { background: #f5f5f5; border-color: #6b7280; }
        .callout-quote .callout-title { color: #6b7280; }
        /* Collapsible callouts */
        details.callout { cursor: pointer; }
        details.callout summary { list-style: none; }
        details.callout summary::-webkit-details-marker { display: none; }
        details.callout summary .callout-title::after { content: '▶'; margin-left: auto; font-size: 12px; transition: transform 0.2s; }
        details.callout[open] summary .callout-title::after { transform: rotate(90deg); }
        
        /* Cheatsheet Relevance Controls */
        .callout-wrapper {
            position: relative;
            transition: opacity 0.3s, transform 0.3s;
        }
        .callout-wrapper.not-relevant {
            opacity: 0.5;
        }
        .callout-wrapper.dragging {
            opacity: 0.8;
            transform: scale(1.02);
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
            z-index: 100;
        }
        .callout-controls {
            position: absolute;
            top: 8px;
            right: 8px;
            display: flex;
            gap: 6px;
            align-items: center;
            z-index: 10;
            opacity: 0;
            transition: opacity 0.2s;
        }
        .callout-wrapper:hover .callout-controls {
            opacity: 1;
        }
        .callout-controls .relevance-checkbox {
            width: 18px;
            height: 18px;
            cursor: pointer;
            accent-color: #10b981;
        }
        .callout-controls .drag-handle {
            cursor: grab;
            padding: 4px;
            color: #888;
            font-size: 14px;
            user-select: none;
        }
        .callout-controls .drag-handle:active {
            cursor: grabbing;
        }
        .callout-controls .drag-handle:hover {
            color: #ccc;
        }
        .cheatsheet-toolbar {
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            padding: 12px;
            background: #2d2d2d;
            border-radius: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        .cheatsheet-toolbar button {
            padding: 8px 14px;
            background: #3c3c3c;
            border: none;
            color: #ccc;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .cheatsheet-toolbar button:hover {
            background: #4c4c4c;
        }
        .cheatsheet-toolbar button.active {
            background: #10b981;
            color: white;
        }
        .cheatsheet-toolbar .stats {
            margin-left: auto;
            color: #888;
            font-size: 12px;
        }
        .callout-wrapper.drag-over {
            border-top: 3px solid #10b981;
        }
        .content a { color: #0066cc; text-decoration: none; }
        .content a:hover { text-decoration: underline; }
        .content a.internal-link { 
            color: #7c3aed; 
            background: rgba(124, 58, 237, 0.1);
            padding: 1px 4px;
            border-radius: 3px;
        }
        .content a.internal-link:hover { 
            background: rgba(124, 58, 237, 0.2);
            text-decoration: none;
        }
        .content .broken-link {
            color: #dc2626;
            background: rgba(220, 38, 38, 0.1);
            padding: 1px 4px;
            border-radius: 3px;
            cursor: help;
        }
        .content strong { color: #1a1a1a; }
        .content hr { border: none; border-top: 2px solid #eee; margin: 30px 0; }
        
        /* Page Navigation Section */
        .page-navigation {
            margin-top: 60px;
            padding-top: 30px;
            border-top: 2px solid #e5e7eb;
            background: linear-gradient(to bottom, #f9fafb, #ffffff);
            border-radius: 12px;
            padding: 24px;
        }
        .page-navigation h3 {
            font-size: 18px;
            font-weight: 600;
            color: #374151;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 1px solid #e5e7eb;
        }
        .page-navigation .nav-section {
            margin-bottom: 20px;
        }
        .page-navigation .nav-section:last-child {
            margin-bottom: 0;
        }
        .page-navigation h4 {
            font-size: 14px;
            font-weight: 600;
            color: #6b7280;
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .page-navigation ul {
            list-style: none;
            padding: 0;
            margin: 0;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        .page-navigation .nav-link {
            display: inline-block;
            padding: 6px 12px;
            background: #f3f4f6;
            border-radius: 6px;
            color: #374151;
            text-decoration: none;
            font-size: 13px;
            transition: all 0.2s ease;
            border: 1px solid transparent;
        }
        .page-navigation .nav-link:hover {
            background: #e5e7eb;
            border-color: #d1d5db;
            text-decoration: none;
        }
        .page-navigation .parent-link {
            background: #dbeafe;
            color: #1e40af;
        }
        .page-navigation .parent-link:hover {
            background: #bfdbfe;
            border-color: #93c5fd;
        }
        .page-navigation .sibling-link {
            background: #f3e8ff;
            color: #6b21a8;
        }
        .page-navigation .sibling-link:hover {
            background: #e9d5ff;
            border-color: #d8b4fe;
        }
        .page-navigation .child-link {
            background: #dcfce7;
            color: #166534;
        }
        .page-navigation .child-link:hover {
            background: #bbf7d0;
            border-color: #86efac;
        }
        .page-navigation .more-items {
            color: #9ca3af;
            font-size: 12px;
            padding: 6px 12px;
            font-style: italic;
        }
        .page-navigation .siblings-list,
        .page-navigation .children-list {
            max-height: 200px;
            overflow-y: auto;
        }
        /* Dark theme for navigation */
        body.dark-theme .page-navigation {
            background: linear-gradient(to bottom, #1f2937, #111827);
            border-top-color: #374151;
        }
        body.dark-theme .page-navigation h3 {
            color: #e5e7eb;
            border-bottom-color: #374151;
        }
        body.dark-theme .page-navigation h4 {
            color: #9ca3af;
        }
        body.dark-theme .page-navigation .nav-link {
            background: #374151;
            color: #e5e7eb;
        }
        body.dark-theme .page-navigation .nav-link:hover {
            background: #4b5563;
            border-color: #6b7280;
        }
        body.dark-theme .page-navigation .parent-link {
            background: #1e3a5f;
            color: #93c5fd;
        }
        body.dark-theme .page-navigation .parent-link:hover {
            background: #1e40af;
        }
        body.dark-theme .page-navigation .sibling-link {
            background: #3b0764;
            color: #d8b4fe;
        }
        body.dark-theme .page-navigation .sibling-link:hover {
            background: #581c87;
        }
        body.dark-theme .page-navigation .child-link {
            background: #14532d;
            color: #86efac;
        }
        body.dark-theme .page-navigation .child-link:hover {
            background: #166534;
        }
        body.dark-theme .page-navigation .more-items {
            color: #6b7280;
        }
        
        /* Nested folders - legacy, now using folder-children */
        .nested { display: none; }
        
        /* Welcome page */
        .welcome { text-align: center; padding: 60px 40px; }
        .welcome h1 { border: none; font-size: 2.5em; margin-bottom: 20px; }
        .welcome p { font-size: 18px; color: #666; max-width: 500px; margin: 0 auto; }
        
        /* File path bar */
        .file-path-bar {
            background: #f8f9fa;
            border-bottom: 1px solid #e9ecef;
            padding: 8px 16px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-family: 'SF Mono', Monaco, 'Courier New', monospace;
            font-size: 13px;
            color: #666;
        }
        .file-path-bar .path-icon {
            color: #888;
        }
        .file-path-bar .path-text {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            max-width: calc(100vw - 450px); /* Limit width to leave space for toolbar */
        }
        @media (max-width: 768px) {
            .file-path-bar .path-text {
                max-width: calc(100vw - 120px); /* More space on mobile */
            }
        }
        .file-path-bar .path-separator {
            color: #ccc;
            margin: 0 2px;
        }
        .file-path-bar .path-segment {
            color: #555;
        }
        .file-path-bar .path-segment:last-child {
            color: #333;
            font-weight: 500;
        }
        .file-path-bar .copy-btn {
            background: #e9ecef;
            border: none;
            padding: 4px 10px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            color: #555;
            transition: background 0.2s;
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .file-path-bar .copy-btn:hover {
            background: #dee2e6;
        }
        .file-path-bar .copy-btn.copied {
            background: #d4edda;
            color: #155724;
        }
        
        /* Fullscreen mode adjustments */
        .fullscreen-mode .sidebar { display: none; }
        .fullscreen-mode .graph-sidebar { display: none; }
        .fullscreen-mode .graph-dock-toggle { display: none; }
        .fullscreen-mode .toggle-btn { left: 15px; }
        .fullscreen-mode .content { max-width: 100%; }
        
        /* Mobile responsive */
        @media (max-width: 768px) {
            body { flex-direction: column; }
            
            /* Hide dock toggle on mobile */
            .dock-toggle { display: none !important; }
            
            /* Graph sidebar as overlay on mobile */
            .graph-sidebar {
                position: fixed !important;
                top: 60px;
                right: 0;
                bottom: 0;
                width: 280px !important;
                min-width: 280px !important;
                z-index: 250;
                transform: translateX(100%);
                transition: transform 0.3s ease;
            }
            .graph-sidebar:not(.collapsed) {
                transform: translateX(0);
            }
            .graph-dock-toggle {
                z-index: 260;
                top: 70px;
                bottom: auto;
                left: auto;
                right: 10px;
                padding: 10px 8px;
                background: #3c3c3c;
                border-radius: 6px;
                width: auto;
                height: auto;
                display: flex !important;
                align-items: center;
                justify-content: center;
                font-size: 12px;
            }
            .graph-dock-toggle.open {
                right: 290px;
            }
            
            /* Show hamburger menu on mobile */
            .toggle-btn { display: block !important; }
            
            /* Sidebar as overlay on mobile */
            .sidebar { 
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                width: 100% !important;
                min-width: 100% !important;
                max-height: 100vh;
                z-index: 300;
                padding: 60px 20px 20px;
                transform: translateY(0);
                overflow-y: auto;
            }
            .sidebar.hidden { 
                transform: translateY(-100%);
                pointer-events: none;
            }
            .sidebar.collapsed {
                transform: none;
                width: 100% !important;
                min-width: 100% !important;
            }
            
            /* Close button inside sidebar on mobile */
            .sidebar-close {
                position: absolute;
                top: 15px;
                right: 15px;
                background: #ff4444;
                color: #fff;
                border: none;
                padding: 8px 14px;
                border-radius: 6px;
                font-size: 14px;
                cursor: pointer;
            }
            
            /* Toggle button as floating action button */
            .toggle-btn { 
                position: fixed;
                top: 15px;
                left: 15px;
                z-index: 200;
                background: #0066cc;
                padding: 10px 14px;
                font-size: 16px;
            }
            .toggle-btn.sidebar-open { 
                left: 15px;
                opacity: 0;
                pointer-events: none;
            }
            
            .content { 
                padding: 60px 20px 20px;
                margin-top: 0;
            }
            .content h1 { font-size: 1.5em; margin-top: 10px; }
            
            /* Toolbar on mobile */
            .toolbar { 
                top: auto;
                bottom: 20px;
                right: 15px;
                gap: 8px;
                flex-direction: column;
                align-items: flex-end;
            }
            .toolbar button {
                padding: 10px 14px;
                font-size: 14px;
                border-radius: 50%;
                width: 48px;
                height: 48px;
                justify-content: center;
            }
            .toolbar button span.btn-text { display: none; }
            
            /* Show close button on mobile */
            .sidebar-close { display: block; }
            
            /* Hide menu text on mobile toggle btn */
            .toggle-btn .btn-text { display: none; }
        }
        
        /* Print styles for PDF */
        @media print {
            .sidebar, .toggle-btn, .toolbar { display: none !important; }
            .content { max-width: 100% !important; padding: 20px !important; }
        }
        
        /* Mermaid diagram styles */
        .mermaid {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            margin: 16px 0;
            text-align: center;
        }
        .mermaid svg {
            max-width: 100%;
            height: auto;
        }
        
        /* Math equation styles */
        .MathJax {
            font-size: 1.1em !important;
        }
        mjx-container[jax="CHTML"][display="true"] {
            margin: 1em 0 !important;
            overflow-x: auto;
            overflow-y: hidden;
        }
        
        /* Overlay Annotation System - Draws on top of content */
        .annotation-overlay {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 50;
        }
        .annotation-overlay.active {
            pointer-events: auto;
        }
        .annotation-overlay canvas {
            position: absolute;
            top: 0;
            left: 0;
            /* Allow finger scrolling, only Apple Pencil draws */
            touch-action: pan-x pan-y;
            /* Smooth anti-aliased rendering for handwriting */
            image-rendering: auto;
            -webkit-transform: translateZ(0);
            transform: translateZ(0);
        }
        
        /* ===== FLASHCARD SYSTEM ===== */
        .flashcard-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.95);
            z-index: 2000;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            padding: 60px 10px 100px 10px;
            overflow: hidden;
        }
        .flashcard-modal.visible {
            display: flex;
        }
        .flashcard-container {
            width: 95%;
            max-width: 600px;
            perspective: 1000px;
            flex: 1;
            max-height: calc(100vh - 180px);
            min-height: 200px;
        }
        .flashcard {
            width: 100%;
            height: 100%;
            min-height: 200px;
            position: relative;
            transform-style: preserve-3d;
            transition: transform 0.6s cubic-bezier(0.4, 0, 0.2, 1);
            cursor: pointer;
        }
        .flashcard.flipped {
            transform: rotateY(180deg);
        }
        .flashcard-face {
            position: absolute;
            width: 100%;
            height: 100%;
            min-height: 200px;
            backface-visibility: hidden;
            border-radius: 16px;
            padding: 25px 20px;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            align-items: center;
            text-align: center;
            box-shadow: 0 10px 40px rgba(0,0,0,0.4);
            overflow-y: auto;
        }
        .flashcard-front {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #fff;
        }
        .flashcard-back {
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
            color: #fff;
            transform: rotateY(180deg);
        }
        .flashcard-label {
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 2px;
            opacity: 0.8;
            margin-bottom: 15px;
        }
        .flashcard-content {
            font-size: 20px;
            line-height: 1.5;
            font-weight: 500;
            overflow-y: auto;
            padding: 10px;
            width: 100%;
            text-align: left;
            flex: 1;
        }
        @media (max-width: 600px) {
            .flashcard-content {
                font-size: 16px;
                padding: 5px;
            }
            .flashcard-face {
                padding: 15px 12px;
            }
            .flashcard-modal {
                padding: 50px 5px 90px 5px;
            }
        }
        .flashcard-content strong {
            font-weight: 700;
        }
        .flashcard-content em {
            font-style: italic;
        }
        .flashcard-content code {
            background: rgba(0,0,0,0.2);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 18px;
        }
        .flashcard-hint {
            position: absolute;
            bottom: 15px;
            font-size: 12px;
            opacity: 0.6;
        }
        .flashcard-controls {
            display: flex;
            gap: 10px;
            flex-shrink: 0;
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 2001;
            background: rgba(0,0,0,0.8);
            padding: 10px 15px;
            border-radius: 30px;
        }
        @media (max-width: 600px) {
            .flashcard-controls {
                bottom: 15px;
                gap: 8px;
                padding: 8px 12px;
            }
            .flashcard-btn {
                padding: 10px 16px;
                font-size: 14px;
            }
        }
        .flashcard-btn {
            padding: 12px 28px;
            border: none;
            border-radius: 25px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .flashcard-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(0,0,0,0.3);
        }
        .flashcard-btn.primary {
            background: #fff;
            color: #333;
        }
        .flashcard-btn.secondary {
            background: rgba(255,255,255,0.2);
            color: #fff;
        }
        .flashcard-btn.correct {
            background: #4ade80;
            color: #fff;
        }
        .flashcard-btn.wrong {
            background: #f87171;
            color: #fff;
        }
        .flashcard-progress {
            position: absolute;
            top: 20px;
            left: 50%;
            transform: translateX(-50%);
            display: flex;
            align-items: center;
            gap: 15px;
            color: #fff;
            font-size: 14px;
        }
        .flashcard-progress-bar {
            width: 200px;
            height: 6px;
            background: rgba(255,255,255,0.2);
            border-radius: 3px;
            overflow: hidden;
        }
        .flashcard-progress-fill {
            height: 100%;
            background: #4ade80;
            border-radius: 3px;
            transition: width 0.3s;
        }
        .flashcard-close {
            position: absolute;
            top: 20px;
            right: 20px;
            background: rgba(255,255,255,0.2);
            border: none;
            color: #fff;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            font-size: 20px;
            cursor: pointer;
            transition: background 0.2s;
        }
        .flashcard-close:hover {
            background: rgba(255,255,255,0.3);
        }
        .flashcard-empty {
            text-align: center;
            color: #fff;
            padding: 40px;
        }
        .flashcard-empty h3 {
            font-size: 24px;
            margin-bottom: 15px;
        }
        .flashcard-empty p {
            opacity: 0.8;
            line-height: 1.6;
            max-width: 400px;
        }
        .flashcard-empty pre {
            background: rgba(0,0,0,0.3);
            padding: 20px;
            border-radius: 8px;
            text-align: left;
            font-size: 13px;
            margin-top: 20px;
            overflow-x: auto;
        }
        .flashcard-stats {
            display: flex;
            gap: 30px;
            margin-top: 20px;
            color: #fff;
        }
        .flashcard-stat {
            text-align: center;
        }
        .flashcard-stat-value {
            font-size: 32px;
            font-weight: bold;
        }
        .flashcard-stat-label {
            font-size: 12px;
            opacity: 0.7;
            text-transform: uppercase;
        }
        .flashcard-stat.correct .flashcard-stat-value { color: #4ade80; }
        .flashcard-stat.wrong .flashcard-stat-value { color: #f87171; }
        
        /* Shuffle animation */
        @keyframes shuffle {
            0% { transform: translateX(0) rotate(0deg); }
            25% { transform: translateX(-20px) rotate(-5deg); }
            50% { transform: translateX(20px) rotate(5deg); }
            75% { transform: translateX(-10px) rotate(-2deg); }
            100% { transform: translateX(0) rotate(0deg); }
        }
        .flashcard.shuffling {
            animation: shuffle 0.5s ease-in-out;
        }

        /* ===== CLOZE DELETION SYSTEM ===== */
        .cloze-blank {
            display: inline-block;
            min-width: 80px;
            padding: 2px 12px;
            margin: 0 2px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border-radius: 4px;
            font-weight: 500;
            text-align: center;
        }
        .cloze-blank.revealed {
            background: linear-gradient(135deg, #4ade80 0%, #22c55e 100%);
        }
        .cloze-card {
            font-size: 1.2em;
            line-height: 1.8;
            text-align: center;
            padding: 40px 20px;
        }
        .cloze-input {
            display: inline-block;
            min-width: 100px;
            padding: 4px 12px;
            border: 2px dashed #667eea;
            border-radius: 4px;
            background: rgba(102, 126, 234, 0.1);
            font-size: 1em;
            text-align: center;
            outline: none;
        }
        .cloze-input:focus {
            border-color: #764ba2;
            background: rgba(118, 75, 162, 0.1);
        }
        .cloze-input.correct {
            border-color: #4ade80;
            background: rgba(74, 222, 128, 0.2);
        }
        .cloze-input.incorrect {
            border-color: #f87171;
            background: rgba(248, 113, 113, 0.2);
        }

        /* ===== SRS RATING BUTTONS ===== */
        .srs-rating-controls {
            display: flex;
            gap: 8px;
            justify-content: center;
            flex-wrap: wrap;
            padding: 16px;
        }
        .srs-btn {
            padding: 12px 20px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
        }
        .srs-btn .interval {
            font-size: 11px;
            opacity: 0.8;
        }
        .srs-btn.again {
            background: #fee2e2;
            color: #dc2626;
        }
        .srs-btn.again:hover {
            background: #fecaca;
        }
        .srs-btn.hard {
            background: #fef3c7;
            color: #d97706;
        }
        .srs-btn.hard:hover {
            background: #fde68a;
        }
        .srs-btn.good {
            background: #d1fae5;
            color: #059669;
        }
        .srs-btn.good:hover {
            background: #a7f3d0;
        }
        .srs-btn.easy {
            background: #dbeafe;
            color: #2563eb;
        }
        .srs-btn.easy:hover {
            background: #bfdbfe;
        }

        /* ===== LEITNER BOX INDICATOR ===== */
        .leitner-box {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 4px 10px;
            background: #f3f4f6;
            border-radius: 12px;
            font-size: 12px;
            color: #6b7280;
        }
        .leitner-box.box-1 { background: #fee2e2; color: #dc2626; }
        .leitner-box.box-2 { background: #fef3c7; color: #d97706; }
        .leitner-box.box-3 { background: #fef9c3; color: #ca8a04; }
        .leitner-box.box-4 { background: #d1fae5; color: #059669; }
        .leitner-box.box-5 { background: #dbeafe; color: #2563eb; }

        /* ===== STUDY DASHBOARD ===== */
        .dashboard-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.85);
            z-index: 10000;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .dashboard-modal.visible {
            display: flex;
        }
        .dashboard-container {
            background: white;
            border-radius: 16px;
            max-width: 800px;
            width: 100%;
            max-height: 90vh;
            overflow-y: auto;
            padding: 24px;
        }
        .dashboard-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
        }
        .dashboard-header h2 {
            font-size: 24px;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .dashboard-stat {
            background: #f9fafb;
            border-radius: 12px;
            padding: 16px;
            text-align: center;
        }
        .dashboard-stat-value {
            font-size: 28px;
            font-weight: 700;
            color: #1f2937;
        }
        .dashboard-stat-label {
            font-size: 13px;
            color: #6b7280;
            margin-top: 4px;
        }
        .dashboard-stat.streak .dashboard-stat-value {
            color: #f97316;
        }
        .dashboard-stat.due .dashboard-stat-value {
            color: #8b5cf6;
        }
        .dashboard-stat.mastery .dashboard-stat-value {
            color: #10b981;
        }
        .dashboard-stat.weak .dashboard-stat-value {
            color: #ef4444;
        }
        .dashboard-progress {
            margin-bottom: 24px;
        }
        .dashboard-progress h3 {
            font-size: 14px;
            color: #6b7280;
            margin-bottom: 8px;
        }
        .progress-bar-container {
            background: #e5e7eb;
            border-radius: 9999px;
            height: 12px;
            overflow: hidden;
        }
        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
            border-radius: 9999px;
            transition: width 0.3s ease;
        }
        .progress-label {
            display: flex;
            justify-content: space-between;
            margin-top: 4px;
            font-size: 12px;
            color: #6b7280;
        }
        .heatmap-container {
            margin-top: 24px;
        }
        .heatmap-container h3 {
            font-size: 14px;
            color: #6b7280;
            margin-bottom: 12px;
        }
        .heatmap {
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 4px;
        }
        .heatmap-day {
            aspect-ratio: 1;
            border-radius: 4px;
            background: #f3f4f6;
            position: relative;
        }
        .heatmap-day.level-1 { background: #d1fae5; }
        .heatmap-day.level-2 { background: #6ee7b7; }
        .heatmap-day.level-3 { background: #34d399; }
        .heatmap-day.level-4 { background: #10b981; }
        .heatmap-day:hover::after {
            content: attr(title);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: #1f2937;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            white-space: nowrap;
            z-index: 10;
        }
        
        /* ===== DASHBOARD CHART ===== */
        .dashboard-chart-section {
            margin-top: 24px;
            padding: 16px;
            background: #f9fafb;
            border-radius: 12px;
        }
        body.dark-theme .dashboard-chart-section {
            background: #1f2937;
        }
        .dashboard-view-tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
        }
        .view-tab {
            flex: 1;
            padding: 10px 16px;
            border: none;
            border-radius: 8px;
            background: #e5e7eb;
            color: #374151;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        body.dark-theme .view-tab {
            background: #374151;
            color: #9ca3af;
        }
        .view-tab:hover {
            background: #d1d5db;
        }
        body.dark-theme .view-tab:hover {
            background: #4b5563;
        }
        .view-tab.active {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        .chart-summary {
            display: flex;
            justify-content: center;
            gap: 20px;
            margin-bottom: 16px;
            font-size: 14px;
            color: #6b7280;
        }
        body.dark-theme .chart-summary {
            color: #9ca3af;
        }
        .chart-summary span strong {
            color: #111827;
        }
        body.dark-theme .chart-summary span strong {
            color: #f3f4f6;
        }
        .dashboard-chart {
            display: flex;
            align-items: flex-end;
            justify-content: space-around;
            height: 120px;
            gap: 8px;
            padding: 0 8px;
        }
        .chart-bar-container {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            height: 100%;
        }
        .chart-bar {
            width: 100%;
            max-width: 40px;
            background: linear-gradient(180deg, #667eea 0%, #764ba2 100%);
            border-radius: 6px 6px 0 0;
            position: relative;
            display: flex;
            align-items: flex-start;
            justify-content: center;
            transition: height 0.3s ease;
            margin-top: auto;
        }
        .chart-bar-value {
            position: absolute;
            top: -20px;
            font-size: 11px;
            font-weight: 600;
            color: #6b7280;
        }
        body.dark-theme .chart-bar-value {
            color: #9ca3af;
        }
        .chart-bar-label {
            margin-top: 8px;
            font-size: 11px;
            color: #6b7280;
            text-align: center;
        }
        body.dark-theme .chart-bar-label {
            color: #9ca3af;
        }

        /* ===== SETTINGS MODAL ===== */
        .settings-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.85);
            z-index: 10000;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .settings-modal.visible {
            display: flex;
        }
        .settings-container {
            background: white;
            border-radius: 16px;
            max-width: 500px;
            width: 100%;
            max-height: 90vh;
            overflow-y: auto;
            padding: 24px;
        }
        .settings-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
        }
        .settings-header h2 {
            font-size: 20px;
            color: #1f2937;
        }
        .settings-section {
            margin-bottom: 24px;
        }
        .settings-section h3 {
            font-size: 14px;
            color: #6b7280;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .setting-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid #e5e7eb;
        }
        .setting-item:last-child {
            border-bottom: none;
        }
        .setting-label {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }
        .setting-label span {
            font-size: 15px;
            color: #1f2937;
        }
        .setting-label small {
            font-size: 12px;
            color: #6b7280;
        }
        .setting-toggle {
            position: relative;
            width: 48px;
            height: 26px;
        }
        .setting-toggle input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .setting-toggle .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #d1d5db;
            transition: 0.3s;
            border-radius: 26px;
        }
        .setting-toggle .slider:before {
            position: absolute;
            content: "";
            height: 20px;
            width: 20px;
            left: 3px;
            bottom: 3px;
            background-color: white;
            transition: 0.3s;
            border-radius: 50%;
        }
        .setting-toggle input:checked + .slider {
            background-color: #8b5cf6;
        }
        .setting-toggle input:checked + .slider:before {
            transform: translateX(22px);
        }
        .setting-input {
            width: 80px;
            padding: 8px 12px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 14px;
            text-align: center;
        }
        .setting-input:focus {
            outline: none;
            border-color: #8b5cf6;
        }
        .settings-save {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            margin-top: 16px;
        }
        .settings-save:hover {
            opacity: 0.9;
        }
        body.dark-theme .settings-container {
            background: #1e1e1e;
        }
        body.dark-theme .settings-header h2 {
            color: #e0e0e0;
        }
        body.dark-theme .setting-label span {
            color: #e0e0e0;
        }
        body.dark-theme .setting-label small {
            color: #9ca3af;
        }
        body.dark-theme .setting-item {
            border-color: #374151;
        }
        body.dark-theme .setting-input {
            background: #2d2d2d;
            border-color: #374151;
            color: #e0e0e0;
        }

        /* ===== CARD TYPE BADGES ===== */
        .card-type-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 500;
        }
        .card-type-badge.flash {
            background: #fef3c7;
            color: #d97706;
        }
        .card-type-badge.mcq {
            background: #dbeafe;
            color: #2563eb;
        }
        .card-type-badge.cloze {
            background: #f3e8ff;
            color: #9333ea;
        }

        /* ===== TIMER BAR ===== */
        .timer-bar {
            height: 4px;
            background: #e5e7eb;
            border-radius: 2px;
            overflow: hidden;
            margin-bottom: 16px;
        }
        .timer-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #4ade80 0%, #f97316 50%, #ef4444 100%);
            transition: width 0.1s linear;
        }
        .timer-bar.warning .timer-bar-fill {
            background: #f97316;
        }
        .timer-bar.danger .timer-bar-fill {
            background: #ef4444;
            animation: pulse 0.5s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }

        /* ===== MCQ SYSTEM ===== */
        .mcq-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.85);
            z-index: 1100;
            align-items: center;
            justify-content: center;
        }
        .mcq-modal.visible {
            display: flex;
        }
        .mcq-container {
            width: 90%;
            max-width: 600px;
            max-height: 90vh;
            overflow-y: auto;
            background: #1e1e1e;
            border-radius: 16px;
            padding: 30px;
            position: relative;
        }
        .mcq-close {
            position: absolute;
            top: 15px;
            right: 15px;
            background: rgba(255,255,255,0.1);
            border: none;
            color: #fff;
            font-size: 20px;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .mcq-close:hover {
            background: rgba(255,255,255,0.2);
        }
        .mcq-progress {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 25px;
        }
        .mcq-progress-bar {
            flex: 1;
            height: 8px;
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
            overflow: hidden;
        }
        .mcq-progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea, #764ba2);
            transition: width 0.3s ease;
        }
        .mcq-counter {
            color: #888;
            font-size: 14px;
            white-space: nowrap;
        }
        .mcq-score-badge {
            background: rgba(255,255,255,0.1);
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 12px;
            color: #888;
        }
        .mcq-question {
            font-size: 20px;
            color: #fff;
            margin-bottom: 25px;
            line-height: 1.5;
        }
        .mcq-options {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .mcq-option {
            background: rgba(255,255,255,0.05);
            border: 2px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 15px 20px;
            color: #fff;
            font-size: 16px;
            cursor: pointer;
            transition: all 0.2s ease;
            text-align: left;
        }
        .mcq-option:hover:not(.disabled) {
            background: rgba(255,255,255,0.1);
            border-color: rgba(255,255,255,0.2);
        }
        .mcq-option.selected {
            border-color: #667eea;
            background: rgba(102,126,234,0.2);
        }
        .mcq-option.correct {
            border-color: #4ade80;
            background: rgba(74,222,128,0.2);
        }
        .mcq-option.wrong {
            border-color: #f87171;
            background: rgba(248,113,113,0.2);
        }
        .mcq-option.disabled {
            cursor: default;
            opacity: 0.7;
        }
        .mcq-option-marker {
            display: inline-block;
            width: 28px;
            height: 28px;
            line-height: 28px;
            text-align: center;
            background: rgba(255,255,255,0.1);
            border-radius: 50%;
            margin-right: 12px;
            font-weight: 600;
        }
        .mcq-feedback {
            margin-top: 20px;
            padding: 15px;
            border-radius: 12px;
            font-size: 14px;
            display: none;
        }
        .mcq-feedback.correct {
            background: rgba(74,222,128,0.15);
            color: #4ade80;
            display: block;
        }
        .mcq-feedback.wrong {
            background: rgba(248,113,113,0.15);
            color: #f87171;
            display: block;
        }
        .mcq-next-btn {
            margin-top: 20px;
            width: 100%;
            padding: 15px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            border-radius: 12px;
            color: #fff;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            display: none;
        }
        .mcq-next-btn.visible {
            display: block;
        }
        .mcq-next-btn:hover {
            opacity: 0.9;
        }
        .mcq-results {
            text-align: center;
            padding: 30px 0;
        }
        .mcq-results-score {
            font-size: 64px;
            font-weight: 700;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .mcq-results-text {
            color: #888;
            font-size: 18px;
            margin: 15px 0 30px;
        }
        .mcq-results-details {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-bottom: 30px;
        }
        .mcq-results-stat {
            text-align: center;
        }
        .mcq-results-stat-value {
            font-size: 28px;
            font-weight: 600;
            color: #fff;
        }
        .mcq-results-stat-label {
            font-size: 12px;
            color: #888;
            text-transform: uppercase;
        }
        .mcq-results-stat.correct .mcq-results-stat-value { color: #4ade80; }
        .mcq-results-stat.wrong .mcq-results-stat-value { color: #f87171; }
        .mcq-restart-btn {
            padding: 15px 40px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            border-radius: 25px;
            color: #fff;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
        }
        .mcq-empty {
            text-align: center;
            padding: 40px;
            color: #888;
        }
        .mcq-empty h3 {
            color: #fff;
            margin-bottom: 15px;
        }
        .mcq-empty pre {
            background: rgba(255,255,255,0.05);
            padding: 20px;
            border-radius: 12px;
            text-align: left;
            font-size: 13px;
            overflow-x: auto;
            margin-top: 20px;
        }
        .mcq-prev-score {
            background: rgba(255,255,255,0.05);
            padding: 15px 20px;
            border-radius: 12px;
            margin-bottom: 25px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .mcq-prev-score-label {
            color: #888;
            font-size: 14px;
        }
        .mcq-prev-score-value {
            color: #fff;
            font-weight: 600;
        }

        /* Metadata Modal */
        .metadata-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .metadata-modal.visible {
            display: flex;
        }
        .metadata-modal-content {
            background: #2d2d2d;
            border-radius: 12px;
            max-width: 500px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }
        .metadata-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px 20px;
            border-bottom: 1px solid #444;
        }
        .metadata-header h3 {
            margin: 0;
            color: #fff;
            font-size: 18px;
        }
        .metadata-close {
            background: none;
            border: none;
            color: #888;
            font-size: 20px;
            cursor: pointer;
            padding: 5px;
        }
        .metadata-close:hover {
            color: #fff;
        }
        .metadata-body {
            padding: 20px;
        }
        .metadata-field {
            margin-bottom: 15px;
        }
        .metadata-field label {
            display: block;
            color: #aaa;
            font-size: 13px;
            margin-bottom: 5px;
        }
        .metadata-field input[type="text"],
        .metadata-field input[type="date"],
        .metadata-field input[type="number"],
        .metadata-field textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid #444;
            border-radius: 6px;
            background: #1e1e1e;
            color: #fff;
            font-size: 14px;
        }
        .metadata-field input:focus,
        .metadata-field textarea:focus {
            outline: none;
            border-color: #0078d4;
        }
        .metadata-field input[type="checkbox"] {
            width: 18px;
            height: 18px;
            margin-right: 8px;
            vertical-align: middle;
        }
        .metadata-field .checkbox-label {
            color: #fff;
            font-size: 15px;
            vertical-align: middle;
        }
        .metadata-footer {
            padding: 15px 20px;
            border-top: 1px solid #444;
            display: flex;
            gap: 10px;
            justify-content: flex-end;
        }
        .metadata-save {
            background: #0078d4;
            color: #fff;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
        }
        .metadata-save:hover {
            background: #006abc;
        }
        .metadata-cancel {
            background: #444;
            color: #fff;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
        }
        .metadata-cancel:hover {
            background: #555;
        }

        /* Floating annotation toolbar */
        .annotation-toolbar {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(30, 30, 30, 0.95);
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            padding: 12px 16px;
            border-radius: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            z-index: 1000;
            opacity: 0;
            visibility: hidden;
            transition: opacity 0.3s, visibility 0.3s, transform 0.3s;
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
        }
        .annotation-toolbar.visible {
            opacity: 1;
            visibility: visible;
        }
        .annotation-toolbar.minimized {
            transform: translateX(-50%) translateY(calc(100% + 10px));
        }
        
        .tool-btn, .color-btn {
            width: 40px;
            height: 40px;
            border: 2px solid transparent;
            border-radius: 10px;
            cursor: pointer;
            font-size: 18px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #444;
            color: #fff;
            transition: all 0.2s;
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
            user-select: none;
            -webkit-user-select: none;
        }
        .tool-btn:hover, .color-btn:hover {
            background: #555;
            transform: scale(1.08);
        }
        .tool-btn:active, .color-btn:active {
            transform: scale(0.95);
        }
        .tool-btn.active {
            border-color: #0066cc;
            background: #0066cc;
        }
        .color-btn {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            border: 2px solid rgba(255,255,255,0.3);
        }
        .color-btn.active {
            border-color: #fff;
            box-shadow: 0 0 0 2px #0066cc;
            transform: scale(1.1);
        }
        .tool-separator {
            width: 1px;
            height: 28px;
            background: #555;
            margin: 0 6px;
        }
        .stroke-size-container {
            display: flex;
            align-items: center;
            gap: 8px;
            background: #444;
            padding: 6px 12px;
            border-radius: 8px;
        }
        .stroke-size-preview {
            width: 28px;
            height: 28px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #333;
            border-radius: 50%;
        }
        .stroke-size-dot {
            background: #fff;
            border-radius: 50%;
            transition: width 0.1s, height 0.1s;
        }
        .stroke-size-slider {
            -webkit-appearance: none;
            appearance: none;
            width: 80px;
            height: 6px;
            background: #666;
            border-radius: 3px;
            outline: none;
            cursor: pointer;
        }
        .stroke-size-slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 18px;
            height: 18px;
            background: #fff;
            border-radius: 50%;
            cursor: pointer;
            box-shadow: 0 1px 3px rgba(0,0,0,0.3);
        }
        .stroke-size-slider::-moz-range-thumb {
            width: 18px;
            height: 18px;
            background: #fff;
            border-radius: 50%;
            cursor: pointer;
            border: none;
            box-shadow: 0 1px 3px rgba(0,0,0,0.3);
        }
        .stroke-size-label {
            color: #ccc;
            font-size: 11px;
            min-width: 24px;
            text-align: center;
        }
        .annotation-close-btn {
            background: #e53935 !important;
            margin-left: 8px;
        }
        .annotation-close-btn:hover {
            background: #c62828 !important;
        }
        
        /* Annotation mode indicator */
        .annotation-mode-badge {
            position: fixed;
            top: 15px;
            left: 50%;
            transform: translateX(-50%);
            background: #7c3aed;
            color: #fff;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 500;
            z-index: 1000;
            display: none;
            align-items: center;
            gap: 8px;
            box-shadow: 0 4px 15px rgba(124, 58, 237, 0.4);
        }
        .annotation-mode-badge.visible {
            display: flex;
        }
        .annotation-mode-badge .dot {
            width: 8px;
            height: 8px;
            background: #4ade80;
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        /* Has annotations indicator (when not in annotation mode) */
        .has-annotations-indicator {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #7c3aed;
            color: #fff;
            padding: 10px 16px;
            border-radius: 20px;
            font-size: 14px;
            cursor: pointer;
            box-shadow: 0 4px 15px rgba(124, 58, 237, 0.3);
            z-index: 100;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: transform 0.2s, background 0.2s;
        }
        .has-annotations-indicator:hover {
            background: #6d28d9;
            transform: scale(1.05);
        }
        
        /* Content wrapper needs position relative for overlay */
        .content-wrapper {
            position: relative;
        }
        .content {
            position: relative;
        }
        
        /* Mobile adjustments for annotation */
        @media (max-width: 768px) {
            .annotation-toolbar {
                bottom: 15px;
                padding: 10px 12px;
                gap: 6px;
                max-width: 95vw;
                flex-wrap: wrap;
                justify-content: center;
            }
            .tool-btn {
                width: 36px;
                height: 36px;
                font-size: 16px;
            }
            .color-btn {
                width: 24px;
                height: 24px;
            }
            .tool-separator {
                display: none;
            }
            .stroke-size-container {
                padding: 4px 8px;
            }
            .stroke-size-slider {
                width: 60px;
            }
            .stroke-size-preview {
                width: 24px;
                height: 24px;
            }
            .annotation-mode-badge {
                top: 70px;
                font-size: 12px;
                padding: 6px 12px;
            }
        }
    </style>
    <!-- MathJax for LaTeX equation rendering -->
    <script>
        MathJax = {
            tex: {
                inlineMath: [['$', '$'], ['\\(', '\\)']],
                displayMath: [['$$', '$$'], ['\\[', '\\]']],
                processEscapes: true,
                processEnvironments: true
            },
            options: {
                skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
                ignoreHtmlClass: 'callout-title|no-mathjax'
            },
            startup: {
                pageReady: () => {
                    return MathJax.startup.defaultPageReady();
                }
            }
        };
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" async></script>
    
    <!-- Mermaid.js for diagram rendering -->
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>
        mermaid.initialize({
            startOnLoad: false,
            theme: 'default',
            securityLevel: 'loose',
            flowchart: { useMaxWidth: true, htmlLabels: true }
        });
    </script>
    
    <!-- Markmap autoloader for elegant mind maps -->
    <script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@latest"></script>
    <style>
        /* Markmap Mind Map Styles */
        .markmap {
            position: relative;
            width: 100%;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            border-radius: 12px;
            margin: 20px 0;
            border: 1px solid #333;
            overflow: hidden;
        }
        .markmap > svg {
            width: 100%;
            height: 450px;
        }
        /* White text for dark background */
        .markmap text,
        .markmap .markmap-node-text,
        .markmap tspan,
        .markmap foreignObject,
        .markmap foreignObject * {
            fill: #ffffff !important;
            color: #ffffff !important;
            font-weight: 500;
            text-shadow: 0 1px 2px rgba(0,0,0,0.5);
        }
    </style>
</head>
<body>
    <button class="toggle-btn" onclick="toggleSidebar()" title="Toggle Sidebar">☰ <span class="btn-text">Menu</span></button>
    
    <button class="dock-toggle" id="dockToggle" onclick="toggleDock()" title="Toggle Sidebar (Ctrl+B)">
        <span class="arrow">◀</span>
    </button>
    
    <div class="toolbar">
        <button class="secondary theme-toggle" id="themeToggle" onclick="toggleTheme()" title="Toggle Dark/Light Theme">🌙</button>
        <button class="secondary" onclick="toggleFullscreen()" title="Toggle Fullscreen">⛶</button>
        <div class="toolbar-menu">
            <button class="toolbar-menu-btn" onclick="toggleToolbarMenu(event)" title="More actions">
                <span style="font-size: 18px; letter-spacing: 2px;">⋮</span>
            </button>
            <div class="toolbar-dropdown" id="toolbarDropdown">
                {% if is_markdown %}
                <button onclick="downloadPDF(); closeToolbarMenu();" title="Download as PDF">📥 PDF</button>
                <button onclick="downloadTopicZip(); closeToolbarMenu();" title="Download this page + all linked subpages as ZIP">📦 Topic</button>
                <button onclick="openFlashcards(); closeToolbarMenu();" title="Study with flashcards">🎴 Flashcards</button>
                <button onclick="openMcq(); closeToolbarMenu();" title="Multiple choice quiz">✅ MCQ</button>
                <button onclick="openCloze(); closeToolbarMenu();" title="Fill in the blanks">📝 Cloze</button>
                <button onclick="openMixMode(); closeToolbarMenu();" title="Mixed study mode">🎲 Mix Mode</button>
                <button onclick="openDashboard(); closeToolbarMenu();" title="Study progress dashboard">📊 Dashboard</button>
                <button onclick="openSummary(); closeToolbarMenu();" title="Quick summary view">📋 Summary</button>
                <button onclick="openExamMode(); closeToolbarMenu();" title="Timed exam simulation">📝 Exam Mode</button>
                <button onclick="openStudySettings(); closeToolbarMenu();" title="Study settings">⚙️ Settings</button>
                {% endif %}
                {% if is_markdown or is_pdf|default(false) %}
                <button onclick="openAnnotation(); closeToolbarMenu();" title="Annotate with Apple Pencil">✏️ Annotate</button>
                {% endif %}
                <button onclick="openMetadataModal(); closeToolbarMenu();" title="File Metadata">ℹ️ Info</button>
                <button onclick="syncMetadata(); closeToolbarMenu();" title="Sync metadata to index tables">🔄 Sync</button>
                <button id="calloutToggle" onclick="toggleAllCallouts(); closeToolbarMenu();" title="Collapse/Expand All Callouts">📂 Collapse All</button>
            </div>
        </div>
    </div>
    
    <!-- Metadata Modal -->
    <div id="metadataModal" class="metadata-modal">
        <div class="metadata-modal-content">
            <div class="metadata-header">
                <h3>ℹ️ File Metadata</h3>
                <button class="metadata-close" onclick="closeMetadataModal()">✕</button>
            </div>
            <div class="metadata-body">
                <div class="metadata-field">
                    <label>
                        <input type="checkbox" id="metaCompleted"> 
                        <span class="checkbox-label">✅ Completed</span>
                    </label>
                </div>
                <div class="metadata-field">
                    <label>📅 Created Date</label>
                    <input type="date" id="metaCreatedDate">
                </div>
                <div class="metadata-field">
                    <label>🔗 Source</label>
                    <input type="text" id="metaSource" placeholder="URL or reference...">
                </div>
                <div class="metadata-field">
                    <label>🔄 Revision Count</label>
                    <input type="number" id="metaRevisionCount" min="0" value="0">
                </div>
                <div class="metadata-field">
                    <label>📝 Summary (short)</label>
                    <input type="text" id="metaSummary" placeholder="Brief summary...">
                </div>
                <div class="metadata-field">
                    <label>📄 One Paragraph Summary</label>
                    <textarea id="metaOneParaSummary" rows="4" placeholder="Detailed summary..."></textarea>
                </div>
            </div>
            <div class="metadata-footer">
                <button class="metadata-save" onclick="saveMetadata()">💾 Save</button>
                <button class="metadata-cancel" onclick="closeMetadataModal()">Cancel</button>
            </div>
        </div>
    </div>
    
    <!-- Flashcard Modal -->
    <div id="flashcardModal" class="flashcard-modal">
        <button class="flashcard-close" onclick="closeFlashcards()">✕</button>
        <div class="flashcard-progress">
            <span id="flashcardCounter">1 / 10</span>
            <div class="flashcard-progress-bar">
                <div class="flashcard-progress-fill" id="flashcardProgressFill" style="width: 10%"></div>
            </div>
        </div>
        <div class="flashcard-container" id="flashcardContainer">
            <!-- Cards will be inserted here by JS -->
        </div>
        <div class="flashcard-controls" id="flashcardControls">
            <button class="flashcard-btn secondary" onclick="prevFlashcard()">⬅️ Previous</button>
            <button class="flashcard-btn primary" onclick="flipFlashcard()">🔄 Flip</button>
            <button class="flashcard-btn secondary" onclick="nextFlashcard()">Next ➡️</button>
        </div>
        <div class="flashcard-controls" id="flashcardRatingControls" style="display: none;">
            <button class="flashcard-btn wrong" onclick="rateFlashcard(false)">❌ Still Learning</button>
            <button class="flashcard-btn correct" onclick="rateFlashcard(true)">✅ Got It!</button>
        </div>
    </div>
    
    <!-- MCQ Modal -->
    <div id="mcqModal" class="mcq-modal">
        <div class="mcq-container">
            <button class="mcq-close" onclick="closeMcq()">✕</button>
            <div id="mcqPrevScore" class="mcq-prev-score" style="display: none;">
                <span class="mcq-prev-score-label">Previous Score</span>
                <span class="mcq-prev-score-value" id="mcqPrevScoreValue">-</span>
            </div>
            <div class="mcq-progress">
                <span class="mcq-counter" id="mcqCounter">1 / 10</span>
                <div class="mcq-progress-bar">
                    <div class="mcq-progress-fill" id="mcqProgressFill" style="width: 10%"></div>
                </div>
            </div>
            <div id="mcqContent">
                <!-- MCQ content will be inserted here by JS -->
            </div>
        </div>
    </div>
    
    <!-- Dashboard Modal -->
    <div id="dashboardModal" class="dashboard-modal">
        <div class="dashboard-container" id="dashboardContainer">
            <!-- Dashboard content will be inserted here by JS -->
        </div>
    </div>
    
    <!-- Settings Modal -->
    <div id="settingsModal" class="settings-modal">
        <div class="settings-container">
            <div class="settings-header">
                <h2>⚙️ Study Settings</h2>
                <button class="flashcard-close" onclick="closeStudySettings()">✕</button>
            </div>
            
            <div class="settings-section">
                <h3>Study Modes</h3>
                <div class="setting-item">
                    <div class="setting-label">
                        <span>Timed Mode</span>
                        <small>30-second countdown per card</small>
                    </div>
                    <label class="setting-toggle">
                        <input type="checkbox" id="settingTimedMode">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="setting-item">
                    <div class="setting-label">
                        <span>Confidence Rating</span>
                        <small>Rate confidence before reveal</small>
                    </div>
                    <label class="setting-toggle">
                        <input type="checkbox" id="settingConfidence">
                        <span class="slider"></span>
                    </label>
                </div>
            </div>
            
            <div class="settings-section">
                <h3>Goals</h3>
                <div class="setting-item">
                    <div class="setting-label">
                        <span>Daily Card Goal</span>
                        <small>Cards to review per day</small>
                    </div>
                    <input type="number" class="setting-input" id="settingDailyGoal" value="50" min="1" max="500">
                </div>
                <div class="setting-item">
                    <div class="setting-label">
                        <span>Timer Duration</span>
                        <small>Seconds per card</small>
                    </div>
                    <input type="number" class="setting-input" id="settingTimerSeconds" value="30" min="10" max="120">
                </div>
            </div>
            
            <div class="settings-section">
                <h3>Spaced Repetition</h3>
                <div class="setting-item">
                    <div class="setting-label">
                        <span>Algorithm</span>
                        <small>SRS (Anki-style) or Leitner (5-box)</small>
                    </div>
                    <select class="setting-input" id="settingSrsMode" style="width: auto;">
                        <option value="srs">SRS</option>
                        <option value="leitner">Leitner</option>
                    </select>
                </div>
            </div>
            
            <button class="settings-save" onclick="saveStudySettings()">Save Settings</button>
        </div>
    </div>
    
    <!-- Annotation Mode Badge -->
    <div id="annotationModeBadge" class="annotation-mode-badge">
        <span class="dot"></span>
        <span>Annotation Mode</span>
    </div>
    
    <!-- Floating Annotation Toolbar -->
    <div id="annotationToolbar" class="annotation-toolbar">
        <button class="tool-btn active" data-tool="pen" title="Pen (P)">🖊️</button>
        <button class="tool-btn" data-tool="highlighter" title="Highlighter (H)">🖍️</button>
        <button class="tool-btn" data-tool="eraser" title="Eraser (E)">🧽</button>
        <div class="tool-separator"></div>
        <button class="color-btn active" data-color="#000000" style="background:#000000" title="Black"></button>
        <button class="color-btn" data-color="#e53935" style="background:#e53935" title="Red"></button>
        <button class="color-btn" data-color="#1e88e5" style="background:#1e88e5" title="Blue"></button>
        <button class="color-btn" data-color="#43a047" style="background:#43a047" title="Green"></button>
        <button class="color-btn" data-color="#fb8c00" style="background:#fb8c00" title="Orange"></button>
        <button class="color-btn" data-color="#8e24aa" style="background:#8e24aa" title="Purple"></button>
        <div class="tool-separator"></div>
        <div class="stroke-size-container" title="Stroke Size">
            <div class="stroke-size-preview">
                <div class="stroke-size-dot" id="strokeSizeDot"></div>
            </div>
            <input type="range" id="strokeSize" class="stroke-size-slider" min="1" max="24" value="4">
            <span class="stroke-size-label" id="strokeSizeLabel">4</span>
        </div>
        <div class="tool-separator"></div>
        <button class="tool-btn" onclick="annotationUndo()" title="Undo (Ctrl+Z)">↩️</button>
        <button class="tool-btn" onclick="annotationRedo()" title="Redo (Ctrl+Y)">↪️</button>
        <button class="tool-btn" onclick="clearAnnotations()" title="Clear All">🗑️</button>
        <button class="tool-btn annotation-close-btn" onclick="exitAnnotationMode()" title="Exit (Esc)">✕</button>
    </div>
    
    <div class="sidebar hidden" id="sidebar">
        <button class="sidebar-close" onclick="toggleSidebar()">✕ Close</button>
        <div class="sidebar-header">
            <h2>{{ vault_name }}</h2>
            <div style="display: flex; gap: 8px; margin-top: 10px;">
                <button onclick="expandAllFolders()" style="flex:1; padding: 4px 8px; font-size: 11px; background: #3c3c3c; color: #ccc; border: none; border-radius: 3px; cursor: pointer;" title="Expand All">+ All</button>
                <button onclick="collapseAllFolders()" style="flex:1; padding: 4px 8px; font-size: 11px; background: #3c3c3c; color: #ccc; border: none; border-radius: 3px; cursor: pointer;" title="Collapse All">− All</button>
            </div>
            <!-- Search Box -->
            <div class="search-container">
                <div class="search-input-wrapper">
                    <span class="search-icon">🔍</span>
                    <input type="text" class="search-input" id="searchInput" placeholder="Search files..." autocomplete="off">
                    <button class="search-clear" id="searchClear" onclick="clearSearch()">✕</button>
                </div>
                <div class="search-options">
                    <label>
                        <input type="checkbox" id="searchContent">
                        Search content
                    </label>
                </div>
                <div class="search-results" id="searchResults"></div>
            </div>
            <!-- Offline Download Button -->
            <div id="offlineSection" style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #3c3c3c;">
                <button id="offlineBtn" onclick="downloadOfflineZip()" style="width: 100%; padding: 8px 12px; font-size: 12px; background: linear-gradient(135deg, #7c3aed, #5b21b6); color: white; border: none; border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; transition: all 0.2s;">
                    <span id="offlineIcon">📥</span>
                    <span id="offlineText">Download for Offline</span>
                </button>
                <div id="offlineStatus" style="font-size: 10px; color: #888; margin-top: 6px; text-align: center;">Creates ZIP with HTML files</div>
            </div>
        </div>
        <div class="sidebar-content tree-loading" id="sidebarContent">
            {{ tree|safe }}
        </div>
    </div>
    <div class="content-wrapper" id="contentWrapper">
        {% if file_path %}
        <div class="file-path-bar" id="filePathBar">
            <span class="path-icon">📄</span>
            <span class="path-text" id="pathText" data-full-path="{{ full_path }}">{{ file_path }}</span>
            <button class="copy-btn" onclick="copyFilePath()" title="Copy full path">
                <span id="copyIcon">📋</span>
                <span id="copyText">Copy</span>
            </button>
        </div>
        {% endif %}
        <div class="content" id="contentArea">
            {{ content|safe }}
        </div>
        <!-- Annotation overlay canvas - positioned on top of content -->
        <div id="annotationOverlay" class="annotation-overlay">
            <canvas id="annotationCanvas"></canvas>
        </div>
    </div>
    
    <!-- Right Sidebar - Graph View -->
    <div class="graph-sidebar collapsed" id="graphSidebar">
        <div class="graph-resize-handle" id="graphResizeHandle"></div>
        <div class="graph-sidebar-header">
            <h3>🔗 Local Graph</h3>
            <button class="graph-sidebar-close" onclick="toggleGraphSidebar()">✕</button>
        </div>
        <div class="graph-container">
            <canvas id="graphCanvas"></canvas>
        </div>
        <div class="graph-controls">
            <button onclick="zoomGraphIn()" title="Zoom In">➕</button>
            <button onclick="zoomGraphOut()" title="Zoom Out">➖</button>
            <button onclick="resetGraphView()" title="Reset View">🎯</button>
            <span class="size-buttons">
                <span style="color:#555">|</span>
                <button onclick="setGraphSize('small')" title="Small panel">S</button>
                <button onclick="setGraphSize('medium')" title="Medium panel">M</button>
                <button onclick="setGraphSize('large')" title="Large panel">L</button>
                <button onclick="setGraphSize('full')" title="Fullscreen">⛶</button>
            </span>
        </div>
    </div>
    <button class="graph-dock-toggle" id="graphDockToggle" onclick="toggleGraphSidebar()" title="Toggle Graph (Ctrl+→)">
        <span class="arrow">▶</span>
    </button>
    
    <script>
        // Check if mobile
        const isMobile = window.innerWidth <= 768;
        
        // Persist sidebar state in localStorage
        let sidebarCollapsed = localStorage.getItem('sidebarCollapsed') === 'true';
        
        // ===== RESUME FROM WHERE YOU LEFT OFF =====
        const currentFilePath = window.location.pathname;
        
        function saveScrollPosition() {
            if (!currentFilePath || currentFilePath === '/') return;
            const contentWrapper = document.getElementById('contentWrapper');
            const pdfViewer = document.getElementById('pdfViewer');
            
            if (pdfViewer) {
                localStorage.setItem('scroll_' + currentFilePath, pdfViewer.scrollTop);
            } else if (contentWrapper) {
                localStorage.setItem('scroll_' + currentFilePath, contentWrapper.scrollTop);
            }
        }
        
        function restoreScrollPosition() {
            if (!currentFilePath || currentFilePath === '/') return;
            const saved = localStorage.getItem('scroll_' + currentFilePath);
            if (!saved) return;
            
            const scrollPos = parseInt(saved, 10);
            const contentWrapper = document.getElementById('contentWrapper');
            const pdfViewer = document.getElementById('pdfViewer');
            
            // Delay to ensure content is loaded
            setTimeout(() => {
                if (pdfViewer) {
                    pdfViewer.scrollTop = scrollPos;
                } else if (contentWrapper) {
                    contentWrapper.scrollTop = scrollPos;
                }
            }, 300);
        }
        
        function saveVideoPosition(videoId) {
            const video = document.getElementById(videoId);
            if (video && video.currentTime > 0) {
                localStorage.setItem('video_' + currentFilePath, video.currentTime);
            }
        }
        
        function restoreVideoPosition(videoId) {
            const saved = localStorage.getItem('video_' + currentFilePath);
            if (!saved) return;
            
            const video = document.getElementById(videoId);
            if (video) {
                video.currentTime = parseFloat(saved);
            }
        }
        
        // Save position when leaving page
        window.addEventListener('beforeunload', saveScrollPosition);
        
        // Also save periodically while scrolling (debounced)
        let scrollSaveTimeout;
        function debouncedSaveScroll() {
            clearTimeout(scrollSaveTimeout);
            scrollSaveTimeout = setTimeout(saveScrollPosition, 500);
        }
        
        // Initialize sidebar state
        document.addEventListener('DOMContentLoaded', function() {
            const sidebar = document.getElementById('sidebar');
            const dockToggle = document.getElementById('dockToggle');
            const toggleBtn = document.querySelector('.toggle-btn');
            
            // Restore scroll position
            restoreScrollPosition();
            
            // Restore tree expansion state
            restoreTreeState();
            
            // Show tree now that state is restored (prevents flicker)
            const sidebarContent = document.getElementById('sidebarContent');
            if (sidebarContent) {
                sidebarContent.classList.remove('tree-loading');
            }
            
            // Scroll active file into view in sidebar
            setTimeout(() => {
                const activeLink = document.querySelector('.sidebar a.active');
                if (activeLink) {
                    activeLink.scrollIntoView({ block: 'center', behavior: 'instant' });
                }
            }, 50);
            
            // Add scroll listener to save position
            const contentWrapper = document.getElementById('contentWrapper');
            if (contentWrapper) {
                contentWrapper.addEventListener('scroll', debouncedSaveScroll);
            }
            
            if (isMobile) {
                // Mobile: use overlay mode
                sidebar.classList.add('hidden');
                toggleBtn.style.display = 'block';
                dockToggle.style.display = 'none';
            } else {
                // Desktop: use dock mode with persisted state
                sidebar.classList.remove('hidden');  // Remove hidden class from HTML default
                toggleBtn.style.display = 'none';
                dockToggle.style.display = 'flex';
                
                if (sidebarCollapsed) {
                    sidebar.classList.add('collapsed');
                    dockToggle.classList.add('collapsed');
                } else {
                    sidebar.classList.remove('collapsed');
                    dockToggle.classList.remove('collapsed');
                }
            }
        });
        
        // Desktop dock toggle
        function toggleDock() {
            const sidebar = document.getElementById('sidebar');
            const dockToggle = document.getElementById('dockToggle');
            
            sidebarCollapsed = !sidebarCollapsed;
            localStorage.setItem('sidebarCollapsed', sidebarCollapsed);
            console.log('toggleDock called, sidebarCollapsed:', sidebarCollapsed);
            
            if (sidebarCollapsed) {
                sidebar.classList.add('collapsed');
                dockToggle.classList.add('collapsed');
            } else {
                sidebar.classList.remove('collapsed');
                dockToggle.classList.remove('collapsed');
            }
        }
        
        // Mobile overlay toggle
        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const toggleBtn = document.querySelector('.toggle-btn');
            
            const isHidden = sidebar.classList.contains('hidden');
            
            if (isHidden) {
                sidebar.classList.remove('hidden');
                toggleBtn.classList.add('sidebar-open');
            } else {
                sidebar.classList.add('hidden');
                toggleBtn.classList.remove('sidebar-open');
            }
        }
        
        // ============================================
        // GRAPH SIDEBAR FUNCTIONS
        // ============================================
        let graphSidebarOpen = localStorage.getItem('graphSidebarOpen') === 'true';
        let graphData = { nodes: [], links: [] };
        let graphZoom = 1;
        let graphOffsetX = 0;
        let graphOffsetY = 0;
        let graphDragging = null;
        let graphPanning = false;
        let graphLastMouse = { x: 0, y: 0 };
        
        // Initialize graph sidebar state
        document.addEventListener('DOMContentLoaded', function() {
            const graphSidebar = document.getElementById('graphSidebar');
            const graphDockToggle = document.getElementById('graphDockToggle');
            
            if (graphSidebarOpen) {
                graphSidebar.classList.remove('collapsed');
                graphDockToggle.classList.add('open');
                // Delay init to ensure layout is complete
                setTimeout(() => {
                    initGraph();
                    requestAnimationFrame(() => renderGraph());
                }, 100);
            }
        });
        
        function toggleGraphSidebar() {
            const graphSidebar = document.getElementById('graphSidebar');
            const graphDockToggle = document.getElementById('graphDockToggle');
            
            graphSidebarOpen = !graphSidebarOpen;
            localStorage.setItem('graphSidebarOpen', graphSidebarOpen);
            
            if (graphSidebarOpen) {
                graphSidebar.classList.remove('collapsed');
                graphDockToggle.classList.add('open');
                // Wait for CSS transition to complete before initializing graph
                setTimeout(() => {
                    initGraph();
                    // Re-render after another frame to ensure proper sizing
                    requestAnimationFrame(() => renderGraph());
                }, 300);
            } else {
                graphSidebar.classList.add('collapsed');
                graphDockToggle.classList.remove('open');
            }
        }
        
        function initGraph() {
            // Parse wiki-links from content
            const content = document.getElementById('contentArea');
            const currentFile = '{{ file_path|default("")|replace(".md", "")|safe }}';
            const links = new Set();
            
            // Find all wiki-links in content
            const wikiLinkPattern = /\[\[([^\]|]+)(?:\|[^\]]+)?\]\]/g;
            const htmlContent = content ? content.innerHTML : '';
            
            // Also check for rendered links (href containing /view/)
            // Exclude links in navigation sections (Referenced By, Related, etc.)
            const anchorLinks = content ? content.querySelectorAll('a[href*="/view/"]') : [];
            anchorLinks.forEach(a => {
                // Skip if link is inside navigation section
                if (a.closest('.page-navigation') || a.closest('.nav-section')) {
                    return;
                }
                const href = a.getAttribute('href');
                if (href) {
                    const match = href.match(/\/view\/(.+)$/);
                    if (match) {
                        links.add(decodeURIComponent(match[1]).replace('.md', ''));
                    }
                }
            });
            
            // Build graph data
            graphData = {
                nodes: [],
                links: []
            };
            
            // Add current file as center node
            if (currentFile) {
                const currentName = currentFile.split('/').pop();
                graphData.nodes.push({
                    id: currentFile,
                    name: currentName,
                    x: 0,
                    y: 0,
                    vx: 0,
                    vy: 0,
                    isCenter: true
                });
            }
            
            // Get backlinks (files that reference this file) from the navigation section
            const backlinks = new Set();
            const parentsList = document.querySelector('.parents-list');
            if (parentsList) {
                parentsList.querySelectorAll('a[href*="/view/"]').forEach(a => {
                    const href = a.getAttribute('href');
                    if (href) {
                        const match = href.match(/\/view\/(.+)$/);
                        if (match) {
                            backlinks.add(decodeURIComponent(match[1]).replace('.md', ''));
                        }
                    }
                });
            }
            
            // Add backlinks (parent nodes) - positioned above center
            let parentAngle = Math.PI; // Start from left side going up
            const parentAngleStep = Math.PI / Math.max(backlinks.size, 1);
            const parentRadius = 100;
            
            backlinks.forEach(link => {
                const linkName = link.split('/').pop();
                graphData.nodes.push({
                    id: link,
                    name: linkName,
                    x: Math.cos(parentAngle) * parentRadius,
                    y: -Math.abs(Math.sin(parentAngle) * parentRadius) - 40, // Above center
                    vx: 0,
                    vy: 0,
                    isCenter: false,
                    isParent: true  // Mark as parent/backlink
                });
                
                if (currentFile) {
                    graphData.links.push({
                        source: link,  // Parent links TO current
                        target: currentFile
                    });
                }
                parentAngle += parentAngleStep;
            });
            
            // Add linked files (children) - positioned below center
            let angle = 0;
            const angleStep = Math.PI / Math.max(links.size, 1);
            const radius = 120;
            
            links.forEach(link => {
                // Skip if already added as backlink
                if (backlinks.has(link)) return;
                
                const linkName = link.split('/').pop();
                graphData.nodes.push({
                    id: link,
                    name: linkName,
                    x: Math.cos(angle) * radius - 60,
                    y: Math.abs(Math.sin(angle) * radius) + 40, // Below center
                    vx: 0,
                    vy: 0,
                    isCenter: false,
                    isChild: true  // Mark as child/outgoing link
                });
                
                if (currentFile) {
                    graphData.links.push({
                        source: currentFile,
                        target: link
                    });
                }
                angle += angleStep;
            });
            
            // Reset view
            graphZoom = 1;
            graphOffsetX = 0;
            graphOffsetY = 0;
            
            // Start rendering
            renderGraph();
            setupGraphInteraction();
        }
        
        function renderGraph() {
            const canvas = document.getElementById('graphCanvas');
            if (!canvas) return;
            
            const container = canvas.parentElement;
            const width = container.clientWidth || 320;
            const height = container.clientHeight || 400;
            
            // Skip render if dimensions are too small (sidebar still animating)
            if (width < 50 || height < 50) {
                setTimeout(renderGraph, 50);
                return;
            }
            
            canvas.width = width;
            canvas.height = height;
            
            const ctx = canvas.getContext('2d');
            const centerX = canvas.width / 2 + graphOffsetX;
            const centerY = canvas.height / 2 + graphOffsetY;
            
            // Clear canvas
            ctx.fillStyle = '#252526';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            
            // Apply zoom
            ctx.save();
            ctx.translate(centerX, centerY);
            ctx.scale(graphZoom, graphZoom);
            
            // Draw links
            ctx.strokeStyle = '#e06c75';
            ctx.lineWidth = 1.5 / graphZoom;
            
            graphData.links.forEach(link => {
                const source = graphData.nodes.find(n => n.id === link.source);
                const target = graphData.nodes.find(n => n.id === link.target);
                if (source && target) {
                    ctx.beginPath();
                    ctx.moveTo(source.x, source.y);
                    ctx.lineTo(target.x, target.y);
                    ctx.stroke();
                }
            });
            
            // Draw nodes
            graphData.nodes.forEach(node => {
                // Node circle
                ctx.beginPath();
                const nodeRadius = node.isCenter ? 12 : 8;
                ctx.arc(node.x, node.y, nodeRadius, 0, Math.PI * 2);
                // Colors: red=current, blue=parents (backlinks), green=children (outgoing)
                let nodeColor = '#98c379'; // default green
                if (node.isCenter) nodeColor = '#e06c75'; // red
                else if (node.isParent) nodeColor = '#61afef'; // blue for parents
                ctx.fillStyle = nodeColor;
                ctx.fill();
                
                // Node label - positioned below to avoid overlap
                ctx.font = `${11 / graphZoom}px -apple-system, BlinkMacSystemFont, sans-serif`;
                ctx.fillStyle = '#abb2bf';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'top';
                
                // Truncate long names
                let displayName = node.name;
                if (displayName.length > 20) {
                    displayName = displayName.substring(0, 18) + '...';
                }
                
                // Draw label with background for readability
                const textMetrics = ctx.measureText(displayName);
                const textY = node.y + nodeRadius + 4;
                
                ctx.fillStyle = 'rgba(37, 37, 38, 0.8)';
                ctx.fillRect(
                    node.x - textMetrics.width / 2 - 3,
                    textY - 1,
                    textMetrics.width + 6,
                    14 / graphZoom
                );
                
                let labelColor = '#abb2bf'; // default gray
                if (node.isCenter) labelColor = '#e5c07b'; // gold
                else if (node.isParent) labelColor = '#61afef'; // blue
                ctx.fillStyle = labelColor;
                ctx.fillText(displayName, node.x, textY);
            });
            
            ctx.restore();
        }
        
        function setupGraphInteraction() {
            const canvas = document.getElementById('graphCanvas');
            if (!canvas) return;
            
            // Remove old listeners
            canvas.onmousedown = null;
            canvas.onmousemove = null;
            canvas.onmouseup = null;
            canvas.onwheel = null;
            canvas.onclick = null;
            
            canvas.onmousedown = function(e) {
                const rect = canvas.getBoundingClientRect();
                const mx = (e.clientX - rect.left - canvas.width / 2 - graphOffsetX) / graphZoom;
                const my = (e.clientY - rect.top - canvas.height / 2 - graphOffsetY) / graphZoom;
                
                // Check if clicking a node
                for (const node of graphData.nodes) {
                    const dist = Math.sqrt((mx - node.x) ** 2 + (my - node.y) ** 2);
                    if (dist < 15) {
                        graphDragging = node;
                        return;
                    }
                }
                
                // Start panning
                graphPanning = true;
                graphLastMouse = { x: e.clientX, y: e.clientY };
            };
            
            canvas.onmousemove = function(e) {
                if (graphDragging) {
                    const rect = canvas.getBoundingClientRect();
                    graphDragging.x = (e.clientX - rect.left - canvas.width / 2 - graphOffsetX) / graphZoom;
                    graphDragging.y = (e.clientY - rect.top - canvas.height / 2 - graphOffsetY) / graphZoom;
                    renderGraph();
                } else if (graphPanning) {
                    graphOffsetX += e.clientX - graphLastMouse.x;
                    graphOffsetY += e.clientY - graphLastMouse.y;
                    graphLastMouse = { x: e.clientX, y: e.clientY };
                    renderGraph();
                }
            };
            
            canvas.onmouseup = function() {
                graphDragging = null;
                graphPanning = false;
            };
            
            canvas.onwheel = function(e) {
                e.preventDefault();
                const delta = e.deltaY > 0 ? 0.9 : 1.1;
                graphZoom = Math.max(0.2, Math.min(6, graphZoom * delta));
                renderGraph();
            };
            
            canvas.onclick = function(e) {
                if (graphDragging) return;
                
                const rect = canvas.getBoundingClientRect();
                const mx = (e.clientX - rect.left - canvas.width / 2 - graphOffsetX) / graphZoom;
                const my = (e.clientY - rect.top - canvas.height / 2 - graphOffsetY) / graphZoom;
                
                // Check if clicking a node
                for (const node of graphData.nodes) {
                    const dist = Math.sqrt((mx - node.x) ** 2 + (my - node.y) ** 2);
                    if (dist < 15 && !node.isCenter) {
                        // Navigate to the linked file
                        window.location.href = '/view/' + encodeURIComponent(node.id + '.md');
                        return;
                    }
                }
            };
            
            // Resize handler
            window.addEventListener('resize', function() {
                if (graphSidebarOpen) {
                    renderGraph();
                }
            });
            
            // Touch support for mobile
            let touchStartDist = 0;
            let touchStartZoom = 1;
            let lastTouchPos = null;
            
            canvas.ontouchstart = function(e) {
                if (e.touches.length === 2) {
                    // Pinch to zoom - record initial distance
                    const dx = e.touches[0].clientX - e.touches[1].clientX;
                    const dy = e.touches[0].clientY - e.touches[1].clientY;
                    touchStartDist = Math.sqrt(dx * dx + dy * dy);
                    touchStartZoom = graphZoom;
                    e.preventDefault();
                } else if (e.touches.length === 1) {
                    // Single touch - pan or drag node
                    const touch = e.touches[0];
                    const rect = canvas.getBoundingClientRect();
                    const mx = (touch.clientX - rect.left - canvas.width / 2 - graphOffsetX) / graphZoom;
                    const my = (touch.clientY - rect.top - canvas.height / 2 - graphOffsetY) / graphZoom;
                    
                    // Check if touching a node
                    for (const node of graphData.nodes) {
                        const dist = Math.sqrt((mx - node.x) ** 2 + (my - node.y) ** 2);
                        if (dist < 20) {
                            graphDragging = node;
                            e.preventDefault();
                            return;
                        }
                    }
                    
                    // Start panning
                    lastTouchPos = { x: touch.clientX, y: touch.clientY };
                    graphPanning = true;
                }
            };
            
            canvas.ontouchmove = function(e) {
                if (e.touches.length === 2) {
                    // Pinch to zoom
                    const dx = e.touches[0].clientX - e.touches[1].clientX;
                    const dy = e.touches[0].clientY - e.touches[1].clientY;
                    const dist = Math.sqrt(dx * dx + dy * dy);
                    const scale = dist / touchStartDist;
                    graphZoom = Math.max(0.2, Math.min(6, touchStartZoom * scale));
                    renderGraph();
                    e.preventDefault();
                } else if (e.touches.length === 1) {
                    const touch = e.touches[0];
                    
                    if (graphDragging) {
                        const rect = canvas.getBoundingClientRect();
                        graphDragging.x = (touch.clientX - rect.left - canvas.width / 2 - graphOffsetX) / graphZoom;
                        graphDragging.y = (touch.clientY - rect.top - canvas.height / 2 - graphOffsetY) / graphZoom;
                        renderGraph();
                        e.preventDefault();
                    } else if (graphPanning && lastTouchPos) {
                        graphOffsetX += touch.clientX - lastTouchPos.x;
                        graphOffsetY += touch.clientY - lastTouchPos.y;
                        lastTouchPos = { x: touch.clientX, y: touch.clientY };
                        renderGraph();
                        e.preventDefault();
                    }
                }
            };
            
            canvas.ontouchend = function(e) {
                // Check for tap on node (navigate)
                if (!graphDragging && !graphPanning && e.changedTouches.length === 1) {
                    const touch = e.changedTouches[0];
                    const rect = canvas.getBoundingClientRect();
                    const mx = (touch.clientX - rect.left - canvas.width / 2 - graphOffsetX) / graphZoom;
                    const my = (touch.clientY - rect.top - canvas.height / 2 - graphOffsetY) / graphZoom;
                    
                    for (const node of graphData.nodes) {
                        const dist = Math.sqrt((mx - node.x) ** 2 + (my - node.y) ** 2);
                        if (dist < 20 && !node.isCenter) {
                            window.location.href = '/view/' + encodeURIComponent(node.id + '.md');
                            return;
                        }
                    }
                }
                
                graphDragging = null;
                graphPanning = false;
                lastTouchPos = null;
            };
        }
        
        function zoomGraphIn() {
            graphZoom = Math.min(6, graphZoom * 1.2);
            renderGraph();
        }
        
        function zoomGraphOut() {
            graphZoom = Math.max(0.2, graphZoom * 0.8);
            renderGraph();
        }
        
        function resetGraphView() {
            graphZoom = 1;
            graphOffsetX = 0;
            graphOffsetY = 0;
            renderGraph();
        }
        
        function setGraphSize(size) {
            const sidebar = document.getElementById('graphSidebar');
            const toggle = document.getElementById('graphDockToggle');
            
            // Remove all size classes from both sidebar and toggle
            sidebar.classList.remove('size-small', 'size-medium', 'size-large', 'size-full');
            toggle.classList.remove('size-small', 'size-medium', 'size-large', 'size-full');
            
            // Add new size class
            if (size !== 'default') {
                sidebar.classList.add('size-' + size);
                toggle.classList.add('size-' + size);
            }
            
            // Update toggle visibility for fullscreen
            if (size === 'full') {
                toggle.style.display = 'none';
            } else {
                toggle.style.display = '';
            }
            
            // Save preference
            localStorage.setItem('graphSidebarSize', size);
            
            // Re-render graph after resize
            setTimeout(renderGraph, 100);
        }
        
        // Restore saved size on load
        document.addEventListener('DOMContentLoaded', function() {
            const savedSize = localStorage.getItem('graphSidebarSize');
            if (savedSize && savedSize !== 'default') {
                const sidebar = document.getElementById('graphSidebar');
                const toggle = document.getElementById('graphDockToggle');
                if (sidebar) {
                    sidebar.classList.add('size-' + savedSize);
                }
                if (toggle) {
                    toggle.classList.add('size-' + savedSize);
                }
            }
            
            // Restore custom width if saved
            const savedWidth = localStorage.getItem('graphSidebarWidth');
            if (savedWidth) {
                const sidebar = document.getElementById('graphSidebar');
                if (sidebar) {
                    sidebar.style.width = savedWidth + 'px';
                    sidebar.style.minWidth = savedWidth + 'px';
                }
            }
            
            // Setup resize handle
            setupGraphResize();
        });
        
        function setupGraphResize() {
            const handle = document.getElementById('graphResizeHandle');
            const sidebar = document.getElementById('graphSidebar');
            const toggle = document.getElementById('graphDockToggle');
            
            if (!handle || !sidebar) return;
            
            let isResizing = false;
            let startX = 0;
            let startWidth = 0;
            
            handle.addEventListener('mousedown', function(e) {
                isResizing = true;
                startX = e.clientX;
                startWidth = sidebar.offsetWidth;
                handle.classList.add('dragging');
                document.body.style.cursor = 'ew-resize';
                document.body.style.userSelect = 'none';
                e.preventDefault();
            });
            
            document.addEventListener('mousemove', function(e) {
                if (!isResizing) return;
                
                const diff = startX - e.clientX;
                const newWidth = Math.max(200, Math.min(800, startWidth + diff));
                
                sidebar.style.width = newWidth + 'px';
                sidebar.style.minWidth = newWidth + 'px';
                
                // Update toggle position
                if (toggle && toggle.classList.contains('open')) {
                    toggle.style.right = newWidth + 'px';
                }
                
                // Re-render graph
                renderGraph();
            });
            
            document.addEventListener('mouseup', function() {
                if (isResizing) {
                    isResizing = false;
                    handle.classList.remove('dragging');
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    
                    // Save custom width
                    const currentWidth = sidebar.offsetWidth;
                    localStorage.setItem('graphSidebarWidth', currentWidth);
                    
                    // Clear size classes since we're using custom width
                    sidebar.classList.remove('size-small', 'size-medium', 'size-large', 'size-full');
                    toggle.classList.remove('size-small', 'size-medium', 'size-large', 'size-full');
                }
            });
        }
        
        // Keyboard shortcut: Ctrl+Right Arrow to toggle graph
        document.addEventListener('keydown', function(e) {
            if (e.ctrlKey && e.key === 'ArrowRight') {
                e.preventDefault();
                toggleGraphSidebar();
            }
        });
        
        // ============================================
        // CHEATSHEET RELEVANCE & REORDERING
        // ============================================
        const isCheatsheetPage = window.location.pathname.includes('/cheatsheet/');
        let cheatsheetState = {};
        
        function initCheatsheetFeatures() {
            if (!isCheatsheetPage) return;
            
            const contentArea = document.getElementById('contentArea');
            if (!contentArea) return;
            
            // Load saved state
            const stateKey = 'cheatsheet_' + window.location.pathname;
            const savedState = localStorage.getItem(stateKey);
            if (savedState) {
                try {
                    cheatsheetState = JSON.parse(savedState);
                } catch (e) {
                    cheatsheetState = {};
                }
            }
            
            // Find all callouts (both div and details)
            const callouts = contentArea.querySelectorAll('.callout');
            if (callouts.length === 0) return;
            
            // Add toolbar at the top of content
            const firstH1 = contentArea.querySelector('h1');
            const toolbar = document.createElement('div');
            toolbar.className = 'cheatsheet-toolbar';
            toolbar.innerHTML = `
                <button onclick="sortByRelevance()" title="Sort: relevant items first">📊 Sort by Relevance</button>
                <button onclick="markAllRelevant()" title="Mark all as relevant">✅ Mark All</button>
                <button onclick="clearAllMarks()" title="Clear all marks">🔄 Reset</button>
                <button onclick="toggleShowIrrelevant()" id="toggleIrrelevantBtn" title="Show/hide irrelevant items">👁️ Hide Irrelevant</button>
                <span class="stats" id="cheatsheetStats"></span>
            `;
            if (firstH1 && firstH1.nextSibling) {
                firstH1.parentNode.insertBefore(toolbar, firstH1.nextSibling);
            } else {
                contentArea.insertBefore(toolbar, contentArea.firstChild);
            }
            
            // Wrap each callout with its preceding h2 header (if any)
            callouts.forEach((callout, index) => {
                const calloutId = getCalloutId(callout, index);
                
                // Create wrapper
                const wrapper = document.createElement('div');
                wrapper.className = 'callout-wrapper';
                wrapper.dataset.calloutId = calloutId;
                wrapper.dataset.originalIndex = index;
                wrapper.draggable = true;
                
                // Find preceding h2 header (skip over whitespace text nodes)
                let prevElement = callout.previousElementSibling;
                let headerToInclude = null;
                
                // Look for h2 that comes before this callout
                while (prevElement) {
                    if (prevElement.tagName === 'H2') {
                        headerToInclude = prevElement;
                        break;
                    } else if (prevElement.tagName === 'HR' || prevElement.classList?.contains('callout-wrapper')) {
                        // Stop if we hit a divider or another wrapper
                        break;
                    }
                    prevElement = prevElement.previousElementSibling;
                }
                
                // Insert wrapper before the header (or callout if no header)
                if (headerToInclude) {
                    headerToInclude.parentNode.insertBefore(wrapper, headerToInclude);
                    wrapper.appendChild(headerToInclude);
                } else {
                    callout.parentNode.insertBefore(wrapper, callout);
                }
                wrapper.appendChild(callout);
                
                // Add controls
                const controls = document.createElement('div');
                controls.className = 'callout-controls';
                
                const isRelevant = cheatsheetState[calloutId]?.relevant !== false;
                controls.innerHTML = `
                    <input type="checkbox" class="relevance-checkbox" 
                           ${isRelevant ? 'checked' : ''} 
                           onchange="toggleRelevance('${calloutId}', this.checked)"
                           title="Mark as relevant for exam">
                    <span class="drag-handle" title="Drag to reorder">⋮⋮</span>
                `;
                wrapper.appendChild(controls);
                
                // Apply saved state
                if (!isRelevant) {
                    wrapper.classList.add('not-relevant');
                }
                
                // Drag and drop events
                wrapper.addEventListener('dragstart', handleDragStart);
                wrapper.addEventListener('dragend', handleDragEnd);
                wrapper.addEventListener('dragover', handleDragOver);
                wrapper.addEventListener('drop', handleDrop);
            });
            
            // Apply saved order
            applySavedOrder();
            updateStats();
        }
        
        function getCalloutId(callout, index) {
            // Generate a stable ID from callout content
            const title = callout.querySelector('.callout-title');
            if (title) {
                return title.textContent.trim().substring(0, 50).replace(/[^a-zA-Z0-9]/g, '_');
            }
            return 'callout_' + index;
        }
        
        function toggleRelevance(calloutId, isRelevant) {
            const wrapper = document.querySelector(`[data-callout-id="${calloutId}"]`);
            if (wrapper) {
                if (isRelevant) {
                    wrapper.classList.remove('not-relevant');
                } else {
                    wrapper.classList.add('not-relevant');
                }
            }
            
            if (!cheatsheetState[calloutId]) {
                cheatsheetState[calloutId] = {};
            }
            cheatsheetState[calloutId].relevant = isRelevant;
            saveCheatsheetState();
            updateStats();
        }
        
        function sortByRelevance() {
            const contentArea = document.getElementById('contentArea');
            const wrappers = Array.from(contentArea.querySelectorAll('.callout-wrapper'));
            
            // Sort: relevant first, then by original index
            wrappers.sort((a, b) => {
                const aRelevant = !a.classList.contains('not-relevant');
                const bRelevant = !b.classList.contains('not-relevant');
                
                if (aRelevant && !bRelevant) return -1;
                if (!aRelevant && bRelevant) return 1;
                
                // Keep original order within each group
                return parseInt(a.dataset.originalIndex) - parseInt(b.dataset.originalIndex);
            });
            
            // Reorder in DOM
            const hr = contentArea.querySelector('hr.section-divider');
            wrappers.forEach((wrapper, newIndex) => {
                wrapper.dataset.sortIndex = newIndex;
                if (hr) {
                    contentArea.insertBefore(wrapper, hr);
                } else {
                    contentArea.appendChild(wrapper);
                }
            });
            
            saveSortOrder();
        }
        
        function markAllRelevant() {
            document.querySelectorAll('.callout-wrapper').forEach(wrapper => {
                wrapper.classList.remove('not-relevant');
                const checkbox = wrapper.querySelector('.relevance-checkbox');
                if (checkbox) checkbox.checked = true;
                
                const calloutId = wrapper.dataset.calloutId;
                if (!cheatsheetState[calloutId]) {
                    cheatsheetState[calloutId] = {};
                }
                cheatsheetState[calloutId].relevant = true;
            });
            saveCheatsheetState();
            updateStats();
        }
        
        function clearAllMarks() {
            cheatsheetState = {};
            localStorage.removeItem('cheatsheet_' + window.location.pathname);
            location.reload();
        }
        
        let showIrrelevant = true;
        function toggleShowIrrelevant() {
            showIrrelevant = !showIrrelevant;
            const btn = document.getElementById('toggleIrrelevantBtn');
            
            document.querySelectorAll('.callout-wrapper.not-relevant').forEach(wrapper => {
                wrapper.style.display = showIrrelevant ? '' : 'none';
            });
            
            btn.textContent = showIrrelevant ? '👁️ Hide Irrelevant' : '👁️ Show Irrelevant';
            btn.classList.toggle('active', !showIrrelevant);
        }
        
        // Drag and drop handlers
        let draggedItem = null;
        
        function handleDragStart(e) {
            draggedItem = this;
            this.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        }
        
        function handleDragEnd(e) {
            this.classList.remove('dragging');
            document.querySelectorAll('.callout-wrapper').forEach(w => {
                w.classList.remove('drag-over');
            });
            draggedItem = null;
            saveSortOrder();
        }
        
        function handleDragOver(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            
            if (this !== draggedItem) {
                this.classList.add('drag-over');
            }
        }
        
        function handleDrop(e) {
            e.preventDefault();
            this.classList.remove('drag-over');
            
            if (draggedItem && this !== draggedItem) {
                const parent = this.parentNode;
                const allWrappers = Array.from(parent.querySelectorAll('.callout-wrapper'));
                const draggedIndex = allWrappers.indexOf(draggedItem);
                const targetIndex = allWrappers.indexOf(this);
                
                if (draggedIndex < targetIndex) {
                    parent.insertBefore(draggedItem, this.nextSibling);
                } else {
                    parent.insertBefore(draggedItem, this);
                }
            }
        }
        
        function saveSortOrder() {
            const wrappers = document.querySelectorAll('.callout-wrapper');
            const order = Array.from(wrappers).map(w => w.dataset.calloutId);
            
            if (!cheatsheetState._order) {
                cheatsheetState._order = [];
            }
            cheatsheetState._order = order;
            saveCheatsheetState();
        }
        
        function applySavedOrder() {
            if (!cheatsheetState._order || cheatsheetState._order.length === 0) return;
            
            const contentArea = document.getElementById('contentArea');
            const wrappers = document.querySelectorAll('.callout-wrapper');
            const wrapperMap = {};
            
            wrappers.forEach(w => {
                wrapperMap[w.dataset.calloutId] = w;
            });
            
            // Find insertion point (before first hr or at end)
            const hr = contentArea.querySelector('hr.section-divider');
            
            cheatsheetState._order.forEach(id => {
                const wrapper = wrapperMap[id];
                if (wrapper) {
                    if (hr) {
                        contentArea.insertBefore(wrapper, hr);
                    } else {
                        contentArea.appendChild(wrapper);
                    }
                }
            });
        }
        
        function saveCheatsheetState() {
            const stateKey = 'cheatsheet_' + window.location.pathname;
            localStorage.setItem(stateKey, JSON.stringify(cheatsheetState));
        }
        
        function updateStats() {
            const total = document.querySelectorAll('.callout-wrapper').length;
            const relevant = document.querySelectorAll('.callout-wrapper:not(.not-relevant)').length;
            const stats = document.getElementById('cheatsheetStats');
            if (stats) {
                stats.textContent = `${relevant}/${total} marked relevant`;
            }
        }
        
        // Initialize on page load
        document.addEventListener('DOMContentLoaded', initCheatsheetFeatures);
        
        // ============================================
        // METADATA FUNCTIONS
        // ============================================
        const metadataFilePath = '{{ file_path|default("")|safe }}';
        
        function openMetadataModal() {
            const modal = document.getElementById('metadataModal');
            modal.classList.add('visible');
            loadMetadata();
        }
        
        function closeMetadataModal() {
            const modal = document.getElementById('metadataModal');
            modal.classList.remove('visible');
        }
        
        async function loadMetadata() {
            if (!metadataFilePath) return;
            
            try {
                const response = await fetch('/api/metadata/' + encodeURIComponent(metadataFilePath));
                const data = await response.json();
                
                if (data.success && data.metadata) {
                    const meta = data.metadata;
                    document.getElementById('metaCompleted').checked = meta.completed || false;
                    document.getElementById('metaCreatedDate').value = meta.created_date || '';
                    document.getElementById('metaSource').value = meta.source || '';
                    document.getElementById('metaRevisionCount').value = meta.revision_count || 0;
                    document.getElementById('metaSummary').value = meta.summary || '';
                    document.getElementById('metaOneParaSummary').value = meta.one_para_summary || '';
                }
            } catch (err) {
                console.error('Failed to load metadata:', err);
            }
        }
        
        async function saveMetadata() {
            if (!metadataFilePath) return;
            
            const metadata = {
                completed: document.getElementById('metaCompleted').checked,
                created_date: document.getElementById('metaCreatedDate').value,
                source: document.getElementById('metaSource').value,
                revision_count: parseInt(document.getElementById('metaRevisionCount').value) || 0,
                summary: document.getElementById('metaSummary').value,
                one_para_summary: document.getElementById('metaOneParaSummary').value
            };
            
            try {
                const response = await fetch('/api/metadata/' + encodeURIComponent(metadataFilePath), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(metadata)
                });
                
                const data = await response.json();
                if (data.success) {
                    closeMetadataModal();
                    // Show brief success indicator
                    const btn = document.querySelector('.metadata-save');
                    btn.textContent = '✅ Saved!';
                    setTimeout(() => { btn.textContent = '💾 Save'; }, 1500);
                } else {
                    alert('Failed to save: ' + (data.error || 'Unknown error'));
                }
            } catch (err) {
                console.error('Failed to save metadata:', err);
                alert('Failed to save metadata');
            }
        }
        
        async function downloadOfflineZip() {
            const btn = document.getElementById('offlineBtn');
            const icon = document.getElementById('offlineIcon');
            const text = document.getElementById('offlineText');
            const status = document.getElementById('offlineStatus');
            
            btn.disabled = true;
            btn.style.opacity = '0.7';
            icon.textContent = '⏳';
            text.textContent = 'Generating ZIP...';
            status.textContent = 'This may take a moment...';
            
            try {
                // Trigger download via hidden link
                const link = document.createElement('a');
                link.href = '/api/download-offline-zip';
                // Use vault name from sidebar header for filename
                const vaultName = document.querySelector('.sidebar-header h2')?.textContent?.trim() || 'Vault';
                const safeVaultName = vaultName.replace(/[^a-zA-Z0-9]/g, '_');
                link.download = `${safeVaultName}_Offline.zip`;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                
                // Reset button after a delay
                setTimeout(() => {
                    icon.textContent = '📥';
                    text.textContent = 'Download for Offline';
                    status.textContent = '✅ ZIP downloaded!';
                    status.style.color = '#4ade80';
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    
                    setTimeout(() => {
                        status.textContent = 'Creates ZIP with HTML files';
                        status.style.color = '#888';
                    }, 3000);
                }, 2000);
            } catch (error) {
                console.error('Download failed:', error);
                icon.textContent = '❌';
                text.textContent = 'Failed. Retry?';
                status.textContent = error.message;
                btn.disabled = false;
                btn.style.opacity = '1';
            }
        }
        
        async function syncMetadata() {
            const btn = event.target.closest('button');
            const originalText = btn.innerHTML;
            btn.innerHTML = '⏳ <span class="btn-text">Syncing...</span>';
            btn.disabled = true;
            
            try {
                const response = await fetch('/api/sync-metadata', { method: 'POST' });
                const data = await response.json();
                
                if (data.success) {
                    // Show cheatsheet info if available
                    let statusMsg = '✅ Synced!';
                    let parts = [];
                    if (data.cheatsheet && data.cheatsheet.success) {
                        parts.push(`📝 ${data.cheatsheet.definitions_found} definitions`);
                    }
                    if (data.memory_tips && data.memory_tips.success) {
                        parts.push(`🧠 ${data.memory_tips.tips_found} tips`);
                    }
                    if (parts.length > 0) {
                        statusMsg = `✅ Synced! (${parts.join(', ')})`;
                    }
                    btn.innerHTML = `<span class="btn-text">${statusMsg}</span>`;
                    setTimeout(() => { 
                        btn.innerHTML = originalText;
                        btn.disabled = false;
                        // Reload page to show updated values
                        location.reload();
                    }, 1500);
                } else {
                    btn.innerHTML = '❌ <span class="btn-text">Failed</span>';
                    alert('Sync failed: ' + (data.error || 'Unknown error'));
                    setTimeout(() => { 
                        btn.innerHTML = originalText;
                        btn.disabled = false;
                    }, 2000);
                }
            } catch (err) {
                console.error('Sync failed:', err);
                btn.innerHTML = '❌ <span class="btn-text">Error</span>';
                alert('Sync failed: ' + err.message);
                setTimeout(() => { 
                    btn.innerHTML = originalText;
                    btn.disabled = false;
                }, 2000);
            }
        }
        
        // Close modal on backdrop click
        document.getElementById('metadataModal')?.addEventListener('click', function(e) {
            if (e.target === this) closeMetadataModal();
        });
        
        // ===== FLASHCARD SYSTEM =====
        let flashcards = [];
        let currentCardIndex = 0;
        let flashcardStats = { correct: 0, wrong: 0 };
        
        async function openFlashcards() {
            const filePath = '{{ file_path|default("")|safe }}';
            if (!filePath) {
                alert('No file loaded');
                return;
            }
            
            try {
                const response = await fetch('/api/flashcards/' + encodeURIComponent(filePath));
                const data = await response.json();
                
                if (data.success && data.flashcards && data.flashcards.length > 0) {
                    flashcards = data.flashcards;
                    currentCardIndex = 0;
                    flashcardStats = { correct: 0, wrong: 0 };
                    currentStudyMode = 'flashcard';
                    renderFlashcard();
                    document.getElementById('flashcardModal').classList.add('visible');
                } else {
                    showEmptyFlashcardState();
                    document.getElementById('flashcardModal').classList.add('visible');
                }
            } catch (err) {
                console.error('Failed to load flashcards:', err);
                alert('Failed to load flashcards: ' + err.message);
            }
        }
        
        function showEmptyFlashcardState() {
            const container = document.getElementById('flashcardContainer');
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>🎴 No Flashcards Found</h3>
                    <p>Add a flashcard section at the end of your MD file to create flashcards.</p>
                    <pre>## Flashcards

Q: What is the capital of France?
A: Paris

Q: What does HTTP stand for?
A: HyperText Transfer Protocol

Q: Name the 4 TCP/IP layers
A: LITA - Link, Internet, Transport, Application</pre>
                    <p style="margin-top: 20px; font-size: 13px;">
                        You can also use <code>&gt; [!flashcard]</code> callouts anywhere in your notes!
                    </p>
                </div>
            `;
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }
        
        function renderFlashcard() {
            const card = flashcards[currentCardIndex];
            const container = document.getElementById('flashcardContainer');
            
            container.innerHTML = `
                <div class="flashcard" id="currentFlashcard" onclick="flipFlashcard()">
                    <div class="flashcard-face flashcard-front">
                        <div class="flashcard-label">Question</div>
                        <div class="flashcard-content">${parseMarkdown(card.question)}</div>
                        <div class="flashcard-hint">Click or tap to reveal answer</div>
                    </div>
                    <div class="flashcard-face flashcard-back">
                        <div class="flashcard-label">Answer</div>
                        <div class="flashcard-content">${parseMarkdown(card.answer)}</div>
                        <div class="flashcard-hint">Rate your response below</div>
                    </div>
                </div>
            `;
            
            // Update progress
            const counter = document.getElementById('flashcardCounter');
            const fill = document.getElementById('flashcardProgressFill');
            counter.textContent = `${currentCardIndex + 1} / ${flashcards.length}`;
            fill.style.width = `${((currentCardIndex + 1) / flashcards.length) * 100}%`;
            
            // Show controls
            document.getElementById('flashcardControls').style.display = 'flex';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'flex';
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function parseMarkdown(text) {
            // First escape HTML for safety
            let html = escapeHtml(text);
            // Parse markdown formatting
            html = html.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');  // **bold**
            html = html.replace(/\\*([^*]+?)\\*/g, '<em>$1</em>');  // *italic*
            html = html.replace(/`([^`]+?)`/g, '<code>$1</code>');  // `code`
            html = html.replace(/\\n/g, '<br>');  // line breaks
            return html;
        }
        
        function flipFlashcard() {
            const card = document.getElementById('currentFlashcard');
            if (card) {
                card.classList.toggle('flipped');
                // Show rating controls when flipped to back
                if (card.classList.contains('flipped')) {
                    document.getElementById('flashcardControls').style.display = 'none';
                    document.getElementById('flashcardRatingControls').style.display = 'flex';
                } else {
                    document.getElementById('flashcardControls').style.display = 'flex';
                    document.getElementById('flashcardRatingControls').style.display = 'none';
                }
            }
        }
        
        function nextFlashcard() {
            if (currentCardIndex < flashcards.length - 1) {
                currentCardIndex++;
                renderFlashcard();
            } else {
                showFlashcardSummary();
            }
        }
        
        function prevFlashcard() {
            if (currentCardIndex > 0) {
                currentCardIndex--;
                renderFlashcard();
            }
        }
        
        function rateFlashcard(correct) {
            // Route to the correct rating function based on current mode
            if (currentStudyMode === 'cloze') {
                rateClozeCard(correct);
                return;
            }
            if (currentStudyMode === 'mix') {
                rateMixCard(correct);
                return;
            }
            
            // Default: flashcard mode
            if (correct) {
                flashcardStats.correct++;
            } else {
                flashcardStats.wrong++;
            }
            
            // Add shuffle animation
            const card = document.getElementById('currentFlashcard');
            if (card) {
                card.classList.add('shuffling');
                setTimeout(() => {
                    nextFlashcard();
                }, 300);
            } else {
                nextFlashcard();
            }
        }
        
        function showFlashcardSummary() {
            const container = document.getElementById('flashcardContainer');
            const total = flashcardStats.correct + flashcardStats.wrong;
            const percentage = total > 0 ? Math.round((flashcardStats.correct / total) * 100) : 0;
            
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>🎉 Session Complete!</h3>
                    <div class="flashcard-stats">
                        <div class="flashcard-stat correct">
                            <div class="flashcard-stat-value">${flashcardStats.correct}</div>
                            <div class="flashcard-stat-label">Correct</div>
                        </div>
                        <div class="flashcard-stat">
                            <div class="flashcard-stat-value">${percentage}%</div>
                            <div class="flashcard-stat-label">Score</div>
                        </div>
                        <div class="flashcard-stat wrong">
                            <div class="flashcard-stat-value">${flashcardStats.wrong}</div>
                            <div class="flashcard-stat-label">Learning</div>
                        </div>
                    </div>
                    <div style="margin-top: 30px;">
                        <button class="flashcard-btn primary" onclick="restartFlashcards()">🔄 Study Again</button>
                    </div>
                </div>
            `;
            
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
        }
        
        function restartFlashcards() {
            currentCardIndex = 0;
            flashcardStats = { correct: 0, wrong: 0 };
            // Shuffle cards for variety
            flashcards = shuffleArray([...flashcards]);
            renderFlashcard();
        }
        
        function shuffleArray(array) {
            for (let i = array.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [array[i], array[j]] = [array[j], array[i]];
            }
            return array;
        }
        
        function closeFlashcards() {
            document.getElementById('flashcardModal').classList.remove('visible');
        }
        
        // Close flashcard modal on backdrop click
        document.getElementById('flashcardModal')?.addEventListener('click', function(e) {
            if (e.target === this) closeFlashcards();
        });
        
        // Keyboard shortcuts for flashcards
        document.addEventListener('keydown', function(e) {
            const modal = document.getElementById('flashcardModal');
            if (!modal || !modal.classList.contains('visible')) return;
            
            switch(e.key) {
                case 'Escape':
                    closeFlashcards();
                    break;
                case ' ':
                case 'Enter':
                    e.preventDefault();
                    flipFlashcard();
                    break;
                case 'ArrowRight':
                case 'l':
                    nextFlashcard();
                    break;
                case 'ArrowLeft':
                case 'h':
                    prevFlashcard();
                    break;
                case '1':
                case 'x':
                    const card = document.getElementById('currentFlashcard');
                    if (card && card.classList.contains('flipped')) {
                        rateFlashcard(false);
                    }
                    break;
                case '2':
                case 'c':
                    const card2 = document.getElementById('currentFlashcard');
                    if (card2 && card2.classList.contains('flipped')) {
                        rateFlashcard(true);
                    }
                    break;
            }
        });
        
        // ===== MCQ SYSTEM =====
        let mcqQuestions = [];
        let currentMcqIndex = 0;
        let mcqScore = { correct: 0, wrong: 0 };
        let mcqAnswered = false;
        let mcqPreviousScore = null;
        
        async function openMcq() {
            const filepath = '{{ file_path|default("")|safe }}';
            if (!filepath) {
                showMcqError('No file selected', 'Open a markdown file first to access MCQs.');
                document.getElementById('mcqModal').classList.add('visible');
                return;
            }
            
            try {
                const response = await fetch('/api/mcq/' + encodeURIComponent(filepath));
                const data = await response.json();
                
                if (!data.success) {
                    showMcqError('Error loading MCQs', data.error);
                    document.getElementById('mcqModal').classList.add('visible');
                    return;
                }
                
                if (data.mcqs.length === 0) {
                    showMcqEmpty();
                    document.getElementById('mcqModal').classList.add('visible');
                    return;
                }
                
                mcqQuestions = data.mcqs;
                currentMcqIndex = 0;
                mcqScore = { correct: 0, wrong: 0 };
                mcqPreviousScore = data.score;
                
                // Show previous score if exists
                const prevScoreEl = document.getElementById('mcqPrevScore');
                if (mcqPreviousScore && mcqPreviousScore.percentage !== undefined) {
                    document.getElementById('mcqPrevScoreValue').textContent = 
                        mcqPreviousScore.percentage + '% (' + mcqPreviousScore.correct + '/' + mcqPreviousScore.total + ')';
                    prevScoreEl.style.display = 'flex';
                } else {
                    prevScoreEl.style.display = 'none';
                }
                
                renderMcq();
                document.getElementById('mcqModal').classList.add('visible');
                
            } catch (err) {
                alert('Error: ' + err.message);
            }
        }
        
        function showMcqError(title, message) {
            document.getElementById('mcqContent').innerHTML = `
                <div class="mcq-empty">
                    <h3>⚠️ ${escapeHtml(title)}</h3>
                    <p>${escapeHtml(message)}</p>
                </div>
            `;
            document.getElementById('mcqPrevScore').style.display = 'none';
            document.querySelector('.mcq-progress').style.display = 'none';
        }
        
        function showMcqEmpty() {
            document.getElementById('mcqContent').innerHTML = `
                <div class="mcq-empty">
                    <h3>📝 No MCQs Found</h3>
                    <p>Add MCQs at the bottom of this file using the syntax:</p>
                    <pre>## MCQ

Q: What is the capital of France?
- [ ] London
- [ ] Berlin
- [x] Paris
- [ ] Madrid

Q: Which port does HTTP use?
- [x] 80
- [ ] 443
- [ ] 22
- [ ] 21</pre>
                </div>
            `;
            document.getElementById('mcqPrevScore').style.display = 'none';
            document.querySelector('.mcq-progress').style.display = 'none';
        }
        
        function renderMcq() {
            const q = mcqQuestions[currentMcqIndex];
            const container = document.getElementById('mcqContent');
            mcqAnswered = false;
            
            // Update progress
            document.getElementById('mcqCounter').textContent = (currentMcqIndex + 1) + ' / ' + mcqQuestions.length;
            document.getElementById('mcqProgressFill').style.width = ((currentMcqIndex + 1) / mcqQuestions.length * 100) + '%';
            document.querySelector('.mcq-progress').style.display = 'flex';
            
            const optionLetters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
            
            let optionsHtml = q.options.map((opt, idx) => `
                <button class="mcq-option" onclick="selectMcqOption(${idx})" data-index="${idx}">
                    <span class="mcq-option-marker">${optionLetters[idx]}</span>
                    ${escapeHtml(opt)}
                </button>
            `).join('');
            
            container.innerHTML = `
                <div class="mcq-question">${parseMarkdown(q.question)}</div>
                <div class="mcq-options">${optionsHtml}</div>
                <div class="mcq-feedback" id="mcqFeedback"></div>
                <button class="mcq-next-btn" id="mcqNextBtn" onclick="nextMcq()">
                    ${currentMcqIndex < mcqQuestions.length - 1 ? 'Next Question →' : 'See Results'}
                </button>
            `;
        }
        
        function selectMcqOption(selectedIdx) {
            if (mcqAnswered) return;
            mcqAnswered = true;
            
            const q = mcqQuestions[currentMcqIndex];
            const isCorrect = selectedIdx === q.correct;
            
            if (isCorrect) {
                mcqScore.correct++;
            } else {
                mcqScore.wrong++;
            }
            
            // Update option styles
            const options = document.querySelectorAll('.mcq-option');
            options.forEach((opt, idx) => {
                opt.classList.add('disabled');
                if (idx === q.correct) {
                    opt.classList.add('correct');
                } else if (idx === selectedIdx && !isCorrect) {
                    opt.classList.add('wrong');
                }
            });
            
            // Show feedback
            const feedback = document.getElementById('mcqFeedback');
            if (isCorrect) {
                feedback.className = 'mcq-feedback correct';
                feedback.textContent = '✓ Correct!';
            } else {
                feedback.className = 'mcq-feedback wrong';
                const correctLetter = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'][q.correct];
                feedback.textContent = '✗ Incorrect. The correct answer is ' + correctLetter + '.';
            }
            
            // Show next button
            document.getElementById('mcqNextBtn').classList.add('visible');
        }
        
        function nextMcq() {
            if (currentMcqIndex < mcqQuestions.length - 1) {
                currentMcqIndex++;
                renderMcq();
            } else {
                showMcqResults();
            }
        }
        
        async function showMcqResults() {
            const total = mcqQuestions.length;
            const percentage = Math.round((mcqScore.correct / total) * 100);
            
            // Save score
            const filepath = '{{ file_path|default("")|safe }}';
            try {
                await fetch('/api/mcq-score/' + encodeURIComponent(filepath), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        correct: mcqScore.correct,
                        total: total,
                        percentage: percentage,
                        last_attempt: new Date().toISOString()
                    })
                });
            } catch (err) {
                console.error('Failed to save score:', err);
            }
            
            let message = '';
            if (percentage >= 90) message = '🌟 Excellent!';
            else if (percentage >= 70) message = '👍 Good job!';
            else if (percentage >= 50) message = '📚 Keep studying!';
            else message = '💪 Try again!';
            
            document.getElementById('mcqContent').innerHTML = `
                <div class="mcq-results">
                    <div class="mcq-results-score">${percentage}%</div>
                    <div class="mcq-results-text">${message}</div>
                    <div class="mcq-results-details">
                        <div class="mcq-results-stat correct">
                            <div class="mcq-results-stat-value">${mcqScore.correct}</div>
                            <div class="mcq-results-stat-label">Correct</div>
                        </div>
                        <div class="mcq-results-stat wrong">
                            <div class="mcq-results-stat-value">${mcqScore.wrong}</div>
                            <div class="mcq-results-stat-label">Wrong</div>
                        </div>
                    </div>
                    <button class="mcq-restart-btn" onclick="restartMcq()">🔄 Try Again</button>
                </div>
            `;
            document.querySelector('.mcq-progress').style.display = 'none';
            document.getElementById('mcqPrevScore').style.display = 'none';
        }
        
        function restartMcq() {
            currentMcqIndex = 0;
            mcqScore = { correct: 0, wrong: 0 };
            renderMcq();
        }
        
        function closeMcq() {
            document.getElementById('mcqModal').classList.remove('visible');
        }
        
        // Close MCQ modal on backdrop click
        document.getElementById('mcqModal')?.addEventListener('click', function(e) {
            if (e.target === this) closeMcq();
        });
        
        // Keyboard shortcuts for MCQ
        document.addEventListener('keydown', function(e) {
            const modal = document.getElementById('mcqModal');
            if (!modal || !modal.classList.contains('visible')) return;
            
            switch(e.key) {
                case 'Escape':
                    closeMcq();
                    break;
                case '1':
                case 'a':
                case 'A':
                    if (!mcqAnswered) selectMcqOption(0);
                    break;
                case '2':
                case 'b':
                case 'B':
                    if (!mcqAnswered) selectMcqOption(1);
                    break;
                case '3':
                case 'c':
                case 'C':
                    if (!mcqAnswered) selectMcqOption(2);
                    break;
                case '4':
                case 'd':
                case 'D':
                    if (!mcqAnswered) selectMcqOption(3);
                    break;
                case 'Enter':
                case ' ':
                    if (mcqAnswered) {
                        e.preventDefault();
                        nextMcq();
                    }
                    break;
            }
        });
        
        // ===== CLOZE DELETION SYSTEM =====
        let clozeCards = [];
        let currentClozeIndex = 0;
        let clozeStats = { correct: 0, wrong: 0 };
        let currentStudyMode = 'flashcard'; // 'flashcard', 'cloze', 'mix'
        
        async function openCloze() {
            const filePath = '{{ file_path|default("")|safe }}';
            if (!filePath) {
                alert('No file loaded');
                return;
            }
            
            try {
                const response = await fetch('/api/cloze/' + encodeURIComponent(filePath));
                const data = await response.json();
                
                if (data.success && data.cloze && data.cloze.length > 0) {
                    clozeCards = data.cloze;
                    currentClozeIndex = 0;
                    clozeStats = { correct: 0, wrong: 0 };
                    currentStudyMode = 'cloze';
                    renderClozeCard();
                    document.getElementById('flashcardModal').classList.add('visible');
                } else {
                    showEmptyClozeState();
                    document.getElementById('flashcardModal').classList.add('visible');
                }
            } catch (err) {
                console.error('Failed to load cloze:', err);
            }
        }
        
        function showEmptyClozeState() {
            const container = document.getElementById('flashcardContainer');
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>📝 No Cloze Deletions Found</h3>
                    <p>Add a cloze section to create fill-in-the-blank cards:</p>
                    <pre>## Cloze

TCP uses {<!-- -->{sequence numbers}} to track packets.
The OSI model has {<!-- -->{c1::7}} layers.
HTTP is a ==stateless== protocol.</pre>
                    <p style="margin-top: 16px; font-size: 13px; color: #6b7280;">
                        Syntax: <code>{<!-- -->{text}}</code>, <code>{<!-- -->{c1::text}}</code>, or <code>==highlighted==</code>
                    </p>
                </div>
            `;
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }
        
        function processClozeText(text, showAnswers = false) {
            // Convert {% raw %}{{answer}}{% endraw %} or {% raw %}{{answer|hint}}{% endraw %} to blanks or revealed answers
            const pipe = String.fromCharCode(124); // | character
            return text.replace(/\{\{([^}]+)\}\}/g, (match, content) => {
                let answer = content;
                let hint = '...';
                const hintMarker = pipe + 'hint:';
                if (content.includes(hintMarker)) {
                    [answer, hint] = content.split(hintMarker);
                    answer = answer.trim();
                    hint = hint.trim();
                } else if (content.includes(pipe)) {
                    [answer, hint] = content.split(pipe);
                    answer = answer.trim();
                    hint = hint.trim();
                }
                if (showAnswers) {
                    return '<span class="cloze-blank revealed">' + answer + '</span>';
                } else {
                    return '<span class="cloze-blank">___</span>';
                }
            });
        }
        
        function renderClozeCard() {
            const card = clozeCards[currentClozeIndex];
            const container = document.getElementById('flashcardContainer');
            
            // Process the question to show blanks, and revealed version for answer
            const questionWithBlanks = processClozeText(card.question, false);
            const questionWithAnswers = processClozeText(card.question, true);
            
            container.innerHTML = `
                <div class="flashcard" id="currentFlashcard">
                    <div class="flashcard-face flashcard-front">
                        <div class="flashcard-label">
                            <span class="card-type-badge cloze">📝 Cloze</span>
                        </div>
                        <div class="cloze-card">${questionWithBlanks}</div>
                        <div class="flashcard-hint">Fill in the blank, then click to reveal</div>
                    </div>
                    <div class="flashcard-face flashcard-back">
                        <div class="flashcard-label">Answer</div>
                        <div class="cloze-card" style="font-size: 1.2em;">${questionWithAnswers}</div>
                    </div>
                </div>
            `;
            
            // Update progress
            const counter = document.getElementById('flashcardCounter');
            const fill = document.getElementById('flashcardProgressFill');
            counter.textContent = `${currentClozeIndex + 1} / ${clozeCards.length}`;
            fill.style.width = `${((currentClozeIndex + 1) / clozeCards.length) * 100}%`;
            
            document.getElementById('flashcardControls').style.display = 'flex';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'flex';
            
            // Add click to flip
            document.getElementById('currentFlashcard').onclick = function() {
                this.classList.toggle('flipped');
                if (this.classList.contains('flipped')) {
                    document.getElementById('flashcardControls').style.display = 'none';
                    document.getElementById('flashcardRatingControls').style.display = 'flex';
                } else {
                    document.getElementById('flashcardControls').style.display = 'flex';
                    document.getElementById('flashcardRatingControls').style.display = 'none';
                }
            };
        }
        
        function rateClozeCard(correct) {
            if (correct) {
                clozeStats.correct++;
            } else {
                clozeStats.wrong++;
            }
            
            const card = document.getElementById('currentFlashcard');
            if (card) {
                card.classList.add('shuffling');
                setTimeout(() => {
                    nextClozeCard();
                }, 300);
            } else {
                nextClozeCard();
            }
        }
        
        function nextClozeCard() {
            if (currentClozeIndex < clozeCards.length - 1) {
                currentClozeIndex++;
                renderClozeCard();
            } else {
                showClozeSummary();
            }
        }
        
        function showClozeSummary() {
            const container = document.getElementById('flashcardContainer');
            const total = clozeStats.correct + clozeStats.wrong;
            const percentage = total > 0 ? Math.round((clozeStats.correct / total) * 100) : 0;
            
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>🎉 Cloze Session Complete!</h3>
                    <div class="flashcard-stats">
                        <div class="flashcard-stat correct">
                            <div class="flashcard-stat-value">${clozeStats.correct}</div>
                            <div class="flashcard-stat-label">Correct</div>
                        </div>
                        <div class="flashcard-stat">
                            <div class="flashcard-stat-value">${percentage}%</div>
                            <div class="flashcard-stat-label">Score</div>
                        </div>
                        <div class="flashcard-stat wrong">
                            <div class="flashcard-stat-value">${clozeStats.wrong}</div>
                            <div class="flashcard-stat-label">Learning</div>
                        </div>
                    </div>
                    <div style="margin-top: 30px;">
                        <button class="flashcard-btn primary" onclick="restartCloze()">🔄 Study Again</button>
                    </div>
                </div>
            `;
            
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }
        
        function restartCloze() {
            currentClozeIndex = 0;
            clozeStats = { correct: 0, wrong: 0 };
            clozeCards = shuffleArray([...clozeCards]);
            renderClozeCard();
            document.querySelector('.flashcard-progress').style.display = 'flex';
        }
        
        // ===== SRS RATING SYSTEM =====
        let currentSrsMode = 'srs';
        let srsFilePath = '{{ file_path|default("")|safe }}';
        
        async function rateSrs(cardId, cardType, rating, filepath = null) {
            try {
                const targetPath = filepath || srsFilePath;
                if (!targetPath) {
                    console.warn('No SRS path available');
                    return;
                }
                const response = await fetch('/api/srs/' + encodeURIComponent(targetPath), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cardId, cardType, rating })
                });
                const data = await response.json();
                if (data.success) {
                    console.log('SRS updated:', data);
                }
            } catch (err) {
                console.error('Failed to update SRS:', err);
            }
        }
        
        // Track wrong cards in current session for review
        let sessionWrongCards = [];
        
        function formatInterval(days) {
            if (days < 0.01) return '< 10m';
            if (days < 1) return Math.round(days * 24) + 'h';
            if (days < 7) return Math.round(days) + 'd';
            if (days < 30) return Math.round(days / 7) + 'w';
            return Math.round(days / 30) + 'mo';
        }
        
        // ===== STUDY DASHBOARD =====
        let dashboardData = null;
        let currentDashboardView = 'daily';
        
        async function openDashboard() {
            try {
                const response = await fetch('/api/study/dashboard');
                const data = await response.json();
                
                if (!data.success) {
                    console.error('Dashboard error:', data.error);
                    return;
                }
                
                dashboardData = data.dashboard;
                renderDashboard();
                document.getElementById('dashboardModal').classList.add('visible');
            } catch (err) {
                console.error('Failed to load dashboard:', err);
            }
        }
        
        function renderDashboard() {
            const d = dashboardData;
            const container = document.getElementById('dashboardContainer');
            
            // Build heatmap
            let heatmapHtml = '';
            const dates = Object.keys(d.heatmap).sort().slice(-28);
            for (const date of dates) {
                const count = d.heatmap[date];
                let level = 0;
                if (count > 0) level = 1;
                if (count >= 10) level = 2;
                if (count >= 25) level = 3;
                if (count >= 50) level = 4;
                heatmapHtml += `<div class="heatmap-day level-${level}" title="${date}: ${count} cards"></div>`;
            }
            
            const progressPct = Math.min(100, (d.today.reviewed / d.today.goal) * 100);
            
            // Build chart for current view
            const viewData = d[currentDashboardView] || d.daily;
            const maxCards = Math.max(...viewData.map(v => v.cards), 1);
            
            let chartHtml = viewData.map(item => {
                const height = (item.cards / maxCards) * 100;
                const correctPct = item.cards > 0 ? Math.round((item.correct / item.cards) * 100) : 0;
                return `
                    <div class="chart-bar-container">
                        <div class="chart-bar" style="height: ${Math.max(height, 2)}%;" title="${item.cards} cards (${correctPct}% correct)">
                            <span class="chart-bar-value">${item.cards || ''}</span>
                        </div>
                        <div class="chart-bar-label">${item.label}</div>
                    </div>
                `;
            }).join('');
            
            // Calculate totals for current view
            const viewTotals = viewData.reduce((acc, item) => ({
                cards: acc.cards + item.cards,
                correct: acc.correct + item.correct,
                wrong: acc.wrong + item.wrong
            }), { cards: 0, correct: 0, wrong: 0 });
            
            const viewLabels = {
                hourly: 'Today by Hour',
                daily: 'Last 7 Days',
                weekly: 'Last 4 Weeks',
                monthly: 'Last 6 Months'
            };
            
            container.innerHTML = `
                <div class="dashboard-header">
                    <h2>📊 Study Dashboard</h2>
                    <button class="flashcard-close" onclick="closeDashboard()">✕</button>
                </div>
                
                <div class="dashboard-grid">
                    <div class="dashboard-stat streak">
                        <div class="dashboard-stat-value">🔥 ${d.streak}</div>
                        <div class="dashboard-stat-label">Day Streak</div>
                    </div>
                    <div class="dashboard-stat due">
                        <div class="dashboard-stat-value">${d.dueCount}</div>
                        <div class="dashboard-stat-label">Cards Due</div>
                    </div>
                    <div class="dashboard-stat mastery">
                        <div class="dashboard-stat-value">${d.masteryPercent}%</div>
                        <div class="dashboard-stat-label">Mastery</div>
                    </div>
                    <div class="dashboard-stat weak">
                        <div class="dashboard-stat-value">${d.weakCards}</div>
                        <div class="dashboard-stat-label">Weak Cards</div>
                    </div>
                </div>
                
                <div class="dashboard-progress">
                    <h3>Today's Progress</h3>
                    <div class="progress-bar-container">
                        <div class="progress-bar-fill" style="width: ${progressPct}%"></div>
                    </div>
                    <div class="progress-label">
                        <span>${d.today.reviewed} reviewed (${d.today.correct} ✓ / ${d.today.wrong} ✗)</span>
                        <span>Goal: ${d.today.goal}</span>
                    </div>
                </div>
                
                <div class="dashboard-chart-section">
                    <div class="dashboard-view-tabs">
                        <button class="view-tab ${currentDashboardView === 'hourly' ? 'active' : ''}" onclick="switchDashboardView('hourly')">🕐 Hourly</button>
                        <button class="view-tab ${currentDashboardView === 'daily' ? 'active' : ''}" onclick="switchDashboardView('daily')">📅 Daily</button>
                        <button class="view-tab ${currentDashboardView === 'weekly' ? 'active' : ''}" onclick="switchDashboardView('weekly')">📆 Weekly</button>
                        <button class="view-tab ${currentDashboardView === 'monthly' ? 'active' : ''}" onclick="switchDashboardView('monthly')">🗓️ Monthly</button>
                    </div>
                    <div class="chart-summary">
                        <span><strong>${viewTotals.cards}</strong> cards</span>
                        <span>✓ ${viewTotals.correct}</span>
                        <span>✗ ${viewTotals.wrong}</span>
                    </div>
                    <div class="dashboard-chart">
                        ${chartHtml}
                    </div>
                </div>
                
                <div class="heatmap-container">
                    <h3>Activity Heatmap</h3>
                    <div class="heatmap">${heatmapHtml}</div>
                </div>
                
                <div style="margin-top: 24px; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
                    <button class="flashcard-btn primary" onclick="closeDashboard(); startFocusModeReal();">
                        🎯 Focus on Weak Cards (${d.weakCards})
                    </button>
                </div>
                <div style="margin-top: 12px; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
                    <button class="flashcard-btn secondary" onclick="closeDashboard(); startWeakCardsMixMode(true);" ${d.weakCards === 0 ? 'disabled style="opacity: 0.5;"' : ''} title="Review weak cards from this file only">
                        📄 Mistakes (This File)
                    </button>
                    <button class="flashcard-btn secondary" onclick="closeDashboard(); startWeakCardsMixMode(false);" ${d.weakCards === 0 ? 'disabled style="opacity: 0.5;"' : ''} title="Review weak cards from all files">
                        📚 Mistakes (Whole Vault)
                    </button>
                </div>
            `;
        }
        
        function switchDashboardView(view) {
            currentDashboardView = view;
            renderDashboard();
        }
        
        function closeDashboard() {
            document.getElementById('dashboardModal').classList.remove('visible');
        }
        
        // ===== FOCUS MODE (WEAK CARDS) =====
        async function startFocusMode() {
            try {
                const response = await fetch('/api/study/weak-cards');
                const data = await response.json();
                
                if (!data.success || data.weakCards.length === 0) {
                    showEmptyFocusState();
                    document.getElementById('flashcardModal').classList.add('visible');
                    return;
                }
                
                // TODO: Load and display weak cards from multiple files
                showEmptyFocusState(data.count);
            } catch (err) {
                console.error('Failed to start focus mode:', err);
            }
        }
        
        function showEmptyFocusState(count = 0) {
            const container = document.getElementById('flashcardContainer');
            if (count === 0) {
                container.innerHTML = `
                    <div class="flashcard-empty">
                        <h3>🎉 No Weak Cards!</h3>
                        <p>Great job! You don't have any cards that need extra focus.</p>
                        <p style="margin-top: 16px; color: #6b7280;">
                            Cards become "weak" when you miss them 2+ times.
                        </p>
                        <button class="flashcard-btn primary" onclick="closeFlashcards();" style="margin-top: 20px;">
                            👍 Keep it up!
                        </button>
                    </div>
                `;
            } else {
                container.innerHTML = `
                    <div class="flashcard-empty">
                        <h3>🎯 Focus Mode</h3>
                        <p>Found <strong>${count}</strong> weak cards across your notes.</p>
                        <p style="margin-top: 16px; color: #6b7280;">
                            Focus mode for cross-file review coming soon!
                        </p>
                        <button class="flashcard-btn secondary" onclick="closeFlashcards();" style="margin-top: 20px;">
                            Close
                        </button>
                    </div>
                `;
            }
            document.getElementById('flashcardModal').classList.add('visible');
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }
        
        // ===== MIX MODE (ALL CARD TYPES) =====
        let mixCards = [];
        let currentMixIndex = 0;
        let mixStats = { correct: 0, wrong: 0 };
        
        async function openMixMode() {
            const filePath = '{{ file_path|default("")|safe }}';
            if (!filePath) {
                alert('No file loaded');
                return;
            }
            
            try {
                const response = await fetch('/api/study/all-cards/' + encodeURIComponent(filePath));
                const data = await response.json();
                
                if (data.success && data.cards && data.cards.length > 0) {
                    mixCards = data.cards.sort(() => Math.random() - 0.5);
                    currentMixIndex = 0;
                    mixStats = { correct: 0, wrong: 0 };
                    currentStudyMode = 'mix';
                    renderMixCard();
                    document.getElementById('flashcardModal').classList.add('visible');
                } else {
                    showEmptyMixState();
                    document.getElementById('flashcardModal').classList.add('visible');
                }
            } catch (err) {
                console.error('Failed to load mix cards:', err);
            }
        }
        
        function showEmptyMixState() {
            const container = document.getElementById('flashcardContainer');
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>🎲 No Study Cards Found</h3>
                    <p>This file doesn't have any flashcards, MCQs, or cloze deletions yet.</p>
                    <p style="margin-top: 16px;">Add any of these sections:</p>
                    <pre>## Flashcards
Q: Question here?
A: Answer here

## MCQ
Q: Question?
- [ ] Wrong answer
- [x] Correct answer

## Cloze
Text with {<!-- -->{blanks}} here.</pre>
                </div>
            `;
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }
        
        // ===== SESSION WRONG CARDS REVIEW =====
        function reviewSessionWrongCards() {
            if (sessionWrongCards.length === 0) {
                alert('No wrong cards to review!');
                return;
            }
            
            // Shuffle and restart mix mode with just the wrong cards
            mixCards = [...sessionWrongCards].sort(() => Math.random() - 0.5);
            currentMixIndex = 0;
            mixStats = { correct: 0, wrong: 0 };
            sessionWrongCards = []; // Clear for this new session
            currentStudyMode = 'mix';
            
            renderMixCard();
            
            // Update counter to show review mode
            const counter = document.getElementById('flashcardCounter');
            counter.innerHTML = `<span style="color: #ef4444;">🔄 Reviewing Mistakes</span> ${currentMixIndex + 1} / ${mixCards.length}`;
        }
        
        // ===== WEAK CARDS MIX MODE =====
        async function startWeakCardsMixMode(currentFileOnly = false) {
            try {
                // Build API URL with optional filepath filter
                let url = '/api/study/weak-cards-full';
                if (currentFileOnly) {
                    // Extract relative filepath from URL (remove /view/ prefix)
                    const urlPath = window.location.pathname;
                    if (urlPath.startsWith('/view/')) {
                        const filepath = decodeURIComponent(urlPath.replace('/view/', ''));
                        url += '?filepath=' + encodeURIComponent(filepath);
                    }
                }
                
                const response = await fetch(url);
                const data = await response.json();
                
                const scopeLabel = currentFileOnly ? '📄 This File' : '📚 All Files';
                
                if (data.success && data.cards && data.cards.length > 0) {
                    // Shuffle weak cards
                    mixCards = data.cards.sort(() => Math.random() - 0.5);
                    currentMixIndex = 0;
                    mixStats = { correct: 0, wrong: 0 };
                    currentStudyMode = 'mix';
                    
                    // Show badge indicating weak cards mode
                    const container = document.getElementById('flashcardContainer');
                    renderMixCard();
                    
                    // Add weak cards indicator with scope
                    const counter = document.getElementById('flashcardCounter');
                    counter.innerHTML = `<span style="color: #f59e0b;">🎯 Weak Cards (${scopeLabel})</span> ${currentMixIndex + 1} / ${mixCards.length}`;
                    
                    document.getElementById('flashcardModal').classList.add('visible');
                } else {
                    // No weak cards
                    const container = document.getElementById('flashcardContainer');
                    const scopeText = currentFileOnly ? 'in this file' : 'in the vault';
                    container.innerHTML = `
                        <div class="flashcard-empty">
                            <h3>🎉 No Weak Cards!</h3>
                            <p>Great job! You don't have any frequently missed cards ${scopeText}.</p>
                            <p style="margin-top: 16px; color: #6b7280;">
                                Cards become "weak" when you miss them 2+ times.
                            </p>
                            ${currentFileOnly ? '<button class="flashcard-btn secondary" onclick="closeFlashcards(); startWeakCardsMixMode(false);" style="margin-top: 12px;">Try Whole Vault Instead</button>' : ''}
                            <button class="flashcard-btn primary" onclick="closeFlashcards();" style="margin-top: 12px;">
                                👍 Keep it up!
                            </button>
                        </div>
                    `;
                    document.getElementById('flashcardControls').style.display = 'none';
                    document.getElementById('flashcardRatingControls').style.display = 'none';
                    document.querySelector('.flashcard-progress').style.display = 'none';
                    document.getElementById('flashcardModal').classList.add('visible');
                }
            } catch (err) {
                console.error('Failed to load weak cards:', err);
            }
        }
        
        function renderMixCard() {
            const card = mixCards[currentMixIndex];
            const container = document.getElementById('flashcardContainer');
            
            const typeEmoji = { flash: '🎴', mcq: '✅', cloze: '📝' };
            const typeName = { flash: 'Flashcard', mcq: 'MCQ', cloze: 'Cloze' };
            
            if (card.type === 'mcq') {
                let optionsHtml = card.options.map((opt, i) => 
                    `<button class="mcq-option" onclick="selectMixMcq(${i}, ${card.correct})">${String.fromCharCode(65+i)}. ${opt}</button>`
                ).join('');
                
                container.innerHTML = `
                    <div class="mcq-question">
                        <span class="card-type-badge mcq">${typeEmoji[card.type]} ${typeName[card.type]}</span>
                        <div style="margin-top: 16px; font-size: 1.3em;">${card.question}</div>
                    </div>
                    <div class="mcq-options" id="mixMcqOptions">${optionsHtml}</div>
                `;
                document.getElementById('flashcardControls').style.display = 'none';
                document.getElementById('flashcardRatingControls').style.display = 'none';
            } else {
                const questionContent = card.type === 'cloze' 
                    ? `<div class="cloze-card">${card.question}</div>`
                    : `<div class="flashcard-content">${parseMarkdown(card.question)}</div>`;
                
                container.innerHTML = `
                    <div class="flashcard" id="currentFlashcard" onclick="flipMixCard()">
                        <div class="flashcard-face flashcard-front">
                            <div class="flashcard-label">
                                <span class="card-type-badge ${card.type}">${typeEmoji[card.type]} ${typeName[card.type]}</span>
                            </div>
                            ${questionContent}
                            <div class="flashcard-hint">Click to reveal</div>
                        </div>
                        <div class="flashcard-face flashcard-back">
                            <div class="flashcard-label">Answer</div>
                            <div class="flashcard-content">${parseMarkdown(card.answer)}</div>
                        </div>
                    </div>
                `;
                document.getElementById('flashcardControls').style.display = 'flex';
                document.getElementById('flashcardRatingControls').style.display = 'none';
            }
            
            const counter = document.getElementById('flashcardCounter');
            const fill = document.getElementById('flashcardProgressFill');
            counter.textContent = `${currentMixIndex + 1} / ${mixCards.length}`;
            fill.style.width = `${((currentMixIndex + 1) / mixCards.length) * 100}%`;
            document.querySelector('.flashcard-progress').style.display = 'flex';
        }
        
        function flipMixCard() {
            const card = document.getElementById('currentFlashcard');
            if (card) {
                card.classList.toggle('flipped');
                if (card.classList.contains('flipped')) {
                    document.getElementById('flashcardControls').style.display = 'none';
                    document.getElementById('flashcardRatingControls').style.display = 'flex';
                } else {
                    document.getElementById('flashcardControls').style.display = 'flex';
                    document.getElementById('flashcardRatingControls').style.display = 'none';
                }
            }
        }
        
        function selectMixMcq(selectedIdx, correctIdx) {
            const options = document.querySelectorAll('#mixMcqOptions .mcq-option');
            options.forEach((opt, i) => {
                opt.disabled = true;
                if (i === correctIdx) opt.classList.add('correct');
                if (i === selectedIdx && selectedIdx !== correctIdx) opt.classList.add('incorrect');
            });
            
            const correct = selectedIdx === correctIdx;
            if (correct) mixStats.correct++;
            else mixStats.wrong++;
            
            const card = mixCards[currentMixIndex];
            const cardId = card.id || `${card.type}-${currentMixIndex}`;
            rateSrs(cardId, card.type, correct ? 3 : 1, card.filepath);
            
            // Track wrong cards for session review
            if (!correct && !sessionWrongCards.find(c => c.id === card.id && c.filepath === card.filepath)) {
                sessionWrongCards.push({...card});
            }
            
            // Show feedback and Next button instead of rating controls
            setTimeout(() => {
                const feedback = correct ? '✅ Correct!' : '❌ Wrong';
                const feedbackEl = document.createElement('div');
                feedbackEl.className = 'mcq-feedback ' + (correct ? 'correct' : 'wrong');
                feedbackEl.innerHTML = `
                    <span style="font-size: 1.2em; font-weight: 600;">${feedback}</span>
                    <button class="flashcard-btn primary" onclick="nextMixCard()" style="margin-top: 16px;">
                        ${currentMixIndex < mixCards.length - 1 ? 'Next Question →' : 'See Results'}
                    </button>
                `;
                document.getElementById('mixMcqOptions').after(feedbackEl);
            }, 300);
        }
        
        function rateMixCard(correct) {
            if (correct) mixStats.correct++;
            else mixStats.wrong++;
            
            const card = mixCards[currentMixIndex];
            const cardId = card.id || `${card.type}-${currentMixIndex}`;
            rateSrs(cardId, card.type, correct ? 3 : 1, card.filepath);
            
            // Track wrong cards for session review
            if (!correct && !sessionWrongCards.find(c => c.id === card.id && c.filepath === card.filepath)) {
                sessionWrongCards.push({...card});
            }
            
            nextMixCard();
        }
        
        function nextMixCard() {
            if (currentMixIndex < mixCards.length - 1) {
                currentMixIndex++;
                renderMixCard();
            } else {
                showMixSummary();
            }
        }
        
        function showMixSummary() {
            const container = document.getElementById('flashcardContainer');
            const total = mixStats.correct + mixStats.wrong;
            const pct = total > 0 ? Math.round((mixStats.correct / total) * 100) : 0;
            
            const reviewBtn = sessionWrongCards.length > 0 
                ? `<button class="flashcard-btn secondary" onclick="reviewSessionWrongCards();" style="margin-top: 12px;">
                        🔄 Review ${sessionWrongCards.length} Wrong Cards
                   </button>`
                : '';
            
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>🎉 Study Session Complete!</h3>
                    <div class="flashcard-stats" style="margin: 24px 0;">
                        <div class="flashcard-stat correct">
                            <div class="flashcard-stat-value">${mixStats.correct}</div>
                            <div class="flashcard-stat-label">Correct</div>
                        </div>
                        <div class="flashcard-stat wrong">
                            <div class="flashcard-stat-value">${mixStats.wrong}</div>
                            <div class="flashcard-stat-label">Needs Review</div>
                        </div>
                        <div class="flashcard-stat">
                            <div class="flashcard-stat-value">${pct}%</div>
                            <div class="flashcard-stat-label">Score</div>
                        </div>
                    </div>
                    <button class="flashcard-btn primary" onclick="closeFlashcards(); openDashboard();">
                        📊 View Dashboard
                    </button>
                    ${reviewBtn}
                </div>
            `;
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }

        // ===== TIMED MODE =====
        let timerInterval = null;
        let timerSeconds = 30;
        let timerEnabled = false;
        
        function startTimer(seconds = 30) {
            stopTimer();
            timerSeconds = seconds;
            timerEnabled = true;
            updateTimerDisplay();
            
            timerInterval = setInterval(() => {
                timerSeconds--;
                updateTimerDisplay();
                
                if (timerSeconds <= 0) {
                    stopTimer();
                    handleTimerExpired();
                }
            }, 1000);
        }
        
        function stopTimer() {
            if (timerInterval) {
                clearInterval(timerInterval);
                timerInterval = null;
            }
            const timerBar = document.getElementById('timerBar');
            if (timerBar) timerBar.style.display = 'none';
        }
        
        function updateTimerDisplay() {
            let timerBar = document.getElementById('timerBar');
            if (!timerBar) {
                // Create timer bar if it doesn't exist
                const progress = document.querySelector('.flashcard-progress');
                if (progress) {
                    timerBar = document.createElement('div');
                    timerBar.id = 'timerBar';
                    timerBar.className = 'timer-bar';
                    timerBar.innerHTML = '<div class="timer-bar-fill" id="timerBarFill"></div>';
                    progress.parentNode.insertBefore(timerBar, progress);
                }
            }
            
            if (timerBar) {
                timerBar.style.display = 'block';
                const fill = document.getElementById('timerBarFill');
                const pct = (timerSeconds / 30) * 100;
                fill.style.width = pct + '%';
                
                // Color warnings
                timerBar.className = 'timer-bar';
                if (timerSeconds <= 10) timerBar.classList.add('danger');
                else if (timerSeconds <= 15) timerBar.classList.add('warning');
            }
        }
        
        function handleTimerExpired() {
            // Auto-mark as wrong when timer expires
            const card = document.getElementById('currentFlashcard');
            if (card && !card.classList.contains('flipped')) {
                card.classList.add('flipped');
            }
            // Show timeout message
            const container = document.getElementById('flashcardContainer');
            const timeoutMsg = document.createElement('div');
            timeoutMsg.className = 'timer-expired-msg';
            timeoutMsg.innerHTML = '⏱️ Time\\'s up!';
            timeoutMsg.style.cssText = 'text-align:center;color:#ef4444;font-weight:bold;margin-top:10px;';
            container.appendChild(timeoutMsg);
            
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'flex';
        }
        
        // ===== CONFIDENCE RATING =====
        let pendingConfidence = null;
        
        function showConfidenceRating() {
            const controls = document.getElementById('flashcardControls');
            controls.innerHTML = `
                <div style="text-align: center; width: 100%;">
                    <div style="margin-bottom: 12px; color: #6b7280;">How confident are you?</div>
                    <div style="display: flex; gap: 8px; justify-content: center;">
                        <button class="srs-btn again" onclick="setConfidence(1)">1<br><span class="interval">Not at all</span></button>
                        <button class="srs-btn hard" onclick="setConfidence(2)">2<br><span class="interval">Unsure</span></button>
                        <button class="srs-btn" style="background:#e0e7ff;color:#4338ca;" onclick="setConfidence(3)">3<br><span class="interval">Maybe</span></button>
                        <button class="srs-btn good" onclick="setConfidence(4)">4<br><span class="interval">Likely</span></button>
                        <button class="srs-btn easy" onclick="setConfidence(5)">5<br><span class="interval">Certain</span></button>
                    </div>
                </div>
            `;
            controls.style.display = 'flex';
        }
        
        function setConfidence(level) {
            pendingConfidence = level;
            // Now flip the card to reveal answer
            flipFlashcard();
        }
        
        // ===== CROSS-FILE FOCUS MODE =====
        let focusCards = [];
        let currentFocusIndex = 0;
        let focusStats = { correct: 0, wrong: 0 };
        
        async function startFocusModeReal() {
            try {
                const response = await fetch('/api/study/weak-cards');
                const data = await response.json();
                
                if (!data.success || data.weakCards.length === 0) {
                    showEmptyFocusState();
                    document.getElementById('flashcardModal').classList.add('visible');
                    return;
                }
                
                // Load actual card content for each weak card
                focusCards = [];
                for (const weak of data.weakCards) {
                    const cardResp = await fetch('/api/study/all-cards/' + encodeURIComponent(weak.filepath));
                    const cardData = await cardResp.json();
                    
                    if (cardData.success && cardData.cards) {
                        // Find the specific card by key (e.g., "flash-0", "mcq-2")
                        const [cardType, cardIdx] = weak.cardKey.split('-');
                        const idx = parseInt(cardIdx);
                        const card = cardData.cards.find(c => c.type === cardType && c.id === weak.cardKey);
                        if (card) {
                            card.filepath = weak.filepath;
                            card.srsData = weak.data;
                            focusCards.push(card);
                        }
                    }
                }
                
                if (focusCards.length === 0) {
                    showEmptyFocusState();
                    document.getElementById('flashcardModal').classList.add('visible');
                    return;
                }
                
                currentFocusIndex = 0;
                focusStats = { correct: 0, wrong: 0 };
                renderFocusCard();
                document.getElementById('flashcardModal').classList.add('visible');
            } catch (err) {
                console.error('Failed to load focus cards:', err);
            }
        }
        
        function renderFocusCard() {
            const card = focusCards[currentFocusIndex];
            const container = document.getElementById('flashcardContainer');
            
            const typeEmoji = { flash: '🎴', mcq: '✅', cloze: '📝' };
            const fileName = card.filepath.split('/').pop().replace('.md', '');
            
            if (card.type === 'mcq') {
                let optionsHtml = card.options.map((opt, i) => 
                    `<button class="mcq-option" onclick="selectFocusMcq(${i}, ${card.correct})">${String.fromCharCode(65+i)}. ${opt}</button>`
                ).join('');
                
                container.innerHTML = `
                    <div class="mcq-question">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                            <span class="card-type-badge mcq">${typeEmoji[card.type]} Focus</span>
                            <span style="font-size:12px;color:#6b7280;">📄 ${fileName}</span>
                        </div>
                        <div style="font-size: 1.3em;">${card.question}</div>
                    </div>
                    <div class="mcq-options" id="focusMcqOptions">${optionsHtml}</div>
                `;
                document.getElementById('flashcardControls').style.display = 'none';
                document.getElementById('flashcardRatingControls').style.display = 'none';
            } else {
                const questionContent = card.type === 'cloze' 
                    ? `<div class="cloze-card">${card.question}</div>`
                    : `<div class="flashcard-content">${parseMarkdown(card.question)}</div>`;
                
                container.innerHTML = `
                    <div class="flashcard" id="currentFlashcard" onclick="flipFocusCard()">
                        <div class="flashcard-face flashcard-front">
                            <div class="flashcard-label" style="display:flex;justify-content:space-between;">
                                <span class="card-type-badge ${card.type}">${typeEmoji[card.type]} Focus</span>
                                <span style="font-size:12px;color:#6b7280;">📄 ${fileName}</span>
                            </div>
                            ${questionContent}
                            <div class="flashcard-hint">Click to reveal</div>
                        </div>
                        <div class="flashcard-face flashcard-back">
                            <div class="flashcard-label">Answer</div>
                            <div class="flashcard-content">${parseMarkdown(card.answer)}</div>
                        </div>
                    </div>
                `;
                document.getElementById('flashcardControls').style.display = 'flex';
                document.getElementById('flashcardRatingControls').style.display = 'none';
            }
            
            const counter = document.getElementById('flashcardCounter');
            const fill = document.getElementById('flashcardProgressFill');
            counter.textContent = `🎯 ${currentFocusIndex + 1} / ${focusCards.length}`;
            fill.style.width = `${((currentFocusIndex + 1) / focusCards.length) * 100}%`;
            document.querySelector('.flashcard-progress').style.display = 'flex';
        }
        
        function flipFocusCard() {
            const card = document.getElementById('currentFlashcard');
            if (card) {
                card.classList.toggle('flipped');
                if (card.classList.contains('flipped')) {
                    document.getElementById('flashcardControls').style.display = 'none';
                    document.getElementById('flashcardRatingControls').style.display = 'flex';
                }
            }
        }
        
        function selectFocusMcq(selectedIdx, correctIdx) {
            const options = document.querySelectorAll('#focusMcqOptions .mcq-option');
            options.forEach((opt, i) => {
                opt.disabled = true;
                if (i === correctIdx) opt.classList.add('correct');
                if (i === selectedIdx && selectedIdx !== correctIdx) opt.classList.add('incorrect');
            });
            
            const correct = selectedIdx === correctIdx;
            rateFocusCard(correct);
        }
        
        function rateFocusCard(correct) {
            if (correct) focusStats.correct++;
            else focusStats.wrong++;
            
            const card = focusCards[currentFocusIndex];
            // Update SRS for this card's file
            fetch('/api/srs/' + encodeURIComponent(card.filepath), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    cardId: card.id.split('-')[1], 
                    cardType: card.type, 
                    rating: correct ? 3 : 1 
                })
            });
            
            setTimeout(() => nextFocusCard(), 500);
        }
        
        function nextFocusCard() {
            if (currentFocusIndex < focusCards.length - 1) {
                currentFocusIndex++;
                renderFocusCard();
            } else {
                showFocusSummary();
            }
        }
        
        function showFocusSummary() {
            const container = document.getElementById('flashcardContainer');
            const total = focusStats.correct + focusStats.wrong;
            const pct = total > 0 ? Math.round((focusStats.correct / total) * 100) : 0;
            
            container.innerHTML = `
                <div class="flashcard-empty">
                    <h3>🎯 Focus Session Complete!</h3>
                    <div class="flashcard-stats" style="margin: 24px 0;">
                        <div class="flashcard-stat correct">
                            <div class="flashcard-stat-value">${focusStats.correct}</div>
                            <div class="flashcard-stat-label">Improved</div>
                        </div>
                        <div class="flashcard-stat wrong">
                            <div class="flashcard-stat-value">${focusStats.wrong}</div>
                            <div class="flashcard-stat-label">Still Weak</div>
                        </div>
                        <div class="flashcard-stat">
                            <div class="flashcard-stat-value">${pct}%</div>
                            <div class="flashcard-stat-label">Score</div>
                        </div>
                    </div>
                    <p style="color:#6b7280;margin-bottom:20px;">Keep practicing weak cards until they stick!</p>
                    <button class="flashcard-btn primary" onclick="closeFlashcards(); openDashboard();">
                        📊 View Dashboard
                    </button>
                </div>
            `;
            document.getElementById('flashcardControls').style.display = 'none';
            document.getElementById('flashcardRatingControls').style.display = 'none';
            document.querySelector('.flashcard-progress').style.display = 'none';
        }
        
        // ===== SUMMARY VIEW =====
        async function openSummary() {
            const filePath = '{{ file_path|default("")|safe }}';
            if (!filePath) return;
            
            try {
                const response = await fetch('/api/summary/' + encodeURIComponent(filePath));
                const data = await response.json();
                
                const container = document.getElementById('flashcardContainer');
                
                if (data.success && data.summary) {
                    container.innerHTML = `
                        <div style="max-width: 600px; margin: 0 auto; padding: 20px; text-align: left;">
                            <h2 style="margin-bottom: 20px; display: flex; align-items: center; gap: 10px;">
                                📋 Quick Summary
                            </h2>
                            <div style="background: #f9fafb; border-radius: 12px; padding: 20px;">
                                ${data.summary.sections.map(s => `
                                    <div style="margin-bottom: 16px;">
                                        <h3 style="font-size: 14px; color: #6b7280; margin-bottom: 8px;">${s.title}</h3>
                                        <ul style="margin: 0; padding-left: 20px;">
                                            ${s.points.map(p => `<li style="margin: 4px 0;">${p}</li>`).join('')}
                                        </ul>
                                    </div>
                                `).join('')}
                            </div>
                            ${data.summary.keyTerms.length > 0 ? `
                                <div style="margin-top: 20px;">
                                    <h3 style="font-size: 14px; color: #6b7280; margin-bottom: 8px;">Key Terms</h3>
                                    <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                                        ${data.summary.keyTerms.map(t => `<span style="background: #e0e7ff; color: #4338ca; padding: 4px 10px; border-radius: 12px; font-size: 13px;">${t}</span>`).join('')}
                                    </div>
                                </div>
                            ` : ''}
                        </div>
                    `;
                } else {
                    container.innerHTML = `
                        <div class="flashcard-empty">
                            <h3>📋 No Summary Available</h3>
                            <p>Add summaries to your notes using callouts:</p>
                            <pre>> [!summary]
> - Key point 1
> - Key point 2
> - Key point 3</pre>
                        </div>
                    `;
                }
                
                document.getElementById('flashcardModal').classList.add('visible');
                document.getElementById('flashcardControls').style.display = 'none';
                document.getElementById('flashcardRatingControls').style.display = 'none';
                document.querySelector('.flashcard-progress').style.display = 'none';
            } catch (err) {
                console.error('Failed to load summary:', err);
            }
        }

        // ===== EXAM MODE =====
        function openExamMode() {
            const filePath = '{{ file_path|default("")|safe }}';
            if (!filePath) return;
            
            // Navigate to exam simulation page
            window.location.href = '/exam/' + encodeURIComponent(filePath);
        }

        // ===== STUDY SETTINGS =====
        let studySettings = {
            timedMode: false,
            confidenceRating: false,
            dailyGoal: 50,
            timerSeconds: 30,
            srsMode: 'srs'
        };
        
        async function loadStudySettings() {
            try {
                const response = await fetch('/api/study/settings');
                const data = await response.json();
                if (data.success && data.settings) {
                    studySettings = { ...studySettings, ...data.settings };
                    applySettingsToUI();
                }
            } catch (err) {
                console.error('Failed to load settings:', err);
            }
        }
        
        function applySettingsToUI() {
            document.getElementById('settingTimedMode').checked = studySettings.timedMode || false;
            document.getElementById('settingConfidence').checked = studySettings.confidenceRating || false;
            document.getElementById('settingDailyGoal').value = studySettings.dailyGoal || 50;
            document.getElementById('settingTimerSeconds').value = studySettings.timerSeconds || 30;
            document.getElementById('settingSrsMode').value = studySettings.srsMode || 'srs';
        }
        
        function openStudySettings() {
            loadStudySettings();
            document.getElementById('settingsModal').classList.add('visible');
        }
        
        function closeStudySettings() {
            document.getElementById('settingsModal').classList.remove('visible');
        }
        
        async function saveStudySettings() {
            studySettings = {
                timedMode: document.getElementById('settingTimedMode').checked,
                confidenceRating: document.getElementById('settingConfidence').checked,
                dailyGoal: parseInt(document.getElementById('settingDailyGoal').value) || 50,
                timerSeconds: parseInt(document.getElementById('settingTimerSeconds').value) || 30,
                srsMode: document.getElementById('settingSrsMode').value || 'srs'
            };
            
            try {
                const response = await fetch('/api/study/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(studySettings)
                });
                const data = await response.json();
                if (data.success) {
                    closeStudySettings();
                    // Show confirmation
                    const btn = document.querySelector('.settings-save');
                    btn.textContent = '✅ Saved!';
                    setTimeout(() => { btn.textContent = 'Save Settings'; }, 1500);
                }
            } catch (err) {
                console.error('Failed to save settings:', err);
            }
        }
        
        // Close settings on backdrop click
        document.getElementById('settingsModal')?.addEventListener('click', function(e) {
            if (e.target === this) closeStudySettings();
        });
        
        // Load settings on page load
        document.addEventListener('DOMContentLoaded', loadStudySettings);

        function toggleFullscreen() {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen();
            } else {
                document.exitFullscreen();
            }
        }
        
        // ===== CALLOUT TOGGLE =====
        let calloutsExpanded = true;  // Track state
        
        function toggleAllCallouts() {
            const details = document.querySelectorAll('details.callout');
            const btn = document.getElementById('calloutToggle');
            
            if (calloutsExpanded) {
                // Collapse all
                details.forEach(d => d.removeAttribute('open'));
                if (btn) {
                    btn.innerHTML = '📁 Expand All';
                    btn.title = 'Expand All Callouts';
                }
            } else {
                // Expand all
                details.forEach(d => d.setAttribute('open', ''));
                if (btn) {
                    btn.innerHTML = '📂 Collapse All';
                    btn.title = 'Collapse All Callouts';
                }
            }
            calloutsExpanded = !calloutsExpanded;
        }
        
        // ===== THEME TOGGLE =====
        function toggleTheme() {
            const body = document.body;
            const themeToggle = document.getElementById('themeToggle');
            const isDark = body.classList.toggle('dark-theme');
            
            // Update button icon
            themeToggle.textContent = isDark ? '☀️' : '🌙';
            themeToggle.title = isDark ? 'Switch to Light Theme' : 'Switch to Dark Theme';
            
            // Persist preference
            localStorage.setItem('theme', isDark ? 'dark' : 'light');
        }
        
        function initTheme() {
            const savedTheme = localStorage.getItem('theme');
            const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
            const themeToggle = document.getElementById('themeToggle');
            
            // Use saved preference, or fall back to system preference
            const shouldBeDark = savedTheme === 'dark' || (savedTheme === null && prefersDark);
            
            if (shouldBeDark) {
                document.body.classList.add('dark-theme');
                if (themeToggle) {
                    themeToggle.textContent = '☀️';
                    themeToggle.title = 'Switch to Light Theme';
                }
            }
        }
        
        // Initialize theme early to prevent flash
        initTheme();
        
        // ===== TOOLBAR DROPDOWN MENU =====
        function toggleToolbarMenu(event) {
            event.stopPropagation();
            const dropdown = document.getElementById('toolbarDropdown');
            dropdown.classList.toggle('show');
        }
        
        function closeToolbarMenu() {
            const dropdown = document.getElementById('toolbarDropdown');
            if (dropdown) dropdown.classList.remove('show');
        }
        
        // Close dropdown when clicking outside
        document.addEventListener('click', function(event) {
            const dropdown = document.getElementById('toolbarDropdown');
            const menuBtn = event.target.closest('.toolbar-menu-btn');
            if (!menuBtn && dropdown && dropdown.classList.contains('show')) {
                dropdown.classList.remove('show');
            }
        });
        
        // ===== TREE STATE PERSISTENCE =====
        // Get unique path for a folder by traversing up to build the path
        function getFolderPath(folderItem) {
            const parts = [];
            let current = folderItem;
            while (current && current.classList.contains('folder-item')) {
                const nameEl = current.querySelector(':scope > .folder-header > .folder-name');
                if (nameEl) {
                    parts.unshift(nameEl.textContent.trim());
                }
                // Go up: folder-item > folder-children (ul) > folder-item
                current = current.parentElement?.closest('.folder-item');
            }
            return parts.join('/');
        }
        
        function saveTreeState() {
            const expandedFolders = [];
            document.querySelectorAll('.folder-item.open').forEach(item => {
                const path = getFolderPath(item);
                if (path) expandedFolders.push(path);
            });
            localStorage.setItem('treeState', JSON.stringify(expandedFolders));
        }
        
        function restoreTreeState() {
            const saved = localStorage.getItem('treeState');
            let expandedFolders = [];
            
            try {
                if (saved) {
                    const parsed = JSON.parse(saved);
                    if (Array.isArray(parsed)) {
                        expandedFolders = parsed;
                    }
                }
            } catch (e) {
                console.warn('Failed to parse tree state:', e);
            }
            
            // Expand saved folders (all start collapsed from server)
            if (expandedFolders.length > 0) {
                document.querySelectorAll('.folder-item').forEach(item => {
                    const path = getFolderPath(item);
                    if (expandedFolders.includes(path)) {
                        item.classList.add('open');
                        const icon = item.querySelector(':scope > .folder-header > .folder-icon');
                        if (icon) icon.classList.remove('collapsed');
                    }
                });
            }
            
            // Always ensure parents of active file are expanded
            const activeLink = document.querySelector('.sidebar a.active');
            if (activeLink) {
                let parent = activeLink.closest('.folder-item');
                while (parent) {
                    parent.classList.add('open');
                    const icon = parent.querySelector(':scope > .folder-header > .folder-icon');
                    if (icon) icon.classList.remove('collapsed');
                    parent = parent.parentElement?.closest('.folder-item');
                }
            }
        }
        
        function toggleFolder(header) {
            const folderItem = header.parentElement;
            const icon = header.querySelector('.folder-icon');
            
            folderItem.classList.toggle('open');
            icon.classList.toggle('collapsed');
            
            // Save state after toggle
            saveTreeState();
        }
        
        function collapseAllFolders() {
            document.querySelectorAll('.folder-item').forEach(item => {
                item.classList.remove('open');
                item.querySelector('.folder-icon').classList.add('collapsed');
            });
            saveTreeState();
        }
        
        function expandAllFolders() {
            document.querySelectorAll('.folder-item').forEach(item => {
                item.classList.add('open');
                item.querySelector('.folder-icon').classList.remove('collapsed');
            });
            saveTreeState();
        }
        
        // ===== SEARCH FUNCTIONALITY =====
        let searchTimeout = null;
        
        function initSearch() {
            const searchInput = document.getElementById('searchInput');
            const searchResults = document.getElementById('searchResults');
            const searchContentCheckbox = document.getElementById('searchContent');
            const searchClear = document.getElementById('searchClear');
            const sidebarTreeContent = document.getElementById('sidebarContent');
            
            if (!searchInput) return;
            
            searchInput.addEventListener('input', function() {
                const query = this.value.trim();
                
                // Show/hide clear button
                if (searchClear) {
                    searchClear.style.display = query.length > 0 ? 'block' : 'none';
                }
                
                // Clear previous timeout
                if (searchTimeout) {
                    clearTimeout(searchTimeout);
                }
                
                if (query.length < 2) {
                    hideSearchResults();
                    showFileTree();
                    return;
                }
                
                // Debounce search
                searchTimeout = setTimeout(() => {
                    performSearch(query);
                }, 200);
            });
            
            // Handle Escape key
            searchInput.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    clearSearch();
                }
            });
            
            function performSearch(query) {
                const searchContent = searchContentCheckbox ? searchContentCheckbox.checked : false;
                
                // Show loading state
                if (searchResults) {
                    searchResults.innerHTML = '<div class="search-loading">🔍 Searching...</div>';
                    searchResults.classList.add('visible');
                }
                hideFileTree();
                
                // Call search API
                fetch('/api/search?q=' + encodeURIComponent(query) + '&content=' + searchContent + '&limit=30')
                    .then(response => response.json())
                    .then(data => {
                        if (data.success && data.results.length > 0) {
                            renderSearchResults(data.results, query);
                        } else if (searchResults) {
                            searchResults.innerHTML = '<div class="search-no-results">No files found</div>';
                        }
                    })
                    .catch(err => {
                        console.error('Search error:', err);
                        if (searchResults) {
                            searchResults.innerHTML = '<div class="search-no-results">Search error</div>';
                        }
                    });
            }
            
            function renderSearchResults(results, query) {
                if (!searchResults) return;
                const queryTerms = query.toLowerCase().split(/\s+/);
                
                let html = '';
                results.forEach(result => {
                    // Highlight query terms in filename
                    let filename = escapeHtml(result.filename);
                    queryTerms.forEach(term => {
                        const regex = new RegExp('(' + escapeRegex(term) + ')', 'gi');
                        filename = filename.replace(regex, '<mark>$1</mark>');
                    });
                    
                    // Get folder path
                    const pathParts = result.path.split('/');
                    const folderPath = pathParts.length > 1 ? pathParts.slice(0, -1).join('/') : '';
                    
                    var safeUrl = result.url.replace(/'/g, "\\'");
                    html += '<div class="search-result-item" data-url="' + escapeHtml(result.url) + '" onclick="window.location.href=this.dataset.url">';
                    html += '<div class="search-result-filename">' + filename + '</div>';
                    if (folderPath) {
                        html += '<div class="search-result-path">📁 ' + escapeHtml(folderPath) + '</div>';
                    }
                    if (result.snippet) {
                        html += '<div class="search-result-snippet">' + highlightSnippet(result.snippet, queryTerms) + '</div>';
                    }
                    html += '</div>';
                });
                
                searchResults.innerHTML = html;
            }
            
            function highlightSnippet(snippet, queryTerms) {
                let highlighted = escapeHtml(snippet);
                queryTerms.forEach(term => {
                    const regex = new RegExp('(' + escapeRegex(term) + ')', 'gi');
                    highlighted = highlighted.replace(regex, '<mark>$1</mark>');
                });
                return highlighted;
            }
            
            function hideSearchResults() {
                if (searchResults) {
                    searchResults.classList.remove('visible');
                    searchResults.innerHTML = '';
                }
            }
            
            function showFileTree() {
                if (sidebarTreeContent) {
                    sidebarTreeContent.style.display = 'block';
                }
            }
            
            function hideFileTree() {
                if (sidebarTreeContent) {
                    sidebarTreeContent.style.display = 'none';
                }
            }
            
            // Expose clearSearch globally
            window.clearSearch = function() {
                searchInput.value = '';
                if (searchClear) searchClear.style.display = 'none';
                hideSearchResults();
                showFileTree();
                searchInput.focus();
            };
        }
        
        function escapeRegex(string) {
            return string.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Initialize search after DOM is ready
        document.addEventListener('DOMContentLoaded', initSearch);
        
        // Keyboard shortcut: Ctrl/Cmd + K to focus search
        document.addEventListener('keydown', function(e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                const searchInput = document.getElementById('searchInput');
                if (searchInput) {
                    searchInput.focus();
                    searchInput.select();
                }
            }
        });
        
        function downloadPDF() {
            const currentPath = window.location.pathname;
            if (currentPath.startsWith('/view/')) {
                const filepath = currentPath.replace('/view/', '');
                window.location.href = '/pdf/' + filepath;
            }
        }
        
        function downloadTopicZip() {
            const currentPath = window.location.pathname;
            if (currentPath.startsWith('/view/')) {
                const filepath = currentPath.replace('/view/', '');
                // Show loading indicator
                const btn = event.target.closest('button');
                const originalText = btn.innerHTML;
                btn.innerHTML = '⏳ <span class="btn-text">Loading...</span>';
                btn.disabled = true;
                
                // Trigger download
                const link = document.createElement('a');
                link.href = '/api/download-topic-zip/' + filepath;
                link.click();
                
                // Reset button after a delay
                setTimeout(() => {
                    btn.innerHTML = originalText;
                    btn.disabled = false;
                }, 2000);
            }
        }
        
        function copyFilePath() {
            const pathText = document.getElementById('pathText');
            if (!pathText) return;
            
            // Use full system path from data attribute
            const path = pathText.dataset.fullPath || pathText.textContent;
            
            function showCopiedFeedback() {
                const copyBtn = document.querySelector('.copy-btn');
                const copyIcon = document.getElementById('copyIcon');
                const copyText = document.getElementById('copyText');
                
                copyBtn.classList.add('copied');
                copyIcon.textContent = '✓';
                copyText.textContent = 'Copied!';
                
                setTimeout(() => {
                    copyBtn.classList.remove('copied');
                    copyIcon.textContent = '📋';
                    copyText.textContent = 'Copy';
                }, 2000);
            }
            
            function fallbackCopy(text) {
                const textArea = document.createElement('textarea');
                textArea.value = text;
                textArea.style.position = 'fixed';
                textArea.style.left = '-9999px';
                document.body.appendChild(textArea);
                textArea.select();
                try {
                    document.execCommand('copy');
                    showCopiedFeedback();
                } catch (e) {
                    alert('Path: ' + text);
                }
                document.body.removeChild(textArea);
            }
            
            // Try modern clipboard API first, fallback to execCommand
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(path).then(showCopiedFeedback).catch(() => fallbackCopy(path));
            } else {
                fallbackCopy(path);
            }
        }
        
        // Keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            // Ctrl+B or Cmd+B to toggle sidebar
            if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
                e.preventDefault();
                if (isMobile) {
                    toggleSidebar();
                } else {
                    toggleDock();
                }
            }
            // F11 or Ctrl+Shift+F for fullscreen
            if (e.key === 'F11' || ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'F')) {
                e.preventDefault();
                toggleFullscreen();
            }
            // Escape to close sidebar on mobile
            if (e.key === 'Escape' && isMobile) {
                const sidebar = document.getElementById('sidebar');
                if (!sidebar.classList.contains('hidden')) {
                    toggleSidebar();
                }
            }
        });
        
        // Close sidebar when clicking a link on mobile
        document.querySelectorAll('.sidebar a').forEach(link => {
            link.addEventListener('click', function() {
                if (isMobile) {
                    const sidebar = document.getElementById('sidebar');
                    if (!sidebar.classList.contains('hidden')) {
                        toggleSidebar();
                    }
                }
            });
        });
        
        // Convert Mermaid mindmap to Markmap markdown format
        function convertMermaidMindmapToMarkdown(content) {
            var lines = content.split('\\n');
            var result = [];
            var started = false;
            
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i];
                var trimmed = line.trim();
                
                // Skip mindmap declaration
                if (trimmed.toLowerCase() === 'mindmap') {
                    started = true;
                    continue;
                }
                if (!started) continue;
                if (!trimmed) continue;
                
                // Count leading spaces for depth
                var spaces = 0;
                for (var j = 0; j < line.length; j++) {
                    if (line[j] === ' ') spaces++;
                    else break;
                }
                var depth = Math.floor(spaces / 2);
                
                // Clean up Mermaid node syntax
                var text = trimmed;
                text = text.replace(/^root\\(/, '').replace(/\\)$/, '');
                text = text.replace(/^\\)\\)/, '').replace(/\\(\\($/, '');
                text = text.replace(/^\\(/, '').replace(/\\)$/, '');
                text = text.replace(/^\\[/, '').replace(/\\]$/, '');
                text = text.replace(/^\\{\\{/, '').replace(/\\}\\}$/, '');
                
                if (!text) continue;
                
                // Build markdown: root = h1, children = nested lists
                if (depth === 0) {
                    result.push('# ' + text);
                } else {
                    var indent = '';
                    for (var k = 0; k < depth - 1; k++) indent += '  ';
                    result.push(indent + '- ' + text);
                }
            }
            return result.join('\\n');
        }
        
        // Initialize Diagrams (Mermaid + Markmap for mindmaps)
        document.addEventListener('DOMContentLoaded', function() {
            // Find all code blocks that might be diagrams
            const codeBlocks = document.querySelectorAll('pre code');
            
            codeBlocks.forEach(function(codeBlock, index) {
                const pre = codeBlock.parentElement;
                const content = codeBlock.textContent.trim();
                
                // Check if it's a mindmap - use Markmap for elegant rendering
                const isMindmap = content.toLowerCase().startsWith('mindmap');
                
                if (isMindmap) {
                    // Convert to Markmap format
                    const markmapContent = convertMermaidMindmapToMarkdown(content);
                    
                    // Create markmap container
                    const container = document.createElement('div');
                    container.className = 'markmap';
                    
                    // Use script template to hold content
                    const script = document.createElement('script');
                    script.type = 'text/template';
                    script.textContent = markmapContent;
                    container.appendChild(script);
                    
                    // Replace the pre element
                    pre.parentNode.replaceChild(container, pre);
                    return;
                }
                
                // Check if it's a mermaid block (other diagram types)
                const isMermaidClass = codeBlock.className.includes('mermaid') || 
                                       codeBlock.className.includes('language-mermaid');
                const isMermaidContent = content.match(/^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|journey|gantt|pie|gitGraph|timeline|quadrantChart|xychart|sankey|packet|block)/i);
                
                if (isMermaidClass || isMermaidContent) {
                    // Create a new div for mermaid
                    const mermaidDiv = document.createElement('div');
                    mermaidDiv.className = 'mermaid';
                    mermaidDiv.textContent = content;
                    mermaidDiv.id = 'mermaid-' + index;
                    
                    // Replace the pre element with the mermaid div
                    pre.parentNode.replaceChild(mermaidDiv, pre);
                }
            });
            
            // Run mermaid on all .mermaid elements (non-mindmap)
            if (document.querySelectorAll('.mermaid').length > 0) {
                mermaid.run({
                    nodes: document.querySelectorAll('.mermaid')
                });
            }
            
            // Markmap autoloader will automatically render .markmap elements
            // Fix text color for dark backgrounds after markmap renders
            setTimeout(function() {
                var isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
                var bodyDark = document.body.classList.contains('dark') || 
                               document.body.style.background.includes('#1') ||
                               document.body.style.background.includes('#2');
                if (isDark || bodyDark) {
                    document.querySelectorAll('.markmap text').forEach(function(el) {
                        el.style.fill = '#ffffff';
                    });
                }
            }, 1000);
            
            // Re-typeset MathJax after content is loaded
            if (typeof MathJax !== 'undefined' && MathJax.typesetPromise) {
                MathJax.typesetPromise().then(function() {
                    // Process inline-math spans after initial typeset completes
                    processInlineMath();
                }).catch(function(err) {
                    console.log('MathJax typeset failed: ' + err.message);
                });
            }
            
            function processInlineMath() {
                var inlineMaths = document.querySelectorAll('.inline-math');
                if (inlineMaths.length === 0) return;
                
                // Convert inline-math spans to proper MathJax-processable format
                inlineMaths.forEach(function(el) {
                    // Check if already processed by MathJax
                    if (!el.querySelector('mjx-container')) {
                        var mathContent = el.getAttribute('data-math');
                        if (mathContent) {
                            // Set text content with delimiters for MathJax to process
                            el.textContent = '\\(' + mathContent + '\\)';
                        }
                    }
                });
                
                // Re-typeset the inline math elements
                if (typeof MathJax !== 'undefined' && MathJax.typesetPromise) {
                    MathJax.typesetPromise(Array.from(inlineMaths)).catch(function(e) {
                        console.log('Inline math typeset error:', e);
                    });
                }
            }
            
            // Check for existing annotations and show indicator
            if (typeof updateAnnotationIndicator === 'function') {
                updateAnnotationIndicator();
            }
        });
        
        // ============================================
        // OVERLAY ANNOTATION SYSTEM (Apple Pencil Support)
        // Draws directly on top of content
        // ============================================
        
        // Get current file path from URL for annotations
        const annotationFilePath = window.location.pathname.startsWith('/view/') 
            ? window.location.pathname.replace('/view/', '') 
            : null;
        
        // Annotation state
        let annotationStrokes = [];
        let redoStack = [];
        let currentStroke = null;
        let isDrawing = false;
        let currentTool = 'pen';
        let currentColor = '#000000';
        let currentSize = 4;
        let annotationCanvas, annotationCtx;
        let annotationOverlay;
        let contentWrapper;
        let hasUnsavedChanges = false;
        let annotationModeActive = false;
        let lastCanvasWidth = 0;
        let lastCanvasHeight = 0;
        let canvasDPR = 1; // Device pixel ratio for crisp rendering
        
        // Palm rejection state
        let penIsActive = false;
        let palmRejectionTimeout = null;
        
        // Perfect-freehand inspired stroke smoothing
        function getStrokeOutline(points, size, thinning = 0.5) {
            if (points.length < 2) return [];
            
            const outline = [];
            const totalLength = points.length;
            
            for (let i = 0; i < totalLength; i++) {
                const point = points[i];
                const pressure = point.pressure || 0.5;
                
                // Calculate dynamic width based on pressure and position
                const t = i / (totalLength - 1);
                const tapering = Math.sin(t * Math.PI);
                const width = size * (1 - thinning + thinning * pressure) * (0.5 + 0.5 * tapering);
                
                outline.push({
                    x: point.x,
                    y: point.y,
                    width: Math.max(1, width)
                });
            }
            
            return outline;
        }
        
        // Fast incremental drawing - only draws last segment (for real-time)
        function drawStrokeIncremental(ctx, stroke) {
            const points = stroke.points;
            if (points.length < 2) return;
            
            const p0 = points[points.length - 2];
            const p1 = points[points.length - 1];
            
            ctx.save();
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            
            if (stroke.tool === 'eraser') {
                ctx.globalCompositeOperation = 'destination-out';
                ctx.strokeStyle = 'rgba(0,0,0,1)';
            } else if (stroke.tool === 'highlighter') {
                ctx.globalCompositeOperation = 'source-over';
                ctx.strokeStyle = stroke.color;
                ctx.globalAlpha = 0.35;
            } else {
                ctx.globalCompositeOperation = 'source-over';
                ctx.strokeStyle = stroke.color;
                ctx.globalAlpha = 1;
            }
            
            // Simple line for speed
            const pressure = (p0.pressure + p1.pressure) / 2 || 0.5;
            ctx.lineWidth = stroke.size * (0.5 + 0.5 * pressure);
            
            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.lineTo(p1.x, p1.y);
            ctx.stroke();
            ctx.restore();
        }
        
        // Full stroke drawing (for redraw/replay)
        function drawStroke(ctx, stroke, isEraser = false) {
            if (!stroke.points || stroke.points.length < 2) return;
            
            ctx.save();
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            
            if (isEraser || stroke.tool === 'eraser') {
                ctx.globalCompositeOperation = 'destination-out';
                ctx.strokeStyle = 'rgba(0,0,0,1)';
            } else if (stroke.tool === 'highlighter') {
                ctx.globalCompositeOperation = 'source-over';
                ctx.strokeStyle = stroke.color;
                ctx.globalAlpha = 0.35;
            } else {
                ctx.globalCompositeOperation = 'source-over';
                ctx.strokeStyle = stroke.color;
                ctx.globalAlpha = 1;
            }
            
            const points = stroke.points;
            ctx.beginPath();
            ctx.moveTo(points[0].x, points[0].y);
            
            for (let i = 1; i < points.length; i++) {
                const p0 = points[i - 1];
                const p1 = points[i];
                const pressure = (p0.pressure + p1.pressure) / 2 || 0.5;
                ctx.lineWidth = stroke.size * (0.5 + 0.5 * pressure);
                ctx.lineTo(p1.x, p1.y);
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(p1.x, p1.y);
            }
            
            ctx.restore();
        }
        
        function redrawAllStrokes() {
            if (!annotationCanvas || !annotationCtx) return;
            
            // Clear using logical dimensions (context is already scaled)
            annotationCtx.clearRect(0, 0, lastCanvasWidth, lastCanvasHeight);
            
            for (const stroke of annotationStrokes) {
                drawStroke(annotationCtx, stroke);
            }
        }
        
        function resizeCanvas() {
            if (!annotationCanvas || !contentWrapper) return;
            
            let scrollWidth, scrollHeight;
            
            // Cap canvas size to browser limits
            const maxCanvasSize = 16384;
            
            if (window.isPdfAnnotationMode) {
                // PDF mode - size to cover PDF pages (capped)
                scrollWidth = Math.min(contentWrapper.scrollWidth, maxCanvasSize);
                scrollHeight = Math.min(contentWrapper.scrollHeight, maxCanvasSize);
            } else {
                // Normal mode
                const content = document.getElementById('contentArea');
                scrollWidth = Math.max(content.scrollWidth, contentWrapper.clientWidth);
                scrollHeight = Math.max(content.scrollHeight, contentWrapper.scrollHeight, contentWrapper.clientHeight);
            }
            
            // Get device pixel ratio for crisp rendering on Retina/high-DPI displays
            canvasDPR = window.devicePixelRatio || 1;
            
            // Only resize if dimensions changed significantly
            if (Math.abs(scrollWidth - lastCanvasWidth) > 10 || Math.abs(scrollHeight - lastCanvasHeight) > 10) {
                // Save current strokes
                const strokesBackup = [...annotationStrokes];
                
                // Set canvas size accounting for DPR (internal resolution)
                annotationCanvas.width = scrollWidth * canvasDPR;
                annotationCanvas.height = scrollHeight * canvasDPR;
                
                // Set CSS size (display size)
                annotationCanvas.style.width = scrollWidth + 'px';
                annotationCanvas.style.height = scrollHeight + 'px';
                
                // Scale context to match DPR
                annotationCtx.setTransform(canvasDPR, 0, 0, canvasDPR, 0, 0);
                
                // Restore high-quality rendering settings after transform
                annotationCtx.imageSmoothingEnabled = true;
                annotationCtx.imageSmoothingQuality = 'high';
                
                lastCanvasWidth = scrollWidth;
                lastCanvasHeight = scrollHeight;
                
                // Restore strokes
                annotationStrokes = strokesBackup;
                redrawAllStrokes();
            }
        }
        
        function initAnnotationOverlay() {
            // Check if we're in PDF mode - if pdfViewer exists, skip init (handled by initPdfAnnotation)
            const pdfViewer = document.getElementById('pdfViewer');
            if (pdfViewer) {
                // PDF mode - annotation will be initialized by initPdfAnnotation after PDF loads
                window.isPdfAnnotationMode = true;
                return;
            }
            
            // Normal mode (markdown files)
            annotationCanvas = document.getElementById('annotationCanvas');
            annotationOverlay = document.getElementById('annotationOverlay');
            contentWrapper = document.getElementById('contentWrapper');
            window.isPdfAnnotationMode = false;
            
            if (!annotationCanvas || !annotationOverlay || !contentWrapper) return;
            
            annotationCtx = annotationCanvas.getContext('2d');
            
            // Enable high-quality rendering for crisp strokes
            annotationCtx.imageSmoothingEnabled = true;
            annotationCtx.imageSmoothingQuality = 'high';
            
            // Initial canvas sizing
            resizeCanvas();
            
            // Resize observer for dynamic content
            const resizeObserver = new ResizeObserver(() => {
                resizeCanvas();
            });
            resizeObserver.observe(contentWrapper);
            resizeObserver.observe(document.getElementById('contentArea'));
            
            // Load existing annotations
            loadAnnotations();
            
            // Setup event listeners (always attached, but only work in annotation mode)
            setupAnnotationEvents();
            
            // Setup tool buttons
            setupToolButtons();
        }
        
        function enterAnnotationMode() {
            if (!annotationFilePath) {
                alert('Please open a file first');
                return;
            }
            
            annotationModeActive = true;
            
            // Show toolbar and badge
            document.getElementById('annotationToolbar').classList.add('visible');
            document.getElementById('annotationModeBadge').classList.add('visible');
            
            // Activate overlay (enable pointer events)
            if (annotationOverlay) {
                annotationOverlay.classList.add('active');
            }
            
            // Also activate PDF annotation overlay if in PDF mode
            const pdfOverlay = document.getElementById('pdfAnnotationOverlay');
            if (pdfOverlay) {
                pdfOverlay.classList.add('active');
            }
            
            // Hide the annotation indicator if visible
            const indicator = document.querySelector('.has-annotations-indicator');
            if (indicator) indicator.style.display = 'none';
            
            // Resize canvas to match current content
            resizeCanvas();
            
            // Set cursor
            updateCursor();
        }
        
        function exitAnnotationMode() {
            annotationModeActive = false;
            
            // Hide toolbar and badge
            document.getElementById('annotationToolbar').classList.remove('visible');
            document.getElementById('annotationModeBadge').classList.remove('visible');
            
            // Deactivate overlay (disable pointer events, allow normal scrolling)
            annotationOverlay.classList.remove('active');
            
            // Also deactivate PDF annotation overlay
            const pdfOverlay = document.getElementById('pdfAnnotationOverlay');
            if (pdfOverlay) pdfOverlay.classList.remove('active');
            
            // Save annotations
            saveAnnotations();
            
            // Show indicator if there are annotations
            updateAnnotationIndicator();
        }
        
        // This is called by the Annotate button in toolbar
        function openAnnotation() {
            if (annotationModeActive) {
                exitAnnotationMode();
            } else {
                enterAnnotationMode();
            }
        }
        
        function setupAnnotationEvents() {
            if (!annotationCanvas) return;
            
            // Pointer events for Apple Pencil support
            annotationCanvas.addEventListener('pointerdown', handlePointerDown, { passive: false });
            annotationCanvas.addEventListener('pointermove', handlePointerMove, { passive: false });
            annotationCanvas.addEventListener('pointerup', handlePointerUp);
            annotationCanvas.addEventListener('pointerleave', handlePointerUp);
            annotationCanvas.addEventListener('pointercancel', handlePointerUp);
            
            // Palm rejection: block touch events while pen is active
            // This prevents palm touches from causing scroll while writing
            annotationCanvas.addEventListener('touchstart', handleTouchForPalmRejection, { passive: false });
            annotationCanvas.addEventListener('touchmove', handleTouchForPalmRejection, { passive: false });
        }
        
        function handleTouchForPalmRejection(e) {
            if (!annotationModeActive) return;
            
            // If pen is currently active (drawing), block ALL touch events (palm rejection)
            if (penIsActive) {
                e.preventDefault();
                e.stopPropagation();
                return;
            }
            
            // If pen was recently used (within 300ms), also block touch to prevent palm jitter
            // Otherwise, allow touch for scrolling
        }
        
        function handlePointerDown(e) {
            if (!annotationModeActive) return;
            
            // Check pointer type - only draw with Apple Pencil or mouse
            const isPencil = e.pointerType === 'pen';
            const isMouse = e.pointerType === 'mouse';
            const isTouch = e.pointerType === 'touch';
            
            // If it's a finger touch, check palm rejection
            if (isTouch) {
                // If pen is active, this is likely a palm - block it
                if (penIsActive) {
                    e.preventDefault();
                    return;
                }
                // Otherwise allow scrolling
                return;
            }
            
            // Pen or mouse detected - activate palm rejection
            if (isPencil) {
                penIsActive = true;
                // Clear any pending timeout
                if (palmRejectionTimeout) {
                    clearTimeout(palmRejectionTimeout);
                    palmRejectionTimeout = null;
                }
            }
            
            // Only draw with Apple Pencil or mouse
            isDrawing = true;
            e.preventDefault();
            
            // Get coordinates relative to canvas
            // Canvas scrolls with content, so getBoundingClientRect already accounts for scroll
            const rect = annotationCanvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            
            currentStroke = {
                tool: currentTool,
                color: currentColor,
                size: currentSize,
                points: [{
                    x: x,
                    y: y,
                    pressure: e.pressure || 0.5
                }]
            };
            
            // Clear redo stack when new stroke starts
            redoStack = [];
        }
        
        function handlePointerMove(e) {
            if (!isDrawing || !currentStroke || !annotationModeActive) return;
            
            // Ignore touch (finger) - only pen/mouse should draw
            if (e.pointerType === 'touch') return;
            
            e.preventDefault();
            
            // Canvas scrolls with content, so getBoundingClientRect already accounts for scroll
            const rect = annotationCanvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            
            currentStroke.points.push({
                x: x,
                y: y,
                pressure: e.pressure || 0.5
            });
            
            // Draw only the new segment (fast incremental drawing)
            drawStrokeIncremental(annotationCtx, currentStroke);
        }
        
        function handlePointerUp(e) {
            // Deactivate palm rejection when pen lifts
            // Use a small delay so palm doesn't immediately trigger scroll
            if (e.pointerType === 'pen' && penIsActive) {
                if (palmRejectionTimeout) clearTimeout(palmRejectionTimeout);
                palmRejectionTimeout = setTimeout(() => {
                    penIsActive = false;
                }, 300); // 300ms grace period after pen lifts
            }
            
            if (!isDrawing || !currentStroke) return;
            
            isDrawing = false;
            
            if (currentStroke.points.length > 1) {
                annotationStrokes.push(currentStroke);
                hasUnsavedChanges = true;
                autoSaveAnnotations();
            }
            
            currentStroke = null;
            redrawAllStrokes();
        }
        
        function updateCursor() {
            if (!annotationCanvas) return;
            
            if (currentTool === 'eraser') {
                annotationCanvas.style.cursor = 'cell';
            } else if (currentTool === 'highlighter') {
                annotationCanvas.style.cursor = 'text';
            } else {
                annotationCanvas.style.cursor = 'crosshair';
            }
        }
        
        function setupToolButtons() {
            // Prevent zoom on all toolbar buttons (touchstart preventDefault)
            document.querySelectorAll('#annotationToolbar button, #annotationToolbar select').forEach(el => {
                el.addEventListener('touchstart', (e) => {
                    // Allow the touch but prevent zoom
                    e.stopPropagation();
                }, { passive: true });
                
                // Prevent double-tap zoom by handling touchend
                el.addEventListener('touchend', (e) => {
                    e.preventDefault();
                    // Trigger click for buttons
                    if (el.tagName === 'BUTTON') {
                        el.click();
                    }
                }, { passive: false });
            });
            
            // Tool buttons
            document.querySelectorAll('#annotationToolbar .tool-btn[data-tool]').forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll('#annotationToolbar .tool-btn[data-tool]').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    currentTool = btn.dataset.tool;
                    updateCursor();
                });
            });
            
            // Color buttons
            document.querySelectorAll('#annotationToolbar .color-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll('#annotationToolbar .color-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    currentColor = btn.dataset.color;
                });
            });
            
            // Stroke size slider
            const strokeSlider = document.getElementById('strokeSize');
            const strokeSizeDot = document.getElementById('strokeSizeDot');
            const strokeSizeLabel = document.getElementById('strokeSizeLabel');
            
            function updateStrokeSizePreview(size) {
                if (strokeSizeDot) {
                    // Scale the dot preview (max 24px in a 28px container)
                    const dotSize = Math.max(4, Math.min(24, size));
                    strokeSizeDot.style.width = dotSize + 'px';
                    strokeSizeDot.style.height = dotSize + 'px';
                }
                if (strokeSizeLabel) {
                    strokeSizeLabel.textContent = size;
                }
            }
            
            if (strokeSlider) {
                // Real-time update while sliding
                strokeSlider.addEventListener('input', (e) => {
                    currentSize = parseInt(e.target.value);
                    updateStrokeSizePreview(currentSize);
                });
                // Initialize preview
                updateStrokeSizePreview(currentSize);
            }
        }
        
        function annotationUndo() {
            if (annotationStrokes.length > 0) {
                const stroke = annotationStrokes.pop();
                redoStack.push(stroke);
                hasUnsavedChanges = true;
                redrawAllStrokes();
                autoSaveAnnotations();
            }
        }
        
        function annotationRedo() {
            if (redoStack.length > 0) {
                const stroke = redoStack.pop();
                annotationStrokes.push(stroke);
                hasUnsavedChanges = true;
                redrawAllStrokes();
                autoSaveAnnotations();
            }
        }
        
        function clearAnnotations() {
            if (annotationStrokes.length === 0) return;
            
            if (confirm('Clear all annotations? This cannot be undone.')) {
                annotationStrokes = [];
                redoStack = [];
                hasUnsavedChanges = true;
                redrawAllStrokes();
                
                // Delete from server
                if (annotationFilePath) {
                    fetch('/api/annotations/' + annotationFilePath, {
                        method: 'DELETE'
                    });
                }
            }
        }
        
        let saveTimeout = null;
        function autoSaveAnnotations() {
            // Debounce auto-save
            if (saveTimeout) clearTimeout(saveTimeout);
            saveTimeout = setTimeout(() => {
                saveAnnotations();
            }, 500); // Save faster for better reliability
        }
        
        function saveAnnotations() {
            if (!annotationFilePath || !annotationCanvas) return;
            
            fetch('/api/annotations/' + annotationFilePath, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    strokes: annotationStrokes,
                    canvasWidth: lastCanvasWidth,  // Save logical dimensions, not scaled
                    canvasHeight: lastCanvasHeight
                })
            }).then(res => res.json()).then(data => {
                if (data.success) {
                    hasUnsavedChanges = false;
                }
            }).catch(err => console.error('Save failed:', err));
        }
        
        function loadAnnotations() {
            if (!annotationFilePath) return;
            
            fetch('/api/annotations/' + annotationFilePath)
                .then(res => res.json())
                .then(data => {
                    if (data.strokes && data.strokes.length > 0) {
                        annotationStrokes = data.strokes;
                        
                        // If saved canvas was different size, we might need to scale
                        // For now, just redraw at current positions
                        redrawAllStrokes();
                        
                        // Show indicator that annotations exist
                        updateAnnotationIndicator();
                    }
                })
                .catch(err => console.error('Load failed:', err));
        }
        
        function updateAnnotationIndicator() {
            // Remove existing indicator
            const existing = document.querySelector('.has-annotations-indicator');
            if (existing) existing.remove();
            
            // Don't show if in annotation mode or no annotations
            if (annotationModeActive || !annotationFilePath || annotationStrokes.length === 0) return;
            
            const indicator = document.createElement('div');
            indicator.className = 'has-annotations-indicator';
            indicator.innerHTML = '✏️ ' + annotationStrokes.length + ' annotation' + (annotationStrokes.length > 1 ? 's' : '');
            indicator.onclick = enterAnnotationMode;
            document.body.appendChild(indicator);
        }
        
        // Initialize overlay when DOM is ready
        document.addEventListener('DOMContentLoaded', function() {
            // Wait a bit for content to render
            setTimeout(initAnnotationOverlay, 100);
        });
        
        // Keyboard shortcuts for annotation
        document.addEventListener('keydown', (e) => {
            // Global shortcuts
            if (e.key === 'Escape' && annotationModeActive) {
                exitAnnotationMode();
                return;
            }
            
            // Shortcuts only when annotation mode is active
            if (!annotationModeActive) return;
            
            // Undo: Ctrl+Z
            if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
                e.preventDefault();
                annotationUndo();
                return;
            }
            // Redo: Ctrl+Y or Ctrl+Shift+Z
            if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) {
                e.preventDefault();
                annotationRedo();
                return;
            }
            // Tool shortcuts
            if (e.key === 'p' || e.key === 'P') {
                document.querySelector('#annotationToolbar .tool-btn[data-tool="pen"]').click();
            }
            if (e.key === 'h' || e.key === 'H') {
                document.querySelector('#annotationToolbar .tool-btn[data-tool="highlighter"]').click();
            }
            if (e.key === 'e' || e.key === 'E') {
                document.querySelector('#annotationToolbar .tool-btn[data-tool="eraser"]').click();
            }
        });
        
        // ===== OFFLINE DOWNLOAD (ZIP) =====
        // No service worker needed - just downloads a ZIP file
        document.addEventListener('DOMContentLoaded', () => {
            const statusEl = document.getElementById('offlineStatus');
            if (statusEl) {
                statusEl.innerHTML = 'Creates ZIP with HTML files';
                statusEl.style.color = '#888';
            }
        });
    </script>
    
    <!-- Image Lightbox Modal -->
    <div id="imageLightbox" class="image-lightbox" onclick="closeLightbox(event)">
        <div class="lightbox-controls">
            <button class="lightbox-btn" onclick="zoomOut(event)" title="Zoom Out">−</button>
            <button class="lightbox-btn" onclick="zoomIn(event)" title="Zoom In">+</button>
            <button class="lightbox-btn" onclick="resetZoom(event)" title="Reset">⟲</button>
            <button class="lightbox-btn" onclick="closeLightbox(event)" title="Close">✕</button>
        </div>
        <img id="lightboxImage" src="" alt="Full size image" draggable="false">
        <div class="lightbox-zoom-info" id="zoomInfo">100%</div>
    </div>
    
    <script>
        // Image Lightbox functionality
        let currentZoom = 1;
        let isDragging = false;
        let startX, startY, translateX = 0, translateY = 0;
        
        // Make all content images clickable
        document.addEventListener('DOMContentLoaded', function() {
            const contentImages = document.querySelectorAll('.content img');
            contentImages.forEach(img => {
                img.addEventListener('click', function(e) {
                    e.preventDefault();
                    openLightbox(this.src);
                });
            });
        });
        
        function openLightbox(src) {
            const lightbox = document.getElementById('imageLightbox');
            const img = document.getElementById('lightboxImage');
            img.src = src;
            currentZoom = 1;
            translateX = 0;
            translateY = 0;
            updateImageTransform();
            lightbox.classList.add('active');
            document.body.style.overflow = 'hidden';
        }
        
        function closeLightbox(event) {
            if (event.target.id === 'imageLightbox' || event.target.classList.contains('lightbox-btn')) {
                const lightbox = document.getElementById('imageLightbox');
                lightbox.classList.remove('active');
                document.body.style.overflow = '';
                currentZoom = 1;
                translateX = 0;
                translateY = 0;
            }
        }
        
        function zoomIn(event) {
            event.stopPropagation();
            currentZoom = Math.min(currentZoom * 1.25, 5);
            updateImageTransform();
        }
        
        function zoomOut(event) {
            event.stopPropagation();
            currentZoom = Math.max(currentZoom / 1.25, 0.5);
            updateImageTransform();
        }
        
        function resetZoom(event) {
            event.stopPropagation();
            currentZoom = 1;
            translateX = 0;
            translateY = 0;
            updateImageTransform();
        }
        
        function updateImageTransform() {
            const img = document.getElementById('lightboxImage');
            img.style.transform = `translate(${translateX}px, ${translateY}px) scale(${currentZoom})`;
            img.classList.toggle('zoomed', currentZoom > 1);
            document.getElementById('zoomInfo').textContent = Math.round(currentZoom * 100) + '%';
        }
        
        // Mouse wheel zoom
        document.getElementById('imageLightbox').addEventListener('wheel', function(e) {
            if (!this.classList.contains('active')) return;
            e.preventDefault();
            if (e.deltaY < 0) {
                currentZoom = Math.min(currentZoom * 1.1, 5);
            } else {
                currentZoom = Math.max(currentZoom / 1.1, 0.5);
            }
            updateImageTransform();
        });
        
        // Drag to pan when zoomed
        const lightboxImg = document.getElementById('lightboxImage');
        lightboxImg.addEventListener('mousedown', function(e) {
            if (currentZoom > 1) {
                isDragging = true;
                startX = e.clientX - translateX;
                startY = e.clientY - translateY;
                this.style.cursor = 'grabbing';
            }
        });
        
        document.addEventListener('mousemove', function(e) {
            if (isDragging) {
                translateX = e.clientX - startX;
                translateY = e.clientY - startY;
                updateImageTransform();
            }
        });
        
        document.addEventListener('mouseup', function() {
            isDragging = false;
            lightboxImg.style.cursor = currentZoom > 1 ? 'grab' : 'zoom-out';
        });
        
        // ===== TOUCH PINCH-TO-ZOOM SUPPORT =====
        let lastTouchDist = 0;
        let isTouchPanning = false;
        let lastTouchPos = { x: 0, y: 0 };
        let pinchCenter = { x: 0, y: 0 };
        
        function getTouchDistance(touches) {
            if (touches.length < 2) return 0;
            const dx = touches[0].clientX - touches[1].clientX;
            const dy = touches[0].clientY - touches[1].clientY;
            return Math.sqrt(dx * dx + dy * dy);
        }
        
        function getTouchCenter(touches) {
            if (touches.length < 2) {
                return { x: touches[0].clientX, y: touches[0].clientY };
            }
            return {
                x: (touches[0].clientX + touches[1].clientX) / 2,
                y: (touches[0].clientY + touches[1].clientY) / 2
            };
        }
        
        const lightboxContainer = document.getElementById('imageLightbox');
        
        lightboxContainer.addEventListener('touchstart', function(e) {
            if (!this.classList.contains('active')) return;
            
            if (e.touches.length === 2) {
                // Pinch start
                e.preventDefault();
                lastTouchDist = getTouchDistance(e.touches);
                pinchCenter = getTouchCenter(e.touches);
                isTouchPanning = false;
            } else if (e.touches.length === 1) {
                // Single finger - prepare for pan or double-tap
                isTouchPanning = true;
                lastTouchPos = { x: e.touches[0].clientX, y: e.touches[0].clientY };
            }
        }, { passive: false });
        
        lightboxContainer.addEventListener('touchmove', function(e) {
            if (!this.classList.contains('active')) return;
            e.preventDefault();
            
            if (e.touches.length === 2) {
                // Pinch zoom with anchor point
                const newDist = getTouchDistance(e.touches);
                const newCenter = getTouchCenter(e.touches);
                
                if (lastTouchDist > 0) {
                    // Dampen the scale to prevent zoom acceleration at high zoom levels
                    let scale = newDist / lastTouchDist;
                    // Apply logarithmic dampening - more dampening at higher zoom
                    const dampening = 0.4 + (0.6 / (1 + currentZoom * 0.3));
                    scale = 1 + (scale - 1) * dampening;
                    const newZoom = Math.min(Math.max(currentZoom * scale, 0.5), 5);
                    
                    // Zoom toward pinch center
                    const img = document.getElementById('lightboxImage');
                    const rect = img.getBoundingClientRect();
                    const imgCenterX = rect.left + rect.width / 2;
                    const imgCenterY = rect.top + rect.height / 2;
                    
                    // Calculate offset from image center to pinch point
                    const offsetX = pinchCenter.x - imgCenterX;
                    const offsetY = pinchCenter.y - imgCenterY;
                    
                    // Adjust translation to zoom toward pinch center
                    const zoomDelta = newZoom / currentZoom;
                    translateX = translateX * zoomDelta - offsetX * (zoomDelta - 1);
                    translateY = translateY * zoomDelta - offsetY * (zoomDelta - 1);
                    
                    currentZoom = newZoom;
                    
                    // Also pan with the pinch movement
                    translateX += newCenter.x - pinchCenter.x;
                    translateY += newCenter.y - pinchCenter.y;
                    
                    updateImageTransform();
                }
                
                lastTouchDist = newDist;
                pinchCenter = newCenter;
                isTouchPanning = false;
                
            } else if (e.touches.length === 1 && isTouchPanning) {
                // Pan with single finger
                const dx = e.touches[0].clientX - lastTouchPos.x;
                const dy = e.touches[0].clientY - lastTouchPos.y;
                translateX += dx;
                translateY += dy;
                lastTouchPos = { x: e.touches[0].clientX, y: e.touches[0].clientY };
                updateImageTransform();
            }
        }, { passive: false });
        
        lightboxContainer.addEventListener('touchend', function(e) {
            if (e.touches.length === 0) {
                lastTouchDist = 0;
                
                // Double-tap to toggle zoom
                const now = Date.now();
                const lastTap = this.lastTapTime || 0;
                if (now - lastTap < 300 && isTouchPanning) {
                    // Double tap detected - zoom to tap point or reset
                    if (currentZoom > 1.1) {
                        currentZoom = 1;
                        translateX = 0;
                        translateY = 0;
                    } else {
                        // Zoom to the tapped point
                        const tapX = lastTouchPos.x;
                        const tapY = lastTouchPos.y;
                        const img = document.getElementById('lightboxImage');
                        const rect = img.getBoundingClientRect();
                        const imgCenterX = rect.left + rect.width / 2;
                        const imgCenterY = rect.top + rect.height / 2;
                        
                        const newZoom = 2.5;
                        const zoomDelta = newZoom / currentZoom;
                        const offsetX = tapX - imgCenterX;
                        const offsetY = tapY - imgCenterY;
                        
                        translateX = -offsetX * (zoomDelta - 1);
                        translateY = -offsetY * (zoomDelta - 1);
                        currentZoom = newZoom;
                    }
                    updateImageTransform();
                }
                this.lastTapTime = now;
                isTouchPanning = false;
            } else if (e.touches.length === 1) {
                // Switched from pinch to single finger
                lastTouchDist = 0;
                isTouchPanning = true;
                lastTouchPos = { x: e.touches[0].clientX, y: e.touches[0].clientY };
            }
        });
        
        // Keyboard controls
        document.addEventListener('keydown', function(e) {
            const lightbox = document.getElementById('imageLightbox');
            if (!lightbox.classList.contains('active')) return;
            
            if (e.key === 'Escape') {
                lightbox.classList.remove('active');
                document.body.style.overflow = '';
            } else if (e.key === '+' || e.key === '=') {
                zoomIn(e);
            } else if (e.key === '-') {
                zoomOut(e);
            } else if (e.key === '0') {
                resetZoom(e);
            }
        });
    </script>
</body>
</html>
'''

def natural_sort_key(s):
    """
    Natural sorting key that handles numbers properly.
    '1 Week' < '2 Week' < '10 Week' (not '1' < '10' < '2')
    Numbers come before letters, then alphabetical.
    """
    import re
    # Split string into numeric and non-numeric parts
    parts = re.split(r'(\d+)', s.lower())
    # Convert numeric parts to integers for proper comparison
    result = []
    for part in parts:
        if part.isdigit():
            result.append((0, int(part)))  # 0 = number (comes first)
        elif part:
            result.append((1, part))  # 1 = text (comes after numbers)
    return result


def get_file_tree(path, base_path, current_file="", depth=0):
    """Recursively build HTML file tree with collapsible folders"""
    items = []
    try:
        # Sort: folders first, then natural sort (numbers in correct order)
        entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), natural_sort_key(x)))
        
        for entry in entries:
            if entry.startswith('.'):
                continue
                
            full_path = os.path.join(path, entry)
            rel_path = os.path.relpath(full_path, base_path)
            
            if os.path.isdir(full_path):
                subtree = get_file_tree(full_path, base_path, current_file, depth + 1)
                if subtree:  # Only show folders that have content
                    # Always render collapsed - JS will restore state from localStorage
                    # This prevents flicker on page load/navigation
                    items.append(f'''<li class="folder-item" data-path="{rel_path}">
                        <div class="folder-header" onclick="toggleFolder(this)">
                            <span class="folder-icon collapsed">▼</span>
                            <span class="folder-name">{entry}</span>
                        </div>
                        <ul class="folder-children">{subtree}</ul>
                    </li>''')
            else:
                ext = entry.lower().rsplit('.', 1)[-1] if '.' in entry else ''
                if ext in ['md', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'txt', 'mp4', 'mkv', 'avi', 'mov', 'webm', 'mp3', 'wav', 'ogg']:
                    css_class = 'file-md' if ext == 'md' else 'file-pdf' if ext == 'pdf' else 'file-img' if ext in ['png','jpg','jpeg','gif','webp'] else 'file-video' if ext in ['mp4','mkv','avi','mov','webm','mp3','wav','ogg'] else 'file-txt'
                    active = 'active' if rel_path == current_file else ''
                    display_name = entry[:-3] if ext == 'md' else entry  # Remove .md extension for cleaner look
                    items.append(f'<li><a href="/view/{rel_path}" class="{css_class} {active}">{display_name}</a></li>')
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
    """Home page with dashboard showing vault structure"""
    tree = f'<ul>{get_file_tree(VAULT_PATH, VAULT_PATH)}</ul>'
    
    # Build dashboard content
    vault_name = get_vault_name()
    
    # Load all metadata once for completion tracking
    all_metadata = load_all_metadata()
    
    # Get folder structure for dashboard
    folders = []
    files_in_root = []
    total_md_files = 0
    total_folders = 0
    total_completed = 0
    
    try:
        for entry in sorted(os.listdir(VAULT_PATH), key=natural_sort_key):
            entry_path = os.path.join(VAULT_PATH, entry)
            rel_path = entry
            
            # Skip hidden files/folders
            if entry.startswith('.'):
                continue
                
            if os.path.isdir(entry_path):
                total_folders += 1
                # Count md files and completed files in this folder
                md_count = 0
                completed_count = 0
                subfolders = []
                for root, dirs, files in os.walk(entry_path):
                    # Skip hidden folders
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for f in files:
                        if f.endswith('.md'):
                            md_count += 1
                            # Check completion status
                            file_rel_path = os.path.relpath(os.path.join(root, f), VAULT_PATH)
                            if all_metadata.get(file_rel_path, {}).get('completed', False):
                                completed_count += 1
                    # Get immediate subfolders
                    if root == entry_path:
                        subfolders = [d for d in sorted(dirs, key=natural_sort_key) if not d.startswith('.')][:5]  # Max 5 subfolders shown
                
                total_md_files += md_count
                total_completed += completed_count
                folders.append({
                    'name': entry,
                    'path': rel_path,
                    'md_count': md_count,
                    'completed_count': completed_count,
                    'subfolders': subfolders
                })
            elif entry.endswith('.md'):
                total_md_files += 1
                is_completed = all_metadata.get(rel_path, {}).get('completed', False)
                if is_completed:
                    total_completed += 1
                files_in_root.append({
                    'name': entry[:-3],  # Remove .md
                    'path': rel_path,
                    'completed': is_completed
                })
    except Exception as e:
        pass
    
    # Build folder cards HTML
    folder_cards = []
    for folder in folders:
        subfolder_links = ''
        if folder['subfolders']:
            subfolder_items = []
            for sf in folder['subfolders']:
                sf_path = f"{folder['path']}/{sf}"
                subfolder_items.append(f'<a href="/view/{sf_path}" class="subfolder-link">📁 {sf}</a>')
            if len(folder['subfolders']) == 5:
                subfolder_items.append('<span class="more-indicator">...</span>')
            subfolder_links = '<div class="subfolder-list">' + ''.join(subfolder_items) + '</div>'
        
        # Calculate completion percentage
        md_count = folder['md_count']
        completed = folder['completed_count']
        pct = int((completed / md_count * 100)) if md_count > 0 else 0
        
        # Determine completion color
        if pct == 100:
            pct_color = '#22c55e'  # Green
            status_icon = '✅'
        elif pct >= 50:
            pct_color = '#f59e0b'  # Yellow/Orange
            status_icon = '📊'
        else:
            pct_color = '#6b7280'  # Gray
            status_icon = '📊'
        
        completion_html = f'''
            <div class="completion-bar">
                <div class="completion-fill" style="width: {pct}%; background: {pct_color};"></div>
            </div>
            <span class="completion-text">{status_icon} {completed}/{md_count} ({pct}%)</span>
        ''' if md_count > 0 else ''
        
        folder_cards.append(f'''
            <div class="folder-card" onclick="window.location='/view/{folder['path']}'">
                <div class="folder-card-header">
                    <span class="folder-icon">📂</span>
                    <span class="folder-title">{folder['name']}</span>
                </div>
                <div class="folder-card-stats">
                    <span class="stat-badge">📝 {folder['md_count']} notes</span>
                </div>
                <div class="folder-completion">
                    {completion_html}
                </div>
                {subfolder_links}
            </div>
        ''')
    
    # Build root files list if any
    root_files_html = ''
    if files_in_root:
        file_items = []
        for f in files_in_root[:10]:
            status = '✅' if f.get('completed') else '📄'
            file_items.append(f'<a href="/view/{f["path"]}" class="root-file-link">{status} {f["name"]}</a>')
        root_files_html = f'''
            <div class="root-files-section">
                <h3>📄 Files in Root</h3>
                <div class="root-files-list">{''.join(file_items)}</div>
            </div>
        '''
    
    content = f'''
    <style>
        .dashboard {{
            padding: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        .dashboard-header {{
            text-align: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #3c3c3c;
        }}
        .dashboard-header h1 {{
            font-size: 2em;
            margin-bottom: 10px;
            color: #e0e0e0;
        }}
        .dashboard-stats {{
            display: flex;
            gap: 20px;
            justify-content: center;
            margin-top: 15px;
        }}
        .dashboard-stat {{
            background: #2d2d2d;
            padding: 10px 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .dashboard-stat-value {{
            font-size: 1.5em;
            font-weight: bold;
            color: #7c3aed;
        }}
        .dashboard-stat-label {{
            font-size: 0.85em;
            color: #888;
        }}
        .folder-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 16px;
            margin-top: 20px;
        }}
        .folder-card {{
            background: #252526;
            border: 1px solid #3c3c3c;
            border-radius: 12px;
            padding: 16px;
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        .folder-card:hover {{
            border-color: #7c3aed;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.2);
        }}
        .folder-card-header {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
        }}
        .folder-icon {{
            font-size: 1.5em;
        }}
        .folder-title {{
            font-size: 1.1em;
            font-weight: 600;
            color: #e0e0e0;
            flex: 1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .folder-card-stats {{
            margin-bottom: 10px;
        }}
        .stat-badge {{
            background: #3c3c3c;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.8em;
            color: #aaa;
        }}
        .folder-completion {{
            margin: 10px 0;
        }}
        .completion-bar {{
            height: 6px;
            background: #1e1e1e;
            border-radius: 3px;
            overflow: hidden;
            margin-bottom: 6px;
        }}
        .completion-fill {{
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s ease;
        }}
        .completion-text {{
            font-size: 0.75em;
            color: #888;
        }}
        .subfolder-list {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid #3c3c3c;
        }}
        .subfolder-link {{
            background: #1e1e1e;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.75em;
            color: #888;
            text-decoration: none;
            transition: all 0.15s;
        }}
        .subfolder-link:hover {{
            background: #7c3aed;
            color: white;
        }}
        .more-indicator {{
            color: #666;
            padding: 4px;
        }}
        .root-files-section {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #3c3c3c;
        }}
        .root-files-section h3 {{
            margin-bottom: 15px;
            color: #e0e0e0;
        }}
        .root-files-list {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }}
        .root-file-link {{
            background: #2d2d2d;
            padding: 8px 14px;
            border-radius: 8px;
            color: #aaa;
            text-decoration: none;
            transition: all 0.15s;
        }}
        .root-file-link:hover {{
            background: #7c3aed;
            color: white;
        }}
        .keyboard-hints {{
            margin-top: 30px;
            padding: 15px;
            background: #1e1e1e;
            border-radius: 8px;
            font-size: 0.85em;
            color: #666;
            text-align: center;
        }}
        .keyboard-hints kbd {{
            background: #3c3c3c;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
        }}
        /* Study Dashboard Styles */
        .study-dashboard {{
            background: #1e1e1e;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 30px;
            border: 1px solid #3c3c3c;
        }}
        .study-dashboard-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
        }}
        .study-dashboard-header h2 {{
            font-size: 1.4em;
            color: #e0e0e0;
            margin: 0;
        }}
        .study-stats-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 24px;
        }}
        .study-stat-card {{
            background: #252526;
            border-radius: 12px;
            padding: 16px;
            text-align: center;
            border: 1px solid #3c3c3c;
        }}
        .study-stat-card.streak {{
            border-color: #f97316;
            background: linear-gradient(135deg, #f9731610, #252526);
        }}
        .study-stat-card.due {{
            border-color: #3b82f6;
            background: linear-gradient(135deg, #3b82f610, #252526);
        }}
        .study-stat-card.mastery {{
            border-color: #22c55e;
            background: linear-gradient(135deg, #22c55e10, #252526);
        }}
        .study-stat-card.weak {{
            border-color: #ef4444;
            background: linear-gradient(135deg, #ef444410, #252526);
        }}
        .study-stat-value {{
            font-size: 2em;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .study-stat-card.streak .study-stat-value {{ color: #f97316; }}
        .study-stat-card.due .study-stat-value {{ color: #3b82f6; }}
        .study-stat-card.mastery .study-stat-value {{ color: #22c55e; }}
        .study-stat-card.weak .study-stat-value {{ color: #ef4444; }}
        .study-stat-label {{
            font-size: 0.85em;
            color: #888;
        }}
        .study-progress-section {{
            margin-bottom: 24px;
        }}
        .study-progress-section h3 {{
            font-size: 1em;
            color: #e0e0e0;
            margin: 0 0 12px 0;
        }}
        .study-progress-bar {{
            height: 10px;
            background: #252526;
            border-radius: 5px;
            overflow: hidden;
            margin-bottom: 8px;
        }}
        .study-progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, #22c55e, #3b82f6);
            border-radius: 5px;
            transition: width 0.5s ease;
        }}
        .study-progress-label {{
            display: flex;
            justify-content: space-between;
            font-size: 0.85em;
            color: #888;
        }}
        .study-tabs {{
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
            background: #252526;
            padding: 6px;
            border-radius: 10px;
        }}
        .study-tab {{
            flex: 1;
            padding: 10px 16px;
            border: none;
            background: transparent;
            color: #888;
            cursor: pointer;
            border-radius: 8px;
            font-size: 0.9em;
            transition: all 0.2s;
        }}
        .study-tab:hover {{
            background: #3c3c3c;
            color: #e0e0e0;
        }}
        .study-tab.active {{
            background: #7c3aed;
            color: white;
        }}
        .study-chart-summary {{
            text-align: center;
            margin-bottom: 16px;
            color: #aaa;
            font-size: 0.9em;
        }}
        .study-chart-summary strong {{
            color: #e0e0e0;
        }}
        .study-chart {{
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            height: 120px;
            padding: 0 10px;
            gap: 8px;
        }}
        .study-chart-bar-container {{
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            height: 100%;
        }}
        .study-chart-bar {{
            width: 100%;
            max-width: 50px;
            background: linear-gradient(180deg, #7c3aed, #5b21b6);
            border-radius: 4px 4px 0 0;
            transition: height 0.3s ease;
            position: relative;
        }}
        .study-chart-bar-label {{
            font-size: 0.75em;
            color: #666;
            margin-top: 8px;
        }}
        .study-heatmap-section {{
            margin-top: 24px;
        }}
        .study-heatmap-section h3 {{
            font-size: 1em;
            color: #e0e0e0;
            margin: 0 0 12px 0;
        }}
        .study-heatmap {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 6px;
        }}
        .study-heatmap-day {{
            aspect-ratio: 1;
            border-radius: 4px;
            background: #252526;
            transition: background 0.2s;
            cursor: pointer;
            position: relative;
        }}
        .study-heatmap-day:hover {{
            transform: scale(1.1);
            z-index: 10;
        }}
        .study-heatmap-day.level-1 {{ background: #22c55e30; }}
        .study-heatmap-day.level-2 {{ background: #22c55e60; }}
        .study-heatmap-day.level-3 {{ background: #22c55e90; }}
        .study-heatmap-day.level-4 {{ background: #22c55e; }}
        .heatmap-tooltip {{
            position: fixed;
            background: #1e1e1e;
            border: 1px solid #7c3aed;
            color: #e0e0e0;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.85em;
            pointer-events: none;
            z-index: 1000;
            white-space: nowrap;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            display: none;
        }}
        .heatmap-tooltip.visible {{
            display: block;
        }}
        .heatmap-tooltip .date {{
            color: #7c3aed;
            font-weight: 600;
        }}
        .heatmap-tooltip .count {{
            color: #22c55e;
        }}
        .study-loading {{
            text-align: center;
            padding: 40px;
            color: #666;
        }}
        .study-actions {{
            display: flex;
            gap: 12px;
            justify-content: center;
            margin-top: 24px;
            flex-wrap: wrap;
        }}
        .study-btn {{
            padding: 12px 24px;
            border: none;
            border-radius: 10px;
            font-size: 0.95em;
            cursor: pointer;
            transition: all 0.2s;
            font-weight: 500;
        }}
        .study-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .study-btn.primary {{
            background: linear-gradient(135deg, #7c3aed, #5b21b6);
            color: white;
        }}
        .study-btn.primary:hover:not(:disabled) {{
            background: linear-gradient(135deg, #8b5cf6, #6d28d9);
            transform: translateY(-2px);
        }}
        .study-btn.secondary {{
            background: #252526;
            color: #e0e0e0;
            border: 1px solid #3c3c3c;
        }}
        .study-btn.secondary:hover:not(:disabled) {{
            background: #3c3c3c;
            border-color: #7c3aed;
        }}
        @media (max-width: 768px) {{
            .study-stats-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
            .study-chart {{
                height: 80px;
            }}
            .study-actions {{
                flex-direction: column;
            }}
            .study-btn {{
                width: 100%;
            }}
        }}
    </style>
    
    <div class="dashboard">
        <div class="dashboard-header">
            <h1>📚 {vault_name}</h1>
            <div class="dashboard-stats">
                <div class="dashboard-stat">
                    <div class="dashboard-stat-value">{total_folders}</div>
                    <div class="dashboard-stat-label">Folders</div>
                </div>
                <div class="dashboard-stat">
                    <div class="dashboard-stat-value">{total_md_files}</div>
                    <div class="dashboard-stat-label">Notes</div>
                </div>
                <div class="dashboard-stat" style="background: {'#22c55e20' if total_completed == total_md_files and total_md_files > 0 else '#2d2d2d'};">
                    <div class="dashboard-stat-value" style="color: {'#22c55e' if total_completed == total_md_files and total_md_files > 0 else '#7c3aed'};">
                        {total_completed}/{total_md_files}
                    </div>
                    <div class="dashboard-stat-label">✅ Completed</div>
                </div>
            </div>
        </div>
        
        <!-- Study Dashboard Section -->
        <div class="study-dashboard" id="studyDashboard">
            <div class="study-loading">Loading study stats...</div>
        </div>
        
        <div class="folder-grid">
            {''.join(folder_cards)}
        </div>
        
        {root_files_html}
        
        <div class="keyboard-hints">
            <kbd>Ctrl+B</kbd> Toggle sidebar &nbsp;•&nbsp;
            <kbd>Ctrl+→</kbd> Toggle graph &nbsp;•&nbsp;
            <kbd>F11</kbd> Fullscreen &nbsp;•&nbsp;
            <kbd>Esc</kbd> Show sidebar
        </div>
    </div>
    
    <script>
        let currentStudyView = 'daily';
        let studyData = null;
        
        async function loadStudyDashboard() {{
            try {{
                const response = await fetch('/api/study/dashboard');
                const data = await response.json();
                
                if (data.success) {{
                    studyData = data.dashboard;
                    renderStudyDashboard();
                    setupHeatmapTooltips();
                }} else {{
                    document.getElementById('studyDashboard').innerHTML = '<div class="study-loading">No study data available</div>';
                }}
            }} catch (err) {{
                console.error('Failed to load study dashboard:', err);
                document.getElementById('studyDashboard').innerHTML = '<div class="study-loading">Failed to load study stats</div>';
            }}
        }}
        
        function renderStudyDashboard() {{
            const d = studyData;
            const container = document.getElementById('studyDashboard');
            
            // Build heatmap (last 28 days in 4 rows of 7)
            let heatmapHtml = '';
            const dates = Object.keys(d.heatmap).sort().slice(-28);
            for (const date of dates) {{
                const count = d.heatmap[date];
                let level = 0;
                if (count > 0) level = 1;
                if (count >= 10) level = 2;
                if (count >= 25) level = 3;
                if (count >= 50) level = 4;
                heatmapHtml += `<div class="study-heatmap-day level-${{level}}" data-date="${{date}}" data-count="${{count}}"></div>`;
            }}
            
            const progressPct = Math.min(100, (d.today.reviewed / d.today.goal) * 100);
            
            // Build chart for current view
            const viewData = d[currentStudyView] || d.daily;
            const maxCards = Math.max(...viewData.map(v => v.cards), 1);
            
            let chartHtml = viewData.map(item => {{
                const height = (item.cards / maxCards) * 100;
                return `
                    <div class="study-chart-bar-container">
                        <div class="study-chart-bar" style="height: ${{Math.max(height, 2)}}%;" title="${{item.cards}} cards"></div>
                        <div class="study-chart-bar-label">${{item.label}}</div>
                    </div>
                `;
            }}).join('');
            
            // Calculate totals for current view
            const viewTotals = viewData.reduce((acc, item) => ({{
                cards: acc.cards + item.cards,
                correct: acc.correct + item.correct,
                wrong: acc.wrong + item.wrong
            }}), {{ cards: 0, correct: 0, wrong: 0 }});
            
            container.innerHTML = `
                <div class="study-dashboard-header">
                    <h2>📊 Study Dashboard</h2>
                </div>
                
                <div class="study-stats-grid">
                    <div class="study-stat-card streak">
                        <div class="study-stat-value">🔥 ${{d.streak}}</div>
                        <div class="study-stat-label">Day Streak</div>
                    </div>
                    <div class="study-stat-card due">
                        <div class="study-stat-value">${{d.dueCount}}</div>
                        <div class="study-stat-label">Cards Due</div>
                    </div>
                    <div class="study-stat-card mastery">
                        <div class="study-stat-value">${{d.masteryPercent}}%</div>
                        <div class="study-stat-label">Mastery</div>
                    </div>
                    <div class="study-stat-card weak">
                        <div class="study-stat-value">${{d.weakCards}}</div>
                        <div class="study-stat-label">Weak Cards</div>
                    </div>
                </div>
                
                <div class="study-progress-section">
                    <h3>Today's Progress</h3>
                    <div class="study-progress-bar">
                        <div class="study-progress-fill" style="width: ${{progressPct}}%"></div>
                    </div>
                    <div class="study-progress-label">
                        <span>${{d.today.reviewed}} reviewed (${{d.today.correct}} ✓ / ${{d.today.wrong}} ✗)</span>
                        <span>Goal: ${{d.today.goal}}</span>
                    </div>
                </div>
                
                <div class="study-tabs">
                    <button class="study-tab ${{currentStudyView === 'hourly' ? 'active' : ''}}" onclick="switchStudyView('hourly')">⏰ Hourly</button>
                    <button class="study-tab ${{currentStudyView === 'daily' ? 'active' : ''}}" onclick="switchStudyView('daily')">📅 Daily</button>
                    <button class="study-tab ${{currentStudyView === 'weekly' ? 'active' : ''}}" onclick="switchStudyView('weekly')">📆 Weekly</button>
                    <button class="study-tab ${{currentStudyView === 'monthly' ? 'active' : ''}}" onclick="switchStudyView('monthly')">🗓️ Monthly</button>
                </div>
                
                <div class="study-chart-summary">
                    <strong>${{viewTotals.cards}}</strong> cards &nbsp;•&nbsp; ✓ ${{viewTotals.correct}} &nbsp;•&nbsp; ✗ ${{viewTotals.wrong}}
                </div>
                
                <div class="study-chart">
                    ${{chartHtml}}
                </div>
                
                <div class="study-heatmap-section">
                    <h3>Activity Heatmap</h3>
                    <div class="study-heatmap">${{heatmapHtml}}</div>
                </div>
                
                <div id="heatmapTooltip" class="heatmap-tooltip"></div>
                
                <div class="study-actions">
                    <button class="study-btn primary" onclick="startVaultReview('due')" ${{d.dueCount === 0 ? 'disabled' : ''}}>
                        📚 Review Due Cards (${{d.dueCount}})
                    </button>
                    <button class="study-btn secondary" onclick="startVaultReview('weak')" ${{d.weakCards === 0 ? 'disabled' : ''}}>
                        🎯 Review Mistakes (${{d.weakCards}})
                    </button>
                </div>
            `;
        }}
        
        function switchStudyView(view) {{
            currentStudyView = view;
            renderStudyDashboard();
        }}
        
        function setupHeatmapTooltips() {{
            const tooltip = document.getElementById('heatmapTooltip');
            const heatmapDays = document.querySelectorAll('.study-heatmap-day');
            
            heatmapDays.forEach(day => {{
                const date = day.dataset.date;
                const count = day.dataset.count;
                
                // Format date nicely
                const dateObj = new Date(date);
                const formatted = dateObj.toLocaleDateString('en-US', {{ 
                    weekday: 'short', 
                    month: 'short', 
                    day: 'numeric' 
                }});
                
                const showTooltip = (e) => {{
                    tooltip.innerHTML = `<span class="date">${{formatted}}</span><br><span class="count">${{count}} cards</span>`;
                    tooltip.classList.add('visible');
                    
                    // Position tooltip
                    const rect = day.getBoundingClientRect();
                    let left = rect.left + rect.width / 2;
                    let top = rect.top - 50;
                    
                    // Keep tooltip in viewport
                    if (left < 100) left = 100;
                    if (left > window.innerWidth - 100) left = window.innerWidth - 100;
                    if (top < 10) top = rect.bottom + 10;
                    
                    tooltip.style.left = left + 'px';
                    tooltip.style.top = top + 'px';
                    tooltip.style.transform = 'translateX(-50%)';
                }};
                
                const hideTooltip = () => {{
                    tooltip.classList.remove('visible');
                }};
                
                // Desktop hover
                day.addEventListener('mouseenter', showTooltip);
                day.addEventListener('mouseleave', hideTooltip);
                
                // Mobile touch
                day.addEventListener('touchstart', (e) => {{
                    e.preventDefault();
                    showTooltip(e);
                    setTimeout(hideTooltip, 2000);
                }});
            }});
        }}
        
        function startVaultReview(mode) {{
            // Redirect to first note with flashcards for vault-wide review
            window.location.href = '/review?scope=vault&mode=' + mode;
        }}
        
        // Load on page load
        document.addEventListener('DOMContentLoaded', loadStudyDashboard);
    </script>
    '''
    return render_template_string(HTML_TEMPLATE, title="Home", tree=tree, content=content, vault_name=vault_name, is_markdown=False)


def view_folder(filepath, full_path):
    """View a folder as a dashboard with its contents"""
    tree = f'<ul>{get_file_tree(VAULT_PATH, VAULT_PATH, filepath)}</ul>'
    folder_name = os.path.basename(filepath)
    
    # Load all metadata for completion tracking
    all_metadata = load_all_metadata()
    
    # Get folder contents
    subfolders = []
    md_files = []
    other_files = []
    total_completed = 0
    
    try:
        for entry in sorted(os.listdir(full_path), key=natural_sort_key):
            if entry.startswith('.'):
                continue
            
            entry_path = os.path.join(full_path, entry)
            rel_path = f"{filepath}/{entry}"
            
            if os.path.isdir(entry_path):
                # Count contents and completion in subfolder
                md_count = 0
                completed_count = 0
                for root, dirs, files in os.walk(entry_path):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for f in files:
                        if f.endswith('.md'):
                            md_count += 1
                            file_rel = os.path.relpath(os.path.join(root, f), VAULT_PATH)
                            if all_metadata.get(file_rel, {}).get('completed', False):
                                completed_count += 1
                
                subfolders.append({
                    'name': entry,
                    'path': rel_path,
                    'md_count': md_count,
                    'completed_count': completed_count
                })
            elif entry.endswith('.md'):
                is_completed = all_metadata.get(rel_path, {}).get('completed', False)
                if is_completed:
                    total_completed += 1
                md_files.append({
                    'name': entry[:-3],
                    'path': rel_path,
                    'completed': is_completed
                })
            else:
                ext = entry.rsplit('.', 1)[-1].lower() if '.' in entry else ''
                if ext in ['pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp']:
                    other_files.append({
                        'name': entry,
                        'path': rel_path,
                        'type': 'pdf' if ext == 'pdf' else 'image'
                    })
    except Exception as e:
        pass
    
    # Calculate total completion across all subfolders + direct files
    all_md_count = len(md_files) + sum(sf['md_count'] for sf in subfolders)
    all_completed_count = total_completed + sum(sf['completed_count'] for sf in subfolders)
    completion_pct = int((all_completed_count / all_md_count * 100)) if all_md_count > 0 else 0
    is_folder_complete = all_completed_count == all_md_count and all_md_count > 0
    
    # Build subfolder cards
    subfolder_cards = []
    for sf in subfolders:
        md_count = sf['md_count']
        completed = sf['completed_count']
        pct = int((completed / md_count * 100)) if md_count > 0 else 0
        pct_color = '#22c55e' if pct == 100 else '#f59e0b' if pct >= 50 else '#6b7280'
        
        completion_html = f'''
            <div class="completion-bar" style="margin-top: 8px;">
                <div class="completion-fill" style="width: {pct}%; background: {pct_color};"></div>
            </div>
            <span class="completion-text">{completed}/{md_count}</span>
        ''' if md_count > 0 else ''
        
        subfolder_cards.append(f'''
            <a href="/view/{sf['path']}" class="folder-card-link">
                <div class="folder-card small">
                    <div class="folder-card-header">
                        <span class="folder-icon">📁</span>
                        <span class="folder-title">{sf['name']}</span>
                    </div>
                    <div class="folder-card-stats">
                        <span class="stat-badge">📝 {sf['md_count']} notes</span>
                    </div>
                    {completion_html}
                </div>
            </a>
        ''')
    
    # Build MD files list
    md_files_html = ''
    if md_files:
        file_items = []
        for f in md_files:
            status = '✅' if f.get('completed') else '📄'
            completed_class = 'completed' if f.get('completed') else ''
            file_items.append(f'<a href="/view/{f["path"]}" class="file-card {completed_class}">{status} {f["name"]}</a>')
        
        completed_in_folder = sum(1 for f in md_files if f.get('completed'))
        md_files_html = f'''
            <div class="files-section">
                <h3>📄 Notes ({completed_in_folder}/{len(md_files)} completed)</h3>
                <div class="files-grid">{''.join(file_items)}</div>
            </div>
        '''
    
    # Build other files list
    other_files_html = ''
    if other_files:
        file_items = []
        for f in other_files:
            icon = '📕' if f['type'] == 'pdf' else '🖼️'
            file_items.append(f'<a href="/view/{f["path"]}" class="file-card other">{icon} {f["name"]}</a>')
        other_files_html = f'''
            <div class="files-section">
                <h3>📎 Other Files ({len(other_files)})</h3>
                <div class="files-grid">{''.join(file_items)}</div>
            </div>
        '''
    
    # Breadcrumb navigation
    breadcrumb_parts = filepath.split('/')
    breadcrumbs = ['<a href="/" class="breadcrumb-link">🏠 Home</a>']
    current_path = ''
    for i, part in enumerate(breadcrumb_parts):
        current_path = f"{current_path}/{part}" if current_path else part
        if i == len(breadcrumb_parts) - 1:
            breadcrumbs.append(f'<span class="breadcrumb-current">{part}</span>')
        else:
            breadcrumbs.append(f'<a href="/view/{current_path}" class="breadcrumb-link">{part}</a>')
    
    content = f'''
    <style>
        .folder-dashboard {{
            padding: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        .folder-header {{
            margin-bottom: 20px;
        }}
        .breadcrumb {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 15px;
            font-size: 0.9em;
        }}
        .breadcrumb-link {{
            color: #888;
            text-decoration: none;
        }}
        .breadcrumb-link:hover {{
            color: #7c3aed;
        }}
        .breadcrumb::after {{
            content: none;
        }}
        .breadcrumb > *:not(:last-child)::after {{
            content: ' / ';
            color: #555;
            margin-left: 8px;
        }}
        .breadcrumb-current {{
            color: #e0e0e0;
            font-weight: 600;
        }}
        .folder-title-bar {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .folder-title-bar h1 {{
            font-size: 1.8em;
            color: #e0e0e0;
            margin: 0;
        }}
        .folder-stats {{
            display: flex;
            gap: 15px;
            margin-top: 10px;
        }}
        .folder-stat {{
            background: #2d2d2d;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85em;
            color: #aaa;
        }}
        .subfolders-section {{
            margin-top: 25px;
        }}
        .subfolders-section h3, .files-section h3 {{
            color: #e0e0e0;
            margin-bottom: 15px;
            font-size: 1.1em;
        }}
        .subfolders-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 12px;
        }}
        .folder-card-link {{
            text-decoration: none;
        }}
        .folder-card.small {{
            background: #252526;
            border: 1px solid #3c3c3c;
            border-radius: 10px;
            padding: 14px;
            transition: all 0.2s ease;
        }}
        .folder-card.small:hover {{
            border-color: #7c3aed;
            transform: translateY(-2px);
        }}
        .folder-card.small .folder-card-header {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
        }}
        .folder-card.small .folder-icon {{
            font-size: 1.2em;
        }}
        .folder-card.small .folder-title {{
            font-size: 0.95em;
            font-weight: 500;
            color: #e0e0e0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .folder-card.small .stat-badge {{
            background: #3c3c3c;
            padding: 3px 8px;
            border-radius: 10px;
            font-size: 0.75em;
            color: #888;
        }}
        .files-section {{
            margin-top: 25px;
        }}
        .files-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 10px;
        }}
        .file-card {{
            background: #252526;
            border: 1px solid #3c3c3c;
            border-radius: 8px;
            padding: 12px 14px;
            color: #aaa;
            text-decoration: none;
            transition: all 0.15s;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: block;
        }}
        .file-card:hover {{
            background: #7c3aed;
            color: white;
            border-color: #7c3aed;
        }}
        .file-card.other {{
            background: #1e1e1e;
        }}
        .file-card.completed {{
            background: #22c55e15;
            border-color: #22c55e40;
        }}
        .file-card.completed:hover {{
            background: #22c55e;
            border-color: #22c55e;
        }}
        /* Folder Study Dashboard Styles */
        .folder-study-dashboard {{
            background: #1e1e1e;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 24px;
            border: 1px solid #3c3c3c;
        }}
        .folder-study-dashboard h2 {{
            font-size: 1.3em;
            color: #e0e0e0;
            margin: 0 0 20px 0;
        }}
        .folder-study-stats {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 20px;
        }}
        .folder-study-stat {{
            background: #252526;
            border-radius: 10px;
            padding: 14px;
            text-align: center;
            border: 1px solid #3c3c3c;
        }}
        .folder-study-stat.due {{ border-color: #3b82f6; }}
        .folder-study-stat.mastery {{ border-color: #22c55e; }}
        .folder-study-stat.weak {{ border-color: #ef4444; }}
        .folder-study-stat.total {{ border-color: #7c3aed; }}
        .folder-study-stat-value {{
            font-size: 1.8em;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .folder-study-stat.due .folder-study-stat-value {{ color: #3b82f6; }}
        .folder-study-stat.mastery .folder-study-stat-value {{ color: #22c55e; }}
        .folder-study-stat.weak .folder-study-stat-value {{ color: #ef4444; }}
        .folder-study-stat.total .folder-study-stat-value {{ color: #7c3aed; }}
        .folder-study-stat-label {{
            font-size: 0.8em;
            color: #888;
        }}
        .folder-study-actions {{
            display: flex;
            gap: 12px;
            justify-content: center;
            flex-wrap: wrap;
        }}
        .folder-study-btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-size: 0.9em;
            cursor: pointer;
            transition: all 0.2s;
            font-weight: 500;
        }}
        .folder-study-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .folder-study-btn.primary {{
            background: linear-gradient(135deg, #7c3aed, #5b21b6);
            color: white;
        }}
        .folder-study-btn.primary:hover:not(:disabled) {{
            background: linear-gradient(135deg, #8b5cf6, #6d28d9);
            transform: translateY(-2px);
        }}
        .folder-study-btn.secondary {{
            background: #252526;
            color: #e0e0e0;
            border: 1px solid #3c3c3c;
        }}
        .folder-study-btn.secondary:hover:not(:disabled) {{
            background: #3c3c3c;
            border-color: #7c3aed;
        }}
        .folder-study-loading {{
            text-align: center;
            padding: 20px;
            color: #666;
        }}
        @media (max-width: 768px) {{
            .folder-study-stats {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
    </style>
    
    <div class="folder-dashboard">
        <div class="folder-header">
            <div class="breadcrumb">
                {' '.join(breadcrumbs)}
            </div>
            <div class="folder-title-bar">
                <span style="font-size: 1.5em;">📂</span>
                <h1>{folder_name}</h1>
            </div>
            <div class="folder-stats">
                <span class="folder-stat">📁 {len(subfolders)} folders</span>
                <span class="folder-stat">📄 {len(md_files)} notes</span>
                <span class="folder-stat" style="background: {'#22c55e30' if is_folder_complete else '#2d2d2d'}; color: {'#22c55e' if is_folder_complete else '#aaa'};">{'✅' if is_folder_complete else '📊'} {all_completed_count}/{all_md_count} completed</span>
            </div>
        </div>
        
        <!-- Folder Study Dashboard -->
        <div class="folder-study-dashboard" id="folderStudyDashboard">
            <div class="folder-study-loading">Loading study stats...</div>
        </div>
        
        {"<div class='subfolders-section'><h3>📁 Subfolders</h3><div class='subfolders-grid'>" + ''.join(subfolder_cards) + "</div></div>" if subfolders else ""}
        
        {md_files_html}
        {other_files_html}
    </div>
    
    <script>
        const folderPath = '{filepath}';
        
        async function loadFolderStudyDashboard() {{
            try {{
                const response = await fetch('/api/study/dashboard/' + encodeURIComponent(folderPath));
                const data = await response.json();
                
                if (data.success) {{
                    renderFolderStudyDashboard(data.dashboard);
                }} else {{
                    document.getElementById('folderStudyDashboard').innerHTML = '<div class="folder-study-loading">No study data for this folder</div>';
                }}
            }} catch (err) {{
                console.error('Failed to load folder study dashboard:', err);
                document.getElementById('folderStudyDashboard').innerHTML = '<div class="folder-study-loading">Failed to load study stats</div>';
            }}
        }}
        
        function renderFolderStudyDashboard(d) {{
            const container = document.getElementById('folderStudyDashboard');
            
            if (d.totalCards === 0) {{
                container.innerHTML = `
                    <h2>📊 Study Dashboard</h2>
                    <div class="folder-study-loading">No flashcards in this folder yet</div>
                `;
                return;
            }}
            
            container.innerHTML = `
                <h2>📊 Study Dashboard</h2>
                
                <div class="folder-study-stats">
                    <div class="folder-study-stat total">
                        <div class="folder-study-stat-value">${{d.totalCards}}</div>
                        <div class="folder-study-stat-label">Total Cards</div>
                    </div>
                    <div class="folder-study-stat due">
                        <div class="folder-study-stat-value">${{d.dueCount}}</div>
                        <div class="folder-study-stat-label">Cards Due</div>
                    </div>
                    <div class="folder-study-stat mastery">
                        <div class="folder-study-stat-value">${{d.masteryPercent}}%</div>
                        <div class="folder-study-stat-label">Mastery</div>
                    </div>
                    <div class="folder-study-stat weak">
                        <div class="folder-study-stat-value">${{d.weakCards}}</div>
                        <div class="folder-study-stat-label">Weak Cards</div>
                    </div>
                </div>
                
                <div class="folder-study-actions">
                    <button class="folder-study-btn primary" onclick="startFolderReview('due')" ${{d.dueCount === 0 ? 'disabled' : ''}}>
                        📚 Review Due Cards (${{d.dueCount}})
                    </button>
                    <button class="folder-study-btn secondary" onclick="startFolderReview('weak')" ${{d.weakCards === 0 ? 'disabled' : ''}}>
                        🎯 Review Mistakes (${{d.weakCards}})
                    </button>
                </div>
            `;
        }}
        
        function startFolderReview(mode) {{
            window.location.href = '/review?scope=folder&folder=' + encodeURIComponent(folderPath) + '&mode=' + mode;
        }}
        
        document.addEventListener('DOMContentLoaded', loadFolderStudyDashboard);
    </script>
    '''
    
    return render_template_string(HTML_TEMPLATE, title=folder_name, tree=tree, content=content, vault_name=get_vault_name(), is_markdown=False)


@app.route('/review')
def review_page():
    """Review page for vault-wide or folder-specific flashcard review"""
    scope = request.args.get('scope', 'vault')  # 'vault' or 'folder'
    folder_path = request.args.get('folder', '')
    mode = request.args.get('mode', 'due')  # 'due' or 'weak'
    
    tree = f'<ul>{get_file_tree(VAULT_PATH, VAULT_PATH)}</ul>'
    vault_name = get_vault_name()
    
    # Determine title and scope text
    if scope == 'folder' and folder_path:
        folder_name = os.path.basename(folder_path)
        title = f"Review - {folder_name}"
        scope_text = folder_name
        back_url = f"/view/{folder_path}"
    else:
        title = "Review - Entire Vault"
        scope_text = "Entire Vault"
        folder_path = ""
        back_url = "/"
    
    mode_text = "Due Cards" if mode == 'due' else "Weak Cards (Mistakes)"
    
    content = f'''
    <style>
        .review-page {{
            padding: 20px;
            max-width: 900px;
            margin: 0 auto;
        }}
        .review-header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .review-header h1 {{
            font-size: 1.8em;
            color: #e0e0e0;
            margin-bottom: 10px;
        }}
        .review-scope {{
            color: #888;
            font-size: 0.95em;
        }}
        .review-scope strong {{
            color: #7c3aed;
        }}
        .review-container {{
            background: #1e1e1e;
            border-radius: 16px;
            padding: 30px;
            border: 1px solid #3c3c3c;
            min-height: 400px;
        }}
        .review-loading {{
            text-align: center;
            padding: 60px;
            color: #888;
        }}
        .review-loading .spinner {{
            font-size: 2em;
            margin-bottom: 20px;
            animation: spin 1s linear infinite;
        }}
        @keyframes spin {{
            from {{ transform: rotate(0deg); }}
            to {{ transform: rotate(360deg); }}
        }}
        .review-empty {{
            text-align: center;
            padding: 60px;
        }}
        .review-empty h2 {{
            color: #22c55e;
            font-size: 1.5em;
            margin-bottom: 15px;
        }}
        .review-empty p {{
            color: #888;
            margin-bottom: 20px;
        }}
        .review-card {{
            background: #252526;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            border: 1px solid #3c3c3c;
        }}
        .review-card-source {{
            font-size: 0.8em;
            color: #666;
            margin-bottom: 15px;
        }}
        .review-card-question {{
            font-size: 1.2em;
            color: #e0e0e0;
            margin-bottom: 20px;
            line-height: 1.6;
        }}
        .review-card-answer {{
            background: #1e1e1e;
            padding: 20px;
            border-radius: 8px;
            color: #e0e0e0;
            display: none;
            line-height: 1.6;
        }}
        .review-card-answer.visible {{
            display: block;
        }}
        .review-actions {{
            display: flex;
            gap: 12px;
            justify-content: center;
            margin-top: 20px;
            flex-wrap: wrap;
        }}
        .review-btn {{
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 0.95em;
            cursor: pointer;
            transition: all 0.2s;
            font-weight: 500;
        }}
        .review-btn.show {{
            background: #7c3aed;
            color: white;
        }}
        .review-btn.show:hover {{
            background: #8b5cf6;
        }}
        .review-btn.wrong {{
            background: #ef4444;
            color: white;
        }}
        .review-btn.wrong:hover {{
            background: #dc2626;
        }}
        .review-btn.hard {{
            background: #f59e0b;
            color: white;
        }}
        .review-btn.hard:hover {{
            background: #d97706;
        }}
        .review-btn.good {{
            background: #22c55e;
            color: white;
        }}
        .review-btn.good:hover {{
            background: #16a34a;
        }}
        .review-btn.easy {{
            background: #3b82f6;
            color: white;
        }}
        .review-btn.easy:hover {{
            background: #2563eb;
        }}
        .review-btn.back {{
            background: #3c3c3c;
            color: #e0e0e0;
        }}
        .review-btn.back:hover {{
            background: #4c4c4c;
        }}
        .review-progress {{
            margin-bottom: 20px;
        }}
        .review-progress-bar {{
            height: 8px;
            background: #252526;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 8px;
        }}
        .review-progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, #7c3aed, #22c55e);
            border-radius: 4px;
            transition: width 0.3s ease;
        }}
        .review-progress-text {{
            font-size: 0.85em;
            color: #888;
            text-align: center;
        }}
        .review-stats {{
            display: flex;
            gap: 20px;
            justify-content: center;
            margin-top: 20px;
            font-size: 0.9em;
        }}
        .review-stats span {{
            color: #888;
        }}
        .review-stats .correct {{
            color: #22c55e;
        }}
        .review-stats .wrong {{
            color: #ef4444;
        }}
    </style>
    
    <div class="review-page">
        <div class="review-header">
            <h1>📚 {mode_text}</h1>
            <p class="review-scope">Scope: <strong>{scope_text}</strong></p>
        </div>
        
        <div class="review-container" id="reviewContainer">
            <div class="review-loading">
                <div class="spinner">⏳</div>
                <p>Loading cards...</p>
            </div>
        </div>
        
        <div style="text-align: center; margin-top: 20px;">
            <a href="{back_url}" class="review-btn back">← Back</a>
        </div>
    </div>
    
    <script>
        const reviewScope = '{scope}';
        const reviewFolder = '{folder_path}';
        const reviewMode = '{mode}';
        
        let allCards = [];
        let currentCardIndex = 0;
        let stats = {{ correct: 0, wrong: 0 }};
        let answerShown = false;
        
        async function loadReviewCards() {{
            try {{
                let url = '/api/study/review-cards?mode=' + reviewMode;
                if (reviewScope === 'folder' && reviewFolder) {{
                    url += '&folder=' + encodeURIComponent(reviewFolder);
                }}
                
                const response = await fetch(url);
                const data = await response.json();
                
                if (data.success && data.cards.length > 0) {{
                    allCards = data.cards;
                    currentCardIndex = 0;
                    renderCard();
                }} else {{
                    showEmpty();
                }}
            }} catch (err) {{
                console.error('Failed to load cards:', err);
                document.getElementById('reviewContainer').innerHTML = `
                    <div class="review-empty">
                        <h2>❌ Error</h2>
                        <p>Failed to load review cards. Please try again.</p>
                    </div>
                `;
            }}
        }}
        
        function showEmpty() {{
            const mode_msg = reviewMode === 'weak' ? 'No weak cards' : 'No cards due';
            document.getElementById('reviewContainer').innerHTML = `
                <div class="review-empty">
                    <h2>🎉 ${{mode_msg}}!</h2>
                    <p>Great job! You're all caught up.</p>
                    <a href="${{reviewScope === 'folder' ? '/view/' + reviewFolder : '/'}}" class="review-btn show">
                        ← Back to ${{reviewScope === 'folder' ? 'Folder' : 'Home'}}
                    </a>
                </div>
            `;
        }}
        
        function renderCard() {{
            if (currentCardIndex >= allCards.length) {{
                showComplete();
                return;
            }}
            
            const card = allCards[currentCardIndex];
            const progress = Math.round((currentCardIndex / allCards.length) * 100);
            
            answerShown = false;
            
            document.getElementById('reviewContainer').innerHTML = `
                <div class="review-progress">
                    <div class="review-progress-bar">
                        <div class="review-progress-fill" style="width: ${{progress}}%"></div>
                    </div>
                    <p class="review-progress-text">${{currentCardIndex + 1}} / ${{allCards.length}}</p>
                </div>
                
                <div class="review-card">
                    <p class="review-card-source">📄 ${{card.source}}</p>
                    <div class="review-card-question">${{card.question}}</div>
                    <div class="review-card-answer" id="cardAnswer">${{card.answer}}</div>
                </div>
                
                <div class="review-actions" id="reviewActions">
                    <button class="review-btn show" onclick="showAnswer()">Show Answer</button>
                </div>
                
                <div class="review-stats">
                    <span class="correct">✓ ${{stats.correct}}</span>
                    <span class="wrong">✗ ${{stats.wrong}}</span>
                </div>
            `;
        }}
        
        function showAnswer() {{
            document.getElementById('cardAnswer').classList.add('visible');
            answerShown = true;
            
            document.getElementById('reviewActions').innerHTML = `
                <button class="review-btn wrong" onclick="rateCard(1)">✗ Wrong</button>
                <button class="review-btn hard" onclick="rateCard(2)">Hard</button>
                <button class="review-btn good" onclick="rateCard(3)">Good</button>
                <button class="review-btn easy" onclick="rateCard(4)">Easy</button>
            `;
        }}
        
        async function rateCard(rating) {{
            const card = allCards[currentCardIndex];
            
            // Track stats
            if (rating === 1) {{
                stats.wrong++;
            }} else {{
                stats.correct++;
            }}
            
            // Submit rating to API
            try {{
                await fetch('/api/study/rate-card', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        filepath: card.filepath,
                        cardKey: card.cardKey,
                        rating: rating
                    }})
                }});
            }} catch (err) {{
                console.error('Failed to submit rating:', err);
            }}
            
            currentCardIndex++;
            renderCard();
        }}
        
        function showComplete() {{
            const total = stats.correct + stats.wrong;
            const pct = total > 0 ? Math.round((stats.correct / total) * 100) : 0;
            
            document.getElementById('reviewContainer').innerHTML = `
                <div class="review-empty">
                    <h2>🎉 Session Complete!</h2>
                    <p>You reviewed <strong>${{total}}</strong> cards.</p>
                    <div class="review-stats" style="margin: 20px 0;">
                        <span class="correct">✓ ${{stats.correct}} correct</span>
                        <span class="wrong">✗ ${{stats.wrong}} wrong</span>
                        <span>(${{pct}}% accuracy)</span>
                    </div>
                    <a href="${{reviewScope === 'folder' ? '/view/' + reviewFolder : '/'}}" class="review-btn show">
                        ← Back to ${{reviewScope === 'folder' ? 'Folder' : 'Home'}}
                    </a>
                </div>
            `;
        }}
        
        document.addEventListener('DOMContentLoaded', loadReviewCards);
    </script>
    '''
    
    return render_template_string(HTML_TEMPLATE, title=title, tree=tree, content=content, vault_name=vault_name, is_markdown=False)


@app.route('/view/<path:filepath>')
def view_file(filepath):
    """View a specific file or folder"""
    full_path = os.path.join(VAULT_PATH, filepath)
    
    # Security check - prevent directory traversal
    if not os.path.abspath(full_path).startswith(os.path.abspath(VAULT_PATH)):
        abort(403)
    
    if not os.path.exists(full_path):
        abort(404)
    
    # Handle directory view
    if os.path.isdir(full_path):
        return view_folder(filepath, full_path)
    
    ext = filepath.lower().rsplit('.', 1)[-1] if '.' in filepath else ''
    tree = f'<ul>{get_file_tree(VAULT_PATH, VAULT_PATH, filepath)}</ul>'
    filename = os.path.basename(filepath)
    
    if ext == 'md':
        with open(full_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        
        # Get metadata from JSON (info panel saves here) - takes priority
        json_meta = get_file_metadata(filepath)
        
        # Parse YAML frontmatter as fallback
        frontmatter_meta = {'completed': False, 'revision_count': 0}
        if md_content.startswith('---'):
            parts = md_content.split('---', 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    fm_data = yaml.safe_load(parts[1])
                    if fm_data:
                        frontmatter_meta['completed'] = fm_data.get('completed', False)
                        frontmatter_meta['revision_count'] = fm_data.get('revision_count', 0)
                except:
                    pass
                md_content = parts[2]
        
        # Merge: JSON metadata takes priority over frontmatter
        display_meta = {
            'completed': json_meta.get('completed', frontmatter_meta['completed']),
            'revision_count': json_meta.get('revision_count', frontmatter_meta['revision_count'])
        }
        
        # Convert Obsidian callouts before markdown processing
        md_content = convert_obsidian_callouts(md_content)
        
        # Fix list continuation (numbered items after nested bullets)
        md_content = fix_list_continuation(md_content)
        
        # Convert task lists (- [ ] and - [x]) to checkboxes
        md_content = convert_task_lists(md_content)
        
        # Protect math expressions before markdown processing
        md_content, math_placeholders = protect_math_expressions(md_content)
        
        # Convert markdown to HTML
        html_content = markdown.markdown(
            md_content, 
            extensions=['tables', 'fenced_code', 'toc', 'nl2br', 'sane_lists']
        )
        
        # Restore math expressions after markdown processing
        html_content = restore_math_expressions(html_content, math_placeholders)
        
        # Convert Obsidian [[wiki-links]] to HTML links
        current_dir = os.path.dirname(filepath)
        html_content = convert_obsidian_links(html_content, current_dir)
        
        # Add title with metadata badges (uses merged metadata: JSON > frontmatter)
        completed_badge = '<span class="meta-badge completed" title="Completed">✅ Completed</span>' if display_meta['completed'] else '<span class="meta-badge incomplete" title="Not completed">📝 In Progress</span>'
        revision_badge = f'<span class="meta-badge revision" title="Revision count">🔄 Rev {display_meta["revision_count"]}</span>' if display_meta['revision_count'] > 0 else ''
        title_html = f'<div class="title-with-meta"><h1>{filename.replace(".md", "")}</h1><div class="meta-badges">{completed_badge}{revision_badge}</div></div>'
        
        # Generate page navigation (parent, siblings, children)
        nav_data = get_page_navigation(filepath)
        nav_html = render_page_navigation(nav_data, current_dir)
        
        return render_template_string(
            HTML_TEMPLATE, 
            title=filename, 
            tree=tree, 
            content=title_html + html_content + nav_html,
            vault_name=get_vault_name(),
            is_markdown=True,
            file_path=filepath,
            full_path=os.path.join(VAULT_PATH, filepath)
        )
    
    elif ext == 'pdf':
        # Embed PDF using PDF.js for cross-platform compatibility (especially iOS)
        pdf_full_path = os.path.join(VAULT_PATH, filepath)
        pdf_content = f'''
        <style>
            .pdf-container {{
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                display: flex;
                flex-direction: column;
                background: #525659;
            }}
            .pdf-header {{
                background: #323639;
                color: #fff;
                padding: 10px 20px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 15px;
                flex-shrink: 0;
                z-index: 50;
            }}
            .pdf-title {{
                font-size: 16px;
                font-weight: 500;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }}
            .pdf-actions {{
                display: flex;
                gap: 10px;
                align-items: center;
            }}
            .pdf-actions button, .pdf-actions a {{
                background: #4a4d50;
                color: #fff;
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
                text-decoration: none;
                transition: background 0.2s;
            }}
            .pdf-actions button:hover, .pdf-actions a:hover {{
                background: #5a5d60;
            }}
            .pdf-nav {{
                display: flex;
                align-items: center;
                gap: 8px;
                background: #4a4d50;
                padding: 4px 12px;
                border-radius: 6px;
            }}
            .pdf-nav button {{
                background: transparent;
                padding: 4px 8px;
                font-size: 16px;
            }}
            .pdf-nav button:hover {{
                background: #5a5d60;
            }}
            .pdf-copy-path {{
                background: #4a4d50;
                border: none;
                padding: 6px 10px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
                color: #fff;
                transition: background 0.2s;
            }}
            .pdf-copy-path:hover {{
                background: #5a5d60;
            }}
            .pdf-copy-path.copied {{
                background: #2e7d32;
            }}
            .pdf-page-info {{
                font-size: 13px;
                min-width: 80px;
                text-align: center;
            }}
            .pdf-viewer {{
                flex: 1;
                overflow: auto;
                display: flex;
                flex-direction: column;
                align-items: center;
                padding: 20px;
                gap: 20px;
            }}
            .pdf-viewer.zoomed-in {{
                align-items: flex-start;
            }}
            .pdf-page {{
                background: #fff;
                box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            }}
            .pdf-loading {{
                color: #fff;
                font-size: 18px;
                padding: 40px;
            }}
            
            /* PDF annotation overlay - positioned to cover all pages */
            .pdf-viewer {{
                position: relative;
            }}
            .pdf-annotation-overlay {{
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                pointer-events: none;
                z-index: 10;
            }}
            .pdf-annotation-overlay.active {{
                pointer-events: auto;
            }}
            .pdf-annotation-overlay.active canvas {{
                touch-action: none;
                cursor: crosshair;
            }}
            .pdf-annotation-overlay canvas {{
                position: absolute;
                top: 0;
                left: 0;
                touch-action: pan-x pan-y;
            }}
            
            /* Override content styles for PDF view */
            .content {{
                padding: 0 !important;
                max-width: 100% !important;
                overflow: hidden;
            }}
            .content-wrapper {{
                background: #525659;
            }}
            /* Hide main toolbar - PDF has its own header buttons */
            .toolbar {{
                display: none !important;
            }}
            /* Hide regular annotation overlay - PDF uses its own inside pdf-viewer */
            #annotationOverlay {{
                display: none !important;
            }}
            
            /* Metadata Modal */
            .metadata-modal {{
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: rgba(0,0,0,0.7);
                z-index: 1000;
                align-items: center;
                justify-content: center;
            }}
            .metadata-modal.visible {{
                display: flex;
            }}
            .metadata-modal-content {{
                background: #2d2d2d;
                border-radius: 12px;
                max-width: 500px;
                width: 90%;
                max-height: 90vh;
                overflow-y: auto;
                box-shadow: 0 10px 40px rgba(0,0,0,0.5);
            }}
            .metadata-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 15px 20px;
                border-bottom: 1px solid #444;
            }}
            .metadata-header h3 {{
                margin: 0;
                color: #fff;
                font-size: 18px;
            }}
            .metadata-close {{
                background: none;
                border: none;
                color: #888;
                font-size: 20px;
                cursor: pointer;
                padding: 5px;
            }}
            .metadata-close:hover {{
                color: #fff;
            }}
            .metadata-body {{
                padding: 20px;
            }}
            .metadata-field {{
                margin-bottom: 15px;
            }}
            .metadata-field label {{
                display: block;
                color: #aaa;
                font-size: 13px;
                margin-bottom: 5px;
            }}
            .metadata-field input[type="text"],
            .metadata-field input[type="date"],
            .metadata-field input[type="number"],
            .metadata-field textarea {{
                width: 100%;
                padding: 10px;
                border: 1px solid #444;
                border-radius: 6px;
                background: #1e1e1e;
                color: #fff;
                font-size: 14px;
            }}
            .metadata-field input:focus,
            .metadata-field textarea:focus {{
                outline: none;
                border-color: #0078d4;
            }}
            .metadata-field input[type="checkbox"] {{
                width: 18px;
                height: 18px;
                margin-right: 8px;
                vertical-align: middle;
            }}
            .metadata-field .checkbox-label {{
                color: #fff;
                font-size: 15px;
                vertical-align: middle;
            }}
            .metadata-footer {{
                padding: 15px 20px;
                border-top: 1px solid #444;
                display: flex;
                gap: 10px;
                justify-content: flex-end;
            }}
            .metadata-save {{
                background: #0078d4;
                color: #fff;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
            }}
            .metadata-save:hover {{
                background: #006abc;
            }}
            .metadata-cancel {{
                background: #444;
                color: #fff;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
            }}
            .metadata-cancel:hover {{
                background: #555;
            }}
            
            @media (max-width: 768px) {{
                .pdf-header {{
                    padding: 8px 15px;
                    flex-wrap: wrap;
                    gap: 10px;
                }}
                .pdf-title {{
                    font-size: 14px;
                    max-width: 150px;
                }}
                .pdf-actions {{
                    flex-wrap: wrap;
                }}
                .pdf-actions button, .pdf-actions a {{
                    padding: 6px 12px;
                    font-size: 12px;
                }}
                .pdf-viewer {{
                    padding: 10px;
                    gap: 10px;
                }}
            }}
        </style>
        
        <!-- PDF.js library -->
        <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
        
        <div class="pdf-container">
            <div class="pdf-header">
                <span class="pdf-title" title="{filepath}">📕 {filepath}</span>
                <button class="pdf-copy-path" onclick="copyPdfPath()" title="Copy path">📋</button>
                <div class="pdf-actions">
                    <div class="pdf-nav">
                        <button onclick="pdfZoomOut()" title="Zoom Out">−</button>
                        <span id="pdfZoomLevel">100%</span>
                        <button onclick="pdfZoomIn()" title="Zoom In">+</button>
                    </div>
                    <a href="/raw/{filepath}" target="_blank" title="Open in new tab">↗</a>
                    <a href="/raw/{filepath}" download="{filename}" title="Download PDF">📥</a>
                    <button onclick="openAnnotation()" title="Annotate">✏️</button>
                    <button onclick="openMetadataModal()" title="File Info">ℹ️</button>
                    <button onclick="toggleFullscreen()" title="Fullscreen">⛶</button>
                </div>
            </div>
            <div class="pdf-viewer" id="pdfViewer">
                <div class="pdf-loading">Loading PDF...</div>
                <!-- PDF annotation overlay - inside viewer so it scrolls with pages -->
                <div id="pdfAnnotationOverlay" class="pdf-annotation-overlay">
                    <canvas id="pdfAnnotationCanvas"></canvas>
                </div>
            </div>
        </div>
        
        <!-- Metadata Modal for PDF -->
        <div id="metadataModal" class="metadata-modal">
            <div class="metadata-modal-content">
                <div class="metadata-header">
                    <h3>ℹ️ File Metadata</h3>
                    <button class="metadata-close" onclick="closeMetadataModal()">✕</button>
                </div>
                <div class="metadata-body">
                    <div class="metadata-field">
                        <label>
                            <input type="checkbox" id="metaCompleted"> 
                            <span class="checkbox-label">✅ Completed</span>
                        </label>
                    </div>
                    <div class="metadata-field">
                        <label>📅 Created Date</label>
                        <input type="date" id="metaCreatedDate">
                    </div>
                    <div class="metadata-field">
                        <label>🔗 Source</label>
                        <input type="text" id="metaSource" placeholder="URL or reference...">
                    </div>
                    <div class="metadata-field">
                        <label>🔄 Revision Count</label>
                        <input type="number" id="metaRevisionCount" min="0" value="0">
                    </div>
                    <div class="metadata-field">
                        <label>📝 Summary (short)</label>
                        <input type="text" id="metaSummary" placeholder="Brief summary...">
                    </div>
                    <div class="metadata-field">
                        <label>📄 One Paragraph Summary</label>
                        <textarea id="metaOneParaSummary" rows="4" placeholder="Detailed summary..."></textarea>
                    </div>
                </div>
                <div class="metadata-footer">
                    <button class="metadata-save" onclick="saveMetadata()">💾 Save</button>
                    <button class="metadata-cancel" onclick="closeMetadataModal()">Cancel</button>
                </div>
            </div>
        </div>
        
        <script>
            // Configure PDF.js worker
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
            
            let pdfDoc = null;
            let currentScale = 1.5;
            const minScale = 0.1;  // Allow very small zoom for full-page view
            const maxScale = 4.0;
            const isMobilePdf = window.innerWidth <= 768 || 'ontouchstart' in window;
            
            async function loadPDF() {{
                const url = '/raw/{filepath}';
                const viewer = document.getElementById('pdfViewer');
                
                try {{
                    pdfDoc = await pdfjsLib.getDocument(url).promise;
                    viewer.innerHTML = '';
                    
                    // On mobile, calculate scale to fit width
                    if (isMobilePdf) {{
                        const firstPage = await pdfDoc.getPage(1);
                        const defaultViewport = firstPage.getViewport({{ scale: 1.0 }});
                        // Use window width minus padding (more reliable than viewer.clientWidth on initial load)
                        const availableWidth = window.innerWidth - 40; // 20px padding each side
                        currentScale = availableWidth / defaultViewport.width;
                        // Clamp to reasonable bounds (allow smaller for wide PDFs)
                        currentScale = Math.max(minScale, Math.min(currentScale, 1.5));
                        console.log('Mobile PDF scale:', currentScale, 'Page width:', defaultViewport.width, 'Available:', availableWidth);
                    }}
                    
                    // Render all pages
                    for (let i = 1; i <= pdfDoc.numPages; i++) {{
                        await renderPage(i, viewer);
                    }}
                }} catch (error) {{
                    viewer.innerHTML = '<div class="pdf-loading">Error loading PDF: ' + error.message + '</div>';
                }}
            }}
            
            async function renderPage(pageNum, container) {{
                const page = await pdfDoc.getPage(pageNum);
                const viewport = page.getViewport({{ scale: currentScale }});
                
                // Use device pixel ratio for crisp rendering on high-DPI screens
                const pixelRatio = window.devicePixelRatio || 1;
                
                const canvas = document.createElement('canvas');
                canvas.className = 'pdf-page';
                
                // Set canvas size at higher resolution
                canvas.width = Math.floor(viewport.width * pixelRatio);
                canvas.height = Math.floor(viewport.height * pixelRatio);
                
                // Scale down with CSS for display
                canvas.style.width = viewport.width + 'px';
                canvas.style.height = viewport.height + 'px';
                
                const context = canvas.getContext('2d');
                // Scale context to match pixel ratio
                context.scale(pixelRatio, pixelRatio);
                
                await page.render({{
                    canvasContext: context,
                    viewport: viewport
                }}).promise;
                
                container.appendChild(canvas);
            }}
            
            async function rerender() {{
                if (!pdfDoc) return;
                const viewer = document.getElementById('pdfViewer');
                const scrollTop = viewer.scrollTop;
                const scrollLeft = viewer.scrollLeft;
                viewer.innerHTML = '';
                
                for (let i = 1; i <= pdfDoc.numPages; i++) {{
                    await renderPage(i, viewer);
                }}
                
                // Check if content is wider than viewer (needs horizontal scroll)
                const firstPage = viewer.querySelector('.pdf-page');
                if (firstPage && firstPage.offsetWidth > viewer.clientWidth) {{
                    viewer.classList.add('zoomed-in');
                }} else {{
                    viewer.classList.remove('zoomed-in');
                }}
                
                viewer.scrollTop = scrollTop;
                viewer.scrollLeft = scrollLeft;
                // Show zoom as percentage (100% = fit to width on mobile)
                const baseScale = isMobilePdf ? (window.innerWidth - 40) / 595 : 1.5; // 595 is typical A4 width in points
                document.getElementById('pdfZoomLevel').textContent = Math.round(currentScale / baseScale * 100) + '%';
            }}
            
            function pdfZoomIn() {{
                if (currentScale < maxScale) {{
                    currentScale += 0.25;
                    rerender();
                }}
            }}
            
            function pdfZoomOut() {{
                if (currentScale > minScale) {{
                    currentScale -= 0.25;
                    rerender();
                }}
            }}
            
            // Pinch-to-zoom support for touch devices
            let initialPinchDistance = null;
            let initialScale = null;
            let pinchScaleFactor = 1;
            
            function getPinchDistance(touches) {{
                const dx = touches[0].clientX - touches[1].clientX;
                const dy = touches[0].clientY - touches[1].clientY;
                return Math.sqrt(dx * dx + dy * dy);
            }}
            
            function handlePinchStart(e) {{
                if (e.touches.length === 2) {{
                    initialPinchDistance = getPinchDistance(e.touches);
                    initialScale = currentScale;
                    pinchScaleFactor = 1;
                }}
            }}
            
            function handlePinchMove(e) {{
                if (e.touches.length === 2 && initialPinchDistance) {{
                    e.preventDefault();
                    const currentDistance = getPinchDistance(e.touches);
                    pinchScaleFactor = currentDistance / initialPinchDistance;
                    
                    // Calculate what the new scale would be
                    let newScale = initialScale * pinchScaleFactor;
                    newScale = Math.max(minScale, Math.min(newScale, maxScale));
                    
                    // Adjust pinchScaleFactor if we hit bounds
                    pinchScaleFactor = newScale / initialScale;
                    
                    // Apply CSS transform for instant visual feedback
                    const pages = document.querySelectorAll('.pdf-page');
                    pages.forEach(page => {{
                        page.style.transform = `scale(${{pinchScaleFactor}})`;
                        page.style.transformOrigin = 'center top';
                    }});
                    
                    // Update zoom display
                    const baseScale = isMobilePdf ? (window.innerWidth - 40) / 595 : 1.5;
                    document.getElementById('pdfZoomLevel').textContent = Math.round(newScale / baseScale * 100) + '%';
                }}
            }}
            
            function handlePinchEnd(e) {{
                if (e.touches.length < 2 && initialPinchDistance) {{
                    // Calculate final scale
                    let newScale = initialScale * pinchScaleFactor;
                    newScale = Math.max(minScale, Math.min(newScale, maxScale));
                    
                    // Reset CSS transforms
                    const pages = document.querySelectorAll('.pdf-page');
                    pages.forEach(page => {{
                        page.style.transform = '';
                        page.style.transformOrigin = '';
                    }});
                    
                    // Re-render at new scale for crisp quality
                    if (Math.abs(newScale - currentScale) > 0.02) {{
                        currentScale = newScale;
                        rerender();
                    }}
                    
                    initialPinchDistance = null;
                    initialScale = null;
                    pinchScaleFactor = 1;
                }}
            }}
            
            // Attach pinch handlers to PDF viewer
            document.addEventListener('DOMContentLoaded', () => {{
                const viewer = document.getElementById('pdfViewer');
                if (viewer) {{
                    viewer.addEventListener('touchstart', handlePinchStart, {{ passive: true }});
                    viewer.addEventListener('touchmove', handlePinchMove, {{ passive: false }});
                    viewer.addEventListener('touchend', handlePinchEnd, {{ passive: true }});
                }}
            }});
            
            function copyPdfPath() {{
                const path = '{pdf_full_path}';
                const btn = document.querySelector('.pdf-copy-path');
                
                // Try modern clipboard API first, fallback to execCommand
                if (navigator.clipboard && window.isSecureContext) {{
                    navigator.clipboard.writeText(path).then(() => showCopied(btn)).catch(() => fallbackCopy(path, btn));
                }} else {{
                    fallbackCopy(path, btn);
                }}
            }}
            
            function fallbackCopy(text, btn) {{
                const textArea = document.createElement('textarea');
                textArea.value = text;
                textArea.style.position = 'fixed';
                textArea.style.left = '-9999px';
                document.body.appendChild(textArea);
                textArea.select();
                try {{
                    document.execCommand('copy');
                    showCopied(btn);
                }} catch (e) {{
                    alert('Path: ' + text);
                }}
                document.body.removeChild(textArea);
            }}
            
            function showCopied(btn) {{
                btn.classList.add('copied');
                btn.textContent = '✓';
                setTimeout(() => {{
                    btn.classList.remove('copied');
                    btn.textContent = '📋';
                }}, 2000);
            }}
            
            // Initialize PDF annotation canvas
            function initPdfAnnotation() {{
                const viewer = document.getElementById('pdfViewer');
                const overlay = document.getElementById('pdfAnnotationOverlay');
                const canvas = document.getElementById('pdfAnnotationCanvas');
                
                if (!viewer || !overlay || !canvas) return;
                
                // Calculate total size of all pages
                const pages = viewer.querySelectorAll('.pdf-page');
                if (pages.length === 0) return;
                
                // Get the full scrollable area
                const totalWidth = viewer.scrollWidth;
                const totalHeight = viewer.scrollHeight;
                
                // Get device pixel ratio - use 1 to avoid huge canvas
                const dpr = 1;
                
                // Cap canvas size to browser limits (16384 is common max)
                const maxCanvasSize = 16384;
                const canvasWidth = Math.min(totalWidth, maxCanvasSize);
                const canvasHeight = Math.min(totalHeight, maxCanvasSize);
                
                // Size canvas (capped)
                canvas.width = canvasWidth * dpr;
                canvas.height = canvasHeight * dpr;
                canvas.style.width = canvasWidth + 'px';
                canvas.style.height = canvasHeight + 'px';
                
                const ctx = canvas.getContext('2d');
                if (!ctx) {{
                    console.error('Failed to get canvas context');
                    return;
                }}
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
                ctx.imageSmoothingEnabled = true;
                ctx.imageSmoothingQuality = 'high';
                
                // Set global PDF annotation mode flag
                window.isPdfAnnotationMode = true;
                
                // Reinitialize the annotation system with PDF elements
                annotationCanvas = canvas;
                annotationOverlay = overlay;
                contentWrapper = viewer;
                annotationCtx = ctx;
                lastCanvasWidth = canvasWidth;
                lastCanvasHeight = canvasHeight;
                
                // Re-attach event listeners to PDF canvas
                setupAnnotationEvents();
                setupToolButtons();
                
                // Load any saved annotations for this file
                loadAnnotations();
                
                console.log('PDF annotation ready');
            }}
            
            // Resize PDF annotation after re-render
            const originalRerender = rerender;
            rerender = async function() {{
                await originalRerender();
                // Re-add overlay since innerHTML was cleared
                const viewer = document.getElementById('pdfViewer');
                let overlay = document.getElementById('pdfAnnotationOverlay');
                if (!overlay) {{
                    overlay = document.createElement('div');
                    overlay.id = 'pdfAnnotationOverlay';
                    overlay.className = 'pdf-annotation-overlay';
                    const canvas = document.createElement('canvas');
                    canvas.id = 'pdfAnnotationCanvas';
                    overlay.appendChild(canvas);
                    viewer.appendChild(overlay);
                }}
                setTimeout(initPdfAnnotation, 100);
            }};
            
            // Load PDF on page load
            loadPDF().then(() => {{
                const viewer = document.getElementById('pdfViewer');
                
                // Create annotation overlay
                let overlay = document.getElementById('pdfAnnotationOverlay');
                if (overlay) overlay.remove();
                
                overlay = document.createElement('div');
                overlay.id = 'pdfAnnotationOverlay';
                overlay.className = 'pdf-annotation-overlay';
                overlay.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;z-index:10;';
                
                const canvas = document.createElement('canvas');
                canvas.id = 'pdfAnnotationCanvas';
                canvas.style.cssText = 'position:absolute;top:0;left:0;';
                overlay.appendChild(canvas);
                viewer.appendChild(overlay);
                
                setTimeout(initPdfAnnotation, 200);
                
                // Restore scroll position for PDF
                const savedScroll = localStorage.getItem('scroll_' + window.location.pathname);
                if (savedScroll) {{
                    setTimeout(() => {{ viewer.scrollTop = parseInt(savedScroll, 10); }}, 400);
                }}
                
                // Save scroll position on scroll
                viewer.addEventListener('scroll', () => {{
                    localStorage.setItem('scroll_' + window.location.pathname, viewer.scrollTop);
                }});
            }});
            
            // ============================================
            // METADATA FUNCTIONS
            // ============================================
            const pdfFilePath = '{filepath}';
            
            function openMetadataModal() {{
                const modal = document.getElementById('metadataModal');
                modal.classList.add('visible');
                loadMetadata();
            }}
            
            function closeMetadataModal() {{
                const modal = document.getElementById('metadataModal');
                modal.classList.remove('visible');
            }}
            
            async function loadMetadata() {{
                if (!pdfFilePath) return;
                
                try {{
                    const response = await fetch('/api/metadata/' + encodeURIComponent(pdfFilePath));
                    const data = await response.json();
                    
                    if (data.success && data.metadata) {{
                        const meta = data.metadata;
                        document.getElementById('metaCompleted').checked = meta.completed || false;
                        document.getElementById('metaCreatedDate').value = meta.created_date || '';
                        document.getElementById('metaSource').value = meta.source || '';
                        document.getElementById('metaRevisionCount').value = meta.revision_count || 0;
                        document.getElementById('metaSummary').value = meta.summary || '';
                        document.getElementById('metaOneParaSummary').value = meta.one_para_summary || '';
                    }}
                }} catch (err) {{
                    console.error('Failed to load metadata:', err);
                }}
            }}
            
            async function saveMetadata() {{
                if (!pdfFilePath) return;
                
                const metadata = {{
                    completed: document.getElementById('metaCompleted').checked,
                    created_date: document.getElementById('metaCreatedDate').value,
                    source: document.getElementById('metaSource').value,
                    revision_count: parseInt(document.getElementById('metaRevisionCount').value) || 0,
                    summary: document.getElementById('metaSummary').value,
                    one_para_summary: document.getElementById('metaOneParaSummary').value
                }};
                
                try {{
                    const response = await fetch('/api/metadata/' + encodeURIComponent(pdfFilePath), {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(metadata)
                    }});
                    
                    const data = await response.json();
                    if (data.success) {{
                        closeMetadataModal();
                    }} else {{
                        alert('Failed to save: ' + (data.error || 'Unknown error'));
                    }}
                }} catch (err) {{
                    console.error('Failed to save metadata:', err);
                    alert('Failed to save metadata');
                }}
            }}
            
            // Close modal on backdrop click
            document.getElementById('metadataModal')?.addEventListener('click', function(e) {{
                if (e.target === this) closeMetadataModal();
            }});
        </script>
        '''
        return render_template_string(
            HTML_TEMPLATE,
            title=filename,
            tree=tree,
            content=pdf_content,
            vault_name=get_vault_name(),
            is_markdown=False,
            is_pdf=True,
            file_path=filepath,
            full_path=os.path.join(VAULT_PATH, filepath)
        )
    
    elif ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
        return send_file(full_path)
    
    elif ext in ['mp4', 'mkv', 'avi', 'mov', 'webm']:
        # Plyr.js - Professional video player with fullscreen support
        video_content = f'''
        <link rel="stylesheet" href="https://cdn.plyr.io/3.7.8/plyr.css" />
        
        <h1 id="videoTitle">🎬 {filename}</h1>
        
        <div class="plyr-container">
            <video id="player" playsinline controls>
                <source src="/stream/{filepath}" type="video/{ext if ext != 'mkv' else 'mp4'}" />
            </video>
        </div>
        
        <div class="extra-controls">
            <span>Quick Skip:</span>
            <button onclick="player.currentTime -= 10">⏪ -10s</button>
            <button onclick="player.currentTime -= 5">◀ -5s</button>
            <button onclick="player.currentTime += 5">+5s ▶</button>
            <button onclick="player.currentTime += 10">+10s ⏩</button>
            <span style="margin-left: auto;"></span>
            <button onclick="openMetadataModal()">ℹ️ Info</button>
        </div>
        
        <style>
            .plyr-container {{
                max-width: 100%;
                margin: 20px 0;
                border-radius: 10px;
                overflow: hidden;
                background: #000;
            }}
            
            /* Plyr customizations */
            .plyr {{
                --plyr-color-main: #ff0000;
                --plyr-video-background: #000;
            }}
            
            .plyr--fullscreen-active {{
                border-radius: 0;
            }}
            
            /* Extra controls below video */
            .extra-controls {{
                background: #1e1e1e;
                padding: 15px;
                border-radius: 10px;
                margin-top: 15px;
                display: flex;
                align-items: center;
                gap: 10px;
                flex-wrap: wrap;
            }}
            .extra-controls span {{
                color: #ccc;
                font-size: 14px;
            }}
            .extra-controls button {{
                background: #333;
                color: #fff;
                border: none;
                padding: 10px 18px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 14px;
                transition: background 0.2s;
            }}
            .extra-controls button:hover {{
                background: #0066cc;
            }}
            
            /* Mobile adjustments */
            @media (max-width: 768px) {{
                .extra-controls {{
                    justify-content: center;
                }}
                .extra-controls button {{
                    padding: 8px 14px;
                    font-size: 13px;
                }}
            }}
            
            /* Metadata Modal */
            .metadata-modal {{
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: rgba(0,0,0,0.7);
                z-index: 1000;
                align-items: center;
                justify-content: center;
            }}
            .metadata-modal.visible {{
                display: flex;
            }}
            .metadata-modal-content {{
                background: #2d2d2d;
                border-radius: 12px;
                max-width: 500px;
                width: 90%;
                max-height: 90vh;
                overflow-y: auto;
            }}
            .metadata-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 15px 20px;
                border-bottom: 1px solid #444;
            }}
            .metadata-header h3 {{ margin: 0; color: #fff; }}
            .metadata-close {{
                background: none;
                border: none;
                color: #888;
                font-size: 20px;
                cursor: pointer;
            }}
            .metadata-close:hover {{ color: #fff; }}
            .metadata-body {{ padding: 20px; }}
            .metadata-field {{ margin-bottom: 15px; }}
            .metadata-field label {{
                display: block;
                color: #aaa;
                font-size: 13px;
                margin-bottom: 5px;
            }}
            .metadata-field input[type="text"],
            .metadata-field input[type="date"],
            .metadata-field input[type="number"],
            .metadata-field textarea {{
                width: 100%;
                padding: 10px;
                border: 1px solid #444;
                border-radius: 6px;
                background: #1e1e1e;
                color: #fff;
                font-size: 14px;
            }}
            .metadata-field input[type="checkbox"] {{
                width: 18px;
                height: 18px;
                margin-right: 8px;
            }}
            .metadata-field .checkbox-label {{ color: #fff; font-size: 15px; }}
            .metadata-footer {{
                padding: 15px 20px;
                border-top: 1px solid #444;
                display: flex;
                gap: 10px;
                justify-content: flex-end;
            }}
            .metadata-save {{
                background: #0078d4;
                color: #fff;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                cursor: pointer;
            }}
            .metadata-cancel {{
                background: #444;
                color: #fff;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                cursor: pointer;
            }}
        </style>
        
        <!-- Metadata Modal -->
        <div id="metadataModal" class="metadata-modal">
            <div class="metadata-modal-content">
                <div class="metadata-header">
                    <h3>ℹ️ File Metadata</h3>
                    <button class="metadata-close" onclick="closeMetadataModal()">✕</button>
                </div>
                <div class="metadata-body">
                    <div class="metadata-field">
                        <label>
                            <input type="checkbox" id="metaCompleted"> 
                            <span class="checkbox-label">✅ Completed</span>
                        </label>
                    </div>
                    <div class="metadata-field">
                        <label>📅 Created Date</label>
                        <input type="date" id="metaCreatedDate">
                    </div>
                    <div class="metadata-field">
                        <label>🔗 Source</label>
                        <input type="text" id="metaSource" placeholder="URL or reference...">
                    </div>
                    <div class="metadata-field">
                        <label>🔄 Revision Count</label>
                        <input type="number" id="metaRevisionCount" min="0" value="0">
                    </div>
                    <div class="metadata-field">
                        <label>📝 Summary (short)</label>
                        <input type="text" id="metaSummary" placeholder="Brief summary...">
                    </div>
                    <div class="metadata-field">
                        <label>📄 One Paragraph Summary</label>
                        <textarea id="metaOneParaSummary" rows="4" placeholder="Detailed summary..."></textarea>
                    </div>
                </div>
                <div class="metadata-footer">
                    <button class="metadata-save" onclick="saveMetadata()">💾 Save</button>
                    <button class="metadata-cancel" onclick="closeMetadataModal()">Cancel</button>
                </div>
            </div>
        </div>
        
        <script src="https://cdn.plyr.io/3.7.8/plyr.polyfilled.js"></script>
        <script>
            const player = new Plyr('#player', {{
                controls: [
                    'play-large', 'play', 'rewind', 'fast-forward', 'progress', 
                    'current-time', 'duration', 'mute', 'volume', 
                    'settings', 'fullscreen'
                ],
                settings: ['quality', 'speed'],
                speed: {{ 
                    selected: 1, 
                    options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] 
                }},
                keyboard: {{ 
                    focused: true, 
                    global: true 
                }},
                tooltips: {{ 
                    controls: true, 
                    seek: true 
                }},
                seekTime: 10,
                invertTime: false,
                fullscreen: {{
                    enabled: true,
                    fallback: true,
                    iosNative: true
                }}
            }});
            
            // Double-click to toggle fullscreen
            player.on('dblclick', () => {{
                player.fullscreen.toggle();
            }});
            
            // Resume from last position
            const savedTime = localStorage.getItem('video_' + window.location.pathname);
            if (savedTime) {{
                player.once('canplay', () => {{
                    player.currentTime = parseFloat(savedTime);
                }});
            }}
            
            // Save position periodically while playing
            player.on('timeupdate', () => {{
                if (player.currentTime > 0) {{
                    localStorage.setItem('video_' + window.location.pathname, player.currentTime);
                }}
            }});
            
            // Keyboard shortcuts
            document.addEventListener('keydown', (e) => {{
                if (e.target.tagName === 'INPUT') return;
                switch(e.key) {{
                    case 'f': player.fullscreen.toggle(); break;
                    case 'j': player.currentTime -= 10; break;
                    case 'l': player.currentTime += 10; break;
                }}
            }});
            
            // ============================================
            // METADATA FUNCTIONS
            // ============================================
            const videoFilePath = '{filepath}';
            
            function openMetadataModal() {{
                document.getElementById('metadataModal').classList.add('visible');
                loadMetadata();
            }}
            
            function closeMetadataModal() {{
                document.getElementById('metadataModal').classList.remove('visible');
            }}
            
            async function loadMetadata() {{
                try {{
                    const response = await fetch('/api/metadata/' + encodeURIComponent(videoFilePath));
                    const data = await response.json();
                    if (data.success && data.metadata) {{
                        const meta = data.metadata;
                        document.getElementById('metaCompleted').checked = meta.completed || false;
                        document.getElementById('metaCreatedDate').value = meta.created_date || '';
                        document.getElementById('metaSource').value = meta.source || '';
                        document.getElementById('metaRevisionCount').value = meta.revision_count || 0;
                        document.getElementById('metaSummary').value = meta.summary || '';
                        document.getElementById('metaOneParaSummary').value = meta.one_para_summary || '';
                    }}
                }} catch (err) {{ console.error('Failed to load metadata:', err); }}
            }}
            
            async function saveMetadata() {{
                const metadata = {{
                    completed: document.getElementById('metaCompleted').checked,
                    created_date: document.getElementById('metaCreatedDate').value,
                    source: document.getElementById('metaSource').value,
                    revision_count: parseInt(document.getElementById('metaRevisionCount').value) || 0,
                    summary: document.getElementById('metaSummary').value,
                    one_para_summary: document.getElementById('metaOneParaSummary').value
                }};
                try {{
                    const response = await fetch('/api/metadata/' + encodeURIComponent(videoFilePath), {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(metadata)
                    }});
                    const data = await response.json();
                    if (data.success) closeMetadataModal();
                    else alert('Failed to save');
                }} catch (err) {{ alert('Failed to save metadata'); }}
            }}
            
            document.getElementById('metadataModal')?.addEventListener('click', function(e) {{
                if (e.target === this) closeMetadataModal();
            }});
        </script>
        '''
        return render_template_string(
            HTML_TEMPLATE,
            title=filename,
            tree=tree,
            content=video_content,
            vault_name=get_vault_name(),
            is_markdown=False,
            file_path=filepath,
            full_path=os.path.join(VAULT_PATH, filepath)
        )

    elif ext in ['mp3', 'wav', 'ogg']:
        # Audio player
        audio_content = f'''
        <h1>🎵 {filename}</h1>
        <div class="audio-container">
            <audio id="audioPlayer" controls style="width: 100%;">
                <source src="/stream/{filepath}" type="audio/{ext}">
                Your browser does not support audio playback.
            </audio>
            <div class="audio-controls" style="background: #1e1e1e; padding: 15px; border-radius: 10px; margin-top: 10px;">
                <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                    <span style="color: #ccc;">Speed:</span>
                    <button onclick="document.getElementById('audioPlayer').playbackRate=0.5" style="background:#444;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;">0.5x</button>
                    <button onclick="document.getElementById('audioPlayer').playbackRate=0.75" style="background:#444;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;">0.75x</button>
                    <button onclick="document.getElementById('audioPlayer').playbackRate=1" style="background:#0066cc;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;">1x</button>
                    <button onclick="document.getElementById('audioPlayer').playbackRate=1.25" style="background:#444;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;">1.25x</button>
                    <button onclick="document.getElementById('audioPlayer').playbackRate=1.5" style="background:#444;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;">1.5x</button>
                    <button onclick="document.getElementById('audioPlayer').playbackRate=2" style="background:#444;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;">2x</button>
                </div>
            </div>
        </div>
        '''
        return render_template_string(
            HTML_TEMPLATE,
            title=filename,
            tree=tree,
            content=audio_content,
            vault_name=get_vault_name(),
            is_markdown=False,
            file_path=filepath,
            full_path=os.path.join(VAULT_PATH, filepath)
        )
    
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
            file_path=filepath,
            full_path=os.path.join(VAULT_PATH, filepath),
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
    
    # Protect math expressions before markdown processing
    md_content, math_placeholders = protect_math_expressions(md_content)
    
    # Convert to HTML
    html_content = markdown.markdown(
        md_content,
        extensions=['tables', 'fenced_code', 'toc', 'nl2br', 'sane_lists']
    )
    
    # Restore math expressions
    html_content = restore_math_expressions(html_content, math_placeholders)
    
    # Convert Obsidian wikilink images ![[image.png]] to proper <img> tags for PDF
    current_dir = os.path.dirname(filepath)
    def replace_embed_pdf(match):
        inner = match.group(1)
        # Handle display text: ![[image.png|alt text]]
        if '|' in inner:
            link_part, alt_text = inner.split('|', 1)
        else:
            link_part = inner
            alt_text = inner
        
        # Find the file - try relative path first
        relative_path = os.path.join(current_dir, link_part)
        relative_path = os.path.normpath(relative_path)
        file_full_path = os.path.join(VAULT_PATH, relative_path)
        
        if not os.path.exists(file_full_path):
            # Try finding in vault
            found_path = find_file_in_vault(link_part)
            if found_path:
                file_full_path = os.path.join(VAULT_PATH, found_path)
        
        if os.path.exists(file_full_path):
            ext = link_part.lower().rsplit('.', 1)[-1] if '.' in link_part else ''
            if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp']:
                # Use file:// protocol for local PDF generation
                return f'<img src="file://{file_full_path}" alt="{alt_text}" style="max-width: 100%;">'
        return match.group(0)  # Return original if not found
    
    html_content = re.sub(r'!\[\[([^\]]+)\]\]', replace_embed_pdf, html_content)
    
    # Full HTML document for PDF (with MathJax for equation rendering)
    title = os.path.basename(filepath).replace('.md', '')
    full_html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{title}</title>
        <script>
            MathJax = {{
                tex: {{
                    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
                    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
                    processEscapes: true
                }},
                svg: {{ fontCache: 'global' }},
                startup: {{
                    pageReady: () => {{
                        return MathJax.startup.defaultPageReady().then(() => {{
                            window.mathJaxReady = true;
                        }});
                    }}
                }}
            }};
        </script>
        <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
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
            mjx-container {{ overflow-x: auto; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        {html_content}
    </body>
    </html>
    '''
    
    # Try Playwright first (best quality, renders MathJax properly)
    try:
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w', encoding='utf-8') as html_file:
            html_file.write(full_html)
            html_path = html_file.name
        
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as pdf_file:
            pdf_path = pdf_file.name
        
        # Use Playwright to render with full JS support
        result = subprocess.run(
            ['npx', 'playwright', 'pdf', f'file://{html_path}', pdf_path,
             '--wait-for-selector', 'mjx-container, body',
             '--wait-for-timeout', '3000'],
            capture_output=True,
            timeout=60
        )
        
        if result.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            response = send_file(
                pdf_path,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
            os.unlink(html_path)
            return response
        
        # Cleanup failed attempt
        if os.path.exists(html_path):
            os.unlink(html_path)
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
            
    except Exception as e:
        print(f"Playwright PDF error: {e}")
    
    # Try to convert using wkhtmltopdf or weasyprint (fallback, no MathJax)
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
    
    # Fallback: Try weasyprint (check venv first, then system)
    try:
        # Try importing from venv via subprocess (more reliable)
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w', encoding='utf-8') as html_file:
            html_file.write(full_html)
            html_path = html_file.name
        
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as pdf_file:
            pdf_path = pdf_file.name
        
        # Try venv weasyprint first, then system
        # Permanent location: ~/clawd/envs/pdfenv (survives reboots)
        # Fallback to /tmp/pdfenv for backwards compatibility
        home = os.path.expanduser('~')
        weasyprint_paths = [
            os.path.join(home, 'clawd/envs/pdfenv/bin/python3'),  # permanent venv
            '/tmp/pdfenv/bin/python3',  # legacy temp venv (backwards compat)
            'python3',  # system python
        ]
        
        for python_path in weasyprint_paths:
            try:
                result = subprocess.run(
                    [python_path, '-c', f'''
import sys
from weasyprint import HTML
HTML(filename="{html_path}").write_pdf("{pdf_path}")
print("success")
'''],
                    capture_output=True,
                    timeout=60
                )
                if result.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                    os.unlink(html_path)
                    return send_file(
                        pdf_path,
                        mimetype='application/pdf',
                        as_attachment=True,
                        download_name=filename
                    )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        
        # Cleanup
        if os.path.exists(html_path):
            os.unlink(html_path)
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
            
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

@app.route('/stream/<path:filepath>')
def stream_file(filepath):
    """Stream video/audio files with range request support for seeking."""
    from flask import request, Response
    
    full_path = os.path.join(VAULT_PATH, filepath)
    
    # Security check
    if not os.path.abspath(full_path).startswith(os.path.abspath(VAULT_PATH)):
        abort(403)
    
    if not os.path.exists(full_path):
        abort(404)
    
    ext = filepath.lower().rsplit('.', 1)[-1] if '.' in filepath else ''
    
    # Determine MIME type
    mime_types = {
        'mp4': 'video/mp4',
        'mkv': 'video/x-matroska',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'webm': 'video/webm',
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'ogg': 'audio/ogg',
    }
    mime_type = mime_types.get(ext, 'application/octet-stream')
    
    file_size = os.path.getsize(full_path)
    
    # Handle range requests for seeking
    range_header = request.headers.get('Range')
    
    if range_header:
        # Parse range header
        byte_start = 0
        byte_end = file_size - 1
        
        if range_header.startswith('bytes='):
            range_spec = range_header[6:]
            if '-' in range_spec:
                parts = range_spec.split('-')
                if parts[0]:
                    byte_start = int(parts[0])
                if parts[1]:
                    byte_end = int(parts[1])
        
        # Ensure valid range
        byte_end = min(byte_end, file_size - 1)
        content_length = byte_end - byte_start + 1
        
        def generate():
            with open(full_path, 'rb') as f:
                f.seek(byte_start)
                remaining = content_length
                chunk_size = 1024 * 1024  # 1MB chunks
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        
        response = Response(
            generate(),
            status=206,  # Partial Content
            mimetype=mime_type,
            direct_passthrough=True
        )
        response.headers['Content-Range'] = f'bytes {byte_start}-{byte_end}/{file_size}'
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Length'] = content_length
        return response
    
    else:
        # Full file request
        def generate():
            with open(full_path, 'rb') as f:
                chunk_size = 1024 * 1024  # 1MB chunks
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        
        response = Response(
            generate(),
            status=200,
            mimetype=mime_type,
            direct_passthrough=True
        )
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Length'] = file_size
        return response

# ============================================
# ANNOTATION ENDPOINTS
# ============================================

def get_annotation_path(filepath):
    """Get the annotation file path for a given file"""
    annotations_dir = os.path.join(VAULT_PATH, 'annotations')
    os.makedirs(annotations_dir, exist_ok=True)
    
    # Replace path separators with underscores and add .json extension
    safe_name = filepath.replace('/', '_').replace('\\', '_')
    return os.path.join(annotations_dir, f"{safe_name}.json")

@app.route('/api/annotations/<path:filepath>', methods=['GET'])
def get_annotations(filepath):
    """Get annotations for a file"""
    annotation_path = get_annotation_path(filepath)
    
    if os.path.exists(annotation_path):
        try:
            with open(annotation_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return jsonify(data)
        except Exception as e:
            return jsonify({'strokes': [], 'error': str(e)})
    
    return jsonify({'strokes': []})

@app.route('/api/annotations/<path:filepath>', methods=['POST'])
def save_annotations(filepath):
    """Save annotations for a file"""
    try:
        data = request.get_json()
        annotation_path = get_annotation_path(filepath)
        
        with open(annotation_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/annotations/<path:filepath>', methods=['DELETE'])
def delete_annotations(filepath):
    """Delete annotations for a file"""
    try:
        annotation_path = get_annotation_path(filepath)
        
        if os.path.exists(annotation_path):
            os.remove(annotation_path)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# FLASHCARD API ENDPOINTS
# ============================================

def get_study_json_path(filepath):
    """
    Get the path to the .study.json file for a given MD file.
    Returns (json_path, exists) tuple.
    
    Checks two locations in order:
    1. Same directory: {filename}.study.json (new preferred location)
    2. Legacy: .obsidian-viewer/study/{path_with_underscores}.study.json
    """
    # New location: alongside the MD file
    full_md_path = os.path.join(VAULT_PATH, filepath)
    if filepath.endswith('.md'):
        new_json_path = full_md_path[:-3] + '.study.json'
    else:
        new_json_path = full_md_path + '.study.json'
    
    if os.path.exists(new_json_path):
        return new_json_path, True
    
    # Legacy location: in .obsidian-viewer/study folder
    rel_path = filepath
    json_filename = rel_path.replace('/', '_').replace('\\', '_') + '.study.json'
    study_dir = os.path.join(VAULT_PATH, '.obsidian-viewer', 'study')
    legacy_json_path = os.path.join(study_dir, json_filename)
    
    if os.path.exists(legacy_json_path):
        return legacy_json_path, True
    
    # Return new path (for creating new files)
    return new_json_path, False


def load_study_json(filepath):
    """
    Load study data from JSON file if it exists.
    Returns dict with flashcards, mcq, cloze or None if no JSON file.
    """
    json_path, exists = get_study_json_path(filepath)
    if not exists:
        return None
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def parse_flashcards(content):
    """
    Parse flashcards from markdown content.
    
    Supports multiple formats:
    1. ## Flashcards section with Q:/A: pairs
    2. > [!flashcard] callouts
    3. #flashcard tagged sections
    """
    flashcards = []
    
    # Pattern 1: ## Flashcards section
    flashcard_section = re.search(r'##\s*Flashcards?\s*\n([\s\S]*?)(?=\n##\s|\Z)', content, re.IGNORECASE)
    if flashcard_section:
        section_content = flashcard_section.group(1)
        # Parse Q:/A: pairs
        qa_pattern = re.compile(r'Q:\s*(.+?)\nA:\s*(.+?)(?=\nQ:|\n\n|\Z)', re.DOTALL | re.IGNORECASE)
        for match in qa_pattern.finditer(section_content):
            question = match.group(1).strip()
            answer = match.group(2).strip()
            if question and answer:
                flashcards.append({'question': question, 'answer': answer})
    
    # Pattern 2: > [!flashcard] callouts
    callout_pattern = re.compile(r'>\s*\[!flashcard\][+-]?\s*(.+?)\n((?:>.*\n)*)', re.IGNORECASE)
    for match in callout_pattern.finditer(content):
        title = match.group(1).strip()
        body_lines = match.group(2).strip()
        # Remove > prefix from body lines
        body = '\n'.join(line.lstrip('>').strip() for line in body_lines.split('\n') if line.strip())
        if title and body:
            flashcards.append({'question': title, 'answer': body})
    
    # Pattern 3: Extract from [!brain] memory tips (Q&A tables)
    brain_pattern = re.compile(r'>\s*\[!brain\][+-]?\s*(.+?)\n((?:>.*\n)*)', re.IGNORECASE)
    for match in brain_pattern.finditer(content):
        title = match.group(1).strip()
        body_lines = match.group(2).strip()
        body = '\n'.join(line.lstrip('>').strip() for line in body_lines.split('\n') if line.strip())
        if title and body:
            # Create a flashcard from the memory tip
            flashcards.append({
                'question': f"🧠 {title.replace('Memory Tip:', '').strip()}",
                'answer': body[:500] + ('...' if len(body) > 500 else '')  # Truncate long answers
            })
    
    # Pattern 4: Simple --- separated cards
    card_blocks = re.findall(r'---\s*\nQ:\s*(.+?)\nA:\s*(.+?)\n---', content, re.DOTALL | re.IGNORECASE)
    for q, a in card_blocks:
        question = q.strip()
        answer = a.strip()
        if question and answer:
            flashcards.append({'question': question, 'answer': answer})
    
    return flashcards

@app.route('/api/flashcards/<path:filepath>')
def api_get_flashcards(filepath):
    """Get flashcards - checks JSON file first, falls back to parsing MD"""
    try:
        full_path = os.path.join(VAULT_PATH, filepath)
        
        if not os.path.exists(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # Check for study JSON first
        study_data = load_study_json(filepath)
        if study_data and study_data.get('flashcards'):
            return jsonify({
                'success': True,
                'flashcards': study_data['flashcards'],
                'count': len(study_data['flashcards']),
                'source': 'json'
            })
        
        # Fallback: parse from MD file
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        flashcards = parse_flashcards(content)
        
        return jsonify({
            'success': True,
            'flashcards': flashcards,
            'count': len(flashcards),
            'source': 'md'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# MCQ (Multiple Choice Questions) API ENDPOINTS
# ============================================

def parse_mcq(content):
    """
    Parse MCQs from markdown content.
    
    Supports format:
    ## MCQ
    Q: Question text?
    - [ ] Option A
    - [ ] Option B
    - [x] Correct answer
    - [ ] Option D
    """
    mcqs = []
    
    # Look for ## MCQ section
    mcq_section = re.search(r'##\s*MCQ\s*\n([\s\S]*?)(?=\n##\s|\Z)', content, re.IGNORECASE)
    if not mcq_section:
        return mcqs
    
    section_content = mcq_section.group(1)
    
    # Split by Q: to get individual questions
    questions = re.split(r'\nQ:\s*', section_content)
    
    for q_block in questions:
        if not q_block.strip():
            continue
        
        lines = q_block.strip().split('\n')
        if not lines:
            continue
        
        # First line is the question
        question = lines[0].strip()
        if question.startswith('Q:'):
            question = question[2:].strip()
        
        options = []
        correct_index = -1
        
        # Parse options (- [ ] or - [x])
        for line in lines[1:]:
            line = line.strip()
            # Match checked option [x] or [X]
            if re.match(r'^-\s*\[x\]\s*', line, re.IGNORECASE):
                option_text = re.sub(r'^-\s*\[x\]\s*', '', line, flags=re.IGNORECASE).strip()
                if option_text:
                    correct_index = len(options)
                    options.append(option_text)
            # Match unchecked option [ ]
            elif re.match(r'^-\s*\[\s*\]\s*', line):
                option_text = re.sub(r'^-\s*\[\s*\]\s*', '', line).strip()
                if option_text:
                    options.append(option_text)
        
        if question and len(options) >= 2 and correct_index >= 0:
            mcqs.append({
                'question': question,
                'options': options,
                'correct': correct_index
            })
    
    return mcqs

@app.route('/api/mcq/<path:filepath>')
def api_get_mcq(filepath):
    """Get MCQs - checks JSON file first, falls back to parsing MD"""
    try:
        full_path = os.path.join(VAULT_PATH, filepath)
        
        if not os.path.exists(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # Check for study JSON first
        study_data = load_study_json(filepath)
        if study_data and study_data.get('mcq'):
            mcqs = [normalize_mcq(m) for m in study_data['mcq']]
            source = 'json'
        else:
            # Fallback: parse from MD file
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            mcqs = parse_mcq(content)
            source = 'md'
        
        # Get existing MCQ score from metadata
        metadata = get_file_metadata(filepath)
        mcq_score = metadata.get('mcq_score', {})
        
        return jsonify({
            'success': True,
            'mcqs': mcqs,
            'count': len(mcqs),
            'score': mcq_score,
            'source': source
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/mcq-score/<path:filepath>', methods=['POST'])
def api_save_mcq_score(filepath):
    """Save MCQ score for a file"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        metadata = get_file_metadata(filepath)
        metadata['mcq_score'] = {
            'correct': data.get('correct', 0),
            'total': data.get('total', 0),
            'percentage': data.get('percentage', 0),
            'last_attempt': data.get('last_attempt', '')
        }
        set_file_metadata(filepath, metadata)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# METADATA API ENDPOINTS
# ============================================

@app.route('/api/metadata/<path:filepath>', methods=['GET'])
def api_get_metadata(filepath):
    """Get metadata for a file"""
    try:
        metadata = get_file_metadata(filepath)
        return jsonify({'success': True, 'metadata': metadata})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/metadata/<path:filepath>', methods=['POST'])
def api_set_metadata(filepath):
    """Set metadata for a file"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        # Get existing metadata and update with new values
        metadata = get_file_metadata(filepath)
        
        # Update fields if provided
        if 'completed' in data:
            metadata['completed'] = bool(data['completed'])
        if 'created_date' in data:
            metadata['created_date'] = str(data['created_date'])
        if 'source' in data:
            metadata['source'] = str(data['source'])
        if 'revision_count' in data:
            metadata['revision_count'] = int(data['revision_count'])
        if 'summary' in data:
            metadata['summary'] = str(data['summary'])
        if 'one_para_summary' in data:
            metadata['one_para_summary'] = str(data['one_para_summary'])
        
        set_file_metadata(filepath, metadata)
        return jsonify({'success': True, 'metadata': metadata})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/download-offline-zip')
def api_download_offline_zip():
    """Generate a ZIP file with HTML versions of all markdown files"""
    import zipfile
    import io
    from datetime import datetime
    
    # Get CSS for embedding in HTML
    css_content = '''
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6; 
            max-width: 900px; 
            margin: 0 auto; 
            padding: 20px;
            background: #1e1e1e;
            color: #d4d4d4;
        }
        h1, h2, h3, h4, h5, h6 { color: #569cd6; margin-top: 1.5em; }
        h1 { border-bottom: 2px solid #3c3c3c; padding-bottom: 0.3em; }
        h2 { border-bottom: 1px solid #3c3c3c; padding-bottom: 0.2em; }
        a { color: #4fc1ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        code { 
            background: #2d2d2d; 
            padding: 2px 6px; 
            border-radius: 3px; 
            font-family: 'Fira Code', monospace;
            color: #ce9178;
        }
        pre { 
            background: #2d2d2d; 
            padding: 16px; 
            border-radius: 6px; 
            overflow-x: auto;
            border: 1px solid #3c3c3c;
        }
        pre code { background: none; padding: 0; }
        blockquote { 
            border-left: 4px solid #569cd6; 
            margin: 1em 0; 
            padding: 0.5em 1em;
            background: #252526;
            color: #9cdcfe;
        }
        table { 
            border-collapse: collapse; 
            width: 100%; 
            margin: 1em 0;
        }
        th, td { 
            border: 1px solid #3c3c3c; 
            padding: 8px 12px; 
            text-align: left;
        }
        th { background: #2d2d2d; color: #569cd6; }
        tr:nth-child(even) { background: #252526; }
        img { max-width: 100%; height: auto; }
        hr { border: none; border-top: 1px solid #3c3c3c; margin: 2em 0; }
        ul, ol { padding-left: 1.5em; }
        li { margin: 0.3em 0; }
        .nav-link { 
            display: inline-block; 
            background: #2d2d2d; 
            padding: 4px 10px; 
            border-radius: 4px; 
            margin: 2px;
            border: 1px solid #3c3c3c;
        }
        .file-header {
            background: #252526;
            padding: 10px 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            border: 1px solid #3c3c3c;
            font-size: 12px;
            color: #888;
        }
        .callout {
            border-left: 4px solid;
            padding: 12px 16px;
            margin: 16px 0;
            border-radius: 0 6px 6px 0;
        }
        .callout-title { font-weight: bold; margin-bottom: 8px; }
        .callout-note, .callout-info { background: #1a3a5c; border-color: #0066cc; }
        .callout-tip, .callout-hint { background: #1a3c2a; border-color: #10b981; }
        .callout-warning, .callout-caution { background: #3c2a1a; border-color: #f59e0b; }
        .callout-danger, .callout-error { background: #3c1a1a; border-color: #dc2626; }
        .callout-question, .callout-help, .callout-faq { background: #2a1a3c; border-color: #8b5cf6; }
        .callout-example { background: #1a2a3c; border-color: #6366f1; }
        .callout-quote { background: #2a2a2a; border-color: #6b7280; }
        .callout-diagram { background: #1a2a3c; border-color: #06b6d4; }
        details.callout summary { cursor: pointer; }
        details.callout summary::-webkit-details-marker { display: none; }
        details.callout summary::before { content: '▶ '; font-size: 10px; }
        details.callout[open] summary::before { content: '▼ '; }
    </style>
    '''
    
    # Helper function to convert image to base64 data URI
    def find_image_path(image_name, source_file_path):
        """Find the actual path to an image file."""
        current_dir = os.path.dirname(source_file_path)
        
        # Try different possible locations
        possible_paths = [
            os.path.join(current_dir, image_name),
            os.path.join(current_dir, 'img', image_name),
            os.path.join(current_dir, 'images', image_name),
            os.path.join(VAULT_PATH, image_name),
            os.path.join(VAULT_PATH, 'img', image_name),
        ]
        
        # Also try parent directories
        parent = os.path.dirname(current_dir)
        if parent:
            possible_paths.extend([
                os.path.join(parent, image_name),
                os.path.join(parent, 'img', image_name),
            ])
        
        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None
    
    # Collect all images to copy
    all_images_to_copy = set()
    
    def image_to_base64(image_path, file_full_path):
        """Convert an image file to base64 data URI"""
        import base64
        import mimetypes
        
        # Determine full path
        if os.path.isabs(image_path):
            full_img_path = image_path
        else:
            # Try relative to current markdown file
            current_dir = os.path.dirname(file_full_path)
            full_img_path = os.path.join(current_dir, image_path)
            full_img_path = os.path.normpath(full_img_path)
            
            # If not found, try relative to vault
            if not os.path.exists(full_img_path):
                full_img_path = os.path.join(VAULT_PATH, image_path)
            
            # Try finding in vault using find_file_in_vault
            if not os.path.exists(full_img_path):
                found = find_file_in_vault(image_path)
                if found:
                    full_img_path = os.path.join(VAULT_PATH, found)
        
        if not os.path.exists(full_img_path):
            return None
        
        try:
            mime_type, _ = mimetypes.guess_type(full_img_path)
            if not mime_type:
                ext = image_path.lower().rsplit('.', 1)[-1]
                mime_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 
                           'gif': 'image/gif', 'webp': 'image/webp', 'svg': 'image/svg+xml'}
                mime_type = mime_map.get(ext, 'application/octet-stream')
            
            with open(full_img_path, 'rb') as img_file:
                img_data = base64.b64encode(img_file.read()).decode('utf-8')
            return f'data:{mime_type};base64,{img_data}'
        except Exception as e:
            return None
    
    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        md_files = []
        md_folders = set()
        
        # Find all .md files
        for root, dirs, files in os.walk(VAULT_PATH):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for fname in files:
                if fname.endswith('.md') and not fname.startswith('.'):
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, VAULT_PATH)
                    md_files.append((full_path, rel_path))
                    
                    # Track folders that have .md files
                    folder = os.path.dirname(rel_path)
                    while folder:
                        md_folders.add(folder)
                        folder = os.path.dirname(folder)
        
        # Convert each .md file to HTML and add to ZIP
        for full_path, rel_path in md_files:
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    md_content = f.read()
                
                # Strip YAML frontmatter
                if md_content.startswith('---'):
                    parts = md_content.split('---', 2)
                    if len(parts) >= 3:
                        md_content = parts[2]
                
                # Convert Obsidian image embeds ![[image.png]] to relative paths
                # Images will be copied to the ZIP as separate files (faster than base64)
                images_to_copy = set()
                
                def replace_obsidian_image(match):
                    inner = match.group(1)
                    if '|' in inner:
                        link_part, alt_text = inner.split('|', 1)
                    else:
                        link_part = inner
                        alt_text = inner
                    
                    ext = link_part.lower().rsplit('.', 1)[-1] if '.' in link_part else ''
                    if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp']:
                        # Find the image file
                        img_path = find_image_path(link_part, full_path)
                        if img_path:
                            images_to_copy.add(img_path)
                            # Use relative path from HTML file to image
                            html_dir = os.path.dirname(rel_path)
                            img_rel = os.path.relpath(img_path, VAULT_PATH)
                            if html_dir:
                                img_from_html = os.path.relpath(img_rel, html_dir)
                            else:
                                img_from_html = img_rel
                            return f'![{alt_text}]({img_from_html})'
                        return f'![{alt_text}]({link_part})'
                    return match.group(0)
                
                md_content = re.sub(r'!\[\[([^\]]+)\]\]', replace_obsidian_image, md_content)
                
                # Also handle standard markdown images ![alt](path)
                def replace_md_image(match):
                    alt_text = match.group(1)
                    img_path_str = match.group(2)
                    
                    if img_path_str.startswith('data:') or img_path_str.startswith('http'):
                        return match.group(0)
                    
                    img_path = find_image_path(img_path_str, full_path)
                    if img_path:
                        images_to_copy.add(img_path)
                    return match.group(0)  # Keep original path
                
                md_content = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_md_image, md_content)
                
                # Process callouts
                md_content = convert_obsidian_callouts(md_content)
                
                # Fix list continuation (numbered items after nested bullets)
                md_content = fix_list_continuation(md_content)
                
                # Protect math expressions
                md_content, math_placeholders = protect_math_expressions(md_content)
                
                # Convert markdown to HTML
                html_body = markdown.markdown(
                    md_content,
                    extensions=['tables', 'fenced_code', 'toc', 'nl2br', 'sane_lists']
                )
                
                # Restore math
                html_body = restore_math_expressions(html_body, math_placeholders)
                
                # Convert wiki links to relative HTML links
                current_dir = os.path.dirname(rel_path)
                
                def convert_wiki_link(match):
                    inner = match.group(1)
                    if '|' in inner:
                        link_part, display = inner.split('|', 1)
                    else:
                        link_part = inner
                        display = inner.split('/')[-1]
                    
                    # Remove heading anchors for file path
                    if '#' in link_part:
                        link_part = link_part.split('#')[0]
                    
                    # Find the target file
                    target_path = None
                    link_normalized = link_part.replace(' ', '_')
                    
                    # Search for matching file
                    for fp, rp in md_files:
                        rp_normalized = rp.replace(' ', '_').rsplit('.', 1)[0]
                        # Match by full path or just filename
                        if rp_normalized == link_normalized or rp_normalized.endswith('/' + link_normalized) or os.path.basename(rp_normalized) == link_normalized:
                            target_path = rp.rsplit('.', 1)[0] + '.html'
                            break
                    
                    if target_path:
                        # Calculate relative path from current file to target
                        target_path = target_path.replace(' ', '_')
                        if current_dir:
                            # Go up from current dir and then to target
                            rel_link = os.path.relpath(target_path, current_dir)
                        else:
                            rel_link = target_path
                        html_link = rel_link.replace('\\', '/')
                    else:
                        # File not found, use the link as-is
                        html_link = link_normalized + '.html'
                    
                    return f'<a href="{html_link}" class="nav-link">{display}</a>'
                
                html_body = re.sub(r'\[\[([^\]]+)\]\]', convert_wiki_link, html_body)
                
                # Build full HTML document
                title = os.path.splitext(os.path.basename(rel_path))[0]
                html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    {css_content}
    <script>
        MathJax = {{
            tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']] }},
            svg: {{ fontCache: 'global' }}
        }};
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js" async></script>
</head>
<body>
    <div class="file-header">📄 {rel_path}</div>
    {html_body}
</body>
</html>'''
                
                # Add to ZIP with .html extension
                # Sanitize path for Windows/Android (remove : and other problematic chars)
                html_path = rel_path.rsplit('.', 1)[0] + '.html'
                html_path = html_path.replace(':', '-').replace('?', '').replace('*', '').replace('<', '').replace('>', '').replace('|', '-')
                zf.writestr(html_path, html_content.encode('utf-8'))
                
                # Collect images to copy
                all_images_to_copy.update(images_to_copy)
                
            except Exception as e:
                print(f"Error processing {rel_path}: {e}")
                continue
        
        # Create index.html with links to all files
        index_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EWADIS Offline Index</title>
    {css_content}
</head>
<body>
    <h1>📚 EWADIS Offline Index</h1>
    <p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
    <p>Total files: {len(md_files)}</p>
    <hr>
    <h2>📁 Files</h2>
    <ul>
'''
        # Group files by folder
        files_by_folder = {}
        for full_path, rel_path in sorted(md_files, key=lambda x: x[1].lower()):
            folder = os.path.dirname(rel_path) or 'Root'
            if folder not in files_by_folder:
                files_by_folder[folder] = []
            files_by_folder[folder].append(rel_path)
        
        for folder in sorted(files_by_folder.keys()):
            safe_folder = folder.replace(':', '-').replace('?', '').replace('*', '')
            index_html += f'<li><strong>📁 {safe_folder}</strong><ul>'
            for rel_path in files_by_folder[folder]:
                html_path = rel_path.rsplit('.', 1)[0] + '.html'
                html_path = html_path.replace(':', '-').replace('?', '').replace('*', '').replace('<', '').replace('>', '').replace('|', '-')
                name = os.path.basename(rel_path).rsplit('.', 1)[0]
                index_html += f'<li><a href="{html_path}">{name}</a></li>'
            index_html += '</ul></li>'
        
        index_html += '''
    </ul>
</body>
</html>'''
        
        zf.writestr('index.html', index_html.encode('utf-8'))
        
        # Copy all images to the ZIP
        for img_path in all_images_to_copy:
            try:
                if os.path.exists(img_path):
                    img_rel_path = os.path.relpath(img_path, VAULT_PATH)
                    # Sanitize path for Windows/Android
                    img_rel_path = img_rel_path.replace(':', '-').replace('?', '').replace('*', '').replace('<', '').replace('>', '').replace('|', '-')
                    with open(img_path, 'rb') as img_file:
                        zf.writestr(img_rel_path, img_file.read())
            except Exception as e:
                print(f"Error copying image {img_path}: {e}")
    
    zip_buffer.seek(0)
    zip_data = zip_buffer.getvalue()
    
    # Use vault name for the zip filename
    safe_vault_name = get_vault_name().replace(' ', '_').replace('/', '_')
    zip_filename = f'{safe_vault_name}_Offline.zip'
    
    return Response(
        zip_data,
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{zip_filename}"',
            'Content-Length': str(len(zip_data)),
            'Content-Type': 'application/zip',
            'Cache-Control': 'no-cache'
        }
    )


def find_linked_pages(filepath, visited=None):
    """Recursively find all wiki-linked pages from a markdown file.
    
    Returns a set of (full_path, relative_path) tuples.
    """
    if visited is None:
        visited = set()
    
    full_path = os.path.join(VAULT_PATH, filepath)
    if not os.path.exists(full_path) or filepath in visited:
        return visited
    
    visited.add(filepath)
    
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except:
        return visited
    
    # Find all [[wiki-links]] (excluding embeds ![[...]])
    # Match [[link]] or [[link|display text]] or [[link#heading]]
    pattern = r'(?<!!)\[\[([^\]|#]+)(?:#[^\]|]*)?\|?[^\]]*\]\]'
    matches = re.findall(pattern, content)
    
    current_dir = os.path.dirname(filepath)
    
    for link in matches:
        link = link.strip()
        if not link:
            continue
        
        # Try to resolve the link to an actual file
        possible_paths = [
            os.path.join(current_dir, link + '.md'),
            os.path.join(current_dir, link, link.split('/')[-1] + '.md'),
            link + '.md',
            os.path.join(current_dir, link),
            link
        ]
        
        # Also try with the link as-is if it ends with .md
        if link.endswith('.md'):
            possible_paths.insert(0, os.path.join(current_dir, link))
            possible_paths.insert(1, link)
        
        for path in possible_paths:
            normalized = os.path.normpath(path)
            full = os.path.join(VAULT_PATH, normalized)
            if os.path.exists(full) and normalized.endswith('.md'):
                if normalized not in visited:
                    find_linked_pages(normalized, visited)
                break
    
    return visited


@app.route('/api/download-topic-zip/<path:filepath>')
def api_download_topic_zip(filepath):
    """Download current page and all linked subpages as a ZIP file.
    
    Unlike the full vault export, this only includes:
    - The current page
    - All pages linked from it (recursively)
    """
    import zipfile
    import io
    from datetime import datetime
    
    full_path = os.path.join(VAULT_PATH, filepath)
    if not os.path.exists(full_path):
        abort(404, f"File not found: {filepath}")
    
    if not filepath.endswith('.md'):
        abort(400, "Only markdown files can be exported")
    
    # Find all linked pages recursively
    linked_pages = find_linked_pages(filepath)
    
    # CSS for embedded HTML (dark theme matching obsidian-viewer)
    css_content = '''
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6; 
            max-width: 900px; 
            margin: 0 auto; 
            padding: 20px;
            background: #1e1e1e;
            color: #d4d4d4;
        }
        h1, h2, h3, h4, h5, h6 { color: #569cd6; margin-top: 1.5em; }
        h1 { border-bottom: 2px solid #3c3c3c; padding-bottom: 0.3em; }
        h2 { border-bottom: 1px solid #3c3c3c; padding-bottom: 0.2em; }
        a { color: #4fc1ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        code { 
            background: #2d2d2d; 
            padding: 2px 6px; 
            border-radius: 3px; 
            font-family: 'Fira Code', monospace;
            color: #ce9178;
        }
        pre { 
            background: #2d2d2d; 
            padding: 16px; 
            border-radius: 6px; 
            overflow-x: auto;
            border: 1px solid #3c3c3c;
        }
        pre code { background: none; padding: 0; }
        blockquote { 
            border-left: 4px solid #569cd6; 
            margin: 1em 0; 
            padding: 0.5em 1em;
            background: #252526;
            color: #9cdcfe;
        }
        table { 
            border-collapse: collapse; 
            width: 100%; 
            margin: 1em 0;
        }
        th, td { 
            border: 1px solid #3c3c3c; 
            padding: 8px 12px; 
            text-align: left;
        }
        th { background: #2d2d2d; color: #569cd6; }
        tr:nth-child(even) { background: #252526; }
        img { max-width: 100%; height: auto; }
        hr { border: none; border-top: 1px solid #3c3c3c; margin: 2em 0; }
        ul, ol { padding-left: 1.5em; }
        li { margin: 0.3em 0; }
        .nav-link { 
            display: inline-block; 
            background: #2d2d2d; 
            padding: 4px 10px; 
            border-radius: 4px; 
            margin: 2px;
            border: 1px solid #3c3c3c;
        }
        .file-header {
            background: #252526;
            padding: 10px 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            border: 1px solid #3c3c3c;
            font-size: 12px;
            color: #888;
        }
        .callout {
            border-left: 4px solid;
            padding: 12px 16px;
            margin: 16px 0;
            border-radius: 0 6px 6px 0;
        }
        .callout-title { font-weight: bold; margin-bottom: 8px; }
        .callout-note, .callout-info { background: #1a3a5c; border-color: #0066cc; }
        .callout-tip, .callout-hint { background: #1a3c2a; border-color: #10b981; }
        .callout-warning, .callout-caution { background: #3c2a1a; border-color: #f59e0b; }
        .callout-danger, .callout-error { background: #3c1a1a; border-color: #dc2626; }
        .callout-question, .callout-help, .callout-faq { background: #2a1a3c; border-color: #8b5cf6; }
        .callout-example { background: #1a2a3c; border-color: #6366f1; }
        .callout-quote { background: #2a2a2a; border-color: #6b7280; }
        details.callout summary { cursor: pointer; }
        details.callout summary::-webkit-details-marker { display: none; }
        details.callout summary::before { content: '▶ '; font-size: 10px; }
        details.callout[open] summary::before { content: '▼ '; }
    </style>
    '''
    
    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    
    # Get the topic name from the main file
    topic_name = os.path.basename(filepath).replace('.md', '')
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        converted_files = []
        
        for rel_path in sorted(linked_pages):
            file_full_path = os.path.join(VAULT_PATH, rel_path)
            
            try:
                with open(file_full_path, 'r', encoding='utf-8') as f:
                    md_content = f.read()
            except:
                continue
            
            # Strip YAML frontmatter
            if md_content.startswith('---'):
                parts = md_content.split('---', 2)
                if len(parts) >= 3:
                    md_content = parts[2]
            
            # Helper function to convert image to base64 data URI
            def image_to_base64(image_path):
                """Convert an image file to base64 data URI"""
                import base64
                import mimetypes
                
                # Determine full path
                if os.path.isabs(image_path):
                    full_img_path = image_path
                else:
                    # Try relative to current markdown file
                    current_dir = os.path.dirname(file_full_path)
                    full_img_path = os.path.join(current_dir, image_path)
                    full_img_path = os.path.normpath(full_img_path)
                    
                    # If not found, try relative to vault
                    if not os.path.exists(full_img_path):
                        full_img_path = os.path.join(VAULT_PATH, image_path)
                    
                    # Try finding in vault using find_file_in_vault
                    if not os.path.exists(full_img_path):
                        found = find_file_in_vault(image_path)
                        if found:
                            full_img_path = os.path.join(VAULT_PATH, found)
                
                if not os.path.exists(full_img_path):
                    return None
                
                try:
                    mime_type, _ = mimetypes.guess_type(full_img_path)
                    if not mime_type:
                        ext = image_path.lower().rsplit('.', 1)[-1]
                        mime_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 
                                   'gif': 'image/gif', 'webp': 'image/webp', 'svg': 'image/svg+xml'}
                        mime_type = mime_map.get(ext, 'application/octet-stream')
                    
                    with open(full_img_path, 'rb') as img_file:
                        img_data = base64.b64encode(img_file.read()).decode('utf-8')
                    return f'data:{mime_type};base64,{img_data}'
                except Exception as e:
                    return None
            
            # Convert Obsidian image embeds ![[image.png]] to base64
            def replace_obsidian_image(match):
                inner = match.group(1)
                if '|' in inner:
                    link_part, alt_text = inner.split('|', 1)
                else:
                    link_part = inner
                    alt_text = inner
                
                ext = link_part.lower().rsplit('.', 1)[-1] if '.' in link_part else ''
                if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp']:
                    data_uri = image_to_base64(link_part)
                    if data_uri:
                        return f'![{alt_text}]({data_uri})'
                    else:
                        return f'![{alt_text} (image not found)]({link_part})'
                return match.group(0)  # Return unchanged for non-images
            
            md_content = re.sub(r'!\[\[([^\]]+)\]\]', replace_obsidian_image, md_content)
            
            # Also convert standard markdown images ![alt](path) to base64
            def replace_md_image(match):
                alt_text = match.group(1)
                img_path = match.group(2)
                
                # Skip if already a data URI or external URL
                if img_path.startswith('data:') or img_path.startswith('http'):
                    return match.group(0)
                
                data_uri = image_to_base64(img_path)
                if data_uri:
                    return f'![{alt_text}]({data_uri})'
                return match.group(0)  # Return unchanged if image not found
            
            md_content = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_md_image, md_content)
            
            # Process through callouts first
            md_content = convert_obsidian_callouts(md_content)
            
            # Fix list continuation (numbered items after nested bullets)
            md_content = fix_list_continuation(md_content)
            
            # Protect math expressions
            md_content, math_placeholders = protect_math_expressions(md_content)
            
            # Convert to HTML
            html_body = markdown.markdown(
                md_content,
                extensions=['tables', 'fenced_code', 'toc', 'nl2br', 'sane_lists']
            )
            
            # Restore math
            html_body = restore_math_expressions(html_body, math_placeholders)
            
            # Convert wiki links to relative HTML links within the ZIP
            def convert_wiki_link(match):
                inner = match.group(1)
                # Handle [[link|display]] format
                if '|' in inner:
                    link_part, display = inner.split('|', 1)
                else:
                    link_part = inner
                    display = inner.split('/')[-1]  # Use last part as display
                
                # Remove heading references for file resolution
                link_file = link_part.split('#')[0]
                heading = '#' + link_part.split('#')[1] if '#' in link_part else ''
                
                # Find if this file is in our linked pages
                current_dir = os.path.dirname(rel_path)
                possible = [
                    os.path.normpath(os.path.join(current_dir, link_file + '.md')),
                    os.path.normpath(link_file + '.md'),
                    os.path.normpath(os.path.join(current_dir, link_file)),
                    os.path.normpath(link_file)
                ]
                
                for p in possible:
                    if p in linked_pages:
                        # Calculate relative path from current file to linked file
                        current_dir = os.path.dirname(rel_path)
                        relative_link = os.path.relpath(p, current_dir) if current_dir else p
                        html_path = relative_link.replace('.md', '.html')
                        return f'<a href="{html_path}{heading}">{display}</a>'
                
                return f'<span class="broken-link">{display}</span>'
            
            html_body = re.sub(r'\[\[([^\]]+)\]\]', convert_wiki_link, html_body)
            
            # Build full HTML
            file_name = os.path.basename(rel_path).replace('.md', '')
            html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{file_name}</title>
    {css_content}
    <script>
        MathJax = {{
            tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']] }},
            svg: {{ fontCache: 'global' }}
        }};
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js" async></script>
</head>
<body>
    <div class="file-header">
        📄 {rel_path} | Exported: {datetime.now().strftime("%Y-%m-%d %H:%M")}
    </div>
    <h1>{file_name.replace('_', ' ')}</h1>
    {html_body}
</body>
</html>'''
            
            # Add to ZIP with folder structure preserved
            html_path = rel_path.replace('.md', '.html')
            zf.writestr(html_path, html_content.encode('utf-8'))
            converted_files.append((rel_path, html_path))
    
    zip_buffer.seek(0)
    zip_data = zip_buffer.getvalue()
    
    # Sanitize filename
    safe_topic = re.sub(r'[^\w\-]', '_', topic_name)
    zip_filename = f'{safe_topic}_Topic.zip'
    
    return Response(
        zip_data,
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{zip_filename}"',
            'Content-Length': str(len(zip_data)),
            'Content-Type': 'application/zip',
            'Cache-Control': 'no-cache'
        }
    )


@app.route('/api/access-log')
def api_access_log():
    """View IP access statistics"""
    # Sort IPs by access count
    sorted_ips = sorted(
        _access_log['ips'].items(),
        key=lambda x: x[1]['count'],
        reverse=True
    )
    
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Access Log - Obsidian Viewer</title>
        <style>
            body { font-family: -apple-system, sans-serif; background: #1e1e1e; color: #d4d4d4; padding: 20px; max-width: 1000px; margin: 0 auto; }
            h1 { color: #569cd6; }
            h2 { color: #4ec9b0; margin-top: 30px; }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #333; }
            th { background: #2d2d2d; color: #569cd6; }
            tr:hover { background: #2a2a2a; }
            .count { color: #f59e0b; font-weight: bold; }
            .time { color: #888; font-size: 12px; }
            .path { color: #ce9178; font-size: 12px; }
            .back { display: inline-block; margin-bottom: 20px; color: #4fc1ff; text-decoration: none; }
            .back:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <a class="back" href="/">← Back to Viewer</a>
        <h1>📊 Access Log</h1>
        
        <h2>IP Statistics (''' + str(len(_access_log['ips'])) + ''' unique IPs)</h2>
        <table>
            <tr><th>IP Address</th><th>Requests</th><th>First Seen</th><th>Last Seen</th><th>Recent Paths</th></tr>
    '''
    
    for ip, data in sorted_ips:
        paths = ', '.join(data['paths'][-3:]) if data['paths'] else '-'
        html += f'''
            <tr>
                <td>{ip}</td>
                <td class="count">{data['count']}</td>
                <td class="time">{data['first_seen']}</td>
                <td class="time">{data['last_seen']}</td>
                <td class="path">{paths}</td>
            </tr>
        '''
    
    html += '''
        </table>
        
        <h2>Recent Requests (last 20)</h2>
        <table>
            <tr><th>Time</th><th>IP</th><th>Path</th></tr>
    '''
    
    for entry in reversed(_access_log['recent'][-20:]):
        html += f'''
            <tr>
                <td class="time">{entry['time']}</td>
                <td>{entry['ip']}</td>
                <td class="path">{entry['path']}</td>
            </tr>
        '''
    
    html += '''
        </table>
    </body>
    </html>
    '''
    
    return html


def generate_quick_definitions_cheatsheet():
    """
    Generate a consolidated cheatsheet from all 📝 Quick Definitions for Exam callouts.
    
    Scans all MD files in the vault, extracts [!quote]+ 📝 Quick Definitions callouts,
    and generates a single cheatsheet.md file in a 'cheatsheet' folder.
    
    Returns:
        dict: {success: bool, files_scanned: int, definitions_found: int, error: str}
    """
    if not VAULT_PATH:
        return {'success': False, 'error': 'VAULT_PATH not set', 'files_scanned': 0, 'definitions_found': 0}
    
    cheatsheet_dir = os.path.join(VAULT_PATH, 'cheatsheet')
    cheatsheet_file = os.path.join(cheatsheet_dir, 'Quick Definitions Cheatsheet.md')
    
    # Create cheatsheet directory if it doesn't exist
    os.makedirs(cheatsheet_dir, exist_ok=True)
    
    # Pattern to match Quick Definitions callouts
    # Matches: > [!quote]+ 📝 Quick Definitions for Exam (or similar variations)
    callout_start_pattern = re.compile(
        r'^>\s*\[!quote\][+-]?\s*📝\s*Quick\s+Definitions.*$',
        re.IGNORECASE | re.MULTILINE
    )
    
    definitions_by_source = {}
    files_scanned = 0
    
    for root, dirs, files in os.walk(VAULT_PATH):
        # Skip hidden directories and the cheatsheet directory itself
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'cheatsheet']
        
        for filename in files:
            if not filename.endswith('.md') or filename.startswith('.'):
                continue
            
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, VAULT_PATH)
            files_scanned += 1
            
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue
            
            # Find Quick Definitions callouts
            lines = content.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i]
                
                # Check if this line starts a Quick Definitions callout
                if callout_start_pattern.match(line):
                    # Extract the callout content
                    callout_lines = []
                    i += 1
                    while i < len(lines) and lines[i].startswith('>'):
                        # Remove the > prefix and preserve content
                        callout_line = re.sub(r'^>\s?', '', lines[i])
                        callout_lines.append(callout_line)
                        i += 1
                    
                    if callout_lines:
                        callout_content = '\n'.join(callout_lines)
                        if rel_path not in definitions_by_source:
                            definitions_by_source[rel_path] = []
                        definitions_by_source[rel_path].append(callout_content)
                else:
                    i += 1
    
    # Generate the cheatsheet content
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cheatsheet_content = f"""---
title: Quick Definitions Cheatsheet
generated: {timestamp}
auto_generated: true
sources: {len(definitions_by_source)}
---

# 📝 Quick Definitions Cheatsheet

> [!info] Auto-Generated
> This file is automatically generated from all `📝 Quick Definitions for Exam` callouts found in the vault.
> 
> **Last synced:** {timestamp}
> **Source files:** {len(definitions_by_source)}
> **Total definitions sections:** {sum(len(v) for v in definitions_by_source.values())}
> 
> To update, click the **🔄 Sync** button in the toolbar.

---

"""
    
    if not definitions_by_source:
        cheatsheet_content += "> No Quick Definitions callouts found in the vault.\n"
    else:
        for source_path, definitions_list in sorted(definitions_by_source.items()):
            # Create a cleaner title from the path
            source_name = os.path.splitext(os.path.basename(source_path))[0]
            wiki_link_path = source_path.replace('.md', '').replace('\\', '/')
            
            cheatsheet_content += f"## 📄 [[{wiki_link_path}|{source_name}]]\n\n"
            
            for definition_block in definitions_list:
                # Add the definition content
                cheatsheet_content += definition_block + "\n\n"
            
            cheatsheet_content += "---\n\n"
    
    # Write the cheatsheet file
    try:
        with open(cheatsheet_file, 'w', encoding='utf-8') as f:
            f.write(cheatsheet_content)
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to write cheatsheet: {str(e)}',
            'files_scanned': files_scanned,
            'definitions_found': len(definitions_by_source)
        }
    
    return {
        'success': True,
        'files_scanned': files_scanned,
        'definitions_found': len(definitions_by_source),
        'total_sections': sum(len(v) for v in definitions_by_source.values()),
        'cheatsheet_path': cheatsheet_file
    }


def generate_memory_tips_cheatsheet():
    """
    Generate a consolidated cheatsheet from all 🧠 Memory Tip callouts.
    
    Scans all MD files in the vault, extracts [!brain] 🧠 Memory Tip callouts,
    and generates a single Memory Tips Cheatsheet.md file in a 'cheatsheet' folder.
    
    Returns:
        dict: {success: bool, files_scanned: int, tips_found: int, error: str}
    """
    if not VAULT_PATH:
        return {'success': False, 'error': 'VAULT_PATH not set', 'files_scanned': 0, 'tips_found': 0}
    
    cheatsheet_dir = os.path.join(VAULT_PATH, 'cheatsheet')
    cheatsheet_file = os.path.join(cheatsheet_dir, 'Memory Tips Cheatsheet.md')
    
    # Create cheatsheet directory if it doesn't exist
    os.makedirs(cheatsheet_dir, exist_ok=True)
    
    # Pattern to match Memory Tip callouts
    # Matches: > [!brain]- 🧠 Memory Tip: <title> (or similar variations)
    callout_start_pattern = re.compile(
        r'^>\s*\[!brain\][+-]?\s*🧠\s*Memory\s+Tip[:\s]*(.*?)$',
        re.IGNORECASE | re.MULTILINE
    )
    
    tips_by_source = {}
    files_scanned = 0
    
    for root, dirs, files in os.walk(VAULT_PATH):
        # Skip hidden directories and the cheatsheet directory itself
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'cheatsheet']
        
        for filename in files:
            if not filename.endswith('.md') or filename.startswith('.'):
                continue
            
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, VAULT_PATH)
            files_scanned += 1
            
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue
            
            # Find Memory Tip callouts
            lines = content.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i]
                
                # Check if this line starts a Memory Tip callout
                match = callout_start_pattern.match(line)
                if match:
                    tip_title = match.group(1).strip() if match.group(1) else ''
                    # Extract the callout content
                    callout_lines = []
                    i += 1
                    while i < len(lines) and lines[i].startswith('>'):
                        # Remove the > prefix and preserve content
                        callout_line = re.sub(r'^>\s?', '', lines[i])
                        callout_lines.append(callout_line)
                        i += 1
                    
                    if callout_lines:
                        callout_content = '\n'.join(callout_lines)
                        if rel_path not in tips_by_source:
                            tips_by_source[rel_path] = []
                        tips_by_source[rel_path].append({
                            'title': tip_title,
                            'content': callout_content
                        })
                else:
                    i += 1
    
    # Generate the cheatsheet content
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_tips = sum(len(v) for v in tips_by_source.values())
    
    cheatsheet_content = f"""---
title: Memory Tips Cheatsheet
generated: {timestamp}
auto_generated: true
sources: {len(tips_by_source)}
---

# 🧠 Memory Tips Cheatsheet

> [!info] Auto-Generated
> This file is automatically generated from all `🧠 Memory Tip` callouts found in the vault.
> 
> **Last synced:** {timestamp}
> **Source files:** {len(tips_by_source)}
> **Total memory tips:** {total_tips}
> 
> To update, click the **🔄 Sync** button in the toolbar.

---

"""
    
    if not tips_by_source:
        cheatsheet_content += "> No Memory Tip callouts found in the vault.\n"
    else:
        for source_path, tips_list in sorted(tips_by_source.items()):
            # Create a cleaner title from the path
            source_name = os.path.splitext(os.path.basename(source_path))[0]
            
            # Create Obsidian wiki link
            # Convert path to wiki link format (without .md extension)
            wiki_link_path = source_path.replace('.md', '').replace('\\', '/')
            
            cheatsheet_content += f"## 📄 [[{wiki_link_path}|{source_name}]]\n\n"
            
            for tip in tips_list:
                title = tip['title']
                content = tip['content']
                
                # Add the tip as a callout with title
                if title:
                    cheatsheet_content += f"> [!brain] 🧠 {title}\n"
                else:
                    cheatsheet_content += "> [!brain] 🧠 Memory Tip\n"
                
                # Add content lines with proper callout formatting
                for line in content.split('\n'):
                    cheatsheet_content += f"> {line}\n"
                
                cheatsheet_content += "\n"
            
            cheatsheet_content += "---\n\n"
    
    # Write the cheatsheet file
    try:
        with open(cheatsheet_file, 'w', encoding='utf-8') as f:
            f.write(cheatsheet_content)
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to write cheatsheet: {str(e)}',
            'files_scanned': files_scanned,
            'tips_found': total_tips
        }
    
    return {
        'success': True,
        'files_scanned': files_scanned,
        'tips_found': total_tips,
        'source_files': len(tips_by_source),
        'cheatsheet_path': cheatsheet_file
    }


@app.route('/api/sync-metadata', methods=['POST'])
def api_sync_metadata():
    """Sync metadata from obsidian-viewer-meta.json to file frontmatter and update index tables"""
    try:
        import subprocess
        
        # Scripts location
        scripts_dir = os.path.expanduser('~/clawd/scripts')
        
        # Run sync-obsidian-meta to sync JSON metadata to file frontmatter
        result1 = subprocess.run(
            ['python3', os.path.join(scripts_dir, 'sync-obsidian-meta')],
            capture_output=True, text=True, timeout=30
        )
        
        # Run update-index-tables to update the MD Files Index
        result2 = subprocess.run(
            ['python3', os.path.join(scripts_dir, 'update-index-tables')],
            capture_output=True, text=True, timeout=30
        )
        
        # Generate Quick Definitions Cheatsheet
        cheatsheet_result = generate_quick_definitions_cheatsheet()
        
        # Generate Memory Tips Cheatsheet
        memory_tips_result = generate_memory_tips_cheatsheet()
        
        # Rebuild file cache to pick up any frontmatter changes
        build_file_cache()
        
        return jsonify({
            'success': True,
            'sync_output': result1.stdout + result1.stderr,
            'update_output': result2.stdout + result2.stderr,
            'cheatsheet': cheatsheet_result,
            'memory_tips': memory_tips_result
        })
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Script timed out'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/generate-cheatsheet', methods=['POST'])
def api_generate_cheatsheet():
    """Generate the Quick Definitions Cheatsheet without running other sync operations"""
    try:
        result = generate_quick_definitions_cheatsheet()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/generate-memory-tips', methods=['POST'])
def api_generate_memory_tips():
    """Generate the Memory Tips Cheatsheet without running other sync operations"""
    try:
        result = generate_memory_tips_cheatsheet()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# SEARCH API
# ============================================

@app.route('/api/search')
def api_search():
    """Search for files and content in the vault"""
    query = request.args.get('q', '').strip().lower()
    search_content = request.args.get('content', 'false').lower() == 'true'
    limit = int(request.args.get('limit', 50))
    
    if not query or len(query) < 2:
        return jsonify({'success': False, 'error': 'Query too short', 'results': []})
    
    results = []
    query_terms = query.split()
    
    for root, dirs, filenames in os.walk(VAULT_PATH):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for filename in filenames:
            if filename.startswith('.'):
                continue
            
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, VAULT_PATH)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            
            # Only search viewable files
            if ext not in ['md', 'pdf', 'txt']:
                continue
            
            filename_lower = filename.lower()
            path_lower = rel_path.lower()
            
            # Calculate filename match score
            filename_score = 0
            for term in query_terms:
                if term in filename_lower:
                    filename_score += 10
                    if filename_lower.startswith(term):
                        filename_score += 5
                if term in path_lower:
                    filename_score += 2
            
            # Search content for .md files if requested
            content_match = None
            content_score = 0
            if search_content and ext == 'md' and filename_score == 0:
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read(50000)  # Read first 50KB
                        content_lower = content.lower()
                        
                        for term in query_terms:
                            if term in content_lower:
                                content_score += 1
                                # Find snippet
                                idx = content_lower.find(term)
                                if idx >= 0 and not content_match:
                                    start = max(0, idx - 40)
                                    end = min(len(content), idx + len(term) + 60)
                                    snippet = content[start:end].strip()
                                    # Clean up snippet
                                    snippet = ' '.join(snippet.split())
                                    if start > 0:
                                        snippet = '...' + snippet
                                    if end < len(content):
                                        snippet = snippet + '...'
                                    content_match = snippet
                except:
                    pass
            
            total_score = filename_score + content_score
            
            if total_score > 0:
                result = {
                    'path': rel_path,
                    'filename': filename,
                    'type': ext,
                    'score': total_score,
                    'url': f'/view/{rel_path}'
                }
                if content_match:
                    result['snippet'] = content_match
                results.append(result)
            
            if len(results) >= limit * 2:  # Get more than needed for sorting
                break
    
    # Sort by score (highest first) and limit
    results.sort(key=lambda x: (-x['score'], x['filename'].lower()))
    results = results[:limit]
    
    return jsonify({
        'success': True,
        'query': query,
        'results': results,
        'total': len(results)
    })

# PWA / OFFLINE SUPPORT
# ============================================

@app.route('/api/all-files')
def api_all_files():
    """List all files in the vault for offline caching"""
    files = []
    for root, dirs, filenames in os.walk(VAULT_PATH):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for filename in filenames:
            if filename.startswith('.'):
                continue
            
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, VAULT_PATH)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            
            # Include viewable files
            if ext in ['md', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']:
                files.append({
                    'path': rel_path,
                    'type': ext,
                    'url': f'/view/{rel_path}' if ext == 'md' else f'/raw/{rel_path}'
                })
    
    return jsonify({
        'success': True,
        'files': files,
        'total': len(files)
    })

@app.route('/manifest.json')
def pwa_manifest():
    """PWA manifest for installable web app"""
    manifest = {
        "name": f"Obsidian Viewer - {get_vault_name()}",
        "short_name": "Obsidian",
        "description": "View your Obsidian vault offline",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1e1e1e",
        "theme_color": "#7c3aed",
        "icons": [
            {
                "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>",
                "sizes": "any",
                "type": "image/svg+xml"
            }
        ]
    }
    return jsonify(manifest)

@app.route('/service-worker.js')
def service_worker():
    """Service worker for offline caching"""
    sw_code = '''
const CACHE_NAME = 'obsidian-viewer-v2';

self.addEventListener('install', (event) => {
    console.log('[SW] Installing...');
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    console.log('[SW] Activating...');
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('[SW] Deleting old cache:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    
    // Skip API calls - always go to network
    if (event.request.url.includes('/api/')) {
        return;
    }
    
    event.respondWith(
        caches.match(event.request).then((cachedResponse) => {
            // Return cached version if available
            if (cachedResponse) {
                console.log('[SW] Serving from cache:', event.request.url);
                return cachedResponse;
            }
            
            // Otherwise fetch from network
            return fetch(event.request).then((response) => {
                if (!response || response.status !== 200) {
                    return response;
                }
                
                // Cache successful responses
                const responseToCache = response.clone();
                caches.open(CACHE_NAME).then((cache) => {
                    cache.put(event.request, responseToCache);
                });
                
                return response;
            }).catch((error) => {
                console.log('[SW] Fetch failed:', error);
                // Return a basic offline message for navigation
                if (event.request.mode === 'navigate') {
                    return new Response('<html><body style="font-family:sans-serif;padding:40px;text-align:center;"><h1>📴 Offline</h1><p>This page is not cached. Connect to the server and try again.</p></body></html>', {
                        headers: { 'Content-Type': 'text/html' }
                    });
                }
                return new Response('Offline', { status: 503 });
            });
        })
    );
});

// Handle cache commands from main page
self.addEventListener('message', (event) => {
    if (event.data.action === 'cacheFiles') {
        const files = event.data.files;
        console.log('[SW] Caching', files.length, 'files...');
        
        caches.open(CACHE_NAME).then(async (cache) => {
            let cached = 0;
            const total = files.length;
            
            for (const file of files) {
                try {
                    const response = await fetch(file.url);
                    if (response.ok) {
                        await cache.put(file.url, response);
                        cached++;
                        
                        // Report progress every 5 files
                        if (cached % 5 === 0 || cached === total) {
                            self.clients.matchAll().then(clients => {
                                clients.forEach(client => {
                                    client.postMessage({ 
                                        action: 'cacheProgress', 
                                        cached, total,
                                        file: file.path
                                    });
                                });
                            });
                        }
                    }
                } catch (e) {
                    console.log('[SW] Failed to cache:', file.path);
                }
            }
            
            // Also cache the home page and current page
            try {
                const homeResponse = await fetch('/');
                await cache.put('/', homeResponse);
            } catch (e) {}
            
            console.log('[SW] Caching complete:', cached, '/', total);
            self.clients.matchAll().then(clients => {
                clients.forEach(client => {
                    client.postMessage({ action: 'cacheComplete', total: cached });
                });
            });
        });
    }
    
    if (event.data.action === 'clearCache') {
        caches.delete(CACHE_NAME).then(() => {
            self.clients.matchAll().then(clients => {
                clients.forEach(client => {
                    client.postMessage({ action: 'cacheCleared' });
                });
            });
        });
    }
    
    if (event.data.action === 'getCacheSize') {
        caches.open(CACHE_NAME).then(cache => {
            cache.keys().then(keys => {
                self.clients.matchAll().then(clients => {
                    clients.forEach(client => {
                        client.postMessage({ action: 'cacheSize', count: keys.length });
                    });
                });
            });
        });
    }
});
'''
    return Response(sw_code, mimetype='application/javascript')


# ============================================
# CLOZE API ENDPOINTS
# ============================================

@app.route('/api/cloze/<path:filepath>')
def api_get_cloze(filepath):
    """Get cloze deletions - checks JSON file first, falls back to parsing MD"""
    try:
        full_path = os.path.join(VAULT_PATH, filepath)
        
        if not os.path.exists(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # Check for study JSON first
        study_data = load_study_json(filepath)
        if study_data and study_data.get('cloze'):
            # Normalize JSON format: 'text' -> 'question' for frontend compatibility
            cloze_cards = []
            for card in study_data['cloze']:
                normalized = {
                    'question': card.get('text', card.get('question', '')),
                    'answers': card.get('answers', [])
                }
                cloze_cards.append(normalized)
            source = 'json'
        else:
            # Fallback: parse from MD file
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            cloze_cards = parse_cloze(content)
            source = 'md'
        
        # Get SRS data for these cards
        srs_data = load_srs_data(filepath)
        
        return jsonify({
            'success': True,
            'cloze': cloze_cards,
            'count': len(cloze_cards),
            'srs': srs_data,
            'source': source
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# SRS API ENDPOINTS
# ============================================

@app.route('/api/srs/<path:filepath>')
def api_get_srs(filepath):
    """Get SRS data for a file"""
    try:
        srs_data = load_srs_data(filepath)
        return jsonify({'success': True, 'srs': srs_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/srs/<path:filepath>', methods=['POST'])
def api_update_srs(filepath):
    """Update SRS data after a review"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        card_id = data.get('cardId')
        rating = data.get('rating')  # 1-4 for SRS, or 'correct'/'wrong' for Leitner
        card_type = data.get('cardType', 'flash')  # flash, mcq, cloze
        
        if not card_id or rating is None:
            return jsonify({'success': False, 'error': 'Missing cardId or rating'}), 400
        
        srs_data = load_srs_data(filepath)
        card_key = f'{card_type}-{card_id}'
        
        # Get existing card data or create new
        card_data = srs_data['cards'].get(card_key, {
            'interval': 1,
            'easeFactor': 2.5,
            'reps': 0,
            'lapses': 0,
            'box': 1,
            'focusStreak': 0
        })
        
        # Calculate new values based on mode
        if srs_data.get('mode') == 'leitner':
            correct = rating in [3, 4, 'correct', True]
            new_data = calculate_leitner_box(card_data, correct)
            card_data.update(new_data)
        else:
            # SRS mode
            if isinstance(rating, str):
                rating = {'again': 1, 'hard': 2, 'good': 3, 'easy': 4}.get(rating, 3)
            new_data = calculate_srs_interval(card_data, rating)
            card_data.update(new_data)
        
        # Update focus streak
        if rating in [3, 4, 'correct', 'good', 'easy', True]:
            card_data['focusStreak'] = card_data.get('focusStreak', 0) + 1
        else:
            card_data['focusStreak'] = 0
        
        srs_data['cards'][card_key] = card_data
        srs_data['lastReview'] = datetime.utcnow().isoformat() + 'Z'
        save_srs_data(filepath, srs_data)
        
        # Log study session
        log_study_session(filepath, card_type, rating in [3, 4, 'correct', 'good', 'easy', True])
        
        return jsonify({
            'success': True,
            'card': card_data,
            'nextReview': card_data.get('nextReview')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/srs/due')
def api_get_due_cards():
    """Get all cards due for review across all files"""
    try:
        due_cards = []
        now = datetime.utcnow().isoformat() + 'Z'
        
        for srs_data in scan_all_srs_files():
            filepath = srs_data.get('filePath', '')
            for card_key, card_data in srs_data.get('cards', {}).items():
                next_review = card_data.get('nextReview', '')
                if next_review and next_review <= now:
                    due_cards.append({
                        'filepath': filepath,
                        'cardKey': card_key,
                        'data': card_data
                    })
        
        return jsonify({
            'success': True,
            'dueCards': due_cards,
            'count': len(due_cards)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/srs/mode/<path:filepath>', methods=['POST'])
def api_set_srs_mode(filepath):
    """Set SRS mode (srs or leitner) for a file"""
    try:
        data = request.get_json()
        mode = data.get('mode', 'srs')
        
        if mode not in ['srs', 'leitner']:
            return jsonify({'success': False, 'error': 'Invalid mode'}), 400
        
        srs_data = load_srs_data(filepath)
        srs_data['mode'] = mode
        save_srs_data(filepath, srs_data)
        
        return jsonify({'success': True, 'mode': mode})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# STUDY SESSION & DASHBOARD API
# ============================================

def log_study_session(filepath, card_type, correct):
    """Log a study event to session history"""
    sessions = load_study_sessions()
    now = datetime.utcnow()
    today = now.strftime('%Y-%m-%d')
    hour = now.strftime('%H')
    
    if today not in sessions:
        sessions[today] = {
            'cardsReviewed': 0,
            'correct': 0,
            'wrong': 0,
            'timeSpentMs': 0,
            'files': [],
            'byType': {'flash': 0, 'mcq': 0, 'cloze': 0},
            'hourly': {}  # Track by hour
        }
    
    # Ensure hourly exists for older sessions
    if 'hourly' not in sessions[today]:
        sessions[today]['hourly'] = {}
    
    # Initialize hourly slot
    if hour not in sessions[today]['hourly']:
        sessions[today]['hourly'][hour] = {'cards': 0, 'correct': 0, 'wrong': 0}
    
    sessions[today]['cardsReviewed'] += 1
    sessions[today]['hourly'][hour]['cards'] += 1
    
    if correct:
        sessions[today]['correct'] += 1
        sessions[today]['hourly'][hour]['correct'] += 1
    else:
        sessions[today]['wrong'] += 1
        sessions[today]['hourly'][hour]['wrong'] += 1
    
    sessions[today]['byType'][card_type] = sessions[today]['byType'].get(card_type, 0) + 1
    
    if filepath not in sessions[today]['files']:
        sessions[today]['files'].append(filepath)
    
    save_study_sessions(sessions)

@app.route('/api/study/dashboard')
@app.route('/api/study/dashboard/<path:folder_path>')
def api_study_dashboard(folder_path=None):
    """Get study dashboard data with optional time view and folder filter
    
    Args:
        folder_path: Optional URL path parameter to filter stats by folder
    """
    try:
        sessions = load_study_sessions()
        settings = load_study_settings()
        view = request.args.get('view', 'day')  # day, week, month
        
        today = datetime.utcnow().strftime('%Y-%m-%d')
        today_data = sessions.get(today, {'cardsReviewed': 0, 'correct': 0, 'wrong': 0})
        
        # Calculate streak (vault-wide, not folder-specific)
        streak = 0
        check_date = datetime.utcnow()
        while True:
            date_str = check_date.strftime('%Y-%m-%d')
            if date_str in sessions and sessions[date_str].get('cardsReviewed', 0) >= settings.get('dailyGoal', 50):
                streak += 1
                check_date -= timedelta(days=1)
            else:
                break
        
        # Count due cards (filtered by folder if specified)
        due_count = 0
        total_cards = 0
        mastered_cards = 0
        weak_cards = 0
        now = datetime.utcnow().isoformat() + 'Z'
        
        for srs_data in scan_all_srs_files(folder_path):
            for card_key, card_data in srs_data.get('cards', {}).items():
                total_cards += 1
                
                # Check if due
                next_review = card_data.get('nextReview', '')
                if next_review and next_review <= now:
                    due_count += 1
                
                # Check if mastered (box 4-5 or interval > 7 days)
                if card_data.get('box', 1) >= 4 or card_data.get('interval', 0) >= 7:
                    mastered_cards += 1
                
                # Check if weak (lapses >= 1 for immediate feedback)
                if card_data.get('lapses', 0) >= 1 or card_data.get('easeFactor', 2.5) < 2.0:
                    weak_cards += 1
        
        # Build heatmap (last 30 days)
        heatmap = {}
        for i in range(30):
            date = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            if date in sessions:
                heatmap[date] = sessions[date].get('cardsReviewed', 0)
            else:
                heatmap[date] = 0
        
        # Build time-based views
        now = datetime.utcnow()
        
        # Hourly view - today's activity by hour
        hourly_data = []
        today_session = sessions.get(today, {})
        hourly_breakdown = today_session.get('hourly', {})
        for h in range(24):
            hour_str = f'{h:02d}'
            hour_data = hourly_breakdown.get(hour_str, {})
            hourly_data.append({
                'label': f'{h:02d}:00',
                'hour': h,
                'cards': hour_data.get('cards', 0),
                'correct': hour_data.get('correct', 0),
                'wrong': hour_data.get('wrong', 0)
            })
        
        # Daily view - last 7 days, cards per day
        daily_data = []
        for i in range(6, -1, -1):
            date = (now - timedelta(days=i)).strftime('%Y-%m-%d')
            day_name = (now - timedelta(days=i)).strftime('%a')
            session = sessions.get(date, {})
            daily_data.append({
                'label': day_name,
                'date': date,
                'cards': session.get('cardsReviewed', 0),
                'correct': session.get('correct', 0),
                'wrong': session.get('wrong', 0),
                'timeMs': session.get('timeSpentMs', 0)
            })
        
        # Weekly view - last 4 weeks
        weekly_data = []
        for w in range(3, -1, -1):
            week_start = now - timedelta(days=now.weekday() + (w * 7))
            week_end = week_start + timedelta(days=6)
            week_label = f"{week_start.strftime('%b %d')}"
            
            total_cards = 0
            total_correct = 0
            total_wrong = 0
            total_time = 0
            
            for d in range(7):
                date = (week_start + timedelta(days=d)).strftime('%Y-%m-%d')
                session = sessions.get(date, {})
                total_cards += session.get('cardsReviewed', 0)
                total_correct += session.get('correct', 0)
                total_wrong += session.get('wrong', 0)
                total_time += session.get('timeSpentMs', 0)
            
            weekly_data.append({
                'label': week_label,
                'cards': total_cards,
                'correct': total_correct,
                'wrong': total_wrong,
                'timeMs': total_time
            })
        
        # Monthly view - last 6 months
        monthly_data = []
        for m in range(5, -1, -1):
            month_date = now - timedelta(days=m * 30)
            month_label = month_date.strftime('%b')
            month_str = month_date.strftime('%Y-%m')
            
            total_cards = 0
            total_correct = 0
            total_wrong = 0
            total_time = 0
            
            for date_str, session in sessions.items():
                if date_str.startswith(month_str):
                    total_cards += session.get('cardsReviewed', 0)
                    total_correct += session.get('correct', 0)
                    total_wrong += session.get('wrong', 0)
                    total_time += session.get('timeSpentMs', 0)
            
            monthly_data.append({
                'label': month_label,
                'cards': total_cards,
                'correct': total_correct,
                'wrong': total_wrong,
                'timeMs': total_time
            })
        
        return jsonify({
            'success': True,
            'folderPath': folder_path,
            'dashboard': {
                'today': {
                    'reviewed': today_data.get('cardsReviewed', 0),
                    'correct': today_data.get('correct', 0),
                    'wrong': today_data.get('wrong', 0),
                    'goal': settings.get('dailyGoal', 50)
                },
                'streak': streak,
                'dueCount': due_count,
                'totalCards': total_cards,
                'masteredCards': mastered_cards,
                'weakCards': weak_cards,
                'masteryPercent': round((mastered_cards / total_cards * 100) if total_cards > 0 else 0, 1),
                'heatmap': heatmap,
                'hourly': hourly_data,
                'daily': daily_data,
                'weekly': weekly_data,
                'monthly': monthly_data
            }
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/api/study/settings', methods=['GET'])
def api_get_study_settings():
    """Get study settings"""
    try:
        settings = load_study_settings()
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/study/settings', methods=['POST'])
def api_set_study_settings():
    """Update study settings"""
    try:
        data = request.get_json()
        settings = load_study_settings()
        
        if 'dailyGoal' in data:
            settings['dailyGoal'] = int(data['dailyGoal'])
        if 'timerSeconds' in data:
            settings['timerSeconds'] = int(data['timerSeconds'])
        if 'srsMode' in data:
            settings['srsMode'] = data['srsMode']
        if 'timedMode' in data:
            settings['timedMode'] = bool(data['timedMode'])
        if 'confidenceRating' in data:
            settings['confidenceRating'] = bool(data['confidenceRating'])
        
        save_study_settings(settings)
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/study/review-cards')
def api_get_review_cards():
    """Get cards to review for the review page
    
    Query params:
        mode: 'due' (default) or 'weak'
        folder: optional folder path to filter cards
    """
    try:
        mode = request.args.get('mode', 'due')
        folder_path = request.args.get('folder', None)
        
        cards = []
        now = datetime.utcnow().isoformat() + 'Z'
        
        for srs_data in scan_all_srs_files(folder_path):
            filepath = srs_data.get('filePath', '')
            
            # Get the study.json for this file to get flashcard questions/answers
            study_data = load_study_json(filepath)
            flashcards = study_data.get('flashcards', []) if study_data else []
            
            for card_key, card_data in srs_data.get('cards', {}).items():
                include = False
                
                if mode == 'due':
                    # Include if card is due for review
                    next_review = card_data.get('nextReview', '')
                    if next_review and next_review <= now:
                        include = True
                elif mode == 'weak':
                    # Include if card is weak (lapses >= 1 or low ease factor)
                    if card_data.get('lapses', 0) >= 1 or card_data.get('easeFactor', 2.5) < 2.0:
                        if card_data.get('focusStreak', 0) < 3:  # Not yet graduated
                            include = True
                
                if include:
                    # Find the flashcard content
                    question = ''
                    answer = ''
                    
                    # Parse card_key to find the flashcard
                    # Format is usually like 'flashcard_0' or 'fc_0'
                    for i, fc in enumerate(flashcards):
                        fc_key = f'flashcard_{i}'
                        if card_key == fc_key or card_key == f'fc_{i}':
                            question = fc.get('front', fc.get('question', 'Question not found'))
                            answer = fc.get('back', fc.get('answer', 'Answer not found'))
                            break
                    
                    if not question:
                        # Try MCQ or cloze
                        mcqs = study_data.get('mcq', []) if study_data else []
                        for i, mcq in enumerate(mcqs):
                            if card_key == f'mcq_{i}':
                                question = mcq.get('question', 'Question not found')
                                options = mcq.get('options', [])
                                correct = mcq.get('correctIndex', 0)
                                answer = options[correct] if correct < len(options) else 'Answer not found'
                                break
                        
                        clozes = study_data.get('cloze', []) if study_data else []
                        for i, cloze in enumerate(clozes):
                            if card_key == f'cloze_{i}':
                                question = cloze.get('text', '').replace('{{c1::', '[___]').replace('}}', '')
                                # Extract cloze answer
                                import re
                                match = re.search(r'\{\{c1::([^}]+)\}\}', cloze.get('text', ''))
                                answer = match.group(1) if match else 'Answer not found'
                                break
                    
                    if question:
                        # Get readable source name
                        source = os.path.basename(filepath).replace('.md', '') if filepath else 'Unknown'
                        
                        cards.append({
                            'filepath': filepath,
                            'cardKey': card_key,
                            'source': source,
                            'question': question,
                            'answer': answer,
                            'data': card_data
                        })
        
        # Shuffle cards for variety
        import random
        random.shuffle(cards)
        
        return jsonify({
            'success': True,
            'cards': cards,
            'count': len(cards),
            'mode': mode,
            'folder': folder_path
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/study/weak-cards')
def api_get_weak_cards():
    """Get all weak cards (frequently missed)"""
    try:
        weak_cards = []
        
        for srs_data in scan_all_srs_files():
            filepath = srs_data.get('filePath', '')
            for card_key, card_data in srs_data.get('cards', {}).items():
                # Card is weak if: lapses >= 1 OR easeFactor < 2.0 (lowered threshold for better tracking)
                if card_data.get('lapses', 0) >= 1 or card_data.get('easeFactor', 2.5) < 2.0:
                    # Only include if not yet graduated from focus mode (need 3 correct in a row)
                    if card_data.get('focusStreak', 0) < 3:
                        weak_cards.append({
                            'filepath': filepath,
                            'cardKey': card_key,
                            'data': card_data
                        })
        
        return jsonify({
            'success': True,
            'weakCards': weak_cards,
            'count': len(weak_cards)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/study/weak-cards-full')
def api_get_weak_cards_full():
    """Get weak cards with full content for mix mode review.
    
    Query params:
        filepath (optional): Filter to only this file's weak cards
    """
    try:
        weak_cards_full = []
        filter_filepath = request.args.get('filepath', None)
        
        # First get weak card references
        weak_refs = {}  # filepath -> [card_keys]
        for srs_data in scan_all_srs_files():
            filepath = srs_data.get('filePath', '')
            
            # If filtering by filepath, skip non-matching files
            if filter_filepath and filepath != filter_filepath:
                continue
                
            for card_key, card_data in srs_data.get('cards', {}).items():
                # Card is weak if: lapses >= 1 OR easeFactor < 2.0 (lowered threshold)
                if card_data.get('lapses', 0) >= 1 or card_data.get('easeFactor', 2.5) < 2.0:
                    if card_data.get('focusStreak', 0) < 3:
                        if filepath not in weak_refs:
                            weak_refs[filepath] = []
                        weak_refs[filepath].append({
                            'key': card_key,
                            'lapses': card_data.get('lapses', 0),
                            'easeFactor': card_data.get('easeFactor', 2.5)
                        })
        
        # Load actual card content for each weak card
        for filepath, card_refs in weak_refs.items():
            study_data = load_study_json(filepath)
            if not study_data:
                # Try parsing from MD
                full_path = os.path.join(VAULT_PATH, filepath)
                if os.path.exists(full_path):
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    flashcards = parse_flashcards(content)
                    mcqs = parse_mcq(content)
                    cloze = parse_cloze(content)
                    study_data = {'flashcards': flashcards, 'mcq': mcqs, 'cloze': cloze}
            
            if study_data:
                flashcards = study_data.get('flashcards', [])
                mcqs = [normalize_mcq(m) for m in study_data.get('mcq', [])]
                cloze = [normalize_cloze(c) for c in study_data.get('cloze', [])]
                
                for ref in card_refs:
                    key = ref['key']
                    card_type, idx_str = key.split('-') if '-' in key else (key, '0')
                    try:
                        idx = int(idx_str)
                    except:
                        continue
                    
                    card = None
                    if card_type == 'flash' and idx < len(flashcards):
                        card = flashcards[idx].copy()
                        card['type'] = 'flash'
                    elif card_type == 'mcq' and idx < len(mcqs):
                        card = mcqs[idx].copy()
                        card['type'] = 'mcq'
                    elif card_type == 'cloze' and idx < len(cloze):
                        card = cloze[idx].copy()
                        card['type'] = 'cloze'
                    
                    if card:
                        card['id'] = key
                        card['filepath'] = filepath
                        card['lapses'] = ref['lapses']
                        weak_cards_full.append(card)
        
        # Sort by lapses (most mistakes first)
        weak_cards_full.sort(key=lambda x: x.get('lapses', 0), reverse=True)
        
        return jsonify({
            'success': True,
            'cards': weak_cards_full,
            'count': len(weak_cards_full)
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


# ============================================
# COMBINED STUDY MODE API
# ============================================

def normalize_mcq(mcq):
    """
    Normalize MCQ format from JSON (options as objects) to frontend format (options as strings + correct index).
    JSON format: {options: [{correct: bool, text: str}, ...]}
    Frontend format: {options: [str, ...], correct: int}
    """
    if not mcq.get('options'):
        return mcq
    
    # Check if options are already in the simple string format
    if mcq['options'] and isinstance(mcq['options'][0], str):
        return mcq  # Already normalized
    
    # Convert from object format to string format
    normalized = {k: v for k, v in mcq.items() if k != 'options'}
    normalized['options'] = []
    normalized['correct'] = -1
    
    for i, opt in enumerate(mcq['options']):
        if isinstance(opt, dict):
            normalized['options'].append(opt.get('text', str(opt)))
            if opt.get('correct'):
                normalized['correct'] = i
        else:
            normalized['options'].append(str(opt))
    
    return normalized


def normalize_cloze(cloze):
    """
    Normalize cloze format from JSON to frontend format.
    JSON format may have 'text' field, frontend expects 'question'.
    Also ensures 'answer' field exists for display in mix mode.
    
    Handles answers in multiple formats:
    - List of strings: ["answer1", "answer2"]
    - List of dicts: [{"answer": "answer1"}, {"answer": "answer2"}]
    
    Converts cloze markers in question to blanks:
    - {{c1::text}} -> <span class="cloze-blank">[...]</span>
    - {{text}} -> <span class="cloze-blank">[...]</span>
    - {{text|hint}} -> <span class="cloze-blank">[hint]</span>
    """
    import re
    
    # Extract answers, handling both formats
    raw_answers = cloze.get('answers', [])
    if raw_answers and isinstance(raw_answers[0], dict):
        # Format: [{"answer": "text"}, ...]
        answers = [a.get('answer', '') for a in raw_answers]
    else:
        # Format: ["text", ...] or empty
        answers = raw_answers
    
    # Clean up cloze markers from answers (e.g., "c1::Duration" -> "Duration")
    clean_answers = []
    for ans in answers:
        if '::' in str(ans):
            clean_answers.append(ans.split('::')[-1])
        else:
            clean_answers.append(ans)
    
    # Process question text to convert cloze markers to blanks
    question_text = cloze.get('text', cloze.get('question', ''))
    
    # Pattern 1: {{c1::text}} or {{c1::text::hint}} - Anki style
    def replace_anki_cloze(match):
        hint = match.group(2) if match.group(2) else '...'
        return f'<span class="cloze-blank">[{hint}]</span>'
    question_text = re.sub(r'\{\{c\d+::([^}:]+)(?:::([^}]+))?\}\}', replace_anki_cloze, question_text)
    
    # Pattern 2: {{text}} or {{text|hint}} - simple style
    def replace_simple_cloze(match):
        parts = match.group(1).split('|')
        hint = parts[1] if len(parts) > 1 else '...'
        return f'<span class="cloze-blank">[{hint}]</span>'
    question_text = re.sub(r'\{\{([^}]+)\}\}', replace_simple_cloze, question_text)
    
    normalized = {
        'question': question_text,
        'answers': clean_answers,
        'answer': cloze.get('answer', ', '.join(clean_answers))
    }
    # Preserve any other fields
    for k, v in cloze.items():
        if k not in ('text', 'question', 'answers', 'answer'):
            normalized[k] = v
    return normalized


@app.route('/api/study/all-cards/<path:filepath>')
def api_get_all_study_cards(filepath):
    """Get all study cards (flashcards + MCQ + cloze) for a file - JSON first, then MD"""
    try:
        full_path = os.path.join(VAULT_PATH, filepath)
        
        if not os.path.exists(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # Check for study JSON first
        study_data = load_study_json(filepath)
        if study_data:
            flashcards = study_data.get('flashcards', [])
            mcqs = [normalize_mcq(m) for m in study_data.get('mcq', [])]
            cloze = [normalize_cloze(c) for c in study_data.get('cloze', [])]
            source = 'json'
        else:
            # Fallback: parse from MD file
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            flashcards = parse_flashcards(content)
            mcqs = parse_mcq(content)
            cloze = parse_cloze(content)
            source = 'md'
        
        # Add type markers
        all_cards = []
        for i, card in enumerate(flashcards):
            card['type'] = 'flash'
            card['id'] = f'flash-{i}'
            all_cards.append(card)
        
        for i, card in enumerate(mcqs):
            card['type'] = 'mcq'
            card['id'] = f'mcq-{i}'
            all_cards.append(card)
        
        for i, card in enumerate(cloze):
            card['type'] = 'cloze'
            card['id'] = f'cloze-{i}'
            all_cards.append(card)
        
        # Get SRS data
        srs_data = load_srs_data(filepath)
        
        return jsonify({
            'success': True,
            'cards': all_cards,
            'counts': {
                'flash': len(flashcards),
                'mcq': len(mcqs),
                'cloze': len(cloze),
                'total': len(all_cards)
            },
            'srs': srs_data,
            'source': source
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/summary/<path:filepath>')
def api_get_summary(filepath):
    """Get auto-generated summary for a markdown file"""
    try:
        full_path = os.path.join(VAULT_PATH, filepath)
        
        if not os.path.exists(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        summary = parse_summary(content)
        
        return jsonify({
            'success': True,
            'summary': summary
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# EXAM SIMULATION FEATURE
# ============================================

def parse_exam_sections(content):
    """
    Parse an exam markdown file into sections and questions.
    
    Returns a list of sections, each with:
    - title: Section name
    - points: Total points for the section
    - questions: List of questions/subsections
    - rawContent: The raw markdown content
    """
    sections = []
    
    # Split by ## headers (main sections)
    section_pattern = re.compile(r'^##\s+(\d+)\s+(.+?)$\n\*\*Total:\s*(\d+)\s*Points?\*\*', re.MULTILINE)
    
    # Find all section headers with their positions
    section_matches = list(section_pattern.finditer(content))
    
    for i, match in enumerate(section_matches):
        section_num = match.group(1)
        section_title = match.group(2).strip()
        section_points = int(match.group(3))
        section_start = match.end()
        
        # Find end of section (start of next section or end of content)
        if i < len(section_matches) - 1:
            section_end = section_matches[i + 1].start()
        else:
            section_end = len(content)
        
        section_content = content[section_start:section_end].strip()
        
        # Parse subsections (### headers)
        subsections = []
        subsection_pattern = re.compile(r'^###\s+(\d+\.\d+)\s+(.+?)$(?:\n\*\*\((\d+)\s*Points?\)\*\*)?', re.MULTILINE)
        
        subsection_matches = list(subsection_pattern.finditer(section_content))
        
        if subsection_matches:
            for j, sub_match in enumerate(subsection_matches):
                sub_num = sub_match.group(1)
                sub_title = sub_match.group(2).strip()
                sub_points_match = sub_match.group(3)
                sub_points = int(sub_points_match) if sub_points_match else 0
                
                # Try to extract points from title if not in separate line
                if sub_points == 0:
                    points_in_title = re.search(r'\((\d+)\s*Points?\)', sub_title)
                    if points_in_title:
                        sub_points = int(points_in_title.group(1))
                        sub_title = re.sub(r'\s*\(\d+\s*Points?\)', '', sub_title).strip()
                
                sub_start = sub_match.end()
                if j < len(subsection_matches) - 1:
                    sub_end = subsection_matches[j + 1].start()
                else:
                    sub_end = len(section_content)
                
                sub_content = section_content[sub_start:sub_end].strip()
                
                subsections.append({
                    'number': sub_num,
                    'title': sub_title,
                    'points': sub_points,
                    'content': sub_content
                })
        else:
            # No subsections, use the entire section as one question
            subsections.append({
                'number': section_num,
                'title': section_title,
                'points': section_points,
                'content': section_content
            })
        
        sections.append({
            'number': section_num,
            'title': section_title,
            'points': section_points,
            'questions': subsections,
            'rawContent': section_content
        })
    
    # If no sections found with the pattern, try a simpler approach
    if not sections:
        # Try to split by ## headers without the strict pattern
        simple_pattern = re.compile(r'^##\s+(\d+)?\s*(.+?)$', re.MULTILINE)
        matches = list(simple_pattern.finditer(content))
        
        for i, match in enumerate(matches):
            section_num = match.group(1) or str(i + 1)
            section_title = match.group(2).strip()
            
            # Try to extract points from title
            points_match = re.search(r'\*\*Total:\s*(\d+)\s*Points?\*\*', content[match.end():match.end()+200])
            section_points = int(points_match.group(1)) if points_match else 10
            
            section_start = match.end()
            if i < len(matches) - 1:
                section_end = matches[i + 1].start()
            else:
                section_end = len(content)
            
            section_content = content[section_start:section_end].strip()
            
            sections.append({
                'number': section_num,
                'title': section_title,
                'points': section_points,
                'questions': [{
                    'number': section_num,
                    'title': section_title,
                    'points': section_points,
                    'content': section_content
                }],
                'rawContent': section_content
            })
    
    return sections


@app.route('/api/exam/<path:filepath>')
def api_get_exam(filepath):
    """Get parsed exam data for simulation"""
    try:
        full_path = os.path.join(VAULT_PATH, filepath)
        
        if not os.path.exists(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        sections = parse_exam_sections(content)
        
        # Calculate total points and suggested time per question
        total_points = sum(s['points'] for s in sections)
        
        return jsonify({
            'success': True,
            'sections': sections,
            'totalPoints': total_points,
            'filename': os.path.basename(filepath)
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/exam/<path:filepath>')
def exam_simulation(filepath):
    """Exam simulation view"""
    full_path = os.path.join(VAULT_PATH, filepath)
    
    if not os.path.exists(full_path):
        abort(404)
    
    # Get exam data
    with open(full_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    sections = parse_exam_sections(content)
    total_points = sum(s['points'] for s in sections)
    
    # Calculate all questions flattened
    all_questions = []
    for section in sections:
        for q in section['questions']:
            all_questions.append({
                'sectionNum': section['number'],
                'sectionTitle': section['title'],
                'number': q['number'],
                'title': q['title'],
                'points': q['points'],
                'content': q['content']
            })
    
    filename = os.path.basename(filepath)
    current_dir = os.path.dirname(filepath)
    
    return render_template_string(EXAM_TEMPLATE, 
        filename=filename,
        filepath=filepath,
        sections=sections,
        all_questions=all_questions,
        total_points=total_points,
        total_questions=len(all_questions),
        current_dir=current_dir,
        content=content)


EXAM_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Exam Simulation - {{ filename }}</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📝</text></svg>">
    <script>
        MathJax = {
            tex: { inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']] },
            svg: { fontCache: 'global' }
        };
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js" async></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        :root {
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: #0f3460;
            --text-primary: #eaeaea;
            --text-secondary: #a0a0a0;
            --accent: #e94560;
            --accent-green: #00d68f;
            --accent-yellow: #ffcc00;
            --accent-blue: #4da6ff;
            --border-color: #2a2a4a;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        
        /* Top Timer Bar */
        .timer-bar {
            background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-card) 100%);
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid var(--accent);
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        
        .timer-main {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        
        .timer-display {
            font-size: 2.5em;
            font-weight: bold;
            font-family: 'SF Mono', Monaco, monospace;
            color: var(--accent-green);
            text-shadow: 0 0 20px rgba(0, 214, 143, 0.3);
        }
        
        .timer-display.warning {
            color: var(--accent-yellow);
            animation: pulse 1s infinite;
        }
        
        .timer-display.danger {
            color: var(--accent);
            animation: pulse 0.5s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        
        .timer-label {
            font-size: 0.9em;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .timer-controls {
            display: flex;
            gap: 10px;
        }
        
        .timer-btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .timer-btn.start { background: var(--accent-green); color: #000; }
        .timer-btn.pause { background: var(--accent-yellow); color: #000; }
        .timer-btn.reset { background: var(--text-secondary); color: #000; }
        .timer-btn:hover { transform: translateY(-2px); filter: brightness(1.1); }
        
        .progress-info {
            text-align: right;
        }
        
        .progress-text {
            font-size: 1.1em;
            margin-bottom: 5px;
        }
        
        .progress-bar-container {
            width: 200px;
            height: 8px;
            background: var(--border-color);
            border-radius: 4px;
            overflow: hidden;
        }
        
        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--accent-green), var(--accent-blue));
            transition: width 0.3s ease;
        }
        
        /* Main Content Layout */
        .exam-container {
            display: flex;
            flex: 1;
            overflow: hidden;
        }
        
        /* Question Navigator Sidebar */
        .question-nav {
            width: 280px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border-color);
            overflow-y: auto;
            padding: 20px;
        }
        
        .nav-section {
            margin-bottom: 20px;
        }
        
        .nav-section-title {
            font-size: 0.85em;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
            padding-bottom: 5px;
            border-bottom: 1px solid var(--border-color);
        }
        
        .nav-questions {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        
        .nav-question-btn {
            width: 45px;
            height: 45px;
            border: 2px solid var(--border-color);
            border-radius: 8px;
            background: var(--bg-card);
            color: var(--text-primary);
            font-weight: 600;
            font-size: 0.9em;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        
        .nav-question-btn:hover {
            border-color: var(--accent-blue);
            transform: scale(1.05);
        }
        
        .nav-question-btn.active {
            background: var(--accent-blue);
            border-color: var(--accent-blue);
            color: #fff;
        }
        
        .nav-question-btn.answered {
            background: var(--accent-green);
            border-color: var(--accent-green);
            color: #000;
        }
        
        .nav-question-btn.flagged {
            border-color: var(--accent-yellow);
            box-shadow: 0 0 10px rgba(255, 204, 0, 0.3);
        }
        
        .nav-question-btn .points {
            font-size: 0.6em;
            opacity: 0.7;
        }
        
        /* Question Content Area */
        .question-area {
            flex: 1;
            overflow-y: auto;
            padding: 30px 40px;
        }
        
        .question-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid var(--border-color);
        }
        
        .question-title {
            font-size: 1.5em;
            font-weight: 600;
        }
        
        .question-meta {
            display: flex;
            gap: 15px;
            align-items: center;
        }
        
        .question-points {
            background: var(--accent-blue);
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
        }
        
        .question-time-suggest {
            background: var(--bg-card);
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 0.9em;
            display: flex;
            align-items: center;
            gap: 5px;
        }
        
        .question-time-suggest .time-icon {
            font-size: 1.2em;
        }
        
        .question-content {
            background: var(--bg-card);
            padding: 25px;
            border-radius: 12px;
            margin-bottom: 20px;
            line-height: 1.8;
            font-size: 1.05em;
        }
        
        .question-content h1, .question-content h2, .question-content h3 {
            color: var(--accent-blue);
            margin: 20px 0 10px;
        }
        
        .question-content h1:first-child, .question-content h2:first-child, .question-content h3:first-child {
            margin-top: 0;
        }
        
        .question-content table {
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
        }
        
        .question-content th, .question-content td {
            border: 1px solid var(--border-color);
            padding: 10px;
            text-align: left;
        }
        
        .question-content th {
            background: var(--bg-secondary);
        }
        
        .question-content code {
            background: var(--bg-secondary);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'SF Mono', Monaco, monospace;
        }
        
        .question-content pre {
            background: #0d1117;
            padding: 15px;
            border-radius: 8px;
            overflow-x: auto;
        }
        
        .question-content pre code {
            background: none;
            padding: 0;
        }
        
        .question-content img {
            max-width: 100%;
            border-radius: 8px;
            margin: 15px 0;
        }
        
        .question-content blockquote {
            border-left: 4px solid var(--accent-blue);
            padding-left: 15px;
            margin: 15px 0;
            color: var(--text-secondary);
        }
        
        /* Answer Area */
        .answer-area {
            margin-top: 20px;
        }
        
        .answer-label {
            font-weight: 600;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .answer-textarea {
            width: 100%;
            min-height: 200px;
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            border-radius: 10px;
            padding: 15px;
            color: var(--text-primary);
            font-size: 1em;
            font-family: inherit;
            resize: vertical;
            transition: border-color 0.2s;
        }
        
        .answer-textarea:focus {
            outline: none;
            border-color: var(--accent-blue);
        }
        
        .answer-actions {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        
        .action-btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .action-btn.flag {
            background: var(--bg-card);
            color: var(--accent-yellow);
            border: 2px solid var(--accent-yellow);
        }
        
        .action-btn.flag.flagged {
            background: var(--accent-yellow);
            color: #000;
        }
        
        .action-btn.prev, .action-btn.next {
            background: var(--accent-blue);
            color: #fff;
        }
        
        .action-btn.submit {
            background: var(--accent-green);
            color: #000;
        }
        
        .action-btn:hover {
            transform: translateY(-2px);
            filter: brightness(1.1);
        }
        
        .action-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        
        /* Navigation Footer */
        .nav-footer {
            display: flex;
            justify-content: space-between;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid var(--border-color);
        }
        
        /* Start Screen Overlay */
        .start-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.9);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        
        .start-card {
            background: var(--bg-card);
            padding: 40px 50px;
            border-radius: 20px;
            text-align: center;
            max-width: 500px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
        }
        
        .start-card h1 {
            font-size: 2em;
            margin-bottom: 10px;
        }
        
        .start-card .exam-name {
            color: var(--accent-blue);
            font-size: 1.2em;
            margin-bottom: 30px;
        }
        
        .start-stats {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .start-stat {
            background: var(--bg-secondary);
            padding: 15px;
            border-radius: 10px;
        }
        
        .start-stat-value {
            font-size: 2em;
            font-weight: bold;
            color: var(--accent-green);
        }
        
        .start-stat-label {
            font-size: 0.85em;
            color: var(--text-secondary);
            text-transform: uppercase;
        }
        
        .time-input-group {
            margin-bottom: 30px;
        }
        
        .time-input-group label {
            display: block;
            margin-bottom: 10px;
            font-weight: 600;
        }
        
        .time-input {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }
        
        .time-input input {
            width: 80px;
            padding: 10px;
            font-size: 1.5em;
            text-align: center;
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
        }
        
        .time-input input:focus {
            outline: none;
            border-color: var(--accent-blue);
        }
        
        .time-input span {
            font-size: 1.2em;
            color: var(--text-secondary);
        }
        
        .start-btn {
            padding: 15px 50px;
            font-size: 1.2em;
            font-weight: 600;
            background: linear-gradient(135deg, var(--accent-green), var(--accent-blue));
            color: #000;
            border: none;
            border-radius: 30px;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .start-btn:hover {
            transform: scale(1.05);
            box-shadow: 0 10px 30px rgba(0, 214, 143, 0.3);
        }
        
        /* Finish Overlay */
        .finish-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.95);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        
        .finish-overlay.show {
            display: flex;
        }
        
        .finish-card {
            background: var(--bg-card);
            padding: 40px 50px;
            border-radius: 20px;
            text-align: center;
            max-width: 600px;
        }
        
        .finish-card h1 {
            font-size: 2.5em;
            margin-bottom: 20px;
            color: var(--accent-green);
        }
        
        .finish-stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 20px;
            margin: 30px 0;
        }
        
        .finish-btn {
            padding: 15px 40px;
            font-size: 1.1em;
            font-weight: 600;
            background: var(--accent-blue);
            color: #fff;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            margin: 5px;
        }
        
        .finish-btn:hover {
            filter: brightness(1.1);
        }
        
        /* Solution Toggle */
        .solution-toggle {
            margin-top: 20px;
        }
        
        .solution-btn {
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            color: var(--text-primary);
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
        }
        
        .solution-btn:hover {
            border-color: var(--accent-blue);
        }
        
        .solution-content {
            display: none;
            margin-top: 15px;
            padding: 20px;
            background: rgba(0, 214, 143, 0.1);
            border: 2px solid var(--accent-green);
            border-radius: 10px;
        }
        
        .solution-content.show {
            display: block;
        }
        
        .solution-content h4 {
            color: var(--accent-green);
            margin-bottom: 10px;
        }
        
        /* Interactive MCQ Styles */
        .mcq-table {
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
        }
        
        .mcq-table th {
            background: var(--bg-secondary);
            padding: 12px;
            text-align: center;
            border: 1px solid var(--border-color);
        }
        
        .mcq-table td {
            padding: 12px;
            border: 1px solid var(--border-color);
            vertical-align: middle;
        }
        
        .mcq-table tr:hover {
            background: rgba(77, 166, 255, 0.1);
        }
        
        .mcq-checkbox {
            width: 24px;
            height: 24px;
            cursor: pointer;
            accent-color: var(--accent-blue);
        }
        
        .mcq-cell {
            text-align: center;
            width: 60px;
        }
        
        .mcq-statement {
            text-align: left;
        }
        
        /* Paper Question Indicator */
        .paper-question-banner {
            background: linear-gradient(135deg, #fbbf24 0%, #f59e0b 100%);
            color: #000;
            padding: 15px 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 12px;
            font-weight: 600;
        }
        
        .paper-question-banner .icon {
            font-size: 1.5em;
        }
        
        .paper-done-checkbox {
            margin-top: 20px;
            padding: 15px;
            background: var(--bg-secondary);
            border-radius: 10px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .paper-done-checkbox input {
            width: 24px;
            height: 24px;
            cursor: pointer;
        }
        
        .paper-done-checkbox label {
            cursor: pointer;
            font-weight: 500;
        }
        
        /* Question Type Badge */
        .question-type-badge {
            padding: 5px 12px;
            border-radius: 15px;
            font-size: 0.8em;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .question-type-badge.mcq {
            background: var(--accent-blue);
            color: #fff;
        }
        
        .question-type-badge.paper {
            background: var(--accent-yellow);
            color: #000;
        }
        
        .question-type-badge.text {
            background: var(--accent-green);
            color: #000;
        }
        
        /* Mobile Responsive */
        @media (max-width: 768px) {
            .timer-bar {
                flex-direction: column;
                gap: 15px;
            }
            
            .question-nav {
                display: none;
            }
            
            .question-area {
                padding: 20px;
            }
            
            .question-header {
                flex-direction: column;
                gap: 15px;
            }
            
            .start-stats {
                grid-template-columns: 1fr;
            }
            
            .nav-footer {
                flex-direction: column;
                gap: 10px;
            }
            
            .action-btn {
                width: 100%;
                justify-content: center;
            }
        }
    </style>
</head>
<body>
    <!-- Start Screen Overlay -->
    <div class="start-overlay" id="startOverlay">
        <div class="start-card">
            <h1>📝 Exam Simulation</h1>
            <div class="exam-name">{{ filename }}</div>
            
            <div class="start-stats">
                <div class="start-stat">
                    <div class="start-stat-value">{{ total_questions }}</div>
                    <div class="start-stat-label">Questions</div>
                </div>
                <div class="start-stat">
                    <div class="start-stat-value">{{ total_points }}</div>
                    <div class="start-stat-label">Points</div>
                </div>
                <div class="start-stat">
                    <div class="start-stat-value" id="avgTimePerQuestion">--</div>
                    <div class="start-stat-label">Min/Question</div>
                </div>
            </div>
            
            <div class="time-input-group">
                <label>⏱️ Exam Duration</label>
                <div class="time-input">
                    <input type="number" id="hoursInput" value="1" min="0" max="5">
                    <span>hours</span>
                    <input type="number" id="minutesInput" value="30" min="0" max="59">
                    <span>minutes</span>
                </div>
            </div>
            
            <button class="start-btn" onclick="startExam()">
                🚀 Start Exam
            </button>
        </div>
    </div>
    
    <!-- Finish Overlay -->
    <div class="finish-overlay" id="finishOverlay">
        <div class="finish-card">
            <h1>🎉 Exam Complete!</h1>
            <div class="finish-stats">
                <div class="start-stat">
                    <div class="start-stat-value" id="finalTimeUsed">--</div>
                    <div class="start-stat-label">Time Used</div>
                </div>
                <div class="start-stat">
                    <div class="start-stat-value" id="finalAnswered">--</div>
                    <div class="start-stat-label">Answered</div>
                </div>
                <div class="start-stat">
                    <div class="start-stat-value" id="finalFlagged">--</div>
                    <div class="start-stat-label">Flagged</div>
                </div>
                <div class="start-stat">
                    <div class="start-stat-value" id="finalPoints">--</div>
                    <div class="start-stat-label">Total Points</div>
                </div>
            </div>
            <button class="finish-btn" onclick="reviewAnswers()">📋 Review Answers</button>
            <button class="finish-btn" onclick="location.href='/view/{{ filepath }}'">📄 View Original</button>
            <button class="finish-btn" onclick="restartExam()">🔄 Restart</button>
        </div>
    </div>
    
    <!-- Timer Bar -->
    <div class="timer-bar">
        <div class="timer-main">
            <div>
                <div class="timer-label">Time Remaining</div>
                <div class="timer-display" id="timerDisplay">01:30:00</div>
            </div>
            <div class="timer-controls">
                <button class="timer-btn pause" id="pauseBtn" onclick="togglePause()">⏸️ Pause</button>
            </div>
        </div>
        
        <div class="progress-info">
            <div class="progress-text">
                Question <span id="currentQuestion">1</span> of {{ total_questions }}
            </div>
            <div class="progress-bar-container">
                <div class="progress-bar-fill" id="progressBar" style="width: 0%"></div>
            </div>
        </div>
    </div>
    
    <!-- Main Content -->
    <div class="exam-container">
        <!-- Question Navigator -->
        <div class="question-nav">
            <div class="nav-section">
                <div class="nav-section-title">Questions</div>
                <div class="nav-questions" id="questionNav">
                    <!-- Generated by JS -->
                </div>
            </div>
            
            <div class="nav-section" style="margin-top: 30px;">
                <div class="nav-section-title">Legend</div>
                <div style="font-size: 0.85em; color: var(--text-secondary);">
                    <div style="margin: 5px 0;">🔵 Current</div>
                    <div style="margin: 5px 0;">🟢 Answered</div>
                    <div style="margin: 5px 0;">🟡 Flagged</div>
                    <div style="margin: 5px 0;">⚪ Not Visited</div>
                </div>
            </div>
        </div>
        
        <!-- Question Area -->
        <div class="question-area" id="questionArea">
            <!-- Generated by JS -->
        </div>
    </div>
    
    <script>
        // Exam Data
        const examData = {
            filename: "{{ filename }}",
            filepath: "{{ filepath }}",
            totalPoints: {{ total_points }},
            totalQuestions: {{ total_questions }},
            sections: {{ sections | tojson | safe }},
            questions: {{ all_questions | tojson | safe }}
        };
        
        // State
        let state = {
            started: false,
            paused: false,
            finished: false,
            totalSeconds: 90 * 60, // Default 1.5 hours
            remainingSeconds: 90 * 60,
            currentIndex: 0,
            answers: {},
            mcqAnswers: {},  // Store MCQ selections separately
            flagged: new Set(),
            startTime: null,
            timerInterval: null
        };
        
        // Initialize
        document.addEventListener('DOMContentLoaded', () => {
            updateAvgTime();
            buildQuestionNav();
            
            // Listen for time input changes
            document.getElementById('hoursInput').addEventListener('input', updateAvgTime);
            document.getElementById('minutesInput').addEventListener('input', updateAvgTime);
        });
        
        function updateAvgTime() {
            const hours = parseInt(document.getElementById('hoursInput').value) || 0;
            const minutes = parseInt(document.getElementById('minutesInput').value) || 0;
            const totalMinutes = hours * 60 + minutes;
            const avgTime = (totalMinutes / examData.totalQuestions).toFixed(1);
            document.getElementById('avgTimePerQuestion').textContent = avgTime;
        }
        
        function buildQuestionNav() {
            const nav = document.getElementById('questionNav');
            nav.innerHTML = '';
            
            examData.questions.forEach((q, i) => {
                const btn = document.createElement('button');
                btn.className = 'nav-question-btn';
                btn.innerHTML = `${q.number}<span class="points">${q.points}p</span>`;
                btn.onclick = () => goToQuestion(i);
                btn.id = `navBtn${i}`;
                nav.appendChild(btn);
            });
        }
        
        function startExam() {
            const hours = parseInt(document.getElementById('hoursInput').value) || 0;
            const minutes = parseInt(document.getElementById('minutesInput').value) || 0;
            state.totalSeconds = (hours * 60 + minutes) * 60;
            state.remainingSeconds = state.totalSeconds;
            state.started = true;
            state.startTime = Date.now();
            
            document.getElementById('startOverlay').style.display = 'none';
            
            updateTimerDisplay();
            state.timerInterval = setInterval(tick, 1000);
            
            goToQuestion(0);
        }
        
        function tick() {
            if (state.paused || state.finished) return;
            
            state.remainingSeconds--;
            updateTimerDisplay();
            
            if (state.remainingSeconds <= 0) {
                finishExam();
            }
        }
        
        function updateTimerDisplay() {
            const hours = Math.floor(state.remainingSeconds / 3600);
            const mins = Math.floor((state.remainingSeconds % 3600) / 60);
            const secs = state.remainingSeconds % 60;
            
            const display = document.getElementById('timerDisplay');
            display.textContent = `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
            
            // Warning states
            const percentRemaining = state.remainingSeconds / state.totalSeconds;
            display.classList.remove('warning', 'danger');
            
            if (percentRemaining < 0.1) {
                display.classList.add('danger');
            } else if (percentRemaining < 0.25) {
                display.classList.add('warning');
            }
        }
        
        function togglePause() {
            state.paused = !state.paused;
            const btn = document.getElementById('pauseBtn');
            btn.textContent = state.paused ? '▶️ Resume' : '⏸️ Pause';
            btn.classList.toggle('start', state.paused);
            btn.classList.toggle('pause', !state.paused);
        }
        
        function detectQuestionType(content) {
            // Check for type hints in comments
            if (content.includes('<!-- type: mcq-truefalse -->') || content.includes('<!-- type: mcq -->')) {
                return 'mcq';
            }
            if (content.includes('<!-- type: paper -->')) {
                return 'paper';
            }
            if (content.includes('<!-- type: text -->')) {
                return 'text';
            }
            
            // Auto-detect MCQ tables (True/False columns)
            if (content.includes('| True | False |') || content.includes('| # | True | False |')) {
                return 'mcq';
            }
            
            // Auto-detect drawing/calculation questions
            if (content.toLowerCase().includes('draw the graph') || 
                content.toLowerCase().includes('calculate') ||
                content.toLowerCase().includes('provide all') ||
                content.includes('adjacency matrix')) {
                return 'paper';
            }
            
            return 'text';
        }
        
        function parseAndRenderMCQ(content, questionIndex) {
            // Remove type hints
            content = content.replace(/<!-- type: \\w+ -->/g, '');
            
            // Find the table in content
            const tableMatch = content.match(/\\|[^\\n]+\\|[\\s\\S]*?\\n(?:\\|[^\\n]+\\|\\n)+/);
            if (!tableMatch) {
                return { before: content, mcqHtml: '', after: '' };
            }
            
            const tableStart = content.indexOf(tableMatch[0]);
            const tableEnd = tableStart + tableMatch[0].length;
            const before = content.substring(0, tableStart);
            const after = content.substring(tableEnd);
            
            // Parse table rows
            const rows = tableMatch[0].trim().split('\\n').filter(r => r.includes('|'));
            if (rows.length < 3) {
                return { before: content, mcqHtml: '', after: '' };
            }
            
            // Skip header and separator rows
            const dataRows = rows.slice(2);
            
            // Get saved answers for this question
            const savedMcq = state.mcqAnswers[questionIndex] || {};
            
            let mcqHtml = `<table class="mcq-table">
                <thead>
                    <tr>
                        <th style="width: 40px">#</th>
                        <th style="width: 60px">True</th>
                        <th style="width: 60px">False</th>
                        <th>Statement</th>
                    </tr>
                </thead>
                <tbody>`;
            
            dataRows.forEach((row, rowIdx) => {
                const cells = row.split('|').map(c => c.trim()).filter(c => c);
                if (cells.length >= 4) {
                    const num = cells[0];
                    const statement = cells[3];
                    const savedValue = savedMcq[num] || '';
                    
                    mcqHtml += `
                        <tr>
                            <td class="mcq-cell">${num}</td>
                            <td class="mcq-cell">
                                <input type="checkbox" class="mcq-checkbox" 
                                    data-num="${num}" data-value="true"
                                    ${savedValue === 'true' ? 'checked' : ''}
                                    onchange="handleMcqChange(this, ${questionIndex}, '${num}', 'true')">
                            </td>
                            <td class="mcq-cell">
                                <input type="checkbox" class="mcq-checkbox"
                                    data-num="${num}" data-value="false"
                                    ${savedValue === 'false' ? 'checked' : ''}
                                    onchange="handleMcqChange(this, ${questionIndex}, '${num}', 'false')">
                            </td>
                            <td class="mcq-statement">${statement}</td>
                        </tr>`;
                }
            });
            
            mcqHtml += '</tbody></table>';
            
            return { before, mcqHtml, after };
        }
        
        function handleMcqChange(checkbox, questionIndex, num, value) {
            // Initialize if needed
            if (!state.mcqAnswers[questionIndex]) {
                state.mcqAnswers[questionIndex] = {};
            }
            
            // Uncheck the other checkbox in the same row
            const row = checkbox.closest('tr');
            const checkboxes = row.querySelectorAll('.mcq-checkbox');
            checkboxes.forEach(cb => {
                if (cb !== checkbox) {
                    cb.checked = false;
                }
            });
            
            // Save the answer
            if (checkbox.checked) {
                state.mcqAnswers[questionIndex][num] = value;
            } else {
                delete state.mcqAnswers[questionIndex][num];
            }
            
            // Mark as answered if any MCQ is filled
            if (Object.keys(state.mcqAnswers[questionIndex]).length > 0) {
                state.answers[questionIndex] = 'MCQ_ANSWERED';
            } else {
                delete state.answers[questionIndex];
            }
            
            updateNavState();
            updateProgress();
        }
        
        function handlePaperDone(checkbox, questionIndex) {
            if (checkbox.checked) {
                state.answers[questionIndex] = 'PAPER_DONE';
            } else {
                delete state.answers[questionIndex];
            }
            updateNavState();
            updateProgress();
        }
        
        function goToQuestion(index) {
            // Save current answer
            saveCurrentAnswer();
            
            state.currentIndex = index;
            const question = examData.questions[index];
            
            // Detect question type
            const questionType = detectQuestionType(question.content);
            
            // Calculate suggested time for this question
            const minutesPerPoint = state.totalSeconds / 60 / examData.totalPoints;
            const suggestedMinutes = Math.ceil(question.points * minutesPerPoint);
            
            // Build question HTML
            const area = document.getElementById('questionArea');
            
            // Type badge
            const typeBadges = {
                'mcq': '<span class="question-type-badge mcq">MCQ</span>',
                'paper': '<span class="question-type-badge paper">✏️ Paper</span>',
                'text': '<span class="question-type-badge text">Written</span>'
            };
            
            let answerAreaHtml = '';
            let contentHtml = '';
            
            if (questionType === 'mcq') {
                // Parse and render interactive MCQ
                const { before, mcqHtml, after } = parseAndRenderMCQ(question.content, index);
                contentHtml = marked.parse(before) + mcqHtml + marked.parse(after);
                answerAreaHtml = `
                    <div class="answer-area" style="background: var(--bg-secondary); padding: 15px; border-radius: 10px; margin-top: 20px;">
                        <p style="color: var(--text-secondary); margin: 0;">
                            ℹ️ Click the checkboxes above to mark your answers. Your selections are auto-saved.
                        </p>
                    </div>`;
            } else if (questionType === 'paper') {
                // Paper question - show banner and completion checkbox
                contentHtml = marked.parse(question.content.replace(/<!-- type: \\w+ -->/g, ''));
                const isDone = state.answers[index] === 'PAPER_DONE';
                answerAreaHtml = `
                    <div class="paper-question-banner">
                        <span class="icon">📝</span>
                        <div>
                            <strong>Paper Question</strong> - Write or draw your answer on paper
                        </div>
                    </div>
                    <div class="paper-done-checkbox">
                        <input type="checkbox" id="paperDone" ${isDone ? 'checked' : ''} 
                            onchange="handlePaperDone(this, ${index})">
                        <label for="paperDone">✅ I've completed this question on paper</label>
                    </div>`;
            } else {
                // Text question - show textarea
                contentHtml = marked.parse(question.content.replace(/<!-- type: \\w+ -->/g, ''));
                answerAreaHtml = `
                    <div class="answer-area">
                        <div class="answer-label">
                            ✏️ Your Answer
                            <span style="font-weight: normal; color: var(--text-secondary);">
                                (Auto-saved)
                            </span>
                        </div>
                        <textarea 
                            class="answer-textarea" 
                            id="answerInput"
                            placeholder="Type your answer here..."
                            oninput="autoSave()"
                        >${state.answers[index] || ''}</textarea>
                    </div>`;
            }
            
            // Fix image paths - convert relative paths to /raw/ URLs
            contentHtml = contentHtml.replace(/src="(?!http|\/raw)([^"]+)"/g, (match, path) => {
                const dir = examData.filepath.substring(0, examData.filepath.lastIndexOf('/'));
                return `src="/raw/${dir}/${path}"`;
            });
            
            area.innerHTML = `
                <div class="question-header">
                    <div>
                        <div class="question-title">
                            ${question.sectionNum}. ${question.sectionTitle}
                            ${typeBadges[questionType]}
                        </div>
                        <div style="color: var(--text-secondary); margin-top: 5px;">
                            Question ${question.number}: ${question.title}
                        </div>
                    </div>
                    <div class="question-meta">
                        <div class="question-points">${question.points} Points</div>
                        <div class="question-time-suggest">
                            <span class="time-icon">⏱️</span>
                            ~${suggestedMinutes} min suggested
                        </div>
                    </div>
                </div>
                
                <div class="question-content" id="questionContent">
                    ${contentHtml}
                </div>
                
                ${answerAreaHtml}
                
                <div class="nav-footer">
                    <div class="answer-actions">
                        <button class="action-btn flag ${state.flagged.has(index) ? 'flagged' : ''}" onclick="toggleFlag()">
                            🚩 ${state.flagged.has(index) ? 'Unflag' : 'Flag for Review'}
                        </button>
                    </div>
                    <div class="answer-actions">
                        <button class="action-btn prev" onclick="prevQuestion()" ${index === 0 ? 'disabled' : ''}>
                            ← Previous
                        </button>
                        <button class="action-btn next" onclick="nextQuestion()" ${index === examData.totalQuestions - 1 ? 'disabled' : ''}>
                            Next →
                        </button>
                        ${index === examData.totalQuestions - 1 ? `
                            <button class="action-btn submit" onclick="finishExam()">
                                ✅ Finish Exam
                            </button>
                        ` : ''}
                    </div>
                </div>
            `;
            
            // Re-render math
            if (window.MathJax) {
                MathJax.typesetPromise([document.getElementById('questionContent')]);
            }
            
            // Update navigation
            updateNavState();
            updateProgress();
        }
        
        function saveCurrentAnswer() {
            const input = document.getElementById('answerInput');
            if (input) {
                const answer = input.value.trim();
                if (answer) {
                    state.answers[state.currentIndex] = answer;
                } else {
                    delete state.answers[state.currentIndex];
                }
            }
        }
        
        function autoSave() {
            saveCurrentAnswer();
            updateNavState();
        }
        
        function updateNavState() {
            document.querySelectorAll('.nav-question-btn').forEach((btn, i) => {
                btn.classList.remove('active', 'answered', 'flagged');
                
                if (i === state.currentIndex) {
                    btn.classList.add('active');
                }
                if (state.answers[i]) {
                    btn.classList.add('answered');
                }
                if (state.flagged.has(i)) {
                    btn.classList.add('flagged');
                }
            });
            
            document.getElementById('currentQuestion').textContent = state.currentIndex + 1;
        }
        
        function updateProgress() {
            const answered = Object.keys(state.answers).length;
            const percent = (answered / examData.totalQuestions) * 100;
            document.getElementById('progressBar').style.width = `${percent}%`;
        }
        
        function toggleFlag() {
            if (state.flagged.has(state.currentIndex)) {
                state.flagged.delete(state.currentIndex);
            } else {
                state.flagged.add(state.currentIndex);
            }
            goToQuestion(state.currentIndex); // Refresh
        }
        
        function prevQuestion() {
            if (state.currentIndex > 0) {
                goToQuestion(state.currentIndex - 1);
            }
        }
        
        function nextQuestion() {
            if (state.currentIndex < examData.totalQuestions - 1) {
                goToQuestion(state.currentIndex + 1);
            }
        }
        
        function finishExam() {
            saveCurrentAnswer();
            state.finished = true;
            
            if (state.timerInterval) {
                clearInterval(state.timerInterval);
            }
            
            // Calculate stats
            const timeUsed = state.totalSeconds - state.remainingSeconds;
            const hours = Math.floor(timeUsed / 3600);
            const mins = Math.floor((timeUsed % 3600) / 60);
            
            document.getElementById('finalTimeUsed').textContent = 
                hours > 0 ? `${hours}h ${mins}m` : `${mins} min`;
            document.getElementById('finalAnswered').textContent = 
                `${Object.keys(state.answers).length}/${examData.totalQuestions}`;
            document.getElementById('finalFlagged').textContent = state.flagged.size;
            document.getElementById('finalPoints').textContent = examData.totalPoints;
            
            document.getElementById('finishOverlay').classList.add('show');
        }
        
        function reviewAnswers() {
            document.getElementById('finishOverlay').classList.remove('show');
            state.finished = false; // Allow navigation
            goToQuestion(0);
        }
        
        function restartExam() {
            state = {
                started: false,
                paused: false,
                finished: false,
                totalSeconds: 90 * 60,
                remainingSeconds: 90 * 60,
                currentIndex: 0,
                answers: {},
                mcqAnswers: {},
                flagged: new Set(),
                startTime: null,
                timerInterval: null
            };
            
            document.getElementById('finishOverlay').classList.remove('show');
            document.getElementById('startOverlay').style.display = 'flex';
            updateAvgTime();
            buildQuestionNav();
        }
        
        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (!state.started || state.finished) return;
            if (e.target.tagName === 'TEXTAREA') return;
            
            if (e.key === 'ArrowLeft' || e.key === 'p') {
                prevQuestion();
            } else if (e.key === 'ArrowRight' || e.key === 'n') {
                nextQuestion();
            } else if (e.key === 'f') {
                toggleFlag();
            } else if (e.key === ' ') {
                e.preventDefault();
                togglePause();
            }
        });
    </script>
</body>
</html>
'''


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
  python app.py ~/notes --ssl              # HTTPS for offline PWA on network
        '''
    )
    parser.add_argument('vault_path', nargs='?', help='Path to your Obsidian vault or any markdown folder')
    parser.add_argument('--port', '-p', type=int, default=5000, help='Port to run on (default: 5000)')
    parser.add_argument('--host', '-H', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0 for network access)')
    parser.add_argument('--ssl', '-s', action='store_true', help='Enable HTTPS (required for offline PWA on network)')
    
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
    
    # Load access log
    load_access_log()
    
    ip = get_local_ip()
    protocol = "https" if args.ssl else "http"
    
    # SSL certificate paths
    ssl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ssl')
    ssl_cert = os.path.join(ssl_dir, 'cert.pem')
    ssl_key = os.path.join(ssl_dir, 'key.pem')
    
    if args.ssl and (not os.path.exists(ssl_cert) or not os.path.exists(ssl_key)):
        print("❌ SSL certificates not found. Generating...")
        os.makedirs(ssl_dir, exist_ok=True)
        import subprocess
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:4096',
            '-keyout', ssl_key, '-out', ssl_cert,
            '-days', '365', '-nodes',
            '-subj', '/CN=obsidian-viewer',
            '-addext', f'subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost'
        ], check=True)
        print("✅ SSL certificates generated")
    
    ssl_note = ""
    if args.ssl:
        ssl_note = """
║  ⚠️  First visit: Accept the security warning         ║
║      (Self-signed cert - safe on local network)      ║"""
    
    print(f"""
╔══════════════════════════════════════════════════════╗
║           📚 Obsidian Local Viewer                   ║
╠══════════════════════════════════════════════════════╣
║  Vault: {get_vault_name():<43} ║
║  Local:   {protocol}://localhost:{args.port:<22} ║
║  Network: {protocol}://{ip}:{args.port:<25} ║{ssl_note}
╠══════════════════════════════════════════════════════╣
║  Keyboard Shortcuts:                                 ║
║    Ctrl+B  — Toggle sidebar                          ║
║    Ctrl+→  — Toggle graph panel                      ║
║    F11     — Fullscreen                              ║
║    Esc     — Show sidebar                            ║
╚══════════════════════════════════════════════════════╝
    
Open the Network URL on your tablet/phone to view your vault!
{"📴 Offline PWA: Use --ssl for network devices" if not args.ssl else "✅ Offline PWA enabled! Click 'Download for Offline' in sidebar"}
Press Ctrl+C to stop the server.
""")
    
    if args.ssl:
        ssl_context = (ssl_cert, ssl_key)
        app.run(host=args.host, port=args.port, debug=False, threaded=True, ssl_context=ssl_context)
    else:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__ == '__main__':
    main()
