 
import subprocess
import os

def read_file(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: File '{path}' not found."

def write_file(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w") as f:
        f.write(content)
    return f"File '{path}' written successfully."

def run_bash(command: str) -> str:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=15
        )
        output = result.stdout or result.stderr
        return output.strip() or "Command ran with no output."
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 15 seconds."
    except Exception as e:
        return f"Error running command: {str(e)}"

def list_files(directory: str = ".") -> str:
    try:
        files = []
        for root, dirs, filenames in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in ["venv", ".git", "__pycache__"]]
            for filename in filenames:
                filepath = os.path.join(root, filename)
                files.append(filepath)
        return "\n".join(files) if files else "No files found."
    except Exception as e:
        return f"Error listing files: {str(e)}"

# Registry — maps tool names the LLM calls to actual functions
TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_bash": run_bash,
    "list_files": list_files,
}