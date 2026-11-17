#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check for exposed credentials before reading untrusted repository content.

Read-only. It never prints secret values, only names and paths.

Checks:
- sensitive files under a directory (.env, private keys, credential stores)
- sensitive environment variable names
- git hooks / symlink / file-protocol settings
- whether the directory is inside the allowed data root

Usage:
    python security_preflight.py --path repos/repo
    python security_preflight.py --path . --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

def _allowed_roots() -> tuple[Path, ...]:
    """Allowed filesystem roots: A2A_ALLOWED_ROOTS (pathsep-separated) or repo root."""
    raw = os.environ.get("A2A_ALLOWED_ROOTS", "")
    roots = tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())
    return roots or (Path(__file__).resolve().parents[1],)


ALLOWED_ROOTS = _allowed_roots()
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "site-packages",
    "dist",
    "build",
    "target",
    ".next",
    "coverage",
}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
SENSITIVE_ENV_RE = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL|"
    r"AUTH|_PAT$|^PAT$)"
)
EXTRA_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_READ_TOKEN",
    "SSH_AUTH_SOCK",
}
EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".md")


@dataclass
class Finding:
    severity: str
    kind: str
    target: str
    detail: str


def _under_allowed_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _sensitive_file(name: str) -> tuple[str, str] | None:
    lower = name.lower()
    if lower.endswith(EXAMPLE_SUFFIXES):
        return None
    if lower == ".env" or (
        lower.startswith(".env.") and not lower.endswith((".example", ".sample", ".template"))
    ):
        return "high", "environment file (may contain secrets)"
    if lower in {".npmrc", ".pypirc", ".git-credentials", "_netrc", "netrc"}:
        return "medium", "credential configuration"
    if lower in {
        "credentials",
        "credentials.json",
        "auth.json",
        "cookies.sqlite",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }:
        return "high", "credential store or private key"
    if lower.endswith((".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx")):
        return "high", "key material"
    return None


def _scan_files(root: Path, max_files: int) -> tuple[list[Finding], bool]:
    findings: list[Finding] = []
    count = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            count += 1
            if count > max_files:
                truncated = True
                break
            match = _sensitive_file(filename)
            if not match:
                continue
            severity, detail = match
            path = Path(dirpath) / filename
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            findings.append(
                Finding(
                    severity=severity,
                    kind="sensitive_file",
                    target=str(path),
                    detail=f"{detail}; size={size} bytes",
                )
            )
        if truncated:
            break
    return findings, truncated


def _scan_env() -> list[Finding]:
    findings: list[Finding] = []
    for name, value in os.environ.items():
        if not value:
            continue
        upper = name.upper()
        if upper not in EXTRA_ENV_NAMES and not SENSITIVE_ENV_RE.search(name):
            continue
        if upper == "GITHUB_TOKEN":
            severity = "high"
            detail = "general GitHub token present; prefer a read-only GITHUB_READ_TOKEN"
        elif upper == "GITHUB_READ_TOKEN":
            severity = "medium"
            detail = "read token present; verify it is read-only and single-repository"
        elif upper == "SSH_AUTH_SOCK":
            severity = "medium"
            detail = "SSH agent socket is exposed to this process"
        else:
            severity = "high"
            detail = "secret-like environment variable is set (value hidden)"
        findings.append(
            Finding(severity=severity, kind="environment", target=name, detail=detail)
        )
    return findings


