#!/usr/bin/env python3
"""
Manages the full extraction pipeline:
- Detects Axelor versions
- Downloads missing repos
- Extracts call graph to vector database
"""

from pathlib import Path
from typing import Optional, List
import sys
import shutil

# Import our modules
from AxelorRepoManager import AxelorRepoManager
from StorageWriter import StorageWriter
from JavaASTExtractor import JavaASTExtractor
from AxelorXmlExtractor import AxelorXmlExtractor


class ExtractionManager:
    """Manages the full extraction pipeline"""

    def __init__(self, project_root: Path, debug: bool = False):
        """Initialize extraction manager

        Args:
            project_root: Root directory of the project
            debug: Enable detailed extraction statistics
        """
        self.project_root = Path(project_root).resolve()
        self.repo_manager = AxelorRepoManager(self.project_root)
        self.db = None
        self.debug = debug

        # Debug statistics
        if self.debug:
            self.debug_stats = {
                'extracted': {},  # repo -> {type -> count}
                'stored': {}      # repo -> count
            }

    def ensure_axelor_repos(self) -> dict:
        """Ensure Axelor repositories are downloaded

        Returns:
            Dictionary of repo paths
        """
        print("\n" + "="*60)
        print("Detecting Axelor versions...")
        print("="*60)

        platform_version, suite_version = self.repo_manager.detect_axelor_versions()

        print(f"  Platform: {platform_version or 'Not detected'}")
        print(f"  Suite: {suite_version or 'Not detected'}")

        if not platform_version and not suite_version:
            print("\nWarning: No Axelor versions detected")
            return {}

        print("\n" + "="*60)
        print("Ensuring Axelor repositories...")
        print("="*60)

        repos = self.repo_manager.ensure_repos(platform_version, suite_version)

        return repos

    def _extract_all_repos(self, repos_to_extract: dict, all_repos: List[Path], project_db: StorageWriter, use_embeddings: bool = False, limit: Optional[int] = None):
        """Extract all repos at once and route entries to their respective databases (caches or project DB)

        Args:
            repos_to_extract: Dict of {repo_name: (repo_path, version)}
            all_repos: List of all repo paths for type resolution
            project_db: Project database writer (for project)
            use_embeddings: Whether to use embeddings
            limit: Optional limit on number of entries to extract per repo
        """
        if not repos_to_extract:
            return

        # Create database writers for each repo
        repo_dbs = {}
        repo_paths_normalized = {}

        for repo_name, (repo_path, version) in repos_to_extract.items():
            # Project goes to project DB, Axelor repos go to their own cache
            if repo_name == 'project':
                repo_dbs[repo_name] = project_db
                print(f"\n[{repo_name.upper()}]")
                print(f"  Target: Project database")
            else:
                cache_db_path = self.repo_manager.get_cached_db_path(repo_name, version, use_embeddings)
                cache_db = StorageWriter(db_path=str(cache_db_path), use_embeddings=use_embeddings)
                cache_db.reset()
                repo_dbs[repo_name] = cache_db
                print(f"\n[{repo_name.upper()}] v{version}")
                print(f"  Cache: {cache_db_path}")

            # Normalize path for routing
            repo_paths_normalized[repo_name] = str(repo_path.resolve()).replace('\\', '/')

        # Statistics per repo
        stats = {repo_name: {'java': 0, 'xml': 0} for repo_name in repos_to_extract.keys()}
        batch_size = 500

        # Batches per repo
        java_batches = {repo_name: [] for repo_name in repos_to_extract.keys()}
        xml_batches = {repo_name: [] for repo_name in repos_to_extract.keys()}

        # Extract Java from all repos at once
        print(f"\n  Processing Java files (with {len(all_repos)} repos for resolution)...")
        try:
            java_extractor = JavaASTExtractor(repos=[str(r) for r in all_repos])
            entry_count = 0
            matched_count = 0
            for source_type, entry in java_extractor.extract_all(limit=limit):
                # Route to correct cache based on source_file
                entry_count += 1

                source_file = entry['metadata'].get('source_file', '')
                source_file_normalized = source_file.replace('\\', '/')

                # Find which repo this entry belongs to
                matched = False
                for repo_name, repo_path_norm in repo_paths_normalized.items():
                    if source_file_normalized.startswith(repo_path_norm):
                        matched = True
                        matched_count += 1
                        # Check per-repo limit
                        if limit is None or stats[repo_name]['java'] < limit:
                            java_batches[repo_name].append(entry)

                            # Flush batch if full
                            if len(java_batches[repo_name]) >= batch_size:
                                repo_dbs[repo_name].add_usages(java_batches[repo_name])
                                stats[repo_name]['java'] += len(java_batches[repo_name])
                                java_batches[repo_name] = []
                        break

                # Debug: log first unmatched entry
                if not matched and entry_count <= 3:
                    print(f"  [DEBUG] Entry {entry_count} NOT MATCHED:")
                    print(f"    source_file: {source_file_normalized[:100]}")
                    print(f"    metadata keys: {list(entry['metadata'].keys())}")
                    print(f"    Expected prefixes: {list(repo_paths_normalized.values())}")

            print(f"  [DEBUG] Java routing: {matched_count}/{entry_count} entries matched")
        except RuntimeError as e:
            print(f"    Error: {e}")
        finally:
            # Flush remaining Java batches
            for repo_name, batch in java_batches.items():
                if batch:
                    repo_dbs[repo_name].add_usages(batch)
                    stats[repo_name]['java'] += len(batch)

        # Extract XML from all repos at once
        print(f"\n  Processing XML files...")
        try:
            xml_extractor = AxelorXmlExtractor(repos=[str(r) for r in all_repos])
            entry_count = 0
            matched_count = 0
            for source_type, entry in xml_extractor.extract_all(limit=limit):
                # Route to correct cache based on source_file
                entry_count += 1

                source_file = entry['metadata'].get('source_file', '')
                source_file_normalized = source_file.replace('\\', '/')

                # Find which repo this entry belongs to
                matched = False
                for repo_name, repo_path_norm in repo_paths_normalized.items():
                    if source_file_normalized.startswith(repo_path_norm):
                        matched = True
                        matched_count += 1
                        # Check per-repo limit
                        if limit is None or stats[repo_name]['xml'] < limit:
                            xml_batches[repo_name].append(entry)

                            # Flush batch if full
                            if len(xml_batches[repo_name]) >= batch_size:
                                repo_dbs[repo_name].add_xml_references(xml_batches[repo_name])
                                stats[repo_name]['xml'] += len(xml_batches[repo_name])
                                xml_batches[repo_name] = []
                        break

            print(f"  [DEBUG] XML routing: {matched_count}/{entry_count} entries matched")
        except RuntimeError as e:
            print(f"    Error: {e}")
        finally:
            # Flush remaining XML batches
            for repo_name, batch in xml_batches.items():
                if batch:
                    repo_dbs[repo_name].add_xml_references(batch)
                    stats[repo_name]['xml'] += len(batch)

        # Debug stats per repo
        for repo_name in repos_to_extract.keys():
            java_count = stats[repo_name]['java']
            xml_count = stats[repo_name]['xml']
            total = java_count + xml_count

            if self.debug:
                # Track stats for DEBUG SUMMARY
                self.debug_stats['extracted'][repo_name] = {
                    'java': java_count,
                    'xml': xml_count,
                    'total': total
                }

                # For project, db count is tracked later; for Axelor repos, track now
                if repo_name != 'project':
                    db_count = repo_dbs[repo_name].collection.count()
                    self.debug_stats['stored'][repo_name] = db_count

                if repo_name != 'project':
                    print(f"\n  [DEBUG] {repo_name}:")
                    print(f"    Extracted: {java_count} Java + {xml_count} XML = {total} entries")
                    print(f"    Stored in DB: {db_count} entries")
                    print(f"    Ratio: {db_count / total:.2f}x" if total > 0 else "    Ratio: N/A")

            if repo_name != 'project':
                print(f"  OK - Cached {java_count} Java + {xml_count} XML entries for {repo_name}")
            else:
                print(f"  OK - Extracted {java_count} Java + {xml_count} XML entries for project")

    def _extract_repo_to_cache(self, repo_path: Path, cache_db_path: Path, all_repos: List[Path], use_embeddings: bool = False, limit: Optional[int] = None):
        """Extract a single repository to its cache database

        Args:
            repo_path: Path to repository to extract
            cache_db_path: Path where to save the cache database
            all_repos: List of all repos for type resolution
            use_embeddings: Whether to use embeddings
            limit: Optional limit on number of entries to extract per repo
        """
        print(f"\n  Extracting {repo_path.name} to cache...")

        # Create cache database
        cache_db = StorageWriter(db_path=str(cache_db_path), use_embeddings=use_embeddings)
        cache_db.reset()

        stats = {'java': 0, 'xml': 0}
        batch_size = 500

        # Normalize repo_path for filtering
        repo_path_normalized = str(repo_path.resolve()).replace('\\', '/')

        # Extract Java with all repos for type resolution, filter results
        print(f"    Processing Java files (with {len(all_repos)} repos for resolution)...")
        java_batch = []
        try:
            java_extractor = JavaASTExtractor(repos=[str(r) for r in all_repos])
            for source_type, entry in java_extractor.extract_all(limit=limit):
                # Filter: only keep entries from target repo
                source_file = entry['metadata'].get('source_file', '')
                source_file_normalized = source_file.replace('\\', '/')

                if source_file_normalized.startswith(repo_path_normalized):
                    java_batch.append(entry)
                    if len(java_batch) >= batch_size:
                        cache_db.add_usages(java_batch)
                        stats['java'] += len(java_batch)
                        java_batch = []
        except RuntimeError as e:
            print(f"    Error: {e}")
        finally:
            if java_batch:
                cache_db.add_usages(java_batch)
                stats['java'] += len(java_batch)

        # Extract XML (no type resolution needed, single repo)
        print(f"    Processing XML files...")
        xml_batch = []
        xml_extractor = AxelorXmlExtractor(repos=[str(repo_path)])
        for source_type, entry in xml_extractor.extract_all(limit=limit):
            xml_batch.append(entry)
            if len(xml_batch) >= batch_size:
                cache_db.add_xml_references(xml_batch)
                stats['xml'] += len(xml_batch)
                xml_batch = []

        if xml_batch:
            cache_db.add_xml_references(xml_batch)
            stats['xml'] += len(xml_batch)

        # Debug: Track extracted entries
        if self.debug:
            repo_name = repo_path.name
            self.debug_stats['extracted'][repo_name] = {
                'java': stats['java'],
                'xml': stats['xml'],
                'total': stats['java'] + stats['xml']
            }

            # Get actual DB count
            try:
                db_count = cache_db.collection.count()
                self.debug_stats['stored'][repo_name] = db_count

                print(f"\n  [DEBUG] {repo_name}:")
                print(f"    Extracted: {stats['java']} Java + {stats['xml']} XML = {stats['java'] + stats['xml']} entries")
                print(f"    Stored in DB: {db_count} entries")
                print(f"    Ratio: {db_count / (stats['java'] + stats['xml']):.2f}x" if stats['java'] + stats['xml'] > 0 else "    Ratio: N/A")
            except Exception as e:
                print(f"  [DEBUG] Could not get DB count: {e}")

        print(f"  OK - Cached {stats['java']} Java + {stats['xml']} XML entries")
        return stats

    def _extract_repo_into_db(self, repo_path: Path, db: StorageWriter, all_repos: List[Path], limit: Optional[int] = None):
        """Extract a repository and add to an existing database

        Args:
            repo_path: Path to repository to extract
            db: Database to add entries to
            all_repos: List of all repos for type resolution
            limit: Optional limit on number of entries to extract per repo
        """
        batch_size = 500
        stats = {'java': 0, 'xml': 0}

        # Normalize repo_path for filtering
        repo_path_normalized = str(repo_path.resolve()).replace('\\', '/')

        # Extract Java with all repos for resolution, filter results
        java_batch = []
        try:
            java_extractor = JavaASTExtractor(repos=[str(r) for r in all_repos])
            for source_type, entry in java_extractor.extract_all(limit=limit):
                # Filter: only keep entries from target repo
                source_file = entry['metadata'].get('source_file', '')
                source_file_normalized = source_file.replace('\\', '/')

                if source_file_normalized.startswith(repo_path_normalized):
                    java_batch.append(entry)
                    if len(java_batch) >= batch_size:
                        db.add_usages(java_batch)
                        stats['java'] += len(java_batch)
                        java_batch = []
        except RuntimeError as e:
            print(f"    Error: {e}")
        finally:
            if java_batch:
                db.add_usages(java_batch)
                stats['java'] += len(java_batch)

        # Extract XML (no type resolution needed, single repo)
        xml_batch = []
        xml_extractor = AxelorXmlExtractor(repos=[str(repo_path)])
        for source_type, entry in xml_extractor.extract_all(limit=limit):
            xml_batch.append(entry)
            if len(xml_batch) >= batch_size:
                db.add_xml_references(xml_batch)
                stats['xml'] += len(xml_batch)
                xml_batch = []

        if xml_batch:
            db.add_xml_references(xml_batch)
            stats['xml'] += len(xml_batch)

        # Note: Debug stats NOT tracked here to avoid duplicates
        # (already tracked when cache was created in _extract_all_repos_to_caches)

    def extract_full(self, reset: bool = True, use_embeddings: bool = False, limit: Optional[int] = None):
        """Full extraction: download Axelor repos + extract everything

        Extraction Pipeline:
        ====================
        PHASE 1: Setup & Cache Detection
            - Download Axelor repos if missing
            - Check for existing caches (platform, suite)
            - Determine what needs to be extracted

        PHASE 2: Extraction to Caches
            - Extract Axelor repos (platform, suite) to their caches
            - Extract project directly to project DB
            - Store in batches of 500 for performance

        PHASE 3: Merge Caches into Project DB
            - Copy/merge Axelor caches into project DB via copy_from_cache()
            - Much faster than re-extracting from source files
            - First cache is used as base (shutil.copytree)
            - Subsequent caches are merged (ChromaDB collection.add)

        Args:
            reset: If True, reset the database before extraction
            use_embeddings: If True, use semantic embeddings (slower)
            limit: Optional limit on number of entries to extract per repo
        """
        print("\n" + "="*60)
        print("FULL EXTRACTION")
        print("="*60)

        # Determine database name based on embeddings
        db_name = ".vector-semantic-db" if use_embeddings else ".vector-raw-db"
        project_db_path = self.project_root / db_name

        # ========================================
        # PHASE 1: Setup & Cache Detection
        # ========================================

        # 1. Ensure Axelor repos are downloaded
        axelor_repos = self.ensure_axelor_repos()

        # 3. Check if we can use cached Axelor databases
        platform_version, suite_version = self.repo_manager.detect_axelor_versions()

        cached_axelor = {}
        repos_to_extract = {}  # Will include both Axelor + project

        for repo_name, repo_path in axelor_repos.items():
            version = platform_version if repo_name == 'platform' else suite_version
            cache_db_path = self.repo_manager.get_cached_db_path(repo_name, version, use_embeddings)

            # If reset=True, delete existing cache and mark as missing
            if reset and cache_db_path.exists():
                print(f"\n[CACHE] Deleting cached DB for {repo_name} v{version} (reset mode)")
                shutil.rmtree(cache_db_path)

            # Check if cache exists (after potential deletion)
            if self.repo_manager.has_cached_db(repo_name, version, use_embeddings):
                cached_axelor[repo_name] = (repo_path, cache_db_path, version)
                print(f"\n[CACHE] Found cached DB for {repo_name} v{version}")
            else:
                repos_to_extract[repo_name] = (repo_path, version)
                print(f"\n[CACHE] No cached DB for {repo_name} v{version}, will extract")

        # 4. Add project to extraction list (always extract, no cache)
        repos_to_extract['project'] = (self.project_root, 'project')
        print(f"\n[PROJECT] Will extract local project from {self.project_root}")

        # ========================================
        # PHASE 2: Project DB Initialization
        # ========================================

        print("\n" + "="*60)
        print("Building project database...")
        print("="*60)
        print(f"Database path: {project_db_path}")
        print(f"Mode: {'Semantic (with embeddings)' if use_embeddings else 'Raw (metadata only)'}")

        # Always reset project DB (but Axelor caches are preserved in reset false mode)
        if project_db_path.exists():
            print("\n  Resetting project database...")
            shutil.rmtree(project_db_path)

        # Copy Axelor caches to project
        if cached_axelor:
            print("\n  Copying Axelor caches to project...")

            # Create empty project DB
            self.db = StorageWriter(db_path=str(project_db_path), use_embeddings=use_embeddings)

            # Merge all caches with limit
            for repo_name, (repo_path, cache_path, version) in cached_axelor.items():
                print(f"    Merging: {repo_name}")
                self.db.copy_from_cache(str(cache_path), limit=limit)

        # Initialize DB connection
        self.db = StorageWriter(db_path=str(project_db_path), use_embeddings=use_embeddings)

        # ========================================
        # PHASE 3: Extract from Source Files
        # ========================================
        # This extracts Java/XML from all repos that need extraction:
        # - Axelor repos (platform, suite) → stored in their caches
        # - Project → stored directly in project DB

        if repos_to_extract:
            print("\n" + "="*60)
            print("PHASE 3: Extracting from source files")
            print("="*60)

            # Get all repo paths for type resolution (Axelor + project)
            all_repo_paths = [repo_path for repo_name, repo_path in axelor_repos.items()]
            all_repo_paths.append(self.project_root)

            # Extract all repos at once and route to their caches/project DB
            self._extract_all_repos(repos_to_extract, all_repo_paths, self.db, use_embeddings, limit)

            # ========================================
            # PHASE 4: Merge Caches into Project DB
            # ========================================
            # Copy cache databases into project DB using copy_from_cache()
            # This is much faster than re-extracting from source files!

            for repo_name, (repo_path, version) in repos_to_extract.items():
                if repo_name != 'project':  # Only cache Axelor repos
                    cache_db_path = self.repo_manager.get_cached_db_path(repo_name, version, use_embeddings)
                    cached_axelor[repo_name] = (repo_path, cache_db_path, version)

                    # Copy newly created cache to project DB
                    if not project_db_path.exists():
                        # First cache becomes the base (simple directory copy)
                        shutil.copytree(cache_db_path, project_db_path)
                        print(f"\n  Copying {repo_name} cache to project DB (base)...")
                    else:
                        # Subsequent caches are merged by copying from cache DB (OPTIMIZED!)
                        print(f"\n  Merging {repo_name} cache into project DB...")
                        self.db.copy_from_cache(str(cache_db_path), limit=limit)

        # 8. Final summary
        print("\n" + "="*60)
        print("EXTRACTION COMPLETED")
        print("="*60)

        db_stats = self.db.get_stats()
        total_db_count = db_stats['total_usages']
        print(f"\n  Total in database: {total_db_count}")

        # Debug: Final summary with project DB count
        if self.debug:
            # Track project stored count now that extraction is complete
            if 'project' in self.debug_stats['extracted']:
                self.debug_stats['stored']['project'] = total_db_count
                # Subtract Axelor entries to get project-only count
                for repo_name, count in self.debug_stats['stored'].items():
                    if repo_name != 'project':
                        self.debug_stats['stored']['project'] -= count

            self._print_debug_summary(total_db_count)

    def _print_debug_summary(self, total_db_count: int):
        """Print detailed debug summary comparing extracted vs stored entries"""
        print("\n" + "="*60)
        print("DEBUG SUMMARY")
        print("="*60)

        # Calculate totals
        total_extracted = sum(
            repo_stats['total']
            for repo_stats in self.debug_stats['extracted'].values()
        )

        total_java = sum(
            repo_stats.get('java', 0)
            for repo_stats in self.debug_stats['extracted'].values()
        )

        total_xml = sum(
            repo_stats.get('xml', 0)
            for repo_stats in self.debug_stats['extracted'].values()
        )

        # Per-repo breakdown
        print("\nExtracted entries by repository:")
        for repo_name, stats in self.debug_stats['extracted'].items():
            java_count = stats.get('java', 0)
            xml_count = stats.get('xml', 0)
            total = stats.get('total', java_count + xml_count)
            print(f"  {repo_name:30s}: {java_count:5d} Java + {xml_count:5d} XML = {total:6d} total")

        # Totals
        print(f"\n{'TOTAL EXTRACTED':30s}: {total_java:5d} Java + {total_xml:5d} XML = {total_extracted:6d} total")
        print(f"{'TOTAL IN DATABASE':30s}: {total_db_count:6d} entries")

        # Ratio analysis
        if total_extracted > 0:
            ratio = total_db_count / total_extracted
            print(f"\nExpansion ratio: {ratio:.2f}x")
            print(f"  (Each extracted entry creates ~{ratio:.1f} database entries due to bidirectional references)")
        else:
            print("\nNo entries extracted")

    def extract_local(self, use_embeddings: bool = False, limit: Optional[int] = None):
        """Extract only local repository (remove old entries first)

        Args:
            use_embeddings: If True, use semantic embeddings (slower)
            limit: Optional limit on number of entries to extract per repo
        """
        print("\n" + "="*60)
        print("LOCAL EXTRACTION")
        print("="*60)

        # 1. Prepare local repos list
        project_dir = self.project_root / "modules"

        if not project_dir.exists():
            print(f"Error: Project directory not found: {project_dir}")
            return

        repos_to_extract = [str(project_dir)]
        print(f"\nLocal repository: {project_dir}")

        # 2. Initialize database
        db_name = ".vector-semantic-db" if use_embeddings else ".vector-raw-db"
        db_path = self.project_root / db_name
        print(f"Database path: {db_path}")
        print(f"Mode: {'Semantic (with embeddings)' if use_embeddings else 'Raw (metadata only)'}")

        self.db = StorageWriter(db_path=str(db_path), use_embeddings=use_embeddings)

        # 3. Delete local entries (TODO: need to implement this in StorageWriter)
        print("\nWarning: Selective deletion not yet implemented")
        print("For now, use full extraction with --reset")

        # 4. Extract local only
        print("\n=== Processing local files ===")

        stats = {'java': 0, 'xml': 0}
        batch_size = 500

        java_batch = []
        xml_batch = []

        def flush_batches():
            """Store accumulated batches"""
            nonlocal java_batch, xml_batch
            if java_batch:
                print(f"  Storing {len(java_batch)} Java entries...")
                self.db.add_usages(java_batch)
                stats['java'] += len(java_batch)
                java_batch = []
            if xml_batch:
                print(f"  Storing {len(xml_batch)} XML entries...")
                self.db.add_xml_references(xml_batch)
                stats['xml'] += len(xml_batch)
                xml_batch = []

        # Extract Java
        print("\n=== Processing Java files ===")
        try:
            java_extractor = JavaASTExtractor(repos=repos_to_extract)

            for source_type, entry in java_extractor.extract_all(limit=limit):
                java_batch.append(entry)

                if len(java_batch) >= batch_size:
                    flush_batches()

        except RuntimeError as e:
            print(f"Error: {e}")
        finally:
            flush_batches()

        # Extract XML
        print("\n=== Processing XML files ===")
        xml_extractor = AxelorXmlExtractor(repos=repos_to_extract)

        for source_type, entry in xml_extractor.extract_all(limit=limit):
            xml_batch.append(entry)

            if len(xml_batch) >= batch_size:
                flush_batches()

        flush_batches()

        # Summary
        print("\n" + "="*60)
        print("LOCAL EXTRACTION COMPLETED")
        print("="*60)
        print(f"  Java entries: {stats['java']}")
        print(f"  XML entries: {stats['xml']}")


