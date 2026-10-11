#!/usr/bin/env python3
"""Daily link check for Buffer video posts. NO LLM, Python standard library only, READ-ONLY (changes nothing in Buffer).

Env: BUFFER_API_KEY, BUFFER_ORG_ID, BUFFER_CHANNEL_ID_X   (GitHub repository secrets)
Queries Buffer GraphQL for posts with status scheduled / sending / error (+ needs_approval) and checks every video asset URL:
HEAD and Range-GET bytes 0-99 -> HTTP 200/206, plausible Content-Length, content-type video/mp4.
Posts with status error are always reported.
Output: 'OK n Posts geprueft' (exit 0) or one line per problem with post id, dueAt, URL, HTTP status (exit 1).
Never prints the API key, post text or response bodies.
Options: --extra-url URL (repeatable, extra URL to check, for tests), --skip-buffer (only check --extra-url, no Buffer call).
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

API = "https://api.buffer.com"
UA = "curl/8.14.1"
FIELDS = "id dueAt status error{message} assets{type source}"
MIN_SIZE = 100 * 1024            # smaller than 100 KB is not a real clip
MAX_SIZE = 512 * 1024 * 1024
STATUSES = (["scheduled", "sending", "error"], ["needs_approval"])


def gql(key, query, variables=None, tries=3):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    err = ""
    for i in range(tries):
        req = urllib.request.Request(API, data=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": UA})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if d.get("errors"):
                msgs = "; ".join(str(e.get("message", ""))[:100] for e in d["errors"][:3])
                raise RuntimeError("Buffer GraphQL error: " + msgs)
            return d["data"]
        except urllib.error.HTTPError as e:
            err = "Buffer HTTP %s" % e.code
            if e.code in (400, 401, 403):
                break
        except RuntimeError:
            raise
        except Exception as e:
            err = "Buffer not reachable: %s" % type(e).__name__
        time.sleep(5 * (i + 1))
    raise RuntimeError(err)


def fetch(org, chan, key, statuses):
    q = ('query($after:String){posts(first:100, after:$after, input:{organizationId:"%s",'
         'filter:{channelIds:["%s"],status:[%s]},sort:[{field:dueAt,direction:asc}]})'
         '{pageInfo{hasNextPage endCursor} edges{node{%s}}}}') % (org, chan, ",".join(statuses), FIELDS)
    out, after = [], None
    while True:
        p = gql(key, q, {"after": after})["posts"]
        out += [e["node"] for e in p["edges"]]
        if not p["pageInfo"]["hasNextPage"]:
            return out
        after = p["pageInfo"]["endCursor"]


def request(url, method, rng=None):
    """Returns (status, headers dict lower-case, first bytes count, error). Follows redirects (urllib default)."""
    h = {"User-Agent": UA}
    if rng:
        h["Range"] = rng
    req = urllib.request.Request(url, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, 0, ""
    except Exception as e:
        return 0, {}, 0, type(e).__name__
    try:
        n = len(r.read(100)) if method == "GET" else 0
    except Exception as e:
        return r.status, {}, 0, type(e).__name__
    finally:
        r.close()
    return r.status, {k.lower(): v for k, v in r.headers.items()}, n, ""


def total_size(status, h):
    cr = h.get("content-range", "")
    if status == 206 and "/" in cr and cr.rsplit("/", 1)[1].isdigit():
        return int(cr.rsplit("/", 1)[1])
    if status == 200 and h.get("content-length", "").isdigit():
        return int(h["content-length"])
    return None


def check_url(url, retries=2):
    """Returns '' when fine, else a short problem text."""
    prob = ""
    for i in range(retries + 1):
        hs, hh, _, he = request(url, "HEAD")
        gs, gh, gn, ge = request(url, "GET", "bytes=0-99")
        prob = ""
        if hs != 200:
            prob = "HEAD HTTP %s %s" % (hs, he)
        elif gs not in (200, 206):
            prob = "GET HTTP %s %s" % (gs, ge)
        elif gn == 0:
            prob = "GET returned no data"
        else:
            ctype = (gh.get("content-type") or "").split(";")[0].strip().lower()
            hsize, gsize = total_size(hs, hh), total_size(gs, gh)
            if ctype != "video/mp4":
                prob = "content-type %r instead of video/mp4" % ctype
            elif not hsize or not (MIN_SIZE <= hsize <= MAX_SIZE):
                prob = "Content-Length implausible: %s" % hsize
            elif gsize and gsize != hsize:
                prob = "size mismatch HEAD %s vs GET %s" % (hsize, gsize)
        if not prob:
            return ""
        if hs in (404, 410, 403, 401) and i >= 1:
            break
        if i < retries:
            time.sleep(5 * (i + 1))
    return prob


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra-url", action="append", default=[], help="additional URL to check (for tests)")
    ap.add_argument("--skip-buffer", action="store_true", help="do not query Buffer, only check --extra-url")
    a = ap.parse_args()
    lines, n = [], 0
    if not a.skip_buffer:
        key, org, chan = (os.environ.get(k, "").strip() for k in ("BUFFER_API_KEY", "BUFFER_ORG_ID", "BUFFER_CHANNEL_ID_X"))
        if not (key and org and chan):
            print("PROBLEM: BUFFER_API_KEY / BUFFER_ORG_ID / BUFFER_CHANNEL_ID_X missing (repository secrets)")
            return 1
        try:
            posts = []
            for st in STATUSES:
                posts += fetch(org, chan, key, st)
        except RuntimeError as e:
            print("PROBLEM: %s" % e)
            return 1
        for p in posts:
            vids = [x for x in p["assets"] if x["type"] == "video"]
            if not vids and p["status"] != "error":
                continue
            n += 1
            tag = "Post %s dueAt=%s status=%s" % (p["id"], p.get("dueAt") or "?", p["status"])
            if p["status"] == "error":
                lines.append("ERROR %s: %s" % (tag, ((p.get("error") or {}).get("message") or "no error text")[:200]))
            for v in vids:
                prob = check_url(v["source"])
                if prob:
                    lines.append("LINK PROBLEM %s url=%s: %s" % (tag, v["source"], prob))
    for u in a.extra_url:
        n += 1
        prob = check_url(u, retries=0)
        if prob:
            lines.append("LINK PROBLEM extra url=%s: %s" % (u, prob))
    if not lines:
        print("OK %d Posts geprueft" % n)
        return 0
    print("%d Posts geprueft, %d problems" % (n, len(lines)))
    print("\n".join(lines))
    return 1


if __name__ == "__main__":
    sys.exit(main())
