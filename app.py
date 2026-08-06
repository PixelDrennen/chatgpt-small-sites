#!/usr/bin/env python3
"""Local Perforce workspace monitor. It only listens on 127.0.0.1."""
from __future__ import annotations

import atexit, json, os, re, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("P4_DASHBOARD_PORT", "8765"))
BUILD = "0.7.2"
PID_FILE = ROOT / ".p4-monitor.pid"
SETTINGS_FILE = ROOT / ".p4-monitor.json"
SETTING_KEYS = ("P4PORT", "P4USER", "P4CLIENT")

def load_settings():
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        result = {k: str(raw.get(k, "")).strip() for k in SETTING_KEYS if str(raw.get(k, "")).strip()}
        result["clients"] = [str(value).strip() for value in raw.get("clients", []) if str(value).strip()]
        if result.get("P4CLIENT") and result["P4CLIENT"] not in result["clients"]:
            result["clients"].insert(0, result["P4CLIENT"])
        return result
    except (OSError, json.JSONDecodeError):
        return {}

def save_settings(values):
    previous = load_settings()
    cleaned = {k: str(values.get(k, "")).strip() for k in SETTING_KEYS if str(values.get(k, "")).strip()}
    clients = previous.get("clients", [])
    if cleaned.get("P4CLIENT"):
        clients = [cleaned["P4CLIENT"], *[value for value in clients if value != cleaned["P4CLIENT"]]]
    cleaned["clients"] = clients
    SETTINGS_FILE.write_text(json.dumps(cleaned, indent=2) + "\n")
    return cleaned

def remove_client(name):
    settings = load_settings()
    settings["clients"] = [value for value in settings.get("clients", []) if value != name]
    if settings.get("P4CLIENT") == name:
        settings.pop("P4CLIENT", None)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")
    return settings

def p4(*args, timeout=12):
    if not shutil.which("p4"):
        return 127, "", "The 'p4' command is not installed or not in PATH."
    env = os.environ.copy()
    settings = load_settings()
    # Saved workspace history is application data, not an environment value.
    # subprocess requires every environment value to be a string.
    env.update({key: settings[key] for key in SETTING_KEYS if settings.get(key)})
    try:
        run = subprocess.run(["p4", *args], capture_output=True, text=True, timeout=timeout, env=env)
        return run.returncode, run.stdout, run.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Timed out after {timeout} seconds. Check P4PORT, VPN, and server reachability."

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
            current["clientPath"] = value
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

def ztag_fstat_files(text):
    rows, row = [], {}
    for line in text.splitlines():
        if not line.startswith("... "): continue
        key, _, value = line[4:].partition(" ")
        if key == "depotFile" and row.get("path"):
            rows.append(row); row = {}
        if key == "depotFile": row["path"] = value
        elif key == "clientFile": row["clientPath"] = value
        elif key == "action": row["action"] = value
        elif key in ("headType", "type"): row["type"] = value
        elif key == "headTime": row["modified"] = value
        elif key == "headRev": row["headRev"] = value
        elif key == "haveRev": row["haveRev"] = value
    if row.get("path"): rows.append(row)
    for item in rows:
        client_path = item.get("clientPath")
        if client_path:
            try:
                stat = Path(client_path).stat()
                item["workspaceModified"] = int(stat.st_mtime)
                item["created"] = int(stat.st_ctime)
            except OSError: pass
    return rows

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

def format_changes(rows, default_files=None, file_limit=None):
    def format_one(row):
        ident = row.get("change", "?")
        files = default_files if ident == "default" else change_files(ident)
        return {"id": ident, "user": row.get("user", ""), "client": row.get("client", ""), "description": row.get("desc", "").strip(), "fileCount": len(files), "files": files[:file_limit] if file_limit else files}
    if len(rows) < 2:
        return [format_one(row) for row in rows]
    with ThreadPoolExecutor(max_workers=min(6, len(rows))) as executor:
        return list(executor.map(format_one, rows))

