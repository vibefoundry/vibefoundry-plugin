#!/usr/bin/env node
"use strict";
/*
 * The VibeFoundry pane server: the one piece of the Claude plugin that runs
 * on the person's machine. It speaks MCP over stdio and has exactly one job -
 * start, find, and stop the local file viewer (the "tap") for a project
 * folder, and hand back the pane URL. Opening the pane is therefore a tool
 * call with a native approval, never a shell command that downloads and runs
 * a script.
 *
 * It carries no toolkit logic. Every other tool still lives on the hosted
 * server named beside this one in .mcp.json, and this process never proxies
 * them. The tap itself is fetched from that same origin by Python (so the
 * TLS behaviour matches the launcher people already run), cached under
 * ~/.vibefoundry so later launches work offline, and started detached so it
 * outlives this process and the session. Running viewers are noted in
 * ~/.vibefoundry/panes.json, so a new session reuses a viewer that is still
 * up instead of starting a second one.
 *
 * stdout is the protocol channel. Nothing else may ever be written to it;
 * diagnostics go to stderr.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const { spawn, spawnSync } = require("child_process");

const VERSION = "0.4.1";
const ORIGIN = String(process.env.VF_ORIGIN || "https://mcp-dev.vibefoundry.ai").replace(/\/+$/, "");
const HOME = path.join(os.homedir(), ".vibefoundry");
const TAP = path.join(HOME, "vf_tap.py");
const REGISTRY = path.join(HOME, "panes.json");
const WIN = process.platform === "win32";
const READY_TIMEOUT_MS = 25000;

const log = (...a) => process.stderr.write("[vibefoundry-pane] " + a.join(" ") + "\n");

// ---------------------------------------------------------------- python --

// The same fallback chain the shell recipe used, probed in-process: the
// first interpreter that is Python 3.8+ wins, and the choice is remembered.
let PY = null;
function pythonCandidates() {
  const h = os.homedir();
  const c = [];
  if (process.env.VF_PYTHON) c.push({ cmd: process.env.VF_PYTHON, pre: [] });
  if (WIN) {
    c.push({ cmd: "python", pre: [] }, { cmd: "py", pre: ["-3"] },
      { cmd: path.join(h, "miniforge3", "python.exe"), pre: [] },
      { cmd: path.join(h, "miniconda3", "python.exe"), pre: [] });
  } else {
    c.push({ cmd: "python3", pre: [] },
      { cmd: path.join(h, "miniforge3", "bin", "python3"), pre: [] },
      { cmd: path.join(h, "miniconda3", "bin", "python3"), pre: [] },
      { cmd: "python", pre: [] });
  }
  return c;
}
function findPython() {
  if (PY) return PY;
  for (const cand of pythonCandidates()) {
    const r = spawnSync(cand.cmd, [...cand.pre, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
      { stdio: "ignore", timeout: 8000, windowsHide: true });
    if (!r.error && r.status === 0) { PY = cand; return PY; }
  }
  throw new Error("No Python 3.8 or newer was found on this machine. Run VibeFoundry's install tool once (vf_install), then try again.");
}

// -------------------------------------------------------------------- tap --

// Fetch-or-reuse, done by Python so certificate handling matches the
// launcher: a fresh copy when the hub answers, the cached one when it does
// not. After the first success a launch needs no network at all.
const FETCH_PY = [
  "import os, sys, ssl, urllib.request",
  "origin, tap = sys.argv[1], sys.argv[2]",
  "os.makedirs(os.path.dirname(tap), exist_ok=True)",
  "ctx = ssl.create_default_context()",
  "try: ctx.load_default_certs()",
  "except Exception: pass",
  "try: ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT",
  "except Exception: pass",
  "try:",
  "    data = urllib.request.urlopen(origin + '/tap.py', timeout=10, context=ctx).read()",
  "    tmp = tap + '.tmp'",
  "    with open(tmp, 'wb') as f: f.write(data)",
  "    os.replace(tmp, tap)",
  "    print('fresh')",
  "except Exception as e:",
  "    if os.path.isfile(tap): print('cached')",
  "    else: sys.exit('Could not reach VibeFoundry to fetch the viewer, and no cached copy exists yet (%s). Check the network once; every later launch works offline.' % e)",
  "",
].join("\n");

function ensureTap() {
  const py = findPython();
  const r = spawnSync(py.cmd, [...py.pre, "-", ORIGIN, TAP],
    { input: FETCH_PY, encoding: "utf8", timeout: 30000, windowsHide: true });
  if (r.error) throw new Error("Could not run Python to fetch the viewer: " + r.error.message);
  if (r.status !== 0) throw new Error((r.stderr || r.stdout || "").trim() || "Fetching the viewer failed.");
  return (r.stdout || "").trim();
}

// ---------------------------------------------------------------- registry --

function readRegistry() {
  try { return JSON.parse(fs.readFileSync(REGISTRY, "utf8")) || {}; } catch { return {}; }
}
function writeRegistry(reg) {
  fs.mkdirSync(HOME, { recursive: true });
  const tmp = REGISTRY + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(reg, null, 1), { mode: 0o600 });
  fs.renameSync(tmp, REGISTRY);
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}

// A viewer counts as up only if its process exists AND its page answers:
// a recycled pid must not pass for a running pane.
function pageAnswers(url) {
  return new Promise((resolve) => {
    let u;
    try { u = new URL(url); } catch { return resolve(false); }
    const req = http.get({ host: u.hostname, port: u.port, path: "/viewer", timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}
async function alive(entry) {
  return !!entry && pidAlive(entry.pid) && (await pageAnswers(entry.url));
}

function killPid(pid) {
  if (!pidAlive(pid)) return;
  if (WIN) spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
  else { try { process.kill(pid, "SIGTERM"); } catch {} }
}

// ------------------------------------------------------------------- panes --

// Claude Code hands plugin servers the project root as CLAUDE_PROJECT_DIR;
// that is the default when the call names no folder.
function resolveRoot(projectDir) {
  let p = projectDir == null || projectDir === "" ? (process.env.CLAUDE_PROJECT_DIR || process.cwd()) : String(projectDir).trim();
  if (p.startsWith("~")) p = path.join(os.homedir(), p.slice(1));
  if (!path.isAbsolute(p)) throw new Error("project_dir must be an absolute path (got: " + p + ").");
  let real;
  try { real = fs.realpathSync(p); } catch { throw new Error("Not a folder on this machine: " + p); }
  if (!fs.statSync(real).isDirectory()) throw new Error("Not a folder: " + real);
  return real;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startTap(root) {
  const py = findPython();
  const source = ensureTap();
  const ready = path.join(os.tmpdir(), `vf_ready_${process.pid}_${Date.now()}.txt`);
  try { fs.unlinkSync(ready); } catch {}
  const child = spawn(py.cmd, [...py.pre, TAP, root, "--announce", ready],
    { cwd: root, detached: true, stdio: "ignore", windowsHide: true });
  let spawnError = null;
  child.on("error", (e) => { spawnError = e; });
  child.unref();
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let url = "";
  while (Date.now() < deadline) {
    if (spawnError) throw new Error("Could not start the viewer: " + spawnError.message);
    try { url = fs.readFileSync(ready, "utf8").trim(); } catch { url = ""; }
    if (url) break;
    if (child.exitCode !== null) throw new Error("The viewer server exited before it was ready.");
    await sleep(200);
  }
  try { fs.unlinkSync(ready); } catch {}
  if (!url) { killPid(child.pid); throw new Error("The viewer did not report ready in time."); }
  return { url, pid: child.pid, source };
}

async function openPane(projectDir) {
  const root = resolveRoot(projectDir);
  const reg = readRegistry();
  const cur = reg[root];
  if (await alive(cur)) return { project_dir: root, url: cur.url, reused: true };
  if (cur) killPid(cur.pid);
  const t = await startTap(root);
  reg[root] = { url: t.url, pid: t.pid, started: new Date().toISOString() };
  writeRegistry(reg);
  return { project_dir: root, url: t.url, reused: false };
}

async function stopPanes(projectDir) {
  const reg = readRegistry();
  const roots = projectDir == null || projectDir === "" ? Object.keys(reg) : [resolveRoot(projectDir)];
  const stopped = [];
  for (const r of roots) {
    const e = reg[r];
    if (!e) continue;
    killPid(e.pid);
    delete reg[r];
    stopped.push(r);
  }
  writeRegistry(reg);
  return { stopped };
}

async function statusPanes() {
  const reg = readRegistry();
  const running = [];
  let changed = false;
  for (const [root, e] of Object.entries(reg)) {
    if (await alive(e)) running.push({ project_dir: root, url: e.url, started: e.started });
    else { delete reg[root]; changed = true; }
  }
  if (changed) writeRegistry(reg);
  return { running };
}

// --------------------------------------------------------------------- MCP --

const OPEN_NOTE =
  "Open this URL in the in-app browser panel, everything after '#' included, then say ONE short " +
  "sentence to the person and STOP. The page shows a consent card that is the PERSON's to click: " +
  "never click, read, screenshot, or drive anything inside the viewer, and do not describe its " +
  "contents, ports, process ids, or status. The viewer runs on this machine only and exits after " +
  "30 idle minutes.";

const TOOLS = [
  {
    name: "vf_pane_open",
    title: "Open the VibeFoundry pane",
    description:
      "Open the VibeFoundry file viewer pane for a project folder: starts the local viewer " +
      "server on this machine (or finds the one already running for that folder) and returns " +
      "its URL. Call this whenever the user asks to open the viewer, the pane, or to look at a " +
      "data file. Use it INSTEAD of any shell command from vf_pane - never download or run a " +
      "launcher when this tool exists. Say 'Starting the local file viewer - your files stay " +
      "on your machine.' first, then open the returned URL (everything after '#' included) in " +
      "the in-app browser panel; use the default browser only if this client has no panel. " +
      "Then say ONE short sentence and STOP: do not read the page, do not click its consent card " +
      "or anything else in it (that is the person's click), do not screenshot it, and do not " +
      "report ports, process ids, or status. NEVER open /viewer without the exact returned URL: " +
      "without its fragment the page has no file source and shows nothing.",
    inputSchema: {
      type: "object",
      properties: {
        project_dir: {
          type: "string",
          description: "Absolute path of the project folder (the folder that holds app_folder). Defaults to the server's working folder.",
        },
      },
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "vf_pane_status",
    title: "List running VibeFoundry panes",
    description: "List the file viewer panes currently running on this machine, with their project folders and URLs.",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "vf_pane_stop",
    title: "Stop a VibeFoundry pane",
    description:
      "Stop the file viewer for one project folder, or every running viewer when no folder is " +
      "given. Only call it when the user asks to close or stop the viewer; viewers also exit on " +
      "their own after 30 idle minutes.",
    inputSchema: { type: "object", properties: { project_dir: { type: "string", description: "Absolute path of the project folder; omit to stop all." } } },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
];

const text = (s, data) => ({ content: [{ type: "text", text: s }], structuredContent: data });
const failure = (s) => ({ content: [{ type: "text", text: s }], isError: true });

async function callTool(params) {
  const name = params && params.name;
  const args = (params && params.arguments) || {};
  try {
    if (name === "vf_pane_open") {
      const r = await openPane(args.project_dir);
      return text((r.reused ? "The viewer for this folder is already running. " : "Pane ready. ") + OPEN_NOTE + "\nURL: " + r.url, r);
    }
    if (name === "vf_pane_status") {
      const r = await statusPanes();
      return text(r.running.length ? r.running.map((p) => p.project_dir + " -> " + p.url).join("\n") : "No viewer is running.", r);
    }
    if (name === "vf_pane_stop") {
      const r = await stopPanes(args.project_dir);
      return text(r.stopped.length ? "Stopped the viewer for: " + r.stopped.join(", ") : "No viewer was running.", r);
    }
    return failure("Unknown tool: " + name);
  } catch (e) {
    log("tool", name, "failed:", e.message);
    return failure(e.message);
  }
}

async function dispatch(msg) {
  const p = msg.params || {};
  switch (msg.method) {
    case "initialize":
      return {
        protocolVersion: p.protocolVersion || "2025-03-26",
        capabilities: { tools: {} },
        serverInfo: { name: "vibefoundry-pane", version: VERSION },
        instructions:
          "This server starts and stops the VibeFoundry file viewer on this machine. When the " +
          "user asks to open the viewer or the pane, call vf_pane_open with the absolute project " +
          "folder, open the URL it returns in the in-app browser panel, say one short sentence, " +
          "and stop: never read, click, or screenshot inside the viewer (its consent card is the " +
          "person's to click) and never report ports, process ids, or status. Never launch the " +
          "viewer with a shell command while this server is present.",
      };
    case "ping":
      return {};
    case "tools/list":
      return { tools: TOOLS };
    case "tools/call":
      return callTool(p);
    default: {
      const err = new Error("Method not found: " + msg.method);
      err.code = -32601;
      throw err;
    }
  }
}

function send(msg) { process.stdout.write(JSON.stringify(msg) + "\n"); }

// Requests are answered in arrival order: a second vf_pane_open for the same
// folder must see the first one's registry entry, not race it.
let chain = Promise.resolve();
function handleLine(line) {
  let msg;
  try { msg = JSON.parse(line); } catch {
    return send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
  }
  if (msg.id === undefined || msg.id === null) return; // a notification: nothing to answer
  chain = chain.then(async () => {
    try { send({ jsonrpc: "2.0", id: msg.id, result: await dispatch(msg) }); }
    catch (e) { send({ jsonrpc: "2.0", id: msg.id, error: { code: e.code || -32603, message: e.message || String(e) } }); }
  });
}

let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (line) handleLine(line);
  }
});
process.stdin.on("end", () => process.exit(0));
process.on("SIGTERM", () => process.exit(0));
process.on("SIGINT", () => process.exit(0));
