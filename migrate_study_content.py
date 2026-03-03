#!/usr/bin/env python3
"""
Migrate study content (Flashcards, MCQ, Cloze) from MD files to separate JSON files.

Usage:
    python migrate_study_content.py <vault_path> [--dry-run] [--remove-from-md]
    
Options:
    --dry-run        Show what would be done without making changes
    --remove-from-md Remove study sections from MD files after extraction
"""

import os
import re
import json
import argparse
import hashlib
from pathlib import Path
from datetime import datetime


def parse_flashcards(content):
    """Parse flashcards from markdown content."""
    flashcards = []
    
    # Pattern 1: ## Flashcards section
    flashcard_section = re.search(r'##\s*Flashcards?\s*\n([\s\S]*?)(?=\n##\s|\Z)', content, re.IGNORECASE)
    if flashcard_section:
        section_content = flashcard_section.group(1)
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
        body = '\n'.join(line.lstrip('>').strip() for line in body_lines.split('\n') if line.strip())
        if title and body:
            flashcards.append({'question': title, 'answer': body})
    
    return flashcards


def parse_mcq(content):
    """Parse MCQs from markdown content."""
    mcqs = []
    
    mcq_section = re.search(r'##\s*MCQ\s*\n([\s\S]*?)(?=\n##\s|\Z)', content, re.IGNORECASE)
    if not mcq_section:
        return mcqs
    
    section_content = mcq_section.group(1)
    questions = re.split(r'\nQ:\s*', section_content)
    
    for q_block in questions:
        if not q_block.strip():
            continue
        
        lines = q_block.strip().split('\n')
        if not lines:
            continue
        
        question = lines[0].strip()
        if question.startswith('Q:'):
            question = question[2:].strip()
        
        options = []
        correct_index = -1
        
        for line in lines[1:]:
            line = line.strip()
            if re.match(r'^-\s*\[x\]\s*', line, re.IGNORECASE):
                option_text = re.sub(r'^-\s*\[x\]\s*', '', line, flags=re.IGNORECASE).strip()
                if option_text:
                    correct_index = len(options)
                    options.append(option_text)
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


def parse_cloze(content):
    """Parse cloze deletions from markdown content."""
    cloze_cards = []
    
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
    
    lines = [l.strip() for l in section_content.split('\n') if l.strip() and not l.strip().startswith('#')]
    
    for line in lines:
        if not (re.search(r'\{\{.*?\}\}', line) or re.search(r'==.+?==', line)):
            continue
        
        blanks = []
        
        # Pattern 1: {{c1::text}} or {{c1::text|hint}}
        numbered_pattern = re.compile(r'\{\{c(\d+)::([^}|]+)(?:\|([^}]+))?\}\}')
        for match in numbered_pattern.finditer(line):
            blanks.append({
                'text': match.group(2).strip(),
                'hint': match.group(3).strip() if match.group(3) else None,
                'group': int(match.group(1))
            })
        
        # Pattern 2: {{text}} or {{text|hint}}
        unnumbered_pattern = re.compile(r'\{\{([^}:]+?)(?:\|([^}]+))?\}\}')
        group_idx = 100
        for match in unnumbered_pattern.finditer(line):
            if '::' in match.group(0):
                continue
            blanks.append({
                'text': match.group(1).strip(),
                'hint': match.group(2).strip() if match.group(2) else None,
                'group': group_idx
            })
            group_idx += 1
        
        # Pattern 3: ==text==
        highlight_pattern = re.compile(r'==(.+?)==')
        for match in highlight_pattern.finditer(line):
            blanks.append({
                'text': match.group(1).strip(),
                'hint': None,
                'group': group_idx
            })
            group_idx += 1
        
        if blanks:
            cloze_cards.append({
                'original': line,
                'blanks': blanks
            })
    
    return cloze_cards


