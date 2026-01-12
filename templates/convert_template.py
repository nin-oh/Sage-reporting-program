#!/usr/bin/env python3
"""
Automatic conversion of report.html to dashboard.html with base template
Usage: python convert_template.py
"""

def convert_report_to_dashboard(input_file='report.html', output_file='dashboard.html'):
    """Convert standalone report.html to template-based dashboard.html"""
    
    print(f"🔄 Converting {input_file} to {output_file}...")
    
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"❌ Error: {input_file} not found!")
        print(f"   Make sure {input_file} is in the current directory")
        return False
    
    # Extract CSS section
    css_start = content.find('<style>')
    css_end = content.find('</style>') + len('</style>')
    
    if css_start == -1 or css_end == -1:
        print("❌ Error: Could not find <style> section")
        return False
    
    css_block = content[css_start:css_end]
    print(f"✅ Extracted CSS block ({len(css_block)} characters)")
    
    # Extract body content
    body_start = content.find('<body>')
    body_end = content.find('</body>')
    
    if body_start == -1 or body_end == -1:
        print("❌ Error: Could not find <body> section")
        return False
    
    body_content = content[body_start + len('<body>'):body_end].strip()
    print(f"✅ Extracted body content ({len(body_content)} characters)")
    
    # Extract script section
    script_start = content.find('<script>')
    script_end = content.rfind('</script>') + len('</script>')  # Use rfind for last script
    
    if script_start == -1 or script_end == -1:
        print("❌ Error: Could not find <script> section")
        return False
    
    script_block = content[script_start:script_end]
    print(f"✅ Extracted script block ({len(script_block)} characters)")
    
    # Create new template with Jinja2 blocks
    new_template = f'''{{% extends "base.html" %}}

{{% block title %}}Dashboard - Executive Dashboard{{% endblock %}}

{{% block extra_styles %}}
{css_block}
{{% endblock %}}

{{% block content %}}
{body_content}
{{% endblock %}}

{{% block extra_scripts %}}
{script_block}
{{% endblock %}}
'''
    
    # Write to output file
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(new_template)
        print(f"✅ Successfully created {output_file}")
        print(f"📊 File size: {len(new_template)} characters")
        return True
    except Exception as e:
        print(f"❌ Error writing file: {e}")
        return False


def verify_conversion(output_file='dashboard.html'):
    """Verify the converted file has correct structure"""
    
    print(f"\n🔍 Verifying {output_file}...")
    
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"❌ {output_file} not found")
        return False
    
    checks = {
        'Extends base': '{{% extends "base.html" %}}' in content,
        'Title block': '{{% block title %}}' in content,
        'Styles block': '{{% block extra_styles %}}' in content,
        'Content block': '{{% block content %}}' in content,
        'Scripts block': '{{% block extra_scripts %}}' in content,
        'No DOCTYPE': '<!DOCTYPE' not in content,
        'No html tag': '<html' not in content,
        'No head tag': '<head>' not in content,
        'No body tag': '<body>' not in content,
    }
    
    all_passed = True
    for check, result in checks.items():
        status = '✅' if result else '❌'
        print(f"  {status} {check}")
        if not result:
            all_passed = False
    
    if all_passed:
        print("\n🎉 Conversion verified! File is ready to use.")
    else:
        print("\n⚠️  Some checks failed. Review the file manually.")
    
    return all_passed


if __name__ == "__main__":
    print("=" * 60)
    print("🔄 REPORT.HTML → DASHBOARD.HTML CONVERTER")
    print("=" * 60)
    print()
    
    success = convert_report_to_dashboard()
    
    if success:
        print()
        verify_conversion()
        print()
        print("=" * 60)
        print("📋 NEXT STEPS:")
        print("=" * 60)
        print("1. Upload dashboard.html to Render templates/ folder")
        print("2. Update app.py route to use 'dashboard.html'")
        print("3. Test at: https://your-app.onrender.com/report/CLIENT")
        print("=" * 60)
    else:
        print("\n❌ Conversion failed. Check error messages above.")
