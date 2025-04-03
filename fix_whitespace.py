import os
import glob

def remove_trailing_whitespace(file_path):
    """Remove trailing whitespace from each line in the file."""
    with open(file_path, 'r', encoding='utf-8') as file:
        lines = file.readlines()

    # Remove trailing whitespace from each line and ensure blank lines have no whitespace
    cleaned_lines = []
    for line in lines:
        if line.strip() == '':
            # Blank line - ensure it has no whitespace
            cleaned_lines.append('\n')
        else:
            # Non-blank line - remove trailing whitespace
            cleaned_lines.append(line.rstrip() + '\n')

    # Write the cleaned content back to the file
    with open(file_path, 'w', encoding='utf-8') as file:
        file.writelines(cleaned_lines)

# Get all Python files
python_files = glob.glob('**/*.py', recursive=True)

# Process each file
for file_path in python_files:
    remove_trailing_whitespace(file_path)

print(f"Processed {len(python_files)} files")

# Remove trailing whitespace from main.py
if __name__ == "__main__":
    remove_trailing_whitespace('main.py')
    print("Trailing whitespace removed from main.py")

