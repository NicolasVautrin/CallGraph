#!/usr/bin/env python3
"""
Fetch Axelor dependencies (axelor-open-platform and axelor-open-suite)
from GitHub based on project configuration
"""

import json
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple
import re


class AxelorRepoManager:
    """Manages Axelor repository dependencies"""

    # GitHub URLs for Axelor repositories
    AXELOR_PLATFORM_URL = "https://github.com/axelor/axelor-open-platform.git"
    AXELOR_SUITE_URL = "https://github.com/axelor/axelor-open-suite.git"

    def __init__(self, project_root: Path):
        """Initialize repo manager

        Args:
            project_root: Root directory of the project
        """
        self.project_root = Path(project_root)
        # Use fixed cache directory
        self.cache_dir = Path(__file__).parent.parent / "axelor-repos"
        self.cache_dir.mkdir(exist_ok=True)

    def detect_axelor_versions(self) -> Tuple[Optional[str], Optional[str]]:
        """Detect Axelor versions used by the project

        Returns:
            Tuple of (platform_version, suite_version)
        """
        # Check build.gradle or gradle.properties
        platform_version = self._detect_platform_version()
        suite_version = self._detect_suite_version()

        return platform_version, suite_version

    def _detect_platform_version(self) -> Optional[str]:
        """Detect axelor-open-platform version from build files"""
        # Check settings.gradle (modern Gradle format)
        settings_gradle = self.project_root / "settings.gradle"
        if settings_gradle.exists():
            with open(settings_gradle, 'r', encoding='utf-8') as f:
                content = f.read()
                # Pattern: id 'com.axelor.app' version '7.2.3'
                match = re.search(r"id\s+['\"]com\.axelor\.app['\"]\s+version\s+['\"]([0-9.]+)['\"]", content)
                if match:
                    return match.group(1)

        # Check gradle.properties
        gradle_props = self.project_root / "gradle.properties"
        if gradle_props.exists():
            with open(gradle_props, 'r', encoding='utf-8') as f:
                for line in f:
                    # Pattern: axelorVersion=8.2.9
                    match = re.match(r'axelorVersion\s*=\s*([0-9.]+)', line)
                    if match:
                        return match.group(1)

        # Check build.gradle
        build_gradle = self.project_root / "build.gradle"
        if build_gradle.exists():
            with open(build_gradle, 'r', encoding='utf-8') as f:
                content = f.read()
                # Pattern: com.axelor:axelor-gradle:8.2.9
                match = re.search(r'com\.axelor:axelor-gradle:([0-9.]+)', content)
                if match:
                    return match.group(1)

        return None

    def _detect_suite_version(self) -> Optional[str]:
        """Detect axelor-open-suite version from build files"""
        # Check gradle.properties
        gradle_props = self.project_root / "gradle.properties"
        if gradle_props.exists():
            with open(gradle_props, 'r', encoding='utf-8') as f:
                for line in f:
                    # Pattern: axelorSuiteVersion=8.2.9
                    match = re.match(r'axelorSuiteVersion\s*=\s*([0-9.]+)', line)
                    if match:
                        return match.group(1)

        # Check modules dependencies
        modules_dir = self.project_root / "modules"
        if modules_dir.exists():
            for module_dir in modules_dir.iterdir():
                if module_dir.is_dir():
                    build_gradle = module_dir / "build.gradle"
                    if build_gradle.exists():
                        with open(build_gradle, 'r', encoding='utf-8') as f:
                            content = f.read()
                            # Pattern: com.axelor:axelor-suite:8.2.9
                            match = re.search(r'com\.axelor:axelor-.*:([0-9.]+)', content)
                            if match:
                                return match.group(1)

        return None

    def get_repo_path(self, repo_name: str, version: str) -> Path:
        """Get local path for a repository version

        Args:
            repo_name: Repository name ('platform' or 'suite')
            version: Version string (e.g., '8.2.9')

        Returns:
            Path to the repository directory
        """
        if repo_name == 'platform':
            return self.cache_dir / f"axelor-open-platform-{version}"
        elif repo_name == 'suite':
            return self.cache_dir / f"axelor-open-suite-{version}"
        else:
            raise ValueError(f"Unknown repo: {repo_name}")

    def is_repo_downloaded(self, repo_name: str, version: str) -> bool:
        """Check if a repository version is already downloaded

        Args:
            repo_name: Repository name ('platform' or 'suite')
            version: Version string

        Returns:
            True if already downloaded
        """
        repo_path = self.get_repo_path(repo_name, version)
        return repo_path.exists() and (repo_path / ".git").exists()

    def download_repo(self, repo_name: str, version: str) -> Path:
        """Download a specific version of an Axelor repository

        Args:
            repo_name: Repository name ('platform' or 'suite')
            version: Version string (e.g., '8.2.9')

        Returns:
            Path to the downloaded repository
        """
        if repo_name == 'platform':
            repo_url = self.AXELOR_PLATFORM_URL
        elif repo_name == 'suite':
            repo_url = self.AXELOR_SUITE_URL
        else:
            raise ValueError(f"Unknown repo: {repo_name}")

        repo_path = self.get_repo_path(repo_name, version)

        # Remove if exists but incomplete
        if repo_path.exists():
            print(f"  Removing incomplete/existing repository at {repo_path}")
            shutil.rmtree(repo_path)

        print(f"  Cloning {repo_name} v{version} from {repo_url}")

        # Clone with specific tag/branch
        tag_name = f"v{version}"

        try:
            # Try cloning specific tag
            subprocess.run(
                ["git", "clone", "--branch", tag_name, "--depth", "1", repo_url, str(repo_path)],
                check=True,
                capture_output=True,
                text=True
            )
            print(f"  OK - Successfully cloned {repo_name} v{version}")
        except subprocess.CalledProcessError as e:
            # If tag doesn't exist, try without tag and checkout manually
            print(f"  Tag {tag_name} not found, cloning full repository...")
            subprocess.run(
                ["git", "clone", repo_url, str(repo_path)],
                check=True,
                capture_output=True,
                text=True
            )

            # Try to checkout the version
            try:
                subprocess.run(
                    ["git", "checkout", tag_name],
                    cwd=str(repo_path),
                    check=True,
                    capture_output=True,
                    text=True
                )
                print(f"  OK - Checked out {repo_name} v{version}")
            except subprocess.CalledProcessError:
                print(f"  Warning: Could not checkout tag {tag_name}, using default branch")

        return repo_path

    def get_cached_db_path(self, repo_name: str, version: str, use_embeddings: bool = False) -> Path:
        """Get path to cached vector database for a repository

        Args:
            repo_name: Repository name ('platform' or 'suite')
            version: Version string
            use_embeddings: Whether to get semantic DB (True) or raw DB (False)

        Returns:
            Path to the cached database directory
        """
        repo_path = self.get_repo_path(repo_name, version)
        db_name = ".vector-semantic-db" if use_embeddings else ".vector-raw-db"
        return repo_path / db_name

    def has_cached_db(self, repo_name: str, version: str, use_embeddings: bool = False) -> bool:
        """Check if a cached vector database exists for a repository

        Args:
            repo_name: Repository name ('platform' or 'suite')
            version: Version string
            use_embeddings: Whether to check for semantic DB (True) or raw DB (False)

        Returns:
            True if cached database exists
        """
        db_path = self.get_cached_db_path(repo_name, version, use_embeddings)
        # Check if directory exists and contains ChromaDB files
        return db_path.exists() and (db_path / "chroma.sqlite3").exists()

    def ensure_repos(self, platform_version: Optional[str] = None,
                     suite_version: Optional[str] = None) -> Dict[str, Path]:
        """Ensure Axelor repositories are downloaded

        Args:
            platform_version: Platform version (auto-detect if None)
            suite_version: Suite version (auto-detect if None)

        Returns:
            Dictionary mapping repo names to paths
        """
        # Auto-detect versions if not provided
        if platform_version is None or suite_version is None:
            detected_platform, detected_suite = self.detect_axelor_versions()
            platform_version = platform_version or detected_platform
            suite_version = suite_version or detected_suite

        repos = {}

        # Download platform if version detected
        if platform_version:
            print(f"\nAxelor Open Platform v{platform_version}:")
            if self.is_repo_downloaded('platform', platform_version):
                print(f"  OK - Already downloaded")
            else:
                self.download_repo('platform', platform_version)

            repos['platform'] = self.get_repo_path('platform', platform_version)

        # Download suite if version detected
        if suite_version:
            print(f"\nAxelor Open Suite v{suite_version}:")
            if self.is_repo_downloaded('suite', suite_version):
                print(f"  OK - Already downloaded")
            else:
                self.download_repo('suite', suite_version)

            repos['suite'] = self.get_repo_path('suite', suite_version)

        return repos
