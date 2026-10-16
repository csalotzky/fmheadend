"""Validate the native SDK XIP .bin and build-generated companion manifest."""
import argparse
import hashlib
import json
import struct
from pathlib import Path


def inspect_image(data, *, factory=False):
    if not 512 <= len(data) <= 0xf0000:
        raise ValueError('Image must fit a 960 KiB slot')
    sp, reset = struct.unpack_from('<II', data)
    if not 0x20000000 <= sp <= 0x20082000 or sp % 8 or not reset & 1 or not 0x10000000 <= reset < 0x10000000+len(data):
        raise ValueError('Expected native ARM XIP image linked at 0x10000000')
    blocks = [i for i in range(8,485,4) if struct.unpack_from('<I',data,i)[0]==0xffffded3]
    if len(blocks)!=1:
        raise ValueError('Expected one SDK IMAGE_DEF in the first 512 bytes')
    i=blocks[0]
    _,kind,item,version,last,delta,end=struct.unpack_from('<7I',data,i)
    if kind!=((0x1021 if factory else 0x9021)<<16|0x142) or item!=0x248 or last!=0x3ff or end!=0xab123579:
        raise ValueError('Require versioned ARM-S XIP IMAGE_DEF; OTA must be TBYB')
    if i+delta+20!=len(data) or struct.unpack_from('<5I',data,i+delta)!=(0xffffded3,0x1fe,0x1ff,(-delta)&0xffffffff,0xab123579):
        raise ValueError('Invalid SDK IMAGE_DEF block loop')
    magic=b'FMHEADEND-OTA-V1'
    marker=data.find(magic)
    if marker<0 or data.find(magic,marker+1)>=0 or marker+52>len(data):
        raise ValueError('Missing or ambiguous application identity')
    major,minor=version>>16,version&65535
    if struct.unpack_from('<HH',data,marker+48)!=(major,minor):
        raise ValueError('Application and ROM versions differ')
    return dict(format=1,image_type='rp2350-arm-s-xip',runtime_base=0x10000000,
        tbyb=not factory,size=len(data),sha256=hashlib.sha256(data).hexdigest(),
        build_id=data[marker+16:marker+32].hex(),
        firmware_version=data[marker+32:marker+48].split(b'\0',1)[0].decode('ascii'),rom_version=[major,minor])


def load_artifact(path, *, factory=False):
    path=Path(path)
    data=path.read_bytes()
    actual=inspect_image(data, factory=factory)
    if json.loads(Path(str(path)+'.json').read_text())!=actual:
        raise ValueError('Stale or inconsistent .bin.json manifest; rebuild')
    return data,actual


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',type=Path)
    parser.add_argument('--build-id',required=True)
    parser.add_argument('--major',type=int,required=True)
    parser.add_argument('--minor',type=int,required=True)
    parser.add_argument('--factory',choices=['0','1'],required=True)
    args=parser.parse_args()
    m=inspect_image(args.image.read_bytes(),factory=args.factory=='1')
    if m['build_id']!=args.build_id or m['rom_version']!=[args.major,args.minor]:
        raise ValueError('Binary differs from build inputs')
    manifest=Path(str(args.image)+'.json')
    content=json.dumps(m,indent=2)+'\n'
    if not manifest.exists() or manifest.read_text()!=content:
        temporary=manifest.with_suffix('.json.tmp')
        temporary.write_text(content)
        temporary.replace(manifest)
    print(f"Validated {m['size']} bytes; SHA256 {m['sha256']}")


if __name__=='__main__': main()
