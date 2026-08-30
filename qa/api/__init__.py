"""HTTP-level tests for `cb-api` — smoke, contract and integration.

The three files here answer three different questions and are runnable
separately, which is the point of splitting them:

| file | question | needs |
|---|---|---|
| `test_smoke.py` | does the deployment I just started answer at all? | a **running** API (`uv run scripts/qa_setup.py`) |
| `test_contract.py` | does every response match the shape `openapi.json` promises? | a database |
| `test_integration.py` | does the API behave correctly against real rows? | a database |

Everything skips cleanly when what it needs is absent, and says what to run.
"""
