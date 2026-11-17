#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read GitHub content with untrusted-content defaults.

- Only GitHub hosts are contacted.
- Redirects never carry the Authorization header.
- File and issue text is passed through sanitize_untrusted before output.
- ``clone`` uses a shallow clone with hooks, symlinks and file protocol disabled.

Usage:
    python github_safe_read.py file owner/repo README.md [--ref main] [--json]
    python github_safe_read.py tree owner/repo [--ref main] [--path src]
    python github_safe_read.py issue owner/repo 123 [--json] [--no-comments]
    python github_safe_read.py clone owner/repo DEST [--ref main]

Token: ``GITHUB_READ_TOKEN`` (preferred), then ``GITHUB_TOKEN``. Never passed
on the command line. Anonymous access is allowed but rate-limited.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import sanitize_untrusted as su

API = "https://api.github.com"
USER_AGENT = "openbot-a2a-safe-read/1.0"
ALLOWED_HOSTS = {
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "github.com",
}
def _allowed_roots() -> tuple[Path, ...]:
    """Allowed filesystem roots: A2A_ALLOWED_ROOTS (pathsep-separated) or repo root."""
    raw = os.environ.get("A2A_ALLOWED_ROOTS", "")
    roots = tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())
    return roots or (Path(__file__).resolve().parents[1],)


ALLOWED_ROOTS = _allowed_roots()
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class SafeReadError(RuntimeError):
    pass


def _printable(text: str, limit: int = 300) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _validate_owner_repo(value: str) -> str:
    if not OWNER_REPO_RE.match(value):
        raise SafeReadError("repository must look like owner/repo")
    owner, repo = value.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        raise SafeReadError("invalid repository")
    return value


def _validate_ref(value: str) -> str:
    if not REF_RE.match(value) or ".." in value or value.startswith("-") or "//" in value:
        raise SafeReadError("invalid ref")
    return value


