# Contributing

Thanks for taking the time. Issues and pull requests are welcome.

## Development setup

Follow [docs/setup.md](docs/setup.md) to run the stack on the host, or `make up` to run it in containers.
Install the tooling with `pip install -r requirements-dev.txt`.

## Before opening a pull request

CI runs these checks, so run them first:

```bash
make lint    # ruff check . && mypy src
make test    # backend tests (needs the compose stack) and frontend Vitest
LLM_PROVIDER=none python -m evaluation.run_eval --gate retrieval
```

The backend tests reset every tenant. Don't run them against a stack that holds data you want to keep.

## Guidelines

- Keep each pull request to one change, and explain why in the description.
- Add or update tests for behaviour changes. If a change moves retrieval quality, include the
  evaluation gate output.
- Put new configuration in `src/core/config.py` and document it in `.env.example`.
- Never commit `.env` or any real API key.
