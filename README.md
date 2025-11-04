# CallGraph MCP Server

MCP server for analyzing call graphs in Axelor projects using ASM bytecode analysis.

## What is this?

A Model Context Protocol (MCP) server that lets Claude Code intelligently query your project's call graph:
- Find where methods/classes are used
- Analyze change impact
- Navigate dependencies
- Trace call chains across Java bytecode

**Key Features**:
- **ASM bytecode analysis** instead of source parsing (100% accurate)
- **SQLite relational database** instead of vector database
- **Gradle dependency discovery** for automatic Axelor package detection
- **Smart caching** with SHA256-based invalidation

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Claude Code

Add to your `.claude/mcp.json` or `claude_desktop_config.json`:

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "sqlite-callgraph": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "C:/path/to/project/.callgraph.db"]
    }
  }
}
```

### 3. Extract your project's call graph

```bash
cd /path/to/your/axelor/project

# Incremental extraction (default, uses cache)
python /path/to/CallGraph/run_asm_extraction.py .

# Full reset (first time or after schema changes)
python /path/to/CallGraph/run_asm_extraction.py . --init

# Limit extraction for testing (requires --init)
python /path/to/CallGraph/run_asm_extraction.py . --init --limit 100
```

This creates `.callgraph.db` in your project directory.

**Modes**:
- **Default (Incremental)**: Smart caching - only re-extracts modified packages
- **--init**: Full reset - drops all tables and rebuilds from scratch

### 4. Restart Claude Desktop

The MCP server provides direct SQLite access to the call graph database.

## Architecture Overview

```
Project (.callgraph.db SQLite database)
    ↓ Extraction via
run_asm_extraction.py
    ├─> GradleDependencyManager   # Auto-discover Axelor deps
    └─> ASMExtractor               # Python client
        ↓ REST API
    ASMAnalysisService (Java)      # Port 8766
        ↓ ASM ClassVisitor
    Bytecode Analysis (.class files)
        ↓ Storage
    SQLite Tables:
    ├─> symbol_index   # FQN → URI → package
    ├─> nodes          # classes, methods
    └─> edges          # calls, inheritance, member_of
```

## Components

### 1. `run_asm_extraction.py`

CLI entry point for the extraction pipeline.

**Usage**:
```bash
# Incremental extraction (default, uses cache)
python run_asm_extraction.py /path/to/project

# Full reset (first time or after schema changes)
python run_asm_extraction.py /path/to/project --init

# Limit extraction for testing (requires --init to avoid partial data)
python run_asm_extraction.py /path/to/project --init --limit 100
```

**Modes**:
- **Incremental (default)**: Uses SHA256 caching - only re-extracts modified packages
- **--init**: Full reset - drops all tables and rebuilds from scratch

**Process (always runs both steps)**:
1. **Package Discovery** via `GradleDependencyManager` (Gradle)
2. **Symbol Indexing** via `ASMExtractor.build_symbol_index()` (FQN → URI)
3. **Call Graph Extraction** via `ASMExtractor.extract()` (Nodes + Edges)

---

### 2. `GradleDependencyManager.py`

Discovers and manages Axelor dependencies via Gradle.

**Features**:
- Query Gradle for runtime JAR dependencies (Axelor only)
- Extract JARs to `axelor-repos/` cache directory
- Provide package metadata (group, artifact, version, jar, sources, classes)

**Cache Structure**:
```
axelor-repos/
├── axelor-core-7.2.6/
│   ├── classes/    # .class files from JAR
│   └── sources/    # .java files from sources JAR
└── axelor-base-8.2.9/
    ├── classes/
    └── sources/
```

---

### 3. `ASMExtractor.py`

Python client for `ASMAnalysisService` with SQLite storage.

**Database Schema**:

```sql
-- Symbol index (FQN → URI → package)
CREATE TABLE symbol_index (
    fqn TEXT PRIMARY KEY,
    uri TEXT NOT NULL,
    package TEXT NOT NULL,
    line INTEGER                    -- Line number (methods only)
);

