#!/usr/bin/env python3
"""
Parse Axelor XSD schema to extract event attributes and other metadata
Used to dynamically configure the XML reference extractor
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Set, Dict, List


class AxelorSchemaParser:
    """Parses Axelor XSD schema to extract metadata"""

    XSD_NS = "{http://www.w3.org/2001/XMLSchema}"

    def __init__(self, xsd_path: Path):
        self.xsd_path = xsd_path
        self.tree = ET.parse(xsd_path)
        self.root = self.tree.getroot()

    def extract_event_attributes(self) -> Set[str]:
        """Extract all event attribute names (onClick, onChange, etc.)"""
        event_attrs = set()

        # Find all xsd:attribute elements with names starting with "on"
        for attr in self.root.iter(f"{self.XSD_NS}attribute"):
            attr_name = attr.get("name", "")
            if attr_name.startswith("on"):
                # Skip attributes marked as prohibited (used in restrictions)
                use = attr.get("use", "")
                if use != "prohibited":
                    event_attrs.add(attr_name)

        return event_attrs

    def extract_view_types(self) -> Set[str]:
        """Extract all view type names (form, grid, etc.)"""
        view_types = set()

        # Look in the root object-views element definition
        for choice in self.root.iter(f"{self.XSD_NS}choice"):
            for element in choice.findall(f"{self.XSD_NS}element"):
                elem_name = element.get("name", "")
                # View types: form, grid, calendar, etc.
                if not elem_name.startswith("action-") and not elem_name.startswith("menuitem"):
                    view_types.add(elem_name)

        return view_types

    def extract_action_types(self) -> Set[str]:
        """Extract all action type names (action-group, action-method, etc.)"""
        action_types = set()

        # Look in the root object-views element definition
        for choice in self.root.iter(f"{self.XSD_NS}choice"):
            for element in choice.findall(f"{self.XSD_NS}element"):
                elem_name = element.get("name", "")
                # Action types start with "action-"
                if elem_name.startswith("action-"):
                    action_types.add(elem_name)

        return action_types

    def extract_conditional_attributes(self) -> Set[str]:
        """Extract all conditional attribute names (showIf, hideIf, etc.)"""
        conditional_attrs = set()

        # Common patterns for conditional attributes
        conditional_patterns = ["If", "domain", "target", "expr", "value"]

        for attr in self.root.iter(f"{self.XSD_NS}attribute"):
            attr_name = attr.get("name", "")

            # Check if attribute name contains conditional patterns
            if any(pattern in attr_name for pattern in conditional_patterns):
                use = attr.get("use", "")
                if use != "prohibited":
                    conditional_attrs.add(attr_name)

        return conditional_attrs

    def get_metadata(self) -> Dict[str, List[str]]:
        """Get all extracted metadata as a dictionary"""
        return {
            'event_attributes': sorted(self.extract_event_attributes()),
            'view_types': sorted(self.extract_view_types()),
            'action_types': sorted(self.extract_action_types()),
            'conditional_attributes': sorted(self.extract_conditional_attributes())
        }


def main():
    """Parse Axelor XSD and display metadata"""
    import sys
    import json

    # Default XSD path
    xsd_path = Path(".axelor-sources/object-views.xsd")

    if len(sys.argv) > 1:
        xsd_path = Path(sys.argv[1])

    if not xsd_path.exists():
        print(f"Error: XSD file not found: {xsd_path}")
        sys.exit(1)

    print(f"Parsing Axelor XSD schema: {xsd_path}\n")

    parser = AxelorSchemaParser(xsd_path)
    metadata = parser.get_metadata()

    print("=== Event Attributes ===")
    for attr in metadata['event_attributes']:
        print(f"  - {attr}")

    print(f"\n=== View Types ({len(metadata['view_types'])}) ===")
    for view_type in metadata['view_types']:
        print(f"  - {view_type}")

    print(f"\n=== Action Types ({len(metadata['action_types'])}) ===")
    for action_type in metadata['action_types']:
        print(f"  - {action_type}")

    print(f"\n=== Conditional Attributes (sample) ===")
    for attr in sorted(metadata['conditional_attributes'])[:20]:
        print(f"  - {attr}")

    # Export as JSON for programmatic use
    print("\n=== JSON Export ===")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
