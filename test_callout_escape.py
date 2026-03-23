#!/usr/bin/env python3
"""Test cases for callout title HTML escaping."""

import sys
import re

# Import the convert_obsidian_callouts function from app.py
sys.path.insert(0, '/home/vineeth/clawd/obsidian-viewer')
from app import convert_obsidian_callouts

def test_ampersand_in_title():
    """Test that & in callout title is properly escaped."""
    md = "> [!question]- 🤔 Why Slide 7? (Y-Axis Equation & Purpose)\n> Some content here"
    html = convert_obsidian_callouts(md)
    
    assert "&amp;" in html, f"Expected &amp; in output, got: {html}"
    assert "Misplaced &" not in html, f"Should not have 'Misplaced &' error"
    assert "Y-Axis Equation &amp; Purpose" in html, f"Title not properly escaped: {html}"
    print("✅ test_ampersand_in_title passed")

def test_less_than_in_title():
    """Test that < in callout title is properly escaped."""
    md = "> [!note] Compare A < B\n> Content"
    html = convert_obsidian_callouts(md)
    
    assert "&lt;" in html, f"Expected &lt; in output, got: {html}"
    assert "A &lt; B" in html, f"Title not properly escaped: {html}"
    print("✅ test_less_than_in_title passed")

def test_greater_than_in_title():
    """Test that > in callout title is properly escaped."""
    md = "> [!note] When X > Y\n> Content"
    html = convert_obsidian_callouts(md)
    
    assert "&gt;" in html, f"Expected &gt; in output, got: {html}"
    print("✅ test_greater_than_in_title passed")

def test_multiple_special_chars():
    """Test multiple special characters together."""
    md = "> [!warning]+ A & B < C > D\n> Important!"
    html = convert_obsidian_callouts(md)
    
    assert "&amp;" in html, "& should be escaped"
    assert "&lt;" in html, "< should be escaped"
    assert "&gt;" in html, "> should be escaped"
    print("✅ test_multiple_special_chars passed")

def test_normal_title_unchanged():
    """Test that normal titles without special chars work fine."""
    md = "> [!info] Normal Title Here\n> Content"
    html = convert_obsidian_callouts(md)
    
    assert "Normal Title Here" in html, f"Normal title should be present: {html}"
    print("✅ test_normal_title_unchanged passed")

def test_markdown_in_title_still_works():
    """Test that bold/italic in titles still work after escaping."""
    md = "> [!tip] **Bold** & *Italic*\n> Content"
    html = convert_obsidian_callouts(md)
    
    assert "<strong>Bold</strong>" in html, "Bold should work"
    assert "<em>Italic</em>" in html, "Italic should work"
    assert "&amp;" in html, "& should be escaped"
    print("✅ test_markdown_in_title_still_works passed")

def test_task_list_unchecked():
    """Test that - [ ] converts to unchecked checkbox."""
    from app import convert_task_lists
    md = "- [ ] Word frequency distribution (Zipf)\n- [ ] Word length"
    html = convert_task_lists(md)
    
    assert 'type="checkbox"' in html, f"Should have checkbox: {html}"
    assert 'disabled' in html, f"Should be disabled: {html}"
    assert 'checked' not in html or 'checked disabled' not in html.replace('checked disabled', ''), f"Should not be checked: {html}"
    print("✅ test_task_list_unchecked passed")

def test_task_list_checked():
    """Test that - [x] converts to checked checkbox."""
    from app import convert_task_lists
    md = "- [x] Completed task\n- [X] Also completed"
    html = convert_task_lists(md)
    
    assert 'checked' in html, f"Should have checked: {html}"
    assert 'task-done' in html, f"Should have task-done class: {html}"
    print("✅ test_task_list_checked passed")


if __name__ == "__main__":
    print("Running callout escape tests...\n")
    
    try:
        test_ampersand_in_title()
        test_less_than_in_title()
        test_greater_than_in_title()
        test_multiple_special_chars()
        test_normal_title_unchanged()
        test_markdown_in_title_still_works()
        test_task_list_unchecked()
        test_task_list_checked()
        
        print("\n" + "="*50)
        print("🎉 All tests passed!")
        print("="*50)
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Error: {e}")
        sys.exit(1)