def _git_config(root: Path, key: str) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        completed = subprocess.run(
            [git, "-C", str(root), "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_config_regexp(root: Path, pattern: str) -> list[str]:
    git = shutil.which("git")
    if not git:
        return []
    try:
        completed = subprocess.run(
            [git, "-C", str(root), "config", "--get-regexp", pattern],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _scan_git(root: Path) -> list[Finding]:
    if not (root / ".git").exists():
        return []
    findings: list[Finding] = []

    if (root / ".gitmodules").exists():
        findings.append(
            Finding(
                severity="medium",
                kind="git_config",
                target=str(root),
                detail=".gitmodules present; do not recurse submodules when cloning",
            )
        )

    dangerous = _git_config_regexp(
        root,
        r"^(url\..*\.insteadof|core\.sshcommand|core\.fsmonitor|core\.pager|"
        r"diff\..*\.command|filter\..*\.(smudge|clean))$",
    )
    for line in dangerous:
        key = line.split(None, 1)[0]
        findings.append(
            Finding(
                severity="medium",
                kind="git_config",
                target=str(root),
                detail=f"{key} is set; git may execute or redirect code (value hidden)",
            )
        )

    hooks = _git_config(root, "core.hooksPath")
    if hooks and hooks.lower() not in {"nul", "/dev/null"}:
        findings.append(
            Finding(
                severity="medium",
                kind="git_config",
                target=str(root),
                detail=f"core.hooksPath is set to {hooks!r}; hooks may execute repository code",
            )
        )
    symlinks = _git_config(root, "core.symlinks")
    if symlinks and symlinks.lower() == "true":
        findings.append(
            Finding(
                severity="medium",
                kind="git_config",
                target=str(root),
                detail="core.symlinks=true; checkout may create links outside the tree",
            )
        )
    file_protocol = _git_config(root, "protocol.file.allow")
    if file_protocol and file_protocol.lower() not in {"never", "false"}:
        findings.append(
            Finding(
                severity="medium",
                kind="git_config",
                target=str(root),
                detail=f"protocol.file.allow={file_protocol!r}; local file transports are enabled",
            )
        )
    return findings


def highest_severity(findings: list[Finding]) -> str:
    best = "none"
    best_rank = 0
    for finding in findings:
        rank = SEVERITY_RANK.get(finding.severity, 0)
        if rank > best_rank:
            best = finding.severity
            best_rank = rank
    return best


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Check for exposed credentials before reading untrusted content.")
    parser.add_argument("--path", default=".", help="directory to scan (default current directory)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--max-files", type=int, default=5000)
    parser.add_argument(
        "--fail-on",
        choices=("none", "low", "medium", "high"),
        default="high",
        help="exit 3 when this severity is reached (default high)",
    )
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"error: path is not a directory: {root}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    if not _under_allowed_root(root):
        findings.append(
            Finding(
                severity="medium",
                kind="policy",
                target=str(root),
                detail="path is outside the allowed roots; generated data must stay in the data root",
            )
        )

    file_findings, truncated = _scan_files(root, max(1, args.max_files))
    findings.extend(file_findings)
    if truncated:
        findings.append(
            Finding(
                severity="low",
                kind="scan",
                target=str(root),
                detail=f"stopped after {args.max_files} files; scan may be incomplete",
            )
        )
    findings.extend(_scan_env())
    findings.extend(_scan_git(root))
    findings.sort(key=lambda item: (-SEVERITY_RANK[item.severity], item.kind, item.target))

    severity = highest_severity(findings)
    payload = {
        "path": str(root.resolve()),
        "severity": severity,
        "findings": [asdict(item) for item in findings],
        "counts": {
            "high": sum(1 for item in findings if item.severity == "high"),
            "medium": sum(1 for item in findings if item.severity == "medium"),
            "low": sum(1 for item in findings if item.severity == "low"),
        },
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"[security-preflight] path={payload['path']} severity={severity} "
            f"findings={len(findings)}"
        )
        for finding in findings:
            print(
                f"  {finding.severity:6s} {finding.kind:16s} "
                f"{finding.target}: {finding.detail}"
            )
        if not findings:
            print("  no obvious credential exposure found; sandbox and token scope still need manual review")

    reached = (
        args.fail_on != "none"
        and severity != "none"
        and SEVERITY_RANK[severity] >= SEVERITY_RANK[args.fail_on]
    )
    return 3 if reached else 0


if __name__ == "__main__":
    raise SystemExit(main())
