"""A narrow publisher to be executed by WorkBuddy, using existing noninteractive Git auth.

This program never opens a browser, creates credentials, changes global Git
settings, stages unlisted files, replaces a repository, or force-pushes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", default="workbuddy-manager-gateway")
    parser.add_argument("--check", action="store_true", help="Validate source list only; no credentials, repository creation, or upload")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", arguments.owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", arguments.repo):
        raise RuntimeError("Invalid GitHub owner or repository name")
    root = Path(__file__).resolve().parent.parent
    names = [line.strip() for line in (root / "docs/PUBLISH_MANIFEST.txt").read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.startswith("#")]
    for name in names:
        relative = PurePosixPath(name)
        candidate = root.joinpath(*relative.parts)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name or not candidate.is_file() or candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            raise RuntimeError("Unsafe or missing publish manifest entry")
        if any(part in {".git", "__pycache__", "state", ".state", "work", "data", "logs"} for part in relative.parts) or candidate.name.startswith(".env"):
            raise RuntimeError("Private/generated file in publish manifest")
        candidate.read_text(encoding="utf-8-sig")
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate publish manifest entry")
    if arguments.check:
        print(json.dumps({"checked_files": len(names), "target": f"{arguments.owner}/{arguments.repo}", "published": False}))
        return
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is not installed; no upload attempted")
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)

    def run(*args, stdin=None, timeout=90, secret=False):
        result = subprocess.run([git, "-c", "http.sslBackend=openssl", *args], cwd=root, env=env,
                                input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            # Never print credential helper stdout, stderr, headers or authenticated URLs.
            raise RuntimeError("Git authentication is unavailable" if secret else "Git operation failed; inspect it locally without exposing credentials")
        return result.stdout

    credentials = run("credential", "fill", stdin="protocol=https\nhost=github.com\n\n", timeout=30, secret=True)
    credential = dict(line.split("=", 1) for line in credentials.splitlines() if "=" in line)
    token = credential.get("password")
    if not token:
        raise RuntimeError("No existing GitHub credential; interactive login was not started")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def api(path, method="GET", body=None, allow_missing=False):
        request = urllib.request.Request("https://api.github.com" + path, method=method,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                           "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                           "User-Agent": "workbuddy-manager-gateway-publisher"})
        try:
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if allow_missing and error.code == 404:
                return None
            raise RuntimeError(f"GitHub API returned HTTP {error.code}; no secret response printed") from None

    profile = api("/user")
    if str(profile.get("login", "")).lower() != arguments.owner.lower():
        raise RuntimeError("Authenticated GitHub account differs from the authorized target; upload refused")
    target = f"{profile['login']}/{arguments.repo}"
    repository = api("/repos/" + target, allow_missing=True)
    if repository is None:
        repository = api("/user/repos", "POST", {"name": arguments.repo, "private": False,
            "description": "Local manager-to-WorkBuddy task gateway with durable tasks and independent acceptance",
            "auto_init": False, "has_issues": True})
    elif repository.get("size", 0) != 0:
        raise RuntimeError("Target repository is not empty; refusing to replace existing work")
    if repository.get("private") is not False or repository.get("owner", {}).get("login", "").lower() != arguments.owner.lower():
        raise RuntimeError("Repository owner/visibility differs from the authorized public target")
    remote = "https://github.com/" + target + ".git"
    if (root / ".git").exists():
        current_remote = run("remote", "get-url", "origin").strip()
        if current_remote != remote:
            raise RuntimeError("Existing checkout has a different remote; refusing to change it")
    else:
        run("init", "-b", "main")
        run("remote", "add", "origin", remote)
    run("add", "--", *names)
    staged = run("diff", "--cached", "--name-only").splitlines()
    if not staged or set(staged) != set(names):
        raise RuntimeError("Staged file set differs from the reviewed source manifest")
    private_patterns = [r"gh[pousr]_[A-Za-z0-9_]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
                        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"]
    for name in staged:
        content = run("show", ":" + name)
        if any(re.search(pattern, content) for pattern in private_patterns):
            raise RuntimeError("Potential secret detected in staged source; no commit or upload attempted")
    name = profile["login"]
    email = f"{profile['id']}+{name}@users.noreply.github.com"
    run("-c", "user.name=" + name, "-c", "user.email=" + email, "commit", "-m", "Initial WorkBuddy manager gateway")
    revision = run("rev-parse", "HEAD").strip()
    run("push", "-u", "origin", "main", timeout=120)
    print(json.dumps({"repository": repository["html_url"], "commit": revision,
                      "files": len(staged), "uploaded": True}))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(json.dumps({"error": str(error), "uploaded": False}), file=sys.stderr)
        sys.exit(1)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # Error text is intentionally stable: credential helper/network exceptions
        # can contain access material. Detailed diagnostics stay in their provider.
        print(json.dumps({"error": "Publication blocked. Verify noninteractive GitHub authentication, account/target, and the reviewed source manifest. No browser was opened."}), file=sys.stderr)
        sys.exit(1)