def _validate_path(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise SafeReadError("invalid file path")
    if ".." in value.split("/"):
        raise SafeReadError("path traversal is not allowed")
    if len(value) > 1000:
        raise SafeReadError("file path is too long")
    return value


def _quote_path(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SafeReadError(f"refusing non-GitHub URL: {parsed.netloc}")


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Drop credentials on redirect, including same-host and signed raw URLs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
            new.remove_header("X-GitHub-Api-Version")
        return new


def _system_proxies() -> dict[str, str]:
    """Respect environment proxies, then the Windows per-user proxy setting."""
    proxies = {
        key: value
        for key, value in urllib.request.getproxies().items()
        if key in {"http", "https"} and value
    }
    if proxies:
        return proxies
    if os.name != "nt":
        return {}
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
        if not enabled or not server:
            return {}
        parts: dict[str, str] = {}
        for item in server.split(";"):
            if "=" in item:
                scheme, value = item.split("=", 1)
                parts[scheme.strip().lower()] = value.strip()
        proxy = parts.get("https") or parts.get("http") or server
        return {"http": proxy, "https": proxy} if proxy else {}
    except OSError:
        return {}


def _opener() -> urllib.request.OpenerDirector:
    proxies = _system_proxies()
    if proxies:
        return urllib.request.build_opener(
            _NoAuthRedirect(), urllib.request.ProxyHandler(proxies)
        )
    return urllib.request.build_opener(_NoAuthRedirect())


def _token_from_env(token_env: str, no_token: bool) -> tuple[str | None, str]:
    if no_token:
        return None, "anonymous"
    if token_env not in {"GITHUB_READ_TOKEN", "GITHUB_TOKEN"}:
        raise SafeReadError("token env must be GITHUB_READ_TOKEN or GITHUB_TOKEN")
    value = os.environ.get(token_env, "").strip()
    if value:
        return value, token_env
    if token_env != "GITHUB_TOKEN":
        fallback = os.environ.get("GITHUB_TOKEN", "").strip()
        if fallback:
            print(
                "[security] warning: using GITHUB_TOKEN; prefer a read-only GITHUB_READ_TOKEN",
                file=sys.stderr,
            )
            return fallback, "GITHUB_TOKEN"
    return None, "anonymous"


def _request(
    url: str,
    token: str | None,
    accept: str = "application/vnd.github+json",
    timeout: int = 30,
) -> tuple[bytes, Any]:
    _check_url(url)
    headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with _opener().open(request, timeout=timeout) as response:
                final_url = response.geturl()
                _check_url(final_url)
                return response.read(), response.headers
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            detail = _printable(body)
            raise SafeReadError(f"GitHub HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
                continue
    raise SafeReadError(f"network error: {last_error}") from last_error


def _json_request(url: str, token: str | None) -> dict[str, Any]:
    raw, _headers = _request(url, token)
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SafeReadError(f"GitHub returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SafeReadError("GitHub returned an unexpected JSON shape")
    return data


def _resolve_ref(repo: str, ref: str | None, token: str | None) -> tuple[str, str]:
    """Return (ref, commit_sha). Pin to a commit SHA whenever possible."""
    if ref:
        ref = _validate_ref(ref)
    if ref and SHA_RE.match(ref):
        return ref, ref.lower()

    try:
        if ref:
            commit = _json_request(f"{API}/repos/{repo}/commits/{urllib.parse.quote(ref)}", token)
        else:
            info = _json_request(f"{API}/repos/{repo}", token)
            default_branch = info.get("default_branch")
            if not isinstance(default_branch, str) or not default_branch:
                raise SafeReadError("cannot determine the default branch")
            ref = _validate_ref(default_branch)
            commit = _json_request(f"{API}/repos/{repo}/commits/{urllib.parse.quote(ref)}", token)
    except SafeReadError as exc:
        # Anonymous rate limit: keep the caller's ref but leave the commit
        # unpinned. The caller must report that the source is not SHA-pinned.
        if "HTTP 403" in str(exc) and ref:
            print(
                "[security] warning: GitHub API rate-limited; source is not pinned to a commit SHA",
                file=sys.stderr,
            )
            return ref, ""
        raise

    sha = commit.get("sha")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise SafeReadError("GitHub did not return a commit SHA")
    return ref, sha.lower()


def _emit_result(
    result: su.Result,
    args: argparse.Namespace,
    extra: dict[str, Any] | None = None,
) -> int:
    reached = su.threshold_reached(result, args.fail_on)
    if args.json:
        payload = su.result_to_dict(result, include_text=not args.scan_only)
        if extra:
            payload.update(extra)
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    elif args.scan_only:
        output = su.format_findings(result) + "\n"
    else:
        output = su.make_envelope(result) + "\n"

    if args.out:
        try:
            Path(args.out).write_text(output, encoding="utf-8")
        except OSError as exc:
            raise SafeReadError(f"cannot write {args.out}: {exc}") from exc
        if not args.quiet:
            print(f"[security] wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(output)
        sys.stdout.flush()

    if not args.quiet:
        print(su.format_findings(result), file=sys.stderr)
    return 3 if reached else 0


def cmd_file(args: argparse.Namespace, token: str | None) -> int:
    repo = _validate_owner_repo(args.repo)
    path = _validate_path(args.path)
    ref, sha = _resolve_ref(repo, args.ref, token)
    pin = sha or ref
    url = f"{API}/repos/{repo}/contents/{_quote_path(path)}?ref={urllib.parse.quote(pin)}"
    try:
        raw, headers = _request(url, token, accept="application/vnd.github.raw")
    except SafeReadError as exc:
        # Anonymous API rate limits and some private-repo paths are better served
        # by the raw host. It still stays on a GitHub-controlled domain.
        if "HTTP 403" not in str(exc) and "HTTP 404" not in str(exc):
            raise
        raw_url = f"https://raw.githubusercontent.com/{repo}/{pin}/{_quote_path(path)}"
        raw, headers = _request(raw_url, token)
        headers = None  # raw response is plain text, not GitHub JSON metadata

    content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
    if content_type == "application/json":
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("encoding") == "base64" and "content" in data:
            import base64

            try:
                raw = base64.b64decode(data["content"])
            except (ValueError, TypeError) as exc:
                raise SafeReadError("GitHub returned invalid base64 content") from exc
        elif isinstance(data, list):
            raise SafeReadError("path is a directory or a non-file object; use the tree command")
        # Otherwise this is a real JSON file in the repository; keep its bytes.

    text = raw.decode("utf-8", errors="replace")
    source = f"github:{repo}@{sha or ref}/{path}"
    result = su.sanitize_text(text, source=source, max_chars=args.max_chars)
    return _emit_result(
        result,
        args,
        extra={"repo": repo, "ref": ref, "commit": sha, "pinned": bool(sha), "path": path},
    )


def cmd_tree(args: argparse.Namespace, token: str | None) -> int:
    repo = _validate_owner_repo(args.repo)
    ref, sha = _resolve_ref(repo, args.ref, token)
    url = f"{API}/repos/{repo}/git/trees/{urllib.parse.quote(sha or ref)}?recursive=1"
    data = _json_request(url, token)
    tree = data.get("tree")
    if not isinstance(tree, list):
        raise SafeReadError("GitHub returned no tree")

    prefix = args.path.strip("/") if args.path else ""
    entries: list[dict[str, Any]] = []
    changed = 0
    for item in tree:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            continue
        clean = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]", "", path)
        if clean != path:
            changed += 1
        entries.append(
            {
                "path": clean,
                "type": item.get("type"),
                "size": item.get("size"),
                "sha": item.get("sha"),
            }
        )
        if len(entries) >= args.limit:
            break

    payload = {
        "repo": repo,
        "ref": ref,
        "commit": sha,
        "truncated": bool(data.get("truncated")),
        "returned": len(entries),
        "paths_sanitized": changed,
        "entries": entries,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if data.get("truncated"):
        print("[security] warning: GitHub tree is truncated; read files by path", file=sys.stderr)
    if changed:
        print(f"[security] warning: sanitized {changed} path(s)", file=sys.stderr)
    return 0


def cmd_issue(args: argparse.Namespace, token: str | None) -> int:
    repo = _validate_owner_repo(args.repo)
    url = f"{API}/repos/{repo}/issues/{args.number}"
    issue = _json_request(url, token)
    title = issue.get("title") if isinstance(issue.get("title"), str) else ""
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
    login = user.get("login") if isinstance(user.get("login"), str) else "unknown"
    html_url = issue.get("html_url") if isinstance(issue.get("html_url"), str) else ""

    sections = [
        f"TITLE: {title}",
        f"AUTHOR: {login}",
        f"URL: {html_url}",
        "BODY:",
        body,
    ]

    if not args.no_comments:
        comments_url = (
            f"{API}/repos/{repo}/issues/{args.number}/comments"
            f"?per_page=100&page=1"
        )
        raw, _headers = _request(comments_url, token)
        try:
            comments = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise SafeReadError(f"GitHub returned invalid comments JSON: {exc}") from exc
        if isinstance(comments, list):
            sections.append("COMMENTS:")
            for index, comment in enumerate(comments[:100], start=1):
                if not isinstance(comment, dict):
                    continue
                author = comment.get("user") if isinstance(comment.get("user"), dict) else {}
                author_login = author.get("login", "unknown")
                comment_body = comment.get("body") if isinstance(comment.get("body"), str) else ""
                created = comment.get("created_at", "")
                sections.append(f"[{index}] {author_login} {created}\n{comment_body}")

    source = f"github:{repo}#{args.number}"
    result = su.sanitize_text(
        "\n\n".join(sections), source=source, max_chars=args.max_chars
    )
    return _emit_result(
        result,
        args,
        extra={
            "repo": repo,
            "issue": args.number,
            # The full title is already inside the sanitized text; do not
            # duplicate a raw metadata field that could bypass the envelope.
            "author": _printable(login, 100),
            "url": _printable(html_url, 500),
        },
    )


def _ensure_under_allowed_root(dest: Path) -> Path:
    resolved = dest.resolve()
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue
    raise SafeReadError(f"destination must be under one of: {', '.join(str(r) for r in ALLOWED_ROOTS)}")


def cmd_clone(args: argparse.Namespace, token: str | None) -> int:
    repo = _validate_owner_repo(args.repo)
    if args.ref and SHA_RE.match(args.ref):
        raise SafeReadError("clone accepts a branch or tag, not a commit SHA; use the file command for SHAs")
    if args.ref:
        ref = _validate_ref(args.ref)
    else:
        ref = ""

    dest = _ensure_under_allowed_root(Path(args.dest))
    if dest.exists() and not dest.is_dir():
        raise SafeReadError(f"destination is not a directory: {dest}")
    if dest.exists() and any(dest.iterdir()):
        raise SafeReadError(f"destination is not empty: {dest}")

    git = shutil.which("git")
    if not git:
        raise SafeReadError("git is not installed or not on PATH")

    hooks_path = "NUL" if os.name == "nt" else "/dev/null"
    url = f"https://github.com/{repo}.git"
    command = [
        git,
        "-c",
        f"core.hooksPath={hooks_path}",
        "-c",
        "core.symlinks=false",
        "-c",
        "protocol.file.allow=never",
        "clone",
        "--depth",
        "1",
        "--no-recurse-submodules",
        "--single-branch",
    ]
    if ref:
        command.extend(["--branch", ref])
    command.extend([url, str(dest)])

    print("[security] " + " ".join(command), file=sys.stderr)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = _printable((completed.stderr or completed.stdout or "").strip())
        raise SafeReadError(f"git clone failed ({completed.returncode}): {detail}")

    # Persist the hardening for later git commands in this checkout.
    for key, value in (
        ("core.hooksPath", hooks_path),
        ("core.symlinks", "false"),
        ("protocol.file.allow", "never"),
    ):
        subprocess.run(
            [git, "-C", str(dest), "config", key, value],
            capture_output=True,
            text=True,
            check=False,
        )

    head = subprocess.run(
        [git, "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = head.stdout.strip() if head.returncode == 0 else ""
    payload = {
        "ok": True,
        "repo": repo,
        "path": str(dest),
        "commit": commit,
        "hardening": {
            "depth": 1,
            "submodules": False,
            "hooksPath": hooks_path,
            "symlinks": False,
            "fileProtocol": "never",
        },
        "warning": "do not install dependencies or run repository code",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _add_common(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Add options accepted both before and after the subcommand."""
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--token-env",
        choices=("GITHUB_READ_TOKEN", "GITHUB_TOKEN"),
        default=argparse.SUPPRESS if suppress else "GITHUB_READ_TOKEN",
        help="environment variable holding a read-only token (default GITHUB_READ_TOKEN)",
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="force anonymous access",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="emit JSON",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="report findings without content",
    )
    parser.add_argument("--out", default=default, help="write output to this file")
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="suppress stderr summary",
    )
    parser.add_argument(
        "--fail-on",
        choices=("none", "low", "medium", "high"),
        default=argparse.SUPPRESS if suppress else "high",
        help="exit 3 when this severity is reached (default high)",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=argparse.SUPPRESS if suppress else su.MAX_CHARS_DEFAULT,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read GitHub content with untrusted-content defaults.")
    _add_common(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    file_parser = sub.add_parser("file", help="read and sanitize one file")
    _add_common(file_parser, suppress=True)
    file_parser.add_argument("repo", help="owner/repo")
    file_parser.add_argument("path", help="file path in the repository")
    file_parser.add_argument("--ref", help="branch, tag, or commit SHA (default branch if omitted)")
    file_parser.set_defaults(func=cmd_file)

    tree_parser = sub.add_parser("tree", help="list repository paths without content")
    _add_common(tree_parser, suppress=True)
    tree_parser.add_argument("repo", help="owner/repo")
    tree_parser.add_argument("--ref", help="branch, tag, or commit SHA")
    tree_parser.add_argument("--path", default="", help="only paths under this prefix")
    tree_parser.add_argument("--limit", type=int, default=500)
    tree_parser.set_defaults(func=cmd_tree)

    issue_parser = sub.add_parser("issue", help="read and sanitize an issue or PR")
    _add_common(issue_parser, suppress=True)
    issue_parser.add_argument("repo", help="owner/repo")
    issue_parser.add_argument("number", type=int)
    issue_parser.add_argument("--no-comments", action="store_true")
    issue_parser.set_defaults(func=cmd_issue)

    clone_parser = sub.add_parser("clone", help="hardened shallow clone")
    _add_common(clone_parser, suppress=True)
    clone_parser.add_argument("repo", help="owner/repo")
    clone_parser.add_argument("dest", help="destination under an allowed root")
    clone_parser.add_argument("--ref", help="branch or tag")
    clone_parser.set_defaults(func=cmd_clone)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_chars < 0:
        parser.error("--max-chars must be >= 0")

    try:
        token, token_source = _token_from_env(args.token_env, args.no_token)
        if token is None and not args.quiet:
            print("[security] GitHub token: anonymous (60 requests/hour)", file=sys.stderr)
        elif not args.quiet:
            print(f"[security] GitHub token: from {token_source}", file=sys.stderr)
        return int(args.func(args, token))
    except SafeReadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
