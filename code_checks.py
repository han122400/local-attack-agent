"""Deterministic pre-execution checks for model-generated run_python code.

These catch the recurring mistakes observed in real runs BEFORE the browser runs the code, so the
model gets a precise, actionable error instead of a raw traceback (and a wasted browser round-trip):
syntax errors, asyncio.run() in a running loop, importing playwright objects that are already provided,
a wrong async def main() signature, and missing await on page/context/browser calls and the helpers.

High precision over recall: only flag what is almost certainly wrong, so valid code is never blocked.
"""

from __future__ import annotations

import ast
from collections import Counter

# page/context/browser methods that are coroutines (must be awaited). Kept to the common ones.
_PAGE_ASYNC = {
    "goto", "content", "title", "query_selector", "query_selector_all", "click", "fill", "press",
    "text_content", "get_attribute", "inner_text", "inner_html", "is_visible", "is_enabled",
    "wait_for_selector", "wait_for_load_state", "wait_for_timeout", "wait_for_url", "evaluate",
    "screenshot", "type", "hover", "focus", "select_option", "set_input_files", "reload", "go_back",
    "set_content", "add_init_script", "bring_to_front",
}
_PAGE_OBJECTS = {"page", "context", "browser"}
# injected async helpers, called as bare names: await get_json(...), await submit_input(...)
_HELPER_FUNCS = {"get_text", "get_json", "http_get", "goto_and_render", "list_inputs", "submit_input", "replay_request"}


def check_generated_code(code: str) -> list[str]:
    """Return a list of problems (empty if the code passes). Does not execute anything."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Syntax error: {exc.msg} (line {exc.lineno})."]

    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    problems: list[str] = []
    bindings = {target.id: node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    writes = Counter(node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    writes.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    responses, pages = set(), set()
    for name, value in bindings.items():
        if writes[name] != 1 or not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
            continue
        call = value.value.func
        if isinstance(call, ast.Attribute) and ast.unparse(call.value) in {"context.request", "page.request"}:
            if call.attr in {"get", "post", "put", "patch", "delete", "head", "fetch"}:
                responses.add(name)
        if isinstance(call, ast.Name) and call.id == "goto_and_render":
            pages.add(name)
    main_defined = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "main"
                       or isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "main"
                       or isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == "main"
                       for node in ast.walk(tree))
    dynamic_namespace = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id in {"exec", "eval", "globals", "locals"} for node in ast.walk(tree))
    if not main_defined and not dynamic_namespace:
        for stmt in tree.body:
            value = getattr(stmt, "value", None)
            value = value.value if isinstance(value, ast.Await) else value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "main":
                problems.append("main is not defined in this call; define async def main(page, context, browser) or remove its call.")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith) and any(ast.unparse(item.context_expr) in {"context.request", "page.request"}
                                                 for item in node.items):
            problems.append("context.request is already provided and is not an async context manager; await its get/post methods directly.")
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in responses
                and node.attr in {"text", "body", "json"}):
            usage = parents.get(node)
            if isinstance(usage, ast.BoolOp):
                usage = parents.get(usage)
            if isinstance(usage, (ast.Subscript, ast.BinOp)):
                problems.append(f"Response.{node.attr} is a method; use await response.{node.attr}(), not the attribute.")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id in pages and node.func.attr == "get"):
            problems.append("goto_and_render returns a Page, not a dictionary; use page.url or awaited Page methods.")
        # asyncio.run(...) inside the already-running loop
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"):
            problems.append("Do not call asyncio.run(); the event loop is already running — use top-level await.")
        # importing playwright objects that are already provided
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("playwright"):
            problems.append("Do not import from playwright; page, context and browser are already provided.")
        if isinstance(node, ast.Import) and any(a.name.startswith("playwright") for a in node.names):
            problems.append("Do not import playwright; page, context and browser are already provided.")
        # async def main must accept exactly (page, context, browser)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            count = len(node.args.args)
            if count != 3:
                problems.append(f"async def main must take exactly (page, context, browser); yours takes {count}.")
        # def main (not async) cannot be awaited by the runtime
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            problems.append("main must be 'async def main(page, context, browser)', not a plain def.")
        # missing await on page/context/browser methods and on the injected helpers
        if isinstance(node, ast.Call) and not isinstance(parents.get(node), ast.Await):
            func = node.func
            name = None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in _PAGE_OBJECTS and func.attr in _PAGE_ASYNC):
                name = f"{func.value.id}.{func.attr}"
            elif isinstance(func, ast.Name) and func.id in _HELPER_FUNCS:
                name = func.id
            if name is not None:
                problems.append(f"'{name}(...)' is async — add await (e.g. await {name}(...)).")

    seen: set[str] = set()
    ordered: list[str] = []
    for problem in problems:
        if problem not in seen:
            seen.add(problem)
            ordered.append(problem)
    return ordered
