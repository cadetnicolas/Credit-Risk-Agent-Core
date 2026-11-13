"""The self-contained setup page: template integrity, rendering, and the CLI.

Two bugs from this page's history are guarded here.

* An HTML comment inside the inline script contained a literal closing script
  tag; the browser ended the block there and rendered the remaining ~400 lines
  of JavaScript as body text. It looked like a blank form with a wall of code
  underneath — the kind of thing that ships because nobody opens the file.
* The form was *built by that script*. Anywhere the script did not run — most
  notably an embedded preview pane — the page showed a heading and nothing
  else, which reads as "this page cannot be filled in" rather than "this window
  cannot save". The form is now real HTML; these tests keep it that way.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

from credit_agent.config import setup_page, store

PAGE = setup_page.template_path()

PLACEHOLDERS = (
    "/*CONFIG_JSON*/",
    "/*FACTS_JSON*/",
    "/*NOTICES_HTML*/",
    "/*TABS_HTML*/",
    "/*PANELS_HTML*/",
    "/*LIMITS_HTML*/",
    "/*STATE_LINE*/",
    "/*ACTIONS_HTML*/",
    "/*SAVE_HINT_HTML*/",
    "/*FORM_OPEN*/",
    "/*FORM_CLOSE*/",
)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("CREDIT_AGENT_CONFIG", raising=False)
    store.reset_state()
    return tmp_path


def _page_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _script_blocks(html: str) -> list[str]:
    """The script bodies exactly as a browser would see them.

    ``HTMLParser`` treats ``<script>`` content as raw text, so a literal closing
    tag inside a comment ends the block — the same trap that once turned the rest
    of this page into visible code. Counting substrings would not reproduce it.
    """

    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.blocks: list[str] = []
            self._buf: list[str] = []
            self._inside = False

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self._inside = True
                self._buf = []

        def handle_endtag(self, tag):
            if tag == "script" and self._inside:
                self.blocks.append("".join(self._buf))
                self._inside = False

        def handle_data(self, data):
            if self._inside:
                self._buf.append(data)

    collector = Collector()
    collector.feed(html)
    return collector.blocks


# --------------------------------------------------------------------------- #
# Template integrity
# --------------------------------------------------------------------------- #
def test_the_inline_script_is_not_truncated():
    """The bug this guards: a ``</script>`` in a comment split the page in two."""
    blocks = _script_blocks(_page_text(PAGE))
    assert len(blocks) == 3, "expected config block, facts block, and the inline code"
    code = blocks[-1]
    assert 'getElementById("setup-form")' in code
    assert code.rstrip().endswith("})();")


def test_template_carries_every_placeholder():
    text = _page_text(PAGE)
    for token in PLACEHOLDERS:
        assert token in text, token


def test_page_has_no_external_dependency():
    """It must work from a file:// URL on a machine with no network."""
    text = _page_text(PAGE)
    for marker in ("http://", "https://", "//cdn", "<link "):
        assert marker not in text, marker


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _config():
    return store.normalize({
        "active_backend": "holo",
        "backends": {"holo": {"host": "hg.internal", "port": 80, "database": "db",
                              "user": "u", "password": "s3cret", "schema": "", "sslmode": ""}},
    })


def _render(config=None, **kwargs):
    """Render with the real facts, so limits and driver notices are present."""
    data = config or _config()
    return setup_page.render_page(data, store.page_facts(data), **kwargs)


def test_render_embeds_the_config_and_the_specs():
    page = _render()
    assert "credit-agent-config" in page
    parsed = store.extract_config_from_html(page)
    assert parsed["backends"]["holo"]["host"] == "hg.internal"


def test_render_never_leaks_a_closing_tag_from_a_password():
    data = _config()
    data["backends"]["holo"]["password"] = "x</script>y"
    page = _render(data)
    blocks = _script_blocks(page)
    assert len(blocks) == 3
    assert json.loads(blocks[0])["backends"]["holo"]["password"] == "x</script>y"


def test_the_form_is_real_html_not_built_by_script():
    """A viewer that blocks scripts must still show something to fill in."""
    page = _render()
    assert 'name="holo.host"' in page
    assert 'name="holo.password"' in page
    assert 'type="password"' in page
    assert page.count('class="tab-radio" type="radio" name="backend"') == len(setup_page.BACKEND_ORDER)
    assert 'name="limit.max_scan_rows"' in page


