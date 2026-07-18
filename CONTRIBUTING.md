# Contributing to Loop Memory

Thanks for considering a contribution! Loop Memory is a small,
zero-dependency framework, and we want to keep it that way.

## Ground rules

- The core `loop_memory` package stays dependency-free. New backends
  (LLM providers, vector stores, embedders) live in their own modules
  and are pulled in via `pyproject.toml` extras only.
- Public APIs must keep type hints and a docstring. If you change the
  shape of `LoopEngine.turn` or any `MemoryItem` field, update both
  the README tour and `tests/` accordingly.
- Run the test suite before opening a PR:

  ```bash
  python -m unittest discover tests -v
  ```

## Local dev loop

```bash
git clone https://github.com/<you>/loop-memory.git
cd loop-memory
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
python -m unittest discover tests -v
python -m loop_memory.examples.demo
```

## Adding a new LLM backend

1. Create `loop_memory/llm/<provider>_adapter.py`.
2. Implement an `LLMClient`-compatible class with `model: str` and
   `complete(history, **kwargs) -> str`.
3. Guard the import on the optional dependency and raise a clear
   `RuntimeError` with the right `pip install …` hint.
4. Add a `tests/test_<provider>_adapter.py` that mocks the SDK, and
   wire it into CI behind an env-var opt-in (so CI without API keys
   still passes).

## Adding a new vector backend

1. Subclass `loop_memory.backends.embedding.BaseEmbedder`.
2. Add it under `loop_memory/backends/<store>.py` and re-export from
   `loop_memory.backends.__init__`.
3. Document why your backend is a better default than
   `HashingEmbedder` for production usage.

## Commit & PR style

- One logical change per commit.
- PR title: `feat: …`, `fix: …`, `docs: …`, `chore: …`, `test: …`.
- Reference any issue number in the PR body.