-- Nodes (classes and methods)
CREATE TABLE nodes (
    fqn TEXT PRIMARY KEY,
    type TEXT NOT NULL,             -- 'class', 'interface', 'enum', 'method'
    package TEXT NOT NULL,
    line INTEGER,
    visibility TEXT,                -- 'public', 'private', 'protected', 'package'
    has_override BOOLEAN,           -- TRUE if @Override annotation present
    is_transactional BOOLEAN        -- TRUE if @Transactional annotation present
);

-- Edges (relationships)
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_fqn TEXT NOT NULL,
    edge_type TEXT NOT NULL,        -- 'call', 'inheritance', 'member_of'
    to_fqn TEXT NOT NULL,
    kind TEXT,                      -- 'invoke', 'extends', 'implements', 'argument', 'return'
    from_package TEXT NOT NULL,
    to_package TEXT NOT NULL,
    from_line INTEGER
);
```

**Constructor**:
```python
ASMExtractor(db_path=".callgraph.db", service_url="http://localhost:8766", init=False)
```
- `init=True`: Full reset - drops and recreates all tables (INIT mode)
- `init=False` (default): Incremental mode - creates tables if they don't exist

**Key Methods**:
- `init_database()`: Full reset - drops and recreates all tables (called automatically when init=True)
- `clean_package_data(package_name)`: Removes all data for a specific package (used automatically in incremental mode)
- `build_symbol_index()`: Builds FQN → URI mapping with automatic cache invalidation
- `extract()`: Extracts call graph from bytecode

**Performance**:
- Symbol indexing: ~6 minutes for 39 packages (107k symbols, 9.4k classes)
- Call graph extraction: ~40 seconds for 9,466 .class files (252 files/sec)
- Total extraction (--init): ~6-7 minutes complete project
- Incremental mode: Only re-extracts modified packages (70%+ speedup on subsequent runs)

**Optimizations**:
- Batch SQL queries (IN clauses): 99.95% reduction in database queries
- Before: ~650k queries → After: ~220 queries
- Insertions by batches of 5000 rows

---

### 4. `ASMAnalysisService` (Java)

REST service for analyzing Java bytecode using ASM.

**Technology**:
- **ASM**: Bytecode manipulation framework
- **Spark Java**: Lightweight HTTP framework
- **Jackson**: JSON serialization

**Endpoints**:

- `GET /health` - Health check
- `POST /index` - Extract symbols (classes AND methods) with nodeType and line
- `POST /analyze` - Extract complete call graph with metadata
- `POST /shutdown` - Gracefully shut down the service

**Extracted Metadata**:
- Class modifiers (public, abstract, final, etc.)
- Method modifiers and visibility
- Annotations: `@Override`, `@Transactional` (Spring, Jakarta, javax)
- Line numbers for methods
- Inheritance relationships
- Method calls with line numbers

**Running the Service**:
```bash
cd Extracteurs/ASMAnalysisService
./gradlew.bat run
# Service starts on port 8766
# Logs written to asm-service.log
```

---

## MCP Tools (SQLite)

Use your configured SQLite MCP server with direct SQL queries.

### Query Examples

**Find methods using class X as argument**:
```sql
SELECT DISTINCT e.to_fqn AS method_fqn, e.from_fqn AS argument_type
FROM edges e
WHERE e.edge_type = 'member_of' AND e.kind = 'argument'
  AND e.from_fqn = 'com.axelor.apps.openauction.db.Lot'
ORDER BY e.to_fqn;
```

**Find all calls to a method**:
```sql
SELECT e.from_fqn AS caller, e.to_fqn AS callee, e.from_line AS line, e.from_package
FROM edges e
WHERE e.edge_type = 'call' AND e.to_fqn LIKE '%setStatus%'
ORDER BY e.from_package;
```

**Count symbols by package**:
```sql
SELECT package, COUNT(*) as symbol_count,
       SUM(CASE WHEN type = 'class' THEN 1 ELSE 0 END) as class_count,
       SUM(CASE WHEN type = 'method' THEN 1 ELSE 0 END) as method_count
