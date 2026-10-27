# CLAUDE.md

Read [AGENTS.md](AGENTS.md) first; it has the rules for this repo. The short version:

- Public MIT repo. No secrets, real IPs, hostnames or people's data in commits. Run `gitleaks git .` before committing.
- File operations stay inside `fsops` + `as_user`. Don't weaken sign-in lockouts, the cross-site write check or the preview CSP.
- New options go in Settings (`app/runtime_settings.py`) with an env default. Setup must never need a hand-edited config file.
- UI follows [DESIGN.md](DESIGN.md). Bump `?v=` on changed assets.
