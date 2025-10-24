#!/usr/bin/env python3
"""
Wrapper script to run extraction from a specific repository
Changes to the target repo directory before running extraction
"""

import sys
import os
from pathlib import Path
import subprocess


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run extraction from a specific repository")
    parser.add_argument("repo_path", type=str,
                       help="Path to the repository (will cd to this directory)")
    parser.add_argument("--mode", type=str, choices=["full", "local"], default="full",
                       help="Extraction mode (default: full)")
    parser.add_argument("--no-reset", action="store_true",
                       help="Don't reset database before extraction")

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

    print(f"Repository: {repo_path}")
    print(f"Extraction mode: {args.mode}")
    print(f"Reset database: {not args.no_reset}")
    print()

    # Build command
    cmd = [
        sys.executable,  # Use same Python interpreter
        str(extracteurs_dir / "extraction_manager.py"),
        "--project-root", str(repo_path),
        f"--{args.mode}"
    ]

    if args.no_reset:
        cmd.append("--no-reset")

    # Run extraction with repo as working directory
    print(f"Running: {' '.join(cmd)}")
    print(f"Working directory: {repo_path}")
    print("=" * 60)
    print()

    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),  # THIS IS KEY: run from repo directory
            check=False  # Don't raise on non-zero exit
        )

        print()
        print("=" * 60)
        if result.returncode == 0:
            print("OK - Extraction completed successfully")
        else:
            print(f"ERROR - Extraction failed with exit code {result.returncode}")

        sys.exit(result.returncode)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
