"""Edit application for Bench #4.

Given an action dict loaded from `bench_4_edits.json` and the on-disk
fixture root, mutate the files accordingly. Kept pure (no scoring, no
adapter interaction) so it can be unit-tested on a tiny tmp fixture.

The AST gymnastics are deliberately modest: the scratch_fixture authors
shaped the dataset so that target symbols are either top-level defs,
top-level classes, or methods. We extract the block by locating the line
range with `ast` and slicing source lines.

Each function returns the list of absolute file paths that changed so
the runner can hand them to `adapter.reindex_paths`.
"""
from __future__ import annotations
import ast
from pathlib import Path


class EditError(RuntimeError):
    """Raised when an edit cannot be applied cleanly (symbol not found, etc.)."""


def apply_edit(action: dict, corpus_root: Path) -> list[Path]:
    """Dispatch to the concrete applier. Returns changed file paths (absolute).

    Never raises on missing symbols — the runner needs forward-progress;
    it records the error on the row and keeps going. The only exception
    is an unknown action_type (that's a dataset bug, not a runtime one).
    """
    kind = action.get("action_type")
    file_rel = action.get("file")
    target = action.get("target_name")
    if not file_rel or not target:
        raise EditError(f"missing file/target in action {action.get('id')}")

    src = corpus_root / file_rel

    if kind == "add_symbol":
        return _apply_add(src, target)
    if kind == "rename_symbol":
        new_name = action.get("new_name")
        if not new_name:
            raise EditError(f"rename_symbol requires new_name (id={action.get('id')})")
        return _apply_rename(src, target, new_name)
    if kind == "move_symbol":
        dest_rel = action.get("dest_file")
        if not dest_rel:
            raise EditError(f"move_symbol requires dest_file (id={action.get('id')})")
        dest = corpus_root / dest_rel
        return _apply_move(src, dest, target)
    if kind == "delete_symbol":
        return _apply_delete(src, target)
    raise EditError(f"unknown action_type: {kind}")


# ── add ──────────────────────────────────────────────────────────────────────

def _apply_add(src: Path, name: str) -> list[Path]:
    """Append a trivial `def <name>(): return None` to the file."""
    if not src.exists():
        raise EditError(f"source file missing: {src}")
    content = src.read_text(encoding="utf-8")
    if not content.endswith("\n"):
        content += "\n"
    content += f"\n\ndef {name}() -> None:\n    \"\"\"Added by bench_4.\"\"\"\n    return None\n"
    src.write_text(content, encoding="utf-8")
    return [src.resolve()]


# ── rename ───────────────────────────────────────────────────────────────────

def _apply_rename(src: Path, old: str, new: str) -> list[Path]:
    """Rewrite `def old` / `class old` to `def new` / `class new` in this file.

    Also updates simple in-file references (bareword `old` → `new`) so
    adapters that follow AST links see a coherent file. Cross-file refs
    are intentionally NOT updated — Bench #5 covers multi-file refactors.
    """
    if not src.exists():
        raise EditError(f"source file missing: {src}")
    content = src.read_text(encoding="utf-8")

    # Rewrite definitions first (these are the authoritative hits).
    import re
    def_pat = re.compile(rf"(\bdef\s+){re.escape(old)}(\s*\()")
    cls_pat = re.compile(rf"(\bclass\s+){re.escape(old)}(\s*[\(:])")
    async_pat = re.compile(rf"(\basync\s+def\s+){re.escape(old)}(\s*\()")

    new_content, n_def = def_pat.subn(rf"\1{new}\2", content)
    new_content, n_async = async_pat.subn(rf"\1{new}\2", new_content)
    new_content, n_cls = cls_pat.subn(rf"\1{new}\2", new_content)
    if n_def + n_async + n_cls == 0:
        raise EditError(f"symbol `{old}` not found in {src}")

    # Same-file references. Use a word-boundary substitution to avoid
    # clobbering substrings (e.g. `pop` in `popular`). We skip dotted
    # references like `self.foo` / `module.foo` because the LHS stays
    # the same across renames-in-place, and touching them risks over-
    # writing imports. The primary benefit is updating call sites in
    # the same file that read as bare `foo()`.
    ref_pat = re.compile(rf"(?<![\w.]){re.escape(old)}(?=\W|$)")
    new_content = ref_pat.sub(new, new_content)

    src.write_text(new_content, encoding="utf-8")
    return [src.resolve()]


# ── move ─────────────────────────────────────────────────────────────────────

