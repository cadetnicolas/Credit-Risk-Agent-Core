"""Generate the self-contained configuration page and read it back.

The page is the whole interface: it carries its own settings inside an inert
``<script id="credit-agent-config" type="application/json">`` block, so there is
no second file to keep in sync. This module

* renders the form in Python — every tab, field and limit is real HTML, not
  something a script builds at load time. A viewer that blocks scripts then
  still shows a fillable form instead of an empty shell, which is the difference
  between "the page is broken" and "this window cannot save";
* optionally runs a bounded connection check so the page can say "this worked
  last time" instead of leaving the person guessing;
* serves the same page over ``127.0.0.1`` when the browser will not write back
  to disk, with plain form posts so saving needs no script either;
* picks a downloaded page back up when the browser could not write in place.

Everything else — validation, precedence, projection into ``os.environ`` — lives
in :mod:`credit_agent.config.store`.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .paths import get_runtime_root
from .store import (
    BACKEND_ORDER,
    CONFIG_FILE_VAR,
    FIELD_SPEC,
    HTML_CONFIG_NAME,
    ConfigError,
    atomic_write,
    config_file_path,
    default_config,
    extract_config_from_html,
    load_config_file,
    missing_required,
    normalize,
    page_facts,
    redacted_config,
    seed_from_environment,
    test_connection,
)

#: Bounded so a wrong host does not turn `credit-risk-config` into a 30s hang.
QUICK_CHECK_TIMEOUT = 8

#: First port tried by ``--serve``; the next 19 are fallbacks.
DEFAULT_PORT = 8765

_TEMPLATE_NAME = "setup.html"


def template_path() -> Path:
    """Locate the bundled page.

    Two locations: the source tree next to this module (editable install, which
    is how the project is normally run) and ``share/credit-risk-agent`` for a
    wheel install, mirroring how ``skills/`` is packaged.
    """
    here = Path(__file__).resolve().parent / _TEMPLATE_NAME
    if here.is_file():
        return here
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "share" / "credit-risk-agent" / "config" / _TEMPLATE_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"找不到配置页模板 {_TEMPLATE_NAME}，安装可能不完整")


# --------------------------------------------------------------------------- #
# HTML fragments
# --------------------------------------------------------------------------- #
def _e(value: Any) -> str:
    """Escape for both attribute and text context.

    Passwords routinely contain ``&``, ``"`` and ``<``; an unescaped quote there
    silently truncates the value and produces "wrong password" for a password
    that was typed correctly.
    """
    return _html.escape("" if value is None else str(value), quote=True)


def _badge(backend: str, missing: List[str]) -> Tuple[str, str]:
    if missing:
        return "warn", f"还缺 {len(missing)} 项"
    return "ok", "无需凭证" if backend == "duckdb" else "已填全"


def _render_field(backend: str, field: Dict[str, Any], value: Any, *,
                  required: bool, origin: str, wide: bool) -> str:
    field_id = f"f-{backend}-{field['name']}"
    attrs = (
        f'id="{field_id}" name="{backend}.{field["name"]}" '
        f'data-backend="{backend}" data-field="{field["name"]}"'
    )
    placeholder = field.get("placeholder")

    label = f'<label for="{field_id}"><span>{_e(field["label"])}</span>'
    if required:
        label += '<span class="req">*</span>'
    label += "</label>"

    kind = field.get("kind", "text")
    if kind == "select":
        current = "" if value is None else str(value)
        options = []
        for option in field.get("options") or [""]:
            chosen = " selected" if str(option) == current else ""
            text = "（不指定，用驱动默认）" if option == "" else option
            options.append(f'<option value="{_e(option)}"{chosen}>{_e(text)}</option>')
        control = f'<select {attrs}>{"".join(options)}</select>'
    elif kind == "secret":
        control = (
            '<div class="pw-wrap">'
            f'<input type="password" {attrs} value="{_e(value)}" '
            f'placeholder="{_e(placeholder)}" autocomplete="new-password" spellcheck="false">'
            '<button class="pw-toggle" type="button">显示</button>'
            "</div>"
        )
    elif kind == "int":
        control = (
            f'<input type="number" {attrs} value="{_e(value)}" min="1" max="65535" '
            f'placeholder="{_e(placeholder)}" inputmode="numeric" autocomplete="off">'
        )
    else:
        control = (
            f'<input type="text" {attrs} value="{_e(value)}" '
            f'placeholder="{_e(placeholder)}" autocomplete="off" spellcheck="false">'
        )

    hint = ""
    if origin == "process_env":
        hint = ('<div class="hint warn">当前生效值来自环境变量或 mcp.json，'
                "会覆盖这里保存的值</div>")
    return f'<div class="field{" wide" if wide else ""}">{label}{control}{hint}</div>'


def _render_panel(backend: str, values: Dict[str, Any], missing: List[str],
                  origins: Dict[str, str]) -> str:
    spec = FIELD_SPEC[backend]
    wide = len(spec["fields"]) == 1
    fields_html = "".join(
        _render_field(
            backend,
            field,
            values.get(field["name"]),
            required=field["name"] in spec["required"],
            origin=origins.get(f"{backend}.{field['name']}", ""),
            wide=wide,
        )
        for field in spec["fields"]
    )
    kind, text = _badge(backend, missing)
    return (
        f'\n  <section class="card panel" id="panel-{backend}" data-backend="{backend}">'
        '\n    <div class="card-head">'
        "\n      <div>"
        f'\n        <h2>{_e(spec["label"])} 连接信息</h2>'
        f'\n        <div class="desc">{_e(spec["summary"])}</div>'
        "\n      </div>"
        f'\n      <span class="pill {kind}" data-badge>{_e(text)}</span>'
        "\n    </div>"
        f'\n    <div class="card-body"><div class="grid">{fields_html}</div></div>'
        "\n  </section>"
    )


def _render_tab(backend: str, active: str, complete: bool) -> str:
    spec = FIELD_SPEC[backend]
    checked = " checked" if backend == active else ""
    return (
        f'<input class="tab-radio" type="radio" name="backend" id="tab-{backend}" '
        f'value="{backend}"{checked}>'
        f'<label class="tab" for="tab-{backend}">'
        f'<span class="dot {"on" if complete else "off"}"></span>'
        f'<span>{_e(spec["label"])}</span></label>'
    )


def _render_limits(data: Dict[str, Any], facts: Dict[str, Any]) -> str:
    stored = data.get("limits") or {}
    out = []
    for name, meta in (facts.get("limits") or {}).items():
        value = stored.get(name)
        out.append(
            '<div class="field">'
            f'<label for="lim-{name}">{_e(meta["label"])}</label>'
            f'<input type="number" id="lim-{name}" name="limit.{name}" data-limit="{name}" '
            f'value="{_e("" if value is None else value)}" '
            f'min="{meta["low"]}" max="{meta["high"]}" inputmode="numeric">'
            f'<div class="hint">留空 = {_e(meta["effective"])}（默认值）</div>'
            "</div>"
        )
    return "".join(out)


def _render_notices(facts: Dict[str, Any]) -> str:
    parts: List[str] = []
    for text in facts.get("warnings") or []:
        parts.append(
            f'<div class="notice warn"><span class="ico">!</span><div>{_e(text)}</div></div>'
        )
    for text in facts.get("skipped") or []:
        parts.append(
            f'<div class="notice err"><span class="ico">×</span>'
            f"<div>已跳过：{_e(text)}</div></div>"
        )
    for item in facts.get("extra_notices") or []:
        parts.append(
            f'<div class="notice {item["kind"]}"><span class="ico">{item["icon"]}</span>'
            f'<div>{item["html"]}</div></div>'
        )

    driver = (facts.get("drivers") or {}).get(facts.get("active_backend") or "") or {}
    if driver.get("installed") is False:
        parts.append(
            '<div class="notice warn"><span class="ico">!</span><div>'
            "<strong>驱动未安装。</strong>先运行 "
            f'<code>{_e(driver.get("hint"))}</code>，否则连接会直接失败。</div></div>'
        )
    return "".join(parts)


def _render_actions(mode: str) -> str:
    if mode == "serve":
        # Plain submit buttons: the server writes the file, so this path works
        # with scripting disabled entirely.
        return (
            '<button class="btn" type="submit" form="setup-form" formaction="/test" '
            'name="__action" value="test">测试连接</button>'
            '<button class="btn primary" type="submit" form="setup-form" '
            'name="__action" value="save">保存配置</button>'
        )
    return (
        '<button class="btn" id="btn-copy" type="button">复制 JSON</button>'
        '<button class="btn primary" id="btn-save" type="button">保存配置</button>'
    )


def _render_save_hint(mode: str, facts: Dict[str, Any]) -> str:
    """Say where the save dialog should be pointed, in one paste.

    The runtime directory is hidden, so a plain save lands in Downloads and
    "I saved it but nothing changed" is the predictable result. Save dialogs
    accept a full path in the name field, which turns that into one paste.
    """
    if mode != "file":
        # The server writes the file; there is nothing to point anywhere.
        return ""
    path = str(facts.get("source_path") or "~/.credit-agent/setup.html")
    return (
        '<div class="save-hint">'
        "<div>首次保存时，把完整路径 <code>" + _e(path) + "</code> 粘进保存对话框的"
        "「文件名」栏再回车，就直接覆盖本页；Chrome 会记住这个位置，之后一键保存。</div>"
        "</div>"
    )


def _state_line(data: Dict[str, Any], active: str, missing: List[str]) -> str:
    label = FIELD_SPEC.get(active, {}).get("label", active)
    if not missing:
        return f"{label} 必填项已填全，记得点右侧保存"
    return f"{label} 还缺：{'、'.join(missing)}"


def render_page(config: Dict[str, Any], facts: Dict[str, Any], *,
                mode: str = "file") -> str:
    """Fill the template. ``mode`` only decides how the form submits."""
    from .store import json_for_html  # noqa: PLC0415 - keep the public surface small

    data = config
    active = str(data.get("active_backend") or "duckdb")
    if active not in FIELD_SPEC:
        active = "duckdb"

    facts = dict(facts)
    facts["active_backend"] = active
    facts["mode"] = mode
    origins = facts.get("origins") or {}

    tabs: List[str] = []
    panels: List[str] = []
    for backend in BACKEND_ORDER:
        values = (data.get("backends") or {}).get(backend) or {}
        missing = missing_required(data, backend)
        tabs.append(_render_tab(backend, active, not missing))
        panels.append(_render_panel(backend, values, missing, origins))

    if mode == "serve":
        form_open = '<form id="setup-form" method="post" action="/save" autocomplete="off">'
        form_close = "</form>"
    else:
        # Deliberately not a <form>: with no action to post to, Enter would
        # reload the page and throw away what was typed.
        form_open = '<div id="setup-form">'
        form_close = "</div>"

    replacements = {
        "/*CONFIG_JSON*/": json_for_html(data),
        "/*FACTS_JSON*/": json_for_html(facts),
        "/*NOTICES_HTML*/": _render_notices(facts),
        "/*TABS_HTML*/": "".join(tabs),
        "/*PANELS_HTML*/": "".join(panels),
        "/*LIMITS_HTML*/": _render_limits(data, facts),
        "/*STATE_LINE*/": _e(_state_line(data, active, missing_required(data, active))),
        "/*ACTIONS_HTML*/": _render_actions(mode),
        "/*SAVE_HINT_HTML*/": _render_save_hint(mode, facts),
        "/*FORM_OPEN*/": form_open,
        "/*FORM_CLOSE*/": form_close,
    }

    text = template_path().read_text(encoding="utf-8")
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


def _verdict_notices(verdict: Dict[str, Any]) -> List[Dict[str, str]]:
    """Turn a connection probe into banner text."""
    if not verdict.get("ran"):
        if verdict.get("reason") == "incomplete":
            missing = "、".join(verdict.get("missing") or [])
            return [{"kind": "warn", "icon": "!",
                     "html": _e(f"没有测试连接：还缺 {missing}")}]
        return []
    if verdict.get("ok"):
        target = verdict.get("target") or ""
        readonly = "服务端只读" if verdict.get("server_side_read_only") else "仅 SQL 文本审计"
        return [{"kind": "ok", "icon": "✓",
                 "html": _e(f"连接成功：{target}（{readonly}）")}]
    hint = verdict.get("hint") or ""
    body = _e(f"连接失败：{verdict.get('error', '未知原因')}")
    if verdict.get("missing_driver"):
        body += f'<br>先装驱动：<code>{_e(verdict["missing_driver"])}</code>'
    if hint:
        body += f"<br>{_e(hint)}"
    return [{"kind": "err", "icon": "×", "html": body}]


def _facts_for(data: Dict[str, Any], target: Path, *,
               extra: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    facts = page_facts(data, {"path": str(target), "exists": True})
    facts["source_path"] = str(target)
    facts["exists"] = True
    facts["extra_notices"] = extra or []
    return facts


def write_config(data: Dict[str, Any], path: Optional[Path] = None, *,
                 mode: str = "file",
                 extra: Optional[List[Dict[str, str]]] = None) -> Path:
    """Render ``data`` and write it to the page. Never leaves a half-written file."""
    target = Path(path) if path else config_file_path()
    text = render_page(data, _facts_for(data, target, extra=extra), mode=mode)
    atomic_write(target, text, mode=0o600)
    return target


def write_page(path: Optional[Path] = None, *,
               check: bool = True) -> Dict[str, Any]:
    """Render the page from the saved configuration and write it out.

    ``check`` runs a real connection attempt with a short timeout. It is on by
    default because "did my password work" is the only question the page cannot
    answer on its own, and answering it late is cheaper than answering it never.
    """
    loaded = load_config_file()
    data = loaded["data"]
    if not loaded["exists"]:
        # First run: mirror what is in force right now, so opening the page and
        # pressing Save changes nothing for someone who configured via .env.
        data = seed_from_environment(data)

    verdict: Dict[str, Any] = {"ran": False}
    if check:
        backend = data["active_backend"]
        missing = missing_required(data, backend)
        if missing:
            verdict = {"ran": False, "reason": "incomplete", "missing": missing}
        else:
            probe = json.loads(json.dumps(data))
            probe.setdefault("limits", {})["connect_timeout_seconds"] = QUICK_CHECK_TIMEOUT
            result = test_connection(probe, list_tables=False)
            result["ran"] = True
            result["timeout_seconds"] = QUICK_CHECK_TIMEOUT
            verdict = result

    target = Path(path) if path else config_file_path()
    notices = _verdict_notices(verdict)
    facts = _facts_for(data, target, extra=notices)
    facts["check"] = verdict
    atomic_write(target, render_page(data, facts, mode="file"), mode=0o600)
    return {"path": target, "check": verdict, "config": data, "facts": facts}


# --------------------------------------------------------------------------- #
# Local write channel
# --------------------------------------------------------------------------- #
def config_from_form(fields: Dict[str, List[str]]) -> Dict[str, Any]:
    """Rebuild the config object from posted form fields.

    Field names carry their backend (``holo.host``) and limits carry a ``limit.``
    prefix. Keying off the name means the form needs no JavaScript to submit and
    no schema can drift out of sync with the rendered inputs.
    """
    data: Dict[str, Any] = {"version": 1, "active_backend": "duckdb",
                            "backends": {}, "limits": {}}
    for key, values in fields.items():
        value = values[-1] if values else ""
        if key in ("__action", "submit"):
            continue
        if key == "backend":
            if value:
                data["active_backend"] = value
        elif key.startswith("limit."):
            # A blank limit means "use the default"; storing an empty string
            # would round-trip as 0 and make the cap look enormous.
            if value.strip():
                data["limits"][key.split(".", 1)[1]] = value
        elif "." in key:
            backend, _, name = key.partition(".")
            data["backends"].setdefault(backend, {})[name] = value
    return data


def _data_at(target: Path) -> Dict[str, Any]:
    """Read the page being edited, not whatever the tool would load first."""
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError:
        return default_config()
    raw = extract_config_from_html(text)
    return normalize(raw) if raw else default_config()


def _make_handler(target: Path) -> type:
    """Request handler bound to one page file.

    A factory rather than a module-level class so the target path is a closure,
    and so a test can drive the exact handler the CLI runs.
    """
    from http.server import BaseHTTPRequestHandler  # noqa: PLC0415

    class Handler(BaseHTTPRequestHandler):
        server_version = "credit-risk-config"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
            # Silent on purpose: request logs would push the "how to use this"
            # lines off the terminal within a few reloads.
            return

        def _send(self, body: str, status: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parts = urlsplit(self.path)
            if parts.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            extra: List[Dict[str, str]] = []
            if parse_qs(parts.query).get("saved"):
                extra.append({
                    "kind": "ok", "icon": "✓",
                    "html": _e(f"已写入 {target}。重启客户端后生效。"),
                })
            data = _data_at(target)
            self._send(render_page(data, _facts_for(data, target, extra=extra),
                                   mode="serve"))

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            fields = parse_qs(body, keep_blank_values=True)
            raw = config_from_form(fields)
            action = (fields.get("__action") or ["save"])[-1]

            try:
                data = normalize(raw, strict=True)
            except ConfigError as exc:
                # Show what was typed, plus the reason it was refused; dropping
                # back to the last saved values would hide the typo.
                loose = normalize(raw)
                self._send(render_page(
                    loose, _facts_for(loose, target, extra=[
                        {"kind": "err", "icon": "×", "html": _e(str(exc))}
                    ]), mode="serve"))
                return

            if parts.path == "/test" or action == "test":
                verdict = test_connection(data, list_tables=False)
                notices = _verdict_notices(verdict) or [
                    {"kind": "warn", "icon": "!", "html": "没有执行连接测试"}
                ]
                self._send(render_page(data, _facts_for(data, target, extra=notices),
                                       mode="serve"))
                return

            write_config(data, target, mode="file")
            self._redirect("/?saved=1")

    return Handler


def build_server(path: Optional[Path] = None, *, port: int = DEFAULT_PORT,
                 tries: int = 20) -> Any:
    """Bind on loopback and return a server ready for ``serve_forever``.

    ``port=0`` asks the OS for a free port — used by the tests so they never
    fight over a fixed number.
    """
    from http.server import ThreadingHTTPServer  # noqa: PLC0415

    target = Path(path) if path else config_file_path()
    handler = _make_handler(target)
    attempts = max(1, tries)
    last_error: Optional[BaseException] = None
    for candidate in range(port, port + attempts):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError as exc:  # port taken, try the next one
            last_error = exc
    raise RuntimeError(f"端口 {port}–{port + attempts - 1} 都不可用：{last_error}")


def serve(path: Optional[Path] = None, *, port: int = DEFAULT_PORT,
          open_browser: bool = True) -> int:
    """Serve the page on loopback and write back on submit.

    Needed because a page opened from ``file://`` cannot reliably write to disk:
    ``showSaveFilePicker`` is unavailable or blocked, and the download fallback
    asks the person to move a file into a hidden directory. A form post to
    ``127.0.0.1`` has neither problem, and works with scripting switched off.
    """
    target = Path(path) if path else config_file_path()
    httpd = build_server(target, port=port)

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    # flush: the address is the whole instruction, and a buffered print does not
    # appear until the server exits — which is the one moment it is useless.
    print(f"配置服务已启动：{url}", flush=True)
    print(f"保存后写到：{target}", flush=True)
    print("填完点「保存配置」，然后回来按 Ctrl+C 退出服务。", flush=True)
    print(flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出配置服务。")
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# Recovery and CLI
# --------------------------------------------------------------------------- #
def adopt_download(directory: Optional[Path] = None) -> Optional[Path]:
    """Move a freshly downloaded page into the runtime root.

    Browsers that refuse the File System Access API fall back to a download, and
    "now put it in the right folder" is exactly the step non-technical users drop.
    This picks the newest matching file and installs it, keeping a copy of the
    previous one so a wrong choice is recoverable.
    """
    source_dir = directory or (Path.home() / "Downloads")
    if not source_dir.is_dir():
        return None
    candidates: List[Path] = [
        p for p in source_dir.glob("setup*.htm*")
        if p.is_file() and p.suffix.lower() in (".html", ".htm")
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)

    target = get_runtime_root() / HTML_CONFIG_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(".html.bak")
        shutil.copy2(target, backup)
    atomic_write(target, newest.read_text(encoding="utf-8"), mode=0o600)
    return target


CHROMIUM_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def _chromium() -> Optional[str]:
    """First installed Chromium-family browser, if any.

    Only Chromium implements the write-back the page depends on. Handing the
    file to whatever the system defaults to risks Safari, which can fill the
    form but can only download it afterwards — a difference worth one lookup,
    because the person cannot tell the two apart until after they hit save.
    """
    for exe in CHROMIUM_CANDIDATES:
        if Path(exe).exists():
            return exe
    for name in ("google-chrome", "chromium", "microsoft-edge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _open_browser(path: Path) -> bool:
    uri = path.as_uri()
    exe = _chromium()
    if exe:
        try:
            subprocess.Popen([exe, uri],  # noqa: S603 - fixed argv, user's own file
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    try:
        return webbrowser.open(uri)
    except Exception:  # pragma: no cover - headless / no browser registered
        return False


def _print_report(result: Dict[str, Any]) -> None:
    target = result["path"]
    print(f"配置页已生成：{target}")
    print("在浏览器里填好连接信息，点「保存配置」会直接覆盖本页；重启客户端后生效。")
    print()

    config = result["config"]
    backend = config.get("active_backend", "duckdb")
    missing = missing_required(config, backend)
    if missing:
        print(f"当前后端 {backend} 还缺：{'、'.join(missing)}")
    else:
        print(f"当前后端 {backend} 的必填项已齐全")

    check = result["check"]
    if check.get("ran"):
        if check.get("ok"):
            target_label = check.get("target") or backend
            readonly = "服务端只读" if check.get("server_side_read_only") else "仅 SQL 文本审计"
            print(f"连接检测：成功（{target_label}，{readonly}）")
        else:
            print(f"连接检测：失败 — {check.get('error', '未知原因')}")
            if check.get("missing_driver"):
                print(f"  先装驱动：{check['missing_driver']}")
            if check.get("hint"):
                print(f"  {check['hint']}")
    elif check.get("reason") == "incomplete":
        print("连接检测：跳过（必填项未填全）")

    facts = result["facts"]
    overridden = [k for k, v in (facts.get("origins") or {}).items() if v == "process_env"]
    if overridden:
        print()
        print("注意：以下字段当前被 mcp.json 或环境变量覆盖，页面里改了不会生效：")
        for key in overridden:
            print(f"  - {key}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="credit-risk-config",
        description="生成数据库配置页（单文件 HTML），填写后工具自动读取",
    )
    parser.add_argument("--path", default="", help="配置页输出路径（默认 ~/.credit-agent/setup.html）")
    parser.add_argument("--no-open", action="store_true", help="只生成，不自动打开浏览器")
    parser.add_argument("--no-check", action="store_true", help="不做连接检测（不访问网络）")
    parser.add_argument("--serve", action="store_true",
                        help="起一个只监听 127.0.0.1 的服务，在浏览器里填完直接写回文件")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"配合 --serve 使用的端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--adopt", action="store_true",
                        help="把「下载」文件夹里最新的 setup.html 安装到配置目录")
    parser.add_argument("--print", dest="show", action="store_true", help="打印当前生效配置（密码脱敏）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    if args.adopt:
        adopted = adopt_download()
        if adopted is None:
            print("在「下载」文件夹里没有找到 setup*.html；请先在页面里点保存。")
            return 1
        print(f"已安装：{adopted}")
        print("重启客户端后生效。")
        return 0

    if args.show:
        loaded = load_config_file()
        payload = {
            "source": loaded["path"],
            "kind": loaded.get("kind", ""),
            "exists": loaded["exists"],
            "skipped": loaded.get("skipped", []),
            "warnings": loaded.get("warnings", []),
            "config": redacted_config(loaded["data"]),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"配置来源：{loaded['path']}" if loaded["exists"] else "尚未保存任何配置文件")
            for text in payload["skipped"]:
                print(f"已跳过：{text}")
            for text in payload["warnings"]:
                print(f"警告：{text}")
            print(json.dumps(payload["config"], ensure_ascii=False, indent=2))
        return 0

    target = Path(args.path).expanduser() if args.path else config_file_path()

    if args.serve:
        if not target.exists():
            write_page(target, check=False)
        return serve(target, port=args.port, open_browser=not args.no_open)

    result = write_page(target, check=not args.no_check)

    if args.json:
        print(json.dumps({
            "path": str(result["path"]),
            "check": result["check"],
            "config": redacted_config(result["config"]),
        }, ensure_ascii=False, indent=2))
        return 0

    _print_report(result)
    if not args.no_open and sys.stdout.isatty():
        if _open_browser(result["path"]):
            print()
            print("已在 Chrome 中打开：填完点「保存配置」会直接覆盖本页，无需其他操作。")
        else:
            print()
            print("未能自动打开浏览器，请手动打开上面的路径。")
    print()
    print("小提示：保存对话框里把上面的完整路径粘进「文件名」栏再回车，就能落在正确位置。")
    print("          如果只能在禁止脚本的预览窗口里看这一页，改用 credit-risk-config --serve。")
    print(f"          {CONFIG_FILE_VAR} 可以指向另一份配置文件，用于切换环境。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
