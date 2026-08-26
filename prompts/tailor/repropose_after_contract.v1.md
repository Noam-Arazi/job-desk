Your previous set of changes was rejected by the contract checker. Correct it and return the whole set again.

The rejection is mechanical, not editorial. Nothing below says your judgement about which lines to change was wrong — it says the changeset broke a rule the checker enforces in code. Fix exactly what the violations name and leave every other change as you wrote it.

The most common cause, by far: `add_term_to_existing_line` may add a term, and it may not lose one. `after` must contain every word that was in `before`, plus the new term. Rewriting the line in your own words drops something almost every time.

Rules:

- Return the complete corrected changeset, not a patch and not only the changes you fixed. What you return replaces the whole set.
- A change you cannot correct without breaking another rule is dropped from the set rather than repaired badly. A smaller changeset that passes is the better answer.
- Do not answer a violation by inventing wording that satisfies it. Every rule that applied to the first attempt applies to this one, and the same checker runs again.
- If the violations cannot be satisfied at all, return an empty `changes` list and say why in `missing_requirements`.

The changes you returned, which were rejected:

---
{rejected}
---

What the checker said, one violation per row:

---
{violations}
---

These rules are absolute and are checked in code after you answer:

{rules}

The base, one addressed line per row:

---
{lines}
---

Return only JSON matching the schema.
