"""Offline picotool contract tests. Set PICOTOOL to the 2.3.1 executable.

Only partition create and info with an explicit local input file are used.
No USB device queries, loading or reboot commands are issued.
"""
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

PICOTOOL = os.environ.get('PICOTOOL')
pytestmark = pytest.mark.skipif(not PICOTOOL, reason='Set PICOTOOL for offline partition generation tests')
TABLE = Path(__file__).resolve().parents[1] / 'partitions.json'


@pytest.mark.parametrize('b_id',[0,1])
def test_id_is_not_ab_link_index(tmp_path,b_id):
    table=json.loads(TABLE.read_text())
    assert [part['id'] for part in table['partitions']]==[0,0]
    table['partitions'][1]['id']=b_id
    source=tmp_path/'table.json';binary=tmp_path/'table.bin'
    source.write_text(json.dumps(table))
    subprocess.run([PICOTOOL,'partition','create',str(source),str(binary)],check=True,capture_output=True,text=True)
    result=subprocess.run([PICOTOOL,'info','-a',str(binary)],check=True,capture_output=True,text=True).stdout
    assert 'hash:                  verified' in result
    assert re.search(r'partition 0 \(A\):\s+00004000->000f4000',result)
    assert re.search(r'partition 1 \(B w/ 0\):\s+000f4000->001e4000',result)
    assert f'id={b_id:016x}' in result
    # Changing the optional 64-bit ID does not change the encoded A/B link.
    assert table['partitions'][1]['link']==['a',0]
