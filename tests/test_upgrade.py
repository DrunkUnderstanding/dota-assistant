"""更新助手本身（app/upgrade.py）：检查新版本、git 拉代码、下载解压、替换文件保留个人数据、出错挪回去、控制面板的状态。

GitHub 接口换成假的；git 用临时目录里的真仓库（一个裸仓库当 GitHub）；不碰本机运行目录。
"""
import hashlib
import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from dota_assistant.app import upgrade
from dota_assistant.app import webui
from tests.test_webui import make_live

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def release(tag="v2026.10.29", body="- 修了 A\n- 加了 B", assets=None, published="2026-10-29T08:00:00Z"):
    if assets is None:
        assets = [{"name": "dota-assistant-20261029-abc1234.zip", "size": 3 * 2**20,
                   "browser_download_url": "https://example/x.zip", "digest": "sha256:" + "0" * 64}]
    return {"tag_name": tag, "name": f"Dota 选人助手 {tag}", "body": body, "assets": assets, "published_at": published}


def fake_github(rel, compare_status=None):
    def get(path):
        if path == "/releases/latest":
            if rel is None:
                raise LookupError(path)
            return rel
        if path.startswith("/compare/"):
            if compare_status is None:
                raise LookupError(path)
            return {"status": compare_status}
        raise AssertionError(path)
    return get


class CheckPackageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "VERSION.txt").write_text("20261027-b285af5\n", encoding="utf-8")

    def check(self, rel, status=None):
        with mock.patch.object(upgrade, "_github", side_effect=fake_github(rel, status)) as gh:
            c = upgrade.check(self.root, frozen=True)
        return c, gh

    def test_newer_release(self):
        c, gh = self.check(release(), "ahead")
        self.assertEqual(c.mode, "package")
        self.assertTrue(c.available)
        self.assertEqual(c.current, "20261027-b285af5")
        self.assertEqual(c.latest, "Dota 选人助手 v2026.10.29")
        self.assertEqual(c.url, "https://example/x.zip")
        self.assertEqual(c.sha256, "0" * 64)
        self.assertIn("修了 A", c.notes)
        gh.assert_any_call("/compare/b285af5...v2026.10.29")   # 本地提交...发布的 tag

    def test_same_or_newer_local(self):
        c, _ = self.check(release(), "identical")
        self.assertFalse(c.available)
        self.assertEqual(c.reason, "已经是最新版")
        c, _ = self.check(release(), "behind")
        self.assertFalse(c.available)
        self.assertIn("还新", c.reason)

    def test_unknown_commit_falls_back_to_date(self):
        c, _ = self.check(release(published="2026-10-29T08:00:00Z"))      # compare 404
        self.assertTrue(c.available)
        c, _ = self.check(release(published="2026-10-27T08:00:00Z"))
        self.assertFalse(c.available)
        self.assertIn("看不出", c.reason)

    def test_no_zip_or_no_release(self):
        c, _ = self.check(release(assets=[]), "ahead")
        self.assertFalse(c.available)
        self.assertIn("zip", c.reason)
        c, _ = self.check(None)
        self.assertFalse(c.available)
        self.assertIn("还没有发布", c.reason)

    def test_not_git_not_package(self):
        c = upgrade.check(self.root, frozen=False)
        self.assertEqual(c.mode, "none")
        self.assertFalse(c.available)
        self.assertIn(upgrade.RELEASES_PAGE, c.reason)


