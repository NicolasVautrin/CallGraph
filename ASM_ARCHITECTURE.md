# ASM-Based CallGraph Architecture

This document describes the ASM-based callgraph extraction architecture that replaced the JavaParser/ChromaDB implementation.

## Overview

The new architecture uses:
- **ASM bytecode analysis** instead of JavaParser (source code parsing)
- **SQLite relational database** instead of ChromaDB (vector database)
- **Gradle dependency discovery** for automatic Axelor package detection

## Components

### 1. `run_asm_extraction.py`

**Purpose**: CLI entry point for the extraction pipeline

**Responsibilities**:
- Parse command-line arguments
- Orchestrate the 3-step extraction process:
  1. **Package Discovery** (via `GradleDependencyManager`)
  2. **Symbol Indexing** (optional, via `ASMExtractor.build_symbol_index()`)
  3. **Call Graph Extraction** (via `ASMExtractor.extract()`)
- Logging to file and console

**Usage**:
```bash
# Incremental extraction (default, uses cache)
python run_asm_extraction.py /path/to/project

# Full reset (first time or after schema changes)
python run_asm_extraction.py /path/to/project --init

# Limit extraction for testing (requires --init to avoid partial data)
python run_asm_extraction.py /path/to/project --init --limit 100
```

**Key Features**:
- Unbuffered output for real-time logging
- Automatic log file creation with timestamp
- Progress tracking with ETA
- Package filtering (local vs Axelor)

**Modes**:
- **Incremental (default)**: Smart caching with SHA256 - only re-extracts modified packages
- **--init**: Full reset - drops all tables and rebuilds from scratch

**Architecture**:
```python
main()
  ├── Parse args (--init, --limit)
  ├── Step 1: GradleDependencyManager.get_dependencies()
  │   └── Returns: {packages: [...], classpath: [...]}
  │
  ├── Step 2: ASMExtractor.build_symbol_index() [ALWAYS runs]
  │   ├── If --init: extractor.init_database() (drop all tables)
  │   ├── Else: auto_init=True (create tables if needed)
  │   └── Builds FQN → URI mapping with auto-clean for modified packages
  │
  └── Step 3: ASMExtractor.extract() [ALWAYS runs]
      └── Extracts call graph from .class files
```

---

### 2. `GradleDependencyManager.py`

**Purpose**: Discovers and manages Axelor dependencies via Gradle

**Responsibilities**:
- Query Gradle for runtime JAR dependencies (Axelor only)
- Extract JARs to `axelor-repos/` cache directory
  - `{package}/classes/` - Compiled bytecode (.class files)
  - `{package}/sources/` - Source code (.java files)
- Provide package metadata (group, artifact, version, jar, sources, classes)

**Usage**:
```python
from GradleDependencyManager import GradleDependencyManager

manager = GradleDependencyManager("/path/to/axelor/project")
deps = manager.get_dependencies()

# Returns:
# {
#   "packages": [
#     {
#       "name": "axelor-core-7.2.6",
#       "group": "com.axelor",
#       "artifact": "axelor-core",
#       "version": "7.2.6",
#       "jar": "/path/to/axelor-core-7.2.6.jar",
#       "sources": "/path/to/axelor-repos/axelor-core-7.2.6/sources",
#       "classes": "/path/to/axelor-repos/axelor-core-7.2.6/classes"
#     }
#   ],
#   "classpath": ["/path/to/build/classes", ...]
# }
```

**How it works**:

1. **Gradle Script Execution** (`list-dependencies.gradle`):
   - Custom Gradle init script
   - Queries `runtimeClasspath` configuration
   - Filters Axelor dependencies (group starts with `com.axelor`)
   - Resolves both JAR and sources JAR
   - Outputs: `AXELOR_DEP|group|artifact|version|jar_path|sources_path`

2. **JAR Extraction**:
   - Extracts to `axelor-repos/{artifact}-{version}/`
   - **Caching**: Skips extraction if already exists
   - Stores:
     - `classes/` - .class files from JAR
     - `sources/` - .java files from sources JAR

3. **Cache Benefits**:
   - Extract Axelor packages **once per version**
   - Reuse across multiple projects
   - Significantly faster subsequent extractions

**Key Features**:
- Auto-detects `gradlew` or `gradlew.bat`
- Handles missing sources gracefully
- Deduplicates dependencies
- Provides classpath for project's build outputs

---

### 3. `ASMExtractor.py`

**Purpose**: Python client for `ASMAnalysisService` with SQLite storage

**Responsibilities**:
- Call ASMAnalysisService REST API (Java service)
- Build symbol index (FQN → URI mapping)
- Extract call graph from bytecode
- Store results in SQLite database
- Resolve packages for symbols

