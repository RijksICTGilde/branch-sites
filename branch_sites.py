"""Polls a Git repository and publishes every branch as a static site, served by nginx."""

import base64
import html
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SITES_DIR = Path("/tmp/sites")
WORK_DIR = Path("/tmp/work")
NGINX_CMD = ["nginx", "-e", "stderr", "-c", "/app/nginx.conf"]

# A hanging fetch (network, proxy) would otherwise stall the loop forever while the old sites keep being served.
GIT_TIMEOUT = 300
SLUG_MAX = 63
TITLE_SCAN_BYTES = 64 * 1024
# The page shown for a site without index.html lists its HTML pages. Enough to find your way, not a file browser.
PAGES_MAX = 100
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
REF_FORMAT = "%(refname:lstrip=2)%00%(objectname)%00%(committerdate:iso-strict)%00%(subject)"

log = logging.getLogger("branch-sites")


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str
    date: str
    subject: str


def slugify(name: str) -> str:
    """Branch name to a DNS-label-safe directory name. Never contains '.' or '_', those are ours."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-")


def auth_env(token: str | None) -> dict[str, str]:
    """Git config that sends the token as a header: never on the command line, never in .git/config."""
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill git and its helpers (git-remote-https), which run in the process group started for it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    # GIT_TOKEN is the only credential: no prompt to hang on, no helper (like a keychain) to fall back to.
    full_env = os.environ | {"GIT_TERMINAL_PROMPT": "0"} | (env or {})
    cmd = ["git", "-c", "credential.helper=", "-C", str(repo), *args]
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=full_env, start_new_session=True
    ) as proc:
        try:
            stdout, _ = proc.communicate(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            raise GitError(f"git {args[0]} timed out after {GIT_TIMEOUT}s") from None
    if proc.returncode != 0:
        # The command line is left out on purpose: it can hold the repository URL.
        raise GitError(f"git {args[0]} failed with exit {proc.returncode}")
    return stdout


def public_url(url: str) -> str:
    """The repository URL without credentials, for showing on the pages."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def _drop_unsafe_member(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.FilterError:
        log.warning("skipping unsafe entry in archive: %s", member.name)
        return None


def _read_title(index_file: Path) -> str:
    try:
        with index_file.open("rb") as f:
            head = f.read(TITLE_SCAN_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""
    match = TITLE_RE.search(head)
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1)).split())[:200]


def _html_pages(site_dir: Path) -> list[str]:
    """HTML pages of a site as paths relative to it, for linking to when the site has no index.html."""
    pages = sorted(
        path.relative_to(site_dir).as_posix()
        for path in site_dir.rglob("*")
        if path.suffix.lower() in (".html", ".htm") and path.is_file() and not path.is_symlink()
    )
    return pages[:PAGES_MAX]


def write_explanation(sites_dir: Path, push_token: str | None) -> None:
    """Settings for the explanation page. The push token is meant to be shown: it is what participants push with."""
    tmp = sites_dir / ".uitleg.json.tmp"
    tmp.write_text(json.dumps({"push_token": push_token or None}))
    os.replace(tmp, sites_dir / "_uitleg.json")


