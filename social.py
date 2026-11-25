"""Cross-poster, run by .github/workflows/social.yml.

A PC publisher schedules each short elsewhere, then puts the video and a post.json (slot time, title, caption) on a
draft release of this repo (drafts are not public). This script posts every short whose slot has passed:
  - an Instagram Reel through a Meta app (resumable upload, so no public file URL is needed)
  - a TikTok post through Post for Me, labelled AI-generated (AI voices)
state.enc (encrypted) records every finished step, so a rerun only does what is left.

Env (GitHub secrets): META_PAGE_ACCESS_TOKEN, META_IG_USER_ID, POSTFORME_API_KEY, TIKTOK_HANDLE, STATE_KEY,
NOTICE_LABEL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GH_TOKEN (the workflow token). Optional: DRY_RUN=1,
TIKTOK_PRIVACY (default public).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = os.environ.get("GITHUB_REPOSITORY", "cloudstrive6/amber-loom")
LABEL = os.environ.get("NOTICE_LABEL", "Poster")
GV = "v23.0"
PFM = "https://api.postforme.dev/v1"
MAX_TRIES = 8          # per platform per short, spread over about 40 hours; then it waits for a person (Telegram says so)
PER_RUN = 2            # shorts per run, so a run stays well inside the 40-minute job limit
DRY = os.environ.get("DRY_RUN") == "1"


def env(k: str) -> str:
    v = os.environ.get(k, "").strip()
    if not v:
        raise RuntimeError(f"missing secret {k}")
    return v


def req(url, data=None, method=None, headers=None, timeout=300):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    r = urllib.request.Request(url, data=body, method=method or ("POST" if body is not None else "GET"),
                               headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {e.read()[:500]!r}") from None


def telegram(text: str) -> None:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    # the text is never printed: the run logs of a public repo are public
    if tok and chat and not DRY:
        try:
            req(f"https://api.telegram.org/bot{tok}/sendMessage", {"chat_id": chat, "text": text[:4000]})
        except Exception as e:  # a failed notice must never stop posting
            print("telegram notice failed:", type(e).__name__)


STATE = HERE / "state.enc"     # post state, encrypted with STATE_KEY because the repo is public
_LAST = ""


def _crypt(data: bytes, decrypt: bool) -> bytes:
    env("STATE_KEY")
    args = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt", "-a", "-A",
            "-pass", "env:STATE_KEY"] + (["-d"] if decrypt else [])
    r = subprocess.run(args, input=data, capture_output=True)
    if r.returncode:
        raise RuntimeError("state.enc could not be " + ("decrypted" if decrypt else "encrypted") + " (STATE_KEY?)")
    return r.stdout


def load_state() -> dict:
    global _LAST
    if not STATE.exists():
        return {}
    _LAST = _crypt(STATE.read_bytes().strip(), True).decode("utf-8")
    return json.loads(_LAST)


def ref(x: str) -> str:
    """A short opaque label for a short in the public log."""
    return "s-" + __import__("hashlib").sha256(x.encode()).hexdigest()[:6]


def gh(*args) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


# ------------------------------------------------------------------ Instagram (Meta Graph API, resumable upload)


def G(path: str) -> str:
    return f"https://graph.facebook.com/{GV}/{path}"


def instagram_reel(video: Path, caption: str, st: dict) -> dict:
    """Container id is saved before publishing; a retry first asks the container whether it already went out, so a
    crash or a misleading error after media_publish never posts the Reel twice."""
    tok, ig = env("META_PAGE_ACCESS_TOKEN"), env("META_IG_USER_ID")
    status = lambda cid: req(G(f"{cid}?fields=status_code,status&access_token={tok}"))
    cid = st.get("ig_cid")
    if cid:
        j = status(cid)
        if j.get("status_code") == "PUBLISHED":
            return {"id": None, "url": None, "container": cid}
        if j.get("status_code") in ("ERROR", "EXPIRED"):
            cid = None
    if not cid:
        c = req(G(f"{ig}/media"), {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                                   "share_to_feed": "true", "access_token": tok})
        cid = c["id"]
        data = video.read_bytes()
        req(c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GV}/{cid}", data=data, method="POST",
            headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(len(data))}, timeout=1800)
        st["ig_cid"] = cid
        save()
    until = time.time() + 15 * 60
    while True:
        j = status(cid)
        if j.get("status_code") == "FINISHED":
            break
        if j.get("status_code") == "PUBLISHED":
            return {"id": None, "url": None, "container": cid}
        if j.get("status_code") in ("ERROR", "EXPIRED"):
            st.pop("ig_cid", None)
            raise RuntimeError(f"Instagram container {cid} {j.get('status_code')}: {j.get('status')}")
        if time.time() > until:
            raise RuntimeError(f"Instagram container {cid} still {j.get('status_code')}; the next run checks again")
        time.sleep(10)
    try:
        pid = req(G(f"{ig}/media_publish"), {"creation_id": cid, "access_token": tok})["id"]
    except RuntimeError:
        time.sleep(20)                     # Meta sometimes errors on a publish that went through
        if status(cid).get("status_code") == "PUBLISHED":
            return {"id": None, "url": None, "container": cid}
        raise
    try:
        link = req(G(f"{pid}?fields=permalink&access_token={tok}")).get("permalink")
    except RuntimeError:
        link = None
    return {"id": pid, "url": link, "container": cid}


# ------------------------------------------------------------------ TikTok (Post for Me)


def pfm(method: str, path: str, body: dict | None = None):
    return req(f"{PFM}{path}", data=json.dumps(body).encode() if body is not None else None, method=method,
               headers={"Authorization": f"Bearer {env('POSTFORME_API_KEY')}", "Content-Type": "application/json"},
               timeout=120)


def tiktok_account() -> dict:
    accts = pfm("GET", "/social-accounts?platform=tiktok&limit=50").get("data", [])
    a = next((x for x in accts if (x.get("username") or "").lstrip("@").lower() == env("TIKTOK_HANDLE").lstrip("@").lower()
              or (x.get("external_id") or "").lower() == env("TIKTOK_HANDLE").lstrip("@").lower()), None)
    if not a:
        raise RuntimeError(f"Post for Me: the TikTok account is not in this project "
                           f"(found: {', '.join(x.get('username') or '?' for x in accts) or 'none'})")
    if a.get("status") != "connected":
        raise RuntimeError(f"Post for Me: the TikTok account is {a.get('status')}; reconnect it at app.postforme.dev")
    return a


def tiktok_post(video: Path, caption: str, st: dict) -> dict:
    acct = tiktok_account()
    if not st.get("tt_post"):
        u = pfm("POST", "/media/create-upload-url", {})
        r = urllib.request.Request(u["upload_url"], data=video.read_bytes(), method="PUT",
                                   headers={"Content-Type": "video/mp4"})
        with urllib.request.urlopen(r, timeout=1800):
            pass
        p = pfm("POST", "/social-posts", {
            "caption": caption[:2200], "social_accounts": [acct["id"]],
            "media": [{"url": u["media_url"], "thumbnail_timestamp_ms": 1000}],
            "platform_configurations": {"tiktok": {
                "privacy_status": os.environ.get("TIKTOK_PRIVACY", "public"),
                "allow_comment": True, "allow_duet": True, "allow_stitch": True,
                "is_ai_generated": True}}})        # the voices are AI: label it
        st["tt_post"] = p["id"]
        save()
    for _ in range(40):                            # TikTok processing usually takes 1 to 3 minutes
        res = (pfm("GET", f"/social-post-results?post_id={st['tt_post']}").get("data") or [None])[0]
        if res:
            if not res.get("success"):
                st.pop("tt_post", None)            # a failed post is retried from the upload next run
                raise RuntimeError(f"TikTok via Post for Me failed: {json.dumps(res.get('error'))[:400]}")
            pd = res.get("platform_data") or {}
            return {"id": pd.get("id") or st["tt_post"], "url": pd.get("url")}
        time.sleep(15)
    raise RuntimeError("TikTok result not back after 10 min; the next run checks again")


# ------------------------------------------------------------------ main loop

S: dict = {}


def save() -> None:
    """Write state.json and, on GitHub Actions, commit and push it at once, so a later or cancelled run never
    repeats a post that already went out."""
    global _LAST
    plain = json.dumps(S, indent=1, ensure_ascii=False) + "\n"
    if plain == _LAST:
        return
    STATE.write_bytes(_crypt(plain.encode("utf-8"), False) + b"\n")
    _LAST = plain
    if os.environ.get("GITHUB_ACTIONS") != "true" or DRY:
        return
    git = lambda *a: subprocess.run(["git", *a], cwd=HERE, capture_output=True, text=True)
    git("add", "state.enc")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("-c", "user.name=poster-bot", "-c", f"user.email={os.environ.get('GITHUB_REPOSITORY_OWNER_ID', '0')}"
        f"+{os.environ.get('GITHUB_REPOSITORY_OWNER', 'bot')}@users.noreply.github.com", "commit", "-q", "-m", "social: post state")
    for _ in range(3):   # only this workflow writes state.json and runs never overlap (concurrency group)
        if git("pull", "--rebase", "-q").returncode == 0 and git("push", "-q").returncode == 0:
            return
        time.sleep(5)
    print("WARNING: state push failed; the end-of-job step retries", flush=True)


def due_posts() -> list[dict]:
    rels = json.loads(gh("api", f"repos/{REPO}/releases?per_page=100"))
    out = []
    for r in rels:
        tag = r["tag_name"]
        if "post.json" not in {a["name"] for a in r.get("assets", [])}:
            continue        # a release the PC is still filling: post.json is always uploaded last
        try:
            with tempfile.TemporaryDirectory() as t:
                gh("release", "download", tag, "--repo", REPO, "--pattern", "post.json", "--dir", t)
                out.append(json.loads((Path(t) / "post.json").read_text(encoding="utf-8")) | {"tag": tag})
        except Exception as e:
            print(f"{ref(tag)}: cannot read post.json, skipped this run ({type(e).__name__})", flush=True)
    return sorted(out, key=lambda p: p["at"])


def backoff_ok(st: dict, k: str, now: dt.datetime) -> bool:
    """Retries spread out in time (30 min, 1 h, 2 h ... up to 12 h) so a Meta or TikTok outage of a day or two does
    not drop a short. After MAX_TRIES it waits for a person: delete the <platform>_tries key in state.json."""
    n = st.get(k + "_tries", 0)
    if n >= MAX_TRIES:
        return False
    nxt = st.get(k + "_next")
    return not nxt or dt.datetime.fromisoformat(nxt) <= now


def run() -> int:
    global S
    S = load_state()
    now = dt.datetime.now(dt.timezone.utc)
    failed = done = 0
    t0 = time.time()
    for p in due_posts():
        if done >= PER_RUN or time.time() - t0 > 15 * 60:
            print("per-run cap reached; the rest go next run", flush=True)   # one short can take ~27 min
            break
        st = S.setdefault(p["id"], {})
        try:
            if dt.datetime.fromisoformat(p["at"]) > now:
                print(f"{ref(p['id'])}: not live until {p['at']}")
                continue
            jobs = [("instagram", lambda v: instagram_reel(v, p["caption"], st)),
                    ("tiktok", lambda v: tiktok_post(v, p["caption"], st))]
            todo = [(k, f) for k, f in jobs if not st.get(k) and backoff_ok(st, k, now)]
            if not todo:
                continue
            print(f"{ref(p['id'])}: posting {', '.join(k for k, _ in todo)}", flush=True)
            if DRY:
                continue
            done += 1
            with tempfile.TemporaryDirectory() as t:
                gh("release", "download", p["tag"], "--repo", REPO, "--pattern", "video.mp4", "--dir", t)
                video = Path(t) / "video.mp4"
                for k, f in todo:
                    try:
                        st[k] = f(video)
                        st[k]["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                        for x in ("_tries", "_next", "_error"):
                            st.pop(k + x, None)
                        telegram(f"{LABEL}: {p['title']} is on {k.title()} {st[k].get('url') or ''}")
                    except Exception as e:
                        failed += 1
                        n = st[k + "_tries"] = st.get(k + "_tries", 0) + 1
                        st[k + "_next"] = (now + dt.timedelta(minutes=min(720, 30 * 2 ** (n - 1)))).isoformat(timespec="seconds")
                        st[k + "_error"] = str(e)[:500]
                        print(f"{ref(p['id'])}: {k} failed (try {n})", flush=True)
                        more = f"next try {st[k + '_next']} UTC" if n < MAX_TRIES else "no more automatic tries"
                        telegram(f"{LABEL}: {k} post of {p['id']} FAILED (try {n}, {more}).\n{e}"[:900])
                    save()
        except Exception as e:      # one broken release must never block the others
            failed += 1
            print(f"{ref(p['id'])}: could not be handled this run", flush=True)
            telegram(f"{LABEL}: could not handle {p['id']}: {e}"[:900])
    save()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
