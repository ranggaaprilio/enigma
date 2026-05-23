# Execute Dashboard Backend

FastAPI service for the Execute Dashboard foundation.

## Commands

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
uv run alembic upgrade head
```

Editor sessions require `code-server` on `PATH` unless you run the provided
backend container, which installs it during image build.
