import json
import logging
import os
import glob
from app.tools.registry import registry, PermissionLevel

logger = logging.getLogger(__name__)

NOTES_DIR = "/app/notes"

@registry.register(
    name="read_user_notes",
    description="Read markdown notes written by the human user. These notes contain human thoughts, context, watchlists, or market observations. Always check this if the user tells you they 'wrote it down' or if you need extra context.",
    parameters={
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Optional. The specific .md filename to read. If omitted, returns a list of all available notes."
            }
        },
        "required": []
    },
    tier=1,
    source="notes",
    permission=PermissionLevel.READ_ONLY,
)
async def read_user_notes(filename: str = None) -> str:
    try:
        if not os.path.exists(NOTES_DIR):
            return json.dumps({"status": "error", "message": "Notes directory not found or not mounted."})

        if not filename:
            files = glob.glob(os.path.join(NOTES_DIR, "*.md"))
            notes_list = [os.path.basename(f) for f in files]
            if not notes_list:
                return json.dumps({"status": "success", "message": "No notes available."})
            return json.dumps({"status": "success", "notes": notes_list, "message": "Specify a filename to read its contents."})

        if not filename.endswith(".md"):
            filename += ".md"

        filepath = os.path.join(NOTES_DIR, filename)
        if not os.path.exists(filepath):
            return json.dumps({"status": "error", "message": f"Note {filename} not found."})

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        return json.dumps({
            "status": "success",
            "filename": filename,
            "content": content
        })
    except Exception as e:
        logger.error("[NotesTool] Read failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
