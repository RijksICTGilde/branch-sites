import base64
import json
import subprocess
import time
from pathlib import Path

import pytest

import branch_sites
from branch_sites import GitError, Syncer, auth_env, public_url, slugify


def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class Origin:
    """A local repository standing in for GitHub."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir()
        run(path, "git", "init", "--quiet", "--initial-branch=main")
        run(path, "git", "config", "user.name", "Team Test")
        run(path, "git", "config", "user.email", "test@example.org")
        self.commit("main", {"index.html": "<title>Main</title>"})

    @property
    def url(self) -> str:
        return self.path.as_uri()

    def commit(self, branch: str, files: dict[str, str], base: str = "main") -> None:
        exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=self.path, capture_output=True)
        if exists.returncode == 0:
            run(self.path, "git", "switch", "--quiet", branch)
        elif branch == "main":
            pass
        else:
            run(self.path, "git", "switch", "--quiet", "-c", branch, base)
        for name, content in files.items():
            (self.path / name).parent.mkdir(parents=True, exist_ok=True)
            (self.path / name).write_text(content)
        run(self.path, "git", "add", "--all")
        run(self.path, "git", "commit", "--quiet", "-m", f"update {branch}")
        run(self.path, "git", "switch", "--quiet", "main")

    def symlink(self, branch: str, name: str, target: str) -> None:
        run(self.path, "git", "switch", "--quiet", branch)
        (self.path / name).symlink_to(target)
        run(self.path, "git", "add", "--all")
        run(self.path, "git", "commit", "--quiet", "-m", "symlink")
        run(self.path, "git", "switch", "--quiet", "main")

    def delete(self, branch: str) -> None:
        run(self.path, "git", "branch", "--quiet", "-D", branch)


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "origin")


@pytest.fixture
def syncer(tmp_path: Path, origin: Origin) -> Syncer:
    s = Syncer(origin.url, tmp_path / "sites", tmp_path / "work")
    s.reset()
    return s


def index(syncer: Syncer) -> dict:
    return json.loads((syncer.sites_dir / "_index.json").read_text())


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Team-Zonnebloem", "team-zonnebloem"),
        ("feature/new_page", "feature-new-page"),
        ("../../etc", "etc"),
        ("_index", "index"),
        ("___", ""),
        ("a" * 80, "a" * 63),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_public_url_strips_credentials() -> None:
    assert public_url("https://x:secret@github.com/org/repo.git") == "https://github.com/org/repo.git"
    assert public_url("https://token@git.example.org:8443/r.git") == "https://git.example.org:8443/r.git"
    assert public_url("https://github.com/org/repo") == "https://github.com/org/repo"


def test_publishes_every_branch_with_index(syncer: Syncer, origin: Origin) -> None:
    origin.commit("Team/Zon", {"index.html": "<title>Zon &amp; Maan</title>", "img/a.txt": "a"})

    assert syncer.sync() is True

    assert (syncer.sites_dir / "main" / "index.html").read_text() == "<title>Main</title>"
    assert (syncer.sites_dir / "team-zon" / "img" / "a.txt").read_text() == "a"
    assert (syncer.sites_dir / "_ready").exists()
    sites = {s["slug"]: s for s in index(syncer)["sites"]}
    assert sites["team-zon"]["title"] == "Zon & Maan"
    assert sites["team-zon"]["name"] == "Team/Zon"
    assert sites["team-zon"]["path"] == "/team-zon/"
    assert sites["team-zon"]["author"] == "Team Test"


def test_unchanged_repository_changes_nothing(syncer: Syncer) -> None:
    syncer.sync()
    assert syncer.sync() is False


def test_update_replaces_site_and_removes_old_version(syncer: Syncer, origin: Origin) -> None:
    origin.commit("zon", {"index.html": "v1", "old.html": "old"})
    syncer.sync()
    first_sha = syncer.published["zon"]

    origin.commit("zon", {"index.html": "v2"})
    run(origin.path, "git", "switch", "--quiet", "zon")
    run(origin.path, "git", "rm", "--quiet", "old.html")
    run(origin.path, "git", "commit", "--quiet", "-m", "remove old")
    run(origin.path, "git", "switch", "--quiet", "main")
    assert syncer.sync() is True

    assert (syncer.sites_dir / "zon" / "index.html").read_text() == "v2"
    assert not (syncer.sites_dir / "zon" / "old.html").exists()
    # A request that resolved the old symlink just before the swap can still read the old files.
    assert (syncer.data_dir / f"zon-{first_sha}" / "old.html").read_text() == "old"

    syncer.sync()

    assert not (syncer.data_dir / f"zon-{first_sha}").exists()


def test_deleted_branch_is_removed(syncer: Syncer, origin: Origin) -> None:
    origin.commit("zon", {"index.html": "x"})
    syncer.sync()
    origin.delete("zon")

    assert syncer.sync() is True

    assert not (syncer.sites_dir / "zon").exists()
    assert [s["slug"] for s in index(syncer)["sites"]] == ["main"]

    syncer.sync()

    assert list(syncer.data_dir.glob("zon-*")) == []


def test_colliding_slugs_publish_first_branch_only(syncer: Syncer, origin: Origin) -> None:
    origin.commit("team-a", {"index.html": "dash"})
    origin.commit("team/a", {"index.html": "slash"})

    syncer.sync()

    assert (syncer.sites_dir / "team-a" / "index.html").read_text() == "dash"
    assert [s["name"] for s in index(syncer)["sites"]].count("team/a") == 0


def test_symlink_outside_site_is_dropped(syncer: Syncer, origin: Origin) -> None:
    origin.commit("zon", {"index.html": "x"})
    origin.symlink("zon", "passwd", "/etc/passwd")

    syncer.sync()

    assert (syncer.sites_dir / "zon" / "index.html").read_text() == "x"
    assert not (syncer.sites_dir / "zon" / "passwd").exists()
    assert not (syncer.sites_dir / "zon" / "passwd").is_symlink()


def test_auth_env_sends_token_as_basic_header() -> None:
    env = auth_env("secret-token")

    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    scheme, encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: ").split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "x-access-token:secret-token"
    assert auth_env(None) == {} and auth_env("") == {}


def test_token_is_not_stored_or_published(tmp_path: Path, origin: Origin) -> None:
    syncer = Syncer(origin.url, tmp_path / "sites", tmp_path / "work", token="secret-token")
    syncer.reset()
    origin.commit("zon", {"index.html": "x"})

    syncer.sync()

    assert (syncer.sites_dir / "zon" / "index.html").read_text() == "x"
    encoded = base64.b64encode(b"x-access-token:secret-token").decode()
    for path in [*syncer.repo.rglob("*"), syncer.sites_dir / "_index.json"]:
        if path.is_file():
            content = path.read_bytes()
            assert b"secret-token" not in content and encoded.encode() not in content, path


def test_failing_branch_does_not_block_others(syncer: Syncer, origin: Origin, monkeypatch: pytest.MonkeyPatch) -> None:
    origin.commit("a-kapot", {"index.html": "kapot"})
    origin.commit("zon", {"index.html": "zon"})
    publish = Syncer.publish

    def failing_publish(self: Syncer, slug: str, sha: str) -> None:
        if slug == "a-kapot":
            raise GitError("git archive failed with exit 128")
        publish(self, slug, sha)

    monkeypatch.setattr(Syncer, "publish", failing_publish)
    syncer.sync()

    assert (syncer.sites_dir / "zon" / "index.html").read_text() == "zon"
    assert not (syncer.sites_dir / "a-kapot").exists()
    assert "a-kapot" not in [s["slug"] for s in index(syncer)["sites"]]

    monkeypatch.setattr(Syncer, "publish", publish)
    assert syncer.sync() is True

    assert (syncer.sites_dir / "a-kapot" / "index.html").read_text() == "kapot"


def test_hanging_git_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    # The child sleep stands in for git-remote-https: it must be killed along with git.
    fake_git.write_text('#!/bin/sh\nsleep 30 &\necho $! > "$PIDFILE"\nwait\n')
    fake_git.chmod(0o755)
    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}")
    monkeypatch.setenv("PIDFILE", str(pidfile))
    monkeypatch.setattr(branch_sites, "GIT_TIMEOUT", 1)

    start = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        branch_sites.git(tmp_path, "fetch")

    assert time.monotonic() - start < 10
    helper = int(pidfile.read_text())
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(helper)], capture_output=True, text=True).stdout.strip()
    assert state in ("", "Z"), f"helper still running: {state}"