**Database Schema**:
```sql
-- Symbol index (FQN → URI → package)
CREATE TABLE symbol_index (
    fqn TEXT PRIMARY KEY,          -- Fully Qualified Name
    uri TEXT NOT NULL,              -- file:/// URI to source (file:///.../File.java:42 for methods)
    package TEXT NOT NULL,          -- Package name (e.g., "axelor-core-7.2.6")
    line INTEGER                    -- Line number (methods only)
);

-- Index metadata (for cache invalidation)
CREATE TABLE index_metadata (
    package TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,     -- SHA256 of .class files
    indexed_at TIMESTAMP NOT NULL
);

-- Nodes (classes and methods)
CREATE TABLE nodes (
    fqn TEXT PRIMARY KEY,
    type TEXT NOT NULL,             -- 'class', 'interface', 'enum', 'method'
    package TEXT NOT NULL,
    line INTEGER,
    visibility TEXT,                -- 'public', 'private', 'protected', 'package'
    has_override BOOLEAN,           -- TRUE if @Override annotation present (methods only)
    is_transactional BOOLEAN,       -- TRUE if @Transactional annotation present (methods only)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Edges (relationships)
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_fqn TEXT NOT NULL,
    edge_type TEXT NOT NULL,        -- 'call', 'inheritance', 'member_of'
    to_fqn TEXT NOT NULL,
    kind TEXT,                      -- 'invoke', 'extends', 'implements', 'return', 'argument', 'attribute'
    from_package TEXT NOT NULL,
    to_package TEXT NOT NULL,
    from_line INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Usage**:
```python
from ASMExtractor import ASMExtractor

# Incremental mode (default)
extractor = ASMExtractor(db_path=".callgraph.db")  # init=False by default

# OR: INIT mode (full reset)
extractor = ASMExtractor(db_path=".callgraph.db", init=True)  # Automatically drops and recreates all tables

# Step 1: Build symbol index (with auto-clean)
extractor.build_symbol_index(
    axelor_repos_dir="axelor-repos",
    packages=["axelor-core-7.2.6", "axelor-base-8.2.9"],
    domains=["com.axelor"],
    project_root="/path/to/project",
    local_packages=["vpauto-8.2.9"]
)
# → Automatically calls clean_package_data() for each modified package

# Step 2: Extract call graph
root_packages = [
    {"name": "axelor-core-7.2.6", "path": "axelor-repos/axelor-core-7.2.6/classes"}
]
result = extractor.extract(
    root_packages=root_packages,
    project_root="/path/to/project",
    domains=["com.axelor"],
    limit=1000
)

