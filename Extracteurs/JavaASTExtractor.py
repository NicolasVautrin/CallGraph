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
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue


class JavaASTExtractor:
    """Extracts call graph from Java files using JavaASTService"""

    # JavaASTService configuration (internal)
    SERVICE_URL = "http://localhost:8765"
    MAX_WORKERS = 4  # Number of parallel repo extraction threads

    def __init__(self, repos: List[str]):
        """Initialize extractor

        Args:
            repos: List of repository paths to scan (required)
        """
        self.repos = repos
        self._check_service()

    def _check_service(self):
        """Verify JavaASTService is running, start it if needed"""
        try:
            response = requests.get(f"{self.SERVICE_URL}/health", timeout=2)
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
                    response = requests.get(f"{self.SERVICE_URL}/health", timeout=1)
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

    def discover_java_files(self, repo: str) -> List[Path]:
        """Discover all Java files from a single repository

        Args:
            repo: Repository path to scan

        Returns:
            List of Java file paths found in the repository
        """
        # Exclusions
        exclude_dirs = {'build', 'node_modules', 'dist', '.git', 'target', 'bin', '.gradle', '.settings', 'out'}

        repo_path = Path(repo)
        if not repo_path.exists():
            print(f"  [SKIP] Not found: {repo}")
            return []

        print(f"  [OK] Scanning: {repo}")

        # Discover Java files recursively
        java_files = [
            f for f in repo_path.rglob("*.java")
            if not any(d in f.parts for d in exclude_dirs)
        ]

        print(f"    Found {len(java_files)} Java files")
        return java_files

    def _extract_repo(self, repo: str, queue: Queue, limit: Optional[int] = None):
        """Extract all Java entries from a single repository and push to queue

        Args:
            repo: Repository path
            queue: Thread-safe queue to push entries to
            limit: Optional limit on number of entries to extract from this repo
        """
        print(f"\n[REPO] Processing repository: {repo}")
        java_files = self.discover_java_files(repo)

        if not java_files:
            print(f"[REPO] No Java files found in {repo}")
            return

        total_files = len(java_files)
        entry_count = 0
        print(f"[REPO] Processing {total_files} Java files from {repo}")

        for file_num, java_file in enumerate(java_files, 1):
            # Check limit on entries
            if limit is not None and entry_count >= limit:
                print(f"\n[REPO] Reached limit of {limit} entries for {repo}")
                break

            # Progress indicator
            progress_pct = int((file_num / total_files) * 100)
            print(f"[REPO] {Path(repo).name} - {file_num}/{total_files} ({progress_pct}%) - {java_file.name}")

            try:
                file_entries = self.extract_from_file(java_file)
                print(f"  Extracted {len(file_entries)} entries")

                for entry in file_entries:
                    if limit is None or entry_count < limit:
                        queue.put(('java', entry))
                        entry_count += 1
                    else:
                        break

            except Exception as e:
                print(f"  Error: {e}")

        print(f"\n[REPO] {repo}: Extracted {entry_count} entries")

    def extract_all(self, limit: Optional[int] = None) -> Iterator[Tuple[str, Dict]]:
        """Generator that yields Java entries as they are extracted (parallelized by repo)

        Args:
            limit: Optional limit on number of ENTRIES to extract (None = all)

        Yields:
            Tuples of ('java', entry_dict) where entry_dict contains 'document' and 'metadata'
        """
        repos = self.repos

        if not repos:
            print("[JAVA] Warning: No repositories provided, nothing to extract")
            return

        print(f"[JAVA] Extracting from {len(repos)} repositories in parallel (max_workers={self.MAX_WORKERS})")

        # Create thread-safe queue for entries
        entry_queue = Queue()
        total_entries = 0
        active_threads = len(repos)

        # Launch all extraction threads
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            # Submit all repos for parallel extraction
            futures = []
            for repo in repos:
                future = executor.submit(self._extract_repo, repo, entry_queue, limit)
                futures.append(future)

            # Read from queue and yield entries as they arrive
            while active_threads > 0 or not entry_queue.empty():
                # Check if any thread has finished
                for future in futures[:]:
                    if future.done():
                        futures.remove(future)
                        active_threads -= 1
                        try:
                            future.result()  # Check for exceptions
                        except Exception as e:
                            print(f"\n[ERROR] Thread failed: {e}")

                # Try to get entries from queue (non-blocking with timeout)
                try:
                    entry = entry_queue.get(timeout=0.1)

                    # Check global limit
                    if limit is not None and total_entries >= limit:
                        print(f"\n[JAVA] Reached global limit of {limit} entries")
                        break

                    yield entry
                    total_entries += 1

                except:
                    # Queue is empty, continue checking threads
                    pass

        print(f"\n[JAVA] Total: {total_entries} entries from {len(repos)} repositories")

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
                f"{self.SERVICE_URL}/analyze",
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
