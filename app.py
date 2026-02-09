#!/usr/bin/env python3
"""
Obsidian Local Viewer - View your Obsidian vault from any device on your network
"""

from flask import Flask, render_template_string, send_file, abort, redirect, url_for, Response, request, jsonify
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
    """Convert Obsidian [[wiki-links]] to HTML links"""
    
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
        
        # Find the file
        file_path = find_file_in_vault(link_part)
        
        if file_path:
            return f'<a href="/view/{file_path}{heading}" class="internal-link">{display_text}</a>'
        else:
            # Return as broken link (styled differently)
            return f'<span class="broken-link" title="File not found: {link_part}">{display_text}</span>'
    
    # Match [[...]] patterns (but not inside code blocks)
    # This regex handles [[link]] and [[link|text]]
    pattern = r'\[\[([^\]]+)\]\]'
    
    return re.sub(pattern, replace_link, html_content)

def protect_math_expressions(content):
    """Protect LaTeX math expressions from markdown processing"""
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
    # Match $ followed by non-space, content, non-space, $
    content = re.sub(r'(?<!\$)\$(?!\$)(?!\s)(.+?)(?<!\s)(?<!\$)\$(?!\$)', replace_math, content)
    
    return content, placeholders

def restore_math_expressions(content, placeholders):
    """Restore protected math expressions after markdown processing"""
    for placeholder, original in placeholders.items():
        content = content.replace(placeholder, original)
    return content

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <title>{{ title }} - Obsidian Viewer</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>">
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
            gap: 10px;
            font-family: 'SF Mono', Monaco, 'Courier New', monospace;
            font-size: 13px;
            color: #666;
        }
        .file-path-bar .path-icon {
            color: #888;
        }
        .file-path-bar .path-text {
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
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
        .fullscreen-mode .toggle-btn { left: 15px; }
        .fullscreen-mode .content { max-width: 100%; }
        
        /* Mobile responsive */
        @media (max-width: 768px) {
            body { flex-direction: column; }
            
            /* Hide dock toggle on mobile */
            .dock-toggle { display: none !important; }
            
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
                skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
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
</head>
<body>
    <button class="toggle-btn" onclick="toggleSidebar()" title="Toggle Sidebar">☰ <span class="btn-text">Menu</span></button>
    
    <button class="dock-toggle" id="dockToggle" onclick="toggleDock()" title="Toggle Sidebar (Ctrl+B)">
        <span class="arrow">◀</span>
    </button>
    
    <div class="toolbar">
        {% if is_markdown %}
        <button onclick="downloadPDF()" title="Download as PDF">📥 <span class="btn-text">PDF</span></button>
        {% endif %}
        {% if is_markdown or is_pdf|default(false) %}
        <button onclick="openAnnotation()" title="Annotate with Apple Pencil (draw on content)">✏️ <span class="btn-text">Annotate</span></button>
        {% endif %}
        <button class="secondary" onclick="toggleFullscreen()" title="Toggle Fullscreen">⛶</button>
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
        </div>
        <div class="sidebar-content">
            {{ tree|safe }}
        </div>
    </div>
    <div class="content-wrapper" id="contentWrapper">
        {% if file_path %}
        <div class="file-path-bar" id="filePathBar">
            <span class="path-icon">📄</span>
            <span class="path-text" id="pathText">{{ file_path }}</span>
            <button class="copy-btn" onclick="copyFilePath()" title="Copy path">
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
        
        function toggleFullscreen() {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen();
            } else {
                document.exitFullscreen();
            }
        }
        
        function toggleFolder(header) {
            const folderItem = header.parentElement;
            const icon = header.querySelector('.folder-icon');
            
            folderItem.classList.toggle('open');
            icon.classList.toggle('collapsed');
        }
        
        function collapseAllFolders() {
            document.querySelectorAll('.folder-item').forEach(item => {
                item.classList.remove('open');
                item.querySelector('.folder-icon').classList.add('collapsed');
            });
        }
        
        function expandAllFolders() {
            document.querySelectorAll('.folder-item').forEach(item => {
                item.classList.add('open');
                item.querySelector('.folder-icon').classList.remove('collapsed');
            });
        }
        
        function downloadPDF() {
            const currentPath = window.location.pathname;
            if (currentPath.startsWith('/view/')) {
                const filepath = currentPath.replace('/view/', '');
                window.location.href = '/pdf/' + filepath;
            }
        }
        
        function copyFilePath() {
            const pathText = document.getElementById('pathText');
            if (!pathText) return;
            
            const path = pathText.textContent;
            
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
        
        // Initialize Mermaid diagrams
        document.addEventListener('DOMContentLoaded', function() {
            // Find all code blocks that might be mermaid
            const codeBlocks = document.querySelectorAll('pre code');
            
            codeBlocks.forEach(function(codeBlock, index) {
                const pre = codeBlock.parentElement;
                const content = codeBlock.textContent.trim();
                
                // Check if it's a mermaid block (by class or content pattern)
                const isMermaidClass = codeBlock.className.includes('mermaid') || 
                                       codeBlock.className.includes('language-mermaid');
                const isMermaidContent = content.match(/^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|xychart|sankey|packet|block)/i);
                
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
            
            // Run mermaid on all .mermaid elements
            if (document.querySelectorAll('.mermaid').length > 0) {
                mermaid.run({
                    nodes: document.querySelectorAll('.mermaid')
                });
            }
            
            // Re-typeset MathJax after content is loaded
            if (typeof MathJax !== 'undefined' && MathJax.typesetPromise) {
                MathJax.typesetPromise().catch(function(err) {
                    console.log('MathJax typeset failed: ' + err.message);
                });
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
    </script>
</body>
</html>
'''

def get_file_tree(path, base_path, current_file="", depth=0):
    """Recursively build HTML file tree with collapsible folders"""
    items = []
    try:
        entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        
        for entry in entries:
            if entry.startswith('.'):
                continue
                
            full_path = os.path.join(path, entry)
            rel_path = os.path.relpath(full_path, base_path)
            
            if os.path.isdir(full_path):
                subtree = get_file_tree(full_path, base_path, current_file, depth + 1)
                if subtree:  # Only show folders that have content
                    # Check if any child is active (to auto-expand)
                    is_parent_of_active = current_file and current_file.startswith(rel_path + os.sep)
                    open_class = 'open' if is_parent_of_active or depth == 0 else ''
                    collapsed_class = '' if is_parent_of_active or depth == 0 else 'collapsed'
                    items.append(f'''<li class="folder-item {open_class}">
                        <div class="folder-header" onclick="toggleFolder(this)">
                            <span class="folder-icon {collapsed_class}">▼</span>
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
        
        # Add title
        title_html = f'<h1>{filename.replace(".md", "")}</h1>'
        
        return render_template_string(
            HTML_TEMPLATE, 
            title=filename, 
            tree=tree, 
            content=title_html + html_content,
            vault_name=get_vault_name(),
            is_markdown=True,
            file_path=filepath
        )
    
    elif ext == 'pdf':
        # Embed PDF using PDF.js for cross-platform compatibility (especially iOS)
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
        
        <script>
            // Configure PDF.js worker
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
            
            let pdfDoc = null;
            let currentScale = 1.5;
            const minScale = 0.5;
            const maxScale = 3.0;
            const isMobilePdf = window.innerWidth <= 768;
            
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
                        const viewerWidth = viewer.clientWidth - 20; // padding
                        currentScale = viewerWidth / defaultViewport.width;
                        // Clamp to reasonable bounds
                        currentScale = Math.max(minScale, Math.min(currentScale, 1.5));
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
                
                const canvas = document.createElement('canvas');
                canvas.className = 'pdf-page';
                canvas.width = viewport.width;
                canvas.height = viewport.height;
                
                const context = canvas.getContext('2d');
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
                viewer.innerHTML = '';
                
                for (let i = 1; i <= pdfDoc.numPages; i++) {{
                    await renderPage(i, viewer);
                }}
                
                viewer.scrollTop = scrollTop;
                document.getElementById('pdfZoomLevel').textContent = Math.round(currentScale * 100 / 1.5) + '%';
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
                    document.getElementById('pdfZoomLevel').textContent = Math.round(newScale * 100 / 1.5) + '%';
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
                const path = '{filepath}';
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
            file_path=filepath
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
        </style>
        
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
        </script>
        '''
        return render_template_string(
            HTML_TEMPLATE,
            title=filename,
            tree=tree,
            content=video_content,
            vault_name=get_vault_name(),
            is_markdown=False,
            file_path=filepath
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
            file_path=filepath
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
