# 14: Document and verify the complete MVP

**What to build:** A user can follow the documentation from an empty environment through OAuth, import, search, chat, restart, synchronization, reindexing, troubleshooting, and complete local-data reset, while a developer can verify the agreed end-to-end acceptance scenario.

**Blocked by:** 13: Operate safely through partial outages

**Status:** ready-for-agent

- [ ] Documentation covers prerequisites, uv installation, environment configuration, Docker startup and persistence, Google OAuth setup, OpenRouter configuration, and embedding-model setup.
- [ ] Documentation explains first import, interactive commands, direct search, synchronization, reindexing, troubleshooting, privacy boundaries, and complete local-data reset.
- [ ] The reset procedure identifies all application-owned data and credentials to remove without risking unrelated user files.
- [ ] The documented manual acceptance flow verifies import statistics, an email answer, an attachment answer, source citations, restart reuse, receipt of a new test message, synchronization, and subsequent searchability.
- [ ] The application is checked on supported Windows, Linux, and macOS environments where dependencies permit, or any unverified platform limitations are stated explicitly.
- [ ] Automated acceptance coverage uses the agreed CLI/application seam with synthetic mail and controlled external adapters.
- [ ] The final verification confirms that normal operation requires no source-code edits between setup, launch, restart, and synchronization.

