#!/usr/bin/env python3
"""
MCP Server for Call Graph Analysis
Provides tools for querying the Java/XML call graph database
"""

import sys
import json
import logging
from pathlib import Path
from typing import Any, Sequence

# Add Extracteurs directory to path for imports
extracteurs_dir = Path(__file__).parent / "Extracteurs"
sys.path.insert(0, str(extracteurs_dir))

from mcp.server import Server
from mcp.types import Tool, TextContent
from call_graph_service import CallGraphService
from extraction_manager import ExtractionManager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("callgraph-mcp")

# Initialize MCP server
app = Server("callgraph")

# Global service instance
service: CallGraphService = None


def init_service():
    """Initialize call graph service"""
    global service
    if service is None:
        try:
            # Auto-detect which vector DB exists in current working directory
            cwd = Path.cwd()

            # Priority: semantic > raw (if both exist, use semantic for better search)
            semantic_db = cwd / ".vector-semantic-db"
            raw_db = cwd / ".vector-raw-db"

            if semantic_db.exists():
                db_path = semantic_db
                logger.info("Using semantic database (with embeddings)")
            elif raw_db.exists():
                db_path = raw_db
                logger.info("Using raw database (metadata only)")
            else:
                # Fallback to raw DB path (will be created if extract is called)
                db_path = raw_db
                logger.info("No database found, will use raw DB when created")

            service = CallGraphService(str(db_path))
            logger.info(f"Call graph service initialized with db: {db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize service: {e}")
            raise


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available call graph analysis tools"""
    return [
        Tool(
            name="find_usages",
            description="""Find all usages of a symbol (method, class, field) with pagination, filtering, and recursive exploration.

            WHEN TO USE:
            - Find where a method/class/field is used across the codebase
            - Analyze impact before modifying code
            - Understand dependencies and call chains

            PARAMETERS:
            - symbol: Symbol name to search (e.g., 'accounting', 'Move', 'validateMove')
            - usage_type: Optional filter by type (e.g., 'java_method_call', 'java_declaration')
            - module_filter: Optional filter by module (e.g., 'open-auction-base')
            - exclude_generated: Exclude generated files (default: true)
            - offset: Pagination offset for direct usages (default: 0)
            - limit: Max direct usages to return (default: 20)
            - depth: Recursion depth (0=no recursion, -1=infinite, 1+=exact levels) (default: 0)
            - max_children_per_level: Max children to show per recursion level (default: 10)

            RETURNS:
            - Formatted tree view with usage records, caller chains, and pagination info""",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name to search"},
                    "usage_type": {"type": "string", "description": "Filter by usage type"},
                    "module_filter": {"type": "string", "description": "Filter by module name"},
                    "exclude_generated": {"type": "boolean", "description": "Exclude generated files", "default": True},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                    "depth": {"type": "integer", "description": "Recursion depth (0=none, -1=max, 1+=levels)", "default": 0},
                    "max_children_per_level": {"type": "integer", "description": "Max children per level", "default": 10}
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="get_definition",
            description="""Find where a symbol is declared/defined.

            WHEN TO USE:
            - Navigate to the definition of a method/class/field
            - Find the source file and line number
            - Understand where a symbol is declared

            PARAMETERS:
            - symbol: Symbol name to find (e.g., 'accounting', 'MoveValidateService')

            RETURNS:
            - Array of definitions with file, line, module info
            - May return multiple results for overloaded methods""",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name to find"}
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="find_callers",
            description="""Find who calls a specific method (simplified version of find_usages for method calls only).

            WHEN TO USE:
            - Find all places that call a specific method
            - Analyze method usage across codebase
            - Impact analysis before modifying a method

            PARAMETERS:
            - symbol: Method name to search
            - offset: Pagination offset (default: 0)
            - limit: Max results (default: 20)

            RETURNS:
            - Paginated results with caller information""",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Method name"},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "limit": {"type": "integer", "description": "Max results", "default": 20}
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="find_callees",
            description="""Find what methods a specific symbol calls (reverse lookup).

            WHEN TO USE:
            - See what a method depends on
            - Understand a method's internal behavior
            - Analyze dependencies

            PARAMETERS:
            - symbol: Symbol name that makes the calls
            - offset: Pagination offset (default: 0)
            - limit: Max results (default: 20)

            RETURNS:
            - Paginated results showing called methods""",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name"},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "limit": {"type": "integer", "description": "Max results", "default": 20}
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="impact_analysis",
            description="""Recursive impact analysis: who calls this symbol, and who calls those callers (cascade effect).

            WHEN TO USE:
            - Understand full impact of modifying a method
            - See the "ripple effect" of changes
            - Analyze call chains and dependencies

            PARAMETERS:
            - symbol: Starting symbol for analysis
            - depth: How many levels to recurse (default: 2, max recommended: 5)
            - only_custom: Only show custom modules, exclude Axelor framework (default: false)
            - offset: Pagination offset for root level (default: 0)
            - limit: Max results per level (default: 50)

            RETURNS:
            - Tree structure showing impact levels
            - Caller counts at each level
            - Pagination info for root level""",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Starting symbol"},
                    "depth": {"type": "integer", "description": "Recursion depth", "default": 2},
                    "only_custom": {"type": "boolean", "description": "Only custom modules", "default": False},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "limit": {"type": "integer", "description": "Max results per level", "default": 50}
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="search_by_file",
            description="""Find all usages within a specific file.

            WHEN TO USE:
            - See all symbols used/defined in a file
            - Analyze file dependencies
            - Understand file structure

            PARAMETERS:
            - file_path: File path (can be partial, e.g., 'MoveValidateService.java')
            - offset: Pagination offset (default: 0)
            - limit: Max results (default: 50)

            RETURNS:
            - Paginated results sorted by line number
            - All usages in the specified file""",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path (full or partial)"},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "limit": {"type": "integer", "description": "Max results", "default": 50}
                },
                "required": ["file_path"]
            }
        ),
        Tool(
            name="get_stats",
            description="""Get call graph database statistics.

            WHEN TO USE:
            - Understand database size and coverage
            - See breakdown by usage types
            - Analyze module distribution

            PARAMETERS:
            - module: Optional filter by module (e.g., 'open-auction-base')

            RETURNS:
            - Total count, usage types, sources, modules""",
            inputSchema={
                "type": "object",
                "properties": {
                    "module": {"type": "string", "description": "Optional module filter"}
                }
            }
        ),
        Tool(
            name="extract",
            description="""Extract call graph from project and dependencies.

            WHEN TO USE:
            - Initialize or update the call graph database
            - After adding new code or dependencies
            - When switching to a new project

            PARAMETERS:
            - mode: Extraction mode - "full" (download Axelor + extract all) or "local" (extract local repo only)
            - reset: Whether to reset the database before extraction (default: true for full, false for local)

            RETURNS:
            - Extraction summary with statistics""",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["full", "local"],
                        "description": "Extraction mode (full or local)"
                    },
                    "reset": {
                        "type": "boolean",
                        "description": "Reset database before extraction",
                        "default": True
                    }
                },
                "required": ["mode"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
    """Handle tool calls"""
    init_service()

    try:
        if name == "find_usages":
            symbol = arguments["symbol"]
            depth = arguments.get("depth", 0)

            result = service.find_usages(
                symbol=symbol,
                usage_type=arguments.get("usage_type"),
                module_filter=arguments.get("module_filter"),
                exclude_generated=arguments.get("exclude_generated", True),
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 20),
                depth=depth,
                max_children_per_level=arguments.get("max_children_per_level", 10)
            )

            # Format result as tree
            formatted = service.format_result("find_usages", result, symbol=symbol, depth=depth)
            return [TextContent(type="text", text=formatted)]

        elif name == "get_definition":
            definitions = service.get_definition(arguments["symbol"])
            result = {
                "definitions": definitions,
                "total": len(definitions)
            }

        elif name == "find_callers":
            result = service.find_callers(
                symbol=arguments["symbol"],
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 20)
            )

        elif name == "find_callees":
            result = service.find_callees(
                symbol=arguments["symbol"],
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 20)
            )

        elif name == "impact_analysis":
            result = service.impact_analysis(
                symbol=arguments["symbol"],
                depth=arguments.get("depth", 2),
                only_custom=arguments.get("only_custom", False),
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 50)
            )

        elif name == "search_by_file":
            result = service.search_by_file(
                file_path=arguments["file_path"],
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 50)
            )

        elif name == "get_stats":
            result = service.get_stats(
                module=arguments.get("module")
            )

        elif name == "extract":
            mode = arguments["mode"]
            reset = arguments.get("reset", True if mode == "full" else False)

            # Get project root (current working directory)
            project_root = Path.cwd()

            logger.info(f"Starting extraction: mode={mode}, reset={reset}, project_root={project_root}")

            manager = ExtractionManager(project_root)

            # Capture output in a string
            import io
            from contextlib import redirect_stdout, redirect_stderr

            output = io.StringIO()

            try:
                with redirect_stdout(output), redirect_stderr(output):
                    if mode == "full":
                        manager.extract_full(reset=reset)
                    elif mode == "local":
                        manager.extract_local()
                    else:
                        raise ValueError(f"Invalid mode: {mode}")

                # Get captured output
                extraction_log = output.getvalue()

                # Re-initialize service after extraction
                global service
                service = None
                init_service()

                # Get stats after extraction
                stats = service.get_stats()

                result = {
                    "status": "success",
                    "mode": mode,
                    "reset": reset,
                    "total_entries": stats.get("total_usages", 0),
                    "log": extraction_log
                }

            except Exception as e:
                logger.error(f"Extraction failed: {e}", exc_info=True)
                result = {
                    "status": "error",
                    "mode": mode,
                    "error": str(e),
                    "log": output.getvalue()
                }

        else:
            raise ValueError(f"Unknown tool: {name}")

        # Format result as pretty JSON (for operations without custom formatting)
        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2, ensure_ascii=False)
        )]

    except Exception as e:
        logger.error(f"Error in {name}: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=json.dumps({
                "error": str(e),
                "tool": name
            }, indent=2)
        )]


async def main():
    """Run MCP server"""
    from mcp.server.stdio import stdio_server

    logger.info("Starting Call Graph MCP Server...")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
