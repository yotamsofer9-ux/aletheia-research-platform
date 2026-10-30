# Source registries

The project keeps research-supporting and journalistic material in separate,
version-controlled registries:

- `research_sources.jsonl` contains scholarship, institutional research,
  official methodology, primary documents, and bounded operational references
  that support the research design or factual baseline.
- `journalistic_sources.jsonl` contains newsroom reporting, wire reports, and
  journalistic or OSINT investigations used as evidence in incident analysis.

They synchronize to separate BigQuery tables in the configured project and
dataset:

- `${GCP_PROJECT_ID}.${BQ_DATASET_NORMALIZED}.research_sources`
- `${GCP_PROJECT_ID}.${BQ_DATASET_NORMALIZED}.journalistic_sources`

The dataset location is `us-central1`; synchronization jobs must use the same
location.

Source IDs are globally unique across both registries. Factual anchors may
refer to the union of the two registries, but journalistic rows are not included
in the academic bibliography merely because they inform an anchor.

The 27 frozen articles that constitute the object of study remain article
documents, not bibliography entries. They are currently canonical in the local
`documents` table and will migrate to the BigQuery `documents` table at M3.
This avoids duplicating the research sample in the journalistic evidence
registry.

## Selection policy

- `INCLUDE` is reserved for peer-reviewed research, scholarly books, primary
  documents, and authoritative institutional research or methodology in the
  research registry.
- `SUPPORTING_ONLY` records a source used for a bounded operational decision but
  prevents it from being presented as core academic evidence. Journalistic
  sources are also stored as `SUPPORTING_ONLY`.
- Wikipedia, anonymous blogs, generic search-result snippets, and AI output are
  not research sources and are not entered.
- A low-quality source is never upgraded merely because it agrees with the
  project. If it materially influenced a past decision, it may be retained as
  `SUPPORTING_ONLY` with an explicit limitation note.
- Every row states what the source was used to support. A URL without a stated
  research use does not belong in the registry.
- Every row contains a complete APA 7 reference in the required
  `citation_text` column. The registry stores a plain-text representation, so
  APA ordering, capitalization, punctuation, author/date data, and the
  canonical URL or DOI are preserved; typographic italics are applied only
  when the final bibliography is rendered. Validation rejects an empty
  citation or one that omits the row's canonical URL/DOI.

Quality tiers:

- `A_PEER_REVIEWED`
- `A_AUTHORITATIVE_METHOD`
- `B_INSTITUTIONAL_RESEARCH`
- `B_PRIMARY_DOCUMENT`
- `B_PROFESSIONAL_INVESTIGATION`
- `C_OPERATIONAL_REFERENCE`
- `C_INTERESTED_PARTY_PRIMARY`
- `C_NEWS_REPORT`

The tier describes source form and reviewability, not whether Aletheia accepts
the source's conclusion. A primary document can establish that an institution
made a statement at a particular time while remaining insufficient to prove
the underlying event claim. Sources that analyze substantially the same public
images or allied intelligence must also be marked as potentially dependent in
the anchor evidence matrix; counting URLs is not counting independent evidence.

From this point forward, a qualifying source should be added to the appropriate
registry during the same work turn in which it materially informs the project.
Validate or synchronize each registry independently:

```bash
python3 scripts/sync_research_sources.py --validate-only
python3 scripts/sync_research_sources.py --kind journalistic --validate-only
python3 scripts/sync_research_sources.py
python3 scripts/sync_research_sources.py --kind journalistic
```

The sync replaces the cloud copy from the validated, version-controlled local
registry. It therefore requires `bigquery.jobs.create`, normally supplied by
`roles/bigquery.jobUser`, in addition to write access on the dataset. The
configured Aletheia project must provide the active research account with the
required access. Machine-specific project history is intentionally kept
outside the public repository.
