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

## Update

```bash
claude plugin marketplace update vibefoundry
```

## What is in here

- `plugins/vibefoundry-toolkit/.mcp.json` - the hosted toolkit server and the local pane server.
- `plugins/vibefoundry-toolkit/server/pane.js` - starts and stops the file viewer on your machine. It fetches the viewer from the hosted server and runs it on loopback. Your files never leave your computer.

Nothing in this repo needs to be edited to use the plugin.
