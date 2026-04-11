"""
Change Queue Manager for narve.ai
Prevents changes from being applied all at once.
Review each change before it goes into the codebase.

Usage: python tools/change_queue.py
Requires: Python 3.10+, git, tkinter (built-in)
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import subprocess
import json
import os
import datetime
from pathlib import Path

QUEUE_FILE = "tools/change_queue.json"
LOG_FILE = "tools/change_log.json"

class ChangeQueueApp:
    """
    Main application window.

    Layout:
    ┌─────────────────────────────────────────────────────┐
    │  narve.ai Change Queue Manager                      │
    ├────────────────────┬────────────────────────────────┤
    │ QUEUE (left panel) │ PREVIEW (right panel)          │
    │                    │                                │
    │ [+] Add change     │ [diff of selected change]      │
    │                    │                                │
    │ 1. Fix login bug   │ --- a/app/auth.py              │
    │ 2. Add skeleton    │ +++ b/app/auth.py              │
    │ 3. Update CSS      │ @@ -42,6 +42,8 @@             │
    │                    │ + new line                     │
    │ [↑] [↓] reorder    │ - removed line                 │
    │ [🗑] remove        │                                │
    ├────────────────────┴────────────────────────────────┤
    │ [Apply Next →]  [Skip]  [Undo Last]  [View Log]     │
    │ Status: 3 changes queued. Next: "Fix login bug"     │
    └─────────────────────────────────────────────────────┘
    """

    def __init__(self, root):
        self.root = root
        self.root.title("narve.ai Change Queue Manager")
        self.root.geometry("1000x700")
        self.queue = self.load_queue()
        self.log = self.load_log()
        self.build_ui()
        self.refresh_queue_list()

    def load_queue(self) -> list:
        """Load queue from JSON file."""
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f:
                return json.load(f)
        return []

    def save_queue(self):
        """Persist queue to JSON file."""
        os.makedirs("tools", exist_ok=True)
        with open(QUEUE_FILE, "w") as f:
            json.dump(self.queue, f, indent=2)

    def load_log(self) -> list:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f:
                return json.load(f)
        return []

    def save_log(self):
        with open(LOG_FILE, "w") as f:
            json.dump(self.log, f, indent=2, default=str)

    def build_ui(self):
        """Build the main UI layout."""

        # Top bar
        top = tk.Frame(self.root, bg="#f5f5f5", pady=8)
        top.pack(fill="x")
        tk.Label(top, text="narve.ai Change Queue Manager",
                 font=("System", 14, "bold"), bg="#f5f5f5").pack(side="left", padx=16)

        # Main pane
        pane = ttk.PanedWindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=4)

        # Left: queue list
        left = tk.Frame(pane, width=340)
        pane.add(left, weight=1)

        tk.Label(left, text="Pending Changes", font=("System", 11, "bold")).pack(anchor="w", padx=8, pady=(8,4))

        # Add change button
        add_frame = tk.Frame(left)
        add_frame.pack(fill="x", padx=8, pady=4)
        tk.Button(add_frame, text="+ Add Change", command=self.add_change,
                  bg="#0d0d0d", fg="white", relief="flat", padx=12, pady=6,
                  cursor="hand2").pack(side="left")
        tk.Button(add_frame, text="Add from file", command=self.add_from_file,
                  relief="flat", padx=12, pady=6,
                  cursor="hand2").pack(side="left", padx=(4,0))

        # Queue listbox
        list_frame = tk.Frame(left)
        list_frame.pack(fill="both", expand=True, padx=8, pady=4)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        self.queue_listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Courier", 10),
            selectbackground="#0d0d0d",
            selectforeground="white",
            activestyle="none",
            height=20
        )
        self.queue_listbox.pack(fill="both", expand=True)
        scrollbar.config(command=self.queue_listbox.yview)
        self.queue_listbox.bind("<<ListboxSelect>>", self.on_select)

        # Reorder/remove buttons
        btn_frame = tk.Frame(left)
        btn_frame.pack(fill="x", padx=8, pady=4)
        tk.Button(btn_frame, text="↑ Move up",   command=self.move_up,   relief="flat", padx=8, pady=4).pack(side="left")
        tk.Button(btn_frame, text="↓ Move down", command=self.move_down, relief="flat", padx=8, pady=4).pack(side="left", padx=4)
        tk.Button(btn_frame, text="🗑 Remove",   command=self.remove_selected, relief="flat", padx=8, pady=4, fg="red").pack(side="right")

        # Right: preview panel
        right = tk.Frame(pane)
        pane.add(right, weight=2)

        tk.Label(right, text="Change Preview", font=("System", 11, "bold")).pack(anchor="w", padx=8, pady=(8,4))

        # Change name
        self.change_name_var = tk.StringVar(value="Select a change to preview")
        tk.Label(right, textvariable=self.change_name_var,
                 font=("System", 10), fg="#555").pack(anchor="w", padx=8)

        # Description
        self.change_desc_var = tk.StringVar()
        tk.Label(right, textvariable=self.change_desc_var,
                 font=("System", 9), fg="#888", wraplength=500, justify="left").pack(anchor="w", padx=8, pady=(2,8))

        # Diff viewer
        self.diff_text = scrolledtext.ScrolledText(
            right,
            font=("Courier", 9),
            bg="#fafafa",
            wrap="none",
            state="disabled"
        )
        self.diff_text.pack(fill="both", expand=True, padx=8, pady=4)

        # Colour tags for diff
        self.diff_text.tag_config("add",    foreground="#1a7a1a", background="#f0fff0")
        self.diff_text.tag_config("remove", foreground="#7a1a1a", background="#fff0f0")
        self.diff_text.tag_config("header", foreground="#555555", font=("Courier", 9, "bold"))
        self.diff_text.tag_config("meta",   foreground="#888888")

        # Bottom bar
        bottom = tk.Frame(self.root, bg="#f0f0f0", pady=8, padx=8)
        bottom.pack(fill="x")

        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(bottom, textvariable=self.status_var,
                 bg="#f0f0f0", font=("System", 9), fg="#555").pack(side="left")

        tk.Button(bottom, text="View Log",
                  command=self.view_log,
                  relief="flat", padx=12, pady=6,
                  cursor="hand2").pack(side="right", padx=4)

        tk.Button(bottom, text="↩ Undo Last",
                  command=self.undo_last,
                  relief="flat", padx=12, pady=6,
                  cursor="hand2").pack(side="right", padx=4)

        tk.Button(bottom, text="⟩⟩ Skip",
                  command=self.skip_change,
                  relief="flat", padx=12, pady=6,
                  cursor="hand2").pack(side="right", padx=4)

        self.apply_btn = tk.Button(
            bottom,
            text="✓ Apply Next →",
            command=self.apply_next,
            bg="#0d0d0d", fg="white",
            relief="flat", padx=16, pady=6,
            font=("System", 10, "bold"),
            cursor="hand2"
        )
        self.apply_btn.pack(side="right", padx=4)

    def refresh_queue_list(self):
        """Refresh the queue listbox from self.queue."""
        self.queue_listbox.delete(0, "end")
        for i, change in enumerate(self.queue):
            prefix = "→ " if i == 0 else f"{i+1}. "
            self.queue_listbox.insert("end", f"{prefix}{change['name']}")

        count = len(self.queue)
        if count == 0:
            self.status_var.set("Queue empty. Add changes to get started.")
            self.apply_btn.config(state="disabled")
        else:
            next_name = self.queue[0]['name']
            self.status_var.set(f"{count} change{'s' if count != 1 else ''} queued. Next: \"{next_name}\"")
            self.apply_btn.config(state="normal")

    def on_select(self, event):
        """Show preview of selected change."""
        selection = self.queue_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx >= len(self.queue):
            return
        change = self.queue[idx]

        self.change_name_var.set(f"#{idx+1}: {change['name']}")
        self.change_desc_var.set(change.get('description', ''))

        self.diff_text.config(state="normal")
        self.diff_text.delete("1.0", "end")

        diff_content = change.get('diff', change.get('description', 'No preview available'))

        for line in diff_content.split('\n'):
            if line.startswith('+') and not line.startswith('+++'):
                self.diff_text.insert("end", line + '\n', "add")
            elif line.startswith('-') and not line.startswith('---'):
                self.diff_text.insert("end", line + '\n', "remove")
            elif line.startswith('@@'):
                self.diff_text.insert("end", line + '\n', "header")
            elif line.startswith('---') or line.startswith('+++'):
                self.diff_text.insert("end", line + '\n', "meta")
            else:
                self.diff_text.insert("end", line + '\n')

        self.diff_text.config(state="disabled")

    def add_change(self):
        """Open dialog to add a new change manually."""
        dialog = AddChangeDialog(self.root)
        self.root.wait_window(dialog.window)
        if dialog.result:
            self.queue.append(dialog.result)
            self.save_queue()
            self.refresh_queue_list()

    def add_from_file(self):
        """Add a change from a .diff or .patch file."""
        filepath = filedialog.askopenfilename(
            title="Select diff/patch file",
            filetypes=[("Diff files", "*.diff *.patch"), ("All files", "*.*")]
        )
        if not filepath:
            return

        with open(filepath) as f:
            diff_content = f.read()

        name = os.path.basename(filepath).replace('.diff', '').replace('.patch', '')
        self.queue.append({
            "id": str(datetime.datetime.now().timestamp()),
            "name": name,
            "description": f"Loaded from {filepath}",
            "diff": diff_content,
            "type": "patch",
            "filepath": filepath,
        })
        self.save_queue()
        self.refresh_queue_list()

    def move_up(self):
        selection = self.queue_listbox.curselection()
        if not selection or selection[0] == 0:
            return
        idx = selection[0]
        self.queue[idx-1], self.queue[idx] = self.queue[idx], self.queue[idx-1]
        self.save_queue()
        self.refresh_queue_list()
        self.queue_listbox.selection_set(idx-1)

    def move_down(self):
        selection = self.queue_listbox.curselection()
        if not selection or selection[0] >= len(self.queue) - 1:
            return
        idx = selection[0]
        self.queue[idx+1], self.queue[idx] = self.queue[idx], self.queue[idx+1]
        self.save_queue()
        self.refresh_queue_list()
        self.queue_listbox.selection_set(idx+1)

    def remove_selected(self):
        selection = self.queue_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        name = self.queue[idx]['name']
        if messagebox.askyesno("Remove change", f"Remove \"{name}\" from queue?"):
            self.queue.pop(idx)
            self.save_queue()
            self.refresh_queue_list()

    def apply_next(self):
        """Apply the first change in the queue."""
        if not self.queue:
            return

        change = self.queue[0]

        # Confirm
        if not messagebox.askyesno(
            "Apply change",
            f"Apply \"{change['name']}\"?\n\n{change.get('description', '')}\n\nThis will modify the codebase."
        ):
            return

        # Get git commit hash before change (for undo)
        before_hash = self.get_git_hash()

        success = False
        error_msg = ""

        try:
            change_type = change.get('type', 'manual')

            if change_type == 'patch' and change.get('filepath'):
                # Apply as git patch
                result = subprocess.run(
                    ["git", "apply", change['filepath']],
                    capture_output=True, text=True
                )
                success = result.returncode == 0
                error_msg = result.stderr

            elif change_type == 'patch' and change.get('diff'):
                # Apply diff content directly
                result = subprocess.run(
                    ["git", "apply", "--whitespace=fix", "-"],
                    input=change['diff'],
                    capture_output=True, text=True
                )
                success = result.returncode == 0
                error_msg = result.stderr

            elif change_type == 'script' and change.get('script'):
                # Run a Python script
                result = subprocess.run(
                    ["python", "-c", change['script']],
                    capture_output=True, text=True
                )
                success = result.returncode == 0
                error_msg = result.stderr or result.stdout

            else:
                # Manual change — just mark as applied
                success = True

        except Exception as e:
            success = False
            error_msg = str(e)

        after_hash = self.get_git_hash()

        # Log the attempt
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "change_name": change['name'],
            "success": success,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "error": error_msg if not success else None,
        }
        self.log.append(log_entry)
        self.save_log()

        if success:
            self.queue.pop(0)
            self.save_queue()
            self.refresh_queue_list()
            self.status_var.set(f"✓ Applied: \"{change['name']}\"")
            messagebox.showinfo("Applied", f"✓ \"{change['name']}\" applied successfully.")
        else:
            self.status_var.set(f"✗ Failed: \"{change['name']}\"")
            messagebox.showerror(
                "Failed to apply",
                f"Could not apply \"{change['name']}\".\n\nError:\n{error_msg}\n\nThe change remains at the top of the queue."
            )

    def skip_change(self):
        """Move the first change to the end of the queue."""
        if not self.queue:
            return
        change = self.queue.pop(0)
        self.queue.append(change)
        self.save_queue()
        self.refresh_queue_list()
        self.status_var.set(f"Skipped \"{change['name']}\" — moved to end of queue.")

    def undo_last(self):
        """Undo the last applied change using git."""
        if not self.log:
            messagebox.showinfo("Nothing to undo", "No changes have been applied yet.")
            return

        last = next((e for e in reversed(self.log) if e['success']), None)
        if not last:
            messagebox.showinfo("Nothing to undo", "No successful changes to undo.")
            return

        before_hash = last.get('before_hash')
        if not before_hash:
            messagebox.showerror("Cannot undo", "No git hash recorded for last change.")
            return

        if not messagebox.askyesno(
            "Undo last change",
            f"Undo \"{last['change_name']}\"?\n\nThis will reset to git commit {before_hash[:8]}.\n\nUnsaved changes will be lost."
        ):
            return

        result = subprocess.run(
            ["git", "checkout", before_hash, "--", "."],
            capture_output=True, text=True
        )

        if result.returncode == 0:
            self.status_var.set(f"↩ Undid: \"{last['change_name']}\"")
            messagebox.showinfo("Undone", f"Successfully reverted to before \"{last['change_name']}\".")
        else:
            messagebox.showerror("Undo failed", f"Could not undo:\n{result.stderr}")

    def view_log(self):
        """Show change log in a new window."""
        log_window = tk.Toplevel(self.root)
        log_window.title("Change Log")
        log_window.geometry("700x500")

        text = scrolledtext.ScrolledText(log_window, font=("Courier", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)

        if not self.log:
            text.insert("end", "No changes logged yet.")
        else:
            for entry in reversed(self.log):
                status = "✓" if entry['success'] else "✗"
                text.insert("end", f"{status} {entry['timestamp'][:19]}  {entry['change_name']}\n")
                if entry.get('error'):
                    text.insert("end", f"   Error: {entry['error']}\n")
                text.insert("end", "\n")

        text.config(state="disabled")

    def get_git_hash(self) -> str | None:
        """Get current git HEAD hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None


