# Claude for Home Assistant

A Home Assistant integration that exposes Claude (running in the companion
**Claude Code** add-on) to Home Assistant itself:

- a **conversation agent** — chat with Claude from HA Assist (text or voice, on
  any device),
- a **`claude.ask` service** — call Claude from automations/scripts,
- a **status sensor**.

It pairs with the [ClaudeInHA add-on](https://github.com/LayerTM/ClaudeInHA):
the integration discovers, installs and starts the add-on for you, and talks to
Claude through it (so it reuses the add-on's HA tools, skills and login).

> Work in progress — building to HACS / Home Assistant Core quality.
