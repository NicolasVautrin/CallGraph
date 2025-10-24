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
from fetch_axelor_repos import AxelorRepoManager
from build_call_graph_db import CallGraphDB
from extract_java_graph import JavaCallGraphExtractor
from extract_xml_graph import AxelorXmlExtractor


class ExtractionManager:
    """Manages the full extraction pipeline"""

    def __init__(self, project_root: Path):
        """Initialize extraction manager

        Args:
            project_root: Root directory of the project
        """
        self.project_root = Path(project_root).resolve()
        self.repo_manager = AxelorRepoManager(self.project_root)
        self.db = None

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

    def _extract_repo_to_cache(self, repo_path: Path, cache_db_path: Path, all_repos: List[Path], use_embeddings: bool = False):
        """Extract a single repository to its cache database

        Args:
            repo_path: Path to repository to extract
            cache_db_path: Path where to save the cache database
            all_repos: List of all repos for type resolution
            use_embeddings: Whether to use embeddings
        """
        print(f"\n  Extracting {repo_path.name} to cache...")

        # Create cache database
        cache_db = CallGraphDB(db_path=str(cache_db_path), use_embeddings=use_embeddings)
        cache_db.reset()

        stats = {'java': 0, 'xml': 0}
        batch_size = 500

        # Normalize repo_path for filtering
        repo_path_normalized = str(repo_path.resolve()).replace('\\', '/')

        # Extract Java with all repos for type resolution, filter results
        print(f"    Processing Java files (with {len(all_repos)} repos for resolution)...")
        java_batch = []
        try:
            java_extractor = JavaCallGraphExtractor(repos=[str(r) for r in all_repos])
            for source_type, entry in java_extractor.extract_all():
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
        for source_type, entry in xml_extractor.extract_all():
            xml_batch.append(entry)
            if len(xml_batch) >= batch_size:
                cache_db.add_xml_references(xml_batch)
                stats['xml'] += len(xml_batch)
                xml_batch = []

        if xml_batch:
            cache_db.add_xml_references(xml_batch)
            stats['xml'] += len(xml_batch)

        print(f"  OK - Cached {stats['java']} Java + {stats['xml']} XML entries")
        return stats

    def _extract_repo_into_db(self, repo_path: Path, db: CallGraphDB, all_repos: List[Path]):
        """Extract a repository and add to an existing database

        Args:
            repo_path: Path to repository to extract
            db: Database to add entries to
            all_repos: List of all repos for type resolution
        """
        batch_size = 500

        # Normalize repo_path for filtering
        repo_path_normalized = str(repo_path.resolve()).replace('\\', '/')

        # Extract Java with all repos for resolution, filter results
        java_batch = []
        try:
            java_extractor = JavaCallGraphExtractor(repos=[str(r) for r in all_repos])
            for source_type, entry in java_extractor.extract_all():
                # Filter: only keep entries from target repo
                source_file = entry['metadata'].get('source_file', '')
                source_file_normalized = source_file.replace('\\', '/')

                if source_file_normalized.startswith(repo_path_normalized):
                    java_batch.append(entry)
                    if len(java_batch) >= batch_size:
                        db.add_usages(java_batch)
                        java_batch = []
        except RuntimeError as e:
            print(f"    Error: {e}")
        finally:
            if java_batch:
                db.add_usages(java_batch)

        # Extract XML (no type resolution needed, single repo)
        xml_batch = []
        xml_extractor = AxelorXmlExtractor(repos=[str(repo_path)])
        for source_type, entry in xml_extractor.extract_all():
            xml_batch.append(entry)
            if len(xml_batch) >= batch_size:
                db.add_xml_references(xml_batch)
                xml_batch = []

        if xml_batch:
            db.add_xml_references(xml_batch)

    def extract_full(self, reset: bool = True, use_embeddings: bool = False):
        """Full extraction: download Axelor repos + extract everything

        Args:
            reset: If True, reset the database before extraction
            use_embeddings: If True, use semantic embeddings (slower)
        """
        print("\n" + "="*60)
        print("FULL EXTRACTION")
        print("="*60)

        # Determine database name based on embeddings
        db_name = ".vector-semantic-db" if use_embeddings else ".vector-raw-db"
        project_db_path = self.project_root / db_name

        # 1. Ensure Axelor repos are downloaded
        axelor_repos = self.ensure_axelor_repos()

        # 2. Check if we can use cached Axelor databases
        platform_version, suite_version = self.repo_manager.detect_axelor_versions()

        cached_axelor = {}
        missing_axelor = {}

        for repo_name, repo_path in axelor_repos.items():
            version = platform_version if repo_name == 'platform' else suite_version
            if self.repo_manager.has_cached_db(repo_name, version, use_embeddings):
                cached_db = self.repo_manager.get_cached_db_path(repo_name, version, use_embeddings)
                cached_axelor[repo_name] = (repo_path, cached_db, version)
                print(f"\n[CACHE] Found cached DB for {repo_name} v{version}")
            else:
                missing_axelor[repo_name] = (repo_path, version)
                print(f"\n[CACHE] No cached DB for {repo_name} v{version}, will extract")

        # 3. Extract missing Axelor repos to their caches
        if missing_axelor:
            print("\n" + "="*60)
            print("Building Axelor caches...")
            print("="*60)

            # Get all Axelor repo paths for type resolution
            all_axelor_paths = [repo_path for repo_name, repo_path in axelor_repos.items()]

            for repo_name, (repo_path, version) in missing_axelor.items():
                cache_db_path = self.repo_manager.get_cached_db_path(repo_name, version, use_embeddings)
                print(f"\n[{repo_name.upper()}] v{version}")
                self._extract_repo_to_cache(repo_path, cache_db_path, all_axelor_paths, use_embeddings)

                # Add to cached list
                cached_axelor[repo_name] = (repo_path, cache_db_path, version)

        # 4. Initialize project database and copy Axelor caches
        print("\n" + "="*60)
        print("Building project database...")
        print("="*60)
        print(f"Database path: {project_db_path}")
        print(f"Mode: {'Semantic (with embeddings)' if use_embeddings else 'Raw (metadata only)'}")

        # Reset project DB if requested
        if reset and project_db_path.exists():
            print("\n  Resetting project database...")
            shutil.rmtree(project_db_path)

        # Copy Axelor caches to project
        if cached_axelor:
            print("\n  Copying Axelor caches to project...")

            # Copy first cache as base
            first_repo_name = list(cached_axelor.keys())[0]
            first_cache_path = cached_axelor[first_repo_name][1]

            if not project_db_path.exists():
                shutil.copytree(first_cache_path, project_db_path)
                print(f"    Base: {first_repo_name}")

            # If multiple Axelor repos, merge them
            if len(cached_axelor) > 1:
                self.db = CallGraphDB(db_path=str(project_db_path), use_embeddings=use_embeddings)

                all_axelor_paths = [repo_path for repo_name, repo_path in axelor_repos.items()]

                for repo_name, (repo_path, cache_path, version) in list(cached_axelor.items())[1:]:
                    print(f"    Merging: {repo_name}")
                    # Extract this repo and add to project DB
                    self._extract_repo_into_db(repo_path, self.db, all_axelor_paths)

        # 5. Initialize DB connection
        self.db = CallGraphDB(db_path=str(project_db_path), use_embeddings=use_embeddings)

        # 6. Extract local modules
        modules_dir = self.project_root / "modules"
        if not modules_dir.exists():
            print("\n  Warning: No modules directory found")
            print("\n" + "="*60)
            print("EXTRACTION COMPLETED (Axelor only)")
            print("="*60)
            return

        print("\n" + "="*60)
        print("Extracting local modules...")
        print("="*60)
        print(f"  Path: {modules_dir}")

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

        # Prepare all repos for type resolution (Axelor + local)
        all_repos_for_resolution = [str(repo_path) for repo_name, repo_path in axelor_repos.items()]
        all_repos_for_resolution.append(str(modules_dir))

        # Extract Java with all repos for resolution
        print("\n=== Processing Java files ===")
        print(f"  Using {len(all_repos_for_resolution)} repos for type resolution")
        try:
            java_extractor = JavaCallGraphExtractor(repos=all_repos_for_resolution)

            # Filter: only keep entries from local modules
            modules_dir_normalized = str(modules_dir.resolve()).replace('\\', '/')

            for source_type, entry in java_extractor.extract_all():
                # Filter: only keep entries from local modules
                source_file = entry['metadata'].get('source_file', '')
                source_file_normalized = source_file.replace('\\', '/')

                if source_file_normalized.startswith(modules_dir_normalized):
                    java_batch.append(entry)

                    if len(java_batch) >= batch_size:
                        flush_batches()

        except RuntimeError as e:
            print(f"Error: {e}")
        finally:
            flush_batches()

        # Extract XML
        print("\n=== Processing XML files ===")
        xml_extractor = AxelorXmlExtractor(repos=[str(modules_dir)])

        for source_type, entry in xml_extractor.extract_all():
            xml_batch.append(entry)

            if len(xml_batch) >= batch_size:
                flush_batches()

        flush_batches()

        # Summary
        print("\n" + "="*60)
        print("EXTRACTION COMPLETED")
        print("="*60)
        print(f"  Local entries: {stats['java']} Java + {stats['xml']} XML")

        db_stats = self.db.get_stats()
        print(f"\n  Total in database: {db_stats['total_usages']}")

    def extract_local(self, use_embeddings: bool = False):
        """Extract only local repository (remove old entries first)

        Args:
            use_embeddings: If True, use semantic embeddings (slower)
        """
        print("\n" + "="*60)
        print("LOCAL EXTRACTION")
        print("="*60)

        # 1. Prepare local repos list
        modules_dir = self.project_root / "modules"

        if not modules_dir.exists():
            print(f"Error: Modules directory not found: {modules_dir}")
            return

        repos_to_extract = [str(modules_dir)]
        print(f"\nLocal repository: {modules_dir}")

        # 2. Initialize database
        db_name = ".vector-semantic-db" if use_embeddings else ".vector-raw-db"
        db_path = self.project_root / db_name
        print(f"Database path: {db_path}")
        print(f"Mode: {'Semantic (with embeddings)' if use_embeddings else 'Raw (metadata only)'}")

        self.db = CallGraphDB(db_path=str(db_path), use_embeddings=use_embeddings)

        # 3. Delete local entries (TODO: need to implement this in CallGraphDB)
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
            java_extractor = JavaCallGraphExtractor(repos=repos_to_extract)

            for source_type, entry in java_extractor.extract_all():
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

        for source_type, entry in xml_extractor.extract_all():
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
    parser.add_argument("--no-reset", action="store_true",
                       help="Don't reset database before full extraction")
    parser.add_argument("--with-embeddings", action="store_true",
                       help="Enable semantic embeddings (slower, enables semantic search)")

    args = parser.parse_args()

    manager = ExtractionManager(Path(args.project_root))

    if args.full:
        manager.extract_full(reset=not args.no_reset, use_embeddings=args.with_embeddings)
    elif args.local:
        manager.extract_local(use_embeddings=args.with_embeddings)
    else:
        print("Please specify --full or --local")
        sys.exit(1)


if __name__ == "__main__":
    main()