def test_every_required_field_is_marked_in_the_markup():
    page = _render()
    required = store.FIELD_SPEC["holo"]["required"]
    for name in required:
        assert f'name="holo.{name}"' in page
    assert page.count('class="req"') >= len(required)


def test_password_special_characters_survive_rendering():
    """An unescaped quote would truncate the value and fail a correct password."""
    data = _config()
    data["backends"]["holo"]["password"] = 'p&a"<b>'
    page = _render(data)
    assert 'value="p&amp;a&quot;&lt;b&gt;"' in page


def test_render_leaves_no_placeholder_behind():
    page = _render()
    for token in PLACEHOLDERS:
        assert token not in page, token


def test_file_mode_does_not_wrap_the_form():
    """No action to post to: Enter would reload and lose what was typed."""
    page = _render()
    assert '<div id="setup-form">' in page
    assert 'action="/save"' not in page
    assert 'id="btn-save"' in page


def test_serve_mode_uses_a_plain_form_post():
    page = _render(mode="serve")
    assert '<form id="setup-form" method="post" action="/save"' in page
    assert 'form="setup-form"' in page
    assert 'id="btn-save"' not in page
    assert not page.count("</form>") == 0


def test_file_mode_says_which_path_to_save_to(tmp_path):
    """The runtime directory is hidden; a plain save lands in Downloads.

    Naming the real path once, where it can be pasted into the dialog, is the
    difference between "saved and working" and "saved and silently ignored".
    """
    data = _config()
    target = tmp_path / "setup.html"
    page = setup_page.render_page(data, setup_page._facts_for(data, target))
    assert 'class="save-hint"' in page
    assert f"<code>{target}</code>" in page
    assert "文件名" in page


def test_the_save_hint_is_hidden_until_the_script_runs():
    """Advice about a save dialog is noise on a page that cannot reach one."""
    text = _page_text(PAGE)
    assert ".save-hint { display: none; }" in text
    assert 'body[data-js="on"] .save-hint' in text


def test_the_save_hint_stays_out_of_serve_mode():
    """There the server writes the file, so there is nothing to point at."""
    assert 'class="save-hint"' not in _render(mode="serve")


def test_the_save_hint_escapes_the_path():
    data = _config()
    hint = setup_page._render_save_hint("file", {"source_path": "/tmp/<weird>/setup.html"})
    assert "&lt;weird&gt;" in hint


def test_saving_keeps_the_tab_that_was_selected():
    """A clicked radio changes the property, not the attribute.

    Without the reflection, the saved page would reopen on the tab it was
    generated with and the next save would quietly rewrite ``active_backend`` to
    that stale value — a silent switch of database at save time.
    """
    code = _script_blocks(_page_text(PAGE))[-1]
    assert 'querySelectorAll(\'input[name="backend"]\')' in code
    assert 'setAttribute("checked", "checked")' in code


def test_the_save_dialog_is_asked_to_remember_its_folder():
    """``id`` is what makes Chrome reopen the same directory next time."""
    code = _script_blocks(_page_text(PAGE))[-1]
    assert 'showSaveFilePicker' in code
    assert 'id: "credit-agent-setup"' in code


def test_a_browser_without_write_access_falls_back_to_adopt():
    code = _script_blocks(_page_text(PAGE))[-1]
    assert "credit-risk-config --adopt" in code


def test_the_warning_banner_stays_until_script_hides_it():
    """CSS, not JS, hides it — otherwise a blocked script leaves no explanation."""
    text = _page_text(PAGE)
    assert 'body[data-js="on"] .banner { display: none; }' in text
    assert 'document.body.setAttribute("data-js", "on")' in _script_blocks(_page_text(PAGE))[-1]


def test_tabs_work_without_script():
    """Radio + :checked, so switching databases needs no JS either."""
    page = _render()
    assert page.count('class="tab-radio"') == len(setup_page.BACKEND_ORDER)
    assert 'id="tab-holo" value="holo" checked' in page


