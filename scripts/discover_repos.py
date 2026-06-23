#!/usr/bin/env python3
"""Discover Claude Code skills / plugins / agents repos on GitHub and update config JSON.

Pipeline:
    search GitHub  ->  filter junk  ->  fetch git tree  ->  classify ->
    merge into JSON  ->  schema-validate  ->  (optional) git commit + push

Usage:
    # Just print what would change (default)
    python3 scripts/discover_repos.py

    # Apply to JSON files but don't touch git
    python3 scripts/discover_repos.py --apply

    # Apply, commit and push to origin (used by cron)
    python3 scripts/discover_repos.py --apply --push

Env:
    GITHUB_TOKEN  or  GH_TOKEN     Optional. Boosts API rate limit & enables push.
    DISCOVER_LIMIT_PER_QUERY       Optional int (default 50)
    DISCOVER_MAX_NEW               Optional int. Hard cap on new entries per run.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT_FILE = REPO_ROOT / "agent_repos.json"
SKILL_FILE = REPO_ROOT / "skill_repos.json"
PLUGIN_FILE = REPO_ROOT / "plugin_repos.json"
REPORT_FILE = REPO_ROOT / "scripts" / "discovery_last_report.json"
DENYLIST_FILE = REPO_ROOT / "scripts" / "discovery_denylist.txt"

API_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
LIMIT_PER_QUERY = int(os.environ.get("DISCOVER_LIMIT_PER_QUERY", "50"))
MAX_NEW = int(os.environ.get("DISCOVER_MAX_NEW", "30"))

# Repo-search queries that surface real Claude Code asset repos.
SEARCH_QUERIES: list[str] = [
    "topic:claude-skills",
    "topic:claude-code-skills",
    "topic:claude-code-plugins",
    "topic:claude-code-marketplace",
    "topic:claude-plugins",
    "topic:claude-code-subagents",
    "topic:claude-subagents",
    "topic:claude-code-agents",
    "topic:claude-code-plugin",
    "topic:claude-skill",
]


# ---------------------------------------------------------------------------
# HTTP


def _request(path: str, params: dict | None = None) -> Any:
    """GET an API URL with retries on 429/5xx and return parsed JSON.

    Returns dict/list on success, None on 404.
    """
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "awesome-repo-configs-discover/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (403, 429, 502, 503, 504):
                wait = min(30, 2 ** (attempt + 1))
                # Honour Retry-After if present
                ra = exc.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, int(ra))
                # Honour rate-limit reset for 403 secondary rate
                reset = exc.headers.get("X-RateLimit-Reset")
                if exc.code == 403 and reset and reset.isdigit():
                    delta = int(reset) - int(time.time())
                    if 0 < delta < 120:
                        wait = max(wait, delta + 2)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GitHub API kept failing for {url}")


# ---------------------------------------------------------------------------
# Search + probe


def search_repos(query: str, limit: int) -> list[dict]:
    """Search GitHub for repos. Returns list of repo dicts."""
    out: list[dict] = []
    per_page = min(100, limit)
    pages = (limit + per_page - 1) // per_page
    for page in range(1, pages + 1):
        data = _request(
            "/search/repositories",
            {"q": query, "sort": "updated", "per_page": per_page, "page": page},
        )
        items = (data or {}).get("items", [])
        out.extend(items)
        if len(items) < per_page:
            break
    return out[:limit]


def fetch_tree(full: str, branch: str) -> list[str] | None:
    """Return all file paths in the repo (recursive). None on failure / truncated."""
    data = _request(f"/repos/{full}/git/trees/{branch}", {"recursive": "1"})
    if not data:
        return None
    return [entry["path"] for entry in data.get("tree", []) if entry.get("type") == "blob"]


# ---------------------------------------------------------------------------
# Filters & classifiers


JUNK_OWNER_PATTERNS = [
    re.compile(r"^[A-Z][a-z]+[A-Z][a-z]+[a-z]+\d{2,}$"),   # "ColorfulNoun123"
    re.compile(r"^[A-Za-z]{12,}-?\d{2,}$"),                  # "Anaglyphic-hogshead760"
    re.compile(r"^[a-z]+-\d{3,}$"),                          # "user-12345"
]
SPAMMY_DESC_TOKENS = {
    "porn", "onlyfans", "casino", "gambling", "pump-and-dump", "viagra",
    "betting", "ponzi", "free-bitcoin",
}


def looks_spammy(repo: dict) -> bool:
    owner = (repo.get("owner") or {}).get("login", "")
    desc = (repo.get("description") or "").lower()
    if any(p.match(owner) for p in JUNK_OWNER_PATTERNS):
        return True
    if any(tok in desc for tok in SPAMMY_DESC_TOKENS):
        return True
    return False


def freshness_days(pushed_at: str | None) -> int | None:
    if not pushed_at:
        return None
    try:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def passes_quality_bar(repo: dict) -> bool:
    """Cheap filters before paying for a tree fetch."""
    if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
        return False
    if (repo.get("size") or 0) < 4:        # KB; empty repos
        return False
    stars = repo.get("stargazers_count") or 0
    fresh = freshness_days(repo.get("pushed_at"))
    # Require either a star OR a recent push (fresh hobby repo) — drops abandoned junk.
    if stars < 1 and (fresh is None or fresh > 365):
        return False
    if looks_spammy(repo):
        return False
    return True


def classify(full: str, paths: list[str]) -> dict | None:
    """Inspect a repo's file tree and return classification dict, or None if nothing matches.

    Output keys:
        skill:        bool
        skillsPath:   str | None
        plugin:       bool
        agent:        bool
        agentsPath:   str | None
    """
    has_root_skill = "SKILL.md" in paths
    skill_files = [p for p in paths if p.endswith("/SKILL.md") or p == "SKILL.md"]
    marketplace_files = [p for p in paths if p.endswith(".claude-plugin/marketplace.json")]
    plugin_jsons = [p for p in paths if p.endswith(".claude-plugin/plugin.json")]
    agent_files = [
        p for p in paths
        if p.endswith(".md")
        and (p.startswith("agents/") or p.startswith(".claude/agents/"))
    ]

    result = {
        "skill": False,
        "skillsPath": None,
        "plugin": False,
        "agent": False,
        "agentsPath": None,
    }

    if marketplace_files:
        result["plugin"] = True
    # Treat a repo with a .claude-plugin/plugin.json + multiple skills as a plugin too
    if plugin_jsons and (len(skill_files) >= 3 or marketplace_files):
        result["plugin"] = True

    if skill_files:
        result["skill"] = True
        if has_root_skill and len(skill_files) <= 2:
            result["skillsPath"] = "./"
        else:
            # Group skill files by their first path component; pick the dominant one.
            top = Counter(
                p.split("/SKILL.md")[0].split("/")[0]
                for p in skill_files
                if "/SKILL.md" in p
            )
            common = [t for t, _ in top.most_common()]
            # Skip junk roots we never want as a skill root
            blocklist_roots = {".gemini", ".opencode", ".agents", "packages", "docs",
                                "tests", "test", "examples", "__MACOSX", "node_modules"}
            chosen = next((t for t in common if t not in blocklist_roots), None)
            dominant_share = (top.most_common(1)[0][1] / len(skill_files)) if top else 0

            if chosen in {"skills", "Skills", "library"} and dominant_share >= 0.6:
                result["skillsPath"] = chosen
            elif chosen == ".claude" and dominant_share >= 0.6:
                result["skillsPath"] = ".claude/skills"
            else:
                # No clean single skillsPath — still classify as skill (consumer can walk),
                # but leave skillsPath unset. Downstream tooling treats missing skillsPath
                # as "scan the whole repo for SKILL.md".
                result["skillsPath"] = None

    if agent_files:
        result["agent"] = True
        agent_tops = Counter(
            "/".join(p.split("/")[:-1])
            for p in agent_files
        )
        # Find shortest dominant prefix
        top1 = agent_tops.most_common(1)[0][0]
        if top1.startswith(".claude/agents"):
            result["agentsPath"] = ".claude/agents"
        elif top1.startswith("agents"):
            result["agentsPath"] = "agents"
        else:
            result["agentsPath"] = top1

    if not (result["skill"] or result["plugin"] or result["agent"]):
        return None
    return result


def make_description(repo: dict, kind: str) -> str:
    """Build a description string for a plugin entry."""
    desc = (repo.get("description") or "").strip()
    if not desc:
        desc = f"{repo['full_name']} — {kind} marketplace."
    # Trim very long descriptions; keep <= 220 chars to stay readable.
    if len(desc) > 220:
        desc = desc[:217].rstrip() + "..."
    return desc


# ---------------------------------------------------------------------------
# IO


def load_json(path: pathlib.Path) -> dict:
    with path.open() as f:
        return json.load(f)


def write_json(path: pathlib.Path, data: dict, indent: int) -> None:
    with path.open("w") as f:
        json.dump(data, f, indent=indent)
        f.write("\n")


def load_denylist() -> set[str]:
    if not DENYLIST_FILE.exists():
        return set()
    out: set[str] = set()
    for line in DENYLIST_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


# ---------------------------------------------------------------------------
# Schema validation (uses repo's own validator if available)


def validate_with_repo_script(file_name: str, key: str, entry: dict) -> tuple[list[str], list[str]]:
    """Call .github/scripts/review_pr_config.py's validate_entry_schema if available."""
    import importlib.util

    script = REPO_ROOT / ".github" / "scripts" / "review_pr_config.py"
    if not script.exists():
        return [], []
    spec = importlib.util.spec_from_file_location("_review", script)
    if spec is None or spec.loader is None:
        return [], []
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate_entry_schema(file_name, key, entry)


