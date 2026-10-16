#!/usr/bin/env python3
"""Simple AST-based code graph builder (fallback when codegraph CLI unavailable).

Builds a structural index of the v3/ codebase:
- Every function/class definition with file, line, docstring
- Import relationships between modules
- Function call relationships (caller -> callee)

Output: .codegraph/codegraph_simple.json
"""
import ast
import json
import os
import sys
from pathlib import Path
from collections import defaultdict


def parse_file(filepath):
    """Parse a Python file and extract symbols."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError) as e:
        return None

    symbols = []
    imports = []
    calls = defaultdict(set)  # caller -> set of callees

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.current_func = None
            self.current_class = None

        def visit_Import(self, node):
            for alias in node.names:
                imports.append({
                    'type': 'import',
                    'module': alias.name,
                    'asname': alias.asname,
                    'line': node.lineno,
                })
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            for alias in node.names:
                imports.append({
                    'type': 'from_import',
                    'module': node.module or '',
                    'name': alias.name,
                    'asname': alias.asname,
                    'line': node.lineno,
                })
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            doc = ast.get_docstring(node) or ""
            symbols.append({
                'type': 'function',
                'name': node.name,
                'line': node.lineno,
                'end_line': node.end_lineno,
                'args': [a.arg for a in node.args.args],
                'docstring': doc[:200],
                'in_class': self.current_class,
            })
            old_func = self.current_func
            self.current_func = node.name
            self.generic_visit(node)
            self.current_func = old_func

        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node):
            doc = ast.get_docstring(node) or ""
            symbols.append({
                'type': 'class',
                'name': node.name,
                'line': node.lineno,
                'end_line': node.end_lineno,
                'bases': [ast.unparse(b) for b in node.bases] if hasattr(ast, 'unparse') else [],
                'docstring': doc[:200],
            })
            old_class = self.current_class
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = old_class

        def visit_Call(self, node):
            # Record caller -> callee
            if self.current_func:
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name:
                    caller = f"{self.current_class}.{self.current_func}" if self.current_class else self.current_func
                    calls[caller].add(func_name)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)

    return {
        'symbols': symbols,
        'imports': imports,
        'calls': {k: sorted(v) for k, v in calls.items()},
    }


def build_index(project_root):
    """Build index for all Python files in project."""
    project_root = Path(project_root)
    index = {
        'project': str(project_root),
        'files': {},
        'all_symbols': [],  # flat list for quick lookup
    }

    py_files = sorted(project_root.rglob("*.py"))
    # Exclude venv, __pycache__, .git
    py_files = [f for f in py_files
                if '.venv' not in str(f)
                and '__pycache__' not in str(f)
                and '.git' not in str(f)]

    for filepath in py_files:
        rel = str(filepath.relative_to(project_root))
        result = parse_file(filepath)
        if result:
            index['files'][rel] = result
            for sym in result['symbols']:
                index['all_symbols'].append({
                    'file': rel,
                    **sym,
                })

    return index


def main():
    project_root = sys.argv[1] if len(sys.argv) > 1 else "."
    project_root = Path(project_root).resolve()

    print(f"Building code graph for {project_root}...", file=sys.stderr)
    index = build_index(project_root)

    out_dir = project_root / ".codegraph"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "codegraph_simple.json"

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    n_files = len(index['files'])
    n_symbols = len(index['all_symbols'])
    n_funcs = sum(1 for s in index['all_symbols'] if s['type'] == 'function')
    n_classes = sum(1 for s in index['all_symbols'] if s['type'] == 'class')

    print(f"Done: {n_files} files, {n_symbols} symbols "
          f"({n_funcs} functions, {n_classes} classes)", file=sys.stderr)
    print(f"Output: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
