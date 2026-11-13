# Ticket 10.5 implementation review

Fixed point: `2814e528c08a48803261fe9a26547a6d23457683`.
Reviewed implementation: initial commit `3a3c4f4` and subsequent correction diff.
Two independent agents reviewed standards and spec in parallel, as required by code-review.

## Standards

No documented-standard violations found. Sources: AGENTS.md, its three referenced guides,
and pyproject.toml; no additional CONTRIBUTING.md/CODING_STANDARDS.md, glossary or ADRs.
Tooling-enforced rules excluded.

One low-priority judgement-call smell: repeated Gmail tool discriminator maps for the
callable and the span name. Fixed by combining callable/name pairs in one map.

Separate correctness concern: absent Gmail adapter could classify an answer disclosing
unavailable Gmail as successful. A regression test reproduced this failure. Fixed outcome
classification to use `investigation.live_unavailable`, preserving all answer behavior.

## Spec

No additional material spec-compliance defects found. No material unrequested scope.
Existing answer/fallback/history and tool-support behavior preserved; native parentage,
session identity, concurrency isolation, default privacy and export-event sanitation tested.

Remaining disclosed limitations:

1. Native model requests/failures are separate generations, but hidden OpenAI transport
   retries remain aggregated into their native request generation.
2. Standard official Langfuse Compose validates; real UI startup/ingestion unverified because
   upstream MinIO image pull returned 403.
3. Official direct OTLP ingestion used instead of preferred Langfuse SDK-v4 client, with
   queue/privacy/lifecycle ownership rationale documented in the operations guide.

Content debugging remains sensitive and uses heuristic credential redaction, not a general
secret detector. Exporter tests cannot prove live Langfuse UI presentation or ingestion.

Summary: Standards 0 hard violations, 1 smell corrected; 1 separate correctness defect
corrected. Spec 0 additional defects, 3 documented limitations/preferences remaining.
