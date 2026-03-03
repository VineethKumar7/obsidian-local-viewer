#!/usr/bin/env python3
"""
Migrate study content (Cloze, Flashcards, MCQ) from Markdown files to JSON.
Creates .study.json files alongside the .md files.
Removes study sections from markdown to keep files clean/shareable.
"""

import os
import re
import json
import argparse
import hashlib
from pathlib import Path
from datetime import datetime

def parse_cloze(lines):
    """Parse cloze lines with {{answer}} or {{answer|hint: text}} syntax."""
    cloze_items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Find all {{...}} patterns
        pattern = r'\{\{([^}]+)\}\}'
        matches = re.findall(pattern, line)
        if matches:
            # Store the full line as template, with answers
            cloze_items.append({
                'text': line,
                'answers': []
            })
            for match in matches:
                if '|hint:' in match:
                    answer, hint = match.split('|hint:', 1)
                    cloze_items[-1]['answers'].append({
                        'answer': answer.strip(),
                        'hint': hint.strip()
                    })
                else:
                    cloze_items[-1]['answers'].append({
                        'answer': match.strip()
                    })
    return cloze_items

def parse_flashcards(lines):
    """Parse Q: and A: lines into flashcard pairs."""
    flashcards = []
    current_q = None
    current_a_lines = []
    
    for line in lines:
        line = line.rstrip()
        if line.startswith('Q:'):
            # Save previous if exists
            if current_q and current_a_lines:
                flashcards.append({
                    'question': current_q,
                    'answer': '\n'.join(current_a_lines)
                })
            current_q = line[2:].strip()
            current_a_lines = []
        elif line.startswith('A:'):
            current_a_lines.append(line[2:].strip())
        elif current_a_lines and line.strip():
            # Continuation of answer
            current_a_lines.append(line.strip())
    
    # Don't forget last one
    if current_q and current_a_lines:
        flashcards.append({
            'question': current_q,
            'answer': '\n'.join(current_a_lines)
        })
    
    return flashcards

def parse_mcq(lines):
    """Parse MCQ format with - [ ] and - [x] options."""
    mcqs = []
    current_q = None
    current_options = []
    
    for line in lines:
        line = line.rstrip()
        if line.startswith('Q:'):
            # Save previous if exists
            if current_q and current_options:
                mcqs.append({
                    'question': current_q,
                    'options': current_options
                })
            current_q = line[2:].strip()
            current_options = []
        elif line.startswith('- [x]'):
            current_options.append({
                'text': line[5:].strip(),
                'correct': True
            })
        elif line.startswith('- [ ]'):
            current_options.append({
                'text': line[5:].strip(),
                'correct': False
            })
    
    # Don't forget last one
    if current_q and current_options:
        mcqs.append({
            'question': current_q,
            'options': current_options
        })
    
    return mcqs

def extract_study_sections(content):
    """Extract study sections and return (cleaned_content, study_data)."""
    lines = content.split('\n')
    
    # Find section boundaries
    sections = {
        'cloze': {'start': None, 'end': None, 'lines': []},
        'flashcards': {'start': None, 'end': None, 'lines': []},
        'mcq': {'start': None, 'end': None, 'lines': []}
    }
    
    current_section = None
    section_start_patterns = {
        '## cloze': 'cloze',
        '## flashcards': 'flashcards', 
        '## mcq': 'mcq'
    }
    
    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        
        # Check for section start
        for pattern, section_name in section_start_patterns.items():
            if line_lower == pattern:
                if current_section:
                    sections[current_section]['end'] = i
                current_section = section_name
                sections[section_name]['start'] = i
                break
        else:
            # Check for any other ## heading (ends current section)
            if current_section and line.strip().startswith('## '):
                sections[current_section]['end'] = i
                current_section = None
            # Check for horizontal rule ending section
            elif current_section and line.strip() == '---':
                sections[current_section]['end'] = i
                current_section = None
            # Collect lines for current section
            elif current_section and sections[current_section]['start'] is not None:
                sections[current_section]['lines'].append(line)
    
    # Handle section that goes to end of file
    if current_section:
        sections[current_section]['end'] = len(lines)
    
    # Parse each section
    study_data = {}
    
    if sections['cloze']['lines']:
        study_data['cloze'] = parse_cloze(sections['cloze']['lines'])
    
    if sections['flashcards']['lines']:
        study_data['flashcards'] = parse_flashcards(sections['flashcards']['lines'])
    
    if sections['mcq']['lines']:
        study_data['mcq'] = parse_mcq(sections['mcq']['lines'])
    
    # Build cleaned content (remove study sections)
    lines_to_remove = set()
    for section in sections.values():
        if section['start'] is not None and section['end'] is not None:
            for i in range(section['start'], section['end']):
                lines_to_remove.add(i)
    
    cleaned_lines = [line for i, line in enumerate(lines) if i not in lines_to_remove]
    
    # Remove trailing empty lines and horizontal rules
    while cleaned_lines and cleaned_lines[-1].strip() in ('', '---'):
        cleaned_lines.pop()
    
    cleaned_content = '\n'.join(cleaned_lines)
    
    return cleaned_content, study_data

