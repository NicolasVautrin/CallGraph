#!/usr/bin/env python3
"""
Extract references from Axelor XML files
Captures action-group hierarchies, action-method calls, field references, etc.
"""

import re
import sys
import json
from pathlib import Path
from typing import List, Dict, Optional, Set, Iterator, Tuple
from dataclasses import dataclass
import xml.etree.ElementTree as ET

# Import XSD parser for schema-driven extraction
try:
    from parse_axelor_xsd import AxelorSchemaParser
    HAS_XSD_PARSER = True
except ImportError:
    HAS_XSD_PARSER = False


@dataclass
class XmlReference:
    """Represents a reference found in XML"""
    ref_type: str  # "action_group", "action_method", "event_trigger", "field_ref", etc.
    ref_value: str  # The actual value (action name, field name, etc.)
    file_path: str
    line_number: int
    module: str  # Module name extracted from path
    context: Dict[str, str]  # Additional context (tag, attributes, etc.)


class AxelorXmlExtractor:
    """Extracts references from Axelor XML files"""

    # Default fallback lists (if XSD parsing fails)
    DEFAULT_EVENT_ATTRIBUTES = [
        'onClick', 'onChange', 'onNew', 'onLoad', 'onSave',
        'onSelect', 'onTabSelect', 'onCopy', 'onDelete', 'onInit', 'onMove'
    ]

    DEFAULT_CONDITIONAL_ATTRIBUTES = [
        'if', 'showIf', 'hideIf', 'requiredIf', 'readonlyIf',
        'validIf', 'canNew', 'canEdit', 'canSave', 'canDelete',
        'collapseIf', 'saveIf'
    ]

    DEFAULT_EXPRESSION_ATTRIBUTES = ['expr', 'domain', 'target', 'value']

    def __init__(self, xsd_path: Optional[Path] = None, view_cache_path: Optional[Path] = None,
                 repos: Optional[List[str]] = None, config_path: str = None):
        self.current_file: Optional[Path] = None
        self.current_module: str = "unknown"
        self.repos = repos  # Can be None
        self.config_path = config_path or str(Path(__file__).parent / "repos_config.json")

        # Try to load attributes from XSD schema
        self.EVENT_ATTRIBUTES = self._load_event_attributes(xsd_path)
        self.CONDITIONAL_ATTRIBUTES = self._load_conditional_attributes(xsd_path)
        self.EXPRESSION_ATTRIBUTES = self.DEFAULT_EXPRESSION_ATTRIBUTES

        # Load global view-to-model cache (for cross-module resolution)
        self.global_view_model_map = self._load_global_view_cache(view_cache_path)

    def _load_event_attributes(self, xsd_path: Optional[Path] = None) -> List[str]:
        """Load event attributes from XSD schema or use defaults"""
        if not HAS_XSD_PARSER:
            return self.DEFAULT_EVENT_ATTRIBUTES

        try:
            if xsd_path is None:
                # Try default location
                xsd_path = Path(".axelor-sources/object-views.xsd")

            if not xsd_path.exists():
                return self.DEFAULT_EVENT_ATTRIBUTES

            parser = AxelorSchemaParser(xsd_path)
            event_attrs = parser.extract_event_attributes()
            return sorted(event_attrs) if event_attrs else self.DEFAULT_EVENT_ATTRIBUTES

        except Exception as e:
            print(f"Warning: Could not parse XSD schema ({e}), using default event attributes")
            return self.DEFAULT_EVENT_ATTRIBUTES

    def _load_conditional_attributes(self, xsd_path: Optional[Path] = None) -> List[str]:
        """Load conditional attributes from XSD schema or use defaults"""
        if not HAS_XSD_PARSER:
            return self.DEFAULT_CONDITIONAL_ATTRIBUTES

        try:
            if xsd_path is None:
                xsd_path = Path(".axelor-sources/object-views.xsd")

            if not xsd_path.exists():
                return self.DEFAULT_CONDITIONAL_ATTRIBUTES

            parser = AxelorSchemaParser(xsd_path)
            cond_attrs = parser.extract_conditional_attributes()
            return sorted(cond_attrs) if cond_attrs else self.DEFAULT_CONDITIONAL_ATTRIBUTES

        except Exception as e:
            print(f"Warning: Could not parse XSD schema ({e}), using default conditional attributes")
            return self.DEFAULT_CONDITIONAL_ATTRIBUTES

    def _load_global_view_cache(self, cache_path: Optional[Path] = None) -> Dict[str, str]:
        """Load global view-to-model cache from JSON file"""
        if cache_path is None:
            cache_path = Path(".view-model-cache.json")

        if not cache_path.exists():
            return {}

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
                view_map = data.get('view_model_map', {})
                if view_map:
                    print(f"Loaded global view cache: {len(view_map)} views from {cache_path}")
                return view_map
        except Exception as e:
            print(f"Warning: Could not load view cache ({e})")
            return {}

    def discover_xml_files(self) -> List[Path]:
        """Discover all XML files from configured repositories

        Returns:
            List of XML file paths found in all configured repositories
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

        xml_files = []

        for repo in repos:
            repo_path = Path(repo)
            if not repo_path.exists():
                print(f"  [SKIP] Not found: {repo}")
                continue

            print(f"  [OK] Scanning: {repo}")

            # Discover XML files recursively
            repo_xml_files = [
                f for f in repo_path.rglob("*.xml")
                if not any(d in f.parts for d in exclude_dirs)
            ]

            xml_files.extend(repo_xml_files)
            print(f"    Found {len(repo_xml_files)} XML files")

        print(f"\nTotal: {len(xml_files)} XML files discovered")
        return xml_files

    def extract_all(self, limit: Optional[int] = None) -> Iterator[Tuple[str, Dict]]:
        """Generator that yields XML entries as they are extracted

        Args:
            limit: Optional limit on number of ENTRIES to extract (None = all)

        Yields:
            Tuples of ('xml', entry_dict) where entry_dict contains 'document' and 'metadata'
        """
        print("[XML] Discovering XML files...", flush=True)
        xml_files = self.discover_xml_files()

        if not xml_files:
            print("No XML files to process", flush=True)
            return

        total_files = len(xml_files)
        total_entries = 0
        print(f"[XML] Processing {total_files} XML files", flush=True)

        for file_num, xml_file in enumerate(xml_files, 1):
            # Check limit on entries
            if limit is not None and total_entries >= limit:
                print(f"\n[XML] Reached limit of {limit} entries")
                break

            # Progress indicator
            progress_pct = int((file_num / total_files) * 100)
            print(f"[XML] Processing {file_num}/{total_files} ({progress_pct}%) - {xml_file.name}")

            try:
                entries = self.extract_from_file(xml_file)
                print(f"  Extracted {len(entries)} entries")

                for entry in entries:
                    if limit is None or total_entries < limit:
                        yield ('xml', entry)
                        total_entries += 1
                    else:
                        break

            except Exception as e:
                print(f"  Error: {e}")

        print(f"\n[XML] Total: {total_entries} entries")

    def _extract_module_from_path(self, file_path: Path) -> str:
        """Extract module name from file path"""
        # Path pattern: modules/module-name/src/main/resources/...
        parts = file_path.parts
        try:
            if 'modules' in parts:
                idx = parts.index('modules')
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        except (ValueError, IndexError):
            pass
        return "unknown"

    def _build_view_model_map(self, root) -> dict:
        """
        Build a mapping of view names to their models.
        This is used to resolve fields in panel-related elements that reference views.

        Returns: dict[view_name, model]
        """
        view_model_map = {}

        view_tags = ['form', 'grid', 'calendar', 'gantt', 'chart', 'custom', 'kanban', 'cards']

        for element in root.iter():
            element_tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            if element_tag in view_tags:
                view_name = element.get('name')
                model = element.get('model')

                if view_name and model:
                    view_model_map[view_name] = model

        return view_model_map

    def extract_from_file(self, xml_file: Path) -> List[Dict]:
        """Extract all references from an XML file

        Returns:
            List of dicts with "document" and "metadata" keys
        """
        self.current_file = xml_file.resolve()  # Convert to absolute path
        self.current_module = self._extract_module_from_path(self.current_file)
        references = []

        try:
            # Parse XML
            tree = ET.parse(xml_file)
            root = tree.getroot()

            # Create parent map for efficient parent lookups
            self.parent_map = {child: parent for parent in root.iter() for child in parent}

            # Build view-to-model map for panel-related resolution
            self.view_model_map = self._build_view_model_map(root)

            # Extract different types of references
            references.extend(self._extract_view_and_action_definitions(root))
            references.extend(self._extract_action_groups(root))
            references.extend(self._extract_action_methods(root))
            references.extend(self._extract_event_triggers(root))
            references.extend(self._extract_view_references(root))
            references.extend(self._extract_field_references(root))
            references.extend(self._extract_viewer_references(root))
            references.extend(self._extract_expressions(root))

        except ET.ParseError as e:
            print(f"Warning: Could not parse {xml_file}: {e}")
        except Exception as e:
            print(f"Error processing {xml_file}: {e}")

        # Convert XmlReference objects to standardized format
        return self._convert_to_entries(references)

    def _convert_to_entries(self, references: List[XmlReference]) -> List[Dict]:
        """Convert XmlReference objects to standardized entry format"""
        entries = []
        for ref in references:
            # Generate document text
            doc_parts = [f"{ref.ref_type}: {ref.ref_value}"]
            if ref.module:
                doc_parts.append(f"in module {ref.module}")
            document = " ".join(doc_parts)

            # Build metadata (context as JSON string since ChromaDB only accepts primitives)
            metadata = {
                "source": "xml",
                "usageType": ref.ref_type,
                "callerUri": ref.file_path,
                "callerLine": ref.line_number,
                "calleeSymbol": ref.ref_value,
                "module": ref.module,
                "context": json.dumps(ref.context) if ref.context else ""
            }

            entries.append({
                "document": document,
                "metadata": metadata
            })

        return entries

    def _get_line_number(self, element) -> int:
        """Try to get line number from element (ElementTree doesn't provide this easily)"""
        # ElementTree doesn't track line numbers by default
        # We'll use a simple heuristic: read file and search for unique element
        return 0  # Fallback, we'll improve this if needed

    def _extract_view_and_action_definitions(self, root) -> List[XmlReference]:
        """Extract view and action definitions, including extensions"""
        references = []

        # View tags
        view_tags = ['grid', 'form', 'calendar', 'gantt', 'chart', 'custom', 'kanban', 'cards']

        for element in root.iter():
            # Skip namespaced tags
            tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            if tag_name in view_tags or tag_name.startswith('action-'):
                name = element.get('name')
                elem_id = element.get('id')
                is_extension = element.get('extension') == 'true'
                model = element.get('model', '')

                if name:
                    # Base definition or extension
                    ref_type = f"{tag_name}_extension" if is_extension else f"{tag_name}_definition"

                    context = {
                        'view_id': elem_id or name,
                        'is_extension': str(is_extension),
                        'model': model
                    }

                    # If extension, extract <extend> information
                    if is_extension:
                        extends = element.findall('.//{http://axelor.com/xml/ns/object-views}extend')
                        if not extends:
                            # Try without namespace
                            extends = element.findall('.//extend')

                        extend_targets = []
                        extend_operations = []

                        for extend in extends:
                            target = extend.get('target', '')
                            if target:
                                extend_targets.append(target)

                            # Check for insert/replace/remove children
                            for child in extend:
                                child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                                if child_tag in ['insert', 'replace', 'remove', 'attribute']:
                                    extend_operations.append(child_tag)

                        if extend_targets:
                            context['extend_targets'] = '|'.join(extend_targets)
                        if extend_operations:
                            context['extend_operations'] = ','.join(extend_operations)

                    references.append(XmlReference(
                        ref_type=ref_type,
                        ref_value=name,
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context=context
                    ))

        return references

    def _extract_action_groups(self, root) -> List[XmlReference]:
        """Extract action-group definitions and their children"""
        references = []

        # Iterate through all elements and filter by tag name (handles namespaces)
        for element in root.iter():
            tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            if tag_name == 'action-group':
                action_name = element.get('name')
                if not action_name:
                    continue

                child_actions = []
                # Find child <action> elements
                for child in element:
                    child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if child_tag == 'action':
                        child_name = child.get('name')
                        if child_name:
                            child_actions.append(child_name)

                references.append(XmlReference(
                    ref_type='action_group_definition',
                    ref_value=action_name,
                    file_path=str(self.current_file),
                    line_number=0,
                    module=self.current_module,
                    context={
                        'child_actions': ','.join(child_actions),
                        'child_count': str(len(child_actions))
                    }
                ))

                # Create bidirectional references for each child action
                for position, child_action in enumerate(child_actions):
                    # Reference 1: "Group contains action" (groupe → action)
                    # Search pattern: "Which group contains action X?" → query ref_value='X'
                    references.append(XmlReference(
                        ref_type='action_group_contains_action',
                        ref_value=child_action,  # The action being contained
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'parent_action': action_name,
                            'position': str(position)
                        }
                    ))

                    # Reference 2: "Action contained by group" (action ← groupe)
                    # Search pattern: "What actions does group X contain?" → query ref_value='X'
                    references.append(XmlReference(
                        ref_type='action_contained_by_group',
                        ref_value=action_name,  # The group containing the action
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'child_action': child_action,
                            'position': str(position)
                        }
                    ))

        return references

    def _extract_action_methods(self, root) -> List[XmlReference]:
        """Extract action-method definitions with Java class/method calls"""
        references = []

        # Iterate through all elements and filter by tag name (handles namespaces)
        for element in root.iter():
            tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            if tag_name == 'action-method':
                action_name = element.get('name')
                if not action_name:
                    continue

                # Find <call> element among children
                call = None
                for child in element.iter():
                    child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if child_tag == 'call':
                        call = child
                        break

                if call is not None:
                    java_class = call.get('class', '')
                    java_method = call.get('method', '')

                    references.append(XmlReference(
                        ref_type='action_method_definition',
                        ref_value=action_name,
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'java_class': java_class,
                            'java_method': java_method,
                            'java_fqn': f"{java_class}.{java_method}" if java_class and java_method else ''
                        }
                    ))

                    # Also create a reference for the Java method itself
                    if java_method:
                        references.append(XmlReference(
                            ref_type='xml_calls_java_method',
                            ref_value=java_method,
                            file_path=str(self.current_file),
                            line_number=0,
                            module=self.current_module,
                            context={
                                'action_name': action_name,
                                'java_class': java_class,
                                'java_fqn': f"{java_class}.{java_method}"
                            }
                        ))

        return references

    def _find_parent_view(self, element) -> tuple:
        """Find the parent form/grid/calendar and return (id, name, tag)"""
        current = self.parent_map.get(element)
        view_tags = ['form', 'grid', 'calendar', 'gantt', 'chart', 'custom', 'kanban', 'cards']

        while current is not None:
            tag_name = current.tag.split('}')[-1] if '}' in current.tag else current.tag
            if tag_name in view_tags:
                view_id = current.get('id', '')
                view_name = current.get('name', '')
                # Prefer id, fallback to name
                identifier = view_id if view_id else view_name
                return (identifier, view_name, tag_name)
            current = self.parent_map.get(current)

        return ('', '', '')

    def _find_parent_action_view(self, element) -> tuple:
        """Find the parent action-view and return (id, name, tag)"""
        current = self.parent_map.get(element)

        while current is not None:
            tag_name = current.tag.split('}')[-1] if '}' in current.tag else current.tag
            if tag_name == 'action-view':
                action_id = current.get('id', '')
                action_name = current.get('name', '')
                # Prefer name, fallback to id
                identifier = action_name if action_name else action_id
                return (identifier, action_name, tag_name)
            current = self.parent_map.get(current)

        return ('', '', '')

    def _resolve_tail_model(self, field_element) -> tuple:
        """
        Resolve the model for the tail of a dotted field.

        Two possible sources:
        1. Panel-related with grid-view/form-view (via cache)
        2. Field's own target attribute (for relational fields)

        Returns: (tail_model, resolution_source) or ('', '')
        """
        # Source 1: Field's own target attribute
        field_target = field_element.get('target')
        if field_target:
            return (field_target, 'tail_from_field_target')

        field_target_name = field_element.get('target-name')
        if field_target_name:
            return (field_target_name, 'tail_from_field_target_name')

        # Source 2: Panel-related with grid-view/form-view
        # Check the element itself first (in case it IS the panel-related)
        current = field_element
        while current is not None:
            tag_name = current.tag.split('}')[-1] if '}' in current.tag else current.tag

            if tag_name == 'panel-related':
                grid_view = current.get('grid-view')
                form_view = current.get('form-view')

                view_name = grid_view or form_view
                if view_name:
                    # Try global cache first
                    if view_name in self.global_view_model_map:
                        model = self.global_view_model_map[view_name]
                        resolution_source = 'tail_from_panel_related_grid_view' if grid_view else 'tail_from_panel_related_form_view'
                        return (model, resolution_source)
                    # Try local cache
                    elif hasattr(self, 'view_model_map') and view_name in self.view_model_map:
                        model = self.view_model_map[view_name]
                        resolution_source = 'tail_from_panel_related_grid_view' if grid_view else 'tail_from_panel_related_form_view'
                        return (model, resolution_source)

                # Stop at panel-related even if no view found
                break

            current = self.parent_map.get(current)

        return ('', '')

    def _resolve_field_model(self, field_element) -> tuple:
        """
        Resolve the model for a field by looking up the hierarchy.

        Priority order:
        1. field's target attribute (for relational fields)
        2. parent panel-related's target attribute
        3. parent editor's x-target or similar
        4. parent view's model attribute (form, grid, etc.)

        Returns: (model_fqn, resolution_source)
        """
        # 1. Check field's own target attribute
        field_target = field_element.get('target')
        if field_target:
            return (field_target, 'field_target')

        # 2. Check field's own target-name attribute
        field_target_name = field_element.get('target-name')
        if field_target_name:
            return (field_target_name, 'field_target_name')

        # 3. Walk up the hierarchy looking for model-defining elements
        current = self.parent_map.get(field_element)
        view_tags = ['form', 'grid', 'calendar', 'gantt', 'chart', 'custom', 'kanban', 'cards']

        while current is not None:
            tag_name = current.tag.split('}')[-1] if '}' in current.tag else current.tag

            # Check panel-related with target
            if tag_name == 'panel-related':
                target = current.get('target')
                if target:
                    return (target, 'panel_related')

                # If no direct target, try to resolve via grid-view or form-view attributes
                grid_view = current.get('grid-view')
                form_view = current.get('form-view')

                # Try grid-view first, then form-view
                view_name = grid_view or form_view
                if view_name:
                    # Try global cache first (cross-module resolution)
                    if view_name in self.global_view_model_map:
                        model = self.global_view_model_map[view_name]
                        resolution_source = 'panel_related_grid_view' if grid_view else 'panel_related_form_view'
                        return (model, resolution_source)
                    # Fallback to local cache (same-file resolution)
                    elif hasattr(self, 'view_model_map') and view_name in self.view_model_map:
                        model = self.view_model_map[view_name]
                        resolution_source = 'panel_related_grid_view' if grid_view else 'panel_related_form_view'
                        return (model, resolution_source)

                # panel-related without target and no resolved view - cannot resolve
                return ('', 'unresolved_panel_related')

            # Check if inside an editor - look at the editor's parent field for target
            if tag_name == 'editor':
                # The editor's parent should be a field element
                editor_parent = self.parent_map.get(current)
                if editor_parent is not None:
                    editor_parent_tag = editor_parent.tag.split('}')[-1] if '}' in editor_parent.tag else editor_parent.tag
                    if editor_parent_tag == 'field':
                        # Check for target or target-name on the parent field
                        parent_target = editor_parent.get('target')
                        if parent_target:
                            return (parent_target, 'editor_parent_field')
                        parent_target_name = editor_parent.get('target-name')
                        if parent_target_name:
                            return (parent_target_name, 'editor_parent_field')
                # Editor without target on parent field - cannot resolve, stop here
                return ('', 'unresolved_editor')

            # Check view (form/grid) with model
            if tag_name in view_tags:
                model = current.get('model')
                if model:
                    return (model, 'view')
                # If view found but no model, stop searching
                break

            current = self.parent_map.get(current)

        return ('', 'unresolved')

    def _extract_event_triggers(self, root) -> List[XmlReference]:
        """Extract event triggers (onClick, onChange, etc.)"""
        references = []

        # Search all elements for event attributes
        for element in root.iter():
            element_tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            # Get element name: prefer 'field' attribute for panel-related, otherwise 'name'
            field_name = element.get('field', '') or element.get('name', '')

            for event_attr in self.EVENT_ATTRIBUTES:
                action_ref = element.get(event_attr)
                if action_ref:
                    # Find parent view for context
                    parent_id, parent_name, parent_tag = self._find_parent_view(element)

                    # Create trigger identifier for bidirectional references
                    # Format: {parent_view_id}:{field_name}:{event_type} or fallback
                    if parent_id and field_name:
                        trigger_identifier = f"{parent_id}:{field_name}:{event_attr}"
                    elif field_name:
                        trigger_identifier = f"{field_name}:{event_attr}"
                    else:
                        trigger_identifier = f"{element_tag}:{event_attr}"

                    # Check if it contains multiple actions (separated by commas)
                    if ',' in action_ref:
                        # Treat as inline action-group
                        actions = [a.strip() for a in action_ref.split(',')]

                        # Find parent view for unique identifier
                        parent_id, parent_name, parent_tag = self._find_parent_view(element)

                        # Generate unique identifier for this inline group
                        # Format: inline-group-{parent_view_id}-{element_name}-{event_type}
                        if parent_id:
                            group_id = f"inline-group-{parent_id}-{field_name or element_tag}-{event_attr}"
                        else:
                            # Fallback if no parent view found
                            group_id = f"inline-group-{field_name or element_tag}-{event_attr}"

                        # Create an inline action-group reference
                        references.append(XmlReference(
                            ref_type='inline_action_group',
                            ref_value=group_id,
                            file_path=str(self.current_file),
                            line_number=0,
                            module=self.current_module,
                            context={
                                'event_type': event_attr,
                                'element_tag': element_tag,
                                'element_name': field_name,
                                'parent_view_id': parent_id,
                                'parent_view_name': parent_name,
                                'parent_view_tag': parent_tag,
                                'child_actions': ','.join(actions),
                                'child_count': str(len(actions)),
                                'is_inline': 'true'
                            }
                        ))

                        # Create bidirectional references for each child action
                        for position, individual_action in enumerate(actions):
                            if individual_action:
                                # Reference 1: "Inline group contains action" (groupe → action)
                                # Search pattern: "Which inline group contains action X?" → query ref_value='X'
                                references.append(XmlReference(
                                    ref_type='inline_group_contains_action',
                                    ref_value=individual_action,  # The action being contained
                                    file_path=str(self.current_file),
                                    line_number=0,
                                    module=self.current_module,
                                    context={
                                        'parent_group': group_id,
                                        'position': str(position),
                                        'event_type': event_attr,
                                        'element_name': field_name,
                                        'parent_view_id': parent_id
                                    }
                                ))

                                # Reference 2: "Action contained by inline group" (action ← groupe)
                                # Search pattern: "What actions does inline group X contain?" → query ref_value='X'
                                references.append(XmlReference(
                                    ref_type='action_contained_by_inline_group',
                                    ref_value=group_id,  # The inline group containing the action
                                    file_path=str(self.current_file),
                                    line_number=0,
                                    module=self.current_module,
                                    context={
                                        'child_action': individual_action,
                                        'position': str(position),
                                        'event_type': event_attr,
                                        'element_name': field_name,
                                        'parent_view_id': parent_id
                                    }
                                ))

                                # Check if individual action is a direct Java call
                                if ':' in individual_action:
                                    parts = individual_action.split(':')
                                    if len(parts) == 2:
                                        java_class = parts[0]
                                        java_method = parts[1]
                                        references.append(XmlReference(
                                            ref_type='xml_calls_java_method',
                                            ref_value=java_method,
                                            file_path=str(self.current_file),
                                            line_number=0,
                                            module=self.current_module,
                                            context={
                                                'event_type': event_attr,
                                                'java_class': java_class,
                                                'java_fqn': individual_action,
                                                'direct_call': 'true',
                                                'inline_group': group_id
                                            }
                                        ))
                    else:
                        # Single action - check if it's a direct Java method call
                        if ':' in action_ref:
                            parts = action_ref.split(':')
                            if len(parts) == 2:
                                java_class = parts[0]
                                java_method = parts[1]
                                references.append(XmlReference(
                                    ref_type='xml_calls_java_method',
                                    ref_value=java_method,
                                    file_path=str(self.current_file),
                                    line_number=0,
                                    module=self.current_module,
                                    context={
                                        'event_type': event_attr,
                                        'java_class': java_class,
                                        'java_fqn': action_ref,
                                        'direct_call': 'true'
                                    }
                                ))
                        else:
                            # Single action (not Java) - create bidirectional references
                            # Reference 1: "Trigger calls action" (trigger → action)
                            # Search pattern: "Which triggers call action X?" → query ref_value='X'
                            references.append(XmlReference(
                                ref_type='xml_trigger_calls_action',
                                ref_value=action_ref,  # The action being called
                                file_path=str(self.current_file),
                                line_number=0,
                                module=self.current_module,
                                context={
                                    'trigger_identifier': trigger_identifier,
                                    'event_type': event_attr,
                                    'element_tag': element_tag,
                                    'field_name': field_name,
                                    'parent_view_id': parent_id,
                                    'parent_view_name': parent_name,
                                    'parent_view_tag': parent_tag
                                }
                            ))

                            # Reference 2: "Action called by trigger" (action ← trigger)
                            # Search pattern: "Which actions does trigger X call?" → query ref_value='X'
                            references.append(XmlReference(
                                ref_type='xml_action_called_by_trigger',
                                ref_value=trigger_identifier,  # The trigger calling the action
                                file_path=str(self.current_file),
                                line_number=0,
                                module=self.current_module,
                                context={
                                    'action_name': action_ref,
                                    'event_type': event_attr,
                                    'element_tag': element_tag,
                                    'field_name': field_name,
                                    'parent_view_id': parent_id,
                                    'parent_view_name': parent_name,
                                    'parent_view_tag': parent_tag
                                }
                            ))

        return references

    def _extract_view_references(self, root) -> List[XmlReference]:
        """Extract view references from attributes (form-view, grid-view, etc.) with bidirectional references"""
        references = []

        # Attributes that reference views
        view_attributes = ['form-view', 'grid-view', 'calendar-view', 'chart-view', 'gantt-view', 'custom-view']

        for element in root.iter():
            element_name = element.get('name', '')
            element_tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            # Extract from attributes (form-view="...", grid-view="...")
            for view_attr in view_attributes:
                view_name = element.get(view_attr)
                if view_name:
                    # Find parent view for context
                    parent_id, parent_name, parent_tag = self._find_parent_view(element)

                    # Determine view type from attribute (form-view → form)
                    view_type = view_attr.replace('-view', '')

                    # Reference 1: "Element uses view" (caller → callee)
                    # Search pattern: "Who uses view X?" → query ref_value='X'
                    references.append(XmlReference(
                        ref_type='xml_element_uses_view',
                        ref_value=view_name,  # The view being used (for "who uses this view" queries)
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'element_name': element_name,
                            'element_tag': element_tag,
                            'parent_view_id': parent_id,
                            'parent_view_name': parent_name,
                            'parent_view_tag': parent_tag,
                            'attribute': view_attr,
                            'view_type': view_type
                        }
                    ))

                    # Reference 2: "View used by element" (callee → caller)
                    # Search pattern: "What views does element X use?" → query ref_value='X'
                    # Use element_name if available, otherwise use a composite identifier
                    element_identifier = element_name if element_name else f"{element_tag}@{parent_id}"

                    references.append(XmlReference(
                        ref_type='xml_view_used_by_element',
                        ref_value=element_identifier,  # The element using the view
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'view_name': view_name,
                            'element_tag': element_tag,
                            'parent_view_id': parent_id,
                            'parent_view_name': parent_name,
                            'parent_view_tag': parent_tag,
                            'attribute': view_attr,
                            'view_type': view_type
                        }
                    ))

            # Extract from <view> elements (like in Menu.xml: <view type="form" name="..."/>)
            if element_tag == 'view':
                view_type = element.get('type', '')
                view_name = element.get('name', '')
                if view_name and view_type:
                    # Find parent view/action-view for context
                    parent_id, parent_name, parent_tag = self._find_parent_action_view(element)

                    # Reference 1: "Action-view uses view" (caller → callee)
                    # Search pattern: "Who uses view X?" → query ref_value='X'
                    references.append(XmlReference(
                        ref_type='xml_action_view_uses_view',
                        ref_value=view_name,  # The view being used
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'parent_action_view': parent_id,
                            'parent_action_name': parent_name,
                            'view_type': view_type
                        }
                    ))

                    # Reference 2: "View used by action-view" (callee → caller)
                    # Search pattern: "What views does action X use?" → query ref_value='X'
                    references.append(XmlReference(
                        ref_type='xml_view_used_by_action_view',
                        ref_value=parent_id,  # The action-view using the view
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'view_name': view_name,
                            'parent_action_name': parent_name,
                            'view_type': view_type
                        }
                    ))

        return references

    def _extract_field_references(self, root) -> List[XmlReference]:
        """Extract field references with bidirectional model relationships"""
        references = []

        # Tags that represent fields
        field_tags = ['field', 'column', 'string', 'integer', 'decimal',
                     'boolean', 'date', 'datetime', 'many-to-one', 'one-to-many',
                     'many-to-many', 'one-to-one', 'binary', 'panel-related']

        for element in root.iter():
            element_tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            if element_tag in field_tags:
                # panel-related uses 'field' attribute, others use 'name'
                if element_tag == 'panel-related':
                    field_name_full = element.get('field')
                else:
                    field_name_full = element.get('name')

                if not field_name_full:
                    continue

                # Handle dotted fields (e.g., "missionHeader.description")
                # Extract the head (first part) which belongs to the resolved model
                is_dotted = '.' in field_name_full
                if is_dotted:
                    parts = field_name_full.split('.', 1)
                    field_name = parts[0]  # Head of the path (e.g., "missionHeader")
                    dotted_path = parts[1]  # Rest of the path (e.g., "description")
                else:
                    field_name = field_name_full
                    dotted_path = ''

                # Resolve the model for this field
                model_fqn, resolution_source = self._resolve_field_model(element)

                # Get parent view context
                parent_view_id, parent_view_name, parent_view_tag = self._find_parent_view(element)

                # Create bidirectional references only if we have a resolved model
                if model_fqn:
                    # Reference 1: "Field references model" (field → model)
                    # Search pattern: "Which fields reference model X?" → query ref_value='X'
                    context1 = {
                        'field_name': field_name,
                        'field_tag': element_tag,
                        'model_resolution': resolution_source,
                        'parent_view_id': parent_view_id,
                        'parent_view_name': parent_view_name,
                        'parent_view_tag': parent_view_tag,
                        'widget': element.get('widget', ''),
                        'target': element.get('target', ''),
                        'target_name': element.get('target-name', '')
                    }

                    # Add dotted field metadata for HEAD
                    if is_dotted:
                        context1['is_head_of_dotted_field'] = 'true'
                        context1['field_path_full'] = field_name_full
                        context1['dotted_path'] = dotted_path

                    references.append(XmlReference(
                        ref_type='field_references_model',
                        ref_value=model_fqn,  # The model being referenced
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context=context1
                    ))

                    # Reference 2: "Model referenced by field" (model ← field)
                    # Search pattern: "What model contains field X?" → query ref_value='X'
                    context2 = {
                        'model': model_fqn,
                        'field_tag': element_tag,
                        'model_resolution': resolution_source,
                        'parent_view_id': parent_view_id,
                        'parent_view_name': parent_view_name,
                        'parent_view_tag': parent_view_tag,
                        'widget': element.get('widget', ''),
                        'target': element.get('target', ''),
                        'target_name': element.get('target-name', '')
                    }

                    # Add dotted field metadata for HEAD
                    if is_dotted:
                        context2['is_head_of_dotted_field'] = 'true'
                        context2['field_path_full'] = field_name_full
                        context2['dotted_path'] = dotted_path

                    references.append(XmlReference(
                        ref_type='model_referenced_by_field',
                        ref_value=field_name,  # The field name
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context=context2
                    ))

                # Try to resolve the tail of dotted fields
                if is_dotted:
                    tail_model, tail_resolution_source = self._resolve_tail_model(element)

                    if tail_model:
                        # Extract the tail field name (last part after last dot)
                        tail_field_name = field_name_full.rsplit('.', 1)[-1]

                        # Reference 3: "Tail field references model" (tail → model)
                        # Search pattern: "Which fields reference model X?" → query ref_value='X'
                        context_tail_1 = {
                            'field_name': tail_field_name,
                            'field_tag': element_tag,
                            'model_resolution': tail_resolution_source,
                            'parent_view_id': parent_view_id,
                            'parent_view_name': parent_view_name,
                            'parent_view_tag': parent_view_tag,
                            'widget': element.get('widget', ''),
                            'target': element.get('target', ''),
                            'target_name': element.get('target-name', ''),
                            'is_tail_of_dotted_field': 'true',
                            'field_path_full': field_name_full,
                            'dotted_path': dotted_path
                        }

                        references.append(XmlReference(
                            ref_type='field_references_model',
                            ref_value=tail_model,  # The tail model being referenced
                            file_path=str(self.current_file),
                            line_number=0,
                            module=self.current_module,
                            context=context_tail_1
                        ))

                        # Reference 4: "Model referenced by tail field" (model ← tail)
                        # Search pattern: "In which views is field X of model Y used?" → query ref_value='X'
                        context_tail_2 = {
                            'model': tail_model,
                            'field_tag': element_tag,
                            'model_resolution': tail_resolution_source,
                            'parent_view_id': parent_view_id,
                            'parent_view_name': parent_view_name,
                            'parent_view_tag': parent_view_tag,
                            'widget': element.get('widget', ''),
                            'target': element.get('target', ''),
                            'target_name': element.get('target-name', ''),
                            'is_tail_of_dotted_field': 'true',
                            'field_path_full': field_name_full,
                            'dotted_path': dotted_path
                        }

                        references.append(XmlReference(
                            ref_type='model_referenced_by_field',
                            ref_value=tail_field_name,  # The tail field name
                            file_path=str(self.current_file),
                            line_number=0,
                            module=self.current_module,
                            context=context_tail_2
                        ))

        return references

    def _extract_viewer_references(self, root) -> List[XmlReference]:
        """Extract field references from viewer elements (JSX/HTML templates)"""
        references = []

        for element in root.iter():
            tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag

            if tag_name == 'viewer':
                # Get parent field element for context
                parent = self.parent_map.get(element)
                parent_tag = parent.tag.split('}')[-1] if parent is not None and '}' in parent.tag else (parent.tag if parent is not None else '')
                field_name = parent.get('name', '') if parent is not None else ''

                # Extract field references from CDATA content
                viewer_content = element.text if element.text else ''

                if viewer_content.strip():
                    # Extract field references from template patterns
                    field_refs = self._extract_field_refs_from_viewer(viewer_content)

                    for field_ref in field_refs:
                        references.append(XmlReference(
                            ref_type='xml_viewer_field_ref',
                            ref_value=field_ref,
                            file_path=str(self.current_file),
                            line_number=0,
                            module=self.current_module,
                            context={
                                'parent_field': field_name,
                                'parent_tag': parent_tag,
                                'viewer_content': viewer_content[:100]  # Limit length
                            }
                        ))

                # Check for depends attribute
                depends = element.get('depends')
                if depends:
                    # Multiple fields can be comma-separated
                    depend_fields = [f.strip() for f in depends.split(',')]
                    for depend_field in depend_fields:
                        if depend_field:
                            references.append(XmlReference(
                                ref_type='xml_viewer_depends',
                                ref_value=depend_field,
                                file_path=str(self.current_file),
                                line_number=0,
                                module=self.current_module,
                                context={
                                    'parent_field': field_name,
                                    'attribute': 'depends'
                                }
                            ))

        return references

    def _extract_field_refs_from_viewer(self, viewer_content: str) -> Set[str]:
        """Extract field references from viewer JSX/HTML template content"""
        field_refs = set()

        # Pattern 1: {{record.fieldName}}
        for match in re.finditer(r'\{\{record\.(\w+)\}\}', viewer_content):
            field_refs.add(match.group(1))

        # Pattern 2: {{fieldName}} (without record prefix)
        for match in re.finditer(r'\{\{(\w+)\}\}', viewer_content):
            field_name = match.group(1)
            # Exclude common keywords
            if field_name not in ['record', 'this', 'self', 'true', 'false', 'null']:
                field_refs.add(field_name)

        # Pattern 3: React-style {record.fieldName}
        for match in re.finditer(r'\{record\.(\w+)\}', viewer_content):
            field_refs.add(match.group(1))

        # Pattern 4: ng-* directives with field references
        # ng-src="{{record.fieldName}}", ng-if="record.fieldName", etc.
        for match in re.finditer(r'ng-\w+="[^"]*\{?\{?record\.(\w+)\}?\}?[^"]*"', viewer_content):
            field_refs.add(match.group(1))

        return field_refs

    def _extract_expressions(self, root) -> List[XmlReference]:
        """Extract references from expressions (domain, if, expr, etc.)"""
        references = []

        for element in root.iter():
            # Check conditional attributes
            for attr_name in self.CONDITIONAL_ATTRIBUTES + self.EXPRESSION_ATTRIBUTES:
                attr_value = element.get(attr_name)
                if attr_value:
                    # Extract field references using patterns
                    field_refs = self._extract_field_refs_from_expression(attr_value)

                    for field_ref in field_refs:
                        references.append(XmlReference(
                            ref_type='xml_expression_field_ref',
                            ref_value=field_ref,
                            file_path=str(self.current_file),
                            line_number=0,
                            module=self.current_module,
                            context={
                                'attribute': attr_name,
                                'expression': attr_value[:100],  # Limit length
                                'element_tag': element.tag
                            }
                        ))

            # Check text content (for CDATA, etc.)
            if element.text and element.text.strip():
                field_refs = self._extract_field_refs_from_expression(element.text)
                for field_ref in field_refs:
                    references.append(XmlReference(
                        ref_type='xml_script_field_ref',
                        ref_value=field_ref,
                        file_path=str(self.current_file),
                        line_number=0,
                        module=self.current_module,
                        context={
                            'element_tag': element.tag,
                            'script_type': 'groovy' if element.tag == 'action-script' else 'expression'
                        }
                    ))

        return references

    def _extract_field_refs_from_expression(self, expression: str) -> Set[str]:
        """Extract field references from an expression using patterns"""
        field_refs = set()

        # Pattern 1: self.fieldName
        for match in re.finditer(r'\bself\.(\w+)', expression):
            field_refs.add(match.group(1))

        # Pattern 2: context.fieldName
        for match in re.finditer(r'\bcontext\.(\w+)', expression):
            field_refs.add(match.group(1))

        # Pattern 3: object.fieldName (for Groovy scripts)
        for match in re.finditer(r'\b(\w+)\.(\w+)', expression):
            # Skip known prefixes
            if match.group(1) not in ['self', 'context', 'eval', 'call']:
                field_refs.add(match.group(2))

        # Pattern 4: standalone field names in simple expressions
        # Like: "fieldName == 'value'" or "fieldName IN (...)"
        for match in re.finditer(r'\b([a-z][a-zA-Z0-9]*)\s*(?:==|!=|IN|NOT|>|<|=)', expression):
            field_refs.add(match.group(1))

        return field_refs


