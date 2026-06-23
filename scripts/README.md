# Auto-discovery

`scripts/discover_repos.py` scrapes GitHub for new Claude Code skills, plugins,
and agent repos, verifies each is structurally legitimate, and merges new entries
into the three JSON config files.

## Pipeline

1. **Search** — runs a fixed set of `topic:` queries against the GitHub search API.
2. **Cheap filter** — drops forks, archived/disabled repos, empty repos, repos
   with no stars *and* no push in the last year, and repos matching spam
   heuristics (junk owner names, spammy description tokens).
3. **Tree probe** — fetches the recursive git tree for surviving candidates
   (parallel, 8 workers).
4. **Classify** — looks for structural markers:
   - `SKILL.md` files → skills repo (with `skillsPath` inferred when ≥60 % of
     skills share a single dominant root, otherwise omitted so consumers walk
     the whole tree)
   - `.claude-plugin/marketplace.json` → plugin marketplace
   - `agents/**/*.md` or `.claude/agents/**/*.md` → agents repo
5. **Schema-validate** — every prospective entry is checked against
   `.github/scripts/review_pr_config.py::validate_entry_schema` (the same
   validator used in PR review).
6. **Merge & write** — passing entries are appended to the relevant JSON file,
   formatted with the indent each file uses.
7. **(optional) Commit & push** — with `--push`, the script fast-forwards from
   `origin/main` first, then commits and pushes the changes.

## Usage

```bash
# Dry-run (default): print what would change, don't touch files
python3 scripts/discover_repos.py

# Apply changes locally
python3 scripts/discover_repos.py --apply

# Apply + commit + push to origin/main (used by automation)
python3 scripts/discover_repos.py --apply --push
```

## Environment

| Var | Default | Meaning |
|---|---|---|
| `GITHUB_TOKEN` / `GH_TOKEN` | none | Auth for GitHub API. Required for push, strongly recommended for the search step (5000 req/h vs 60). |
| `DISCOVER_LIMIT_PER_QUERY` | `50` | How many search results to pull per query. |
| `DISCOVER_MAX_NEW` | `30` | Hard cap on new entries per run. Prevents one bad run from flooding the configs. |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | `discover-bot` | Identity used when committing. |

## Denylist

`scripts/discovery_denylist.txt` is a plain-text list of `owner/name` entries
that should never be auto-added (one per line, `#` for comments). Use it for
spam, abandoned repos, or anything misclassified.

## Output

Each run writes `scripts/discovery_last_report.json` containing:

- `timestamp` — UTC ISO timestamp of the run
- `examined`, `matched`, `new_total`
- `new_per_file` — list of keys added to each file
- `rejected` — entries that failed schema validation
- `applied`, `pushed` — booleans reflecting flags / outcome
