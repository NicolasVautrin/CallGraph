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
    parser.add_argument('--index', action='store_true',
                       help='Build symbol index only (skip extraction)')
    parser.add_argument('--reset', action='store_true',
                       help='Clear nodes and edges tables before extraction')
    parser.add_argument('--limit', type=int, default=None,
                       help='Limit extraction to N files per package')
    parser.add_argument('--log', type=str, default=None,
                       help='Path to log file (default: asm_extraction_YYYYMMDD_HHMMSS.log)')

    args = parser.parse_args()

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
    log_print(f"Mode: {'INDEX ONLY' if args.index else 'INDEX + EXTRACT'}")

    if not args.index:
        if args.reset:
            log_print("Reset: Clear nodes and edges tables")
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

    axelor_packages = deps['packages']
    local_packages = []

    # Identify local packages
    modules_dir = repo_path / "modules"
    if modules_dir.exists():
        for module in modules_dir.iterdir():
            if module.is_dir():
                module_name = module.name
                for pkg in axelor_packages:
                    if pkg['name'].startswith(module_name + '-'):
                        local_packages.append(pkg['name'])
                        break

    log_print(f"Found {len(axelor_packages)} Axelor packages")
    log_print(f"Found {len(local_packages)} local packages: {local_packages}")
    log_print("")

    # Step 2: Build index (only if --index flag)
    db_path = repo_path / ".callgraph.db"

    if args.index:
        log_print("")
        log_print("[STEP 2] Building symbol index...")
        log_print("-" * 60)
        from ASMExtractor import ASMExtractor

        extractor = ASMExtractor(db_path=str(db_path))

        # Clear symbol index
        cursor = extractor.conn.cursor()
        cursor.execute("DELETE FROM symbol_index")
        cursor.execute("DELETE FROM index_metadata")
        extractor.conn.commit()
        log_print("Symbol index cleared")

        # Build index from packages
        axelor_repos_dir = dep_manager.axelor_repos_dir
        package_names = [pkg['name'] for pkg in axelor_packages]
        extractor.build_symbol_index(
            axelor_repos_dir=str(axelor_repos_dir),
            packages=package_names,
            domains=["com.axelor"],
            project_root=str(repo_path),
            local_packages=local_packages
        )
        extractor.close()

        log_print("Symbol index complete")

    # If --index only, stop here
    if args.index:
        log_print("")
        log_print("=" * 60)
        log_print("INDEX COMPLETE")
        log_print("=" * 60)
        log_print(f"Database: {db_path}")
        log_print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_handle.close()
        return

    # Step 3: Extract call graph from all packages
    log_print("")
    log_print("[STEP 3] Extracting call graph...")
    log_print("-" * 60)

    from ASMExtractor import ASMExtractor
    extractor = ASMExtractor(db_path=str(db_path))

    # Reset nodes and edges if requested
    if args.reset:
        log_print("Clearing nodes and edges tables...")
        cursor = extractor.conn.cursor()
        cursor.execute("DELETE FROM nodes")
        cursor.execute("DELETE FROM edges")
        extractor.conn.commit()

    # Build rootPackages list (paths to classes/ directories)
    axelor_repos = Path(dep_manager.axelor_repos_dir)
    root_packages = []
    for pkg in axelor_packages:
        pkg_classes = axelor_repos / pkg['name'] / 'classes'
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
