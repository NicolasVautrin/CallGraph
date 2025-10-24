#!/usr/bin/env python3
"""
Setup configuration for CallGraph MCP Server
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding="utf-8") if readme_file.exists() else ""

setup(
    name="callgraph-mcp-server",
    version="1.0.0",
    description="MCP Server for analyzing Java/XML call graphs in Axelor projects",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Nicolas V",
    python_requires=">=3.8",
    packages=find_packages(include=["Extracteurs", "Extracteurs.*"]),
    install_requires=[
        "mcp>=0.9.0",
        "chromadb>=0.4.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "black>=22.0.0",
            "flake8>=4.0.0",
        ],
        "semantic": [
            "sentence-transformers>=2.2.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "callgraph-server=mcp_callgraph_server:main",
            "callgraph-extract=run_extraction:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