class GitTest(unittest.TestCase):
    """裸仓库当 GitHub：作者那边推新提交，玩家这边检查 / 拉"""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, GIT_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        base = Path(tempfile.mkdtemp())
        self.remote, self.dev, self.user = base / "remote.git", base / "dev", base / "user"
        self.git(base, "init", "--bare", "-b", "main", str(self.remote))
        self.git(base, "clone", "-q", str(self.remote), str(self.dev))
        self.commit(self.dev, "a.txt", "1", "第一版")
        self.git(self.dev, "push", "-q", "origin", "HEAD:main")
        self.git(base, "clone", "-q", str(self.remote), str(self.user))

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                              encoding="utf-8").stdout.strip()

    def commit(self, repo, name, text, msg):
        (repo / name).write_text(text, encoding="utf-8")
        self.git(repo, "add", name)
        self.git(repo, "commit", "-q", "-m", msg)

    def test_up_to_date(self):
        c = upgrade.check(self.user, frozen=False)
        self.assertEqual(c.mode, "git")
        self.assertFalse(c.available)
        self.assertEqual(c.reason, "已经是最新版")

    def test_new_commits_then_pull(self):
        self.commit(self.dev, "a.txt", "2", "修了买活误记")
        self.commit(self.dev, "b.txt", "x", "加了更新功能")
        self.git(self.dev, "push", "-q", "origin", "HEAD:main")
        c = upgrade.check(self.user, frozen=False)
        self.assertTrue(c.available)
        self.assertEqual(c.notes, "- 加了更新功能\n- 修了买活误记")
        said = []
        new = upgrade.pull(self.user, said.append)
        self.assertEqual(new, self.git(self.dev, "rev-parse", "HEAD"))
        self.assertEqual((self.user / "a.txt").read_text(encoding="utf-8"), "2")
        self.assertFalse(upgrade.check(self.user, frozen=False).available)

    def test_local_commits(self):
        self.commit(self.user, "c.txt", "1", "本地改的")
        c = upgrade.check(self.user, frozen=False)
        self.assertFalse(c.available)
        self.assertIn("还没推送", c.reason)
        self.commit(self.dev, "a.txt", "2", "远端改的")
        self.git(self.dev, "push", "-q", "origin", "HEAD:main")
        c = upgrade.check(self.user, frozen=False)
        self.assertFalse(c.available)
        self.assertIn("分叉", c.reason)

    def test_no_upstream(self):
        self.git(self.user, "checkout", "-q", "-b", "mine")
        c = upgrade.check(self.user, frozen=False)
        self.assertFalse(c.available)
        self.assertIn("没有跟踪", c.reason)


def tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def read(root: Path) -> dict[str, str]:
    return {f.relative_to(root).as_posix(): f.read_text(encoding="utf-8")
            for f in sorted(root.rglob("*")) if f.is_file() and upgrade.STAGE not in f.relative_to(root).parts}


