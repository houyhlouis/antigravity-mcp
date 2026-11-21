# Security policy

Antigravity MCP is designed for **one trusted local user** and explicitly selected project folders. It is not a multi-user or remote service. The full trust model is in [docs/security.md](docs/security.md).

## Supported versions

Security fixes are made on the latest release only. Please check that the problem still exists there before reporting it.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub:

1. Open the [new security advisory form](https://github.com/houyhlouis/antigravity-mcp/security/advisories/new). You can also reach it from the repository: **Security → Report a vulnerability**.
2. Describe the problem. Only the maintainer can read the report.

If the form is not available to you, open a normal issue titled **"Security contact request"** and write nothing else in it. The maintainer will reply with a private way to continue. Never put the details of a vulnerability in a public issue, discussion or pull request.

### What to include

- A minimal way to reproduce the problem.
- The Antigravity MCP version (`python3 server.py --version`), the Antigravity CLI version (`agy --version`), your operating system and your MCP client.
- Which boundary was crossed, for example approval routing, a folder reservation, the local socket or redaction.
- The output of `python3 server.py doctor`, if it is relevant.

### What to leave out

- Passwords, tokens, API keys and anything from `~/.gemini`.
- The state folder or `state.sqlite3`. They contain your prompts and Antigravity's output.
- Complete logs or conversation history. Copy only the lines that matter, and read them before you send them.

## What counts as a vulnerability

In scope:

- A permission prompt that is approved without a person and without the `approval_relay` setting.
- A way for tool arguments to make the server read or send Antigravity's credentials, call `agy` with flags outside the documented whitelist, or skip Antigravity's permission checks.
- Another user account on the same computer reading the state folder or connecting to the broker.
- A secret that should have been redacted appearing in a preview, an event or the log.

Not vulnerabilities in this project:

- Anything that needs the attacker to already run programs as your own user account.
- What the Antigravity agent or the model decides to do inside the folder with the permissions you gave it.
- Problems in Antigravity CLI itself. Report those to Google through the [Antigravity support page](https://antigravity.google/support).

## If something goes wrong locally

1. Stop the broker: `python3 server.py stop`.
2. Keep only the evidence you need, for example the relevant part of `broker.log`, and review it before sharing.
3. If your Google account may be affected, sign out in `agy` (`/logout`) and sign in again, and review your account's security settings. The server never handles credentials.
