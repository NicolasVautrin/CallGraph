#!/usr/bin/env python3
"""
Build call graph database in ChromaDB for impact analysis
Processes all Java and XML files and stores usage information
"""

import sys
from pathlib import Path
from typing import List, Optional
from datetime import datetime
import chromadb
from chromadb.config import Settings
from tqdm import tqdm
import uuid
import hashlib

# Import our extractors
sys.path.insert(0, str(Path(__file__).parent))
from JavaASTExtractor import JavaASTExtractor
from AxelorXmlExtractor import AxelorXmlExtractor
from TypeScriptASTExtractor import TypeScriptASTExtractor


class StorageWriter:
    """Manages call graph storage in ChromaDB (Write operations)"""

    # Embedding configuration constants
    EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
    DOCUMENT_STRATEGY_VERSION = "1.0"  # Increment when document generation logic changes
    NO_EMBEDDING_VALUE = "none"  # Sentinel value for entries without embeddings
    UNKNOWN_VALUE = "unknown"  # Sentinel value for missing/unknown metadata

    def __init__(self, db_path: str = ".vector-db", use_embeddings: bool = True):
        """Initialize ChromaDB connectionok vas 

        Args:
            db_path: Path to ChromaDB storage
            use_embeddings: If False, provide minimal vectors (metadata-only mode)
        """
        self.db_path = Path(db_path)
        self.use_embeddings = use_embeddings
        self.client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(anonymized_telemetry=False)
        )

        # Determine embedding configuration
        if not use_embeddings:
            # No embeddings mode - we'll provide minimal vectors manually
            self.embedding_model_name = self.NO_EMBEDDING_VALUE
            self.document_strategy_version = self.NO_EMBEDDING_VALUE
        else:
            # Use class constants for embeddings
            self.embedding_model_name = self.EMBEDDING_MODEL_NAME
            self.document_strategy_version = self.DOCUMENT_STRATEGY_VERSION

        # Always create collection with sentence-transformers (default)
        # In fast mode, we'll provide pre-computed minimal vectors to skip computation
        self.collection = self.client.get_or_create_collection(
            name="call_graph",
            metadata={"description": "Java call graph for impact analysis"}
        )

        if use_embeddings:
            print(f"Using sentence-transformers embeddings (semantic search enabled)")
            print(f"  Model: {self.embedding_model_name}")
        else:
            print("Fast mode: Providing minimal vectors (metadata-only, semantic search disabled)")

    def add_entries(self, entries: List[dict], source_type: str = "unknown"):
        """Add entries to the database (unified method for Java/XML/TypeScript)

        Args:
            entries: List of dicts, each with:
                - "document": str - Text for embedding
                - "metadata": dict - All metadata fields (any structure)
            source_type: Source type for logging ("Java", "XML", "TypeScript")
        """
        if not entries:
            return

        ids = []
        documents = []
        metadatas = []

        # Get current timestamp for this batch
        scan_timestamp = datetime.now().isoformat()
        embedding_timestamp = scan_timestamp

        for entry in entries:
            # Generate unique GUID
            entry_id = str(uuid.uuid4())

            # Extract document and metadata from entry
            document = entry.get("document", "")
            metadata = dict(entry.get("metadata", {}))  # Copy metadata

            # Filter out None values (ChromaDB doesn't accept None)
            metadata = {k: v for k, v in metadata.items() if v is not None}

            # Add the 4 tracking fields
            metadata.update({
                "embedding_model_name": self.embedding_model_name,
                "document_strategy_version": self.document_strategy_version,
                "embedding_timestamp": embedding_timestamp,
                "scan_timestamp": scan_timestamp
            })

            ids.append(entry_id)
            documents.append(document)
            metadatas.append(metadata)

        # Store in batches
        batch_size = 500
        total_batches = (len(ids) + batch_size - 1) // batch_size

        # Prepare minimal embeddings if in fast mode
        if not self.use_embeddings:
            minimal_embeddings = [[0.0] for _ in range(len(ids))]

        for batch_num, i in enumerate(range(0, len(ids), batch_size), 1):

            if self.use_embeddings:
                self.collection.add(
                    ids=ids[i:i+batch_size],
                    documents=documents[i:i+batch_size],
                    metadatas=metadatas[i:i+batch_size]
                )
            else:
                self.collection.add(
                    ids=ids[i:i+batch_size],
                    documents=documents[i:i+batch_size],
                    metadatas=metadatas[i:i+batch_size],
                    embeddings=minimal_embeddings[i:i+batch_size]
                )

    def add_usages(self, usages: List[dict]):
        """Add Java usages (delegates to add_entries)"""
        self.add_entries(usages, source_type="Java")

    def add_xml_references(self, references: List[dict]):
        """Add XML references (delegates to add_entries)"""
        self.add_entries(references, source_type="XML")

    def add_ts_usages(self, usages: List[dict]):
        """Add TypeScript usages (delegates to add_entries)"""
        self.add_entries(usages, source_type="TypeScript")

    def find_callers(self, callee_symbol: str, limit: int = 50) -> List[dict]:
        """Find all places where a method/class/field is used (using new standardized format)"""
        results = self.collection.get(
            where={"calleeSymbol": callee_symbol},
            limit=limit
        )

        # Return metadatas directly, excluding stringContext
        callers = []
        for metadata in results['metadatas']:
            caller_info = {k: v for k, v in metadata.items() if k != 'stringContext'}
            callers.append(caller_info)

        return callers

    def get_stats(self) -> dict:
        """Get database statistics"""
        count = self.collection.count()

        # Sample to get type and source distribution
        sample = self.collection.get(limit=min(1000, count))

        usage_types = {}
        sources = {'java': 0, 'xml': 0, 'typescript': 0}
        modules = {}
        embedding_models = {}
        document_strategy_versions = {}

        for metadata in sample['metadatas']:
            # Support both old 'usage_type' and new 'usageType' field names
            usage_type = metadata.get('usageType') or metadata.get('usage_type', self.UNKNOWN_VALUE)
            usage_types[usage_type] = usage_types.get(usage_type, 0) + 1

            source = metadata.get('source', 'java')
            sources[source] = sources.get(source, 0) + 1

            module = metadata.get('module', '')
            if module:
                modules[module] = modules.get(module, 0) + 1

            # Track embedding models
            emb_model = metadata.get('embedding_model_name', self.UNKNOWN_VALUE)
            embedding_models[emb_model] = embedding_models.get(emb_model, 0) + 1

            # Track document strategy versions
            doc_version = metadata.get('document_strategy_version', self.UNKNOWN_VALUE)
            document_strategy_versions[doc_version] = document_strategy_versions.get(doc_version, 0) + 1

        return {
            'total_usages': count,
            'sources': sources,
            'usage_types': usage_types,
            'modules': modules,
            'embedding_models': embedding_models,
            'document_strategy_versions': document_strategy_versions
        }

    def get_outdated_entries(self, check_strategy: bool = True, check_library: bool = False) -> dict:
        """Get entries with outdated document strategy or embedding library

        Args:
            check_strategy: Check for outdated document_strategy_version
            check_library: Check for outdated embedding_library_type (e.g. 'none')

        Returns:
            Dict with 'ids', 'metadatas', and 'documents' of outdated entries
        """
        # Build where clause based on what we're checking
        where_clauses = []

        if check_strategy:
            # Find entries with different strategy version
            # If current = "1.0", this finds everything != "1.0" (including "none")
            # If current = "none", this finds everything != "none" (nothing normally)
            where_clauses.append({
                "document_strategy_version": {"$ne": self.document_strategy_version}
            })

        if check_library:
            # Find entries without embeddings
            where_clauses.append({
                "embedding_model_name": self.NO_EMBEDDING_VALUE
            })

        if not where_clauses:
            return {'ids': [], 'metadatas': [], 'documents': []}

        # Combine with OR if multiple conditions
        if len(where_clauses) == 1:
            where_filter = where_clauses[0]
        else:
            where_filter = {"$or": where_clauses}

        # Query with filter
        try:
            results = self.collection.get(
                where=where_filter,
                include=['metadatas', 'documents']
            )
            return results
        except Exception as e:
            print(f"Warning: Could not filter outdated entries: {e}")
            return {'ids': [], 'metadatas': [], 'documents': []}

    def get_embedding_health(self) -> dict:
        """Get detailed embedding health status - useful for tracking scan/rescan progress"""
        count = self.collection.count()

        if count == 0:
            return {'status': 'empty', 'total': 0}

        # Get a representative sample
        sample_size = min(1000, count)
        sample = self.collection.get(limit=sample_size, include=['metadatas'])

        # Analyze embedding status
        no_embeddings = 0
        with_embeddings = 0
        outdated_strategy = 0
        current_strategy_version = self.document_strategy_version

        for metadata in sample['metadatas']:
            emb_model = metadata.get('embedding_model_name', self.UNKNOWN_VALUE)
            doc_version = metadata.get('document_strategy_version', self.UNKNOWN_VALUE)

            if emb_model == self.NO_EMBEDDING_VALUE:
                no_embeddings += 1
            else:
                with_embeddings += 1

            if doc_version != current_strategy_version:
                outdated_strategy += 1

        # Extrapolate to full database
        ratio_no_emb = no_embeddings / sample_size
        ratio_with_emb = with_embeddings / sample_size
        ratio_outdated = outdated_strategy / sample_size

        estimated_no_emb = int(count * ratio_no_emb)
        estimated_with_emb = int(count * ratio_with_emb)
        estimated_outdated = int(count * ratio_outdated)

        return {
            'status': 'analyzed',
            'total': count,
            'no_embeddings': estimated_no_emb,
            'with_embeddings': estimated_with_emb,
            'outdated_strategy': estimated_outdated,
            'current_strategy_version': current_strategy_version,
            'completion_percentage': round((estimated_with_emb / count) * 100, 1) if count > 0 else 0
        }

    def reset(self):
        """Clear the call graph collection"""
        self.client.delete_collection("call_graph")

        # Recreate collection (always same config - embedding choice handled at insertion time)
        self.collection = self.client.get_or_create_collection(
            name="call_graph",
            metadata={"description": "Java call graph for impact analysis"}
        )

    def copy_from_cache(self, cache_db_path: str, batch_size: int = 500, limit: Optional[int] = None):
        """Copy entries from a cache database into this database

        This is much faster than re-extracting from source files.
        Used to merge Axelor cache databases into the project database.

        Args:
            cache_db_path: Path to the cache database to copy from
            batch_size: Number of entries to copy per batch (default: 500)
            limit: Optional maximum number of entries to copy (None = all)

        Returns:
            Number of entries copied
        """
        print(f"    Copying from cache: {cache_db_path}")

        # Open cache database (read-only)
        cache_client = chromadb.PersistentClient(
            path=str(cache_db_path),
            settings=Settings(anonymized_telemetry=False)
        )

        try:
            cache_collection = cache_client.get_collection("call_graph")
        except Exception as e:
            print(f"    ERROR: Could not open cache collection: {e}")
            return 0

        # Get total count
        total_count = cache_collection.count()

        # Apply limit if specified
        max_to_copy = min(total_count, limit) if limit is not None else total_count

        print(f"    Found {total_count} entries in cache")
        if limit is not None:
            print(f"    Limiting to {max_to_copy} entries")

        if max_to_copy == 0:
            return 0

        # Copy in batches to avoid memory issues
        copied = 0
        offset = 0

        while offset < max_to_copy and copied < max_to_copy:
            # Calculate how many to fetch in this batch (respecting limit)
            entries_remaining = max_to_copy - copied
            current_batch_size = min(batch_size, entries_remaining)

            # Fetch batch from cache (including embeddings if they exist)
            batch = cache_collection.get(
                limit=current_batch_size,
                offset=offset,
                include=['documents', 'metadatas', 'embeddings']
            )

            batch_size_actual = len(batch['ids'])
            if batch_size_actual == 0:
                break

            # Log first embedding info for debugging (only first batch)
            if copied == 0:
                embeddings = batch.get('embeddings')
                if embeddings is not None:
                    first_emb = embeddings[0] if len(embeddings) > 0 else None
                    if first_emb is not None:
                        emb_type = type(first_emb).__name__
                        emb_len = len(first_emb) if hasattr(first_emb, '__len__') else 'N/A'
                        emb_preview = str(first_emb[:5]) if hasattr(first_emb, '__getitem__') else str(first_emb)
                        print(f"    [DEBUG] First embedding: type={emb_type}, len={emb_len}, preview={emb_preview}...")
                    else:
                        print(f"    [DEBUG] First embedding is None")
                else:
                    print(f"    [DEBUG] No embeddings in batch")

            # Prepare arguments for add()
            add_args = {
                'ids': batch['ids'],
                'documents': batch['documents'],
                'metadatas': batch['metadatas']
            }

            # Only include embeddings if they exist and are not all None
            # ChromaDB returns None or a list of None when no embeddings exist
            embeddings = batch.get('embeddings')
            has_embeddings = False
            if embeddings is not None:
                # Check if any embedding is not None (handles both list and numpy arrays)
                try:
                    has_embeddings = any(emb is not None for emb in embeddings)
                except (TypeError, ValueError):
                    # If iteration fails or ambiguous truth value, assume we have embeddings
                    has_embeddings = True

            if has_embeddings:
                add_args['embeddings'] = embeddings
                if copied == 0:
                    print(f"    [DEBUG] Including embeddings in add() - count: {len(embeddings)}")
            else:
                if copied == 0:
                    print(f"    [DEBUG] No valid embeddings found, ChromaDB will handle based on collection config")
            # Otherwise, let ChromaDB auto-generate or use minimal vectors based on collection config

            # Add to target database
            self.collection.add(**add_args)

            copied += batch_size_actual
            offset += batch_size_actual

            # Progress indicator
            if copied % 5000 == 0:
                progress = f"{copied}/{max_to_copy}" if limit else f"{copied}/{total_count}"
                print(f"    Copied {progress} entries...")

        print(f"    OK - Copied {copied} entries from cache")
        return copied