class ReplaceTest(unittest.TestCase):
    OLD = {"Dota选人助手.exe": "old exe", "_internal/old.dll": "old", "web/panel.js": "old js", "README.md": "old",
           "VERSION.txt": "20261027-b285af5", "config.toml": "account_id = 1", "logs/a.log": "log",
           "shots/1.png": "png", "templates/x.png": "learned", "calibration.json": "mine",
           "data/heroes.json": "local newer", "data/player.json": "me", "data/icons/gold.png": "gold"}
    NEW = {"Dota选人助手.exe": "new exe", "_internal/new.dll": "new", "web/panel.js": "new js", "web/new.css": "css",
           "README.md": "new", "VERSION.txt": "20261029-abc1234", "knowledge/rules.toml": "rules",
           "calibration.json": "packaged", "data/heroes.json": "packaged", "data/icons/gold.png": "packaged",
           "data/icons/new.png": "new icon"}

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.target, self.src = base / "app", base / "app" / upgrade.STAGE / "new" / "Dota选人助手"
        tree(self.target, self.OLD)
        tree(self.src, self.NEW)

    def test_replace_keeps_player_files(self):
        said = []
        placed = upgrade.replace(self.src, self.target, said.append)
        got = read(self.target)
        self.assertEqual(got, {
            "Dota选人助手.exe": "new exe", "_internal/new.dll": "new", "web/panel.js": "new js", "web/new.css": "css",
            "README.md": "new", "VERSION.txt": "20261029-abc1234", "knowledge/rules.toml": "rules",
            # 玩家自己的：不在包里的不碰，校准留本地的，data 只补本地没有的
            "config.toml": "account_id = 1", "logs/a.log": "log", "shots/1.png": "png", "templates/x.png": "learned",
            "calibration.json": "mine", "data/heroes.json": "local newer", "data/player.json": "me",
            "data/icons/gold.png": "gold", "data/icons/new.png": "new icon"})
        self.assertNotIn("data", placed)
        self.assertEqual(said, ["  data/ 补了 1 个本地没有的文件"])
        old = self.target / upgrade.STAGE / "old"                       # 换下来的旧文件，下次启动删
        self.assertEqual((old / "_internal" / "old.dll").read_text(encoding="utf-8"), "old")

    def test_fresh_target_gets_calibration_and_data(self):
        (self.target / "calibration.json").unlink()
        upgrade.replace(self.src, self.target, lambda m: None)
        self.assertEqual((self.target / "calibration.json").read_text(encoding="utf-8"), "packaged")

    def test_failure_rolls_back(self):
        real = upgrade._place

        def place(entry, dst):
            if entry.name == "web":
                raise OSError("磁盘满了")
            real(entry, dst)
        before = read(self.target)
        with mock.patch.object(upgrade, "_place", side_effect=place):
            with self.assertRaises(OSError):
                upgrade.replace(self.src, self.target, lambda m: None)
        self.assertEqual(read(self.target), before)

    def test_locked_entry_gives_up_with_message(self):
        with mock.patch.object(upgrade, "LOCK_WAIT", 0), mock.patch.object(Path, "rename", side_effect=PermissionError("占用")):
            with self.assertRaises(RuntimeError) as e:
                upgrade.replace(self.src, self.target, lambda m: None)
        self.assertIn("被占用", str(e.exception))

    def test_cleanup_removes_stage(self):
        upgrade.cleanup(self.target)
        upgrade._cleaner.join(5)
        self.assertFalse((self.target / upgrade.STAGE).exists())
        self.assertTrue((self.target / "config.toml").exists())


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i:i + n]