def remove_study_sections(content):
    """Remove study sections from markdown content."""
    # Remove ## Flashcards section
    content = re.sub(r'\n---\s*\n##\s*Flashcards?\s*\n[\s\S]*?(?=\n---\s*\n##|\n##\s*MCQ|\n##\s*Cloze|\Z)', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\n##\s*Flashcards?\s*\n[\s\S]*?(?=\n##\s*MCQ|\n##\s*Cloze|\Z)', '', content, flags=re.IGNORECASE)
    
    # Remove ## MCQ section
    content = re.sub(r'\n##\s*MCQ\s*\n[\s\S]*?(?=\n##\s*Cloze|\Z)', '', content, flags=re.IGNORECASE)
    
    # Remove ## Cloze section
    content = re.sub(r'\n##\s*Cloze\s*\n[\s\S]*?(?=\n##\s|\Z)', '', content, flags=re.IGNORECASE)
    
    # Clean up trailing whitespace and multiple newlines
    content = re.sub(r'\n{3,}', '\n\n', content)
    content = content.rstrip() + '\n'
    
    return content


def get_study_json_path(vault_path, md_file_path):
    """Get the path for the .study.json file."""
    rel_path = os.path.relpath(md_file_path, vault_path)
    # Store in .obsidian-viewer/study/ directory
    json_filename = rel_path.replace('/', '_').replace('\\', '_') + '.study.json'
    study_dir = os.path.join(vault_path, '.obsidian-viewer', 'study')
    return os.path.join(study_dir, json_filename)


def migrate_file(vault_path, md_file_path, dry_run=False, remove_from_md=False):
    """Migrate study content from a single MD file."""
    rel_path = os.path.relpath(md_file_path, vault_path)
    
    with open(md_file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Parse study content
    flashcards = parse_flashcards(content)
    mcqs = parse_mcq(content)
    cloze = parse_cloze(content)
    
    # Skip if no study content
    if not flashcards and not mcqs and not cloze:
        return None
    
    # Create study data
    study_data = {
        'source': rel_path,
        'migrated_at': datetime.now().isoformat(),
        'content_hash': hashlib.md5(content.encode()).hexdigest()[:8],
        'flashcards': flashcards,
        'mcq': mcqs,
        'cloze': cloze
    }
    
    json_path = get_study_json_path(vault_path, md_file_path)
    
    result = {
        'md_file': rel_path,
        'json_file': os.path.relpath(json_path, vault_path),
        'flashcards': len(flashcards),
        'mcq': len(mcqs),
        'cloze': len(cloze)
    }
    
    if dry_run:
        print(f"[DRY-RUN] Would migrate: {rel_path}")
        print(f"          Flashcards: {len(flashcards)}, MCQ: {len(mcqs)}, Cloze: {len(cloze)}")
        return result
    
    # Create directory if needed
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    
    # Write JSON file
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(study_data, f, indent=2, ensure_ascii=False)
    
    print(f"✅ Migrated: {rel_path}")
    print(f"   → {os.path.basename(json_path)}")
    print(f"   Flashcards: {len(flashcards)}, MCQ: {len(mcqs)}, Cloze: {len(cloze)}")
    
    # Optionally remove from MD file
    if remove_from_md:
        new_content = remove_study_sections(content)
        if new_content != content:
            with open(md_file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"   ✂️ Removed study sections from MD")
    
    return result


def migrate_vault(vault_path, dry_run=False, remove_from_md=False):
    """Migrate all MD files in a vault directory."""
    vault_path = os.path.abspath(vault_path)
    
    if not os.path.isdir(vault_path):
        print(f"Error: {vault_path} is not a directory")
        return
    
    print(f"🔍 Scanning: {vault_path}")
    print(f"   Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
    print(f"   Remove from MD: {'Yes' if remove_from_md else 'No'}")
    print("-" * 60)
    
    results = []
    
    for root, dirs, files in os.walk(vault_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for filename in files:
            if filename.endswith('.md'):
                md_path = os.path.join(root, filename)
                result = migrate_file(vault_path, md_path, dry_run, remove_from_md)
                if result:
                    results.append(result)
    
    print("-" * 60)
    print(f"📊 Summary:")
    print(f"   Files with study content: {len(results)}")
    print(f"   Total flashcards: {sum(r['flashcards'] for r in results)}")
    print(f"   Total MCQ: {sum(r['mcq'] for r in results)}")
    print(f"   Total cloze: {sum(r['cloze'] for r in results)}")
    
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Migrate study content from MD to JSON')
    parser.add_argument('vault_path', help='Path to the vault directory')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done')
    parser.add_argument('--remove-from-md', action='store_true', help='Remove study sections from MD files')
    
    args = parser.parse_args()
    migrate_vault(args.vault_path, args.dry_run, args.remove_from_md)
