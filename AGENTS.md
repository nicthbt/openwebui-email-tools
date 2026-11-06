# AGENTS.md

## What this repo is

An Open WebUI "tool": a single Python file pasted into Open WebUI's Tools editor.

- Source of truth: `src/` (entry point `src/main.py` defines the `Tools` class).
- `openwebui_email_tools.py` is the **generated** distribution file and is committed. Never edit it by hand — rebuild after changing `src/`:

  ```
  .venv/bin/python build.py --output openwebui_email_tools.py
  ```

  (Also the default VS Code build task.) The build ends with a `compile()` syntax check; a successful build is the main verification available here.

## Bundler constraints (`build.py` hard-fails on violations)

The bundler strips local imports and hoists globals, so `src/` code must follow these rules:

- Local imports: `from src.module import Name` only, at top level (not inside functions/classes). No `import src.x`, no `as` aliases, no wildcard imports.
- Statements the bundler removes (local imports, the module docstring) must be alone on their line.
- No circular imports; all local modules must live under `src/`.
- Duplicate module-level globals across files are deduplicated with a printed warning — `log = logging.getLogger(__name__)` in every module is expected — but two different declarations of the same name is an error.

## Tool metadata

The generated file's header (`title`, `version`, `required_open_webui_version`, `requirements`) comes from the module docstring in `src/main.py`. Edit that docstring — not the generated file — to bump the version or deps.

## Runtime context (why you can't run this locally)

The tool executes *inside* Open WebUI and imports its internals (`open_webui.models.*`, `open_webui.routers.*`, vector DB retrieval, storage). There is no test suite or CI, and importing the tool locally is impossible: the repo `.venv` contains only pip and exists just to run `build.py`. Unresolved imports of `open_webui`/`imapclient`/`fastapi` in an editor are normal.

## Tool anatomy

- `@custom_tool` on the `Tools` class mixes in `CustomTool` (`src/common/base.py`).
- Public tool methods (`search_email_messages`, `inspect_email_messages`, `write_email_message`) are decorated with `@with_context`, which opens the mail session, converts exceptions to `{"error": ...}` JSON, and manages per-call context.
- Dunder params (`__request__`, `__user__`, `__metadata__`, `__event_emitter__`, `__event_call__`) are injected by Open WebUI at call time — not regular arguments.
- `Valves` / `UserValves` are pydantic models declared on the `Tools` class; protocol handler dispatch lives in `_get_handlers`.

## Style

- Ruff is the formatter via the VS Code extension (format-on-save + organize imports). There is no ruff config file and no ruff binary in the venv — use defaults.
- Match existing code: double quotes, magic trailing commas, `_`-prefixed private methods.

## Compatibility

The tool imports Open WebUI internals, so releases of Open WebUI can break it (tested 0.10.2–0.12.5 per README). Keep `required_open_webui_version` in `src/main.py` accurate when touching internals imports.