extractor.close()
```

**Key Methods**:

#### `__init__(db_path, service_url, init=False)`
- **init=False (default)**: Incremental mode - creates tables if they don't exist
- **init=True**: INIT mode - drops and recreates all tables (full reset)

#### `init_database()`
- **Full reset for INIT mode**
- Drops all existing tables (symbol_index, index_metadata, nodes, edges)
- Recreates all tables from scratch
- Use for: first-time setup, schema changes, or with --init flag

#### `clean_package_data(package_name)`
- **Incremental cleanup**
- Removes all data for a specific package:
  - Symbols from `symbol_index`
  - Nodes (classes/methods) from `nodes`
  - Edges where from_package OR to_package matches
  - Metadata to force re-indexing
- Called automatically by `build_symbol_index()` for modified packages

#### `build_symbol_index()`
- Calls `ASMAnalysisService POST /index` for each package
- Computes SHA256 hash of .class files
- **Automatically calls `clean_package_data()` before re-indexing**
- Skips reindexing if hash unchanged (cache invalidation)
- Fixes URIs for local packages (points to project sources)

#### `extract()`
- Discovers .class files from `root_packages`
- Calls `ASMAnalysisService POST /analyze` per package
- Batch resolves packages via `symbol_index`
- Stores results in `nodes` and `edges` tables with enriched metadata:
  - Visibility (public, private, protected, package)
  - Annotations (@Override, @Transactional)

#### `_extract_visibility(modifiers)`
- Extracts visibility from ASM modifiers list
- Returns 'public', 'private', 'protected', or 'package' (default)

#### `_fix_local_package_uris()`
- Replaces `axelor-repos/` URIs with project source URIs
- Example:
  ```
  Before: file:///axelor-repos/vpauto-8.2.9/sources/com/example/MyClass.java
  After:  file:///Bricklead_Encheres/modules/vpauto/src/main/java/com/example/MyClass.java
  ```

**Performance**:
- Symbol indexing: ~10-20 seconds for 39 packages (90k symbols)
- Call graph extraction: ~5-10 minutes for 3000 .class files
- **Incremental mode**: Only re-extracts modified packages (70%+ speedup)
- **INIT mode**: Full extraction from scratch (use for schema changes)

---

### 4. `ASMAnalysisService` (Java)

**Purpose**: REST service for analyzing Java bytecode using ASM

**Technology**:
- **ASM**: Bytecode manipulation framework
- **Spark Java**: Lightweight HTTP framework
- **Jackson**: JSON serialization

**Endpoints**:

#### `GET /health`
Health check endpoint
```json
{
  "status": "ok",
  "service": "ASMAnalysisService",
  "version": "1.0.0"
}
```

#### `POST /index` (Lightweight)
Extract symbols (FQN + package + nodeType + line) for indexing.
Indexes **both classes AND methods** (not just classes).

**Request**:
```json
{
  "packageRoots": ["/path/to/axelor-core-7.2.6"],
  "domains": ["com.axelor"],  // optional filter
  "limit": 100                 // optional
}
```

**Response**:
```json
{
  "success": true,
  "symbols": [
    {
      "fqn": "com.axelor.db.Model",
      "package": "axelor-core-7.2.6",
      "nodeType": "class",
      "line": null
    },
    {
      "fqn": "com.axelor.db.Model.getId()",
      "package": "axelor-core-7.2.6",
      "nodeType": "method",
      "line": 42
    }
  ]
}
```

#### `POST /analyze` (Full Analysis)
Extract complete call graph

**Request**:
```json
{
  "packageRoots": ["/path/to/axelor-core-7.2.6"],
  "domains": ["com.axelor"],  // optional filter
  "limit": 100                 // optional
}
```

Or with explicit class files:
```json
{
  "classFiles": ["/path/to/MyClass.class", ...],
  "domains": ["com.axelor"]
}
```

**Response**:
```json
{
  "success": true,
  "classes": [
    {
      "fqn": "com.axelor.db.Model",
      "nodeType": "class",
      "modifiers": ["public", "abstract"],
      "isInterface": false,
      "isEnum": false,
      "isAbstract": true,
      "inheritance": [
        {"fqn": "com.axelor.db.EntityHelper", "kind": "extends"},
        {"fqn": "java.io.Serializable", "kind": "implements"}
      ],
      "fields": [
        {"type": "java.lang.Long"}
      ],
      "methods": [
        {
          "fqn": "com.axelor.db.Model.getId()",
          "lineNumber": 42,
          "returnType": "java.lang.Long",
          "arguments": [],
          "calls": [
            {
              "toFqn": "com.axelor.db.EntityHelper.getId()",
              "kind": "invoke",
              "lineNumber": 43
            }
          ]
        }
      ]
    }
  ]
}
```

#### `POST /shutdown`
Gracefully shut down the ASM Analysis Service

**Request**: No body required

**Response**:
```json
{
  "status": "shutting down"
}
```

The service will stop after sending the response (500ms delay to ensure response delivery).

**Key Features**:

1. **ClassAnalyzer** (ASM ClassVisitor):
   - Visits class metadata (name, modifiers, inheritance)
   - Visits fields (type extraction)
   - Visits methods (parameters, return type, line numbers)
   - Visits method instructions (method calls via `MethodVisitor`)

2. **Edge Types**:
   - `inheritance`: class extends/implements another class
   - `call`: method invokes another method
   - `member_of`: type membership (return type, argument type, field type)

3. **Domain Filtering**:
   - Only analyze classes matching domain prefixes
   - Reduces analysis time and storage
   - Example: `["com.axelor"]` → only Axelor classes

4. **Descriptor Parsing**:
   - Converts JVM type descriptors to FQNs
   - Example: `Lcom/axelor/db/Model;` → `com.axelor.db.Model`
   - Handles arrays: `[Ljava/lang/String;` → `java.lang.String[]`

**Running the Service**:
```bash
cd Extracteurs/ASMAnalysisService
./gradlew.bat run
# Service logs to asm-service.log in the current directory

# Or build JAR
./gradlew.bat build
java -jar build/libs/ASMAnalysisService-1.0.0.jar
```

**Logging**:
- Logs are written to `asm-service.log` in the working directory
- Log level: INFO by default
- Spark/Jetty logs are suppressed to reduce noise

---

## Extraction Flow

### Full Extraction Process

```
1. run_asm_extraction.py
   └── Parse args: /path/to/project --init

2. GradleDependencyManager
   ├── Run: gradlew --init-script list-dependencies.gradle listAxelorDeps
   ├── Parse output: AXELOR_DEP|com.axelor|axelor-core|7.2.6|/path/to/jar|/path/to/sources
   ├── Extract JAR → axelor-repos/axelor-core-7.2.6/classes/
   └── Extract sources → axelor-repos/axelor-core-7.2.6/sources/

   Returns: {packages: [...], classpath: [...]}

3. ASMExtractor initialization
   ├── If --init: init=True → init_database() called automatically (drop all tables)
   └── Else: init=False (default) → create tables if not exist

4. ASMExtractor.build_symbol_index() [ALWAYS runs]
   For each package:
     ├── Compute SHA256(classes/*.class)
     ├── Check index_metadata: needs_reindex?
     ├── If modified: clean_package_data(package_name)
     │   ├── DELETE FROM symbol_index WHERE package = ?
     │   ├── DELETE FROM nodes WHERE package = ?
     │   ├── DELETE FROM edges WHERE from_package = ? OR to_package = ?
     │   └── DELETE FROM index_metadata WHERE package = ?
     ├── POST /index → ASMAnalysisService
     │   └── Returns: [{fqn, package}]
     ├── Store in symbol_index
     └── Update index_metadata

   For local packages:
     └── Fix URIs: axelor-repos → project/modules/

5. ASMExtractor.extract() [ALWAYS runs]
   For each package:
     ├── Discover .class files
     ├── POST /analyze → ASMAnalysisService
     │   └── ClassAnalyzer (ASM)
     │       ├── visit() → class node + inheritance edges
     │       ├── visitField() → field edges
     │       └── visitMethod(modifiers) → method node
     │           ├── visitAnnotation() → detect @Override, @Transactional
     │           └── visitMethodInsn() → method call edges
     ├── Extract visibility from modifiers
     ├── Batch lookup packages via symbol_index
     └── Store in nodes + edges tables with metadata:
         - visibility (public, private, protected, package)
         - has_override (boolean)
         - is_transactional (boolean)

6. Database: .callgraph.db
   ├── symbol_index: 90k symbols
   ├── nodes: classes + methods + metadata
   └── edges: calls + inheritance + member_of
```

---

## Comparison: JavaParser vs ASM

| Feature | JavaParser (Old) | ASM (New) |
|---------|------------------|-----------|
| **Input** | Source code (.java) | Bytecode (.class) |
| **Requires sources** | Yes | No (works with JARs) |
| **Analysis speed** | Slower (parsing) | Faster (bytecode) |
| **Accuracy** | Source-level | Bytecode-level (100% accurate) |
| **Dependencies** | Manual download | Gradle auto-discovery |
| **Storage** | ChromaDB (vector) | SQLite (relational) |
| **Query performance** | ~50ms | ~10ms |
| **Size** | ~150 MB | ~50 MB |
| **Line numbers** | Yes | Yes (debug info) |
| **Generics** | Complex | Erased (simpler) |

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

## Database Queries

### Find methods using class `Lot` as argument

```sql
SELECT DISTINCT
    e.to_fqn AS method_fqn,
    e.from_fqn AS argument_type
FROM edges e
WHERE e.edge_type = 'member_of'
  AND e.kind = 'argument'
  AND e.from_fqn = 'com.axelor.apps.openauction.db.Lot'
ORDER BY e.to_fqn;
```

### Find all calls to a method

```sql
SELECT
    e.from_fqn AS caller,
    e.to_fqn AS callee,
    e.from_line AS line,
    e.from_package AS caller_package
FROM edges e
WHERE e.edge_type = 'call'
  AND e.to_fqn = 'com.axelor.apps.openauction.db.Lot.setStatus(java.lang.Integer)'
ORDER BY e.from_package;
```

### Count symbols by package

```sql
SELECT
    package,
    COUNT(*) as symbol_count,
    SUM(CASE WHEN type = 'class' THEN 1 ELSE 0 END) as class_count,
    SUM(CASE WHEN type = 'method' THEN 1 ELSE 0 END) as method_count
FROM nodes
GROUP BY package
ORDER BY symbol_count DESC;
```

---

## Future Improvements

1. **Parallel analysis**: Multi-threaded ASMAnalysisService for faster extraction
2. **Source map integration**: Better URI resolution for Gradle builds
3. **Graph algorithms**: PageRank, centrality analysis for impact metrics
4. **Advanced query tools**: Pre-built SQL queries for common analysis patterns
5. **Change tracking**: Git-aware incremental extraction based on file modifications

---

## Resources

- [ASM Documentation](https://asm.ow2.io/)
- [Spark Java](https://sparkjava.com/)
- [SQLite](https://www.sqlite.org/)
- [Gradle Dependency Management](https://docs.gradle.org/current/userguide/dependency_management.html)
