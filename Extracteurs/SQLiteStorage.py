#!/usr/bin/env python3
"""
SQLite storage for call graph (nodes and edges)
Stores AST structure in a graph database format
Auto-detects database location following the same pattern as ChromaDB
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class SQLiteStorage:
    """Manages call graph storage in SQLite (following ChromaDB patterns)"""

    def __init__(self, db_path: Optional[str] = None):
        """Initialize SQLite database with auto-detection

        Args:
            db_path: Explicit path to SQLite database file, or None for auto-detection
                    If None, auto-detects in current working directory:
                    - Priority 1: .callgraph.db (explicit SQLite)
                    - Priority 2: .vector-raw-db/callgraph.db (alongside ChromaDB)
                    - Priority 3: .vector-semantic-db/callgraph.db (alongside ChromaDB)
                    - Fallback: .callgraph.db (will be created)
        """
        if db_path is None:
            db_path = self._auto_detect_db()

        self.db_path = Path(db_path)

        # Create parent directory if needed
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row  # Return rows as dicts
        self._init_schema()

        logger.info(f"SQLite storage initialized: {self.db_path}")

    def _auto_detect_db(self) -> str:
        """Auto-detect database location in current working directory

        Returns:
            Path to SQLite database file
        """
        cwd = Path.cwd()

        # Priority 1: Explicit .callgraph.db
        explicit_db = cwd / ".callgraph.db"
        if explicit_db.exists():
            logger.info("Using explicit .callgraph.db")
            return str(explicit_db)

        # Priority 2: Alongside semantic ChromaDB (if exists)
        semantic_db = cwd / ".vector-semantic-db" / "callgraph.db"
        if semantic_db.parent.exists():
            logger.info("Using callgraph.db alongside semantic ChromaDB")
            return str(semantic_db)

        # Priority 3: Alongside raw ChromaDB (if exists)
        raw_db = cwd / ".vector-raw-db" / "callgraph.db"
        if raw_db.parent.exists():
            logger.info("Using callgraph.db alongside raw ChromaDB")
            return str(raw_db)

        # Fallback: Create explicit .callgraph.db
        logger.info("No database found, will create .callgraph.db")
        return str(explicit_db)

    def _init_schema(self):
        """Create tables and indexes if they don't exist"""
        cursor = self.conn.cursor()

        # Table des noeuds (classes et méthodes)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                fqn TEXT PRIMARY KEY NOT NULL,
                node_type TEXT NOT NULL,
                name TEXT NOT NULL,
                signature TEXT,
                uri TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Table des arêtes (relations)
        # Note: Pas de FOREIGN KEY pour plus de flexibilité
        # Les nodes "stub" seront créés automatiquement si besoin
        # edge_type: 'call', 'inheritance', 'member_of'
        # kind: Pour member_of -> 'method' (method to class), 'return' (type to method), 'argument' (type to method)
        #       Pour inheritance -> 'extends', 'implements', 'overrides'
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edge_type TEXT NOT NULL,
                from_fqn TEXT NOT NULL,
                from_uri TEXT NOT NULL,
                to_fqn TEXT NOT NULL,
                to_uri TEXT NOT NULL,
                kind TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Table des annotations (many-to-many avec edges)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edge_annotations (
                edge_id INTEGER NOT NULL,
                annotation TEXT NOT NULL,
                FOREIGN KEY(edge_id) REFERENCES edges(id) ON DELETE CASCADE
            )
        """)

        # Index pour performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_fqn)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_to_fqn ON edges(to_fqn)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edge_annotations_edge_id ON edge_annotations(edge_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edge_annotations_annotation ON edge_annotations(annotation)")

        self.conn.commit()

    def add_node(self, node: Dict, update_if_exists: bool = False, commit: bool = True) -> bool:
        """Add a single node (class or method)

        Args:
            node: Dict with keys: fqn, node_type, name, signature, uri
            update_if_exists: If True, update existing node with new data (UPSERT)
            commit: If True, commit after insert (set False for batch operations)

        Returns:
            True if inserted/updated, False if already exists and update_if_exists=False
        """
        cursor = self.conn.cursor()

        if update_if_exists:
            # UPSERT: Insert or replace (SQLite's INSERT OR REPLACE)
            cursor.execute("""
                INSERT OR REPLACE INTO nodes (fqn, node_type, name, signature, uri)
                VALUES (?, ?, ?, ?, ?)
            """, (
                node.get('fqn'),
                node.get('node_type'),
                node.get('name'),
                node.get('signature'),
                node.get('uri')
            ))
            if commit:
                self.conn.commit()
            return True
        else:
            try:
                cursor.execute("""
                    INSERT INTO nodes (fqn, node_type, name, signature, uri)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    node.get('fqn'),
                    node.get('node_type'),
                    node.get('name'),
                    node.get('signature'),
                    node.get('uri')
                ))
                if commit:
                    self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Node already exists (FQN is primary key)
                return False

    def ensure_node_exists(self, fqn: str, node_type: str = 'class', commit: bool = True) -> bool:
        """Ensure a node exists, create stub if not

        Args:
            fqn: Fully qualified name
            node_type: Type of node ('class' or 'method')

        Returns:
            True if node was created, False if already existed
        """
        # Check if node already exists
        if self.find_node(fqn):
            return False

        # Create stub node
        name = fqn.split('.')[-1] if '.' in fqn else fqn
        stub_node = {
            'fqn': fqn,
            'node_type': node_type,
            'name': name,
            'signature': None,
            'uri': 'unknown'
        }
        return self.add_node(stub_node, commit=commit)

    def add_nodes_batch(self, nodes: List[Dict], update_if_exists: bool = False) -> Tuple[int, int]:
        """Add multiple nodes in batch (optimized with single transaction)

        Args:
            nodes: List of node dicts
            update_if_exists: If True, update existing nodes (UPSERT)

        Returns:
            Tuple (inserted_count, skipped_count)
        """
        if not nodes:
            return 0, 0

        cursor = self.conn.cursor()
        inserted = 0
        skipped = 0

        if update_if_exists:
            # UPSERT mode: use executemany with INSERT OR REPLACE
            node_rows = [
                (n.get('fqn'), n.get('node_type'), n.get('name'), n.get('signature'), n.get('uri'))
                for n in nodes
            ]
            cursor.executemany("""
                INSERT OR REPLACE INTO nodes (fqn, node_type, name, signature, uri)
                VALUES (?, ?, ?, ?, ?)
            """, node_rows)
            inserted = len(node_rows)
        else:
            # Normal mode: use INSERT OR IGNORE (skip duplicates)
            node_rows = [
                (n.get('fqn'), n.get('node_type'), n.get('name'), n.get('signature'), n.get('uri'))
                for n in nodes
            ]
            cursor.executemany("""
                INSERT OR IGNORE INTO nodes (fqn, node_type, name, signature, uri)
                VALUES (?, ?, ?, ?, ?)
            """, node_rows)
            # SQLite doesn't tell us how many were actually inserted with OR IGNORE
            # We could do a SELECT COUNT but it's expensive, so we estimate
            inserted = cursor.rowcount if cursor.rowcount > 0 else len(node_rows)
            skipped = len(node_rows) - inserted

        self.conn.commit()
        return inserted, skipped

    def add_edge(self, edge: Dict, auto_create_stubs: bool = True, commit: bool = True) -> int:
        """Add a single edge (call, extends, implements, overrides, member_of)

        Args:
            edge: Dict with keys: edge_type, from_fqn, from_uri, to_fqn, to_uri, kind, annotations
            auto_create_stubs: If True, automatically create stub nodes for missing from_fqn and to_fqn
            commit: If True, commit after insert (set False for batch operations)

        Returns:
            The edge id
        """
        cursor = self.conn.cursor()

        # Ensure source and target nodes exist (create stubs if needed)
        if auto_create_stubs:
            # Ensure from_fqn exists
            if edge.get('from_fqn'):
                self.ensure_node_exists(edge['from_fqn'], node_type='method', commit=commit)

            # Ensure to_fqn exists
            if edge.get('to_fqn'):
                # Determine node type from edge type
                if edge.get('edge_type') == 'call':
                    # For calls, to_fqn is usually a method or class
                    # Use FQN to determine: if it has (), it's a method
                    node_type = 'method' if '(' in edge['to_fqn'] else 'class'
                else:
                    # For extends/implements/overrides, it's usually a class
                    node_type = 'class'

                self.ensure_node_exists(edge['to_fqn'], node_type=node_type, commit=commit)

        cursor.execute("""
            INSERT INTO edges (edge_type, from_fqn, from_uri, to_fqn, to_uri, kind)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            edge.get('edge_type'),
            edge.get('from_fqn'),
            edge.get('from_uri'),
            edge.get('to_fqn'),
            edge.get('to_uri'),
            edge.get('kind')
        ))

        edge_id = cursor.lastrowid

        # Add annotations if present
        annotations = edge.get('annotations', [])
        if annotations:
            for annotation in annotations:
                cursor.execute("""
                    INSERT INTO edge_annotations (edge_id, annotation)
                    VALUES (?, ?)
                """, (edge_id, annotation))

        if commit:
            self.conn.commit()
        return edge_id

    def add_edges_batch(self, edges: List[Dict], auto_create_stubs: bool = True) -> int:
        """Add multiple edges in batch (optimized with single transaction)

        Args:
            edges: List of edge dicts
            auto_create_stubs: If True, automatically create stub nodes

        Returns:
            Number of edges inserted
        """
        if not edges:
            return 0

        cursor = self.conn.cursor()

        # Prepare data for batch insert
        edge_rows = []
        annotations_to_insert = []

        for edge in edges:
            # Ensure nodes exist if requested
            if auto_create_stubs:
                if edge.get('from_fqn'):
                    self.ensure_node_exists(edge['from_fqn'], node_type='method', commit=False)
                if edge.get('to_fqn'):
                    if edge.get('edge_type') == 'call':
                        node_type = 'method' if '(' in edge['to_fqn'] else 'class'
                    else:
                        node_type = 'class'
                    self.ensure_node_exists(edge['to_fqn'], node_type=node_type, commit=False)

            # Add to batch
            edge_rows.append((
                edge.get('edge_type'),
                edge.get('from_fqn'),
                edge.get('from_uri'),
                edge.get('to_fqn'),
                edge.get('to_uri'),
                edge.get('kind')
            ))

            # Store annotations for later (we'll get edge IDs after insert)
            if edge.get('annotations'):
                annotations_to_insert.append((len(edge_rows) - 1, edge.get('annotations')))

        # Batch insert edges
        cursor.executemany("""
            INSERT INTO edges (edge_type, from_fqn, from_uri, to_fqn, to_uri, kind)
            VALUES (?, ?, ?, ?, ?, ?)
        """, edge_rows)

        # Get the first inserted ID
        first_id = cursor.lastrowid - len(edge_rows) + 1

        # Batch insert annotations if any
        if annotations_to_insert:
            annotation_rows = []
            for idx, annotations in annotations_to_insert:
                edge_id = first_id + idx
                for annotation in annotations:
                    annotation_rows.append((edge_id, annotation))

            if annotation_rows:
                cursor.executemany("""
                    INSERT INTO edge_annotations (edge_id, annotation)
                    VALUES (?, ?)
                """, annotation_rows)

        self.conn.commit()
        return len(edge_rows)

    def find_node(self, fqn: str) -> Optional[Dict]:
        """Find a node by FQN

        Returns:
            Node dict or None
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE fqn = ?", (fqn,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def find_usages(self, symbol: str, edge_type: str = 'call', limit: int = 100) -> List[Dict]:
        """Find all usages of a symbol (who calls this method/class)

        Args:
            symbol: FQN or name to search
            edge_type: Type of edge ('call', 'extends', 'implements', 'overrides')
            limit: Max results

        Returns:
            List of edges
        """
        cursor = self.conn.cursor()

        # Search by exact FQN or by name pattern
        cursor.execute("""
            SELECT e.*, n.name as from_name, n.node_type as from_type
            FROM edges e
            LEFT JOIN nodes n ON e.from_fqn = n.fqn
            WHERE e.edge_type = ?
            AND (e.to_fqn = ? OR e.to_fqn LIKE ? OR e.to_signature LIKE ?)
            LIMIT ?
        """, (edge_type, symbol, f'%.{symbol}', f'%.{symbol}%', limit))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> Dict:
        """Get database statistics

        Returns:
            Dict with counts
        """
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM nodes")
        total_nodes = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as total FROM nodes WHERE node_type = 'class'")
        total_classes = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as total FROM nodes WHERE node_type = 'method'")
        total_methods = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as total FROM edges")
        total_edges = cursor.fetchone()['total']

        cursor.execute("SELECT edge_type, COUNT(*) as count FROM edges GROUP BY edge_type")
        edges_by_type = {row['edge_type']: row['count'] for row in cursor.fetchall()}

        return {
            'total_nodes': total_nodes,
            'total_classes': total_classes,
            'total_methods': total_methods,
            'total_edges': total_edges,
            'edges_by_type': edges_by_type
        }

    def reset(self):
        """Drop all tables and recreate schema"""
        cursor = self.conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS edges")
        cursor.execute("DROP TABLE IF EXISTS nodes")
        self.conn.commit()
        self._init_schema()

    def close(self):
        """Close database connection"""
        self.conn.close()


# Example usage and test
if __name__ == "__main__":
    # Setup logging for standalone test
    logging.basicConfig(level=logging.INFO)

    # Test
    storage = SQLiteStorage("test.db")

    # Reset for clean test
    storage.reset()

    # Add a class node
    storage.add_node({
        'fqn': 'com.example.MyClass',
        'node_type': 'class',
        'name': 'MyClass',
        'signature': None,
        'uri': 'file:///path/to/MyClass.java'
    })

    # Add a method node
    storage.add_node({
        'fqn': 'com.example.MyClass.myMethod(String)',
        'node_type': 'method',
        'name': 'myMethod',
        'signature': 'myMethod(String)',
        'uri': 'file:///path/to/MyClass.java:10'
    })

    # Add a call edge
    storage.add_edge({
        'edge_type': 'call',
        'from_fqn': 'com.example.MyClass.myMethod(String)',
        'from_uri': 'file:///path/to/MyClass.java:12',
        'to_fqn': 'com.example.OtherClass.otherMethod()',
        'to_signature': 'com.example.OtherClass.otherMethod()',
        'to_uri': 'file:///path/to/OtherClass.java:20',
        'metadata': None
    })

    # Stats
    print("\n=== Database Statistics ===")
    print(json.dumps(storage.get_stats(), indent=2))

    # Test find_node
    print("\n=== Find Node ===")
    node = storage.find_node('com.example.MyClass')
    if node:
        print(f"Found: {node['fqn']} ({node['node_type']})")

    # Test find_usages
    print("\n=== Find Usages ===")
    usages = storage.find_usages('otherMethod')
    for usage in usages:
        print(f"Called from: {usage['from_fqn']} at {usage['from_uri']}")

    storage.close()
    print("\nTest completed successfully!")
