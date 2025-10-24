#!/usr/bin/env python3
"""
Extract call graph from Java source files using JavaASTService
Captures 8 types of Java usages including method calls, constructors, fields,
inheritance (extends/implements), and definitions
"""

import sys
import json
import requests
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Optional, Iterator, Tuple


class JavaCallGraphExtractor:
    """Extracts call graph from Java files using JavaASTService"""

    def __init__(self, service_url: str = "http://localhost:8765",
                 repos: Optional[List[str]] = None,
                 config_path: str = None):
        """Initialize extractor

        Args:
            service_url: URL of JavaASTService (default: http://localhost:8765)
            repos: List of repository paths to scan (overrides config file if provided)
            config_path: Path to repos_config.json (fallback if repos not provided)
        """
        self.service_url = service_url
        self.repos = repos  # Can be None
        self.config_path = config_path or str(Path(__file__).parent / "repos_config.json")
        self._check_service()

    def _check_service(self):
        """Verify JavaASTService is running, start it if needed"""
        try:
            response = requests.get(f"{self.service_url}/health", timeout=2)
            if response.status_code == 200:
                print("JavaASTService already running")
                return
            else:
                print(f"JavaASTService unhealthy: {response.status_code}, restarting...")
        except requests.exceptions.ConnectionError:
            print("JavaASTService not running, starting it...")

        # Start JavaASTService
        service_dir = Path(__file__).parent / "JavaASTService"

        if not service_dir.exists():
            raise RuntimeError(
                f"JavaASTService directory not found at {service_dir}\n"
                f"Cannot auto-start the service"
            )

        print(f"Starting JavaASTService from {service_dir}...")

        # Launch gradle service in background
        try:
            # Use gradlew.bat on Windows, gradlew on Unix
            gradle_cmd = "gradlew.bat" if sys.platform == "win32" else "./gradlew"

            # Start in background (detached process)
            subprocess.Popen(
                [gradle_cmd, "service"],
                cwd=str(service_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )

            # Wait for service to start (max 30 seconds)
            print("Waiting for JavaASTService to start...")
            for i in range(30):
                time.sleep(1)
                try:
                    response = requests.get(f"{self.service_url}/health", timeout=1)
                    if response.status_code == 200:
                        print(f"OK - JavaASTService started successfully after {i+1}s")
                        return
                except requests.exceptions.ConnectionError:
                    pass

            raise RuntimeError(
                "JavaASTService did not start within 30 seconds\n"
                f"Check logs in {service_dir}"
            )

        except FileNotFoundError:
            raise RuntimeError(
                f"Gradle wrapper not found in {service_dir}\n"
                f"Please ensure JavaASTService is properly set up"
            )

    def discover_java_files(self) -> List[Path]:
        """Discover all Java files from configured repositories

        Returns:
            List of Java file paths found in all configured repositories
        """
        # Use repos from constructor if provided, otherwise read from config file
        if self.repos is not None:
            repos = self.repos
        else:
            config_file = Path(self.config_path)

            if not config_file.exists():
                print(f"Warning: Config file not found: {config_file}")
                print("Using default: ['modules']")
                repos = ["modules"]
            else:
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    repos = config.get("repositories", ["modules"])
                except Exception as e:
                    print(f"Error loading config: {e}")
                    print("Using default: ['modules']")
                    repos = ["modules"]

        # Exclusions
        exclude_dirs = {'build', 'node_modules', 'dist', '.git', 'target', 'bin', '.gradle', '.settings', 'out'}

        java_files = []

        for repo in repos:
            repo_path = Path(repo)
            if not repo_path.exists():
                print(f"  [SKIP] Not found: {repo}")
                continue

            print(f"  [OK] Scanning: {repo}")

            # Discover Java files recursively
            repo_java_files = [
                f for f in repo_path.rglob("*.java")
                if not any(d in f.parts for d in exclude_dirs)
            ]

            java_files.extend(repo_java_files)
            print(f"    Found {len(repo_java_files)} Java files")

        print(f"\nTotal: {len(java_files)} Java files discovered")
        return java_files

    def extract_all(self, limit: Optional[int] = None) -> Iterator[Tuple[str, Dict]]:
        """Generator that yields Java entries as they are extracted

        Args:
            limit: Optional limit on number of ENTRIES to extract (None = all)

        Yields:
            Tuples of ('java', entry_dict) where entry_dict contains 'document' and 'metadata'
        """
        print("[JAVA] Discovering Java files...", flush=True)
        java_files = self.discover_java_files()

        if not java_files:
            print("No Java files to process", flush=True)
            return

        total_files = len(java_files)
        total_entries = 0
        print(f"[JAVA] Processing {total_files} Java files", flush=True)

        for file_num, java_file in enumerate(java_files, 1):
            # Check limit on entries
            if limit is not None and total_entries >= limit:
                print(f"\n[JAVA] Reached limit of {limit} entries")
                break

            # Progress indicator
            progress_pct = int((file_num / total_files) * 100)
            print(f"[JAVA] Processing {file_num}/{total_files} ({progress_pct}%) - {java_file.name}")

            try:
                entries = self.extract_from_file(java_file)
                print(f"  Extracted {len(entries)} entries")

                for entry in entries:
                    if limit is None or total_entries < limit:
                        yield ('java', entry)
                        total_entries += 1
                    else:
                        break

            except Exception as e:
                print(f"  Error: {e}")

        print(f"\n[JAVA] Total: {total_entries} entries")

    def extract_from_file(self, java_file: Path) -> List[Dict]:
        """Extract all usages from a Java file

        Args:
            java_file: Path to Java file

        Returns:
            List of dicts with "document" and "metadata" keys
        """
        absolute_path = str(java_file.resolve())

        # Prepare request with repos for type resolution
        request_data = {"files": [absolute_path]}

        # Add repos if provided (for type resolution cache)
        if self.repos:
            # Convert to absolute paths
            absolute_repos = [str(Path(r).resolve()) for r in self.repos]
            request_data["repos"] = absolute_repos

        # Call JavaASTService
        try:
            response = requests.post(
                f"{self.service_url}/analyze",
                json=request_data,
                timeout=30
            )
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.Timeout:
            raise RuntimeError(f"JavaASTService timeout for {java_file}")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"JavaASTService error: {e}")

        # Parse results
        if result.get('failed', 0) > 0:
            # Check if this file specifically failed
            for file_result in result.get('results', []):
                if not file_result.get('success', False):
                    errors = file_result.get('errors', ['Unknown error'])
                    raise RuntimeError(f"JavaParser failed: {errors[0]}")

        # Extract usages from successful results
        entries = []
        for file_result in result.get('results', []):
            if file_result.get('success', False):
                entries.extend(self._parse_usages(file_result))

        return entries

    def _parse_usages(self, file_result: dict) -> List[Dict]:
        """Parse JavaASTService result into standardized entries

        Returns:
            List of dicts with "document" and "metadata" keys
        """
        entries = []

        for metadata in file_result.get('usages', []):
            # Generate document text for embedding
            usage_type = metadata.get('usageType', metadata.get('usage_type', 'unknown'))
            callee_symbol = metadata.get('calleeSymbol', metadata.get('callee_name', ''))
            caller_symbol = metadata.get('callerSymbol', metadata.get('caller_method', ''))
            caller_fqn = metadata.get('caller_fqn', '')

            doc_parts = [f"{usage_type}: {callee_symbol}"]
            if caller_symbol:
                doc_parts.append(f"in {caller_symbol}()")
            if caller_fqn:
                doc_parts.append(f"at {caller_fqn}")

            document = " ".join(doc_parts)

            # Return as expected format
            entries.append({
                "document": document,
                "metadata": metadata  # Pass metadata as-is from JavaASTService
            })

        return entries


