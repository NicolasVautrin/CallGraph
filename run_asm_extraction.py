#!/usr/bin/env python3
"""
ASM-based CallGraph Extraction Tool

Usage:
    python run_asm_extraction.py C:/Users/nicolasv/Bricklead_Encheres --index
"""

import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Force unbuffered output for real-time logging
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='ASM-based CallGraph Extraction Tool')
    parser.add_argument('repo_path', type=str,
                       help='Path to the repository')
    parser.add_argument('--init', action='store_true',
                       help='Full reset: clear all tables and rebuild from scratch (ignores cache)')
    parser.add_argument('--limit', type=int, default=None,
                       help='Limit extraction to N files per package (for testing, requires --init)')
    parser.add_argument('--log', type=str, default=None,
                       help='Path to log file (default: asm_extraction_YYYYMMDD_HHMMSS.log)')

    args = parser.parse_args()

    # Validate arguments
    if args.limit and not args.init:
        print("Error: --limit can only be used with --init (to avoid partial data in incremental mode)")
        print("Usage: python run_asm_extraction.py <repo_path> --init --limit <N>")
        sys.exit(1)

    # Resolve paths
    repo_path = Path(args.repo_path).resolve()
    extracteurs_dir = Path(__file__).parent / "Extracteurs"

    # Validate repo exists
    if not repo_path.exists():
        print(f"Error: Repository not found: {repo_path}")
        sys.exit(1)

    if not repo_path.is_dir():
        print(f"Error: Not a directory: {repo_path}")
        sys.exit(1)

    # Setup log file
    if args.log:
        log_file = Path(args.log)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = Path.cwd() / f"asm_extraction_{timestamp}.log"

    # Open log file for writing
    log_handle = open(log_file, 'w', buffering=1)  # Line buffered

    def log_print(msg):
        """Print to both console and log file"""
        print(msg)
        print(msg, file=log_handle, flush=True)

    log_print("=" * 60)
    log_print("ASM CallGraph Extraction Log")
    log_print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_print(f"Log file: {log_file}")
    log_print("=" * 60)
    log_print(f"Repository: {repo_path}")
    log_print(f"Mode: {'INIT (full reset)' if args.init else 'INCREMENTAL (cache enabled)'}")

    if args.limit:
        log_print(f"Limit: {args.limit} files per package")
    log_print("")

    # Step 1: Always run GradleDependencyManager to discover packages
    log_print("[STEP 1] Discovering packages via GradleDependencyManager...")
    log_print("-" * 60)
    sys.path.insert(0, str(extracteurs_dir))
    from GradleDependencyManager import GradleDependencyManager

    dep_manager = GradleDependencyManager(str(repo_path))
    deps = dep_manager.get_dependencies()

    packages = deps['packages']
    local_packages = []

    # Identify local packages (packages without sources JAR available)
    # External Axelor packages have sources in Maven, local ones don't
    for pkg in packages:
        sources_path = pkg.get('sources')
        # No sources available OR sources directory doesn't exist
        if not sources_path or not Path(sources_path).exists():
            local_packages.append(pkg['name'])

    log_print(f"Found {len(packages)} packages total")
    if local_packages:
        log_print(f"  -> Including {len(local_packages)} local packages: {local_packages}")
    log_print("")

    # Step 2: Initialize database and build symbol index
    db_path = repo_path / ".callgraph.db"
    log_print("")
    log_print("[STEP 2] Building symbol index...")
    log_print("-" * 60)

    from ASMExtractor import ASMExtractor

    # Initialize extractor with appropriate mode
    if args.init:
        log_print("INIT mode: Full database reset...")
        extractor = ASMExtractor(db_path=str(db_path), init=True)
    else:
        log_print("INCREMENTAL mode: Using cache (will auto-clean modified packages)")
        extractor = ASMExtractor(db_path=str(db_path))  # init=False by default

    # Build index from packages
    # Gradle puts LOCAL packages first, then dependencies (base packages last)
    # We need the REVERSE order for indexing: base packages first, locals last
    axelor_repos_dir = dep_manager.axelor_repos_dir
    package_names = [pkg['name'] for pkg in reversed(packages)]

    extractor.build_symbol_index(
        axelor_repos_dir=str(axelor_repos_dir),
        packages=package_names,
        domains=["com.axelor"],
        project_root=str(repo_path),
        local_packages=local_packages
    )

    log_print("Symbol index complete")

    # Step 3: Extract call graph from all packages
    log_print("")
    log_print("[STEP 3] Extracting call graph...")
    log_print("-" * 60)

    # Build rootPackages list (paths to classes/ directories)
    root_packages = []
    for pkg in packages:
        # Use the 'classes' path provided by GradleDependencyManager
        pkg_classes = Path(pkg['classes'])
        if pkg_classes.exists():
            root_packages.append({
                'name': pkg['name'],
                'path': str(pkg_classes.resolve()).replace('\\', '/')
            })

    log_print(f"Extracting {len(root_packages)} packages...")

    try:
        result = extractor.extract(
            root_packages=root_packages,
            project_root=str(repo_path),
            domains=["com.axelor"],
            limit=args.limit
        )
        log_print(f"  -> Extracted {result.get('stats', {}).get('total_classes', 0)} classes")
    except Exception as e:
        log_print(f"  -> ERROR: {e}")

    extractor.close()

    log_print("")
    log_print("=" * 60)
    log_print("EXTRACTION COMPLETE")
    log_print("=" * 60)
    log_print(f"Database: {db_path}")
    log_print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_handle.close()


if __name__ == "__main__":
    main()
