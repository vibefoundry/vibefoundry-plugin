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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import zlib
import urllib.request

VERSION = "0.9.6"
ORIGIN = os.environ.get("VF_ORIGIN", "https://mcp-dev.vibefoundry.ai").rstrip("/")
WIN = os.name == "nt"
USER_HOME = os.path.expanduser("~")
HOME = os.path.join(USER_HOME, ".vibefoundry")
TAP = os.path.join(HOME, "vf_tap.py")
REGISTRY = os.path.join(HOME, "panes.json")
LOCK = os.path.join(HOME, "panes.lock")
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


# ------------------------------------------------------------- environment --

_ENV = None


def host():
    """Which assistant started this server: VF_HOST from the plugin's server
    config, or --host from the hook command. Unknown means no host-specific
    check is made."""
    if "--host" in sys.argv:
        i = sys.argv.index("--host")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip().lower()
    return (os.environ.get("VF_HOST") or "").strip().lower()


def _runs(cmd, timeout=15):
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
    except Exception:
        return False


def environment():
    """What this machine has, as facts, checked once per process.

    The model used to be handed these as shell commands to run and read; a
    failed check was then reported as a pass. Now the server that already
    starts the viewer answers - under the same interpreter rule the viewer and
    every pipeline step inherit - and the model has one line to repeat, or
    nothing to say."""
    global _ENV
    if _ENV:
        return _ENV
    present, missing = [], []
    try:
        py = interpreter()
        v = subprocess.run([py, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=8).stdout.strip()
        present.append("Python " + v)
        (present if _runs([py, "-c", "import polars"], 25) else missing).append("Polars")
    except Exception:
        py = ""
        missing.extend(["Python", "Polars"])
    for name, exe in (("git", "git"), ("Node", "node")):
        (present if shutil.which(exe) and _runs([exe, "--version"]) else missing).append(name)
    h = host()
    if h == "codex":
        cfg = os.path.join(USER_HOME, ".codex", "config.toml")
        try:
            on = os.path.isfile(cfg) and re.search(r"^\s*network_access\s*=\s*true", open(cfg, encoding="utf-8").read(), re.M)
        except Exception:
            on = False
        (present if on else missing).append("Codex sandbox network")
    if missing:
        line = "Environment: MISSING " + ", ".join(missing) + ". Present: " + ", ".join(present) + "."
    else:
        line = "Environment: " + ", ".join(present) + " - all present."
    _ENV = {"present": present, "missing": missing, "python": py, "host": h or "unknown", "line": line}
    return _ENV


# ------------------------------------------------------------- permissions --

SETTINGS = os.path.join(USER_HOME, ".claude", "settings.json")
ASKED = os.path.join(HOME, "preapprove_offered")
BUILD_COMMANDS = ["python", "python3", "py", "pip", "pip3", "conda", "npm", "npx", "node", "git",
                  "mkdir", "ls", "dir", "cat", "cd", "unzip", "curl"]


def plugin_name():
    """The plugin this server ships in, read from its own manifest two folders
    up - so the stamped clones (vibefoundry-ai, diageo-ai-foundry) name their
    own servers without anyone typing them."""
    try:
        mp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "plugin.json")
        return str(json.load(open(mp, encoding="utf-8")).get("name") or "vibefoundry-toolkit")
    except Exception:
        return "vibefoundry-toolkit"


def permission_rules():
    n = plugin_name()
    return ["mcp__plugin_%s_vibefoundry" % n, "mcp__plugin_%s_vibefoundry-pane" % n] + \
           ["Bash(%s:*)" % c for c in BUILD_COMMANDS]


