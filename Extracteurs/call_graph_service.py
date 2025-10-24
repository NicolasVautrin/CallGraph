#!/usr/bin/env python3
"""
Call Graph Service - Business logic for querying call graph database
Provides pagination, filtering, and impact analysis capabilities
"""

import logging
from pathlib import Path
from typing import List, Dict, Optional, Any, Set
import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)


class CallGraphService:
    """Service for querying call graph database with advanced features"""

    def __init__(self, db_path: str = ".vector-db"):
        """Initialize service

        Args:
            db_path: Path to ChromaDB storage (default: .vector-db)
        """
        self.db_path = Path(db_path)
        self.client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(anonymized_telemetry=False)
        )

        # Auto-detect collection name (try new name first, fallback to legacy)
        collections = self.client.list_collections()
        collection_names = [c.name for c in collections]

        if "call_graph" in collection_names:
            self.collection = self.client.get_collection("call_graph")
        elif "java_usages" in collection_names:
            self.collection = self.client.get_collection("java_usages")
            logger.info("Using collection 'java_usages'")
        else:
            raise RuntimeError(
                f"No call graph collection found in {self.db_path}\n"
                f"Available collections: {collection_names}\n"
                "Run: python scripts/build_call_graph_db.py --reset --java-only --no-embeddings"
            )

    def find_usages(
        self,
        symbol: str,
        usage_type: Optional[str] = None,
        module_filter: Optional[str] = None,
        exclude_generated: bool = True,
        offset: int = 0,
        limit: int = 20,
        depth: int = 0,
        max_children_per_level: int = 10,
        max_depth: int = 50,
        _visited: Optional[Set[str]] = None,
        _current_depth: int = 0
    ) -> Dict[str, Any]:
        """Find all usages of a symbol with pagination, filtering, and recursive exploration

        Args:
            symbol: Symbol name to search (method, class, field)
            usage_type: Filter by usage type (e.g., "java_method_call")
            module_filter: Filter by module name (e.g., "open-auction-base")
            exclude_generated: Exclude generated files (src-gen, build/)
            offset: Pagination offset (applies to direct usages only)
            limit: Maximum results to return (applies to direct usages only)
            depth: Recursion depth (0=no recursion, -1=max, 1+=exact levels)
            max_children_per_level: Max children per recursion level (default: 10)
            max_depth: Safety limit for infinite recursion (default: 50)
            _visited: Internal set to track visited symbols (prevents cycles)
            _current_depth: Internal depth counter

        Returns:
            Dict with results (including nested children if depth>0), total, offset, limit, pagination info
        """
        # Initialize visited set on first call
        if _visited is None:
            _visited = set()

        # Safety check: prevent infinite recursion
        if _current_depth >= max_depth:
            return {
                'results': [],
                'total': 0,
                'offset': 0,
                'limit': limit,
                'has_more': False,
                'next_offset': None,
                'error': f'Max depth {max_depth} reached'
            }

        # Check if we should stop recursion
        should_recurse = False
        if depth == -1:  # Infinite mode
            should_recurse = _current_depth < max_depth
        elif depth > 0:  # Exact depth mode
            should_recurse = _current_depth < depth

        # Build where clause
        where_conditions = [{"calleeSymbol": symbol}]

        if usage_type:
            where_conditions.append({"usageType": usage_type})

        if module_filter:
            where_conditions.append({"module": module_filter})

        # Combine conditions with AND
        if len(where_conditions) == 1:
            where_filter = where_conditions[0]
        else:
            where_filter = {"$and": where_conditions}

        # Query ChromaDB (get more than needed to handle filtering)
        fetch_limit = limit * 3 if exclude_generated else limit + offset
        results = self.collection.get(
            where=where_filter,
            limit=min(fetch_limit, 10000),  # ChromaDB max
            include=['metadatas']
        )

        # Filter generated files and external framework files if requested
        filtered_results = []
        for metadata in results['metadatas']:
            if exclude_generated:
                caller_uri = metadata.get('callerUri', '')
                # Exclude build/src-gen directories
                if any(x in caller_uri for x in ['/build/', '/src-gen/', '\\build\\', '\\src-gen\\']):
                    continue
                # Exclude Axelor framework files
                if 'axelor-open-platform' in caller_uri:
                    continue

            filtered_results.append(metadata)

        # Apply pagination
        total = len(filtered_results)
        paginated = filtered_results[offset:offset + limit]

        # Recursively find usages of each caller if depth > 0
        if should_recurse:
            for metadata in paginated:
                caller_symbol = metadata.get('callerSymbol')

                # Skip if already visited (prevent cycles)
                if caller_symbol and caller_symbol not in _visited:
                    _visited.add(caller_symbol)

                    # Recursively find who calls this caller
                    child_usages = self.find_usages(
                        symbol=caller_symbol,
                        usage_type=usage_type,
                        module_filter=module_filter,
                        exclude_generated=exclude_generated,
                        offset=0,
                        limit=max_children_per_level,
                        depth=depth,
                        max_children_per_level=max_children_per_level,
                        max_depth=max_depth,
                        _visited=_visited,
                        _current_depth=_current_depth + 1
                    )

                    # Attach children to parent
                    metadata['_children'] = child_usages['results']
                    metadata['_children_total'] = child_usages['total']
                    metadata['_children_displayed'] = len(child_usages['results'])
                    metadata['_children_truncated'] = child_usages['total'] > len(child_usages['results'])

        return {
            'results': paginated,
            'total': total,
            'offset': offset,
            'limit': limit,
            'has_more': offset + limit < total,
            'next_offset': offset + limit if offset + limit < total else None
        }

    def raw_usages(
        self,
        symbol: str,
        usage_type: Optional[str] = None,
        module_filter: Optional[str] = None,
        exclude_generated: bool = True,
        offset: int = 0,
        limit: int = 20
    ) -> Dict[str, Any]:
        """Find all usages of a symbol - RAW version without formatting or recursion

        Args:
            symbol: Symbol name to search (method, class, field)
            usage_type: Filter by usage type (e.g., "java_method_call")
            module_filter: Filter by module name (e.g., "open-auction-base")
            exclude_generated: Exclude generated files (src-gen, build/)
            offset: Pagination offset
            limit: Maximum results to return

        Returns:
            Dict with raw results (no recursion, no formatting), total, offset, limit, pagination info
        """
        # Build where clause
        where_conditions = [{"calleeSymbol": symbol}]

        if usage_type:
            where_conditions.append({"usageType": usage_type})

        if module_filter:
            where_conditions.append({"module": module_filter})

        # Combine conditions with AND
        if len(where_conditions) == 1:
            where_filter = where_conditions[0]
        else:
            where_filter = {"$and": where_conditions}

        # Query ChromaDB (get more than needed to handle filtering)
        fetch_limit = limit * 3 if exclude_generated else limit + offset
        results = self.collection.get(
            where=where_filter,
            limit=min(fetch_limit, 10000),  # ChromaDB max
            include=['metadatas']
        )

        # Filter generated files if requested
        filtered_results = []
        for metadata in results['metadatas']:
            if exclude_generated:
                caller_uri = metadata.get('callerUri', '')
                # Exclude build/src-gen directories
                if any(x in caller_uri for x in ['/build/', '/src-gen/', '\\build\\', '\\src-gen\\']):
                    continue
                # Exclude Axelor framework files
                if 'axelor-open-platform' in caller_uri:
                    continue

            filtered_results.append(metadata)

        # Apply pagination
        total = len(filtered_results)
        paginated = filtered_results[offset:offset + limit]

        return {
            'results': paginated,
            'total': total,
            'offset': offset,
            'limit': limit,
            'has_more': offset + limit < total,
            'next_offset': offset + limit if offset + limit < total else None
        }

    def get_definition(self, symbol: str) -> List[Dict[str, Any]]:
        """Find where a symbol is declared/defined

        Args:
            symbol: Symbol name to find

        Returns:
            List of definitions with file, line, module info
        """
        results = self.collection.get(
            where={
                "$and": [
                    {"calleeSymbol": symbol},
                    {"usageType": "java_declaration"}
                ]
            },
            limit=100,
            include=['metadatas']
        )

        return results['metadatas']

    def find_callers(
        self,
        symbol: str,
        offset: int = 0,
        limit: int = 20
    ) -> Dict[str, Any]:
        """Find who calls a symbol (method calls only)

        Args:
            symbol: Symbol name
            offset: Pagination offset
            limit: Maximum results

        Returns:
            Paginated results
        """
        return self.find_usages(
            symbol=symbol,
            usage_type="java_method_call",
            offset=offset,
            limit=limit
        )

    def find_callees(
        self,
        symbol: str,
        offset: int = 0,
        limit: int = 20
    ) -> Dict[str, Any]:
        """Find what a symbol calls (reverse lookup: find methods called BY this symbol)

        Args:
            symbol: Caller symbol name
            offset: Pagination offset
            limit: Maximum results

        Returns:
            Paginated results
        """
        # Query by callerSymbol instead of calleeSymbol
        results = self.collection.get(
            where={
                "$and": [
                    {"callerSymbol": symbol},
                    {"usageType": "java_method_call"}
                ]
            },
            limit=limit + offset,
            include=['metadatas']
        )

        total = len(results['metadatas'])
        paginated = results['metadatas'][offset:offset + limit]

        return {
            'results': paginated,
            'total': total,
            'offset': offset,
            'limit': limit,
            'has_more': offset + limit < total,
            'next_offset': offset + limit if offset + limit < total else None
        }

    def impact_analysis(
        self,
        symbol: str,
        depth: int = 2,
        only_custom: bool = False,
        offset: int = 0,
        limit: int = 50
    ) -> Dict[str, Any]:
        """Recursive impact analysis: who calls this symbol, and who calls those callers

        Args:
            symbol: Starting symbol
            depth: How many levels to recurse
            only_custom: Only include custom modules (exclude axelor-open-*)
            offset: Pagination offset (for root level only)
            limit: Maximum results per level

        Returns:
            Tree structure with impact levels
        """
        visited = set()

        def analyze_level(current_symbol: str, current_depth: int) -> Dict[str, Any]:
            """Recursively analyze impact"""
            if current_depth > depth or current_symbol in visited:
                return None

            visited.add(current_symbol)

            # Find callers of current symbol
            module_filter = "open-auction-" if only_custom else None
            results = self.find_usages(
                symbol=current_symbol,
                module_filter=module_filter,
                usage_type="java_method_call",
                offset=0,
                limit=limit
            )

            node = {
                'symbol': current_symbol,
                'depth': current_depth,
                'direct_callers': len(results['results']),
                'total_usages': results['total'],
                'callers': []
            }

            # Recurse for each caller
            if current_depth < depth:
                for caller_metadata in results['results'][:10]:  # Limit recursion width
                    caller_symbol = caller_metadata.get('callerSymbol')
                    if caller_symbol and caller_symbol != current_symbol:
                        child = analyze_level(caller_symbol, current_depth + 1)
                        if child:
                            node['callers'].append(child)

            return node

        impact_tree = analyze_level(symbol, 0)

        # Apply pagination to root level callers only
        if impact_tree and impact_tree['callers']:
            total_root_callers = len(impact_tree['callers'])
            impact_tree['callers'] = impact_tree['callers'][offset:offset + limit]
            impact_tree['pagination'] = {
                'total': total_root_callers,
                'offset': offset,
                'limit': limit,
                'has_more': offset + limit < total_root_callers,
                'next_offset': offset + limit if offset + limit < total_root_callers else None
            }

        return impact_tree

    def search_by_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 50
    ) -> Dict[str, Any]:
        """Find all usages in a specific file

        Args:
            file_path: File path (can be partial, e.g., "MoveValidateService.java")
            offset: Pagination offset
            limit: Maximum results

        Returns:
            Paginated results
        """
        # Query all entries and filter by file path (ChromaDB doesn't support LIKE)
        # We'll need to fetch and filter client-side
        all_results = self.collection.get(
            limit=10000,
            include=['metadatas']
        )

        # Filter by file path
        matching = []
        for metadata in all_results['metadatas']:
            caller_uri = metadata.get('callerUri', '')
            if file_path in caller_uri:
                matching.append(metadata)

        # Sort by line number if available
        matching.sort(key=lambda m: m.get('callerLine', 0))

        # Paginate
        total = len(matching)
        paginated = matching[offset:offset + limit]

        return {
            'results': paginated,
            'total': total,
            'offset': offset,
            'limit': limit,
            'has_more': offset + limit < total,
            'next_offset': offset + limit if offset + limit < total else None
        }

    def get_stats(self, module: Optional[str] = None) -> Dict[str, Any]:
        """Get database statistics

        Args:
            module: Optional module filter

        Returns:
            Statistics dict
        """
        # Get total count
        if module:
            results = self.collection.get(
                where={"module": module},
                limit=10000,
                include=['metadatas']
            )
            count = len(results['metadatas'])
            sample = results['metadatas']
        else:
            count = self.collection.count()
            sample = self.collection.get(
                limit=min(1000, count),
                include=['metadatas']
            )['metadatas']

        # Analyze sample
        usage_types = {}
        sources = {}
        modules = {}

        for metadata in sample:
            usage_type = metadata.get('usageType', 'unknown')
            usage_types[usage_type] = usage_types.get(usage_type, 0) + 1

            source = metadata.get('source', 'java')
            sources[source] = sources.get(source, 0) + 1

            mod = metadata.get('module', '')
            if mod:
                modules[mod] = modules.get(mod, 0) + 1

        return {
            'total': count,
            'usage_types': usage_types,
            'sources': sources,
            'modules': modules
        }

    # ==================== FORMATTING METHODS ====================

    def _extract_filename(self, uri: str) -> str:
        """Extract filename from URI

        Args:
            uri: File URI (e.g., file:///C:/.../MyFile.java:123)

        Returns:
            Filename with extension (e.g., "MyFile.java")
        """
        if not uri:
            return "unknown"

        # Remove line number suffix if present
        uri_without_line = uri.split(':')[0] if ':' in uri.rsplit('/', 1)[-1] else uri

        # Extract filename
        return Path(uri_without_line).name

    def _format_file_location(self, metadata: Dict[str, Any]) -> str:
        """Format file location as 'Filename.java:123 [module]'

        Args:
            metadata: Usage metadata

        Returns:
            Formatted location string
        """
        filename = self._extract_filename(metadata.get('callerUri', ''))
        line = metadata.get('callerLine', '?')
        module = metadata.get('module', '')

        location = f"{filename}:{line}"
        if module:
            location += f" [{module}]"

        return location

    def _format_uri(self, uri: str) -> str:
        """Format URI with icon

        Args:
            uri: Full file URI

        Returns:
            Formatted URI line
        """
        return f"🔗 {uri}"

    def _format_usage_node(
        self,
        metadata: Dict[str, Any],
        index: int,
        total: int,
        current_depth: int,
        indent: str = "",
        is_last: bool = False
    ) -> List[str]:
        """Format a usage node recursively

        Args:
            metadata: Usage metadata
            index: Index of this node (1-based)
            total: Total number of nodes at this level
            current_depth: Current depth level
            indent: Current indentation string
            is_last: Whether this is the last node at this level

        Returns:
            List of formatted lines
        """
        lines = []

        # Node prefix
        if current_depth == 0:
            prefix = f"├─ [{index}/{total}] "
        else:
            prefix = "├─ " if not is_last else "└─ "

        # Symbol info
        symbol = metadata.get('callerSymbol', 'unknown')
        kind = metadata.get('callerKind', '')
        kind_str = f" [{kind}]" if kind else ""

        lines.append(f"{indent}{prefix}{symbol}{kind_str}")

        # File location
        location = self._format_file_location(metadata)
        location_indent = indent + ("│  " if not is_last else "   ")
        lines.append(f"{location_indent}📍 {location}")

        # URI
        uri = metadata.get('callerUri', '')
        lines.append(f"{location_indent}{self._format_uri(uri)}")

        # Children (recursive)
        children = metadata.get('_children', [])
        children_total = metadata.get('_children_total', 0)
        children_displayed = metadata.get('_children_displayed', 0)
        children_truncated = metadata.get('_children_truncated', False)

        if children:
            # Add separator before children
            child_indent = location_indent + "│"
            lines.append(f"{child_indent}")

            # Header for children
            depth_label = f"[depth {current_depth + 1}]"
            if children_truncated:
                lines.append(f"{child_indent}└─ Appelé par ({children_total} total, {children_displayed} affichés) {depth_label}")
            else:
                lines.append(f"{child_indent}└─ Appelé par ({children_total} total, {children_displayed} affichés) {depth_label}")

            # Format each child
            child_lines_indent = location_indent + "   "
            for i, child in enumerate(children):
                is_last_child = (i == len(children) - 1) and not children_truncated
                child_lines = self._format_usage_node(
                    child,
                    index=i + 1,
                    total=children_total,
                    current_depth=current_depth + 1,
                    indent=child_lines_indent,
                    is_last=is_last_child
                )
                lines.extend(child_lines)

            # Truncation warning
            if children_truncated:
                remaining = children_total - children_displayed
                lines.append(f"{child_lines_indent}└─ ⚠️  +{remaining} autres usages non affichés")

        return lines

    def format_find_usages(
        self,
        result: Dict[str, Any],
        symbol: str,
        depth: int = 0
    ) -> str:
        """Format find_usages result as tree

        Args:
            result: Result dict from find_usages
            symbol: Symbol being searched
            depth: Depth used in the search

        Returns:
            Formatted string
        """
        lines = []

        # Header
        depth_info = f" (depth: {depth})" if depth > 0 else ""
        lines.append(f"📞 find_usages: {symbol}{depth_info}")
        lines.append("")

        # Separate declarations and usages
        results = result.get('results', [])
        declarations = [r for r in results if r.get('usageType') == 'java_declaration']
        usages = [r for r in results if r.get('usageType') != 'java_declaration']

        # Format declarations
        if declarations:
            lines.append("DECLARATIONS")
            for decl in declarations:
                filename = self._extract_filename(decl.get('calleeUri', ''))
                line = decl.get('calleeLine', '?')
                module = decl.get('module', '')
                uri = decl.get('calleeUri', '')

                location = f"{filename}:{line}"
                if module:
                    location += f" [{module}]"

                kind = decl.get('calleeKind', 'method')
                lines.append(f"└─ {symbol} [{kind}]")
                lines.append(f"   📍 {location}")
                lines.append(f"   {self._format_uri(uri)}")

            lines.append("")

        # Format usages
        if usages:
            total = result.get('total', len(usages))
            offset = result.get('offset', 0)
            limit = result.get('limit', len(usages))
            displayed = len(usages)

            # Don't subtract declarations from total for the header
            # because total already reflects filtered results
            lines.append(f"USAGES DIRECTS ({total} total, affichage {offset + 1}-{offset + displayed})")
            lines.append("")

            for i, usage in enumerate(usages):
                is_last = i == len(usages) - 1
                node_lines = self._format_usage_node(
                    usage,
                    index=i + 1 + offset,
                    total=total,
                    current_depth=0,
                    indent="",
                    is_last=is_last
                )
                lines.extend(node_lines)

                # Add spacing between top-level nodes
                if not is_last:
                    lines.append("")

        # Pagination info
        if result.get('has_more'):
            lines.append("")
            offset = result.get('offset', 0)
            limit = result.get('limit', 20)
            total = result.get('total', 0)
            next_offset = result.get('next_offset')
            lines.append(f"[Pagination] Affichage {offset + 1}-{offset + len(usages)} sur {total} usages directs")
            if next_offset is not None:
                lines.append(f"[Navigation] Utilisez offset={next_offset} pour voir les usages suivants")

        return "\n".join(lines)

    def format_result(
        self,
        operation: str,
        result: Any,
        **kwargs
    ) -> str:
        """Format result based on operation type

        Args:
            operation: Operation name (find_usages, get_definition, etc.)
            result: Result to format
            **kwargs: Additional formatting parameters

        Returns:
            Formatted string
        """
        if operation == "find_usages":
            return self.format_find_usages(result, kwargs.get('symbol', 'unknown'), kwargs.get('depth', 0))

        # Default: return JSON for other operations (to be implemented)
        import json
        return json.dumps(result, indent=2, ensure_ascii=False)