def package_zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buf.getvalue()


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_download_extract(self):
        data = package_zip({"Dota选人助手/Dota选人助手.exe": "exe", "Dota选人助手/_internal/a.dll": "dll",
                            "Dota选人助手/web/panel.js": "js"})
        c = upgrade.Check("package", url="https://example/x.zip", size=len(data), sha256=hashlib.sha256(data).hexdigest())
        seen = []
        with mock.patch.object(upgrade._session, "get", return_value=FakeResponse(data)):
            z = upgrade.download(c, self.root, seen.append)
        self.assertEqual(seen[-1], 1.0)
        pkg = upgrade.extract(z, self.root)
        self.assertEqual(pkg, self.root / upgrade.STAGE / "new" / "Dota选人助手")
        self.assertEqual((pkg / "web" / "panel.js").read_text(encoding="utf-8"), "js")

    def test_bad_checksum(self):
        c = upgrade.Check("package", url="u", sha256="1" * 64)
        with mock.patch.object(upgrade._session, "get", return_value=FakeResponse(b"broken")):
            with self.assertRaises(RuntimeError):
                upgrade.download(c, self.root)
        self.assertEqual(list((self.root / upgrade.STAGE).iterdir()), [])

    def test_extract_rejects_bad_zips(self):
        bad = self.root / "bad.zip"
        bad.write_bytes(package_zip({"../evil.txt": "x"}))
        with self.assertRaises(RuntimeError):
            upgrade.extract(bad, self.root)
        self.assertFalse((self.root.parent / "evil.txt").exists())
        other = self.root / "other.zip"
        other.write_bytes(package_zip({"源码/README.md": "x"}))
        with self.assertRaises(RuntimeError):
            upgrade.extract(other, self.root)

    def test_finish_command(self):
        pkg = self.root / "pkg"
        tree(pkg, {"Dota选人助手.exe": "", "_internal/a": ""})
        cmd = upgrade.finish_command(self.root, 123, pkg)
        self.assertEqual(cmd, [str(pkg / "Dota选人助手.exe"), "upgrade", "--finish", "--pid", "123",
                               "--target", str(self.root), "--src", str(pkg)])
        cmd = upgrade.finish_command(self.root, 123, None, restart=False)       # 源码版：用同一个 Python 跑新代码
        self.assertEqual(cmd[1:], ["-X", "utf8", "-m", "dota_assistant", "upgrade", "--finish", "--pid", "123",
                                   "--target", str(self.root), "--no-restart"])


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("等不到")
        time.sleep(0.01)


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch.object(upgrade, "_say")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_check_then_install(self):
        u = upgrade.Updater(Path(tempfile.mkdtemp()))
        self.assertEqual(u.view(), {"state": "idle"})
        c = upgrade.Check("package", current="20261027-b285af5", latest="v2", available=True, notes="新东西", size=250 * 2**20)
        with mock.patch.object(upgrade, "check", return_value=c):
            self.assertTrue(u.check_async())
            until(lambda: u.state == "available")
        v = u.view()
        self.assertEqual((v["current"], v["latest"], v["notes"], v["size_mb"]), ("20261027-b285af5", "v2", "新东西", 250))
        quit_evt = threading.Event()
        with mock.patch.object(upgrade, "prepare", return_value=["new.exe", "upgrade", "--finish"]) as prep, \
                mock.patch.object(upgrade, "spawn") as spawn:
            self.assertTrue(u.install_async(quit_evt.set))
            self.assertTrue(quit_evt.wait(5))
        self.assertEqual(u.state, "restarting")
        prep.assert_called_once()
        spawn.assert_called_once_with(["new.exe", "upgrade", "--finish"], u.root)
        self.assertFalse(u.check_async())                 # 要重启了，不再查

    def test_errors(self):
        u = upgrade.Updater(Path(tempfile.mkdtemp()))
        with mock.patch.object(upgrade, "check", side_effect=RuntimeError("断网")):
            u.check_async()
            until(lambda: u.state == "error")
        self.assertEqual(u.view()["error"], "检查更新失败：断网")
        self.assertEqual(u.view()["mode"], "none")          # 联网失败也知道自己是哪种装法
        self.assertFalse(u.view()["available"])
        self.assertFalse(u.install_async(lambda: None))   # 没有可装的
        u.info = upgrade.Check("package", available=True)
        quit_evt = threading.Event()
        with mock.patch.object(upgrade, "prepare", side_effect=RuntimeError("下载断了")):
            u.install_async(quit_evt.set)
            until(lambda: u.error == "更新失败：下载断了")
        self.assertEqual(u.state, "error")
        self.assertFalse(quit_evt.is_set())
        self.assertTrue(u.view()["available"])            # 还能再试


class LiveTest(unittest.TestCase):
    def test_actions(self):
        quiet = mock.patch.object(upgrade, "_say")
        quiet.start()
        self.addCleanup(quiet.stop)
        live = make_live()
        live.updater = upgrade.Updater(Path(tempfile.mkdtemp()))
        self.assertEqual(live.snapshot()["upgrade"], {"state": "idle"})
        with self.assertRaises(ValueError):
            live.action({"type": "upgrade_install"})
        c = upgrade.Check("git", current="a", latest="b", available=True)
        with mock.patch.object(upgrade, "check", return_value=c):
            live.action({"type": "upgrade_check"})
            until(lambda: live.updater.state == "available")
        self.assertTrue(live.snapshot()["upgrade"]["available"])
        with mock.patch.object(upgrade, "prepare", return_value=["x"]), mock.patch.object(upgrade, "spawn"):
            live.action({"type": "upgrade_install"})
            self.assertTrue(live.quit_requested.wait(5))
        live.close()

    def test_quit_ends_electron(self):
        evt = threading.Event()
        proc = mock.Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("electron", 8)
        t = threading.Thread(target=webui._quit_when, args=(evt, proc))
        t.start()
        evt.set()
        t.join(5)
        proc.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
