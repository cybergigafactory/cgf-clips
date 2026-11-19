#!/usr/bin/env python3
"""Weekly cleanup of unused GitHub Pages clips in this repo. NO LLM, Python standard library only.

Reads pending Buffer posts (scheduled / sending / error / needs_approval) and deletes every
tracked .mp4 that is (a) not referenced by any of those posts and (b) last touched more than
MIN_AGE_DAYS days ago. Never prints secrets, post text or API response bodies.

Safety: if the Buffer read fails, or it returns zero pending posts while the repo has clips
from the last RECENT_CLIP_DAYS days, abort with exit 1 and delete nothing.

Env: BUFFER_API_KEY, BUFFER_ORG_ID, BUFFER_CHANNEL_ID_X
     GITHUB_TOKEN / GITHUB_REPOSITORY (optional, used only to print blob sizes via the Git API)
     GITHUB_ACTIONS=true required for commit+push, unless --commit is passed.

Options: --dry-run (list only), --commit (allow commit+push outside Actions),
         --repo DIR (git root, default: cwd / GITHUB_WORKSPACE).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import check_links as linkcheck  # noqa: E402

MIN_AGE_DAYS = 7
RECENT_CLIP_DAYS = 14
PAGES_HOST = "cybergigafactory.github.io"
PAGES_PREFIX = "cgf-clips/"
RAW_HOSTS = {"raw.githubusercontent.com"}
REPO_SLUG_PREFIX = "cybergigafactory/cgf-clips/"
COMMIT_NAME = "github-actions[bot]"
COMMIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def die(msg, code=1):
    print("ABORT: " + msg)
    return code


def git(args, cwd, check=True, env=None):
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:300]
        raise RuntimeError("git %s failed: %s" % (" ".join(args[:6]), err or "exit %d" % r.returncode))
    return r


def fmt_bytes(n):
    if n is None:
        return "unknown"
    n = float(n)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div or unit == "KB":
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def url_to_repo_path(url):
    """Map a public cgf-clips URL to a repo-relative path, or None if it is not this host."""
    if not url:
        return None
    try:
        u = urllib.parse.urlparse(url.strip())
    except Exception:
        return None
    host = (u.netloc or "").lower()
    path = urllib.parse.unquote(u.path or "").lstrip("/")
    if host == PAGES_HOST:
        if path.startswith(PAGES_PREFIX):
            rel = path[len(PAGES_PREFIX):]
            return rel or None
        return None
    if host in RAW_HOSTS:
        if not path.startswith(REPO_SLUG_PREFIX):
            return None
        rest = path[len(REPO_SLUG_PREFIX):]
        for prefix in ("main/", "refs/heads/main/"):
            if rest.startswith(prefix):
                return rest[len(prefix):] or None
        return None
    if host in {"github.com", "www.github.com"}:
        # /cybergigafactory/cgf-clips/raw/main/<file> or /blob/main/<file>
        parts = path.split("/")
        if len(parts) >= 5 and parts[0] == "cybergigafactory" and parts[1] == "cgf-clips" and parts[2] in {"raw", "blob"} and parts[3] == "main":
            return "/".join(parts[4:]) or None
        return None
    return None


def looks_like_our_host(url):
    """True if the URL is clearly on this Pages/repo host (path-bounded, so typo prefixes do not match)."""
    if not url:
        return False
    try:
        u = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    host = (u.netloc or "").lower()
    path = urllib.parse.unquote(u.path or "")
    if host == PAGES_HOST:
        return path == "/cgf-clips" or path.startswith("/cgf-clips/")
    if host in RAW_HOSTS or host in {"github.com", "www.github.com"}:
        return path.startswith("/cybergigafactory/cgf-clips/")
    return False


def list_mp4s(repo, ref="HEAD"):
    r = git(["ls-tree", "-r", "--name-only", ref], cwd=repo)
    return [p for p in r.stdout.splitlines() if p.lower().endswith(".mp4")]


def last_commit_times(repo, paths, ref="HEAD"):
    """Most recent commit timestamp (unix seconds) touching each path, without git log -p."""
    if not paths:
        return {}
    r = git(["log", ref, "--format=COMMIT %ct", "--name-only", "--", *paths], cwd=repo)
    times = {}
    ts = None
    for line in r.stdout.splitlines():
        if line.startswith("COMMIT "):
            try:
                ts = int(line.split()[1])
            except (IndexError, ValueError):
                ts = None
            continue
        p = line.strip()
        if ts is not None and p and p not in times:
            times[p] = ts
    return times


def age_days(ts, now):
    return (now - ts) / 86400.0


def github_blob_sizes(repo_slug, tree_sha, token):
    """Return {path: size} for blobs via the Git Trees API (includes size, no file content)."""
    if not (repo_slug and tree_sha and token):
        return {}
    url = "https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (
        urllib.parse.quote(repo_slug), urllib.parse.quote(tree_sha))
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "cgf-clips-cleanup",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print("note: could not fetch blob sizes (%s)" % type(e).__name__)
        return {}
    if data.get("truncated"):
        print("note: Git tree listing was truncated; sizes may be incomplete")
    out = {}
    for entry in data.get("tree") or []:
        if entry.get("type") == "blob" and "size" in entry and entry.get("path"):
            out[entry["path"]] = int(entry["size"])
    return out


def repo_slug_from(repo, env):
    slug = (env.get("GITHUB_REPOSITORY") or "").strip()
    if slug:
        return slug
    try:
        url = git(["remote", "get-url", "origin"], cwd=repo).stdout.strip()
    except RuntimeError:
        return ""
    url = url.removesuffix(".git")
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1]
    if "github.com/" in url:
        return url.split("github.com/", 1)[1].lstrip("/")
    return ""


def fetch_pending_posts(key, org, chan):
    posts = []
    for st in linkcheck.STATUSES:
        posts += linkcheck.fetch(org, chan, key, st)
    return posts


def collect_referenced_paths(posts):
    """Return (paths set, problem or ''). Abort-worthy if a cgf-clips URL cannot be mapped."""
    paths = set()
    for p in posts:
        for asset in p.get("assets") or []:
            if (asset.get("type") or "").lower() != "video":
                continue
            src = asset.get("source") or ""
            rel = url_to_repo_path(src)
            if rel:
                paths.add(rel)
            elif looks_like_our_host(src):
                return set(), "pending post %s has a cgf-clips video URL that could not be mapped: %s" % (
                    p.get("id") or "?", src)
    return paths, ""


def classify(mp4s, referenced, times, now, min_age=MIN_AGE_DAYS):
    kept, delete = [], []
    for path in mp4s:
        ts = times.get(path)
        age = age_days(ts, now) if ts is not None else None
        if path in referenced:
            kept.append((path, "referenced by a pending Buffer post", ts, age))
        elif ts is None:
            # Missing history in a shallow clone: keep, do not guess.
            kept.append((path, "kept (no commit date found)", ts, age))
        elif age < min_age:
            kept.append((path, "younger than %d days" % min_age, ts, age))
        else:
            delete.append((path, "unreferenced and older than %d days" % min_age, ts, age))
    return kept, delete


def print_report(mp4s, kept, delete, sizes, now, pending_n, ref_n, dry_run):
    size_before = sum(sizes.get(p, 0) for p in mp4s) if sizes else None
    size_after = sum(sizes.get(p, 0) for p, *_ in kept) if sizes else None
    if sizes and any(p not in sizes for p in mp4s):
        # Partial sizes: still print what we know, labelled as such.
        missing = sum(1 for p in mp4s if p not in sizes)
        print("mp4 in repo: %d, size before: %s (%d files without a size)" % (
            len(mp4s), fmt_bytes(size_before), missing))
    else:
        print("mp4 in repo: %d, size before: %s" % (len(mp4s), fmt_bytes(size_before)))
    print("Pending Buffer posts: %d" % pending_n)
    print("Referenced cgf-clips paths: %d" % ref_n)
    print("Kept (%d):" % len(kept))
    for path, reason, ts, age in kept:
        age_s = "%.1fd" % age if age is not None else "age unknown"
        print("  KEEP  %s  (%s, %s, %s)" % (path, reason, age_s, fmt_bytes(sizes.get(path))))
    print("Would delete (%d):" % len(delete) if dry_run else "Delete (%d):" % len(delete))
    for path, reason, ts, age in delete:
        age_s = "%.1fd" % age if age is not None else "age unknown"
        when = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"
        print("  DEL   %s  (%s, %s, last=%s, %s)" % (path, reason, age_s, when, fmt_bytes(sizes.get(path))))
    print("Size after: %s" % fmt_bytes(size_after))


def prepare_full_index(repo, ref):
    """Load the full tree of ref into the index without checking out blobs or disabling sparse checkout via git."""
    # Turning sparseCheckout off in config avoids a sparse-index write-tree that would drop
    # files outside the cone. read-tree without -u does not populate the worktree.
    git(["config", "core.sparseCheckout", "false"], cwd=repo)
    git(["config", "index.sparse", "false"], cwd=repo)
    git(["read-tree", ref], cwd=repo)


def restore_sparse_config(repo):
    # Re-enable the repo's usual sparse-checkout setting for the worktree; do not run
    # `git sparse-checkout disable` (that would fetch videos into the worktree).
    git(["config", "core.sparseCheckout", "true"], cwd=repo, check=False)


def verify_tree(repo, tree, expected_remaining, deleted):
    listed = git(["ls-tree", "-r", "--name-only", tree], cwd=repo).stdout.splitlines()
    remaining_mp4 = {p for p in listed if p.lower().endswith(".mp4")}
    if remaining_mp4 != set(expected_remaining):
        extra = remaining_mp4 - set(expected_remaining)
        missing = set(expected_remaining) - remaining_mp4
        raise RuntimeError("new tree mp4 set mismatch extra=%s missing=%s" % (
            sorted(extra)[:5], sorted(missing)[:5]))
    for p in deleted:
        if p in listed:
            raise RuntimeError("deleted path still in new tree: %s" % p)
    return listed


def commit_and_push(repo, base, deletions, message):
    import subprocess
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = COMMIT_NAME
    env["GIT_AUTHOR_EMAIL"] = COMMIT_EMAIL
    env["GIT_COMMITTER_NAME"] = COMMIT_NAME
    env["GIT_COMMITTER_EMAIL"] = COMMIT_EMAIL
    try:
        prepare_full_index(repo, base)
        for path in deletions:
            git(["update-index", "--force-remove", "--", path], cwd=repo)
        tree = git(["write-tree"], cwd=repo).stdout.strip()
        parent_files = git(["ls-tree", "-r", "--name-only", base], cwd=repo).stdout.splitlines()
        expected = [p for p in parent_files if p not in deletions]
        verify_tree(repo, tree, [p for p in expected if p.lower().endswith(".mp4")], deletions)
        # Also: the new tree must be exactly parent files minus deletions (no other losses).
        new_files = git(["ls-tree", "-r", "--name-only", tree], cwd=repo).stdout.splitlines()
        if set(new_files) != set(expected):
            raise RuntimeError("new tree is not parent minus the intended deletions (would touch other files)")
        commit = git(["commit-tree", tree, "-p", base, "-m", message], cwd=repo, env=env).stdout.strip()
        git(["update-ref", "refs/heads/main", commit], cwd=repo)
        push = git(["push", "origin", "main"], cwd=repo, check=False)
        if push.returncode != 0:
            err = (push.stderr or push.stdout or "").strip().replace("\n", " ")[:300]
            # Did it land anyway?
            git(["fetch", "origin", "main"], cwd=repo)
            landed = git(["merge-base", "--is-ancestor", commit, "origin/main"], cwd=repo, check=False)
            if landed.returncode == 0:
                print("push reported an error but commit landed on origin/main")
                return commit
            raise RuntimeError("git push failed: %s" % err)
        return commit
    finally:
        restore_sparse_config(repo)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekly unused-clip cleanup for cgf-clips")
    ap.add_argument("--dry-run", action="store_true", help="list keep/delete, do not commit or push")
    ap.add_argument("--commit", action="store_true", help="allow commit+push outside GitHub Actions")
    ap.add_argument("--repo", default="", help="git repository root")
    a = ap.parse_args(argv)

    repo = a.repo or os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    repo = str(Path(repo).resolve())
    if not Path(repo, ".git").exists() and not Path(repo, ".git").is_file():
        # In worktrees / some checkouts .git may be a file; exists covers both. If missing, fail.
        if not Path(repo, ".git").exists():
            print("ABORT: %s is not a git repository" % repo)
            return 1

    now = time.time()
    dry_run = a.dry_run
    can_write = (not dry_run) and (a.commit or os.environ.get("GITHUB_ACTIONS") == "true")
    if not dry_run and not can_write:
        print("ABORT: refusing to commit/push outside GitHub Actions (pass --commit to override)")
        return 1

    key, org, chan = (os.environ.get(k, "").strip() for k in
                      ("BUFFER_API_KEY", "BUFFER_ORG_ID", "BUFFER_CHANNEL_ID_X"))
    if not (key and org and chan):
        return die("BUFFER_API_KEY / BUFFER_ORG_ID / BUFFER_CHANNEL_ID_X missing (repository secrets)")

    # Fetch latest main so dates and the file list match origin (read-only on the worktree).
    fetch = git(["fetch", "origin", "main"], cwd=repo, check=False)
    if fetch.returncode != 0:
        print("note: git fetch origin main failed, using local refs")
    try:
        head = git(["rev-parse", "origin/main"], cwd=repo).stdout.strip()
    except RuntimeError:
        head = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    print("Repo: %s" % (repo_slug_from(repo, os.environ) or repo))
    print("HEAD: %s" % head[:12])
    print("Mode: %s" % ("dry-run" if dry_run else "live"))

    try:
        posts = fetch_pending_posts(key, org, chan)
    except RuntimeError as e:
        return die("Buffer read failed: %s" % e)

    referenced, map_err = collect_referenced_paths(posts)
    if map_err:
        return die(map_err)

    mp4s = sorted(list_mp4s(repo, head))
    times = last_commit_times(repo, mp4s, head)

    recent = [p for p in mp4s if p in times and age_days(times[p], now) <= RECENT_CLIP_DAYS]
    if len(posts) == 0 and recent:
        return die("Buffer returned 0 pending posts, but the repo has %d clip(s) from the last %d days"
                   % (len(recent), RECENT_CLIP_DAYS))

    kept, delete = classify(mp4s, referenced, times, now)

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    sizes = github_blob_sizes(repo_slug_from(repo, os.environ), head, token)
    print_report(mp4s, kept, delete, sizes, now, len(posts), len(referenced), dry_run)

    if dry_run:
        print("DRY RUN: no changes")
        return 0
    if not delete:
        print("Nothing to delete")
        return 0

    # Re-fetch just before committing so we do not overwrite a concurrent push.
    # Retry a few times if origin/main moves (no force push).
    last_err = None
    for attempt in range(1, 4):
        git(["fetch", "origin", "main"], cwd=repo)
        base = git(["rev-parse", "origin/main"], cwd=repo).stdout.strip()
        current_mp4s = set(list_mp4s(repo, base))
        wanted = [p for p, *_ in delete if p in current_mp4s]
        # If new unreferenced old files appeared, do not expand the set mid-run; leftover files wait a week.
        # If a file we planned to delete vanished, skip it.
        disappeared = [p for p, *_ in delete if p not in current_mp4s]
        if disappeared:
            print("note: already gone on origin/main: %s" % ", ".join(disappeared))
        if not wanted:
            print("Nothing to delete (files already gone on origin/main)")
            return 0
        # Safety: never delete a path that is now referenced? Re-check classify against current tree
        # only for the original delete set (Buffer data is from this run).
        still_ref = [p for p in wanted if p in referenced]
        if still_ref:
            return die("refusing to delete paths still referenced: %s" % ", ".join(still_ref))
        message = "weekly cleanup: removed %d clips" % len(wanted)
        try:
            sha = commit_and_push(repo, base, wanted, message)
            print("Committed %s %s" % (sha[:12], message))
            print("Pushed to origin/main")
            return 0
        except RuntimeError as e:
            last_err = e
            print("note: attempt %d failed (%s), retrying without force-push" % (attempt, e))
            time.sleep(3 * attempt)
    return die("commit/push failed after retries: %s" % last_err)


if __name__ == "__main__":
    sys.exit(main())
