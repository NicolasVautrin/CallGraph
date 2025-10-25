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
    FILE_WORKERS = 6  # Number of parallel file extractions per repo

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
            gradle_wrapper = service_dir / ("gradlew.bat" if sys.platform == "win32" else "gradlew")

            if not gradle_wrapper.exists():
                raise FileNotFoundError(f"Gradle wrapper not found: {gradle_wrapper}")

            # Start in background (detached process)
            subprocess.Popen(
                [str(gradle_wrapper), "service"],
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

        except FileNotFoundError as e:
            raise RuntimeError(str(e))

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

    def _extract_file_task(self, java_file: Path, queue: Queue):
        """Extract entries from a single Java file and push to queue (used for parallel processing)

        Args:
            java_file: Path to Java file
            queue: Thread-safe queue to push entries to
        """
        try:
            self.extract_from_file(java_file, queue)
        except Exception as e:
            print(f"  Error extracting {java_file.name}: {e}")

    def extract_all(self, limit: Optional[int] = None) -> Iterator[Tuple[str, Dict]]:
        """Generator that yields Java entries as they are extracted (files processed in parallel)

        Args:
            limit: Optional limit on number of ENTRIES to extract per repo (None = all)

        Yields:
            Tuples of ('java', entry_dict) where entry_dict contains 'document' and 'metadata'
        """
        repos = self.repos

        if not repos:
            print("[JAVA] Warning: No repositories provided, nothing to extract")
            return

        print(f"[JAVA] Discovering Java files from {len(repos)} repositories...")

        # Step 1: Discover all Java files from all repos, organized by repo
        files_by_repo = {}
        all_files = []
        for repo in repos:
            print(f"\n[REPO] Scanning: {repo}")
            java_files = self.discover_java_files(repo)
            repo_normalized = str(Path(repo).resolve()).replace('\\', '/')
            files_by_repo[repo_normalized] = java_files
            all_files.extend(java_files)

        total_files = len(all_files)
        print(f"\n[JAVA] Found {total_files} Java files total")
        print(f"[JAVA] Extracting files in parallel (max_workers={self.FILE_WORKERS})...")

        # Step 2: Extract files in parallel using a queue with per-repo limits
        entry_queue = Queue()
        total_yielded = 0
        processed_files = 0
        import time
        start_time = time.time()

        # Track entries yielded per repo
        repo_counters = {repo: 0 for repo in files_by_repo.keys()}

        # Track file indices per repo
        repo_file_indices = {repo: 0 for repo in files_by_repo.keys()}

        with ThreadPoolExecutor(max_workers=self.FILE_WORKERS) as executor:
            # Submit initial batch of files (one per worker)
            # Workers can consume any file, we just need to track which repo each file belongs to
            futures = []
            submitted_count = 0
            initial_batch_size = min(self.FILE_WORKERS, total_files)

            # Submit first N files across all repos
            for repo, files in files_by_repo.items():
                for i in range(len(files)):
                    if submitted_count >= initial_batch_size:
                        break
                    future = executor.submit(self._extract_file_task, files[i], entry_queue)
                    futures.append(future)
                    repo_file_indices[repo] += 1
                    submitted_count += 1
                if submitted_count >= initial_batch_size:
                    break

            # Process results and submit new files dynamically
            while futures or not entry_queue.empty():
                # Check completed futures and submit new files from repos not at limit
                for future in futures[:]:
                    if future.done():
                        futures.remove(future)
                        processed_files += 1

                        # Progress indicator every 50 files
                        if processed_files % 50 == 0:
                            progress_pct = int((processed_files / total_files) * 100)
                            elapsed = time.time() - start_time
                            elapsed_minutes = int(elapsed / 60)
                            elapsed_seconds = int(elapsed % 60)
                            if processed_files > 0:
                                avg_time_per_file = elapsed / processed_files
                                remaining_files = total_files - processed_files
                                eta_seconds = avg_time_per_file * remaining_files
                                eta_minutes = int(eta_seconds / 60)
                                eta_seconds_rem = int(eta_seconds % 60)
                                print(f"[JAVA] Processed {processed_files}/{total_files} files ({progress_pct}%) - Elapsed: {elapsed_minutes}m {elapsed_seconds}s - ETA: {eta_minutes}m {eta_seconds_rem}s")

                        try:
                            future.result()  # Check for exceptions
                        except Exception as e:
                            print(f"  [ERROR] File extraction failed: {e}")

                        # Submit next file from any repo that hasn't reached its limit
                        for repo in files_by_repo.keys():
                            if limit is None or repo_counters[repo] < limit:
                                idx = repo_file_indices[repo]
                                files = files_by_repo[repo]
                                if idx < len(files):
                                    next_future = executor.submit(self._extract_file_task, files[idx], entry_queue)
                                    futures.append(next_future)
                                    repo_file_indices[repo] += 1
                                    submitted_count += 1
                                    break  # Submit only 1 file per completed file

                # Try to get entries from queue (non-blocking)
                try:
                    entry = entry_queue.get(timeout=0.1)

                    # Find which repo this entry belongs to
                    source_file = entry[1]['metadata'].get('source_file', '')
                    source_file_normalized = str(Path(source_file).resolve()).replace('\\', '/')

                    entry_repo = None
                    for repo in files_by_repo.keys():
                        if source_file_normalized.startswith(repo):
                            entry_repo = repo
                            break

                    # Check per-repo limit BEFORE yielding
                    if entry_repo and (limit is None or repo_counters[entry_repo] < limit):
                        yield entry
                        total_yielded += 1
                        repo_counters[entry_repo] += 1

                        # Log every 500 entries
                        if total_yielded % 500 == 0:
                            remaining = total_files - submitted_count
                            print(f"[JAVA] Yielded {total_yielded} entries (queue size: {entry_queue.qsize()}, {remaining} files remaining)")

                    # Check if all repos reached their limit
                    if limit is not None:
                        all_repos_done = all(
                            repo_counters[repo] >= limit or repo_file_indices[repo] >= len(files_by_repo[repo])
                            for repo in files_by_repo.keys()
                        )
                        if all_repos_done and entry_queue.empty():
                            print(f"\n[JAVA] All repos reached limit of {limit} entries, stopping...")
                            for f in futures:
                                f.cancel()
                            return

                except:
                    # Queue is empty, continue
                    pass

        print(f"\n[JAVA] Total: {total_yielded} entries from {processed_files} files")

    def extract_from_file(self, java_file: Path, queue: Optional[Queue] = None) -> Optional[List[Dict]]:
        """Extract all usages from a Java file

        Args:
            java_file: Path to Java file
            queue: Optional queue to push entries to (if provided, returns None; otherwise returns list)

        Returns:
            List of dicts with "document" and "metadata" keys (only if queue is None)
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
        for file_result in result.get('results', []):
            if file_result.get('success', False):
                file_path = file_result.get('file', '')
                entries = self._parse_usages(file_result, file_path)

                if queue is not None:
                    # Push to queue
                    for entry in entries:
                        queue.put(('java', entry))
                else:
                    # Return as list (for backward compatibility)
                    if 'all_entries' not in locals():
                        all_entries = []
                    all_entries.extend(entries)

        return all_entries if queue is None else None

    def _parse_usages(self, file_result: dict, file_path: str) -> List[Dict]:
        """Parse JavaASTService result into standardized entries

        Args:
            file_result: Result dict from JavaASTService
            file_path: Path to the source file

        Returns:
            List of dicts with "document" and "metadata" keys
        """
        entries = []

        for metadata in file_result.get('usages', []):
            # Add source_file to metadata for routing
            metadata['source_file'] = file_path
            metadata['source'] = 'java'  # Mark as Java source for filtering
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
