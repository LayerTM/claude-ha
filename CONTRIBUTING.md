# Contributing

Thanks for your interest in improving Claude for Home Assistant. This is a
Home Assistant custom integration built to the Core quality bar (quality scale:
platinum), so contributions are expected to keep it there.

## Architecture in one paragraph

The integration is the **client** half of a small, versioned HTTP contract with
the [Claude Code add-on](https://github.com/LayerTM/ClaudeInHA) (a separate
repository). The two are developed independently and connect only through that
contract. Changes to the wire format must be agreed on the contract first, then
implemented on both sides — do not add request/response fields unilaterally.

## Development setup

Requires Python 3.14 (matching current Home Assistant).

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt
pre-commit install        # optional but recommended
```

## Checks (all must pass)

```bash
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
mypy custom_components/claude_ha          # strict
pytest --cov=custom_components.claude_ha --cov-report=term-missing --cov-fail-under=100
python scripts/secret_scan.py .
```

CI runs the same set plus `hassfest` and HACS validation on every push and pull
request.

## Standards

- **Follow Home Assistant conventions.** Use `entry.runtime_data`, typed config
  entries, `DataUpdateCoordinator`, translated exceptions, and the entity
  platform patterns already in the codebase.
- **Coverage.** `config_flow.py` stays at 100%; overall coverage stays at 100%.
- **Typing.** `mypy --strict` must pass with no new ignores.
- **No secrets or personal data.** The secret scan blocks tokens, keys, personal
  paths and emails; keep test data synthetic.
- **Docs.** Update `README.md` and `CHANGELOG.md` when behaviour changes.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; explain the *why* in the description.
3. Ensure every check above passes locally.
4. Reference any related issue.

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
