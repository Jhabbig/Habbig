# Change Queue Manager

Prevents changes from being applied to the codebase all at once.
Review each change before it goes in.

## Usage
python tools/change_queue.py

## Adding changes
- Click "+ Add Change" to add manually
- Click "Add from file" to load a .diff or .patch file
- Paste a git diff into the diff field for preview

## Applying changes
- Click a change to preview the diff
- Click "Apply Next →" to apply the first change
- Click "Skip" to move it to the end
- Click "Undo Last" to revert using git

## Queue persistence
Queue is saved to tools/change_queue.json
Log is saved to tools/change_log.json
Both are gitignored — local only

## Keyboard
- Select change: click in list
- Apply: click button (no accidental keyboard shortcut)
