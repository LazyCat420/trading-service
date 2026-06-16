import os
import re
from pathlib import Path

def replace_low_max_tokens(match):
    prefix = match.group(1)
    val_str = match.group(2)
    val = int(val_str)
    
    if val < 8192:
        return f"{prefix}8192"
    return match.group(0)

def main():
    base_dir = Path(__file__).parent.parent / "app"
    pattern = re.compile(r'(max_tokens\s*=\s*)(\d+)')
    
    modified_count = 0
    files_modified = 0
    
    for py_file in base_dir.rglob("*.py"):
        try:
            with open(py_file, "r") as f:
                content = f.read()
                
            new_content, count = pattern.subn(replace_low_max_tokens, content)
            
            if count > 0 and new_content != content:
                with open(py_file, "w") as f:
                    f.write(new_content)
                print(f"Modified {py_file.name}: replaced {count} max_tokens")
                modified_count += count
                files_modified += 1
                
        except Exception as e:
            print(f"Failed processing {py_file}: {e}")
            
    print(f"\nDone! Modified {modified_count} max_tokens across {files_modified} files.")

if __name__ == "__main__":
    main()
