The `long_and_short` editor output contains `long` and `short` arrays. Each entry has `text` and a nonempty `source_ids` array referencing the submissions that support that position. The model output schema requires the object and both arrays; either array can be empty. Older drafts without the object remain supported by local validation and rendering.

For example, two contributors sharing a position can be represented as:

```json
{
  "long_and_short": {
    "long": [
      {"text": "Lorem ipsum", "source_ids": ["alex-001", "bob-001"]}
    ],
    "short": []
  }
}
```

Contributor names are resolved from the referenced submissions and saved as a `contributors` list in issue data, alongside the original source IDs. The renderer displays them inline in smaller, lighter text. It combines matching entries within each column, ignoring capitalisation, whitespace and final sentence punctuation, and removes repeated names. It never combines entries across Long and Short. Earlier preview data using an `attribution` string still renders.

Validation rejects empty positions, missing or unknown sources, and sources with no submitted text. Positions count towards submission coverage and are available as context to the captioner. This validates structure and attribution; it cannot establish that a contributor explicitly intended a position, that all relevant details of a submission were retained, or that differently worded positions have equivalent meaning.

Prompt v2.0 requires clear contributor intent, permits combining semantically equivalent positions with all supporting source IDs, preserves meaningful differences, and controls repetition elsewhere while retaining useful surrounding context. An isolated model trial using four current submissions correctly extracted one contributor's three explicit Long entries and one Short entry without inferring positions from the other contributors. This trial does not establish grouping quality for multiple contributors or follow-up corrections.
