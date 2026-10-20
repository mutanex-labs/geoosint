import re
from pathlib import Path

from geoosint.mcp.server import create_server


def test_skill_links_and_tool_references_match_live_contract(runtime):
    root = Path(__file__).parents[1] / "skills/geoosint"
    server, other = create_server(
        runtime.settings.model_copy(
            update={"data_dir": runtime.settings.data_dir / "skill-contract"}
        )
    )
    try:
        tools = {tool.name for tool in server._tool_manager.list_tools()}
        referenced = set()
        for file in root.rglob("*.md"):
            content = file.read_text(encoding="utf-8")
            referenced.update(re.findall(r"\bgeo_[a-z_]+\b", content))
            for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", content):
                if not target.startswith(("https://", "http://")):
                    assert (file.parent / target).is_file(), (file, target)
        assert referenced <= tools, referenced - tools
    finally:
        other.close()