def read_settings():
    try:
        d = json.load(open(SETTINGS, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def permissions_state():
    """What Claude will do before each tool on this machine, as a fact for the
    hook context and the open result. `offer` is true exactly once per
    machine: the launch skill asks that one time, and never nags."""
    if host() != "claude":
        return None
    perms = read_settings().get("permissions") or {}
    mode = str(perms.get("defaultMode") or "default")
    allow = perms.get("allow") if isinstance(perms.get("allow"), list) else []
    servers = permission_rules()[:2]
    pre = mode == "bypassPermissions" or all(r in allow for r in servers)
    offer = not pre and not os.path.isfile(ASKED)
    if offer:
        try:
            os.makedirs(HOME, exist_ok=True)
            open(ASKED, "w").write(time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception:
            pass
    return {"mode": mode, "preapproved": pre, "offer": offer}


def preapprove(mode):
    """Merge the pre-approval into the person's OWN settings file, once they
    said yes: the mode, the two rules for this plugin's servers, and the
    build commands. Other keys are untouched, nothing is added twice, and a
    machine already in bypass mode is left alone. Never called from the
    hook - only from a tool call the person approves."""
    if host() != "claude":
        return {"applied": False, "why": "Only Claude Code keeps these settings. Codex asks in its own way and this does not apply there."}
    mode = mode if mode in ("acceptEdits", "bypassPermissions") else "acceptEdits"
    cfg = read_settings()
    perms = cfg.get("permissions") if isinstance(cfg.get("permissions"), dict) else {}
    cur = str(perms.get("defaultMode") or "default")
    if cur == "bypassPermissions":
        return {"applied": False, "mode": cur, "why": "This machine already runs in bypass mode: nothing asks, nothing to add."}
    allow = [x for x in (perms.get("allow") or []) if isinstance(x, str)]
    added = [r for r in permission_rules() if r not in allow]
    changed = bool(added) or cur != mode
    if not changed:
        return {"applied": False, "mode": cur, "why": "Already pre-approved; nothing changed."}
    perms["defaultMode"] = mode
    perms["allow"] = allow + added
    cfg["permissions"] = perms
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    return {"applied": True, "mode": mode, "added": added, "file": SETTINGS}


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
    # A port derived from the project path, so the viewer comes back on the
    # same address after a restart and the launch.json entry stops changing.
    # The tap falls back to a free port when this one is taken.
    port = 20000 + zlib.crc32(root.encode("utf-8")) % 20000
    try:
        child = subprocess.Popen([py, TAP, root, "--announce", ready, "--port", str(port)], **kw)
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


def lock():
    """One launcher at a time across processes: the session-start hook and
    the server (or two hooks) must not each start a viewer for one folder.
    A lock older than a minute is a crashed holder and is taken over."""
    os.makedirs(HOME, exist_ok=True)
    end = time.time() + READY_TIMEOUT + 5
    while True:
        try:
            os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK) > 60:
                    os.unlink(LOCK)
                    continue
            except OSError:
                continue
            if time.time() > end:
                return
            time.sleep(0.2)


def unlock():
    try:
        os.unlink(LOCK)
    except OSError:
        pass


PANE_NAME = (os.environ.get("VF_PANE_NAME") or "VibeFoundry").strip()


def origin_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).fragment).get("tap", [""])[0].rstrip("/")


def write_launch_entry(root, url):
    """Claude's desktop app draws a preview card - the one with the Open
    button - only for a NAMED preview from the project's .claude/launch.json.
    So on Claude the viewer gets one attach-only entry there (a name and the
    viewer's origin, no command: nothing is started by the app), rewritten
    every time because the port changes. Other entries are left alone. The
    origin carries no token; the assistant navigates the tab to the real URL
    right after preview_start. Codex has no such file and gets none."""
    if host() != "claude":
        return None
    origin = origin_of(url)
    if not origin:
        return None
    d = os.path.join(root, ".claude")
    p = os.path.join(d, "launch.json")
    try:
        cfg = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("version", "0.0.1")
    confs = [c for c in (cfg.get("configurations") or []) if isinstance(c, dict) and c.get("name") != PANE_NAME]
    entry = {"name": PANE_NAME, "url": origin}
    if any(c.get("name") == PANE_NAME and c.get("url") == origin for c in (cfg.get("configurations") or [])) and os.path.isfile(p):
        return PANE_NAME
    cfg["configurations"] = confs + [entry]
    try:
        os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except Exception as e:
        log("launch.json:", e)
        return None
    return PANE_NAME


