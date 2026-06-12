import datetime
import json

class SessionState:
    def __init__(self):
        self.context = {
            "files_created": [],      # list of file paths
            "folders_created": [],    # list of folder paths
            "commands_run": [],       # list of commands executed
            "command_outputs": {}     # {command: output}
        }
        self.history = []             # list of past tasks
        self.current_run = None       # {task, plan, execution_in_progress}
    
    def get_context_summary(self) -> str:
        """Return a readable summary of session state for agent prompts"""
        summary = "## Session Context\n\n"
        
        if self.context["files_created"]:
            summary += f"**Files created in this session:** {', '.join(self.context['files_created'][:10])}\n"
            if len(self.context['files_created']) > 10:
                summary += f"(and {len(self.context['files_created']) - 10} more)\n"
            summary += "\n"
        
        if self.context["folders_created"]:
            summary += f"**Folders created in this session:** {', '.join(self.context['folders_created'])}\n\n"
        
        if self.context["commands_run"]:
            summary += f"**Recent commands run:** \n"
            for cmd in self.context["commands_run"][-5:]:  # last 5
                summary += f"  - {cmd}\n"
            summary += "\n"
        
        if self.history:
            summary += f"**Tasks completed in this session:** {len(self.history)}\n"
            for task_record in self.history[-3:]:  # last 3
                summary += f"  - {task_record['task'][:60]}...\n" if len(task_record['task']) > 60 else f"  - {task_record['task']}\n"
            summary += "\n"
        
        if summary == "## Session Context\n\n":
            summary = "## Session Context\nThis is a fresh session with no prior context.\n\n"
        
        return summary
    
    def add_to_history(self, task: str, plan: dict, execution: dict):
        """Save completed task to history"""
        self.history.append({
            "task": task,
            "plan": plan,
            "execution": execution,
            "timestamp": datetime.datetime.now().isoformat()
        })
    
    def update_context(self, files: list = None, folders: list = None, 
                       command: str = None, output: str = None):
        """Update session context after actions"""
        if files:
            self.context["files_created"].extend([f for f in files if f not in self.context["files_created"]])
        if folders:
            self.context["folders_created"].extend([f for f in folders if f not in self.context["folders_created"]])
        if command:
            self.context["commands_run"].append(command)
            if output:
                self.context["command_outputs"][command] = output