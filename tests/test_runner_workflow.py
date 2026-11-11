"""Replay the private-runner workflow steps with no git identity and no installed app."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / "deploy" / "private-runner" / ".github" / "workflows" / "weekly-forward.yml"


def _runs(text: str) -> list[str]:
    found: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith("run: |"):
            indent = len(raw) - len(raw.lstrip())
            index += 1
            body: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                    break
                body.append(line[indent + 2 :] if line.startswith(" " * (indent + 2)) else "")
                index += 1
            found.append("\n".join(body).strip() + "\n")
            continue
        if stripped.startswith("run: "):
            found.append(stripped.removeprefix("run: ").strip() + "\n")
        index += 1
    return found


def _commit(repo: Path, message: str, env: dict[str, str]) -> None:
    _git(
        repo,
        "-c",
        "user.name=sterling-forward-runner",
        "-c",
        "user.email=sterling-forward-runner@users.noreply.github.com",
        "commit",
        "-m",
        message,
        env=env,
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env)


class _Ping(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self) -> None:
        _Ping.paths.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def _layout(tmp: Path) -> tuple[Path, Path, dict[str, str], str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp / "empty-home"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (tmp / "empty-home").mkdir()
    # /usr/bin/python3 sees an editable install under dist-packages. A venv does not,
    # which matches a GitHub runner that has not pip-installed this repo.
    venv = tmp / "venv"
    subprocess.run(["/usr/bin/python3", "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    probe = subprocess.run([str(venv / "bin" / "python"), "-c", "import sterling"], env=env, capture_output=True, text=True)
    assert probe.returncode != 0, probe.stdout + probe.stderr
    shim = tmp / "bin"
    shim.mkdir()
    (shim / "python").symlink_to(venv / "bin" / "python")
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"

    app_src = tmp / "app-src"
    shutil.copytree(REPO / "src", app_src / "src")
    shutil.copytree(REPO / "scripts", app_src / "scripts")
    _git(app_src, "init", "-b", "main", env=env)
    _git(app_src, "add", "src", "scripts", env=env)
    _commit(app_src, "App", env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=app_src, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    workspace = tmp / "work"
    app = workspace / "app"
    subprocess.run(["git", "clone", str(app_src), str(app)], check=True, capture_output=True, text=True, env=env)

    seed = tmp / "seed"
    seed.mkdir()
    shutil.copytree(REPO / "deploy" / "private-runner", seed / "deploy" / "private-runner")
    (seed / "deploy" / "private-runner" / "pinned_app_sha.txt").write_text(sha + "\n", encoding="utf-8")
    _git(seed, "init", "-b", "main", env=env)
    _git(seed, "add", "deploy", env=env)
    _commit(seed, "Private runner", env)
    _git(seed, "branch", "evidence", env=env)
    bare = tmp / "private.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)], check=True, capture_output=True, text=True, env=env)
    private = workspace / "private"
    subprocess.run(["git", "clone", str(bare), str(private)], check=True, capture_output=True, text=True, env=env)
    return workspace, bare, env, sha


def _fill(script: str, provider: str) -> str:
    return (
        script.replace("${{ github.event_name }}", "workflow_dispatch")
        .replace("${{ inputs.provider }}", provider)
        .replace("${{ inputs.policy }}", "paper-v2")
    )


def _branch(repo: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _shell(workspace: Path, script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-lc", script], cwd=workspace, env=env, capture_output=True, text=True, check=False)


def test_workflow_steps_keep_main_and_publish_mock(tmp_path: Path) -> None:
    blocks = _runs(WORKFLOW.read_text(encoding="utf-8"))
    assert len(blocks) == 3
    pin_step, decide_step, ping_step = blocks
    workspace, bare, env, sha = _layout(tmp_path)
    output = tmp_path / "github_output"
    env["GITHUB_OUTPUT"] = str(output)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ping)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env["HEALTHCHECKS_PING_URL"] = f"http://127.0.0.1:{server.server_port}/secret-token-77"
    _Ping.paths.clear()
    try:
        pinned = _shell(workspace, pin_step, env)
        assert pinned.returncode == 0, pinned.stderr
        assert sha in output.read_text(encoding="utf-8")
        private = workspace / "private"
        ping = private / "deploy" / "private-runner" / "ping_healthchecks.py"
        hook = private / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        blocked = _shell(workspace, _fill(decide_step, "mock"), env)
        assert blocked.returncode != 0, blocked.stdout
        assert "provider_calls=1" in blocked.stdout
        assert "Author identity unknown" not in blocked.stderr
        assert ping.is_file()
        assert _branch(private, env) == "main"
        absent = subprocess.run(
            ["git", "--git-dir", str(bare), "cat-file", "-e", "evidence:evidence/invest-v2/mock/forward.jsonl"],
            capture_output=True,
            env=env,
        )
        assert absent.returncode != 0
        fail_ping = _shell(workspace, ping_step.replace("${{ job.status }}", "failure"), env)
        assert fail_ping.returncode == 0, fail_ping.stderr
        assert _Ping.paths == ["/secret-token-77/fail"]
        assert "secret-token-77" not in fail_ping.stdout
        hook.unlink()
        retried = _shell(workspace, _fill(decide_step, "mock"), env)
        assert retried.returncode == 0, retried.stderr + retried.stdout
        assert "provider_calls=0" in retried.stdout
        assert "provider_calls=1" not in retried.stdout
        assert ping.is_file()
        assert _branch(private, env) == "main"
        remote = subprocess.run(
            ["git", "--git-dir", str(bare), "show", "evidence:evidence/invest-v2/mock/forward.jsonl"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout
        assert '"evidence_lane":"mock"' in remote
        assert remote.count('"record_type":"decision"') == 1
        success = _shell(workspace, ping_step.replace("${{ job.status }}", "success"), env)
        assert success.returncode == 0, success.stderr
        assert _Ping.paths[-1] == "/secret-token-77"
        assert "secret-token-77" not in success.stdout
        again = _shell(workspace, _fill(decide_step, "mock"), env)
        assert again.returncode == 0, again.stderr + again.stdout
        assert "provider_calls=0" in again.stdout
        assert "provider_calls=1" not in again.stdout
        still = subprocess.run(
            ["git", "--git-dir", str(bare), "show", "evidence:evidence/invest-v2/mock/forward.jsonl"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout
        assert still.count('"record_type":"decision"') == 1
        rejected = _shell(workspace, _fill(decide_step, "nope"), env)
        assert rejected.returncode != 0
    finally:
        server.shutdown()
