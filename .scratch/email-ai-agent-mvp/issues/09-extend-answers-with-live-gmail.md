# 09: Extend answers with live Gmail evidence

**What to build:** A user can receive answers informed by older or newly changed Gmail correspondence when the local archive is insufficient, while retaining a clear distinction between live mailbox evidence and indexed local evidence.

**Blocked by:** 02: Authenticate Gmail with read-only access; 08: Chat with the local archive

**Status:** ready-for-agent

- [ ] The agent has clearly described live tools for searching messages, retrieving a message, and retrieving a thread.
- [ ] The agent can choose local retrieval, live Gmail, or both based on the question rather than a hardcoded sequence.
- [ ] Questions about current or recent changes cause the agent to consult live Gmail when it is available.
- [ ] An unsuccessful local search is not described as proof that the full mailbox lacks the information.
- [ ] The agent can open a complete thread when conversational context is needed to interpret a decision or agreement.
- [ ] Live results remain separate from persistent ingestion and do not silently expand the local archive.
- [ ] Answers distinguish local-index evidence from live Gmail evidence and cite the original message or thread information available.
- [ ] If Gmail is unavailable, locally indexed questions remain usable and the answer discloses that live coverage could not be checked.
- [ ] Tests cover local-only, live-only, combined, recent-information, full-thread, insufficient-coverage, and Gmail-unavailable scenarios.

