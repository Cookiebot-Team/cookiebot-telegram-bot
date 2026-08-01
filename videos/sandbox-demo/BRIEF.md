---
workflow: general-video
flow: automation
storyboard: no
aspect: "16:9"
duration_target: 40
language: en
---

# Cookiebot Sandbox — demonstration video

## Subject

The Cookiebot sandbox: a local Telegram that the real bot talks to. Show the
workbench, a user driving it, the bot answering, and the feature view — with
the conversation in **Portuguese first**, then the same commands in an English
group to demonstrate i18n.

## Why this is not an illustration

Every screen in this video is a real capture of the running stack —
telegram-sandbox on :8083, cb-gateway on :8084 polling it, the web client on
:3001, against a real Postgres and Valkey. The Portuguese replies were
produced by the actual handler stack; the group's language was derived the way
production derives it (the founder's own Telegram `language_code` when the bot
is added), not set by writing a column.

That constraint is the point. A demo of a testing tool that used mocked-up
screens would be the exact failure the tool exists to prevent.

## Assets

Captured from the live UI (`assets/`):

| File                   | What it shows                                                        |
| ---------------------- | -------------------------------------------------------------------- |
| `workbench-full.jpg`   | The three-pane workbench                                             |
| `pt-group.jpg`         | `Furries do Brasil` — Ana's commands, the bot answering in Portuguese |
| `pt-dm-config.jpg`     | The config menu, delivered privately; `Language: pt` in the dump      |
| `en-group.jpg`         | `Furries Worldwide` — the same commands, answered in English          |
| `sidebar-features.png` | The feature rail and the PT-BR / EN-US acting-user chips              |

## Scenes

1. **Title** — what the thing is.
2. **The workbench** — three panes, named.
3. **Portuguese** — `/regras`, `/privacidade`, `/config` and the real answers.
4. **Answered privately** — the menu arrives as a DM, because Telegram forbids
   a bot opening one. `Language: pt` is visible in the settings dump.
5. **i18n** — the same three commands in an English group, side by side.
6. **Features** — the feature rail; close.

## Design

The product's own palette, not an invented one — the video and the tool should
look like the same thing:

- surface `#0e1621`, panel `#17212b`, raised `#1c2733`
- accent `#3390ec` (the sandbox's blue), green `#4dcd5e`, amber `#e5a663`
- text `#ffffff`, muted `#7d8b99`

Type: system UI stack for prose, system monospace for commands — no unbundled
display font, so a cloud render cannot silently substitute one.

Motion: restrained. Screens settle rather than fly; captions cut on the beat.
The screenshots are the subject and motion must not compete with them.

## Deliberately not in the video

The `/config` menu's button labels are English even in a `pt` group. That is a
real finding, reported in the handoff — but a defect callout belongs in a bug
report, not baked into a video the user may publish. The frame shows what it
shows; nothing is captioned as localised that is not.
