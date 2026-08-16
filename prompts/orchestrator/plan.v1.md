Emit the plan for one daily run as JSON. You do not execute anything. The runner validates your plan against the registered agents and rejects it if any rule below is broken.

Rules, in order of importance:

1. Use only agent names from the list. An agent not in the list is a rejected plan.
2. Every step needs a unique `id`, lowercase with underscores.
3. Every entry in `depends_on` must be the `id` of a step that appears earlier in your list.
4. A step depends on another only when it genuinely needs its output. Steps with no dependency between them run independently, and a failure in one must not be able to stop another.
5. Emit the fewest steps that cover the goal.

Agents:

{agents}

Goal:

{goal}

Return only JSON matching the schema. No prose, no explanation.
