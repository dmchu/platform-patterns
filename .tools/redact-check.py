#!/usr/bin/env python3
"""Refuse to ship anything carrying employer-identifying detail.

Run over the repo before every push, and over every post draft before publishing.
Two profiles:
  --profile repo    for the portfolio repo (a public subset is extracted from it later)
  --profile post    for public writing: everything above, plus the vendor-naming rule

Exit 1 on any hit. Print the file, line and the rule that fired -- never the surrounding text,
so the report itself is safe to paste.
"""
import argparse, json, os, re, sys

# Terms live in a LOCAL, GITIGNORED file -- never in this source.
#
# The reason is the whole point of the tool: a rule list naming your employer, its partners,
# its services and your colleagues is itself the disclosure it exists to prevent. Baking the
# terms in would publish a tidy machine-readable inventory of exactly what must not be public.
#
# Ship: redact-check.py + terms.example.json (generic placeholders).
# Keep:  terms.json (your real terms), gitignored.

DEFAULT_TERMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terms.json")


def load_rules(path, profile):
    if not os.path.exists(path):
        sys.exit(
            f"  no term file at {path}\n"
            f"  copy terms.example.json to terms.json and fill in your own terms.\n"
            f"  terms.json is gitignored and must stay that way."
        )
    spec = json.load(open(path))
    rules = [(r["name"], r["pattern"], r["note"]) for r in spec.get("always", [])]
    if profile == "post":
        rules += [(r["name"], r["pattern"], r["note"]) for r in spec.get("post_only", [])]
    return rules



# Deliberately does NOT skip .tools: an earlier version did, and that is exactly where a
# raw research dump full of internal detail ended up. The only exclusion is this file,
# which necessarily contains the very patterns it searches for.
SKIP_DIRS = {".git", "node_modules", ".venv", ".venv-md", "lib", "target", "__pycache__"}
SELF = os.path.basename(__file__)
TEXT_EXT = {".md", ".py", ".ts", ".js", ".yaml", ".yml", ".json", ".xml", ".txt", ".sh", ".toml", ".mmd", ""}


def scan(paths, rules):
    hits = []
    for root in paths:
        if os.path.isfile(root):
            files = [root]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for f in filenames:
                    if f == SELF:
                        continue
                    if os.path.splitext(f)[1].lower() in TEXT_EXT:
                        files.append(os.path.join(dirpath, f))
        for fp in files:
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        # report EVERY rule that fires on a line, not just the first --
                        # a redaction report you have to run three times is a trap.
                        for name, pat, note in rules:
                            if re.search(pat, line, re.I):
                                hits.append((fp, i, name, note))
            except OSError:
                continue
    return hits


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--profile", choices=["repo", "post"], default="repo")
    ap.add_argument("--terms", default=DEFAULT_TERMS,
                    help="JSON term file (default: .tools/terms.json, gitignored)")
    a = ap.parse_args()
    rules = load_rules(a.terms, a.profile)
    hits = scan(a.paths, rules)
    if not hits:
        print(f"  clean ({a.profile} profile, {len(rules)} rules)")
        sys.exit(0)
    print(f"  {len(hits)} hit(s), {a.profile} profile:\n")
    for fp, ln, name, note in hits:
        print(f"    {fp}:{ln}  [{name}] {note}")
    print("\n  Nothing ships until these are zero.")
    sys.exit(1)
