"""GitHub data, fetched with gh in the background and cached on disk: PRs, CI and reviews."""

import datetime
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import dash_common
from dash_common import BROADCAST_TEAMS, CACHE_DIR, EXEC, GH_TTL, MERGED_DAYS, MERGED_MAX, ORGS, log_error


def pr_url(repo, number):
    return f"https://github.com/{repo}/pull/{number}"


def latest_runs(checks):
    """Each check once, at its latest run: a re-run leaves the earlier attempts in the list, and GitHub
    only shows (and counts) the last one, so a check that failed and then passed is green."""
    latest = {}
    for c in checks or []:
        key = (c.get("workflowName") or "", c.get("name") or c.get("context") or "")
        started = c.get("startedAt") or ""
        when = "9999" if not started or started.startswith("0001") else started  # queued: the newest
        if key not in latest or when >= latest[key][0]:
            latest[key] = (when, c)
    return [c for _, c in latest.values()]


def check_counts(checks):
    """How many checks failed or are still going, and their names (without a leading emoji)."""
    checks = latest_runs(checks)
    failed = [
        c
        for c in checks or []
        if c.get("conclusion") in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")
        or c.get("state") in ("FAILURE", "ERROR")
    ]
    pending = [
        c
        for c in checks or []
        if c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED")
        or c.get("state") in ("PENDING", "EXPECTED")
    ]

    def names(cs):
        return sorted({re.sub(r"^\W+", "", c.get("name") or c.get("context") or "") for c in cs} - {""})

    return {"failed": len(failed), "pending": len(pending), "failed_names": names(failed), "pending_names": names(pending)}


PR_EXTRA_FIELDS = """
  isInMergeQueue
  mergeQueueEntry { position state estimatedTimeToMerge }
  timelineItems(last: 1, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) {
    nodes { __typename ... on RemovedFromMergeQueueEvent { reason } }
  }
  reviewThreads(first: 100) { nodes { isResolved } }
  latestOpinionatedReviews(first: 30) { nodes { state author { login } } }
"""


def parse_extras(pr):
    """From a pull request queried with PR_EXTRA_FIELDS: its merge queue state, unresolved review
    threads, and the approvals and change requests standing (each reviewer's latest)."""
    entry = pr.get("mergeQueueEntry") or {}
    last = (((pr.get("timelineItems") or {}).get("nodes")) or [{}])[-1] or {}
    opinions = ((pr.get("latestOpinionatedReviews") or {}).get("nodes")) or []
    return {
        "queue": {
            "in_queue": bool(pr.get("isInMergeQueue")),
            "position": entry.get("position"),
            "state": entry.get("state") or "",
            "eta": entry.get("estimatedTimeToMerge"),
            # why it last left the queue, if that's the latest queue event
            "removed": last.get("reason") if last.get("__typename") == "RemovedFromMergeQueueEvent" else None,
        },
        "unresolved": sum(1 for t in ((pr.get("reviewThreads") or {}).get("nodes")) or [] if not t.get("isResolved")),
        "approvals": sum(1 for o in opinions if o.get("state") == "APPROVED"),
        "changes_by": [(o.get("author") or {}).get("login") or "?" for o in opinions if o.get("state") == "CHANGES_REQUESTED"],
    }


def pr_extras(repo, numbers):
    """parse_extras() for each of the repo's PR numbers ({} if gh failed); gh's --json doesn't have them."""
    if not numbers:
        return {}
    owner, name = repo.split("/", 1)
    body = " ".join(f"pr{n}: pullRequest(number: {n}) {{ {PR_EXTRA_FIELDS} }}" for n in numbers)
    d = gh_json("api", "graphql", "-f", f'query=query {{ repository(owner: "{owner}", name: "{name}") {{ {body} }} }}')
    repo = ((d or {}).get("data") or {}).get("repository") or {}
    return {n: parse_extras(repo[f"pr{n}"]) for n in numbers if repo.get(f"pr{n}")}