# ---------------------------------------------------------------------------
# Main pipeline


def discover() -> dict:
    """Run search + verification and return a dict of pending additions per file."""
    print(f"[discover] token={'set' if TOKEN else 'anonymous'} "
          f"limit_per_query={LIMIT_PER_QUERY} max_new={MAX_NEW}")

    agent_cfg = load_json(AGENT_FILE)
    skill_cfg = load_json(SKILL_FILE)
    plugin_cfg = load_json(PLUGIN_FILE)

    # Lowercase set of every owner/name already tracked anywhere -> avoid re-evaluating.
    present_any: set[str] = set()
    for cfg in (agent_cfg, skill_cfg):
        for k, v in cfg.items():
            present_any.add(f"{v['owner']}/{v['name']}".lower())
    for k, v in plugin_cfg.items():
        present_any.add(f"{v['repoOwner']}/{v['repoName']}".lower())

    present_skill = {k.lower() for k in skill_cfg}
    present_plugin = {k.lower() for k in plugin_cfg}
    present_agent = {k.lower() for k in agent_cfg}

    denylist = load_denylist()
    print(f"[discover] already-tracked={len(present_any)}  denylist={len(denylist)}")

    # 1. Search
    raw_repos: dict[str, dict] = {}
    for q in SEARCH_QUERIES:
        try:
            for r in search_repos(q, LIMIT_PER_QUERY):
                raw_repos.setdefault(r["full_name"], r)
        except Exception as exc:                                       # pragma: no cover
            print(f"[discover] WARN: search '{q}' failed: {exc}")
    print(f"[discover] unique search hits: {len(raw_repos)}")

    # 2. Cheap filter
    candidates = [
        r for full, r in raw_repos.items()
        if full.lower() not in present_any
        and full.lower() not in denylist
        and passes_quality_bar(r)
    ]
    print(f"[discover] candidates after cheap filter: {len(candidates)}")

    # 3. Probe trees (parallel)
    def probe(repo: dict) -> tuple[dict, list[str] | None]:
        branch = repo.get("default_branch") or "main"
        try:
            return repo, fetch_tree(repo["full_name"], branch)
        except Exception as exc:
            print(f"[probe] {repo['full_name']}: {exc}")
            return repo, None

    classified: list[tuple[dict, dict]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for repo, paths in pool.map(probe, candidates):
            if not paths:
                continue
            cls = classify(repo["full_name"], paths)
            if cls:
                classified.append((repo, cls))

    print(f"[discover] classified candidates: {len(classified)}")

    # 4. Build additions, schema-validating each entry
    additions: dict[str, dict] = {
        AGENT_FILE.name: {},
        SKILL_FILE.name: {},
        PLUGIN_FILE.name: {},
    }
    rejected: list[dict] = []
    new_count = 0

    def add(file_name: str, key: str, entry: dict, present_set: set[str]) -> bool:
        nonlocal new_count
        if key.lower() in present_set:
            return False
        errs, _ = validate_with_repo_script(file_name, key, entry)
        if errs:
            rejected.append({"key": key, "file": file_name, "errors": errs})
            return False
        additions[file_name][key] = entry
        present_set.add(key.lower())
        new_count += 1
        return True

    # Sort: prefer higher-star repos so the MAX_NEW cap surfaces the best ones first.
    classified.sort(key=lambda rc: -(rc[0].get("stargazers_count") or 0))

    for repo, cls in classified:
        if new_count >= MAX_NEW:
            break
        full = repo["full_name"]
        owner, name = full.split("/", 1)
        branch = repo.get("default_branch") or "main"

        if cls["skill"]:
            entry: dict[str, Any] = {"owner": owner, "name": name, "branch": branch}
            if cls["skillsPath"]:
                entry["skillsPath"] = cls["skillsPath"]
            entry["enabled"] = True
            add(SKILL_FILE.name, full, entry, present_skill)

        if cls["plugin"]:
            add(
                PLUGIN_FILE.name,
                full,
                {
                    "name": full,
                    "description": make_description(repo, "plugin"),
                    "enabled": True,
                    "type": "marketplace",
                    "repoOwner": owner,
                    "repoName": name,
                    "repoBranch": branch,
                },
                present_plugin,
            )

        if cls["agent"]:
            entry = {"owner": owner, "name": name, "branch": branch}
            if cls["agentsPath"]:
                entry["agentsPath"] = cls["agentsPath"]
            entry["enabled"] = True
            add(AGENT_FILE.name, full, entry, present_agent)

    return {
        "agent_cfg": agent_cfg,
        "skill_cfg": skill_cfg,
        "plugin_cfg": plugin_cfg,
        "additions": additions,
        "rejected": rejected,
        "examined": len(candidates),
        "matched": len(classified),
    }


def merge_and_write(state: dict, apply_: bool) -> dict:
    additions = state["additions"]
    summary = {fn: list(items.keys()) for fn, items in additions.items()}
    if not apply_:
        return summary

    for cfg, file, indent in [
        (state["agent_cfg"], AGENT_FILE, 2),
        (state["skill_cfg"], SKILL_FILE, 4),
        (state["plugin_cfg"], PLUGIN_FILE, 2),
    ]:
        cfg.update(additions[file.name])
        write_json(file, cfg, indent=indent)
    return summary


# ---------------------------------------------------------------------------
# Git helpers


def _git(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=check,
        capture_output=capture,
        text=True,
    )


def is_working_tree_clean() -> bool:
    out = _git("status", "--porcelain", capture=True).stdout
    # Ignore changes to our own report file
    dirty = [
        line for line in out.splitlines()
        if line and pathlib.Path(line[3:]).name not in {REPORT_FILE.name}
    ]
    return not dirty


def commit_and_push(summary: dict) -> bool:
    files_to_add = [AGENT_FILE.name, SKILL_FILE.name, PLUGIN_FILE.name]
    diff = _git("diff", "--name-only", "--", *files_to_add, capture=True).stdout.strip()
    if not diff:
        print("[git] no JSON changes to commit.")
        return False

    parts = []
    for fn, keys in summary.items():
        if keys:
            parts.append(f"  - {fn}: +{len(keys)} ({', '.join(keys[:5])}{'…' if len(keys) > 5 else ''})")
    body = "\n".join(parts) or "(no additions)"

    _git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "discover-bot"))
    _git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "discover-bot@users.noreply.github.com"))
    _git("add", *files_to_add)
    msg = f"chore(discover): auto-add {sum(len(v) for v in summary.values())} repos\n\n{body}\n"
    _git("commit", "-m", msg)
    print("[git] commit created.")
    return True


