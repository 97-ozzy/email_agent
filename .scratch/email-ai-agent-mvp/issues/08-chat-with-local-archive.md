# 08: Chat with the local archive

**What to build:** A user can ask natural-language questions in the CLI and receive evidence-grounded answers from the local archive, with citations, bounded investigation, safe treatment of retrieved content, and conversational follow-up support.

**Blocked by:** 05: Search archived email bodies with hybrid retrieval; 07: Expand neighboring document context

**Status:** ready-for-agent

- [ ] Pydantic AI uses the configured OpenRouter model and detects missing credentials or unsupported tool-calling capability as early as practical.
- [ ] The agent can call `search_email_rag` and `get_local_document` more than once when additional evidence is needed.
- [ ] Tool results contain evidence and metadata rather than a separately generated RAG answer.
- [ ] Email-related factual claims cite available message and attachment evidence near the supported claims.
- [ ] The agent discloses insufficient evidence, identifies conflicting sources, and does not present retrieval scores as factual probabilities.
- [ ] System instructions treat email and attachment contents as untrusted data and prohibit following instructions embedded in them.
- [ ] A configurable tool-call limit defaults to approximately 10; reaching it returns the best supported partial answer and discloses the limitation.
- [ ] The latest 10 user-assistant exchanges are retained in memory with structurally valid tool messages, and `/clear` resets only conversation history.
- [ ] The CLI shows concise tool-activity notifications without exposing private reasoning or full email contents in logs.
- [ ] Controlled-agent tests assert observable grounded outcomes, citations, conflict handling, prompt-injection resistance, follow-ups, history clearing, and execution-limit behavior.

