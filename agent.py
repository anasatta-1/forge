import json
import os
import threading
from openai import OpenAI
from dotenv import load_dotenv
from tools import TOOLS
from session import SessionState
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

# ── Global Session State ──────────────────────────────────────────────
session = SessionState()

# ── Planner Agent ──────────────────────────────────────────────────────
def planner_agent(task: str, context_summary: str) -> dict:
    """
    Takes a task and returns a step-by-step plan.
    Returns: { "steps": [...], "task": "...", "status": "planned" }
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a planning agent. Break down the task into clear, numbered steps. "
                "Return ONLY valid JSON with this exact format (no extra text before or after):\n"
                "{\"steps\": [{\"id\": 1, \"description\": \"step description here\"}, {\"id\": 2, \"description\": \"next step\"}]}\n"
                "Each step should be actionable and specific. "
                "Do NOT output anything except the JSON object.\n\n"
                + context_summary
            )
        },
        {"role": "user", "content": f"Plan this task:\n{task}"}
    ]

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7
        )
        plan_text = response.choices[0].message.content.strip()
        
        # Try to extract JSON from the response
        try:
            plan_data = json.loads(plan_text)
        except json.JSONDecodeError:
            # If no JSON, structure it ourselves
            plan_data = {
                "steps": [{"id": i+1, "description": line.strip()} 
                         for i, line in enumerate(plan_text.split("\n")) if line.strip()]
            }
        
        steps = plan_data.get("steps", [])
        
        # Safety check: if plan has too many steps, something went wrong
        if len(steps) > 15:
            return {
                "task": task,
                "steps": [{"id": 1, "description": "Plan too complex (>15 steps). Please break down the task into smaller parts."}],
                "status": "error"
            }
        
        return {
            "task": task,
            "steps": steps,
            "status": "planned"
        }
    except Exception as e:
        return {
            "task": task,
            "steps": [{"id": 1, "description": f"Error planning: {str(e)}"}],
            "status": "error"
        }

# ── Executor Agent ─────────────────────────────────────────────────────
def executor_agent(plan: dict, context_summary: str, event_queue: queue.Queue, stop_event: threading.Event) -> dict:
    """
    Takes a plan and executes it step by step.
    Emits events for each step, and returns execution results.
    Returns: { "plan": {...}, "execution": {...}, "status": "completed" or "failed" }
    """
    def emit(type: str, label: str, content: str):
        event_queue.put({"type": type, "label": label, "content": content})

    execution = {
        "steps_completed": [],
        "steps_failed": [],
        "total_steps": len(plan.get("steps", []))
    }

    messages = [
        {
            "role": "system",
            "content": (
                "You are an executor agent. Your job is to execute the steps in a plan. "
                "Use the available tools to take real actions. "
                "Always use tools one at a time, wait for the result, then move to the next step. "
                "Follow the plan exactly as given. If a step fails, try to recover or move on. "
                "On Windows use 'python' not 'python3', and 'mkdir' to create folders. "
                "Do NOT use list_files. To check files, use run_bash with 'dir' or 'ls'.\n\n"
                + context_summary
            )
        }
    ]

    for idx, step in enumerate(plan.get("steps", []), 1):
        if stop_event.is_set():
            emit("error", "STOPPED", "Execution was stopped by user.")
            break

        step_id = step.get("id", idx)
        step_desc = step.get("description", "")
        
        emit("step_start", f"STEP {step_id}/{len(plan.get('steps', []))}", step_desc)

        # Build the prompt for this step
        messages.append({
            "role": "user",
            "content": f"Execute step {step_id}: {step_desc}"
        })

        step_attempt = 0
        max_attempts = 3
        step_success = False

        while step_attempt < max_attempts and not step_success:
            step_attempt += 1
            if stop_event.is_set():
                emit("error", "STOPPED", "Execution was stopped by user.")
                break

            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto"
                )
            except Exception as e:
                emit("error", "API_ERROR", str(e))
                execution["steps_failed"].append({"step_id": step_id, "reason": str(e)})
                break

            message = response.choices[0].message

            if not message.tool_calls:
                # No tools called, step is done
                emit("step_complete", f"STEP {step_id} DONE", message.content or "Step complete.")
                execution["steps_completed"].append({"step_id": step_id, "output": message.content})
                step_success = True
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
                    emit("error", "STOPPED", "Execution was stopped by user.")
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

                # ── Update session context based on tool calls ──
                if name == "write_file":
                    path = args.get("path", "")
                    if path:
                        session.update_context(files=[path])
                
                elif name == "run_bash":
                    cmd = args.get("command", "")
                    if cmd:
                        session.update_context(command=cmd, output=result)
                        # Try to detect folder creation from mkdir/mkdir -p commands
                        if "mkdir" in cmd.lower():
                            # Extract folder path (rough parsing)
                            parts = cmd.split()
                            for i, part in enumerate(parts):
                                if part == "mkdir" or part.endswith("mkdir"):
                                    if i + 1 < len(parts):
                                        folder = parts[i + 1].strip()
                                        if folder and not folder.startswith("-"):
                                            session.update_context(folders=[folder])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result
                })

            step_success = True  # Step executed (tools ran)

        if not step_success and step_attempt >= max_attempts:
            execution["steps_failed"].append({"step_id": step_id, "reason": "Max retries reached"})

    return {
        "plan": plan,
        "execution": execution,
        "status": "completed" if not execution["steps_failed"] else "completed_with_errors"
    }

# ── Agent loop (v1 style, will be deprecated) ──────────────────────────
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

@app.route("/run", methods=["POST"])
def run():
    data = request.json
    task = data.get("task", "").strip()
    run_id = data.get("id", "default")
    mode = data.get("mode", "plan")  # "plan" or "execute"
    edited_plan = data.get("plan", None)  # for execute mode
    
    if not task and mode == "plan":
        return jsonify({"error": "No task provided"}), 400

    event_queue = queue.Queue()
    stop_event = threading.Event()
    active_runs[run_id] = stop_event

    def run_planner_then_executor():
        def emit(type: str, label: str, content: str):
            event_queue.put({"type": type, "label": label, "content": content})

        # ── PHASE 1: Planning ──
        if mode == "plan":
            emit("phase", "PHASE", "Planning...")
            context_summary = session.get_context_summary()
            plan = planner_agent(task, context_summary)
            emit("plan", "PLAN", json.dumps(plan))
            session.current_run = {"task": task, "plan": plan}
            event_queue.put(None)
            return

        # ── PHASE 2: Execution ──
        if mode == "execute":
            emit("phase", "PHASE", "Executing...")
            context_summary = session.get_context_summary()
            plan = edited_plan or session.current_run.get("plan", {})
            
            execution_result = executor_agent(plan, context_summary, event_queue, stop_event)
            
            # Save to history
            session.add_to_history(task, plan, execution_result["execution"])
            session.current_run = None
            
            emit("execution_complete", "EXECUTION_COMPLETE", json.dumps(execution_result))
            event_queue.put(None)

    thread = threading.Thread(target=run_planner_then_executor)
    thread.daemon = True
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

@app.route("/history", methods=["GET"])
def history():
    return jsonify({"history": session.history})

@app.route("/current-plan", methods=["GET"])
def current_plan():
    if session.current_run and session.current_run.get("plan"):
        return jsonify({"plan": session.current_run["plan"]})
    return jsonify({"plan": None}), 404

@app.route("/session-state", methods=["GET"])
def session_state():
    return jsonify({
        "context": session.context,
        "history_count": len(session.history),
        "current_run_exists": session.current_run is not None
    })

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