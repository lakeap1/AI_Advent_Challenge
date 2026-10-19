"""One allowlisted read-only Git tool over real MCP stdio transport."""
import os
from pathlib import Path
import subprocess
import shutil


def git_current_branch():
    repo_value = os.environ.get("GRAPHICS_REPO_ROOT")
    if not repo_value:
        raise ValueError("repository is not configured")
    repo = Path(repo_value).resolve()
    if not repo.is_dir():
        raise ValueError("configured repository is missing")
    def git(*arguments):
        executable = shutil.which("git")
        if executable and os.name == "nt" and Path(executable).parent.name.casefold() == "cmd":
            direct = Path(executable).parent.parent / "mingw64" / "bin" / "git.exe"
            if direct.is_file():
                executable = str(direct)
        if not executable:
            raise ValueError("git executable unavailable")
        completed = subprocess.run([executable, "-C", str(repo), *arguments], capture_output=True,
                                   text=True, timeout=5, check=False, shell=False, stdin=subprocess.DEVNULL)
        if completed.returncode != 0:
            raise ValueError("configured checkout has no readable branch and commit")
        return completed.stdout.strip()
    top_level = git("rev-parse", "--show-toplevel")
    if not top_level or Path(top_level).resolve() != repo:
        raise ValueError("configured repository is not the checkout root")
    branch = git("symbolic-ref", "--short", "HEAD")
    commit = git("rev-parse", "--verify", "HEAD")
    if not branch or len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("configured checkout has no readable branch and commit")
    return {"branch": branch, "commit": commit}


def main():
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("graphics-read-only-git")
    server.tool(name="git_current_branch")(git_current_branch)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
