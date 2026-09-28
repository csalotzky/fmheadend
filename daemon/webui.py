"""Optional static SPA serving. API errors never fall through to index.html."""
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse


def mount_webui(app, directory):
    root = Path(directory).resolve()
    if not (root / "index.html").is_file():
        raise ValueError("WebUI directory must contain the production index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        asset = (root / path).resolve()
        if not asset.is_relative_to(root):
            raise HTTPException(404, "Not Found")
        if asset.is_file():
            return FileResponse(asset, headers={"Cache-Control": "no-cache"})
        # Only client routes get fallback; missing assets remain 404s.
        parts = path.strip("/").split("/") if path.strip("/") else []
        valid = not parts or parts in (["dashboard"], ["sources"], ["modulators"], ["audio-profiles"], ["system"])
        if len(parts) == 2 and parts[0] in ("sources", "modulators", "audio-profiles"):
            valid = parts[1].isdigit() or (parts[1] == "new" and parts[0] != "modulators")
            if parts == ["audio-profiles", "edit"]:
                valid = True
        if not valid:
            raise HTTPException(404, "Not Found")
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