def main():
    """Test the extractor on a file or directory"""
    if len(sys.argv) < 2:
        print("Usage: python extract_java_graph.py <java-file-or-directory>")
        sys.exit(1)

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"Error: Path not found: {path}")
        sys.exit(1)

    try:
        extractor = JavaCallGraphExtractor()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    all_entries = []

    # Process file(s)
    if path.is_file():
        files = [path]
    else:
        files = list(path.rglob("*.java"))

    print(f"Processing {len(files)} Java files...")

    for java_file in files:
        try:
            entries = extractor.extract_from_file(java_file)
            all_entries.extend(entries)
            print(f"  {java_file.name}: {len(entries)} entries")
        except Exception as e:
            print(f"  Error processing {java_file}: {e}")

    # Display statistics
    print(f"\nExtracted {len(all_entries)} entries:")

    by_type = {}
    for entry in all_entries:
        metadata = entry['metadata']
        usage_type = metadata.get('usageType', metadata.get('usage_type', 'unknown'))
        by_type[usage_type] = by_type.get(usage_type, 0) + 1

    for usage_type, count in sorted(by_type.items()):
        print(f"  {usage_type}: {count}")

    # Show some examples
    print("\nExample entries:")
    for entry in all_entries[:10]:
        print(f"\nDocument: {entry['document']}")
        print(f"  Metadata: {json.dumps(entry['metadata'], indent=4)}")


if __name__ == "__main__":
    main()
