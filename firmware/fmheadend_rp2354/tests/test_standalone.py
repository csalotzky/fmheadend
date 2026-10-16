"""Offline project-boundary and VS Code task checks. Never access hardware."""
import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StandaloneProject(unittest.TestCase):
    def test_local_task_entry_points(self):
        tasks = json.loads((ROOT / '.vscode/tasks.json').read_text())['tasks']
        for label in ('Compile Project', 'Flash RP2354 Factory (SWD)'):
            task = next(t for t in tasks if t['label'] == label)
            script = task['args'][task['args'].index('-File') + 1]
            path = Path(script.replace('${workspaceFolder}', str(ROOT))).resolve()
            self.assertTrue(path.is_relative_to(ROOT))
            self.assertTrue(path.is_file())
        flash = next(t for t in tasks if t['label'] == 'Flash')
        self.assertEqual(flash['dependsOn'], 'Flash RP2354 Factory (SWD)')

    def test_debug_never_loads_flat_image(self):
        configs = json.loads((ROOT / '.vscode/launch.json').read_text())['configurations']
        for config in configs:
            self.assertEqual(config['request'], 'attach')
            self.assertNotIn('overrideLaunchCommands', config)
            self.assertNotIn('loadFiles', config)

    def test_tools_do_not_import_host_repository(self):
        for path in (ROOT / 'tools').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                         else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse(any(n.split('.')[0] in ('daemon', 'firmware') for n in names), path)
        self.assertFalse((ROOT / 'tools/rp2354_flash.py').exists())

    def test_provisioning_is_noninteractive(self):
        source = (ROOT / 'tools/rp2354_provision.py').read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, 'input')
        script = (ROOT / 'tools/rp2354_build.ps1').read_text()
        self.assertNotIn('Read-Host', script)
        self.assertNotIn('.venv', script)
        self.assertNotIn('../../', script)


if __name__ == '__main__':
    unittest.main()
