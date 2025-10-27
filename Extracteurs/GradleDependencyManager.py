"""
GradleDependencyManager - Query Gradle for dependencies and source locations

This module interrogates a Gradle project to:
1. Get all runtime JAR dependencies (compiled classes)
2. Find or download corresponding source JARs
3. Extract sources to a local cache directory

Usage:
    manager = GradleDependencyManager(project_root="/path/to/project")
    deps = manager.get_dependencies()
    # Returns: {
    #   "jars": ["/path/to/lib.jar", ...],
    #   "sources": ["/path/to/extracted/sources", ...]
    # }
"""

import subprocess
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import zipfile
import tempfile
import shutil


class GradleDependencyManager:
    """Manages Gradle dependencies for ASM-based extraction"""

    def __init__(self, project_root: str):
        """
        Initialize dependency manager

        Args:
            project_root: Path to the Gradle project root
        """
        self.project_root = Path(project_root).resolve()

        # Cache in axelor-repos/ (one directory per package)
        script_dir = Path(__file__).parent
        self.axelor_repos_dir = script_dir.parent / "axelor-repos"
        self.axelor_repos_dir.mkdir(exist_ok=True)

        # Detect gradlew
        self.gradlew = self._find_gradlew()

    def _find_gradlew(self) -> Path:
        """Find gradlew or gradlew.bat"""
        if (self.project_root / "gradlew.bat").exists():
            return self.project_root / "gradlew.bat"
        elif (self.project_root / "gradlew").exists():
            return self.project_root / "gradlew"
        else:
            raise FileNotFoundError(f"No gradlew found in {self.project_root}")

    def get_dependencies(self) -> Dict[str, List[str]]:
        """
        Get Axelor dependencies with their JARs and sources

        Returns:
            {
                "packages": [
                    {
                        "name": "axelor-core-7.2.3",
                        "group": "com.axelor",
                        "artifact": "axelor-core",
                        "version": "7.2.3",
                        "jar": "/path/to/axelor-core-7.2.3.jar",
                        "sources": "/path/to/axelor-repos/axelor-core-7.2.3/sources",
                        "classes": "/path/to/axelor-repos/axelor-core-7.2.3/classes"
                    }
                ],
                "classpath": ["/path/to/build/classes", ...]
            }
        """
        print(f"[GRADLE] Querying Axelor dependencies from {self.project_root}")

        # Get JAR dependencies (Axelor only)
        jars = self._get_runtime_jars()
        print(f"[GRADLE] Found {len(jars)} Axelor JAR dependencies")

        # Process each Axelor package
        packages = []
        for jar_info in jars:
            package_data = self._process_axelor_package(jar_info)
            if package_data:
                packages.append(package_data)

        print(f"[GRADLE] Processed {len(packages)} Axelor packages")

        # Get project's build output directories
        classpath = self._get_build_dirs()

        return {
            "packages": packages,
            "classpath": classpath
        }

    def _get_runtime_jars(self) -> List[Dict[str, str]]:
        """
        Query Gradle for runtime JAR dependencies (Axelor only)

        Uses a custom Gradle script to extract resolved artifact paths

        Returns:
            List of {group, artifact, version, jar_path}
        """
        # Path to our custom Gradle script
        script_dir = Path(__file__).parent
        gradle_script = script_dir / "list-dependencies.gradle"

        if not gradle_script.exists():
            print(f"[GRADLE] Error: {gradle_script} not found")
            return []

        cmd = [
            str(self.gradlew),
            "--init-script", str(gradle_script),
            "listAxelorDeps",
            "--console=plain"
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode != 0:
                print(f"[GRADLE] Warning: listAxelorDeps failed: {result.stderr}")
                return []

            # Parse output lines with format: AXELOR_DEP|group|artifact|version|jar_path
            jars = self._parse_gradle_output(result.stdout)
            return jars

        except subprocess.TimeoutExpired:
            print("[GRADLE] Timeout querying dependencies")
            return []
        except Exception as e:
            print(f"[GRADLE] Error querying dependencies: {e}")
            return []

    def _parse_gradle_output(self, output: str) -> List[Dict[str, str]]:
        """
        Parse custom Gradle script output

        Format: AXELOR_DEP|group|artifact|version|jar_path|sources_path

        Returns:
            List of {group, artifact, version, jar_path, sources_path}
        """
        jars = []
        seen = set()  # Avoid duplicates

        for line in output.splitlines():
            line = line.strip()

            if not line.startswith("AXELOR_DEP|"):
                continue

            # Parse: AXELOR_DEP|group|artifact|version|jar_path|sources_path
            parts = line.split("|")
            if len(parts) != 6:
                continue

            _, group, artifact, version, jar_path, sources_path = parts

            # Avoid duplicates
            key = f"{group}:{artifact}:{version}"
            if key in seen:
                continue
            seen.add(key)

            jars.append({
                "group": group,
                "artifact": artifact,
                "version": version,
                "jar_path": jar_path,
                "sources_path": sources_path if sources_path != "NONE" else None
            })

        return jars

    def _find_jar_in_cache(self, group: str, name: str, version: str) -> Optional[Path]:
        """
        Find a JAR in Gradle's local cache

        Gradle cache structure:
        ~/.gradle/caches/modules-2/files-2.1/GROUP/NAME/VERSION/HASH/name-version.jar
        """
        gradle_home = Path.home() / ".gradle"
        cache_base = gradle_home / "caches" / "modules-2" / "files-2.1"

        # Navigate to group/name/version
        group_path = group.replace(".", "/")
        dep_dir = cache_base / group_path / name / version

        if not dep_dir.exists():
            return None

        # Find the JAR file (usually in a hash subdirectory)
        jar_name = f"{name}-{version}.jar"

        # Search for the JAR
        for hash_dir in dep_dir.iterdir():
            if hash_dir.is_dir():
                jar_file = hash_dir / jar_name
                if jar_file.exists():
                    return jar_file

        return None

    def _process_axelor_package(self, jar_info: Dict[str, str]) -> Optional[Dict[str, str]]:
        """
        Process an Axelor package: extract sources and classes to axelor-repos/

        Args:
            jar_info: {group, artifact, version, jar_path, sources_path}

        Returns:
            {
                name: "axelor-core-7.2.3",
                group: "com.axelor",
                artifact: "axelor-core",
                version: "7.2.3",
                jar: "/path/to/jar",
                sources: "/path/to/axelor-repos/axelor-core-7.2.3/sources",
                classes: "/path/to/axelor-repos/axelor-core-7.2.3/classes"
            }
        """
        artifact = jar_info["artifact"]
        version = jar_info["version"]
        jar_path = Path(jar_info["jar_path"])
        sources_jar_path = Path(jar_info["sources_path"]) if jar_info.get("sources_path") else None

        # Package directory: axelor-repos/axelor-core-7.2.3/
        package_name = f"{artifact}-{version}"
        package_dir = self.axelor_repos_dir / package_name
        sources_dir = package_dir / "sources"
        classes_dir = package_dir / "classes"

        # Already processed?
        if sources_dir.exists() and classes_dir.exists():
            return {
                "name": package_name,
                "group": jar_info["group"],
                "artifact": artifact,
                "version": version,
                "jar": str(jar_path),
                "sources": str(sources_dir),
                "classes": str(classes_dir)
            }

        print(f"[GRADLE] Processing package: {package_name}")

        # Extract sources (if available from Gradle)
        sources_extracted = False
        if sources_jar_path and sources_jar_path.exists():
            sources_extracted = self._extract_jar_to_dir(sources_jar_path, sources_dir, "*.java")
        else:
            print(f"[GRADLE]   No sources available for {package_name}")

        # Extract classes (from JAR)
        classes_extracted = self._extract_jar_to_dir(jar_path, classes_dir, "*.class")

        if not sources_extracted and not classes_extracted:
            print(f"[GRADLE] Failed to extract {package_name}")
            return None

        return {
            "name": package_name,
            "group": jar_info["group"],
            "artifact": artifact,
            "version": version,
            "jar": str(jar_path),
            "sources": str(sources_dir) if sources_extracted else None,
            "classes": str(classes_dir) if classes_extracted else None
        }

    def _extract_jar_to_dir(self, jar_path: Path, target_dir: Path, file_pattern: str) -> bool:
        """
        Extract files matching pattern from JAR to target directory

        Args:
            jar_path: Path to the JAR file
            target_dir: Directory to extract to
            file_pattern: Pattern like "*.java" or "*.class"

        Returns:
            True if extraction successful
        """
        # Check if already extracted
        extension = file_pattern.replace("*", "")
        if target_dir.exists() and list(target_dir.rglob(file_pattern)):
            return True

        try:
            print(f"[GRADLE]   Extracting {file_pattern} from {jar_path.name}")
            target_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(jar_path, 'r') as zip_ref:
                # Extract only matching files
                for member in zip_ref.namelist():
                    if member.endswith(extension):
                        zip_ref.extract(member, target_dir)

            return True

        except Exception as e:
            print(f"[GRADLE]   Failed to extract {file_pattern}: {e}")
            return False

    def _find_sources_jar(self, jar_path: Path) -> Optional[Path]:
        """
        Find the -sources.jar for a given JAR in Gradle cache

        Gradle cache structure:
        ~/.gradle/caches/modules-2/files-2.1/GROUP/ARTIFACT/VERSION/
        ├── HASH1/artifact-version.jar
        └── HASH2/artifact-version-sources.jar
        """
        jar_name = jar_path.name  # e.g., "axelor-core-7.2.6.jar"
        sources_name = jar_name.replace(".jar", "-sources.jar")

        # Check same directory first
        sources_jar = jar_path.parent / sources_name
        if sources_jar.exists():
            return sources_jar

        # Search in sibling hash directories (Gradle cache structure)
        # Parent = hash dir, parent.parent = version dir
        version_dir = jar_path.parent.parent
        if version_dir.exists() and version_dir.is_dir():
            for hash_dir in version_dir.iterdir():
                if hash_dir.is_dir():
                    candidate = hash_dir / sources_name
                    if candidate.exists():
                        return candidate

        return None

    def _get_build_dirs(self) -> List[str]:
        """
        Get project's compiled class directories

        Returns paths like:
        - build/classes/java/main
        - build/resources/main
        - modules/*/build/classes/java/main
        """
        classpath = []

        # Main project build dir
        main_build = self.project_root / "build" / "classes" / "java" / "main"
        if main_build.exists():
            classpath.append(str(main_build))

        # Modules
        modules_dir = self.project_root / "modules"
        if modules_dir.exists():
            for module in modules_dir.iterdir():
                if module.is_dir():
                    module_build = module / "build" / "classes" / "java" / "main"
                    if module_build.exists():
                        classpath.append(str(module_build))

        return classpath

    def download_axelor_sources(self, version: str, repo_name: str = "axelor-open-platform") -> Optional[Path]:
        """
        Download Axelor sources from Maven Central if not in Gradle cache

        Args:
            version: Axelor version (e.g., "7.2.3")
            repo_name: Repository name (axelor-open-platform or axelor-open-suite)

        Returns:
            Path to extracted sources or None
        """
        maven_url = (
            f"https://repo1.maven.org/maven2/com/axelor/{repo_name}/"
            f"{version}/{repo_name}-{version}-sources.jar"
        )

        cache_key = f"{repo_name}-{version}"
        source_dir = self.cache_dir / cache_key

        # Already downloaded?
        if source_dir.exists() and list(source_dir.rglob("*.java")):
            return source_dir

        print(f"[GRADLE] Downloading {repo_name} sources from Maven Central...")

        try:
            import urllib.request

            temp_jar = self.cache_dir / f"{cache_key}-sources.jar"
            urllib.request.urlretrieve(maven_url, temp_jar)

            # Extract
            source_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(temp_jar, 'r') as zip_ref:
                for member in zip_ref.namelist():
                    if member.endswith('.java'):
                        zip_ref.extract(member, source_dir)

            # Cleanup temp JAR
            temp_jar.unlink()

            print(f"[GRADLE] Extracted {repo_name} sources to {source_dir}")
            return source_dir

        except Exception as e:
            print(f"[GRADLE] Failed to download {repo_name} sources: {e}")
            return None


def main():
    """Test the dependency manager"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python GradleDependencyManager.py /path/to/project")
        sys.exit(1)

    project_root = sys.argv[1]

    manager = GradleDependencyManager(project_root)
    deps = manager.get_dependencies()

    print("\n=== AXELOR PACKAGES ===")
    print(f"\nFound {len(deps['packages'])} Axelor packages:")
    for pkg in deps['packages']:
        print(f"\n  [PKG] {pkg['name']}")
        print(f"        Group: {pkg['group']}")
        print(f"        Artifact: {pkg['artifact']}")
        print(f"        Version: {pkg['version']}")
        print(f"        JAR: {pkg['jar']}")
        if pkg.get('sources'):
            print(f"        Sources: {pkg['sources']}")
        if pkg.get('classes'):
            print(f"        Classes: {pkg['classes']}")

    print(f"\n=== PROJECT CLASSPATH ===")
    print(f"\nFound {len(deps['classpath'])} build directories:")
    for cp in deps['classpath']:
        print(f"  - {cp}")


if __name__ == "__main__":
    main()
