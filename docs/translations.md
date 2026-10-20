# Why the agent-facing documents are English-only

**Status:** the reasoning behind the policy stated in
[`CONTRIBUTING.md`](../CONTRIBUTING.md) · **Scope:** `/llms.txt`, `/skill.md`, `/patterns.md`,
`/interop.md`, `/auth.md` and the refusal bodies — the documents whose reader is a model

The policy is short: those documents are English-only, a pull request that adds a translated copy
of one is declined, and this is about instructions written *for agents*, not about people. What
follows is why, and what would change it.

## The reason is drift

These documents carry the sentences an agent's safety rests on — `TRUST`, the `!! UNTRUSTED
CONTENT` banner, the swept character set a signature has to match — and a second copy of them can
lag the first by a commit. A stale translation of a warning is worse than none, because it is still
believed. Keeping copies current is machinery, not goodwill: a maintainer per language, tooling
that shows what the English source changed under them, and a check that fails while they disagree.
Nothing here is set up to carry that, and the reader of these files is a model, so the gain that
would pay for building it is not the obvious one.

## What would change it

The bar is therefore a measurement rather than an argument: an eval that runs the same tasks
against a real instance, one arm given the English document and one given your translation, scored
on the server's answer and on what landed; a result where the translated arm does something the
English arm does not; and a harness that holds the copy in sync and fails CI when it drifts,
generated from the same constants the server enforces rather than restated by hand in prose.

Until then, publish the translation in your own repository — name the upstream commit it was built
from, say plainly that the English document is authoritative, and list it from a community index
such as an `awesome-technocore` repository. And if translating showed you something the English
document gets wrong or leaves unsaid, that is a bug in the English document: send it as its own
small pull request. Those land.
