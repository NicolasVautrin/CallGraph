"""
ASMExtractor - Client for ASMAnalysisService

This module provides a Python client for extracting call graphs from Java bytecode
using the ASMAnalysisService. It handles:
1. Building symbol index from Axelor packages
2. Analyzing project bytecode
3. Resolving URIs via SQLite symbol_index
4. Storing results in .callgraph.db

Usage:
    extractor = ASMExtractor(
        db_path=".callgraph.db",
        service_url="http://localhost:8766"
    )

    # Build index once
    extractor.build_symbol_index(axelor_repos_dir="axelor-repos")

    # Extract project
    extractor.extract_project(project_root="/path/to/project")
"""

import requests
import sqlite3
import json
import hashlib
from pathlib import Path
from typing import List, Dict, Optional
import time
from datetime import datetime


class ASMExtractor:
    """Client for ASMAnalysisService with SQLite symbol resolution"""

    def __init__(self, db_path: str = ".callgraph.db", service_url: str = "http://localhost:8766"):
        """
        Initialize ASM extractor

        Args:
            db_path: Path to SQLite database
            service_url: URL of ASMAnalysisService
        """
        self.db_path = db_path
        self.service_url = service_url
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_extraction_tables()

    def _ensure_extraction_tables(self):
        """Create nodes and edges tables if they don't exist"""
        cursor = self.conn.cursor()

        # Nodes table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                fqn TEXT PRIMARY KEY NOT NULL,
                type TEXT NOT NULL,
                package TEXT NOT NULL,
                line INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Edges table with from_package and to_package
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_fqn TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                to_fqn TEXT NOT NULL,
                kind TEXT,
                from_package TEXT NOT NULL,
                to_package TEXT NOT NULL,
                from_line INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_nodes_package ON nodes(package)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_fqn)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_fqn)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_from_package ON edges(from_package)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_to_package ON edges(to_package)')

        self.conn.commit()

    def _ensure_symbol_index_table(self):
        """Create symbol_index and index_metadata tables if they don't exist"""
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS symbol_index (
                fqn TEXT PRIMARY KEY,
                uri TEXT NOT NULL,
                package TEXT NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_symbol_package ON symbol_index(package)')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS index_metadata (
                package TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                indexed_at TIMESTAMP NOT NULL
            )
        ''')
        self.conn.commit()

    def build_symbol_index(self, axelor_repos_dir: str, packages: List[str] = None, domains: List[str] = None, project_root: str = None, local_packages: List[str] = None):
        """
        Build symbol index from Axelor packages

        Args:
            axelor_repos_dir: Path to axelor-repos directory
            packages: Optional list of package names to index (if None, scans all packages in axelor_repos_dir)
            domains: Optional list of domain filters (e.g., ["com.axelor"])
            project_root: Optional path to project root (for fixing local package URIs)
            local_packages: Optional list of local package names to fix URIs for
        """
        print(f"[ASM] Building symbol index from {axelor_repos_dir}")
        if packages:
            print(f"[ASM] Packages to index: {len(packages)}")
        if domains:
            print(f"[ASM] Domain filter: {', '.join(domains)}")
        if local_packages:
            print(f"[ASM] Local packages (will fix URIs): {', '.join(local_packages)}")
        self._ensure_symbol_index_table()

        axelor_repos = Path(axelor_repos_dir)
        if not axelor_repos.exists():
            raise FileNotFoundError(f"axelor-repos not found: {axelor_repos}")

        # Get package directories
        if packages:
            # Use provided package list
            package_dirs = [axelor_repos / pkg_name for pkg_name in packages if (axelor_repos / pkg_name).is_dir()]
            print(f"[ASM] Found {len(package_dirs)} packages")
        else:
            # Scan all packages
            package_dirs = [d for d in axelor_repos.iterdir() if d.is_dir()]
            print(f"[ASM] Found {len(package_dirs)} packages")

        total_symbols = 0
        skipped = 0

        for package_dir in package_dirs:
            package_name = package_dir.name

            # Check if package needs reindexing
            if not self._needs_reindex(package_name, package_dir):
                skipped += 1
                print(f"[ASM] Skipping {package_name} (unchanged)")
                continue

            print(f"[ASM] Indexing {package_name}...")

            # Analyze package (just to extract symbols)
            package_path = str(package_dir.resolve()).replace('\\', '/')
            symbols = self._index_package(package_path, package_name, domains)

            if symbols:
                total_symbols += len(symbols)
                # Compute and store hash
                content_hash = self._compute_package_hash(package_dir / "classes")
                self._store_symbols(symbols, package_name, content_hash)
                print(f"[ASM]   -> {len(symbols)} symbols indexed")

        # Fix URIs for local packages
        if project_root and local_packages:
            self._fix_local_package_uris(project_root, local_packages)

        print(f"[ASM] Symbol index complete: {total_symbols} total symbols ({skipped} packages skipped)")

    def _index_package(self, package_path: str, package_name: str, domains: List[str] = None) -> List[Dict]:
        """
        Index a single package (extract FQN → URI mapping)

        Args:
            package_path: Full path to package directory
            package_name: Package name with version (e.g., "axelor-core-7.2.6")
            domains: Optional list of domain filters (e.g., ["com.axelor"])

        Returns:
            List of {fqn, uri} dictionaries
        """
        try:
            # Call ASMAnalysisService /index endpoint (lightweight)
            payload = {"packageRoots": [package_path]}
            if domains:
                payload["domains"] = domains

            response = requests.post(
                f"{self.service_url}/index",
                json=payload,
                timeout=300
            )
            response.raise_for_status()
            result = response.json()

            if not result.get('success'):
                print(f"[ASM]   Warning: indexing failed for {package_name}")
                return []

            # Extract symbols (already in correct format)
            symbols = []
            for symbol in result.get('symbols', []):
                symbols.append({
                    'fqn': symbol['fqn'],
                    'uri': symbol['uri']
                })

            return symbols

        except Exception as e:
            print(f"[ASM]   Error indexing {package_name}: {e}")
            return []

    def _compute_package_hash(self, classes_dir: Path) -> str:
        """
        Compute SHA256 hash of all .class files in package

        Args:
            classes_dir: Path to classes directory

        Returns:
            SHA256 hex digest
        """
        if not classes_dir.exists():
            return "no-classes"

        hasher = hashlib.sha256()

        # Sort files for deterministic hash
        class_files = sorted(classes_dir.rglob("*.class"))

        for class_file in class_files:
            # Hash filename and content
            hasher.update(class_file.name.encode('utf-8'))
            hasher.update(class_file.read_bytes())

        return hasher.hexdigest()

    def _needs_reindex(self, package_name: str, package_dir: Path) -> bool:
        """
        Check if package needs reindexing based on content hash

        Args:
            package_name: Package name with version
            package_dir: Path to package directory

        Returns:
            True if package needs reindexing
        """
        classes_dir = package_dir / "classes"
        if not classes_dir.exists():
            return False  # No classes to index

        # Compute current hash
        current_hash = self._compute_package_hash(classes_dir)

        # Get stored hash
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT content_hash FROM index_metadata WHERE package = ?",
            (package_name,)
        )
        row = cursor.fetchone()

        if not row:
            return True  # Not indexed yet

        stored_hash = row['content_hash']
        return current_hash != stored_hash  # Reindex if hash changed

    def _store_symbols(self, symbols: List[Dict], package_name: str, content_hash: str):
        """
        Store symbols in symbol_index table and update metadata

        Args:
            symbols: List of {fqn, uri}
            package_name: Package name with version
            content_hash: SHA256 hash of package content
        """
        cursor = self.conn.cursor()

        # Delete old symbols for this package
        cursor.execute("DELETE FROM symbol_index WHERE package = ?", (package_name,))

        # Insert new symbols
        for symbol in symbols:
            cursor.execute(
                "INSERT OR REPLACE INTO symbol_index (fqn, uri, package) VALUES (?, ?, ?)",
                (symbol['fqn'], symbol['uri'], package_name)
            )

        # Update metadata
        cursor.execute(
            "INSERT OR REPLACE INTO index_metadata (package, content_hash, indexed_at) VALUES (?, ?, ?)",
            (package_name, content_hash, datetime.now().isoformat())
        )

        self.conn.commit()

    def _fix_local_package_uris(self, project_root: str, local_packages: List[str]):
        """
        Fix URIs for local packages to point to project sources instead of axelor-repos

        Args:
            project_root: Path to project root (e.g., C:/Users/nicolasv/Bricklead_Encheres)
            local_packages: List of local package names (e.g., ["vpauto-8.2.9", "open-auction-base-8.2.9"])
        """
        import re

        print(f"[ASM] Fixing URIs for {len(local_packages)} local packages...")

        project_path = Path(project_root)
        modules_dir = project_path / "modules"

        if not modules_dir.exists():
            print(f"[ASM] Warning: modules directory not found at {modules_dir}")
            return

        cursor = self.conn.cursor()

        for package_name in local_packages:
            # Extract module name from package (e.g., "vpauto-8.2.9" -> "vpauto")
            module_name = re.match(r'^(.+?)-[\d.]+', package_name)
            if not module_name:
                print(f"[ASM] Warning: Could not extract module name from {package_name}")
                continue

            module_name = module_name.group(1)
            module_path = modules_dir / module_name

            if not module_path.exists():
                print(f"[ASM] Warning: Module directory not found: {module_path}")
                continue

            # Get all symbols for this package
            cursor.execute("SELECT fqn, uri FROM symbol_index WHERE package = ?", (package_name,))
            symbols = cursor.fetchall()

            if not symbols:
                continue

            print(f"[ASM] Fixing {len(symbols)} URIs for {package_name} -> modules/{module_name}")

            updated = 0
            for symbol in symbols:
                fqn = symbol['fqn']
                old_uri = symbol['uri']

                # Build new URI pointing to project sources
                new_uri = self._build_local_uri(fqn, module_path)

                if new_uri and new_uri != old_uri:
                    cursor.execute(
                        "UPDATE symbol_index SET uri = ? WHERE fqn = ? AND package = ?",
                        (new_uri, fqn, package_name)
                    )
                    updated += 1

            print(f"[ASM]   -> Updated {updated} URIs")

        self.conn.commit()

    def _build_local_uri(self, fqn: str, module_path: Path) -> Optional[str]:
        """
        Build URI for a local project symbol

        Args:
            fqn: Fully qualified name (e.g., "com.example.MyClass" or "com.example.MyClass.method()")
            module_path: Path to module directory

        Returns:
            file:// URI or None
        """
        # Extract class FQN (remove method part if present)
        class_fqn = fqn.split('(')[0]  # Remove method signature
        if '.' in class_fqn:
            # Check if last part looks like a method name (starts with lowercase)
            parts = class_fqn.split('.')
            if parts[-1] and parts[-1][0].islower():
                # It's a method, remove it
                class_fqn = '.'.join(parts[:-1])

        # Convert FQN to file path
        relative_path = class_fqn.replace('.', '/') + '.java'

        # Check if it's a Model entity (.db. in FQN)
        is_model = '.db.' in class_fqn

        # Try different source locations
        possible_paths = []

        if is_model:
            # Model entities are in project root's build/src-gen/java or module's build/src-gen/java
            # Try project root first
            project_root = module_path.parent.parent
            root_src_gen = project_root / 'build' / 'src-gen' / 'java' / relative_path
            if root_src_gen.exists():
                return root_src_gen.resolve().as_uri()

            # Try module as fallback
            module_src_gen = module_path / 'build' / 'src-gen' / 'java' / relative_path
            if module_src_gen.exists():
                return module_src_gen.resolve().as_uri()

        # Regular classes in module sources
        src_main = module_path / 'src' / 'main' / 'java' / relative_path
        if src_main.exists():
            return src_main.resolve().as_uri()

        return None

    def _discover_class_files(self, root_packages: List[Dict], limit: int = None):
        """
        Generator that discovers .class files from root packages

        Args:
            root_packages: List of dicts with 'name' and 'path' keys
            limit: Optional limit of total files across all packages

        Yields:
            Absolute path to .class file
        """
        count = 0
        for pkg in root_packages:
            package_path = Path(pkg['path'])

            if not package_path.exists():
                continue

            # Find all .class files
            for class_file in package_path.rglob('*.class'):
                yield str(class_file.resolve())
                count += 1

                if limit and count >= limit:
                    return

    def extract(self, root_packages: List[Dict], project_root: str, domains: List[str] = None, limit: int = None) -> Dict:
        """
        Extract call graph from multiple packages

        Args:
            root_packages: List of dicts with 'name' and 'path' keys
                          e.g., [{'name': 'axelor-core-7.2.6', 'path': '/path/to/classes'}]
            project_root: Path to project root (for URI resolution)
            domains: Optional list of domain filters (e.g., ["com.axelor"])
            limit: Optional limit of total files across all packages

        Returns:
            Extraction statistics
        """
        print(f"[ASM] Extracting call graph from {len(root_packages)} packages")
        print(f"[ASM] Project root: {project_root}")
        if domains:
            print(f"[ASM] Domain filter: {', '.join(domains)}")
        if limit:
            print(f"[ASM] Limit: {limit} files total")

        # Discover .class files
        class_files = list(self._discover_class_files(root_packages, limit))
        print(f"[ASM] Found {len(class_files)} class files to analyze")

        if not class_files:
            return {'success': True, 'stats': {'total_classes': 0, 'total_methods': 0, 'total_calls': 0}}

        total_classes = 0
        total_methods = 0
        total_calls = 0

        # Progress tracking
        import time
        start_time = time.time()
        files_processed = 0
        total_files = len(class_files)

        # Group by package for progress reporting
        pkg_files = {}
        for pkg in root_packages:
            pkg_path = pkg['path'].replace('/', '\\')  # Normalize to Windows backslashes
            pkg_files[pkg['name']] = [f for f in class_files if f.startswith(pkg_path)]

        for pkg_name, files in pkg_files.items():
            if not files:
                continue

            print(f"[ASM] Extracting {pkg_name} ({len(files)} files)...")

            try:
                # Call ASMAnalysisService with file list
                payload = {
                    "classFiles": files
                }

                # Add domains filter if specified
                if domains:
                    payload["domains"] = domains

                response = requests.post(
                    f"{self.service_url}/analyze",
                    json=payload,
                    timeout=600
                )

                result = response.json()
                response.raise_for_status()

                if not result.get('success'):
                    print(f"[ASM]   -> Analysis failed for {pkg_name}")
                    continue

                # Store results in database
                classes = result.get('classes', [])
                self._store_extraction_results(pkg_name, classes)

                pkg_classes = len(classes)
                pkg_methods = sum(len(c.get('methods', [])) for c in classes)
                pkg_calls = sum(sum(len(m.get('calls', [])) for m in c.get('methods', [])) for c in classes)

                total_classes += pkg_classes
                total_methods += pkg_methods
                total_calls += pkg_calls

                print(f"[ASM]   -> {pkg_classes} classes, {pkg_methods} methods, {pkg_calls} calls")

                # Update progress
                files_processed += len(files)

                # Log progress every 50 files
                if files_processed % 50 < len(files) or files_processed >= total_files:
                    elapsed = time.time() - start_time
                    if files_processed > 0:
                        rate = files_processed / elapsed
                        remaining = total_files - files_processed
                        eta_seconds = remaining / rate if rate > 0 else 0

                        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                        eta_str = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"

                        print(f"[ASM] Progress: {files_processed}/{total_files} files | "
                              f"Elapsed: {elapsed_str} | ETA: {eta_str} | "
                              f"Rate: {rate:.1f} files/s")

            except Exception as e:
                print(f"[ASM]   -> ERROR: {e}")
                files_processed += len(files)  # Count failed files too

        return {
            'success': True,
            'stats': {
                'total_packages': len(root_packages),
                'total_classes': total_classes,
                'total_methods': total_methods,
                'total_calls': total_calls
            }
        }

    def _store_extraction_results(self, package_name: str, classes: List[Dict]):
        """Store extraction results in database (without URIs - resolved at query time)"""
        cursor = self.conn.cursor()

        # Batch collections
        nodes_batch = []
        edges_batch = []

        # Collect all FQNs (including current package classes/methods) to lookup packages in one query
        target_fqns = set()
        for class_data in classes:
            # Collect class and method FQNs from current package
            target_fqns.add(class_data['fqn'])
            for method in class_data.get('methods', []):
                target_fqns.add(method['fqn'])

            # Collect inheritance targets
            for inheritance in class_data.get('inheritance', []):
                parent_fqn = inheritance.get('fqn') if isinstance(inheritance, dict) else inheritance
                target_fqns.add(parent_fqn)

            for method in class_data.get('methods', []):
                # Collect return type
                if method.get('returnType'):
                    target_fqns.add(method['returnType'])

                # Collect arg types (arguments is a list of FQNs)
                for arg_fqn in method.get('arguments', []):
                    if arg_fqn:
                        target_fqns.add(arg_fqn)

                # Collect call targets
                for call in method.get('calls', []):
                    target_fqns.add(call['toFqn'])

            # Collect field types
            for field in class_data.get('fields', []):
                if field.get('type'):
                    target_fqns.add(field['type'])

        # Lookup all target packages in one query
        fqn_to_package = {}
        if target_fqns:
            placeholders = ','.join('?' * len(target_fqns))
            cursor.execute(
                f"SELECT fqn, package FROM symbol_index WHERE fqn IN ({placeholders})",
                list(target_fqns)
            )
            fqn_to_package = {row['fqn']: row['package'] for row in cursor.fetchall()}

        # Process all classes
        for class_data in classes:
            class_fqn = class_data['fqn']
            class_package = fqn_to_package.get(class_fqn)

            # Add class node (no line for classes)
            nodes_batch.append((class_fqn, 'class', class_package, None))

            # Process inheritances as edges (edge_type='inheritance', kind='extends'/'implements')
            for inheritance in class_data.get('inheritance', []):
                parent_fqn = inheritance.get('fqn') if isinstance(inheritance, dict) else inheritance
                inherit_kind = inheritance.get('kind', 'extends') if isinstance(inheritance, dict) else 'extends'
                inherit_kind = inherit_kind.lower()  # Normalize to lowercase
                parent_package = fqn_to_package.get(parent_fqn)
                if parent_package:
                    edges_batch.append((class_fqn, 'inheritance', parent_fqn, inherit_kind, class_package, parent_package, None))

            # Process class fields as edges (edge_type='member_of', kind='attribute')
            for field in class_data.get('fields', []):
                field_type = field.get('type')
                field_package = fqn_to_package.get(field_type)
                if field_type and field_package:
                    edges_batch.append((field_type, 'member_of', class_fqn, 'attribute', field_package, class_package, None))

            # Process methods
            for method in class_data.get('methods', []):
                method_fqn = method['fqn']
                method_package = fqn_to_package.get(method_fqn)

                # Add method node with line number
                method_line = method.get('lineNumber')
                nodes_batch.append((method_fqn, 'method', method_package, method_line))

                # Add member_of edge (method belongs to class) - edge_type='member_of', kind='method'
                edges_batch.append((method_fqn, 'member_of', class_fqn, 'method', method_package, class_package, None))

                # Process return type - edge_type='member_of', kind='return'
                return_type = method.get('returnType')
                return_package = fqn_to_package.get(return_type)
                if return_type and return_package:
                    edges_batch.append((return_type, 'member_of', method_fqn, 'return', return_package, method_package, None))

                # Process args - edge_type='member_of', kind='argument'
                for arg_fqn in method.get('arguments', []):
                    arg_package = fqn_to_package.get(arg_fqn)
                    if arg_fqn and arg_package:
                        edges_batch.append((arg_fqn, 'member_of', method_fqn, 'argument', arg_package, method_package, None))

                # Process calls - edge_type='call', kind=invoke type
                for call in method.get('calls', []):
                    target_fqn = call['toFqn']
                    call_kind = call.get('kind', 'invoke').lower()
                    call_line = call.get('lineNumber')
                    target_package = fqn_to_package.get(target_fqn)
                    if target_package:
                        edges_batch.append((method_fqn, 'call', target_fqn, call_kind, method_package, target_package, call_line))

        # Batch insert nodes
        if nodes_batch:
            cursor.executemany(
                "INSERT OR IGNORE INTO nodes (fqn, type, package, line) VALUES (?, ?, ?, ?)",
                nodes_batch
            )

        # Batch insert edges
        if edges_batch:
            cursor.executemany(
                "INSERT OR IGNORE INTO edges (from_fqn, edge_type, to_fqn, kind, from_package, to_package, from_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
                edges_batch
            )

        self.conn.commit()

    def extract_project(self, project_root: str, project_package: str, allowed_packages: List[str] = None, modules_base_path: str = None) -> Dict:
        """
        Extract call graph from project

        Args:
            project_root: Path to project root (contains build/classes)
            project_package: Package name for the project itself (e.g., "open-auction-base-1.0.0")
            allowed_packages: List of package names to use for package resolution (e.g., ["axelor-core-7.2.6"])
                            If None, uses all packages in symbol_index
            modules_base_path: Path to modules directory (e.g., "/path/to/Bricklead_Encheres/modules")
                              If None, will try to auto-detect from project_root

        Returns:
            Analysis result with packages resolved
        """
        print(f"[ASM] Extracting project: {project_root}")
        print(f"[ASM] Project package: {project_package}")

        project_path = str(Path(project_root).resolve()).replace('\\', '/')

        # Call ASMAnalysisService
        response = requests.post(
            f"{self.service_url}/analyze",
            json={"packageRoots": [project_path]},
            timeout=600
        )
        response.raise_for_status()
        result = response.json()

        if not result.get('success'):
            raise Exception("Analysis failed")

        # Step 1: Collect all unique FQNs from the result
        all_fqns = set()

        for class_data in result.get('classes', []):
            # Class itself
            all_fqns.add(class_data['fqn'])

            # Inheritance
            for inh in class_data.get('inheritance', []):
                all_fqns.add(inh['fqn'])

            # Fields
            for field in class_data.get('fields', []):
                all_fqns.add(field['type'])

            # Methods
            for method in class_data.get('methods', []):
                all_fqns.add(method['fqn'])

                # Return type
                if method.get('returnType'):
                    all_fqns.add(method['returnType'])

                # Arguments
                for arg in method.get('arguments', []):
                    all_fqns.add(arg)

                # Calls
                for call in method.get('calls', []):
                    all_fqns.add(call['toFqn'])

        print(f"[ASM] Collected {len(all_fqns)} unique FQNs")

        # Step 2: Batch resolve packages via symbol_index
        fqn_to_package = self._resolve_packages_batch(all_fqns, allowed_packages)
        print(f"[ASM] Resolved {len(fqn_to_package)} packages from symbol_index")

        # Step 3: Build URIs for project classes and add to symbol_index
        project_classes_count = 0
        project_symbols = []  # Will be inserted into symbol_index

        for class_data in result.get('classes', []):
            class_fqn = class_data['fqn']
            if class_fqn not in fqn_to_package:
                fqn_to_package[class_fqn] = project_package
                project_classes_count += 1

                # Detect Model entities by checking if .db. is in FQN
                is_model_entity = '.db.' in class_fqn

                # Build URI for this project class
                uri = self._build_project_uri(project_path, class_fqn, project_package, modules_base_path, is_model_entity)
                if uri:
                    project_symbols.append({
                        'fqn': class_fqn,
                        'uri': uri,
                        'package': project_package
                    })

            # Also add project package for methods of project classes
            for method in class_data.get('methods', []):
                method_fqn = method['fqn']
                if method_fqn not in fqn_to_package:
                    fqn_to_package[method_fqn] = project_package

        # Insert project symbols into symbol_index
        if project_symbols:
            self._store_project_symbols(project_symbols)
            print(f"[ASM] Added {len(project_symbols)} project symbols to symbol_index")

        print(f"[ASM] Added project package to {project_classes_count} project classes")

        # Step 4: Enrich data with packages
        for class_data in result.get('classes', []):
            # Add package to class
            class_data['package'] = fqn_to_package.get(class_data['fqn'])

            # Add packages to inheritance
            for inh in class_data.get('inheritance', []):
                inh['package'] = fqn_to_package.get(inh['fqn'])

            # Add packages to fields
            for field in class_data.get('fields', []):
                field['package'] = fqn_to_package.get(field['type'])

            # Add packages to methods
            for method in class_data.get('methods', []):
                method['package'] = fqn_to_package.get(method['fqn'])

                # Add package to return type
                if method.get('returnType'):
                    method['returnType_package'] = fqn_to_package.get(method['returnType'])

                # Add packages to arguments (create new structure)
                enriched_args = []
                for arg in method.get('arguments', []):
                    enriched_args.append({
                        'fqn': arg,
                        'package': fqn_to_package.get(arg)
                    })
                method['arguments'] = enriched_args

                # Add packages to calls
                for call in method.get('calls', []):
                    call['package'] = fqn_to_package.get(call['toFqn'])

        if allowed_packages:
            print(f"[ASM] Package filter: {', '.join(allowed_packages)}")

        return result

    def _build_project_uri(self, project_path: str, class_fqn: str, project_package: str, modules_base_path: str = None, is_model_entity: bool = False) -> Optional[str]:
        """
        Build URI for a project class by finding the corresponding source file

        Args:
            project_path: Path to project root (e.g., "/path/to/my-project")
            class_fqn: Fully qualified name (e.g., "com.bricklead.auction.MyClass")
            project_package: Package name (e.g., "open-auction-base-1.0.0")
            modules_base_path: Path to modules directory (e.g., "/path/to/Bricklead_Encheres/modules")

        Returns:
            URI like "file:///path/to/modules/open-auction-base/src/main/java/com/bricklead/auction/MyClass.java"
            or None if source file not found
        """
        # Convert FQN to relative path: com.example.MyClass -> com/example/MyClass.java
        relative_path = class_fqn.replace('.', '/') + '.java'

        # Extract module name from package (remove version)
        # "open-auction-base-1.0.0" -> "open-auction-base"
        module_name = self._extract_module_name(project_package)

        # Strategy 0: For Model entities, check build/src-gen/java first
        if is_model_entity:
            project_root = Path(project_path)

            # Try to find project root (go up from modules if needed)
            current = project_root
            project_base = None
            for _ in range(5):
                if current.name == 'modules':
                    project_base = current.parent  # Bricklead_Encheres
                    break
                current = current.parent
                if current == current.parent:
                    break

            # If found project base, look in build/src-gen/java
            if project_base:
                src_gen_dir = project_base / 'build' / 'src-gen' / 'java'
                if src_gen_dir.exists():
                    source_file = src_gen_dir / relative_path
                    if source_file.exists():
                        return source_file.resolve().as_uri()

        # Strategy 1: Use modules_base_path if provided
        if modules_base_path:
            modules_dir = Path(modules_base_path)

            # For Model entities, try build/src-gen/java at project level
            if is_model_entity:
                project_base = modules_dir.parent  # From .../modules to project root
                src_gen_dir = project_base / 'build' / 'src-gen' / 'java'
                if src_gen_dir.exists():
                    source_file = src_gen_dir / relative_path
                    if source_file.exists():
                        return source_file.resolve().as_uri()

            # Regular classes in module
            module_dir = modules_dir / module_name / 'src' / 'main' / 'java'
            if module_dir.exists():
                source_file = module_dir / relative_path
                if source_file.exists():
                    return source_file.resolve().as_uri()

        # Strategy 2: Try to find modules/ from project_root
        # Assuming project_root might be like: /path/to/Bricklead_Encheres/modules/open-auction-base/build/classes
        project_root = Path(project_path)

        # Go up and look for modules directory
        current = project_root
        for _ in range(5):  # Max 5 levels up
            if current.name == 'modules':
                # Found modules directory, use it
                module_dir = current / module_name / 'src' / 'main' / 'java'
                if module_dir.exists():
                    source_file = module_dir / relative_path
                    if source_file.exists():
                        return source_file.resolve().as_uri()
            current = current.parent
            if current == current.parent:  # Reached root
                break

        # Strategy 3: Try standard Axelor layout (sources/)
        sources_dir = project_root / 'sources'
        if sources_dir.exists():
            source_file = sources_dir / relative_path
            if source_file.exists():
                return source_file.resolve().as_uri()

        # Strategy 4: Try standard Maven/Gradle layout (src/main/java)
        src_main_java = project_root / 'src' / 'main' / 'java'
        if src_main_java.exists():
            source_file = src_main_java / relative_path
            if source_file.exists():
                return source_file.resolve().as_uri()

        # Not found
        return None

    def _extract_module_name(self, project_package: str) -> str:
        """
        Extract module name from package by removing version suffix

        Args:
            project_package: Package name like "open-auction-base-1.0.0"

        Returns:
            Module name like "open-auction-base"
        """
        # Remove version pattern (digits and dots at the end)
        import re
        # Match pattern like "-1.0.0" or "-1.0.0-SNAPSHOT"
        match = re.match(r'^(.+?)-[\d.]+.*$', project_package)
        if match:
            return match.group(1)
        # No version found, return as-is
        return project_package

    def _store_project_symbols(self, symbols: List[Dict]):
        """
        Store project symbols in symbol_index

        Args:
            symbols: List of {fqn, uri, package}
        """
        cursor = self.conn.cursor()

        for symbol in symbols:
            cursor.execute(
                "INSERT OR REPLACE INTO symbol_index (fqn, uri, package) VALUES (?, ?, ?)",
                (symbol['fqn'], symbol['uri'], symbol['package'])
            )

        self.conn.commit()

    def _resolve_packages_batch(self, fqns: set, allowed_packages: List[str] = None) -> Dict[str, str]:
        """
        Batch resolve FQNs to packages using symbol_index

        Args:
            fqns: Set of fully qualified names
            allowed_packages: List of package names to filter by (e.g., ["axelor-core-7.2.6"])
                            If None, searches all packages

        Returns:
            Dictionary mapping fqn -> package
        """
        if not fqns:
            return {}

        cursor = self.conn.cursor()
        fqn_to_package = {}

        # Convert set to list for SQL
        fqn_list = list(fqns)

        if allowed_packages:
            # Filter by allowed packages to handle multiple versions
            placeholders_fqn = ','.join('?' * len(fqn_list))
            placeholders_pkg = ','.join('?' * len(allowed_packages))
            query = f"""
                SELECT fqn, package
                FROM symbol_index
                WHERE fqn IN ({placeholders_fqn})
                AND package IN ({placeholders_pkg})
            """
            cursor.execute(query, (*fqn_list, *allowed_packages))
        else:
            # No filter, search all packages (may return ambiguous result if multiple versions exist)
            placeholders_fqn = ','.join('?' * len(fqn_list))
            query = f"SELECT fqn, package FROM symbol_index WHERE fqn IN ({placeholders_fqn})"
            cursor.execute(query, fqn_list)

        for row in cursor.fetchall():
            fqn_to_package[row['fqn']] = row['package']

        return fqn_to_package

    def _resolve_uri(self, fqn: str, allowed_packages: List[str] = None) -> Optional[str]:
        """
        Resolve FQN to URI using symbol_index

        Args:
            fqn: Fully qualified name
            allowed_packages: List of package names to filter by (e.g., ["axelor-core-7.2.6"])
                            If None, searches all packages

        Returns:
            URI if found, None otherwise
        """
        cursor = self.conn.cursor()

        if allowed_packages:
            # Filter by allowed packages to handle multiple versions
            placeholders = ','.join('?' * len(allowed_packages))
            query = f"SELECT uri FROM symbol_index WHERE fqn = ? AND package IN ({placeholders})"
            cursor.execute(query, (fqn, *allowed_packages))
        else:
            # No filter, search all packages (may return ambiguous result if multiple versions exist)
            cursor.execute("SELECT uri FROM symbol_index WHERE fqn = ?", (fqn,))

        row = cursor.fetchone()
        return row['uri'] if row else None

    def get_index_stats(self) -> Dict:
        """Get statistics about symbol index"""
        cursor = self.conn.cursor()

        # Total symbols
        cursor.execute("SELECT COUNT(*) as total FROM symbol_index")
        total = cursor.fetchone()['total']

        # Symbols by package
        cursor.execute("""
            SELECT package, COUNT(*) as count
            FROM symbol_index
            GROUP BY package
            ORDER BY count DESC
        """)
        by_package = [dict(row) for row in cursor.fetchall()]

        return {
            'total_symbols': total,
            'by_package': by_package
        }

    def close(self):
        """Close database connection"""
        self.conn.close()


def main():
    """Test the ASM extractor"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python ASMExtractor.py <command> [args]")
        print("Commands:")
        print("  build-index <axelor-repos-dir>  - Build symbol index")
        print("  extract <project-root>          - Extract project")
        print("  stats                           - Show index statistics")
        sys.exit(1)

    command = sys.argv[1]
    extractor = ASMExtractor()

    try:
        if command == "build-index":
            if len(sys.argv) < 3:
                print("Usage: python ASMExtractor.py build-index <axelor-repos-dir>")
                sys.exit(1)
            extractor.build_symbol_index(sys.argv[2])

        elif command == "extract":
            if len(sys.argv) < 3:
                print("Usage: python ASMExtractor.py extract <project-root>")
                sys.exit(1)
            result = extractor.extract_project(sys.argv[2])
            print(json.dumps(result, indent=2))

        elif command == "stats":
            stats = extractor.get_index_stats()
            print(f"\nSymbol Index Statistics:")
            print(f"  Total symbols: {stats['total_symbols']}")
            print(f"\nBy package:")
            for pkg in stats['by_package'][:10]:
                print(f"    {pkg['package']}: {pkg['count']} symbols")

        else:
            print(f"Unknown command: {command}")
            sys.exit(1)

    finally:
        extractor.close()


if __name__ == "__main__":
    main()
