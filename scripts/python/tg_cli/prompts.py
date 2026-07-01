"""Prompt abstraction — questionary when available, plain input() else.

Keeping prompting behind a tiny interface means:
  * the wizard code reads the same whether questionary is installed,
  * --non-interactive mode can swap in a no-prompt resolver that pulls
    from env/config and never blocks,
  * unit tests can inject a scripted resolver (no TTY needed).

Every question carries the #487 "question-clarity contract": a plain
question, a one-line WHY, the discovered default, and validation.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

try:  # questionary is optional — degrade to input() if absent.
    import questionary  # type: ignore

    _HAVE_QUESTIONARY = True
except ImportError:  # pragma: no cover - exercised via the fallback
    questionary = None  # type: ignore
    _HAVE_QUESTIONARY = False


class PromptAbort(Exception):
    """Raised on Ctrl-C / EOF so the caller can persist + exit 130."""


@dataclass
class Question:
    key: str
    prompt: str
    why: str
    default: str | None = None
    choices: list[str] | None = None
    validate: Callable[[str], str | None] | None = None  # ret err or None
    secret: bool = False


@dataclass
class Resolver:
    """How answers are obtained. interactive=False never prompts."""

    interactive: bool = True
    # supplied answers (env/config) consulted before prompting
    supplied: dict = field(default_factory=dict)
    # test hook: scripted answers keyed by Question.key
    scripted: dict | None = None

    def ask(self, q: Question) -> str:
        # 1. scripted (tests) wins.
        if self.scripted is not None and q.key in self.scripted:
            return self._checked(q, str(self.scripted[q.key]))
        # 2. already supplied via env/config.
        if q.key in self.supplied and self.supplied[q.key] not in (None, ""):
            return self._checked(q, str(self.supplied[q.key]))
        # 3. non-interactive with no value → use default or fail.
        if not self.interactive:
            if q.default is not None:
                return self._checked(q, q.default)
            raise PromptAbort(
                f"--non-interactive: no value for required '{q.key}' "
                f"(set it via env/config)"
            )
        # 4. interactive prompt.
        return self._prompt(q)

    def _prompt(self, q: Question) -> str:
        self._show_context(q)
        while True:
            try:
                if q.choices and _HAVE_QUESTIONARY:
                    ans = questionary.select(
                        q.prompt, choices=q.choices, default=q.default
                    ).ask()
                elif q.secret and _HAVE_QUESTIONARY:
                    ans = questionary.password(q.prompt).ask()
                elif _HAVE_QUESTIONARY:
                    ans = questionary.text(q.prompt, default=q.default or "").ask()
                else:
                    ans = self._input_fallback(q)
            except (KeyboardInterrupt, EOFError):
                raise PromptAbort("interrupted")
            if ans is None:  # questionary returns None on Ctrl-C
                raise PromptAbort("interrupted")
            ans = ans.strip()
            if not ans and q.default is not None:
                ans = q.default
            err = q.validate(ans) if q.validate else None
            if err:
                print(f"  ✗ {err}", file=sys.stderr)
                continue
            return ans

    def _input_fallback(self, q: Question) -> str:
        if q.choices:
            for i, c in enumerate(q.choices, 1):
                print(f"    {i}) {c}")
        suffix = f" [{q.default}]" if q.default else ""
        raw = input(f"{q.prompt}{suffix}: ")
        if q.choices and raw.strip().isdigit():
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(q.choices):
                return q.choices[idx]
        return raw

    @staticmethod
    def _show_context(q: Question) -> None:
        print()
        print(f"  {q.why}")
        if q.default is not None:
            print(f"  (discovered default: {q.default})")

    def _checked(self, q: Question, val: str) -> str:
        err = q.validate(val) if q.validate else None
        if err:
            raise PromptAbort(f"invalid value for '{q.key}': {err}")
        return val

    # #774: multi-select (questionary checkbox) for the BYO-subnet
    # pick-list. scripted/supplied wins (tests + env), else a live
    # checkbox; non-interactive with no value raises (the caller
    # falls back to the comma-separated text question).
    def ask_multi(self, q: Question) -> list[str]:
        if self.scripted is not None and q.key in self.scripted:
            v = self.scripted[q.key]
            return list(v) if isinstance(v, (list, tuple)) else [v]
        if q.key in self.supplied and self.supplied[q.key]:
            v = self.supplied[q.key]
            return v if isinstance(v, list) else [s.strip() for s in str(v).split(",") if s.strip()]
        if not self.interactive:
            raise PromptAbort(
                f"--non-interactive: no value for required '{q.key}'"
            )
        self._show_context(q)
        try:
            if q.choices and _HAVE_QUESTIONARY:
                ans = questionary.checkbox(q.prompt, choices=q.choices).ask()
            else:
                # plain fallback: numbered list, comma-separated indices.
                for i, c in enumerate(q.choices or [], 1):
                    print(f"    {i}) {c}")
                raw = input(f"{q.prompt} (comma-separated #s): ")
                ans = []
                for tok in raw.split(","):
                    tok = tok.strip()
                    if tok.isdigit() and 1 <= int(tok) <= len(q.choices or []):
                        ans.append(q.choices[int(tok) - 1])
        except (KeyboardInterrupt, EOFError):
            raise PromptAbort("interrupted")
        if ans is None:
            raise PromptAbort("interrupted")
        return list(ans)

    def note(self, msg: str) -> None:
        """Surface a non-fatal hint (e.g. a re-ask reason)."""
        print(f"  ! {msg}", file=sys.stderr)
