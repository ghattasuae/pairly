# pairly-expense

Pairly expense notes (`pairly_expense`): a small, complete workflow plugin. An employee uploads a receipt, Pairly reads it, checks it against the expense policy and scores the risk, and a person always decides. Approving marks the note reimbursable; no money moves.

It is the M1 acceptance workflow and the worked example of [docs/add-a-workflow.md](../../docs/add-a-workflow.md). It needs no change in the core engine.

| Piece | File |
|---|---|
| Plugin object (entry point `expense`) | `pairly_expense/plugin.py` |
| Permissions and default roles | `pairly_expense/permissions.py` |
| Extraction schema | `pairly_expense/schema.py` |
| Policies as code (rules, risk, approval rules) | `pairly_expense/policy/*.yaml`, `pairly_expense/policies.py` |
| Graph `expense_note` v1 | `pairly_expense/graph.py` |
| Final action `mark_reimbursable` | `pairly_expense/actions.py` |
| Table `expense_notes` | `pairly_expense/models.py`, migration `pairly_expense/migrations/expense_0001_expense_notes.py` (Alembic branch `expense`) |
| Routes under `/v1/expense` | `pairly_expense/api.py` |
| Notification templates | `pairly_expense/templates.py` |
| Demo seed `expense-demo` | `pairly_expense/seed.py` |
| Recorded model responses | `pairly_expense/data/recordings.json` (made by `uv run python -m synth.expense`) |

Synthetic receipts live in `synth/expense/receipts`. All data is made up.