# --------------------------------------------------------------------------- #
# Reading the form back
# --------------------------------------------------------------------------- #
def test_form_round_trip_keeps_every_field():
    data = setup_page.config_from_form({
        "backend": ["mysql"],
        "mysql.host": ["rm.internal"],
        "mysql.port": ["3306"],
        "mysql.password": ['p&a"s<s'],
        "limit.max_scan_rows": ["1000"],
        "limit.connect_timeout_seconds": [""],
        "__action": ["save"],
    })
    assert data["active_backend"] == "mysql"
    normalized = store.normalize(data, strict=True)
    assert normalized["backends"]["mysql"]["host"] == "rm.internal"
    assert normalized["backends"]["mysql"]["password"] == 'p&a"s<s'
    assert normalized["limits"]["max_scan_rows"] == 1000
    # Blank means "use the default", not zero.
    assert normalized["limits"]["connect_timeout_seconds"] is None


def test_write_config_persists_what_was_submitted(sandbox):
    data = store.normalize({
        "active_backend": "postgres",
        "backends": {"postgres": {"host": "pg.internal", "port": 5432,
                                  "database": "credit", "user": "u", "password": "p"}},
    })
    target = setup_page.write_config(data, sandbox / "setup.html")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    loaded = store.load_config_file()
    assert loaded["data"]["active_backend"] == "postgres"
    assert loaded["data"]["backends"]["postgres"]["host"] == "pg.internal"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_write_page_is_owner_only_and_reloads(sandbox):
    result = setup_page.write_page(check=False)
    path = result["path"]
    assert path == sandbox / "setup.html"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    loaded = store.load_config_file()
    assert loaded["kind"] == "html"
    assert loaded["data"]["active_backend"] == "duckdb"


def test_first_page_mirrors_what_is_already_configured(sandbox, monkeypatch):
    """Otherwise pressing Save on an untouched page would wipe a working .env."""
    from credit_agent.config.env_schema import reset_env_config

    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "mysql")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_HOST", "my.rds")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_DATABASE", "credit")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_USER", "reader")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_PASSWORD", "pw")
    reset_env_config()

    setup_page.write_page(check=False)
    loaded = store.load_config_file()
    reset_env_config()

    assert loaded["data"]["active_backend"] == "mysql"
    assert loaded["data"]["backends"]["mysql"]["host"] == "my.rds"


def test_an_incomplete_config_skips_the_connection_probe(sandbox):
    store.save_config_file(store.normalize({"active_backend": "holo",
                                            "backends": {"holo": {"host": "h"}}}))
    result = setup_page.write_page(sandbox / "setup.html", check=True)
    assert result["check"]["ran"] is False
    assert result["check"]["reason"] == "incomplete"
    assert "密码" in result["check"]["missing"]


def test_the_probe_uses_a_short_timeout(sandbox):
    """A wrong host must not turn the command into a 30-second hang."""
    store.save_config_file(_config())
    result = setup_page.write_page(sandbox / "setup.html", check=True)
    assert result["check"].get("timeout_seconds") == setup_page.QUICK_CHECK_TIMEOUT


# --------------------------------------------------------------------------- #
# Local write channel
# --------------------------------------------------------------------------- #
@pytest.fixture()
def server(sandbox):
    target = sandbox / "setup.html"
    setup_page.write_config(store.default_config(), target)
    httpd = setup_page.build_server(target, port=0, tries=1)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, target
    finally:
        httpd.shutdown()
        httpd.server_close()


def _base(httpd) -> str:
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def test_the_server_serves_a_fillable_form(server):
    httpd, _ = server
    with urllib.request.urlopen(_base(httpd) + "/", timeout=5) as response:
        page = response.read().decode("utf-8")
    assert response.status == 200
    assert 'action="/save"' in page
    assert 'name="postgres.host"' in page


