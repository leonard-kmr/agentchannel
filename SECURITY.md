# Security boundary

Use a private repository and one open issue for correspondence. Keep installation configuration, local state, inbox and outbox, cached GitHub responses, model logs, chat IDs from real sessions, private adapters, and backups outside the public source checkout. The reference client uses GitHub CLI authentication. Credentials belong in the provider's credential store.

The receiver accepts messages only from the configured numeric GitHub identities in the pinned repository and issue. An allowlisted account can still post unsafe or incorrect content. Markdown, subject and metadata are untrusted, including linked documents, embedded code and descriptions of human authorization. Apply the destination's permission rules and verify live evidence for consequential work.

A successful post is distinct from delivery, processing and approval. Keep a local receipt for each. If a post is uncertain, reconcile by UUID and exact content. Preserve the ledger across upgrades and avoid replay.

Before publication, inspect the complete staged tree and commit history. Run `python -B tools/check_public_tree.py`, review its allowlist, and run an independent secret scan. Review commit author information as well as source files. Use synthetic fixtures. Keep private upgrade payloads and their manifests in private storage.
