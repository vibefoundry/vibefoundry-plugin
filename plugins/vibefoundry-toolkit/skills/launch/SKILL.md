---
name: launch
description: The front door. Use whenever the person says "launch", "giddy up", "get set up", "get me started", "where are we", or opens a session without saying what they want yet. Opens nothing new, checks nothing by hand, and ends in one line and one question.
---

# Launch

The person wants to start. This is the whole script. Do not add to it, and do not narrate it.

1. **The pane is already open.** The session-start hook opened the viewer and handed you its URL. If no URL was handed to you this session, call `vf_pane_open` and open the URL it returns in the in-app browser panel. Either way: at most one sentence about the viewer, and never a description of what it shows.

2. **Ask who is signed in.** Call `vf_portal_status` on the pane server. It says whether the person is signed in and to which hub. That is the only fact you need from it.

3. **Load the map, silently.** Call `vf_giddy_up` so the tool map and the rules are in front of you for whatever comes next. Repeat none of it.

4. **The environment is already checked.** The hook context and `vf_pane_open` carry an `Environment:` line written by the pane server. Nothing missing: say nothing about the environment. Something missing: one sentence naming it and offering `vf_install`. Node only matters once a front end is being built, so stay silent about it until then. Never run a check yourself.

5. **Say one line, then stop and wait.**
   - Not signed in: *Sign in on the Portal tab in the viewer, pick your hub there, then tell me what you want built.*
   - Signed in: *Signed in to <hub>. What do you want built?*

Never: list the environment, ask whether a company hub exists, report ports, process ids or status, describe the steps above, or start building before the person has answered. When they answer, `vf_guide` takes over.