def test_a_posted_form_reaches_the_file(server):
    httpd, target = server
    body = urllib.parse.urlencode({
        "backend": "mysql",
        "mysql.host": "rm.internal",
        "mysql.port": "3306",
        "mysql.database": "credit",
        "mysql.user": "analyst",
        "mysql.password": "p&a<s",
        "limit.max_scan_rows": "500000",
        "__action": "save",
    }).encode()
    request = urllib.request.Request(
        _base(httpd) + "/save", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        page = response.read().decode("utf-8")

    assert "已写入" in page
    stored = store.extract_config_from_html(target.read_text(encoding="utf-8"))
    assert stored["active_backend"] == "mysql"
    assert stored["backends"]["mysql"]["host"] == "rm.internal"
    assert stored["backends"]["mysql"]["password"] == "p&a<s"
    assert stored["limits"]["max_scan_rows"] == 500000


def test_a_rejected_value_does_not_overwrite_the_file(server):
    httpd, target = server
    before = target.read_text(encoding="utf-8")
    body = urllib.parse.urlencode({
        "backend": "mysql",
        "mysql.host": "rm.internal",
        "limit.max_scan_rows": "99999999999",
        "__action": "save",
    }).encode()
    request = urllib.request.Request(
        _base(httpd) + "/save", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        page = response.read().decode("utf-8")

    assert "需要在" in page
    assert target.read_text(encoding="utf-8") == before


def test_the_server_only_answers_on_loopback(server):
    httpd, _ = server
    assert httpd.server_address[0] == "127.0.0.1"


# --------------------------------------------------------------------------- #
# Adopting a downloaded page
# --------------------------------------------------------------------------- #
def test_adopt_installs_the_newest_download(sandbox, monkeypatch, tmp_path_factory):
    downloads = tmp_path_factory.mktemp("downloads")
    older = downloads / "setup.html"
    newer = downloads / "setup (1).html"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    target = setup_page.adopt_download(downloads)
    assert target == sandbox / "setup.html"
    assert target.read_text(encoding="utf-8") == "new"


def test_adopt_keeps_a_backup_of_the_previous_page(sandbox, monkeypatch, tmp_path_factory):
    target = sandbox / "setup.html"
    target.write_text("previous", encoding="utf-8")
    downloads = tmp_path_factory.mktemp("downloads")
    (downloads / "setup.html").write_text("newer", encoding="utf-8")

    setup_page.adopt_download(downloads)
    assert (sandbox / "setup.html.bak").read_text(encoding="utf-8") == "previous"


def test_adopt_reports_when_nothing_was_downloaded(sandbox, tmp_path_factory):
    empty = tmp_path_factory.mktemp("empty")
    assert setup_page.adopt_download(empty) is None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_generates_the_page(sandbox, capsys):
    code = setup_page.main(["--no-open", "--no-check"])
    out = capsys.readouterr().out
    assert code == 0
    assert (sandbox / "setup.html").is_file()
    assert "配置页已生成" in out


def test_cli_mentions_the_serve_escape_hatch(sandbox, capsys):
    """The whole point: a page that cannot save must say what to do instead."""
    setup_page.main(["--no-open", "--no-check"])
    assert "credit-risk-config --serve" in capsys.readouterr().out


def test_opening_prefers_a_browser_that_can_write_the_file_back(tmp_path, monkeypatch):
    """Safari can fill this form but only download it afterwards.

    Handing the page to the system default hides that difference until after the
    person has already typed everything in, so a Chromium binary wins when one
    is installed.
    """
    page = tmp_path / "setup.html"
    page.write_text("<html></html>", encoding="utf-8")
    launched: list[list[str]] = []

    monkeypatch.setattr(setup_page, "_chromium", lambda: "/fake/chrome")
    monkeypatch.setattr(setup_page.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(setup_page.webbrowser, "open", lambda uri: pytest.fail("default browser used"))

    assert setup_page._open_browser(page) is True
    assert launched == [["/fake/chrome", page.as_uri()]]


def test_opening_falls_back_to_the_system_default(tmp_path, monkeypatch):
    page = tmp_path / "setup.html"
    page.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(setup_page, "_chromium", lambda: None)
    monkeypatch.setattr(setup_page.webbrowser, "open", lambda uri: True)
    assert setup_page._open_browser(page) is True


def test_cli_print_redacts_the_password(sandbox, capsys):
    store.save_config_file(_config())
    code = setup_page.main(["--print", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "s3cret" not in json.dumps(payload, ensure_ascii=False)
    assert payload["config"]["backends"]["holo"]["password"] == "已设置"


def test_cli_reports_an_unreadable_file_instead_of_crashing(sandbox, capsys):
    (sandbox / "config.json").write_text("{ broken", encoding="utf-8")
    code = setup_page.main(["--print", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["skipped"]


def test_cli_adopt_without_a_download_exits_nonzero(sandbox, capsys, monkeypatch, tmp_path_factory):
    monkeypatch.setattr(setup_page.Path, "home", classmethod(lambda cls: tmp_path_factory.mktemp("home")))
    code = setup_page.main(["--adopt"])
    assert code == 1
    assert "没有找到" in capsys.readouterr().out
