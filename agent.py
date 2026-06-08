import json
import os
import threading
from openai import OpenAI
from dotenv import load_dotenv
from tools import TOOLS
from flask import Flask, Response, request, jsonify, send_from_directory
import queue

# ── Setup ──────────────────────────────────────────────────────────────
load_dotenv()
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)
MODEL = "llama-3.3-70b-versatile"
app = Flask(__name__, static_folder="frontend")

# ── Tool definitions (list_files removed — causes Groq issues) ─────────
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating it if it doesn't exist",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a terminal command and return the output",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run"}
                },
                "required": ["command"]
            }
        }
    }
]

# ── Agent loop ──────────────────────────────────────────────────────────
def run_agent(task: str, event_queue: queue.Queue, stop_event: threading.Event):
    def emit(type: str, label: str, content: str):
        event_queue.put({"type": type, "label": label, "content": content})

    emit("task", "TASK", task)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a dev agent. You help users by writing code, creating files, "
                "and running commands using the tools available to you. "
                "Always use tools to take real actions — never describe what you would do. "
                "Use tools one at a time, wait for the result, then decide the next step. "
                "Keep going until the task is fully complete. "
                "On Windows use 'python' not 'python3', and 'mkdir' to create folders. "
                "Do NOT use list_files. To check files, use run_bash with 'dir' or 'ls'."
            )
        },
        {"role": "user", "content": task}
    ]

    for step in range(10):
        if stop_event.is_set():
            emit("error", "STOPPED", "Task was stopped by user.")
            break

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto"
            )
        except Exception as e:
            emit("error", "ERROR", str(e))
            break

        message = response.choices[0].message

        if not message.tool_calls:
            emit("done", "DONE", message.content or "Task complete.")
            break

        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in message.tool_calls
            ]
        })

        for tc in message.tool_calls:
            if stop_event.is_set():
                emit("error", "STOPPED", "Task was stopped by user.")
                break

            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            emit("tool_call", f"TOOL → {name}", json.dumps(args, indent=2))

            if name in TOOLS:
                result = TOOLS[name](**args)
            else:
                result = f"Error: unknown tool '{name}'"

            emit("result", f"RESULT ← {name}", result)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result
            })

    else:
        emit("error", "NOTE", "Reached max steps (10).")

    event_queue.put(None)

# ── Active runs tracker ─────────────────────────────────────────────────
active_runs = {}

# ── Flask routes ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")

@app.route("/run", methods=["GET"])
def run():
    task = request.args.get("task", "").strip()
    run_id = request.args.get("id", "default")
    if not task:
        return jsonify({"error": "No task provided"}), 400

    event_queue = queue.Queue()
    stop_event = threading.Event()
    active_runs[run_id] = stop_event

    thread = threading.Thread(target=run_agent, args=(task, event_queue, stop_event))
    thread.start()

    def stream():
        while True:
            event = event_queue.get()
            if event is None:
                yield "data: [DONE]\n\n"
                active_runs.pop(run_id, None)
                break
            yield f"data: {json.dumps(event)}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/stop", methods=["POST"])
def stop():
    run_id = request.json.get("id", "default")
    if run_id in active_runs:
        active_runs[run_id].set()
        return jsonify({"status": "stopped"})
    return jsonify({"status": "not found"}), 404

if __name__ == "__main__":
    print("\n  Forge running at http://localhost:8080\n")
    app.run(port=8080, debug=False)