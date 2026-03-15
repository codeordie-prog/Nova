# Contributing

This repository is intended to be production-grade. Contributions should prioritize correctness, readability, security, and maintainability.

## Workflow (Git)

- **No direct commits to `main`**.
- Create a feature branch from `main`:
  - `feature/<short-description>`
  - `fix/<short-description>`
  - `chore/<short-description>`
- Open a **Pull Request (PR)** for review.
- Keep PRs small and focused (one purpose).
- Prefer **rebase/merge with a clean history** as agreed by the team.

## Code quality standards

### Static typing (required)

- All new code must have **static types**.
- Use modern Python typing:
  - `list[str]`, `dict[str, int]`, `str | None` (Python 3.12+)
- Avoid `Any` unless unavoidable. If used, justify it in the PR.

### Documentation and readability (required)

- Public functions/classes must have **clear docstrings**.
- Docstrings should describe:
  - purpose
  - arguments
  - return values
  - raised exceptions (when meaningful)
- Keep code readable:
  - small functions
  - descriptive names
  - avoid deeply nested logic
- **No grammatical errors** in code comments, docstrings, or docs.

### Error handling (required)

- Handle expected failures explicitly and return **actionable, precise** error messages.
- Prefer **domain exceptions** in lower layers, mapped to HTTP errors at the API boundary.
- Do not swallow exceptions silently.
- Do not leak sensitive/internal details in HTTP errors.

### Security and safety (required)

- Never commit secrets:
  - API keys
  - tokens (e.g., `HF_TOKEN`)
  - `.env` files containing credentials
- Validate untrusted inputs at boundaries (e.g., file uploads, JSON payloads).
- Apply limits to prevent resource exhaustion:
  - maximum upload sizes
  - timeouts
  - concurrency caps
  - rate limits

### Logging and observability

- Log important lifecycle events and failures.
- Use structured, consistent messages.
- Avoid logging sensitive data (tokens, full payloads, base64 images).

## Testing expectations

- Add or update tests when changing behavior.
- Tests should cover:
  - happy paths
  - validation failures
  - error handling paths

## Style guidelines

- Keep imports at the top of the file.
- Prefer deterministic, explicit code over clever shortcuts.
- Avoid unnecessary dependencies.
- Keep documentation up to date when behavior changes.

## Modal / model-serving guidelines

- Treat the FastAPI gateway as a protective layer:
  - validate inputs
  - apply timeouts
  - apply concurrency limits
  - apply rate limits
- Long-running inference must be bounded by timeouts.

## PR checklist

Before requesting review:

- [ ] Types added/updated for new code
- [ ] Docstrings updated
- [ ] Validation and error handling are explicit and user-friendly
- [ ] No secrets committed
- [ ] Tests added/updated (when applicable)
- [ ] Docs updated (README, module docs)
