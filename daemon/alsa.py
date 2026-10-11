"""On-demand, read-only Linux ALSA hardware capture inventory.

The kernel proc interface exposes PCM direction without opening /dev/snd.
Virtual/user-defined PCMs remain available through custom names, not this inventory.
"""
from pathlib import Path
import re

from .errors import DomainError
from .models.configuration import Model


class AlsaCaptureDevice(Model):
    pcm: str
    label: str
    card_id: str
    device: int


class AlsaCaptureDevices(Model):
    devices: list[AlsaCaptureDevice]


class AlsaDiscovery:
    def __init__(self, root=Path('/proc/asound')):
        self.root = Path(root)

    def capture_devices(self) -> AlsaCaptureDevices:
        devices = []
        try:
            try:
                entries = list(self.root.iterdir())
            except FileNotFoundError:
                return AlsaCaptureDevices(devices=[])
            # Kernel-generated, non-localized card metadata; never command output.
            names = {}
            try:
                for line in (self.root / 'cards').read_text().splitlines():
                    match = re.match(r'\s*(\d+)\s+\[.*?\]\s*:\s*.*? - (.+)', line)
                    if match:
                        names[int(match[1])] = match[2].strip()
            except FileNotFoundError:
                pass
            for card in sorted(entries):
                if not re.fullmatch(r'card[0-9]+', card.name):
                    continue
                try:
                    card_id = (card / 'id').read_text().strip()
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', card_id):
                        raise ValueError('Invalid ALSA card identifier')
                    for pcm in sorted(card.iterdir()):
                        match = re.fullmatch(r'pcm([0-9]+)c', pcm.name)
                        if not match:
                            continue
                        try:
                            info = dict(line.split(':', 1) for line in (pcm / 'info').read_text().splitlines() if ':' in line)
                        except FileNotFoundError:
                            continue  # USB device removed during enumeration.
                        if info.get('stream', '').strip() != 'CAPTURE':
                            continue
                        number = int(match[1])
                        name = info.get('name', '').strip() or card_id
                        card_name = names.get(int(card.name[4:]), card_id)
                        devices.append(AlsaCaptureDevice(
                            pcm=f'plughw:CARD={card_id},DEV={number}',
                            label=f'{card_name} / {name} ({card_id}, device {number})',
                            card_id=card_id, device=number))
                except FileNotFoundError:
                    continue
        except (OSError, ValueError) as exc:
            raise DomainError(503, 'ALSA_DISCOVERY_ERROR', f'Cannot enumerate ALSA capture devices: {exc}') from exc
        return AlsaCaptureDevices(devices=sorted(devices, key=lambda d: (d.label.casefold(), d.pcm)))
