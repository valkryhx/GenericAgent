# Workflow Patterns and Examples

## 5. Patterns that improve complex-task quality

- **Adversarial verify** — spawn independent skeptics for each important
  finding, prompt each to REFUTE it, and keep it only when enough skeptics fail
  to refute. Make the refuter fail-closed — unsure means refuted:
  ```js
  const votes = await parallel(Array.from({ length: 3 }, () => () =>
    agent(`Try to refute: ${claim}. Default to refuted=true if uncertain.`,
          { schema: VERDICT })))
  const survives = votes.filter(Boolean).filter(v => !v.refuted).length >= 2
  ```
- **Perspective-diverse verify** — give each verifier a distinct lens
  (correctness, security, performance, reproducibility) instead of N identical
  copies.
- **Judge panel** — generate independent approaches, score them with separate
  judges, and synthesize from the winner while grafting the best ideas from the
  runners-up.
- **Loop until dry** — for unknown-size discovery, keep searching until several
  consecutive rounds return no new findings. Add a hard iteration cap.
  Dedup against everything **seen**, not against the **confirmed** set: otherwise
  a finding a judge rejected reappears every round and the loop never converges.
- **Multi-modal sweep** — use different search angles in parallel so one angle's
  blind spot is covered by another.
- **Completeness critic** — end with an agent that asks what modality, claim, or
  source is still missing.
- **No silent caps** — if the workflow samples, truncates, or skips work, call
  `log()` and include that fact in the final result.
- **Pass values, don't share files** — make analyze / research / verify stages
  return structured values via `schema` and let `parallel()` / `pipeline()`
  carry them forward. Only producing stages write files. When two or more
  producing stages must write files in parallel, give them
  `isolation: "worktree"` so each writes in its own directory; never rely on two
  agents writing the same shared path and hope the last write wins.

Scale the fan-out to what the user asked for. A quick review gets a small number
of finders and one verify pass. A comprehensive audit gets a larger finder pool,
multi-vote verification, and a synthesis stage.