class Syncer:
    def __init__(self, repo_url: str, sites_dir: Path, work_dir: Path, token: str | None = None) -> None:
        self.repo_url = repo_url
        self.auth_env = auth_env(token)
        self.sites_dir = sites_dir
        self.data_dir = sites_dir / ".data"
        self.repo = work_dir / "repo.git"
        self.published: dict[str, str] = {}
        # Versions replaced in the previous round. Deleted one round later, so a request that resolved the old
        # symlink just before the swap can still read its files.
        self.retired: list[Path] = []

    def reset(self) -> None:
        """Start from an empty sites dir, so leftovers from a previous container are not served."""
        shutil.rmtree(self.sites_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True)

    def fetch(self) -> None:
        if not self.repo.exists():
            self.repo.mkdir(parents=True)
            git(self.repo, "init", "--bare", "--quiet")
            git(self.repo, "remote", "add", "origin", self.repo_url)
        git(
            self.repo,
            *("fetch", "--quiet", "--prune", "--depth=1", "origin", "+refs/heads/*:refs/heads/*"),
            env=self.auth_env,
        )

    def branches(self) -> dict[str, Branch]:
        """Branches by slug. When two names map to the same slug, the alphabetically first wins."""
        out = git(self.repo, "for-each-ref", f"--format={REF_FORMAT}", "refs/heads").decode("utf-8", errors="replace")
        result: dict[str, Branch] = {}
        for line in sorted(out.splitlines()):
            branch = Branch(*line.split("\0"))
            slug = slugify(branch.name)
            if not slug:
                log.warning("skipping branch without a usable name: %r", branch.name)
            elif slug in result:
                log.warning("skipping branch %r: %r already publishes on /%s/", branch.name, result[slug].name, slug)
            else:
                result[slug] = branch
        return result

    def sync(self) -> bool:
        """One poll round. Returns whether anything changed."""
        for old in self.retired:
            shutil.rmtree(old, ignore_errors=True)
        self.retired = []
        self.fetch()
        branches = self.branches()
        changed = False
        for slug, branch in branches.items():
            if self.published.get(slug) != branch.sha:
                # One branch that fails to extract must not hold up the others. It is retried next round.
                try:
                    self.publish(slug, branch.sha)
                except (GitError, tarfile.TarError, OSError) as exc:
                    log.error("publishing /%s/ at %s failed: %s", slug, branch.sha[:8], exc)
                    continue
                changed = True
        for slug in set(self.published) - set(branches):
            self.unpublish(slug)
            changed = True
        if changed or not (self.sites_dir / "_index.json").exists():
            self.write_index(branches)
        (self.sites_dir / "_ready").touch()
        return changed

    def publish(self, slug: str, sha: str) -> None:
        target = self.data_dir / f"{slug}-{sha}"
        tmp = self.data_dir / f".{slug}-{sha}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        tmp.mkdir()
        cmd = ["git", "-C", str(self.repo), "archive", "--format=tar", sha]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, start_new_session=True)
        assert proc.stdout is not None
        # Killing git ends the stream, so a hanging archive surfaces as a truncated tar instead of a stuck loop.
        timer = threading.Timer(GIT_TIMEOUT, _kill_group, [proc])
        timer.start()
        try:
            with proc.stdout, tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                tar.extractall(tmp, filter=_drop_unsafe_member)
            if proc.wait() != 0:
                raise GitError(f"git archive failed with exit {proc.returncode}")
        except BaseException:
            _kill_group(proc)
            proc.wait()
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        finally:
            timer.cancel()
        tmp.rename(target)

        # Swap the symlink in one rename, so nginx never sees a half-extracted site.
        link = self.sites_dir / slug
        tmp_link = self.sites_dir / f".{slug}.link"
        tmp_link.unlink(missing_ok=True)
        tmp_link.symlink_to(Path(".data") / target.name)
        os.replace(tmp_link, link)

        old_sha = self.published.get(slug)
        if old_sha:
            self.retired.append(self.data_dir / f"{slug}-{old_sha}")
        self.published[slug] = sha
        log.info("published /%s/ at %s", slug, sha[:8])

    def unpublish(self, slug: str) -> None:
        (self.sites_dir / slug).unlink(missing_ok=True)
        self.retired.append(self.data_dir / f"{slug}-{self.published.pop(slug)}")
        log.info("removed /%s/", slug)

    def write_index(self, branches: dict[str, Branch]) -> None:
        sites = [
            asdict(branch)
            | {
                "slug": slug,
                "path": f"/{slug}/",
                "title": _read_title(self.sites_dir / slug / "index.html"),
                "has_index": (self.sites_dir / slug / "index.html").is_file(),
                "pages": _html_pages(self.data_dir / f"{slug}-{self.published[slug]}"),
            }
            for slug, branch in branches.items()
            if slug in self.published
        ]
        sites.sort(key=lambda s: s["date"], reverse=True)
        index = {
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "repo": public_url(self.repo_url),
            "sites": sites,
        }
        tmp = self.sites_dir / ".index.json.tmp"
        tmp.write_text(json.dumps(index, ensure_ascii=False))
        os.replace(tmp, self.sites_dir / "_index.json")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    repo_url = os.environ["REPO_URL"]
    interval = float(os.environ.get("POLL_INTERVAL_MINUTES", "5")) * 60

    syncer = Syncer(repo_url, SITES_DIR, WORK_DIR, token=os.environ.get("GIT_TOKEN"))
    syncer.reset()
    write_explanation(SITES_DIR, os.environ.get("PUSH_TOKEN"))
    nginx = subprocess.Popen(NGINX_CMD)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    while not stop.is_set():
        if nginx.poll() is not None:
            log.error("nginx exited with %s", nginx.returncode)
            return 1
        try:
            syncer.sync()
        except (GitError, tarfile.TarError, OSError) as exc:
            log.error("sync failed: %s", exc)
        stop.wait(interval)

    nginx.send_signal(signal.SIGQUIT)
    nginx.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
