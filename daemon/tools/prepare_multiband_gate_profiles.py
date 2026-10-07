"""Prepare OFF Gate/Expander presets for deployment; never overwrite originals.

Run with the updated application's PYTHONPATH. Output contains byte-exact backups
and converted default/custom trees. Install converted files only with that daemon.
"""
import argparse
import json
from pathlib import Path
import shutil

from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.models.dsp import MultibandGate, MultibandExpander


def convert(document):
    result = json.loads(json.dumps(document))
    old = result['stages']['gate']
    if old['selected'] != 'OFF':
        raise ValueError('Enabled Gate/Expander requires an explicitly reviewed migration')
    settings = {}
    for previous, current, model in (
        ('LSP_GATE', 'LSP_MULTIBAND_GATE', MultibandGate),
        ('LSP_EXPANDER', 'LSP_MULTIBAND_EXPANDER', MultibandExpander),
    ):
        new = model().model_dump()
        for band in new['bands']:
            for key, value in old['implementations'].get(previous, {}).items():
                if key in band:
                    band[key] = value
        settings[current] = new
    result['stages']['gate'] = dict(selected='OFF', implementations=settings)
    AudioProfileFiles.validate(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--default-root', type=Path, required=True)
    parser.add_argument('--custom-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Validate the entire input set before creating any prepared output.
    prepared = [(kind, path, convert(json.loads(path.read_text(encoding='utf-8'))))
                for kind, root in (('default', args.default_root), ('custom', args.custom_root))
                for path in sorted(root.glob('*.json'))]
    args.output.mkdir(parents=True, exist_ok=False)
    for kind, path, converted in prepared:
        backup = args.output / 'backup' / kind / path.name
        target = args.output / 'converted' / kind / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        target.write_text(json.dumps(converted, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    loader = AudioProfileFiles(args.output / 'converted/default', args.output / 'converted/custom')
    for kind, path, _ in prepared:
        loader.get(f'{kind}/{path.name}')
        print(f'Backed up and validated: {kind}/{path.name}')


if __name__ == '__main__':
    main()
