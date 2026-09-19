import json
import os

import pytest


class TestLoadCookiesFromFile:
    def test_load_valid_file(self, tmp_path):
        from platform_video_downloader.browser import load_cookies_from_file
        cookies = [
            {"name": "SESSDATA", "value": "abc", "domain": ".bilibili.com", "path": "/"},
            {"name": "bili_jct", "value": "def", "domain": ".bilibili.com", "path": "/"},
        ]
        path = str(tmp_path / "cookies.json")
        with open(path, "w") as f:
            json.dump(cookies, f)
        result = load_cookies_from_file(path)
        assert result == cookies

    def test_missing_file_returns_none(self):
        from platform_video_downloader.browser import load_cookies_from_file
        result = load_cookies_from_file("/nonexistent/path/cookies.json")
        assert result is None

    def test_corrupt_file_returns_none(self, tmp_path):
        from platform_video_downloader.browser import load_cookies_from_file
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")
        result = load_cookies_from_file(path)
        assert result is None

    def test_non_list_returns_none(self, tmp_path):
        from platform_video_downloader.browser import load_cookies_from_file
        path = str(tmp_path / "wrong_type.json")
        with open(path, "w") as f:
            json.dump({"not": "a list"}, f)
        result = load_cookies_from_file(path)
        assert result is None


class TestSaveCookiesToFile:
    def test_save_and_roundtrip(self, tmp_path):
        from platform_video_downloader.browser import save_cookies_to_file, load_cookies_from_file
        cookies = [{"name": "SESSDATA", "value": "test123", "domain": ".bilibili.com", "path": "/"}]
        path = str(tmp_path / "cache" / "cookies.json")
        save_cookies_to_file(cookies, path)
        assert os.path.isfile(path)
        loaded = load_cookies_from_file(path)
        assert loaded == cookies


class TestBuildCookiesFromEnv:
    def test_sessdata_from_env(self, monkeypatch):
        from platform_video_downloader.browser import build_cookies_from_env
        monkeypatch.setenv("BILIBILI_SESSDATA", "env_sessdata")
        cookies = build_cookies_from_env()
        assert len(cookies) == 1
        assert cookies[0]["name"] == "SESSDATA"
        assert cookies[0]["value"] == "env_sessdata"
        assert cookies[0]["domain"] == ".bilibili.com"

    def test_both_env_vars(self, monkeypatch):
        from platform_video_downloader.browser import build_cookies_from_env
        monkeypatch.setenv("BILIBILI_SESSDATA", "s1")
        monkeypatch.setenv("BILIBILI_BILI_JCT", "j1")
        cookies = build_cookies_from_env()
        assert len(cookies) == 2
        assert {c["name"] for c in cookies} == {"SESSDATA", "bili_jct"}

    def test_no_env_vars(self, monkeypatch):
        from platform_video_downloader.browser import build_cookies_from_env
        monkeypatch.delenv("BILIBILI_SESSDATA", raising=False)
        monkeypatch.delenv("BILIBILI_BILI_JCT", raising=False)
        cookies = build_cookies_from_env()
        assert cookies == []


class TestResolveCookies:
    def test_cli_cookies_take_priority(self):
        from platform_video_downloader.cli.main import _resolve_cookies
        class Args:
            no_cache = False
        cli = [{"name": "SESSDATA", "value": "cli_val", "domain": ".bilibili.com", "path": "/"}]
        cookies, needs_qr = _resolve_cookies(cli, Args())
        assert cookies == cli
        assert needs_qr is False

    def test_env_cookies_when_no_cli(self, monkeypatch):
        from platform_video_downloader.cli.main import _resolve_cookies
        monkeypatch.setenv("BILIBILI_SESSDATA", "env_val")
        class Args:
            no_cache = False
        cookies, needs_qr = _resolve_cookies([], Args())
        assert len(cookies) == 1
        assert needs_qr is False

    def test_no_cache_skips_file(self, tmp_path, monkeypatch):
        from platform_video_downloader.cli.main import _resolve_cookies
        from platform_video_downloader.config import DEFAULT_COOKIE_CACHE_PATH
        monkeypatch.delenv("BILIBILI_SESSDATA", raising=False)
        monkeypatch.delenv("BILIBILI_BILI_JCT", raising=False)
        class Args:
            no_cache = True
        cookies, needs_qr = _resolve_cookies([], Args())
        assert cookies == []
        assert needs_qr is True

    def test_qr_login_when_nothing_available(self, monkeypatch, tmp_path):
        from platform_video_downloader.cli.main import _resolve_cookies
        from unittest.mock import patch
        monkeypatch.delenv("BILIBILI_SESSDATA", raising=False)
        monkeypatch.delenv("BILIBILI_BILI_JCT", raising=False)
        fake_path = str(tmp_path / "nonexistent_cookies.json")
        with patch("platform_video_downloader.cli.main.DEFAULT_COOKIE_CACHE_PATH", fake_path):
            class Args:
                no_cache = False
            cookies, needs_qr = _resolve_cookies([], Args())
        assert cookies == []
        assert needs_qr is True


class TestKillOrphanProcesses:
    def test_noop_on_non_windows(self):
        """非 Windows 平台不得起任何子进程。"""
        import platform_video_downloader.browser as browser_mod
        from unittest.mock import patch

        def fail_run(*args, **kwargs):
            raise AssertionError("subprocess.run should not be called on non-Windows")

        b = browser_mod.PlaywrightBrowser()
        with patch.object(browser_mod.sys, "platform", "linux"), \
             patch.object(browser_mod.subprocess, "run", side_effect=fail_run):
            b._kill_orphan_processes()  # 不应抛错也不应调用 subprocess

    def test_kills_children_via_powershell_on_windows(self):
        import platform_video_downloader.browser as browser_mod
        from unittest.mock import patch

        b = browser_mod.PlaywrightBrowser()
        ps_output = "1234\r\n5678\r\n"
        with patch.object(browser_mod.subprocess, "run") as mock_run, \
             patch.object(browser_mod.sys, "platform", "win32"):
            mock_run.return_value.stdout = ps_output
            b._kill_orphan_processes()

        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("powershell" in c for c in cmds), "应使用 PowerShell 枚举子进程"
        assert any("taskkill" in c for c in cmds), "应使用 taskkill 击杀子进程"
