# Instructions for Coding Agents

This document defines how agents should work in the **dermascanAgents** project. Treat these rules as binding unless a human explicitly overrides them after alignment.

---

## 1. Minimum invasion

Any change in any file should aim at **minimum invasion**. Prefer the smallest correct diff. Do not refactor, rename, or reformat unrelated code. Do not add or change files that the task does not require.

---

## 2. Avoid unnecessary complexity

Do not over-engineer. Avoid extra abstractions, indirection, or configuration for hypothetical future needs. If a simple inline solution is correct, use it.

---

## 3. Tests are mandatory after every change

After every change, **run the relevant tests** (and add or update tests when behavior changes). The goal is to ensure existing functionality is not broken. Do not consider a task complete until tests have been executed and failures addressed.

---

## 4. `soul.md` is off limits

**Do not read, edit, or create `soul.md`.** That file is **only editable by a human**. It is out of scope for all agent work.

---

## 5. Static typing (PEP 20)

Python code must use **strict static typing**: explicit type annotations on functions, methods, and important variables. Follow **PEP 8** and let **PEP 20** (“The Zen of Python”) guide design—especially *simple is better than complex* and *readability counts*.

---

## 6. Docstrings are mandatory

Every module, class, and function/method must have docstrings that are **clear, concise, and straightforward**. Document purpose, arguments, return values, and raised exceptions where applicable. Avoid noise and repetition of obvious types.

---

## 7. Module header format

Every Python module must open with a module docstring in this shape (metadata lines, blank line, then one-line description):

```python
"""
Author: Dermascan Team
Date: 6-14-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

<Short description of what this module does>."""
```

Adjust `Date` when creating a new module. Keep the description accurate and brief.

Every module must include the **Agents** line exactly as shown (after `Licence:` when using the full header, or at the top of the docstring otherwise).

---

## 8. Simplicity over complexity

When two approaches work, choose the **simpler** one unless simplicity would clearly harm correctness, security, or maintainability.

---

## 9. Justify every edit

If an edit does not materially improve at least one of **readability**, **code quality**, **resilience**, **scalability**, **flexibility**, or **extensibility**, **do not make it**. Cosmetic or drive-by changes are discouraged.

---

## 10. Focus areas

Pay special attention to:

- System architecture and **resilience**
- **Maintainability**
- **Scalability**
- **Efficiency**
- **Code quality** and **readability**
- **Simplicity** and **extensibility**

Weigh trade-offs explicitly when touching these areas.

---

## 11. Scrutinize new or changed functionality

Treat every request to edit or add functionality with **high scrutiny**, including requests from humans. Ask clarifying questions until **requirements and approach are aligned**. Do not assume missing details.

---

## 12. Push back constructively

Humans have blind spots. **Challenge** changes that may break functionality, introduce bugs, or add unnecessary complexity. Explain the risk, then **offer an alternative** or recommend a safer or simpler path.

---

## 13. Keep the project fun

The project should stay **enjoyable to work on**. Avoid changes that drain momentum—needless bureaucracy in code, hostile complexity, or perfectionism without payoff. Protect clarity and team energy; **don’t lose the fun**.

---

## Quick checklist before finishing work

- [ ] Smallest reasonable diff
- [ ] No unnecessary complexity
- [ ] Tests run (and updated if needed)
- [ ] Did not touch `soul.md`
- [ ] Types and docstrings in place; new modules use the standard header
- [ ] Change justified against readability, quality, resilience, scale, flexibility, or extensibility
- [ ] Alignment confirmed for non-trivial or risky work