def process_file(md_path, dry_run=False, backup=True):
    """Process a single markdown file."""
    md_path = Path(md_path)
    json_path = md_path.with_suffix('.study.json')
    
    # Read content
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check if already migrated
    if json_path.exists():
        print(f"  ⏭️  Skipping (JSON exists): {md_path.name}")
        return False
    
    # Extract study content
    cleaned_content, study_data = extract_study_sections(content)
    
    # Skip if no study content found
    if not study_data:
        print(f"  ⚪ No study content: {md_path.name}")
        return False
    
    # Compute content hash
    content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
    
    # Build JSON structure
    json_data = {
        'source': md_path.name,
        'migrated_at': datetime.now().isoformat(),
        'content_hash': content_hash,
        **study_data
    }
    
    if dry_run:
        print(f"  🔍 Would migrate: {md_path.name}")
        print(f"      Cloze: {len(study_data.get('cloze', []))} items")
        print(f"      Flashcards: {len(study_data.get('flashcards', []))} items")
        print(f"      MCQs: {len(study_data.get('mcq', []))} items")
        return True
    
    # Backup original
    if backup:
        backup_path = md_path.with_suffix('.md.backup')
        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(content)
    
    # Write JSON
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    
    # Update markdown (remove study sections)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(cleaned_content)
    
    print(f"  ✅ Migrated: {md_path.name}")
    print(f"      → {json_path.name}")
    print(f"      Cloze: {len(study_data.get('cloze', []))}, Flashcards: {len(study_data.get('flashcards', []))}, MCQs: {len(study_data.get('mcq', []))}")
    
    return True

def process_directory(vault_path, dry_run=False, backup=True):
    """Process all markdown files in a vault directory."""
    vault_path = Path(vault_path)
    
    if not vault_path.exists():
        print(f"❌ Path not found: {vault_path}")
        return
    
    print(f"📂 Processing: {vault_path}")
    print(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print()
    
    migrated = 0
    skipped = 0
    
    for md_file in vault_path.rglob('*.md'):
        # Skip hidden folders
        if any(part.startswith('.') for part in md_file.parts):
            continue
        
        if process_file(md_file, dry_run=dry_run, backup=backup):
            migrated += 1
        else:
            skipped += 1
    
    print()
    print(f"📊 Summary: {migrated} migrated, {skipped} skipped")

def main():
    parser = argparse.ArgumentParser(description='Migrate study content from Markdown to JSON')
    parser.add_argument('vault_path', nargs='?', help='Path to the vault/folder to process')
    parser.add_argument('--dry-run', '-n', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--no-backup', action='store_true', help='Do not create backup files')
    parser.add_argument('--file', '-f', help='Process a single file instead of directory')
    
    args = parser.parse_args()
    
    if args.file:
        process_file(args.file, dry_run=args.dry_run, backup=not args.no_backup)
    elif args.vault_path:
        process_directory(args.vault_path, dry_run=args.dry_run, backup=not args.no_backup)
    else:
        parser.print_help()
        print("\nError: Please provide either a vault_path or --file option")

if __name__ == '__main__':
    main()
