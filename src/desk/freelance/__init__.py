"""The freelance flow — a project is not a job, and it is judged on other facts.

Session 8. Everything else in this repo answers one question: should Noam apply
for this position. This package answers a different one: is this piece of work
worth bidding on, and what would the note that wins it say.

The distinction is not a nuance, it is the reason this is a separate flow
rather than a branch inside the analyst. A freelance project states no
seniority requirement and no degree requirement, so the two gates that decide
most postings in this repo have nothing to read and would pass every project on
silence. Running them here would produce a confident verdict from an empty
premise, which is worse than producing none. What a project does state is its
scope, its budget, its deadline and how many freelancers have already bid, and
those four are what this package reads.

    select.py     which stored projects are proposable at all, decided in
                  Python, and the facts read back out of the stored body
    proposal.py   the "freelance_proposal" stage: one Sonnet call, one draft
    command.py    `desk propose`, a dry run unless --write

Three rules hold across the package, and each of them is structural rather than
a sentence in a prompt:

**Nothing here submits a bid or contacts anybody.** There is no code path from
this package to a network write. It makes exactly one model call and that call
is given no tools, so there is no tool for a persuaded model to reach for; the
one tool in the registry that could contact an employer sits at the external
tier and `Policy` denies it unconditionally. `command.py` additionally refuses
to run at all if `manager.delivery.auto_apply` is ever edited away from
`never`, using the same check `desk digest` uses — one definition of that
promise, not two. The output is a draft file for a human to read, edit and
send.

**No price is proposed.** A bid on this site is a number the client is invited
to accept, and putting one in front of a human with a system's confidence
behind it is a commitment nobody in this repo is qualified to make. So the
draft states what the client budgeted, names what has to be known before a
number can be set, and leaves the number to Noam. The prompt says so and this
package never computes one.

**The CV is background, not the deliverable.** The tailoring agent in session 6
cuts a document for a position. Nothing of the sort happens here: the family
router runs deterministically, for free, and its only use is to tell the
proposal what Noam can credibly claim, in the specification's own vocabulary.
A proposal is a short note about the work, not a résumé with a covering
sentence.
"""

from __future__ import annotations

from .proposal import Proposal, build_request, proposal_from
from .select import DRAFT, SKIP, ProjectView, Refusal, screen, view_of

__all__ = [
    "DRAFT",
    "SKIP",
    "ProjectView",
    "Proposal",
    "Refusal",
    "build_request",
    "proposal_from",
    "screen",
    "view_of",
]
