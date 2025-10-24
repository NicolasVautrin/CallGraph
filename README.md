# CallGraph MCP Server

MCP server for analyzing call graphs in Axelor projects.

## What is this?

A Model Context Protocol (MCP) server that lets Claude Code intelligently query your project's call graph:
- Find where methods/classes are used
- Analyze change impact
- Navigate dependencies
- Trace call chains across Java and XML files

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Claude Desktop

Edit your `claude_desktop_config.json`:

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "callgraph": {
      "command": "python",
      "args": ["C:/Users/nicolasv/MCP_servers/CallGraph/mcp_callgraph_server.py"]
    }
  }
}
```

### 3. Extract your project's call graph

```bash
cd /path/to/your/axelor/project
python /path/to/CallGraph/run_extraction.py . --mode full
```

This creates `.vector-raw-db/` in your project directory.

### 4. Restart Claude Desktop

The server auto-detects the database in your current working directory.

## Architecture

```
run_extraction.py           # CLI entry point
    ↓
ExtractionManager          # Orchestrates extraction
    ↓
├── JavaASTExtractor       # Extracts Java call graph (via JavaASTService)
├── TypeScriptASTExtractor # Extracts TypeScript call graph
├── AxelorXmlExtractor     # Extracts XML references
├── AxelorRepoManager      # Downloads Axelor dependencies
└── StorageWriter          # Writes to ChromaDB

Query Path:
mcp_callgraph_server.py    # MCP server
    ↓
StorageReader              # Reads from ChromaDB
    ↓
ChromaDB (.vector-raw-db)  # Vector database
```

## Extraction Modes

### Full Mode (Recommended)

```bash
python run_extraction.py /path/to/project --mode full
```

- Auto-detects Axelor versions from `gradle.properties` / `settings.gradle`
- Downloads `axelor-open-platform` and `axelor-open-suite`
- Caches Axelor extractions (reused across projects)
- Extracts local modules

### Local Mode (Fast)

```bash
python run_extraction.py /path/to/project --mode local
```

- Only extracts `/modules` directory
- Faster for iterative development
- Doesn't update Axelor caches

### Options

```bash
--reset true/false   # Reset database before extraction (default: true for full, false for local)
--limit N            # Limit entries per repo (for testing)
```

## MCP Tools

### find_usages
Find all usages of a symbol with optional recursion.

```python
find_usages(symbol="validateMove", depth=2)
```

### impact_analysis
Analyze the impact of changing a method.

```python
impact_analysis(symbol="computeTotal", depth=3, only_custom=True)
```

### get_definition
Find where a symbol is defined.

```python
get_definition(symbol="MoveValidateService")
```

### find_callers / find_callees
Navigate the call graph.

```python
find_callers(symbol="validateMove")
find_callees(symbol="processMove")
```

### search_by_file
Find all usages in a specific file.

```python
search_by_file(file_path="MoveValidateService.java")
```

### get_stats
Database statistics.

```python
get_stats()  # Global stats
get_stats(module="open-auction-base")  # Module stats
```

### extract
Trigger extraction from Claude Code.

```python
extract(mode="full", reset=True)
```

## File Naming Conventions

The codebase follows **PascalCase** file naming where each file name matches its main class:

```
JavaASTExtractor.py         → class JavaASTExtractor
TypeScriptASTExtractor.py   → class TypeScriptASTExtractor
AxelorXmlExtractor.py       → class AxelorXmlExtractor
AxelorRepoManager.py        → class AxelorRepoManager
ExtractionManager.py        → class ExtractionManager
StorageWriter.py            → class StorageWriter (Write operations)
StorageReader.py            → class StorageReader (Read operations)
```

This follows modern conventions (similar to Java/C#/TypeScript) for better IDE navigation.

## How It Works

### Auto-detection

The MCP server automatically detects the database in your current working directory:

```python
cwd = Path.cwd()  # Claude Code's current project

if (cwd / ".vector-semantic-db").exists():
    db_path = cwd / ".vector-semantic-db"  # Prioritize semantic
