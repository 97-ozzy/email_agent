# 02: Authenticate Gmail with read-only access

**What to build:** A user can authorize one Gmail account through the browser, reuse and refresh local credentials, and verify read-only access by finding messages and opening a message or conversation without granting modification capabilities.

**Blocked by:** 01: Boot a configurable local CLI

**Status:** ready-for-agent

- [ ] First use initiates an OAuth browser flow when valid credentials are unavailable.
- [ ] The requested Gmail scope is limited to `gmail.readonly`, and no send, draft, delete, archive, or label-modification operation is exposed.
- [ ] Valid credentials are stored locally with appropriate filesystem protection, reused on later launches, and refreshed when possible.
- [ ] Live operations can search messages, retrieve one message, and retrieve one thread using the selected open-source Gmail integration where its interfaces are sufficient.
- [ ] `/status` reports Gmail availability independently of Qdrant and local FastEmbed availability.
- [ ] Missing, invalid, or unrefreshable credentials produce an actionable message without exposing secrets.
- [ ] Tests use a controlled Gmail boundary and do not require personal credentials.
