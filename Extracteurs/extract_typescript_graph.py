#!/usr/bin/env python3
"""
Extract call graph from TypeScript/TSX source files using tree-sitter
Captures function calls, imports, exports, string literals, and React components
"""

import sys
from pathlib import Path
from typing import List, Dict, Optional, Set
from dataclasses import dataclass
from tree_sitter import Language, Parser, Node
import tree_sitter_typescript as ts_typescript


@dataclass
class TsUsage:
    """Represents a usage of a function, import, component, or string reference"""
    usage_type: str  # "function_call", "import", "export", "component", "string_literal", "hook_call"
    caller_file: str
    caller_line: int
    caller_function: Optional[str]
    callee_name: str  # Function name, imported symbol, component name, or string value
    callee_module: Optional[str]  # For imports: source module path
    string_context: Optional[str] = None  # Context where string appears


class TypeScriptGraphExtractor:
    """Extracts call graph from TypeScript/TSX files using tree-sitter"""

    def __init__(self):
        # tree-sitter-typescript provides two languages: typescript and tsx
        self.ts_language = Language(ts_typescript.language_typescript())
        self.tsx_language = Language(ts_typescript.language_tsx())
        self.current_file: Optional[Path] = None
        self.source_code: bytes = b""
        self.import_map: Dict[str, str] = {}  # Maps imported symbol names to their resolved module paths

    def extract_from_file(self, ts_file: Path) -> List[TsUsage]:
        """Extract all usages from a TypeScript/TSX file"""
        self.current_file = ts_file.resolve()  # Convert to absolute path
        self.import_map = {}  # Reset import map for each file

        with open(ts_file, 'rb') as f:
            self.source_code = f.read()

        # Use tsx parser for .tsx files, typescript parser for .ts files
        if ts_file.suffix == '.tsx':
            parser = Parser(self.tsx_language)
        else:
            parser = Parser(self.ts_language)

        tree = parser.parse(self.source_code)

        # Extract all usages
        # IMPORTANT: Extract imports first to populate import_map
        usages = []
        usages.extend(self._extract_imports(tree.root_node))
        usages.extend(self._extract_exports(tree.root_node))
        usages.extend(self._extract_function_calls(tree.root_node))
        usages.extend(self._extract_react_components(tree.root_node))
        usages.extend(self._extract_hook_calls(tree.root_node))
        usages.extend(self._extract_string_literals(tree.root_node))

        return usages

    def _get_text(self, node: Node) -> str:
        """Get text content of a node"""
        return self.source_code[node.start_byte:node.end_byte].decode('utf8', errors='ignore')

    def _get_current_function(self, node: Node) -> Optional[str]:
        """Find the function containing this node"""
        current = node
        while current:
            if current.type in ['function_declaration', 'arrow_function', 'function', 'method_definition']:
                # Try to find function name
                for child in current.children:
                    if child.type == 'identifier':
                        return self._get_text(child)
                # For arrow functions assigned to variables
                if current.parent and current.parent.type == 'variable_declarator':
                    for child in current.parent.children:
                        if child.type == 'identifier':
                            return self._get_text(child)
            current = current.parent
        return None

    def _resolve_module_path(self, module_path: str) -> str:
        """Resolve relative module path to absolute normalized path

        Args:
            module_path: Import path like "../services/auction" or "react"

        Returns:
            Absolute normalized path like "C:/project/src/services/auction"
            or package name for node_modules imports
        """
        if not module_path:
            return ""

        # If it's a node_modules import (doesn't start with . or /), keep as-is
        if not module_path.startswith('.') and not module_path.startswith('/'):
            return module_path

        # Resolve relative path from current file's directory
        current_dir = self.current_file.parent

        # Resolve the path
        try:
            resolved_path = (current_dir / module_path).resolve()

            # Normalize: try with .ts, .tsx extensions, or /index.ts
            candidates = [
                resolved_path,
                Path(str(resolved_path) + '.ts'),
                Path(str(resolved_path) + '.tsx'),
                resolved_path / 'index.ts',
                resolved_path / 'index.tsx'
            ]

            # Find the first existing file
            for candidate in candidates:
                if candidate.exists() and candidate.is_file():
                    # Return absolute path WITH extension
                    return str(candidate)

            # If no file found, return resolved path as-is
            return str(resolved_path)

        except Exception:
            # Fallback: return original module path
            return module_path

    def _extract_imports(self, node: Node) -> List[TsUsage]:
        """Extract all import statements"""
        usages = []

        def visit(n: Node):
            if n.type == 'import_statement':
                # Get source module (raw, relative path)
                source_module_raw = None
                for child in n.children:
                    if child.type == 'string':
                        source_module_raw = self._get_text(child).strip('"\'')

                # Resolve relative path to absolute normalized path
                source_module_resolved = self._resolve_module_path(source_module_raw) if source_module_raw else None

                # Build context: keep original relative path for reference
                import_context = f"from:{source_module_raw}" if source_module_raw else None

                # Get imported symbols
                for child in n.children:
                    if child.type == 'import_clause':
                        # Named imports: import { foo, bar } from 'module'
                        for clause_child in child.children:
                            if clause_child.type == 'named_imports':
                                for import_spec in clause_child.children:
                                    if import_spec.type == 'import_specifier':
                                        for spec_child in import_spec.children:
                                            if spec_child.type == 'identifier':
                                                imported_name = self._get_text(spec_child)
                                                # Add to import map for FQN resolution
                                                if source_module_resolved:
                                                    self.import_map[imported_name] = source_module_resolved
                                                usages.append(TsUsage(
                                                    usage_type="import",
                                                    caller_file=str(self.current_file),
                                                    caller_line=n.start_point[0] + 1,
                                                    caller_function=None,
                                                    callee_name=imported_name,
                                                    callee_module=source_module_resolved,
                                                    string_context=import_context
                                                ))
                            # Default import: import Foo from 'module'
                            elif clause_child.type == 'identifier':
                                imported_name = self._get_text(clause_child)
                                # Add to import map for FQN resolution
                                if source_module_resolved:
                                    self.import_map[imported_name] = source_module_resolved
                                usages.append(TsUsage(
                                    usage_type="import",
                                    caller_file=str(self.current_file),
                                    caller_line=n.start_point[0] + 1,
                                    caller_function=None,
                                    callee_name=imported_name,
                                    callee_module=source_module_resolved,
                                    string_context=import_context
                                ))

            for child in n.children:
                visit(child)

        visit(node)
        return usages

    def _extract_exports(self, node: Node) -> List[TsUsage]:
        """Extract all export statements"""
        usages = []

        def visit(n: Node):
            if n.type == 'export_statement':
                # export { foo, bar }
                for child in n.children:
                    if child.type == 'export_clause':
                        for export_spec in child.children:
                            if export_spec.type == 'export_specifier':
                                for spec_child in export_spec.children:
                                    if spec_child.type == 'identifier':
                                        exported_name = self._get_text(spec_child)
                                        usages.append(TsUsage(
                                            usage_type="export",
                                            caller_file=str(self.current_file),
                                            caller_line=n.start_point[0] + 1,
                                            caller_function=None,
                                            callee_name=exported_name,
                                            callee_module=None
                                        ))
            # export function foo() or export const foo
            elif n.type in ['lexical_declaration', 'function_declaration', 'class_declaration']:
                # Check if parent is export_statement
                if n.parent and n.parent.type == 'export_statement':
                    for child in n.children:
                        if child.type == 'variable_declarator':
                            for vc in child.children:
                                if vc.type == 'identifier':
                                    usages.append(TsUsage(
                                        usage_type="export",
                                        caller_file=str(self.current_file),
                                        caller_line=n.start_point[0] + 1,
                                        caller_function=None,
                                        callee_name=self._get_text(vc),
                                        callee_module=None
                                    ))
                        elif child.type == 'identifier':
                            usages.append(TsUsage(
                                usage_type="export",
                                caller_file=str(self.current_file),
                                caller_line=n.start_point[0] + 1,
                                caller_function=None,
                                callee_name=self._get_text(child),
                                callee_module=None
                            ))

            for child in n.children:
                visit(child)

        visit(node)
        return usages

    def _extract_function_calls(self, node: Node) -> List[TsUsage]:
        """Extract all function/method calls"""
        usages = []

        def visit(n: Node):
            if n.type == 'call_expression':
                function_name = None

                for child in n.children:
                    if child.type == 'identifier':
                        function_name = self._get_text(child)
                    elif child.type == 'member_expression':
                        # obj.method() - get the method name
                        for mc in child.children:
                            if mc.type == 'property_identifier':
                                function_name = self._get_text(mc)

                if function_name:
                    caller_function = self._get_current_function(n)
                    # Resolve FQN if function was imported
                    callee_module = self.import_map.get(function_name)
                    usages.append(TsUsage(
                        usage_type="function_call",
                        caller_file=str(self.current_file),
                        caller_line=n.start_point[0] + 1,
                        caller_function=caller_function,
                        callee_name=function_name,
                        callee_module=callee_module
                    ))

            for child in n.children:
                visit(child)

        visit(node)
        return usages

    def _extract_react_components(self, node: Node) -> List[TsUsage]:
        """Extract React component usages (JSX elements)"""
        usages = []

        def visit(n: Node):
            # JSX opening element: <ComponentName>
            if n.type in ['jsx_opening_element', 'jsx_self_closing_element']:
                for child in n.children:
                    if child.type == 'identifier':
                        component_name = self._get_text(child)
                        # Only track components (start with uppercase)
                        if component_name and component_name[0].isupper():
                            caller_function = self._get_current_function(n)
                            # Resolve FQN if component was imported
                            callee_module = self.import_map.get(component_name)
                            usages.append(TsUsage(
                                usage_type="react_component",
                                caller_file=str(self.current_file),
                                caller_line=n.start_point[0] + 1,
                                caller_function=caller_function,
                                callee_name=component_name,
                                callee_module=callee_module
                            ))

            for child in n.children:
                visit(child)

        visit(node)
        return usages

    def _extract_hook_calls(self, node: Node) -> List[TsUsage]:
        """Extract React hook calls (useState, useEffect, custom hooks)"""
        usages = []

        def visit(n: Node):
            if n.type == 'call_expression':
                for child in n.children:
                    if child.type == 'identifier':
                        function_name = self._get_text(child)
                        # React hooks start with 'use'
                        if function_name.startswith('use'):
                            caller_function = self._get_current_function(n)
                            # Resolve FQN if hook was imported
                            callee_module = self.import_map.get(function_name)
                            usages.append(TsUsage(
                                usage_type="hook_call",
                                caller_file=str(self.current_file),
                                caller_line=n.start_point[0] + 1,
                                caller_function=caller_function,
                                callee_name=function_name,
                                callee_module=callee_module
                            ))

            for child in n.children:
                visit(child)

        visit(node)
        return usages

    def _extract_string_literals(self, node: Node) -> List[TsUsage]:
        """Extract string literals that might reference actions, views, models, etc."""
        usages = []

        def get_string_context(n: Node) -> Optional[str]:
            """Determine the context where a string literal appears"""
            current = n.parent
            context_parts = []

            while current and len(context_parts) < 3:
                if current.type == 'call_expression':
                    # Find function name
                    for child in current.children:
                        if child.type == 'identifier':
                            context_parts.insert(0, f"call:{self._get_text(child)}")
                            break
                        elif child.type == 'member_expression':
                            for mc in child.children:
                                if mc.type == 'property_identifier':
                                    context_parts.insert(0, f"call:{self._get_text(mc)}")
                                    break
                    break
                elif current.type == 'assignment_expression':
                    context_parts.insert(0, "assignment")
                elif current.type == 'variable_declarator':
                    context_parts.insert(0, "variable")
                elif current.type == 'property_assignment':
                    # Object property: { key: "value" }
                    for child in current.children:
                        if child.type == 'property_identifier':
                            context_parts.insert(0, f"prop:{self._get_text(child)}")
                            break
                    break

                current = current.parent

            return '.'.join(context_parts) if context_parts else None

        def visit(n: Node):
            if n.type == 'string':
                # Get string value (remove quotes)
                string_text = self._get_text(n)
                if string_text.startswith('"') and string_text.endswith('"'):
                    string_value = string_text[1:-1]
                elif string_text.startswith("'") and string_text.endswith("'"):
                    string_value = string_text[1:-1]
                elif string_text.startswith("`") and string_text.endswith("`"):
                    string_value = string_text[1:-1]
                else:
                    string_value = string_text

                # Filter: only keep interesting strings
                # - At least 2 characters
                # - Not just whitespace
                # - Not too long (likely not a reference)
                # - Contains dots (Java-style class names) or specific patterns
                if (len(string_value) >= 2 and
                    string_value.strip() and
                    len(string_value) <= 200 and
                    ('.' in string_value or
                     string_value.startswith('ws/') or
                     'Controller' in string_value or
                     'action' in string_value.lower() or
                     'view' in string_value.lower())):

                    context = get_string_context(n)
                    caller_function = self._get_current_function(n)
                    usages.append(TsUsage(
                        usage_type="string_literal",
                        caller_file=str(self.current_file),
                        caller_line=n.start_point[0] + 1,
                        caller_function=caller_function,
                        callee_name=string_value,
                        callee_module=None,
                        string_context=context
                    ))

            for child in n.children:
                visit(child)

        visit(node)
        return usages