elif (cwd / ".vector-raw-db").exists():
    db_path = cwd / ".vector-raw-db"
```

**Example**:
- Open `C:\project-a\` in Claude Code → Uses `C:\project-a\.vector-raw-db\`
- Open `C:\project-b\` in Claude Code → Uses `C:\project-b\.vector-raw-db\`

### Extraction Process

1. **Detect Axelor versions** from `settings.gradle` / `gradle.properties`
2. **Download dependencies** (`axelor-open-platform`, `axelor-open-suite`)
3. **Extract to caches** (reused across projects with same version)
4. **Copy caches** to project database
5. **Extract local modules** (your custom code)

### What Gets Extracted

**Java** (via JavaASTService):
- Method calls
- Constructor calls
- Field access
- Inheritance (extends/implements)
- Annotations
- Method/class declarations

**XML** (via AxelorXmlExtractor):
- Action methods
- Domain class/field references
- View references
- Menu actions

**TypeScript** (via TypeScriptASTExtractor):
- Function calls
- React component usage
- Imports/exports
- Hook calls

## Cache System

Axelor dependencies are cached per version:

```
axelor-repos/
├── axelor-open-platform-7.2.3/
│   └── .vector-raw-db/          # Cached extraction
└── axelor-open-suite-7.2.3/
    └── .vector-raw-db/
```

**Benefits**:
- Extract Axelor once per version
- Share across all projects using same version
- Faster subsequent extractions

## Performance

| Operation | Time | Size |
|-----------|------|------|
| Full extraction (3000 files) | ~5-10 min | ~150 MB |
| Local extraction (500 files) | ~1-2 min | ~50 MB |
| find_usages (depth=0) | 10-50ms | - |
| impact_analysis (depth=3) | 200ms-1s | - |

## Troubleshooting

### No database found

```bash
# Extract first
cd /path/to/project
python /path/to/CallGraph/run_extraction.py . --mode full
```

### JavaASTService not starting

The service starts automatically but requires Java:

```bash
# Check Java installation
java -version

# Manual start (for debugging)
cd Extracteurs/JavaASTService
./gradlew service
```

### Empty results

```python
# Check database stats
get_stats()

# Re-extract with reset
extract(mode="full", reset=True)
```

## Development

### Project Structure

```
CallGraph/
├── mcp_callgraph_server.py    # MCP server entry point
├── run_extraction.py           # CLI entry point
├── Extracteurs/
│   ├── ExtractionManager.py   # Orchestrator (has main())
│   ├── JavaASTExtractor.py    # Java extraction (pure lib)
│   ├── TypeScriptASTExtractor.py
│   ├── AxelorXmlExtractor.py
│   ├── AxelorRepoManager.py
│   ├── StorageWriter.py       # ChromaDB writes
│   ├── StorageReader.py       # ChromaDB reads
│   └── JavaASTService/        # Java parser service
└── axelor-repos/              # Cached Axelor repos
```

### Running Tests

```bash
# Test extraction
python run_extraction.py /path/to/project --mode local --limit 100

# Test specific extractor (now pure libraries - use ExtractionManager)
cd Extracteurs
python ExtractionManager.py --project-root /path/to/project --local
```

### Design Patterns

**CQRS (Command Query Responsibility Segregation)**:
- `StorageWriter`: All write operations (insert, batch, reset)
- `StorageReader`: All read operations (query, filter, format)

**Pure Libraries**:
- Extractors have no `main()` - they're imported by `ExtractionManager`
- Single orchestrator: `ExtractionManager.py`

## Limitations

- One project at a time (based on `cwd`)
- No hot-reload (restart Claude Desktop to switch projects)
- Memory: ~200-500 MB for ChromaDB

## License

MIT

## Resources

- [Extracteurs README](Extracteurs/README.md)
- [JavaASTService README](Extracteurs/JavaASTService/README.md)
- [MCP Protocol](https://modelcontextprotocol.io)
- [ChromaDB](https://www.trychroma.com)