def main():
    """Test the extractor on XML files"""
    if len(sys.argv) < 2:
        print("Usage: python extract_xml_references.py <xml-file-or-directory>")
        sys.exit(1)

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"Error: Path not found: {path}")
        sys.exit(1)

    extractor = AxelorXmlExtractor()
    all_references = []

    # Process file(s)
    if path.is_file():
        files = [path]
    else:
        files = list(path.rglob("*.xml"))

    print(f"Processing {len(files)} XML files...")

    for xml_file in files:
        try:
            references = extractor.extract_from_file(xml_file)
            all_references.extend(references)
        except Exception as e:
            print(f"Error processing {xml_file}: {e}")

    # Display statistics
    print(f"\nExtracted {len(all_references)} references:")

    by_type = {}
    for ref in all_references:
        by_type[ref.ref_type] = by_type.get(ref.ref_type, 0) + 1

    for ref_type, count in sorted(by_type.items()):
        print(f"  {ref_type}: {count}")

    # Show some examples
    print("\nExample references:")
    for ref in all_references[:15]:
        print(f"\n{ref.ref_type}: {ref.ref_value}")
        print(f"  File: {Path(ref.file_path).name}")
        if ref.context:
            for key, value in list(ref.context.items())[:3]:
                print(f"  {key}: {value}")


if __name__ == "__main__":
    main()