def main():
    """Test the extractor on a file or directory"""
    if len(sys.argv) < 2:
        print("Usage: python extract_typescript_graph.py <ts-file-or-directory>")
        sys.exit(1)

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"Error: Path not found: {path}")
        sys.exit(1)

    extractor = TypeScriptGraphExtractor()
    all_usages = []

    # Process file(s)
    if path.is_file():
        files = [path]
    else:
        files = list(path.rglob("*.ts")) + list(path.rglob("*.tsx"))

    print(f"Processing {len(files)} TypeScript files...")

    for ts_file in files:
        try:
            usages = extractor.extract_from_file(ts_file)
            all_usages.extend(usages)
        except Exception as e:
            print(f"Error processing {ts_file}: {e}")

    # Display statistics
    print(f"\nExtracted {len(all_usages)} usages:")

    by_type = {}
    for usage in all_usages:
        by_type[usage.usage_type] = by_type.get(usage.usage_type, 0) + 1

    for usage_type, count in sorted(by_type.items()):
        print(f"  {usage_type}: {count}")

    # Show some examples
    print("\nExample usages:")
    for usage in all_usages[:15]:
        print(f"\n{usage.usage_type}: {usage.callee_name}")
        if usage.caller_function:
            print(f"  In: {usage.caller_function}()")
        print(f"  At: {Path(usage.caller_file).name}:{usage.caller_line}")
        if usage.callee_module:
            print(f"  From: {usage.callee_module}")
        if usage.string_context:
            print(f"  Context: {usage.string_context}")


if __name__ == "__main__":
    main()
