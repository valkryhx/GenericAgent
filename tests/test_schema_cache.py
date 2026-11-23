import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from genericagent.core.schema_cache import load_static_tool_schema


class SchemaCacheTest(unittest.TestCase):
    def test_asset_reuse_keeps_caller_mutation_out_of_cache(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'schema.json'
            path.write_text('[{"function":{"name":"original"}}]', encoding='utf-8')
            with patch('builtins.open', wraps=open) as reader:
                first = load_static_tool_schema(path, os.name)
                first[0]['function']['name'] = 'mutation'
                self.assertEqual(load_static_tool_schema(path, os.name)[0]['function']['name'], 'original')
                self.assertEqual(reader.call_count, 1)
            path.write_text('[{"function":{"name":"changed asset"}}]', encoding='utf-8')
            self.assertEqual(load_static_tool_schema(path, os.name)[0]['function']['name'], 'changed asset')

    def test_language_assets_and_platform_transforms_are_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            en, cn = (Path(root) / name for name in ('schema.json', 'schema_cn.json'))
            en.write_text('["powershell"]', encoding='utf-8')
            cn.write_text('["localized"]', encoding='utf-8')
            self.assertEqual(load_static_tool_schema(en, 'nt'), ['powershell'])
            self.assertEqual(load_static_tool_schema(en, 'posix'), ['bash'])
            self.assertEqual(load_static_tool_schema(cn, 'nt'), ['localized'])