def push() -> None:
    _git("push", "origin", "HEAD:main")
    print("[git] pushed to origin/main.")


# ---------------------------------------------------------------------------
# Entrypoint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to JSON files (otherwise dry-run).")
    parser.add_argument("--push", action="store_true",
                        help="git commit + push to origin/main (implies --apply).")
    parser.add_argument("--report", type=pathlib.Path, default=REPORT_FILE,
                        help="Where to write JSON summary of the run.")
    args = parser.parse_args()

    apply_ = args.apply or args.push

    if args.push:
        if not is_working_tree_clean():
            print("[git] working tree dirty — refusing to run in push mode.", file=sys.stderr)
            return 2
        # Make sure we're up to date before adding, so we don't overwrite peers.
        try:
            _git("fetch", "origin", "main")
            _git("merge", "--ff-only", "origin/main")
        except subprocess.CalledProcessError as exc:
            print(f"[git] cannot fast-forward from origin/main: {exc}", file=sys.stderr)
            return 2

    state = discover()
    summary = merge_and_write(state, apply_=apply_)

    total_new = sum(len(v) for v in summary.values())
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "examined": state["examined"],
        "matched": state["matched"],
        "new_total": total_new,
        "new_per_file": {fn: keys for fn, keys in summary.items()},
        "rejected": state["rejected"],
        "applied": apply_,
        "pushed": False,
    }

    print(f"\n=== Discovery summary ===")
    print(f"examined: {state['examined']}, matched: {state['matched']}, new: {total_new}")
    for fn, keys in summary.items():
        print(f"  {fn}: +{len(keys)}")
        for k in keys[:10]:
            print(f"      + {k}")
        if len(keys) > 10:
            print(f"      … (+{len(keys) - 10} more)")
    if state["rejected"]:
        print(f"  rejected: {len(state['rejected'])} (see report)")

    pushed = False
    if args.push and total_new > 0:
        if commit_and_push(summary):
            push()
            pushed = True
    report["pushed"] = pushed

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"[report] wrote {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