FROM nodes
GROUP BY package ORDER BY symbol_count DESC;
```

**Find transactional methods**:
```sql
SELECT fqn, package, line
FROM nodes
WHERE type = 'method' AND is_transactional = 1
ORDER BY package, line;
```

**Find public methods with @Override**:
```sql
SELECT fqn, package, line
FROM nodes
WHERE type = 'method' AND visibility = 'public' AND has_override = 1
ORDER BY package, line;
```

---

## Comparison: JavaParser vs ASM

| Feature | JavaParser (Old) | ASM (New) |
|---------|------------------|-----------|
| **Input** | Source code (.java) | Bytecode (.class) |
| **Requires sources** | Yes | No (works with JARs) |
| **Analysis speed** | Slower (parsing) | Faster (bytecode) |
| **Accuracy** | Source-level | Bytecode-level (100%) |
| **Dependencies** | Manual download | Gradle auto-discovery |
| **Storage** | ChromaDB (~150 MB) | SQLite (~50 MB) |
| **Query speed** | ~50ms | ~10ms |

---

## Benefits of ASM Approach

1. **No source code required**: Works directly with JARs from Maven/Gradle cache
2. **Automatic dependency discovery**: Gradle integration
3. **100% accurate**: Bytecode analysis reflects actual compilation
4. **Faster**: No parsing overhead
5. **Smaller database**: Relational storage vs vector embeddings
6. **Better caching**: SHA256-based invalidation
7. **Simpler queries**: SQL vs vector similarity search

---

## Performance

### Extraction Times (Real Project - 39 Packages)

**Full Extraction (--init)**: ~6-7 minutes total
- STEP 1 (Gradle discovery): ~10 seconds
- STEP 2 (Symbol indexing): ~6 minutes for 107k symbols
- STEP 3 (Call graph extraction): ~40 seconds for 9,466 .class files (252 files/sec)

**Incremental Mode**: Only re-extracts modified packages (70%+ speedup)

### Database Stats

**Real Project Example**:
- **Size**: ~292 MB
- **Symbols**: 107,312 (9,466 classes + 97,846 methods)
- **Edges**: 319,272 (calls, inheritance, member_of)
- **Packages**: 39 Axelor packages
- **Entities**: 2,089 (22% of classes)

**Database Contents**:
- `symbol_index`: 107,312 rows (~60 MB)
- `nodes`: 94,267 rows (~52 MB)
- `edges`: 319,272 rows (~180 MB)

### Query Performance

- Simple queries (COUNT, SELECT WHERE): ~10ms
- Complex joins (call chains): ~50-100ms
- Full-text search (LIKE): ~20-30ms

---

## Troubleshooting

### ASMAnalysisService not starting

**Solutions**:
1. Check Java: `java -version` (need Java 11+)
2. Build service: `cd Extracteurs/ASMAnalysisService && gradlew.bat build`
3. Start manually: `gradlew.bat service:run`

### Cache not detected

**Verification**:
```bash
ls axelor-repos/axelor-core-7.2.6/classes/
# Should contain .class files
```

### FQN resolution fails or stale data

**Solution**:
- Full reset: `python run_asm_extraction.py /path/to/project --init`
- This drops all tables and rebuilds from scratch

### Kill stuck Java processes

```bash
# Windows
wmic process where "name='java.exe' and CommandLine like '%ASMAnalysisService%'" delete
```

---

## Development

### Project Structure

```
CallGraph/
├── run_asm_extraction.py            # CLI entry point
├── mcp_callgraph_server.py          # Legacy MCP server
├── mcp.json                         # MCP configuration
├── ASM_ARCHITECTURE.md              # Detailed architecture
├── Extracteurs/
│   ├── GradleDependencyManager.py   # Gradle integration
│   ├── ASMExtractor.py              # Python client
│   └── ASMAnalysisService/          # Java service
│       └── service/                 # Kotlin/Gradle implementation
└── axelor-repos/                    # Cached Axelor packages
```

### File Naming Conventions

**PascalCase** for Python files (matches main class):
- `ASMExtractor.py` → `class ASMExtractor`
- `GradleDependencyManager.py` → `class GradleDependencyManager`

---

## License

MIT

## Resources

- [ASM_ARCHITECTURE.md](ASM_ARCHITECTURE.md) - Detailed technical architecture
- [ASM Documentation](https://asm.ow2.io/)
- [Spark Java](https://sparkjava.com/)
- [SQLite](https://www.sqlite.org/)
- [MCP Protocol](https://modelcontextprotocol.io)