def main():
    """Build call graph database from project sources

    NOTE: This is a legacy script. Use extraction_manager.py instead for better features:
    - Axelor dependency detection and downloading
    - Cache management for platform/suite
    - Better progress tracking
    - More robust error handling
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Build call graph database from Java and XML (LEGACY - use extraction_manager.py instead)"
    )
    parser.add_argument("--reset", action="store_true",
                       help="Reset the database before building")
    parser.add_argument("--stats", action="store_true",
                       help="Show database statistics")
    parser.add_argument("--find", type=str,
                       help="Find callers of a method/class")
    parser.add_argument("--with-embeddings", action="store_true",
                       help="Enable embedding generation (slower, enables semantic search)")
    parser.add_argument("--health", action="store_true",
                       help="Show embedding health status")
    parser.add_argument("--limit", type=int, default=None,
                       help="Limit on number of entries to extract (for testing)")

    args = parser.parse_args()

    # Initialize database (no embeddings by default for speed)
    db = CallGraphDB(use_embeddings=args.with_embeddings)

    # Show health if requested
    if args.health:
        health = db.get_embedding_health()
        print("\n" + "="*60)
        print("Embedding Health Status")
        print("="*60)

        if health['status'] == 'empty':
            print("\nDatabase is empty. No entries found.")
        else:
            print(f"\nTotal entries: {health['total']:,}")
            print(f"\nEmbedding Status:")
            print(f"  [+] With embeddings:     {health['with_embeddings']:,} ({health['completion_percentage']}%)")
            print(f"  [-] Without embeddings:  {health['no_embeddings']:,}")

            if health['outdated_strategy'] > 0:
                print(f"  [!] Outdated strategy:   {health['outdated_strategy']:,}")
                print(f"\n  Current strategy version: {health['current_strategy_version']}")
                print(f"  → Consider regenerating embeddings for outdated entries")

            if health['no_embeddings'] > 0:
                print(f"\n💡 Next steps:")
                print(f"  Run: python scripts/regenerate_embeddings.py")
                print(f"  This will generate embeddings for {health['no_embeddings']:,} entries")
        return

    # Show stats if requested
    if args.stats:
        stats = db.get_stats()
        print("\nCall Graph Database Statistics:")
        print(f"  Total usages: {stats['total_usages']}")
        print(f"\n  By source:")
        for source, count in sorted(stats['sources'].items()):
            print(f"    {source}: {count}")
        print(f"\n  By type:")
        for usage_type, count in sorted(stats['usage_types'].items()):
            print(f"    {usage_type}: {count}")
        if stats['modules']:
            print(f"\n  By module (top 10):")
            for module, count in sorted(stats['modules'].items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"    {module}: {count}")

        # Also show embedding models in stats
        print(f"\n  Embedding Models:")
        for emb_model, count in sorted(stats['embedding_models'].items()):
            print(f"    {emb_model}: {count}")

        print(f"\n  Document Strategy Versions:")
        for version, count in sorted(stats['document_strategy_versions'].items()):
            print(f"    v{version}: {count}")
        return

    # Find callers if requested
    if args.find:
        print(f"\nFinding usages of: {args.find}\n")
        callers = db.find_callers(args.find, limit=100)

        if not callers:
            print("No usages found.")
            return

        # Group by type
        by_type = {}
        for caller in callers:
            # Support both old 'usage_type' and new 'usageType' field names
            usage_type = caller.get('usageType') or caller.get('usage_type', 'unknown')
            if usage_type not in by_type:
                by_type[usage_type] = []
            by_type[usage_type].append(caller)

        # Display results
        for usage_type, items in sorted(by_type.items()):
            print(f"{usage_type.upper()} ({len(items)} usages):")
            for caller in items[:20]:  # Show first 20 of each type
                source_badge = f"[{caller['source'].upper()}]" if caller.get('source') else ""
                module_badge = f"[{caller['module']}]" if caller.get('module') else ""
                caller_uri = caller.get('callerUri', '')
                caller_line = caller.get('callerLine', '')
                print(f"  {source_badge}{module_badge} {caller_uri}")
                caller_symbol = caller.get('callerSymbol')
                if caller_symbol:
                    caller_kind = caller.get('callerKind', '')
                    print(f"    in: {caller_symbol} ({caller_kind})")
                if caller.get('stringContext'):
                    print(f"    context: {caller['stringContext']}")
            print()

        return

    # Reset if requested
    if args.reset:
        print("Resetting call graph database...")
        db.reset()

    # Simple generator-based processing
    stats = {'java': 0, 'xml': 0}
    batch_size = 500

    java_batch = []
    xml_batch = []

    def flush_batches():
        """Store accumulated batches"""
        nonlocal java_batch, xml_batch
        if java_batch:
            db.add_usages(java_batch)
            stats['java'] += len(java_batch)
            java_batch = []
        if xml_batch:
            db.add_xml_references(xml_batch)
            stats['xml'] += len(xml_batch)
            xml_batch = []

    # Process Java files
    print("\n=== Processing Java files ===")
    try:
        java_extractor = JavaCallGraphExtractor(repos=["."])

        for source_type, entry in java_extractor.extract_all(limit=args.limit):
            java_batch.append(entry)
            if len(java_batch) >= batch_size:
                flush_batches()

    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        flush_batches()

    # Process XML files
    print("\n=== Processing XML files ===")
    xml_extractor = AxelorXmlExtractor(repos=["."])

    for source_type, entry in xml_extractor.extract_all(limit=args.limit):
        xml_batch.append(entry)
        if len(xml_batch) >= batch_size:
            flush_batches()

    flush_batches()

    # Show final stats
    final_stats = db.get_stats()
    print("\n" + "="*60)
    print("Database updated:")
    print(f"  Total entries: {final_stats['total_usages']}")
    print(f"\n  By source:")
    for source, count in sorted(final_stats['sources'].items()):
        print(f"    {source}: {count}")
    print(f"\n  By type:")
    for usage_type, count in sorted(final_stats['usage_types'].items()):
        print(f"    {usage_type}: {count}")
    if final_stats['modules']:
        print(f"\n  By module (top 10):")
        for module, count in sorted(final_stats['modules'].items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"    {module}: {count}")


if __name__ == "__main__":
    main()