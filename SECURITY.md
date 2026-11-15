# Security policy

## Supported versions

Security fixes target the current `main` branch and the latest tagged release, when one exists. Older commits and experimental branches are unsupported. Update before reporting a suspected vulnerability. There is no guaranteed response SLA for this volunteer project.

## Local trust boundary

Job_Runner is a single-user, local-first application. The API has no authentication; it can read résumé data and trigger application workflows. API, dashboard, PostgreSQL, and optional Redis must bind to loopback. Keep `ALLOW_NON_LOCAL=false`. Do not expose them through a public interface, tunnel, reverse proxy, or shared machine account without an independently designed authentication boundary. Loopback and Origin/Host checks reduce network/browser attacks; they do not isolate untrusted processes on the same machine.

`AUTO_SUBMIT=false` is the shipped default. Keep human review enabled, inspect the résumé diff and answers, and approve each application deliberately. A per-profile opt-in and score threshold apply to automated submission. Unknown answers park for review. Work authorization and employment history must come verbatim from the profile. CAPTCHA, OTP, or site restrictions require the owner's involvement; no evasion is supported.

## Credentials, browser sessions, and personal data

- Playwright profiles, cookies, session exports, screenshots, and résumés are sensitive. Keep them under ignored local storage with private filesystem permissions. Use a dedicated browser profile and account where practical. Never attach session state or full page HTML to issues.
- ATS credentials use the Fernet vault in `packages/core/vault.py`. Keep `VAULT_KEY` separate from encrypted credential files, preferably outside the checkout. Backups containing the database or storage are personal data too.
- `.env.example` contains placeholders and local Compose credentials only. `.env`, `.env.*` backups, `.secrets/`, and storage must stay out of commits and image layers. Rotate any credential that has entered git history; deleting a file does not revoke it.
- Local model inference keeps prompts on the machine. Explicitly selected remote LLM providers can receive résumé/posting text; inspect their policies and the local LLM audit records before enabling them. GitHub tokens should have the narrowest read permissions needed.
- Redis is a disposable vector/metadata cache. It must contain no résumé, credentials, or browser state. PostgreSQL revalidates candidate hits. Redis metadata is eventually consistent: sync after changing or deleting postings. Rebuild after cache loss.

## Reporting a vulnerability

Do not put secrets, personal data, exploit payloads against live employers, or unpatched vulnerability details in a public issue. On the repository's GitHub **Security → Advisories** page, use **Report a vulnerability** if private reporting is enabled. Maintainers should enable private reporting before public release. If unavailable, open a minimal issue asking for a private reporting channel without disclosing the vulnerability, and wait for that channel before sending details.

Include the affected commit/version, OS and runtime versions, a minimal synthetic reproduction, expected versus actual behavior, impact, and a suggested fix if available. Test only against your own local instance and fixture forms. Maintainers will acknowledge and triage when available, coordinate a fix and disclosure with the reporter, and credit reporters who consent. If exposed credentials are involved, revoke them immediately rather than waiting for a fix.
