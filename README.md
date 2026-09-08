# VibeFoundry plugin for Claude Code

This is the distribution repo for the VibeFoundry toolkit plugin. It holds only what your machine needs to connect: the plugin manifest, the server addresses, and a small launcher for the local file viewer. Everything else runs on VibeFoundry's servers.

## Install

```bash
claude plugin marketplace add https://github.com/vibefoundry/vibefoundry-plugin.git
```

```bash
claude plugin install vibefoundry-toolkit@vibefoundry
```

The full HTTPS address matters: the short `owner/repo` form clones over SSH, which needs keys and is blocked on many networks.

Then start a new Claude Code session in your project folder and say `giddy up`.

## Requirements

Python 3.8 or newer. A Mac already has it. On Windows, say `giddy up` in a
session and let `vf_install` set up Miniforge; it also tells the plugin where
that Python lives. Then quit and reopen Claude once so it sees the change.

## The pane opens by itself

Every session starts by opening the VibeFoundry file viewer for the project - a
session-start hook starts it and Claude opens the page. Nothing to ask for. On
Windows the automatic open needs Git Bash (Git for Windows); opening it on
request works either way. Set `VF_PANE_AUTO=0` to turn the automatic open off.

## Update

```bash
claude plugin marketplace update vibefoundry && claude plugin update vibefoundry-toolkit@vibefoundry
```

## What is in here

- `plugins/vibefoundry-toolkit/.mcp.json` - the hosted toolkit server and the local pane server.
- `plugins/vibefoundry-toolkit/hooks/hooks.json` - opens the pane at the start of every session.
- The pane server also answers the company-portal calls (tables, queries, landing a table) directly, through the viewer it started - so being signed in is never mistaken for being signed out.
- `plugins/vibefoundry-toolkit/server/pane.py` - starts and stops the file viewer on your machine. It fetches the viewer from the hosted server and runs it on loopback. Your files never leave your computer.

Nothing in this repo needs to be edited to use the plugin.
