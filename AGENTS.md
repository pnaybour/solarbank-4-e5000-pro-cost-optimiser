# Instructions for AI coding agents

The working rules for any AI coding agent in this repository (OpenAI Codex, ChatGPT, Claude Code or another) are in `CLAUDE.md`. Read it first and follow it. Its hard rules apply to every agent: never write to the battery, never change `control-mode` or `write-switch`, never bypass the Pi's test gate, and never ask the owner for a key or password in the chat.

To install this for a new owner, follow `INSTALL_WITH_CLAUDE_CODE.md`. Despite its name, it is written for any coding agent that can run commands on the owner's computer and on the Pi over SSH.

If you are an ordinary chat assistant with no access to the owner's computer, use the same guide, but give the owner one command at a time to copy and run themselves. Explain each command before they run it, ask them to paste back the output (never a key or a password), and wait for it before the next step.
