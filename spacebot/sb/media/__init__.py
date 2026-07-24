"""Multimodal ingestion: any source (pdf / image / png-sequence / video / audio /
transcript) is decomposed by a per-modality Adapter into a uniform stream of Segments,
enriched by pluggable capability providers, then structured into a workflow draft.

The structuring engine never sees the original file type — only Segments. That is what
makes adding a new modality a new Adapter, not a rewrite.
"""