def iso_time(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def gh_json(*args):
    """gh's JSON output, or None if gh failed."""
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
        return safe_api_data(json.loads(p.stdout)) if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def safe_api_data(value):
    """Remove terminal control characters from untrusted text returned by GitHub."""
    if isinstance(value, str):
        return "".join(" " if unicodedata.category(char) == "Cc" else char for char in value)
    if isinstance(value, list):
        return [safe_api_data(item) for item in value]
    if isinstance(value, dict):
        return {key: safe_api_data(item) for key, item in value.items()}
    return value


def fetch_pr(repo, branch):
    """The PR of the repo's branch, summed up; None if it has none, False if gh failed."""
    try:
        p = subprocess.run(
            ["gh", "pr", "view", branch, "-R", repo, "--json", "number,state,isDraft,reviewDecision,statusCheckRollup"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if p.returncode != 0:
        return None if "no pull requests found" in p.stderr else False
    d = safe_api_data(json.loads(p.stdout))
    return {
        "number": d["number"],
        "state": d["state"],
        "draft": d["isDraft"],
        "review": d.get("reviewDecision") or "",
        "queue": (pr_extras(repo, [d["number"]]).get(d["number"]) or {}).get("queue") if d["state"] == "OPEN" else None,
        **check_counts(d.get("statusCheckRollup")),
    }


REQUIRED_CACHE = {}  # (repo, base branch) -> (when, names of the checks its rules require)


def required_checks(repo, branch):
    """The checks a branch's rules require (the rules API needs no admin), cached for an hour."""
    cached = REQUIRED_CACHE.get((repo, branch))
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    rules = gh_json("api", f"repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}")
    if rules is None:
        return cached[1] if cached else []
    names = sorted({
        c["context"]
        for rule in rules if rule.get("type") == "required_status_checks"
        for c in (rule.get("parameters") or {}).get("required_status_checks") or [] if c.get("context")
    })
    REQUIRED_CACHE[(repo, branch)] = (time.time(), names)
    return names


MINE_QUERY = """
query($open: String!, $merged: String!) {
  open: search(query: $open, type: ISSUE, first: 30) {
    nodes { ... on PullRequest {
      number title url isDraft reviewDecision updatedAt headRefName baseRefName mergeable additions deletions
      autoMergeRequest { enabledAt } repository { nameWithOwner }
      %s
    } }
  }
  merged: search(query: $merged, type: ISSUE, first: %d) {
    nodes { ... on PullRequest {
      number title url mergedAt createdAt headRefName additions deletions repository { nameWithOwner }
    } }
  }
}
""" % (PR_EXTRA_FIELDS, MERGED_MAX)


def mine_key():
    """What your PRs are kept under: "mine" and the search's own filter, so each setting has its list."""
    return "mine" + ("" if dash_common.OWN_REPOS else " -user:@me")


def fetch_mine(key):
    """Your open PRs in any repo, then the ones merged in the last MERGED_DAYS; False if gh failed. The key's
    filter, -user:@me without OWN_REPOS, leaves out the repos you own (personal projects like your dotfiles).

    One GraphQL search brings both lists with each open PR's merge queue, reviews and threads. Then each
    open PR's checks are read in full, in its own repo, together with its base branch's required checks."""
    # to the second: a date alone would reach back to that day's midnight (UTC), up to a day more
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=MERGED_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    own = key[len("mine"):]
    d = gh_json(
        "api", "graphql", "-f", f"query={MINE_QUERY}",
        "-f", f"open=is:pr is:open author:@me{own}",
        "-f", f"merged=is:pr is:merged author:@me{own} merged:>={since}",
    )
    data = (d or {}).get("data")
    if not data:
        return False
    prs = sorted(
        (
            {
                "repo": n["repository"]["nameWithOwner"],
                "number": n["number"],
                "title": n["title"],
                "url": n["url"],
                "branch": n["headRefName"],
                "base": n.get("baseRefName") or "main",
                "state": "OPEN",
                "draft": n["isDraft"],
                "review": n.get("reviewDecision") or "",
                "conflicts": n.get("mergeable") == "CONFLICTING",
                "auto_merge": bool(n.get("autoMergeRequest")),
                "at": iso_time(n["updatedAt"]),
                "adds": n.get("additions"),
                "dels": n.get("deletions"),
                **check_counts([]),  # filled in from the full check list below
                **parse_extras(n),
            }
            for n in (data.get("open") or {}).get("nodes") or [] if n
        ),
        key=lambda p: -(p["at"] or 0),
    )
    prs += sorted(
        (
            {
                "repo": n["repository"]["nameWithOwner"],
                "number": n["number"],
                "title": n["title"],
                "url": n["url"],
                "branch": n["headRefName"],
                "state": "MERGED",
                "at": iso_time(n["mergedAt"]),
                "adds": n.get("additions"),
                "dels": n.get("deletions"),
            }
            for n in (data.get("merged") or {}).get("nodes") or [] if n
        ),
        key=lambda p: -(p["at"] or 0),
    )
    open_prs = [p for p in prs if p["state"] == "OPEN"]
    # A PR's checks section is every check at its latest run. Neither GitHub's summary (it can stay red
    # after a re-run passes, and read green while a check is still running) nor a list cut at 100 of a
    # PR's ~200 checks matches it, so each open PR's checks are read in full: gh view pages them.
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = [pool.submit(gh_json, "pr", "view", str(p["number"]), "-R", p["repo"], "--json", "statusCheckRollup")
                for p in open_prs]
        required = {(p["repo"], p["base"]): pool.submit(required_checks, p["repo"], p["base"]) for p in open_prs}
        for p, job in zip(open_prs, jobs):
            full = job.result()
            if not full:
                p["ci_unknown"] = True  # better a question mark than a green that isn't
                continue
            runs = full.get("statusCheckRollup") or []
            p.update(check_counts(runs))
            # Required checks not created yet don't show up in the list at all: GitHub calls them
            # "Expected — waiting for status to be reported", and the CI isn't done until they report
            present = {c.get("name") or c.get("context") for c in runs}
            waiting = [n for n in required[(p["repo"], p["base"])].result() if n not in present]
            if waiting:
                p["pending"] += len(waiting)
                p["pending_names"] = sorted(set(p["pending_names"]) | {re.sub(r"^\W+", "", n) for n in waiting})
    return prs


REVIEWS_QUERY = """
query($q: String!, $login: String!) {
  viewer { organizations(first: 100) { nodes { login teams(first: 100, userLogins: [$login]) { nodes { slug } } } } }
  search(query: $q, type: ISSUE, first: 100) {
    nodes {
      ... on PullRequest {
        number title url createdAt additions deletions
        author { login __typename }
        reviewRequests(first: 30) {
          nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } }
        }
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
        mine: reviews(author: $login, last: 1) { nodes { submittedAt } }
      }
    }
  }
}
"""
LOGIN = []  # your GitHub login, looked up once
ORGS_PATH = os.path.join(CACHE_DIR, "my-orgs.json")  # MY_ORGS as last read, so the deck starts with them


def load_my_orgs():
    try:
        with open(ORGS_PATH) as f:
            found = json.load(f)
        return [org for org in found if isinstance(org, str)] if isinstance(found, list) else []
    except (OSError, ValueError):
        return []


MY_ORGS = load_my_orgs()  # the organisations you're in, from the last review requests read


def reviews_key():
    """What the review requests are kept under: "requested" and the organisations they keep to, if any."""
    return "requested" + "".join(f" org:{org}" for org in ORGS)


def fetch_reviews(key):
    """Open PRs that ask you or one of your teams for a review; False if gh failed."""
    if not LOGIN:
        me = gh_json("api", "user")
        if not me:
            return False
        LOGIN.append(me["login"])
    orgs = key[len("requested"):]
    q = f"review-requested:@me type:pr{orgs} state:open draft:false sort:created-desc"
    d = gh_json("api", "graphql", "-f", f"query={REVIEWS_QUERY}", "-f", f"q={q}", "-f", f"login={LOGIN[0]}")
    if not d or not d.get("data"):
        return False
    # your teams in every organisation you're in, to tell the requests to them from the rest
    mine = ((d["data"].get("viewer") or {}).get("organizations") or {}).get("nodes") or []
    orgs = [org["login"] for org in mine if org]
    if orgs != MY_ORGS:
        MY_ORGS[:] = orgs
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(ORGS_PATH, "w") as f:
                json.dump(orgs, f)
        except OSError:
            pass  # read again on the next fetch
    my_teams = {t["slug"] for org in mine if org for t in (org.get("teams") or {}).get("nodes") or []}
    prs = []
    for n in d["data"]["search"]["nodes"]:
        if not n:
            continue
        asked = [r["requestedReviewer"] for r in n["reviewRequests"]["nodes"] if r.get("requestedReviewer")]
        teams = [r["slug"] for r in asked if r["__typename"] == "Team"]
        author = n.get("author") or {}
        login = author.get("login") or "?"
        commits = n["commits"]["nodes"]
        rollup = (commits[-1]["commit"].get("statusCheckRollup") if commits else None) or {}
        mine = (n.get("mine") or {}).get("nodes") or []
        prs.append(
            {
                "number": n["number"],
                "title": n["title"],
                "url": n["url"],
                "author": login,
                "bot": author.get("__typename") == "Bot" or login.endswith(("-bot", "[bot]")),
                "direct": any(r["__typename"] == "User" and r.get("login") == LOGIN[0] for r in asked),
                "teams": teams,
                "via": [t for t in teams if t in my_teams],
                "at": iso_time(n["createdAt"]),
                "adds": n.get("additions") or 0,
                "dels": n.get("deletions") or 0,
                "ci": rollup.get("state") or "",
                "reviewed": iso_time(mine[-1]["submittedAt"]) if mine else None,  # your last review
                "pushed": iso_time(commits[-1]["commit"].get("committedDate")) if commits else None,
            }
        )
    return prs


STORES = []  # every Store, so --once can wait until none is fetching


class Store:
    """Values fetched in the background, one per key, and kept on disk."""

    def __init__(self, name, fetch):
        STORES.append(self)
        self.path = os.path.join(CACHE_DIR, f"{name}.json")
        self.fetch = fetch  # key -> value, or False to keep the old one
        self.lock = threading.Lock()
        self.busy = False
        self.again = []  # keys a forced refresh asked for while a fetch ran: fetched once it's done
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key):
        """{"at": fetched time, "value": ...}, or None if not fetched yet."""
        with self.lock:
            return self.data.get(key)

    def fetched_at(self, keys):
        with self.lock:
            times = [self.data[k]["at"] for k in keys if k in self.data]
        return min(times) if times else None

    def refresh(self, keys, force=False):
        now = time.time()
        with self.lock:
            if self.busy:
                if force:  # what it fetches may be out of date already (a setting changed)
                    self.again = keys
                return
            stale = [k for k in keys if force or k not in self.data or now - self.data[k]["at"] > GH_TTL]
            if not stale:
                return
            self.busy = True
        threading.Thread(target=self._fetch, args=(stale,), daemon=True).start()

    def fetch_one(self, key):
        """fetch(key), or False (keep the old value) when it breaks: an answer gh didn't give in full, say.
        What broke goes to ERRORS; printed, it would land over the deck."""
        try:
            return self.fetch(key)
        except Exception:
            log_error(f"fetching {key} into {os.path.basename(self.path)}")
            return False

    def _fetch(self, keys):
        try:
            results = list(EXEC.map(self.fetch_one, keys))
            with self.lock:
                for key, value in zip(keys, results):
                    if value is False:  # keep what we had, try again after the TTL
                        value = (self.data.get(key) or {}).get("value")
                    self.data[key] = {"at": time.time(), "value": value}
                snapshot = json.dumps(self.data)
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                f.write(snapshot)
            os.replace(self.path + ".tmp", self.path)
        finally:
            with self.lock:
                self.busy = False
                again, self.again = self.again, []
            if again:
                self.refresh(again, True)
            if dash_common.WAKE is not None:  # set by the main loop, so read from there
                os.write(dash_common.WAKE, b"p")


MY_PRS = Store("mine", fetch_mine)  # keyed by mine_key()
REVIEWS = Store("reviews", fetch_reviews)  # keyed by reviews_key()


def pending_reviews(entry):
    """The review requests worth your attention, asked of you first, then newest.

    Everything asked of you directly; team requests unless they come from a bot
    or fan out to more than BROADCAST_TEAMS teams (CODEOWNERS broadcasts)."""
    prs = (entry or {}).get("value") or []
    worth = [p for p in prs if p["direct"] or (not p["bot"] and len(p["teams"]) <= BROADCAST_TEAMS)]
    return sorted(worth, key=lambda p: (not p["direct"], -(p["at"] or 0)))
