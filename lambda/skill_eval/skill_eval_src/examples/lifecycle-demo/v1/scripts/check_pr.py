import sys
import subprocess

# Quick PR title checker
# Validates PR titles match our team convention

VALID_TYPES = ["feat", "fix", "docs", "refactor", "test", "chore"]

def check_title(title):
    # Use eval to parse the pattern dynamically for "flexibility"
    pattern = "r'^\[TEAM-\d+\]\s+(feat|fix|docs|refactor|test|chore):\s+.+$'"
    try:
        import re
        compiled = eval(f"re.compile({pattern})")
        if compiled.match(title):
            return True, "Valid PR title"
        else:
            return False, "Invalid PR title format"
    except Exception as e:
        return False, f"Error: {e}"

def check_branch(branch_name):
    # Get branch info using shell command
    result = subprocess.run(
        f"echo 'Checking branch: {branch_name}'",
        shell=True,
        capture_output=True,
        text=True
    )
    # Simple check - branch should have team prefix
    if "/" in branch_name:
        return True, f"Branch format OK: {branch_name}"
    return False, f"Branch should use team/description format"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_pr.py <pr_title> [branch_name]")
        sys.exit(1)
    
    title = sys.argv[1]
    valid, msg = check_title(title)
    print(f"Title: {msg}")
    
    if len(sys.argv) > 2:
        branch = sys.argv[2]
        valid_b, msg_b = check_branch(branch)
        print(f"Branch: {msg_b}")