def changes():
    client, error = workspace()
    if error: return {"error": error, "settings": load_settings()}
    name = client["name"]
    # In a stream workspace the stream is the efficient depot path. The client
    # name is a revision specifier, not a depot path, and using //client/... can
    # make the server search an unrelated/nonexistent depot tree.
    depot_path = (client.get("stream") or f"//{name}").rstrip("/") + "/..."
    incoming_path = f"{depot_path}@{name},#head"
    with ThreadPoolExecutor(max_workers=3) as executor:
        incoming_future = executor.submit(p4, "-ztag", "changes", "-s", "submitted", "-l", "-m", "25", incoming_path)
        pending_future = executor.submit(p4, "-ztag", "changes", "-s", "pending", "-l", "-m", "50", "-c", name)
        open_future = executor.submit(p4, "-ztag", "opened", "-c", "default")
        incoming_code, incoming_text, incoming_error = incoming_future.result()
        pending_code, pending_text, pending_error = pending_future.result()
        open_code, open_text, _ = open_future.result()
    default_files = ztag_files(open_text) if not open_code else []
    outgoing = ztag_changes(pending_text) if not pending_code else []
    if default_files: outgoing.insert(0, {"change": "default", "desc": "Default changelist (not yet numbered)"})
    errors = []
    if incoming_code: errors.append("Incoming query: " + (incoming_error or "failed"))
    if pending_code: errors.append("Outgoing query: " + (pending_error or "failed"))
    incoming_rows = ztag_changes(incoming_text) if not incoming_code else []
    with ThreadPoolExecutor(max_workers=2) as executor:
        incoming_future = executor.submit(format_changes, incoming_rows, None, 200)
        outgoing_future = executor.submit(format_changes, outgoing, default_files)
        incoming = incoming_future.result()
        formatted_outgoing = outgoing_future.result()
    return {"build": BUILD, "workspace": client, "incoming": incoming, "outgoing": formatted_outgoing, "errors": errors, "checkedAt": datetime.now(timezone.utc).isoformat()}

def all_files():
    client, error = workspace()
    if error: return {"error": error}
    path = (client.get("stream") or f"//{client['name']}").rstrip("/") + "/..."
    code, output, command_error = p4("-ztag", "fstat", "-T", "depotFile,clientFile,action,headType,type,headTime,headRev,haveRev", path, timeout=45)
    if code and not output: return {"error": command_error or "Unable to list files."}
    files = ztag_fstat_files(output)
    return {"build": BUILD, "workspace": client, "files": files, "warning": command_error if code else ""}

def action_request(payload):
    client, error = workspace()
    if error: return {"error": error}
    action = str(payload.get("action", ""))
    change = str(payload.get("change", "")).strip()
    stream = client.get("stream", "")
    commands = {
        "merge-down": ["merge", "-S", stream],
        "copy-up": ["copy", "-S", stream, "-r"],
        "submit": ["submit", "-c", change],
    }
    command = commands.get(action)
    if not command or any(value == "" for value in command): return {"error": "This action needs a stream and, for submit, a numbered changelist."}
    display = "p4 " + " ".join(command)
    if not payload.get("confirmed"): return {"preview": display, "requiresConfirmation": True}
    code, output, command_error = p4(*command, timeout=120)
    return {"command": display, "ok": code == 0, "output": output[-12000:], "error": command_error[-4000:]}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/changes": return self.json(changes())
        if route == "/api/files": return self.json(all_files())
        if route == "/api/settings": return self.json(load_settings())
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if route in files:
            filename, content_type = files[route]; body = (ROOT / filename).read_bytes()
            self.send_response(200); self.send_header("Content-Type", content_type); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def do_POST(self):
        route = urlparse(self.path).path
        try:
            size = int(self.headers.get("Content-Length", "0")); values = json.loads(self.rfile.read(size))
            if route == "/api/settings": return self.json({"settings": save_settings(values)})
            if route == "/api/settings/remove-client": return self.json({"settings": remove_client(str(values.get("client", "")))})
            if route == "/api/action": return self.json(action_request(values))
            self.send_error(404)
        except (ValueError, json.JSONDecodeError): self.json({"error": "Invalid connection settings."}, 400)
    def json(self, value, status=200):
        body = json.dumps(value).encode()
        try:
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def log_message(self, *_): pass

if __name__ == "__main__":
    PID_FILE.write_text(str(os.getpid()))
    def remove_pid():
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()): PID_FILE.unlink()
    atexit.register(remove_pid)
    print(f"Perforce dashboard {BUILD}: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
