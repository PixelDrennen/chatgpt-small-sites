#!/usr/bin/env python3
"""Local Perforce workspace monitor. It only listens on 127.0.0.1."""
from __future__ import annotations

import atexit, json, os, re, shutil, subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("P4_DASHBOARD_PORT", "8765"))
BUILD = "0.6.1"
PID_FILE = ROOT / ".p4-monitor.pid"
SETTINGS_FILE = ROOT / ".p4-monitor.json"
SETTING_KEYS = ("P4PORT", "P4USER", "P4CLIENT")

def load_settings():
    try:
        return {k: str(v).strip() for k, v in json.loads(SETTINGS_FILE.read_text()).items() if k in SETTING_KEYS and str(v).strip()}
    except (OSError, json.JSONDecodeError):
        return {}

def save_settings(values):
    cleaned = {k: str(values.get(k, "")).strip() for k in SETTING_KEYS if str(values.get(k, "")).strip()}
    SETTINGS_FILE.write_text(json.dumps(cleaned, indent=2) + "\n")
    return cleaned

def p4(*args):
    if not shutil.which("p4"):
        return 127, "", "The 'p4' command is not installed or not in PATH."
    env = os.environ.copy(); env.update(load_settings())
    try:
        run = subprocess.run(["p4", *args], capture_output=True, text=True, timeout=12, env=env)
        return run.returncode, run.stdout, run.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Timed out after 12 seconds. Check P4PORT, VPN, and server reachability."

def fields(spec):
    out = {}
    for line in spec.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1); out[k.strip()] = v.strip()
    return out

def ztag_changes(text):
    records, row = [], {}
    for line in text.splitlines():
        if not line.startswith("... "): continue
        k, _, v = line[4:].partition(" ")
        if k == "change" and "change" in row: records.append(row); row = {}
        row[k] = v
    return records + ([row] if row else [])

def ztag_files(text):
    # `p4 describe -ztag` commonly uses numbered depotFile0/action0 fields,
    # while `p4 opened -ztag` emits one unnumbered record per file. Support both.
    paths, actions, records, current = {}, {}, [], {}
    for line in text.splitlines():
        if not line.startswith("... "): continue
        key, _, value = line[4:].partition(" ")
        if key == "depotFile":
            if current.get("path"):
                records.append(current)
                current = {}
            current["path"] = value
            continue
        # `opened -ztag` includes clientFile in the *same* record as depotFile.
        # Keep the depot path (needed for the shared hierarchy) and use the
        # client path only if Perforce did not send a depot path.
        if key == "clientFile":
            if not current.get("path"):
                current["path"] = value
            continue
        if key == "action" and current.get("path"):
            current["action"] = value
            continue
        match = re.fullmatch(r"(depotFile|clientFile|action)(\d+)", key)
        if not match: continue
        group, index = match.groups()
        if group in ("depotFile", "clientFile"): paths[index] = value
        else: actions[index] = value
    if current.get("path"):
        records.append(current)
    numbered = [{"path": paths[i], "action": actions.get(i, "edit")} for i in sorted(paths, key=int)]
    return records or numbered

def change_files(change):
    code, output, _ = p4("-ztag", "describe", "-s", change)
    return ztag_files(output) if not code else []

def workspace():
    configured = load_settings()
    missing = [key for key in ("P4PORT", "P4USER", "P4CLIENT") if not configured.get(key) and not os.environ.get(key)]
    if missing:
        return None, "Set " + ", ".join(missing) + " below. The dashboard starts outside your Perforce workspace, so it cannot safely guess them."
    code, output, error = p4("-ztag", "info")
    if code: return None, error or output or "Unable to connect to Perforce."
    code, spec, error = p4("client", "-o")
    if code: return None, error or output or "Connected, but the selected client could not be read."
    client = fields(spec)
    return {"name": client.get("Client", configured.get("P4CLIENT", "")), "root": client.get("Root", ""), "stream": client.get("Stream", "")}, None

def format_changes(rows, default_files=None):
    result = []
    for row in rows:
        ident = row.get("change", "?")
        result.append({"id": ident, "user": row.get("user", ""), "client": row.get("client", ""), "description": row.get("desc", "").strip(), "files": default_files if ident == "default" else change_files(ident)})
    return result

def changes():
    client, error = workspace()
    if error: return {"error": error, "settings": load_settings()}
    name = client["name"]
    # In a stream workspace the stream is the efficient depot path. The client
    # name is a revision specifier, not a depot path, and using //client/... can
    # make the server search an unrelated/nonexistent depot tree.
    depot_path = (client.get("stream") or f"//{name}").rstrip("/") + "/..."
    incoming_path = f"{depot_path}@{name},#head"
    incoming_code, incoming_text, incoming_error = p4("-ztag", "changes", "-s", "submitted", "-l", "-m", "25", incoming_path)
    pending_code, pending_text, pending_error = p4("-ztag", "changes", "-s", "pending", "-l", "-m", "50", "-c", name)
    open_code, open_text, _ = p4("-ztag", "opened", "-c", "default")
    default_files = ztag_files(open_text) if not open_code else []
    outgoing = ztag_changes(pending_text) if not pending_code else []
    if default_files: outgoing.insert(0, {"change": "default", "desc": "Default changelist (not yet numbered)"})
    errors = []
    if incoming_code: errors.append("Incoming query: " + (incoming_error or "failed"))
    if pending_code: errors.append("Outgoing query: " + (pending_error or "failed"))
    return {"build": BUILD, "workspace": client, "incoming": format_changes(ztag_changes(incoming_text) if not incoming_code else []), "outgoing": format_changes(outgoing, default_files), "errors": errors, "checkedAt": datetime.now(timezone.utc).isoformat()}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/changes": return self.json(changes())
        if route == "/api/settings": return self.json(load_settings())
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if route in files:
            filename, content_type = files[route]; body = (ROOT / filename).read_bytes()
            self.send_response(200); self.send_header("Content-Type", content_type); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def do_POST(self):
        if urlparse(self.path).path != "/api/settings": return self.send_error(404)
        try:
            size = int(self.headers.get("Content-Length", "0")); values = json.loads(self.rfile.read(size))
            self.json({"settings": save_settings(values)})
        except (ValueError, json.JSONDecodeError): self.json({"error": "Invalid connection settings."}, 400)
    def json(self, value, status=200):
        body = json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass

if __name__ == "__main__":
    PID_FILE.write_text(str(os.getpid()))
    def remove_pid():
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()): PID_FILE.unlink()
    atexit.register(remove_pid)
    print(f"Perforce dashboard {BUILD}: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