def open_pane(project_dir):
    root = resolve_root(project_dir)
    lock()
    try:
        reg = read_registry()
        cur = reg.get(root)
        if alive(cur):
            r = {"project_dir": root, "url": cur["url"], "reused": True}
        else:
            if cur:
                kill_pid(cur.get("pid"))
            t = start_tap(root)
            reg = read_registry()
            reg[root] = {"url": t["url"], "pid": t["pid"], "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
            write_registry(reg)
            r = {"project_dir": root, "url": t["url"], "reused": False}
        r["preview"] = write_launch_entry(root, r["url"])
        r["permissions"] = permissions_state()
        return r
    finally:
        unlock()


def perms_line(r):
    p = r.get("permissions")
    if not p or p.get("preapproved") or not p.get("offer"):
        return ""
    return ("\nPermissions: not pre-approved - Claude asks before every tool and command on this machine. "
            "Offer ONCE, in one sentence: 'Claude will ask before every tool and command. Want me to pre-approve "
            "the VibeFoundry tools and the build commands?' Yes: call vf_pane_preapprove. No: never raise it again.")


def open_steps(r):
    """The words that open the pane, for the hook context and vf_pane_open
    alike. Claude: preview_start on the named entry (the card), then navigate
    the tab to the real URL. Everywhere else: open the URL in the panel."""
    if r.get("preview"):
        return ("FIRST call preview_start with name '" + r["preview"] + "' - it attaches the in-app browser panel "
                "to the running viewer and puts a preview card with an Open button in the chat; nothing is started. "
                "THEN call navigate on that tab with this exact URL, everything after '#' included, so the panel "
                "shows the viewer itself. ")
    return "Open this URL in the in-app browser panel, everything after '#' included. "


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


# ------------------------------------------------------------------ portal --

def tap_of(project_dir):
    """The live tap's origin and token for this project - from the viewer we
    started (or start it now). This is the address the model used to type by
    hand from an old URL; it is never typed again."""
    root = resolve_root(project_dir)
    cur = read_registry().get(root)
    url = cur["url"] if alive(cur) else open_pane(project_dir)["url"]
    frag = url.split("#", 1)[1] if "#" in url else ""
    q = urllib.parse.parse_qs(frag)
    origin, token = q.get("tap", [""])[0], q.get("token", [""])[0]
    if not origin or not token:
        raise RuntimeError("the viewer URL carries no tap address")
    return origin, token


def tap_call(project_dir, path, method="GET", body=None, params=None):
    """One request to the running tap, answered as (status, text)."""
    origin, token = tap_of(project_dir)
    qs = {"token": token}
    qs.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = origin + path + "?" + urllib.parse.urlencode(qs)
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


SIGN_IN = ("Not signed in to the company portal. Ask the person to open the pane's Citizen Engineer "
           "Portal tab and sign in (their normal work login), then call this again. Nothing else is "
           "wrong, and nothing needs to be installed or pasted.")


def portal_result(code, text, ok_note=""):
    """Turn a tap answer into a tool result. Only a 409 means sign in - a 403
    is a stale address, which cannot happen from here, and a 401 is an
    expired session (sign in again)."""
    if code == 409:
        return failure(SIGN_IN)
    if code == 401:
        return failure("The portal sign-in expired (it lasts one hour). Ask the person to sign in again from the pane's Portal tab, then call this again.")
    if code == 403:
        return failure("The viewer refused the address; it may have restarted. Call this again.")
    if code != 200:
        return failure("The portal answered %d: %s" % (code, text[:600]))
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    return text_result((ok_note + "\n" if ok_note else "") + text[:200000], data if isinstance(data, dict) else {"result": data})


def text_result(s, data):
    return {"content": [{"type": "text", "text": s}], "structuredContent": data}


# --------------------------------------------------------------------- MCP --

OPEN_NOTE = (
    "Then say ONE short "
    "sentence to the person and STOP. The page shows a consent card that is the PERSON's to click: "
    "never click, read, screenshot, or drive anything inside the viewer, and do not describe its "
    "contents, ports, process ids, or status. The viewer runs on this machine only and exits only after "
    "a full day with no request at all; an open page keeps it alive.")

ENV_NOTE = (
    "This line IS the environment check - never run one yourself. Nothing missing: say nothing about "
    "the environment. Something missing: one sentence naming it and offering vf_install (Node only "
    "matters once a front end is being built).")

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
            "on your machine.' first, then follow the result's text: on Claude, preview_start on the "
            "named launch.json entry it wrote (the preview card), then navigate that tab to the returned "
            "URL (everything after '#' included); elsewhere open the URL in the in-app browser panel; "
            "use the default browser only if this client has no panel. "
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
            "their own after a full day with no request at all."),
        "inputSchema": {"type": "object", "properties": {"project_dir": {
            "type": "string", "description": "Absolute path of the project folder; omit to stop all."}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    # The portal tools. This server holds the running viewer's address, so
    # these need no placeholders and can never hit a stale token: the model
    # used to assemble a curl by hand from an old URL, get 403 'bad token',
    # and tell a signed-in person they were signed out.
    {
        "name": "vf_pane_preapprove",
        "title": "Stop the permission prompts",
        "description": (
            "Pre-approve the VibeFoundry tools and the usual build commands in the person's OWN Claude "
            "settings (user scope), so Claude stops asking before every tool call. Call it ONLY after the "
            "person said yes to the one-sentence offer; never on your own. mode 'acceptEdits' (default) "
            "pre-approves file edits, both VibeFoundry servers and the build commands and still asks for "
            "anything else; 'bypassPermissions' stops every prompt. Takes effect from the next session. "
            "Claude Code only; on Codex it says it does not apply."),
        "inputSchema": {"type": "object", "properties": {"mode": {
            "type": "string", "enum": ["acceptEdits", "bypassPermissions"],
            "description": "acceptEdits (default) or bypassPermissions."}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_portal_status",
        "title": "Is the company portal signed in?",
        "description": ("Whether the running viewer holds a portal session for this project, and for whom. "
                        "Call this before assuming anything about sign-in. Only 'linked: false' means the person "
                        "must sign in (from the pane's Citizen Engineer Portal tab)."),
        "inputSchema": {"type": "object", "properties": {"project_dir": {"type": "string", "description": "Absolute project folder; defaults to this session's."}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_portal_tables",
        "title": "List the portal's tables (or one table's profile)",
        "description": ("The person's private tables from their company portal - every table they may read, or with "
                        "`table`, that table's full profile (columns, types, nulls, distinct counts, samples). Answered "
                        "directly by this server through the running viewer: no command to run, no placeholders. Use "
                        "THIS, never the hosted vf_portal_tables recipe, while this server is present. Call it at stage 1 "
                        "of every build, before the public catalogue."),
        "inputSchema": {"type": "object", "properties": {
            "project_dir": {"type": "string", "description": "Absolute project folder; defaults to this session's."},
            "table": {"type": "string", "description": "A table id, for that table's full profile."}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_portal_query",
        "title": "Ask the portal a question (SQL)",
        "description": ("One read-only SELECT (DuckDB dialect) against the person's private tables, answered through the "
                        "running viewer. For ANSWERS: the reply is capped at 200,000 characters because an answer belongs "
                        "in the conversation. To bring a table into an app use vf_portal_fetch, never this. Call "
                        "vf_portal_tables first so the column names are real."),
        "inputSchema": {"type": "object", "properties": {
            "project_dir": {"type": "string", "description": "Absolute project folder; defaults to this session's."},
            "sql": {"type": "string", "description": "One SELECT or WITH statement."}}, "required": ["sql"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_portal_fetch",
        "title": "Bring a private table into an app",
        "description": ("Land a private table - or a SQL slice of it - as a parquet in an app's raw_data/, streamed straight "
                        "to disk by the running viewer: uncapped, refused if the hub would truncate it, with a .hub sidecar so "
                        "the profiler does not re-read it. For served or shareable apps (Track 3/5); a Track 1 pipeline "
                        "pulls in a step instead."),
        "inputSchema": {"type": "object", "properties": {
            "project_dir": {"type": "string", "description": "Absolute project folder; defaults to this session's."},
            "app_dir": {"type": "string", "description": "The app folder, relative to the project root, e.g. app_folder/scripts/my_app."},
            "table": {"type": "string", "description": "The table id."},
            "sql": {"type": "string", "description": "Optional SELECT - only its result lands."},
            "name": {"type": "string", "description": "Optional file name; defaults to <table>.parquet."}},
            "required": ["app_dir", "table"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "vf_portal_credential",
        "title": "Write an app's portal credential (.env)",
        "description": ("Mint an app credential for this app - scoped to the tables its hub pull steps name - and "
                        "write VF_GATEWAY / VF_APP_ID / VF_APP_KEY into the app's .env, through the running viewer. "
                        "The viewer already does this on its own when it sees a hub pull in the plan and the person "
                        "is signed in; call this only to do it now, or to confirm. Requires a signed-in portal session "
                        "and a plan with at least one hub pull step."),
        "inputSchema": {"type": "object", "properties": {
            "project_dir": {"type": "string", "description": "Absolute project folder; defaults to this session's."},
            "app_dir": {"type": "string", "description": "The app folder, relative to the project root."}}, "required": ["app_dir"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]

INSTRUCTIONS = (
    "This server starts and stops the VibeFoundry file viewer on this machine. "
    "THE PANE OPENS ONLY WHEN IT IS WANTED: when the person asks about the data in their company's "
    "hub, asks to build something, says 'launch' or 'giddy up', or asks for the viewer or the pane. "
    "For any other message say nothing about it and open nothing. When it is wanted: if the "
    "session-start hook handed you a viewer URL, do what its text says (on Claude: preview_start on the "
    "named launch.json entry, which puts the preview card in the chat, then navigate that tab to the "
    "URL); otherwise call vf_pane_open for the project folder and follow its text the same way. Then say "
    "one short sentence and continue. Never read, click, or screenshot inside the viewer (its consent card is the "
    "person's to click) and never report ports, process ids, or status. Never launch the "
    "viewer with a shell command while this server is present. PORTAL CALLS - is the person "
    "signed in, which private tables exist, a SQL answer, landing a table in an app - go through "
    "THIS server's vf_portal_status / vf_portal_tables / vf_portal_query / vf_portal_fetch, which "
    "talk to the running viewer directly. Never run the hosted portal recipes (curl with "
    "<TAP-ORIGIN>/<TAP-TOKEN>) while this server is present: only a 'not signed in' answer from "
    "these tools means the person must sign in. THE ENVIRONMENT IS ALREADY CHECKED: this server probes "
    "the machine when it starts the viewer - Python, Polars under the Python the plugin uses, git, Node, "
    "and on Codex its sandbox network - and hands the result over as an 'Environment:' line in the "
    "session-start hook context and in vf_pane_open / vf_pane_status. Read that line; never run a "
    "check yourself. Nothing missing: say nothing about it. Something missing: one sentence naming "
    "it and offering vf_install. PERMISSIONS: when the hook context or vf_pane_open says the machine is "
    "not pre-approved, offer ONCE in one sentence to pre-approve the VibeFoundry tools and the build "
    "commands; yes means vf_pane_preapprove, no means never raise it again. Never call it unasked.")


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
            r["environment"] = environment()
            return text(("The viewer for this folder is already running. " if r["reused"] else "Pane ready. ")
                        + open_steps(r) + OPEN_NOTE + "\nURL: " + r["url"] + "\n" + r["environment"]["line"] + " " + ENV_NOTE + perms_line(r), r)
        if name == "vf_pane_status":
            r = status_panes()
            r["environment"] = environment()
            return text(("\n".join(p["project_dir"] + " -> " + p["url"] for p in r["running"]) or "No viewer is running.")
                        + "\n" + r["environment"]["line"], r)
        if name == "vf_pane_stop":
            r = stop_panes(args.get("project_dir"))
            return text("Stopped the viewer for: " + ", ".join(r["stopped"]) if r["stopped"] else "No viewer was running.", r)
        if name == "vf_pane_preapprove":
            r = preapprove(str(args.get("mode") or "acceptEdits"))
            if r.get("applied"):
                msg = ("Done: %s mode, %d rule(s) added to the person's own settings. It takes effect when "
                       "Claude is fully quit and reopened." % (r["mode"], len(r["added"])))
            else:
                msg = r.get("why", "Nothing changed.")
            return text_result(msg, r)
        pd = args.get("project_dir")
        if name == "vf_portal_status":
            code, body = tap_call(pd, "/portal/status")
            if code != 200:
                return portal_result(code, body)
            d = json.loads(body)
            msg = ("Signed in as %s (expires %s)." % (d.get("email") or "?", d.get("expires") or "?")) if d.get("linked") \
                else "Not signed in. The person signs in from the pane's Citizen Engineer Portal tab."
            return text_result(msg, d)
        if name == "vf_portal_tables":
            code, body = tap_call(pd, "/portal/tables", params={"table": args.get("table")})
            return portal_result(code, body)
        if name == "vf_portal_query":
            sql = str(args.get("sql") or "").strip()
            if not sql:
                return failure("sql is required")
            code, body = tap_call(pd, "/portal/query", method="POST", body=sql)
            return portal_result(code, body, "Answer (capped at 200,000 characters - use vf_portal_fetch to land a table):")
        if name == "vf_portal_credential":
            code, body = tap_call(pd, "/portal/credential", method="POST", params={"dir": args.get("app_dir")})
            if code == 400:
                return failure("This app's plan declares no hub pull step yet, so no credential is needed. Declare the pull step first.")
            return portal_result(code, body, "The app's .env now holds its portal credential; gateway.pull() in the pull step will use it.")
        if name == "vf_portal_fetch":
            code, body = tap_call(pd, "/portal/fetch", method="POST", body=str(args.get("sql") or ""),
                                  params={"dir": args.get("app_dir"), "table": args.get("table"), "name": args.get("name")})
            if code == 413:
                return failure("The hub would have truncated that result, so nothing was saved. Narrow the SQL, aggregate further, or fetch the whole table and filter in the app.")
            return portal_result(code, body, "Landed. Record it in app_manifest.json where a step reads it, and copy its columns and types from vf_portal_tables into `columns`.")
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


def session_start():
    """The SessionStart hook: start (or find) the viewer for the project and
    hand the model its URL as context, so the pane is open in every session
    without anyone remembering to ask. stdout IS the context - one message,
    nothing else. Never fails the session: a problem is one calm line."""
    if os.environ.get("VF_PANE_AUTO", "1") in ("0", "false", "no"):
        return
    root = os.environ.get("CLAUDE_PROJECT_DIR") or ""
    try:
        hook = json.loads(sys.stdin.read() or "{}")
        root = root or hook.get("cwd") or ""
    except Exception:
        pass
    root = root or os.getcwd()
    try:
        r = open_pane(root)
        print("VibeFoundry pane is running for this project, in the background. "
              "DO NOT open it now and say nothing about it. "
              "Open it ONLY when the person asks about the data in their company's hub, "
              "asks to build something, or says 'launch' or 'giddy up' - then: " + open_steps(r)
              + "Say 'Starting the local file viewer - your files stay on your machine.' and one short "
              "sentence, then continue with their request. Never read, click, or screenshot inside "
              "it, never report ports or process ids, and never launch the viewer with a shell command.\nURL: "
              + r["url"] + "\n" + environment()["line"] + " " + ENV_NOTE + perms_line(r))
    except Exception as e:
        log("session start:", e)
        print("The VibeFoundry pane could not start on its own (" + str(e)[:120] + "). When the user wants "
              "the viewer, call vf_pane_open; never download or run a launcher.")


if __name__ == "__main__":
    if "--open" in sys.argv[1:]:
        session_start()
    else:
        main()