def main():
    """Test the extraction manager"""
    import argparse

    parser = argparse.ArgumentParser(description="Manage call graph extraction")
    parser.add_argument("--project-root", type=str, default=".",
                       help="Project root directory")
    parser.add_argument("--full", action="store_true",
                       help="Full extraction (Axelor repos + local)")
    parser.add_argument("--local", action="store_true",
                       help="Local extraction only")
    parser.add_argument("--reset", type=str, choices=["true", "false"], default="true",
                       help="Reset database before full extraction (default: true)")
    parser.add_argument("--with-embeddings", action="store_true",
                       help="Enable semantic embeddings (slower, enables semantic search)")
    parser.add_argument("--limit", type=int, default=None,
                       help="Limit number of entries to extract per repo (for testing)")
    parser.add_argument("--debug", type=str, choices=["true", "false"], default="false",
                       help="Enable detailed extraction statistics (default: false)")

    args = parser.parse_args()

    debug = args.debug.lower() == "true"
    manager = ExtractionManager(Path(args.project_root), debug=debug)

    reset = args.reset.lower() == "true"

    if args.full:
        manager.extract_full(reset=reset, use_embeddings=args.with_embeddings, limit=args.limit)
    elif args.local:
        manager.extract_local(use_embeddings=args.with_embeddings, limit=args.limit)
    else:
        print("Please specify --full or --local")
        sys.exit(1)


if __name__ == "__main__":
    main()
