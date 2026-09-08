# VibeFoundry plugin for Claude Code and Codex

This is the distribution repo for the VibeFoundry toolkit plugin. It holds only what your machine needs to connect: the plugin manifests, the server addresses, and a small launcher for the local file viewer. Everything else runs on VibeFoundry's servers. One repo serves both hosts: Claude reads `.claude-plugin/`, Codex reads `.agents/plugins/` and `.codex-plugin/`.

## Install in Codex

In the Codex app open Plugins, choose to add a marketplace, and paste
`vibefoundry/vibefoundry-plugin` (or the full URL below). Then install
**VibeFoundry** from the `vibefoundry` marketplace. From a terminal it is:

```bash
codex plugin marketplace add https://github.com/vibefoundry/vibefoundry-plugin.git
```

```bash
codex plugin add vibefoundry-toolkit@vibefoundry
```

Codex runs commands with the network off, which blocks both the viewer and the
hub your pipelines pull from. Say `giddy up` once and let `vf_install` set
`network_access = true` in `~/.codex/config.toml`, then restart Codex.

## Install in Claude Code

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

- `plugins/vibefoundry-toolkit/.mcp.json` - the hosted toolkit server and the local pane server (Claude); `.mcp.codex.json` is the same pair in Codex's shape.
- `plugins/vibefoundry-toolkit/.codex-plugin/plugin.json` and `.agents/plugins/marketplace.json` - the Codex manifests.
- `plugins/vibefoundry-toolkit/hooks/hooks.json` - opens the pane at the start of every session.
- The pane server also answers the company-portal calls (tables, queries, landing a table) directly, through the viewer it started - so being signed in is never mistaken for being signed out.
- `plugins/vibefoundry-toolkit/server/pane.py` - starts and stops the file viewer on your machine. It fetches the viewer from the hosted server and runs it on loopback. Your files never leave your computer.

Nothing in this repo needs to be edited to use the plugin.