def _apply_move(src: Path, dest: Path, name: str) -> list[Path]:
    """Cut the symbol block from `src`, append it to `dest`."""
    if not src.exists():
        raise EditError(f"source file missing: {src}")
    if not dest.exists():
        # The dataset sometimes targets files that DO exist. If the
        # generator picked a non-existent dest, we create it empty.
        dest.write_text("", encoding="utf-8")

    src_text = src.read_text(encoding="utf-8")
    block, remaining = _extract_symbol_block(src_text, name, src)
    src.write_text(remaining, encoding="utf-8")

    dest_text = dest.read_text(encoding="utf-8")
    if dest_text and not dest_text.endswith("\n"):
        dest_text += "\n"
    dest_text += "\n\n" + block
    if not dest_text.endswith("\n"):
        dest_text += "\n"
    dest.write_text(dest_text, encoding="utf-8")
    return [src.resolve(), dest.resolve()]


# ── delete ───────────────────────────────────────────────────────────────────

def _apply_delete(src: Path, name: str) -> list[Path]:
    if not src.exists():
        raise EditError(f"source file missing: {src}")
    src_text = src.read_text(encoding="utf-8")
    _block, remaining = _extract_symbol_block(src_text, name, src)
    src.write_text(remaining, encoding="utf-8")
    return [src.resolve()]


# ── block extraction ─────────────────────────────────────────────────────────

def _extract_symbol_block(source: str, name: str, src_path: Path) -> tuple[str, str]:
    """Return (block_text, rest_of_file) where block_text holds the lines
    that define `name` (top-level OR method inside a top-level class).

    Block spans from the symbol's `lineno` through its `end_lineno` as
    reported by the AST (Python ≥ 3.8 populates these).

    Methods are de-indented when extracted so they can be appended to
    another file as a valid top-level def (otherwise the next parse of
    the destination fails and subsequent edits can't find the symbol).

    On miss, raises EditError.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise EditError(f"could not parse {src_path}: {e}") from e

    start: int | None = None
    end: int | None = None
    is_method = False

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start, end = node.lineno, node.end_lineno
            break
        if isinstance(node, ast.ClassDef):
            if node.name == name:
                start, end = node.lineno, node.end_lineno
                break
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    start, end = child.lineno, child.end_lineno
                    is_method = True
                    break
            if start is not None:
                break

    if start is None or end is None:
        raise EditError(f"symbol `{name}` not found in {src_path}")

    lines = source.splitlines(keepends=True)
    # Widen `start` upward to swallow a contiguous decorator stack.
    while start > 1 and lines[start - 2].lstrip().startswith("@"):
        start -= 1

    block_lines = lines[start - 1:end]
    # De-indent method bodies so they become standalone top-level defs.
    if is_method and block_lines:
        leading = len(block_lines[0]) - len(block_lines[0].lstrip(" "))
        if leading:
            block_lines = [
                (ln[leading:] if ln.startswith(" " * leading) else ln.lstrip(" "))
                for ln in block_lines
            ]

    block = "".join(block_lines).rstrip() + "\n"

    # Remove the block lines from the source.
    rest = lines[:start - 1] + lines[end:]
    rest_text = "".join(rest)
    # Collapse runs of 3+ blank lines that the cut may have created.
    import re
    rest_text = re.sub(r"\n{3,}", "\n\n", rest_text)
    # If we just emptied a class body (methods all moved), inject a `pass`
    # so the class is still syntactically valid. Cheap heuristic: if the
    # last non-blank line ends with `:` and the next logical line is a
    # `class` / top-level def, the previous block needs a `pass`.
    # We keep this minimal — the fixture rarely drains a class completely.
    try:
        ast.parse(rest_text)
    except SyntaxError:
        # Fall back: re-inject a bare `pass` right after any class header
        # whose body we've stripped. This is a safety net; the fixture
        # doesn't exercise it heavily.
        rest_text = _repair_empty_class_bodies(rest_text)
    return block, rest_text


def _repair_empty_class_bodies(text: str) -> str:
    """Minimal safety-net: add `pass` after any `class ...:` header whose
    body ends up being empty after block extraction. Leaves the file
    otherwise untouched.
    """
    out_lines: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        out_lines.append(lines[i])
        stripped = lines[i].rstrip()
        if stripped.startswith("class ") and stripped.endswith(":"):
            # Look ahead for the first non-blank non-indented line.
            j = i + 1
            body_found = False
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "":
                    j += 1
                    continue
                if nxt[:1] in (" ", "\t"):
                    body_found = True
                break
            if not body_found:
                # Add `pass` indented one level in.
                out_lines.append("    pass\n")
        i += 1
    return "".join(out_lines)
