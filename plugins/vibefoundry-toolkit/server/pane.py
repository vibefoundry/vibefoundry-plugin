#!/usr/bin/env python3
"""
The VibeFoundry pane server: the one piece of the Claude plugin that runs on
the person's machine. It speaks MCP over stdio and has exactly one job -
start, find, and stop the local file viewer (the "tap") for a project folder,
and hand back the pane URL. Opening the pane is therefore a tool call with a
native approval, never a shell command that downloads and runs a script.

It is Python because everything it launches is Python. The old node version
existed only to find an interpreter and spawn this same tap - a runtime whose
job was to locate another runtime, and the one students did not have.

WHICH PYTHON RUNS THINGS IS DECIDED HERE, ONCE. Claude Code starts this server
with `${VF_PYTHON:-python3}`: on a Mac that is Apple's python3, present on
every machine; on Windows vf_install sets VF_PYTHON to Miniforge's python.exe
in Miniforge's own folder. This process then picks the interpreter for the
tap - Miniforge at its home when it is there, itself otherwise - and every
step the tap runs inherits it. No other file guesses.

It carries no toolkit logic. Every other tool lives on the hosted server named
beside this one in .mcp.json, and this process never proxies them. The tap is
fetched from that origin and cached under ~/.vibefoundry so later launches
work offline, and started detached so it outlives this process. Running
viewers are noted in ~/.vibefoundry/panes.json, so a new session reuses a
viewer that is still up instead of starting a second one.

stdout is the protocol channel. Nothing else may ever be written to it;
diagnostics go to stderr.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

VERSION = "0.5.0"
ORIGIN = os.environ.get("VF_ORIGIN", "https://mcp-dev.vibefoundry.ai").rstrip("/")
WIN = os.name == "nt"
USER_HOME = os.path.expanduser("~")
HOME = os.path.join(USER_HOME, ".vibefoundry")
TAP = os.path.join(HOME, "vf_tap.py")
REGISTRY = os.path.join(HOME, "panes.json")
READY_TIMEOUT = 25.0


def log(*a):
    sys.stderr.write("[vibefoundry-pane] " + " ".join(str(x) for x in a) + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------- interpreter --

_PY = None


def interpreter():
    """The one place that decides which Python runs the tap and the apps.

    VF_PYTHON wins when set. Otherwise Miniforge at its own home, because
    that is where the person's packages are; then Miniconda; then whatever
    is running this server. Each candidate is checked once, and the answer
    is kept for the life of the process."""
    global _PY
    if _PY:
        return _PY
    cands = []
    if os.environ.get("VF_PYTHON"):
        cands.append(os.environ["VF_PYTHON"])
    for d in ("miniforge3", "miniconda3"):
        cands.append(os.path.join(USER_HOME, d, "python.exe") if WIN
                     else os.path.join(USER_HOME, d, "bin", "python3"))
    cands.append(sys.executable)
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            if r.returncode == 0:
                _PY = c
                return c
        except Exception:
            continue
    raise RuntimeError("No Python 3.8 or newer was found on this machine. Run VibeFoundry's install tool once (vf_install), then try again.")


# --------------------------------------------------------------------- tap --

def ensure_tap():
    """A fresh copy when the hub answers, the cached one when it does not.
    After the first success a launch needs no network at all."""
    os.makedirs(HOME, exist_ok=True)
    try:
        import ssl
        ctx = ssl.create_default_context()
        try:
            ctx.load_default_certs()
        except Exception:
            pass
        try:
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        except Exception:
            pass
        data = urllib.request.urlopen(ORIGIN + "/tap.py", timeout=10, context=ctx).read()
        tmp = TAP + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, TAP)
        return "fresh"
    except Exception as e:
        if os.path.isfile(TAP):
            return "cached"
        raise RuntimeError("Could not reach VibeFoundry to fetch the viewer, and no cached copy exists yet (%s). "
                           "Check the network once; every later launch works offline." % e)


# ---------------------------------------------------------------- registry --

def read_registry():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_registry(reg):
    os.makedirs(HOME, exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, REGISTRY)


def pid_alive(pid):
    if not pid:
        return False
    if WIN:
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                                 capture_output=True, text=True, timeout=8).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def page_answers(url):
    """A viewer counts as up only if its process exists AND its page answers:
    a recycled pid must not pass for a running pane."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url)
        if not u.hostname or not u.port:
            return False
        r = urllib.request.urlopen("http://%s:%d/viewer" % (u.hostname, u.port), timeout=1.5)
        return r.status == 200
    except Exception:
        return False


