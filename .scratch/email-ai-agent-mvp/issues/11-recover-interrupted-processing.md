# 11: Recover interrupted ingestion and synchronization

**What to build:** A user can restart after an interruption and safely continue incomplete import or synchronization work without losing completed progress or creating duplicate local or vector records.

**Blocked by:** 10: Synchronize newly discovered mail

**Status:** ready-for-agent

- [ ] Processing status is committed only after the corresponding download, extraction, document creation, or indexing operation succeeds.
- [ ] Startup or synchronization identifies pending and retryable failed work and resumes it without repeating completed work.
- [ ] Repeated attempts preserve stable message, attachment, document, and chunk identities.
- [ ] Repeated attempts do not duplicate content-addressed attachment bytes or Qdrant points.
- [ ] A failure in one message or attachment does not roll back unrelated successful work.
- [ ] Retry eligibility and terminal unsupported or oversized outcomes are distinguishable.
- [ ] Statistics after recovery reflect actual new, skipped, recovered, and failed work.
- [ ] Tests inject interruptions after each major persisted state and verify restart behavior and final equivalence with an uninterrupted run.