class AddChangeDialog:
    """Dialog for adding a new change to the queue manually."""

    def __init__(self, parent):
        self.result = None
        self.window = tk.Toplevel(parent)
        self.window.title("Add Change")
        self.window.geometry("600x500")
        self.window.grab_set()

        tk.Label(self.window, text="Change name:", font=("System", 10)).pack(anchor="w", padx=16, pady=(16,4))
        self.name_entry = tk.Entry(self.window, font=("System", 10))
        self.name_entry.pack(fill="x", padx=16)
        self.name_entry.focus()

        tk.Label(self.window, text="Description:", font=("System", 10)).pack(anchor="w", padx=16, pady=(12,4))
        self.desc_entry = tk.Entry(self.window, font=("System", 10))
        self.desc_entry.pack(fill="x", padx=16)

        tk.Label(self.window, text="Diff / patch content (optional — paste git diff here):", font=("System", 10)).pack(anchor="w", padx=16, pady=(12,4))
        self.diff_text = scrolledtext.ScrolledText(self.window, font=("Courier", 9), height=12)
        self.diff_text.pack(fill="both", expand=True, padx=16)

        btn_frame = tk.Frame(self.window)
        btn_frame.pack(fill="x", padx=16, pady=12)

        tk.Button(btn_frame, text="Cancel", command=self.window.destroy,
                  relief="flat", padx=12, pady=6).pack(side="right", padx=4)
        tk.Button(btn_frame, text="Add to queue →",
                  command=self.submit,
                  bg="#0d0d0d", fg="white", relief="flat", padx=12, pady=6).pack(side="right")

    def submit(self):
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showerror("Required", "Change name is required.")
            return

        self.result = {
            "id": str(datetime.datetime.now().timestamp()),
            "name": name,
            "description": self.desc_entry.get().strip(),
            "diff": self.diff_text.get("1.0", "end").strip(),
            "type": "manual",
        }
        self.window.destroy()


def main():
    root = tk.Tk()
    app = ChangeQueueApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