def alive(entry):
    return bool(entry) and pid_alive(entry.get("pid")) and page_answers(entry.get("url", ""))


def kill_pid(pid):
    if not pid_alive(pid):
        return
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        else:
            import signal
            os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


# ------------------------------------------------------------------- panes --

def resolve_root(project_dir):
    """Claude Code hands plugin servers the project root as CLAUDE_PROJECT_DIR;
    that is the default when the call names no folder."""
    p = (os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) if project_dir in (None, "") else str(project_dir).strip()
    if p.startswith("~"):
        p = os.path.join(USER_HOME, p[1:].lstrip("/\\"))
    if not os.path.isabs(p):
        raise ValueError("project_dir must be an absolute path (got: %s)." % p)
    if not os.path.exists(p):
        raise ValueError("Not a folder on this machine: %s" % p)
    real = os.path.realpath(p)
    if not os.path.isdir(real):
        raise ValueError("Not a folder: %s" % real)
    return real


def start_tap(root):
    py = interpreter()
    source = ensure_tap()
    ready = os.path.join(tempfile.gettempdir(), "vf_ready_%d_%d.txt" % (os.getpid(), int(time.time() * 1000)))
    try:
        os.unlink(ready)
    except OSError:
        pass
    kw = {"cwd": root, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if WIN:
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    try:
        child = subprocess.Popen([py, TAP, root, "--announce", ready], **kw)
    except Exception as e:
        raise RuntimeError("Could not start the viewer: %s" % e)
    deadline = time.time() + READY_TIMEOUT
    url = ""
    while time.time() < deadline:
        try:
            with open(ready, encoding="utf-8") as f:
                url = f.read().strip()
        except OSError:
            url = ""
        if url:
            break
        if child.poll() is not None:
            raise RuntimeError("The viewer server exited before it was ready.")
        time.sleep(0.2)
    try:
        os.unlink(ready)
    except OSError:
        pass
    if not url:
        kill_pid(child.pid)
        raise RuntimeError("The viewer did not report ready in time.")
    return {"url": url, "pid": child.pid, "source": source}


def open_pane(project_dir):
    root = resolve_root(project_dir)
    reg = read_registry()
    cur = reg.get(root)
    if alive(cur):
        return {"project_dir": root, "url": cur["url"], "reused": True}
    if cur:
        kill_pid(cur.get("pid"))
    t = start_tap(root)
    reg[root] = {"url": t["url"], "pid": t["pid"], "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    write_registry(reg)
    return {"project_dir": root, "url": t["url"], "reused": False}


def stop_panes(project_dir):
    reg = read_registry()
    roots = list(reg.keys()) if project_dir in (None, "") else [resolve_root(project_dir)]
    stopped = []
    for r in roots:
        e = reg.get(r)
        if not e:
            continue
        kill_pid(e.get("pid"))
        del reg[r]
        stopped.append(r)
    write_registry(reg)
    return {"stopped": stopped}


def status_panes():
    reg = read_registry()
    running, changed = [], False
    for root, e in list(reg.items()):
        if alive(e):
            running.append({"project_dir": root, "url": e.get("url"), "started": e.get("started")})
        else:
            del reg[root]
            changed = True
    if changed:
        write_registry(reg)
    return {"running": running}


# --------------------------------------------------------------------- MCP --

OPEN_NOTE = (
    "Open this URL in the in-app browser panel, everything after '#' included, then say ONE short "
    "sentence to the person and STOP. The page shows a consent card that is the PERSON's to click: "
    "never click, read, screenshot, or drive anything inside the viewer, and do not describe its "
    "contents, ports, process ids, or status. The viewer runs on this machine only and exits after "
    "30 idle minutes.")

TOOLS = [
    {
        "name": "vf_pane_open",
        "title": "Open the VibeFoundry pane",
        "description": (
            "Open the VibeFoundry file viewer pane for a project folder: starts the local viewer "
            "server on this machine (or finds the one already running for that folder) and returns "
            "its URL. Call this whenever the user asks to open the viewer, the pane, or to look at a "
            "data file. Use it INSTEAD of any shell command from vf_pane - never download or run a "
            "launcher when this tool exists. Say 'Starting the local file viewer - your files stay "
            "on your machine.' first, then open the returned URL (everything after '#' included) in "
            "the in-app browser panel; use the default browser only if this client has no panel. "
            "Then say ONE short sentence and STOP: do not read the page, do not click its consent card "
            "or anything else in it (that is the person's click), do not screenshot it, and do not "
            "report ports, process ids, or status. NEVER open /viewer without the exact returned URL: "
            "without its fragment the page has no file source and shows nothing."),
        "inputSchema": {"type": "object", "properties": {"project_dir": {
            "type": "string",
            "description": "Absolute path of the project folder (the folder that holds app_folder). Defaults to the server's working folder."}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_pane_status",
        "title": "List running VibeFoundry panes",
        "description": "List the file viewer panes currently running on this machine, with their project folders and URLs.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_pane_stop",
        "title": "Stop a VibeFoundry pane",
        "description": (
            "Stop the file viewer for one project folder, or every running viewer when no folder is "
            "given. Only call it when the user asks to close or stop the viewer; viewers also exit on "
            "their own after 30 idle minutes."),
        "inputSchema": {"type": "object", "properties": {"project_dir": {
            "type": "string", "description": "Absolute path of the project folder; omit to stop all."}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]

INSTRUCTIONS = (
    "This server starts and stops the VibeFoundry file viewer on this machine. When the "
    "user asks to open the viewer or the pane, call vf_pane_open with the absolute project "
    "folder, open the URL it returns in the in-app browser panel, say one short sentence, "
    "and stop: never read, click, or screenshot inside the viewer (its consent card is the "
    "person's to click) and never report ports, process ids, or status. Never launch the "
    "viewer with a shell command while this server is present.")


def text(s, data):
    return {"content": [{"type": "text", "text": s}], "structuredContent": data}


def failure(s):
    return {"content": [{"type": "text", "text": s}], "isError": True}


def call_tool(params):
    name = (params or {}).get("name")
    args = (params or {}).get("arguments") or {}
    try:
        if name == "vf_pane_open":
            r = open_pane(args.get("project_dir"))
            return text(("The viewer for this folder is already running. " if r["reused"] else "Pane ready. ")
                        + OPEN_NOTE + "\nURL: " + r["url"], r)
        if name == "vf_pane_status":
            r = status_panes()
            return text("\n".join(p["project_dir"] + " -> " + p["url"] for p in r["running"]) or "No viewer is running.", r)
        if name == "vf_pane_stop":
            r = stop_panes(args.get("project_dir"))
            return text("Stopped the viewer for: " + ", ".join(r["stopped"]) if r["stopped"] else "No viewer was running.", r)
        return failure("Unknown tool: %s" % name)
    except Exception as e:
        log("tool", name, "failed:", e)
        return failure(str(e))


class MethodNotFound(Exception):
    code = -32601


def dispatch(msg):
    p = msg.get("params") or {}
    m = msg.get("method")
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion") or "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vibefoundry-pane", "version": VERSION},
                "instructions": INSTRUCTIONS}
    if m == "ping":
        return {}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        return call_tool(p)
    raise MethodNotFound("Method not found: %s" % m)


def send(obj):
    sys.stdout.buffer.write((json.dumps(obj) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main():
    # Requests are answered in arrival order: a second vf_pane_open for the
    # same folder must see the first one's registry entry, not race it.
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        mid = msg.get("id")
        if mid is None:
            continue          # a notification: nothing to answer
        try:
            send({"jsonrpc": "2.0", "id": mid, "result": dispatch(msg)})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": getattr(e, "code", -32603), "message": str(e)}})


if __name__ == "__main__":
    main()
