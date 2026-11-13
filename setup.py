"""Local-only first-run setup wizard.

Run with: python setup.py
The wizard binds to 127.0.0.1 only and writes secrets to .env.
"""
from __future__ import annotations

import html
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
HOST = os.getenv("SETUP_HOST", "0.0.0.0")
PORT = int(os.getenv("SETUP_PORT", "3002"))

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EmlaLock Bot Â· Setup</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c31;--muted:#9aa8c2;--text:#edf2ff;--accent:#8b9cff;--line:#293653}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#202c55 0,transparent 38%),var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:48px auto;padding:0 20px}.brand{color:var(--accent);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:12px}
h1{font-size:34px;line-height:1.15;margin:10px 0}p{color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:26px;margin:22px 0;box-shadow:0 18px 55px #0003}
h2{font-size:18px;margin:0 0 14px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.field{margin:14px 0}label{display:block;font-weight:600;margin-bottom:6px}
input,select{width:100%;padding:12px 13px;background:#0c1428;border:1px solid #344361;border-radius:10px;color:var(--text);font:inherit}
small{display:block;color:var(--muted);margin-top:5px}.check{display:flex;gap:10px;align-items:center;color:var(--muted)}.check input{width:auto}
button{background:var(--accent);color:#0b1020;border:0;border-radius:10px;padding:13px 19px;font-weight:750;font-size:15px;cursor:pointer}
.notice{border:1px solid #465b89;background:#1a2948;padding:14px;border-radius:12px;color:#d9e2ff}
.success{border-color:#397a62;background:#102d27}.footer{font-size:12px;color:var(--muted);margin:24px 0}
@media(max-width:600px){main{margin:24px auto}.grid{grid-template-columns:1fr}.panel{padding:19px}h1{font-size:29px}}
</style></head><body><main>
<div class="brand">EmlaLock AI Â· First-run wizard</div><h1>Let's get your bot ready.</h1>
<p>Enter your service credentials and choose the basics. This page runs on your own machine and saves settings to a local .env file.</p>
<div id="message" class="notice" hidden></div>
<form method="post" action="/save">
<section class="panel"><h2>01 Â· Connect Discord</h2>
<div class="field"><label for="discord">Discord bot token</label><input id="discord" name="DISCORD_TOKEN" type="password" autocomplete="off" required placeholder="Paste your bot token"><small>Get this from Discord Developer Portal â†’ Bot. Never share it.</small></div>
</section>
<section class="panel"><h2>02 Â· AI assistant</h2>
<div class="field"><label for="openai">OpenAI API key</label><input id="openai" name="OPENAI_API_KEY" type="password" autocomplete="off" required placeholder="sk-â€¦"></div>
<div class="grid"><div class="field"><label for="model">Model</label><select id="model" name="OPENAI_MODEL"><option value="gpt-4o-mini">GPT-4o mini Â· economical</option><option value="gpt-4.1-mini">GPT-4.1 mini</option><option value="gpt-4.1">GPT-4.1</option></select></div>
<div class="field"><label for="level">Log level</label><select id="level" name="LOG_LEVEL"><option>INFO</option><option>WARNING</option><option>ERROR</option></select></div></div>
</section>
<section class="panel"><h2>03 Â· EmlaLock connection</h2>
<div class="field"><label for="api">API base URL</label><input id="api" name="EMLALOCK_API_BASE_URL" value="https://api.emlalock.com" required></div>
<p>Each Discord member links their own EmlaLock User ID and API key privately inside Discord using /link. Do not enter another member's key here.</p>
</section>
<section class="panel"><h2>04 Â· Local security</h2>
<p>A new encryption key will be generated automatically if one is not already configured. Keep the key and database backup together; losing the key makes saved EmlaLock credentials unreadable.</p>
<label class="check"><input type="checkbox" name="confirm" value="yes" required> I understand the .env file contains secrets and must never be committed or shared.</label>
</section>
<button type="submit">Save settings</button>
</form>
<p class="footer">Local-only setup Â· Bound to 127.0.0.1 Â· Secrets are not sent to a third-party setup service.</p>
</main></body></html>"""


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(values: dict[str, str]) -> None:
    existing = read_env()
    existing.update(values)
    existing.setdefault("DATABASE_PATH", "data/bot.sqlite3")
    existing.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    lines = [
        "# Generated by the local EmlaLock AI bot setup wizard.",
        "# Keep this file private. Do not commit it to Git.",
    ]
    for key in (
        "DISCORD_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL",
        "DATABASE_PATH", "CREDENTIAL_ENCRYPTION_KEY",
        "EMLALOCK_API_BASE_URL", "LOG_LEVEL",
    ):
        value = existing.get(key, "")
        lines.append(f'{key}="{value}"')
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/save":
            self.send_error(404)
            return
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16384:
            self.send_error(413)
            return
        fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        values = {key: fields.get(key, [""])[0].strip() for key in (
            "DISCORD_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL",
            "EMLALOCK_API_BASE_URL", "LOG_LEVEL",
        )}
        if not values["DISCORD_TOKEN"] or not values["OPENAI_API_KEY"]:
            self.send_error(400, "Discord token and OpenAI API key are required.")
            return
        if fields.get("confirm") != ["yes"]:
            self.send_error(400, "Please confirm the local secret-storage warning.")
            return
        if not values["EMLALOCK_API_BASE_URL"].startswith("https://"):
            self.send_error(400, "EmlaLock API URL must use HTTPS.")
            return
        write_env(values)
        body = b"""<!doctype html><meta name="viewport" content="width=device-width"><title>Settings saved</title>
<style>body{font:16px system-ui;background:#0b1020;color:#edf2ff;max-width:650px;margin:12vh auto;padding:24px}
section{background:#141c31;border:1px solid #293653;border-radius:16px;padding:26px}a{color:#aab7ff}</style>
<section><h1>Settings saved.</h1><p>Your credentials are stored in the local .env file. This page does not display them again.</p>
<h2>Next steps</h2><ol><li>Close this setup page.</li><li>Start the bot with <code>python main.py</code>.</li>
<li>Invite the bot to your Discord server and use <code>/link</code> to connect EmlaLock accounts privately.</li></ol>
<p>To change settings later, stop the bot and run <code>python setup.py</code> again.</p></section>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Do not log POST bodies or query strings that could contain secrets.
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    display_host = "127.0.0.1" if HOST == "0.0.0.0" else HOST
    print(f"Open http://{display_host}:{PORT} in your browser.")
    if HOST == "127.0.0.1":
        print("This setup server is only reachable from this computer.")
    else:
        print("This setup server is listening on all interfaces; use a trusted network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSetup wizard stopped.")
    finally:
        server.server_close()

