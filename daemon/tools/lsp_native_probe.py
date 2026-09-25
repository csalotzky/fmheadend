"""Isolated stereo LV2 verification; no application, devices or RF access."""
import json
from pathlib import Path
from dsp_native_probe import Gst, process, tone, rms

names = ['gate', 'expander', 'autogain', 'compressor', 'para-equalizer-x16', 'mb-compressor', 'limiter']
inventory = {}
for name in names:
    factory = 'lsp-plug-in-plugins-lv2-' + name + '-stereo'
    element = Gst.ElementFactory.make(factory)
    props = {}
    for p in element.list_properties():
        if not p.flags & 2 or p.name in ('name', 'parent', 'qos'):
            continue
        enums = getattr(type(p.default_value), '__enum_values__', {})
        props[p.name] = dict(type=p.value_type.name, min=getattr(p, 'minimum', None),
                             max=getattr(p, 'maximum', None), default=p.default_value,
                             label=p.blurb, enums={str(k): v.value_nick for k, v in enums.items()})
    inventory[name] = props
Path(__file__).with_name('lsp-properties.json').write_text(json.dumps(inventory, indent=2))

results = {}
for name in names:
    factory = 'lsp-plug-in-plugins-lv2-' + name + '-stereo'
    x = tone(1000, .4, frames=48000)
    # Compare both enable settings: installed wrapper differs from online manuals.
    results[name] = {}
    for enabled in (False, True):
        settings = ' enabled=' + str(enabled).lower()
        if name == 'compressor': settings += ' al=0.1 cr=4 at=1 rt=10'
        if name == 'gate': settings += ' gt=0.8'
        if name == 'limiter': settings += ' th=0.1 boost=false'
        try:
            y = process(factory + settings, x)
            results[name][str(enabled)] = {'rms': rms(y), 'peak': max(abs(v) for v in y),
                                         'frames': len(y)//2}
        except Exception as exc:
            results[name][str(enabled)] = {'error': str(exc)}
print(json.dumps(results, indent=2))

# Non-neutral stereo EQ and multiband checks, not merely successful negotiation.
for description in [
    'para-equalizer-x16-stereo enabled=true ft-0=1 f-0=1000 q-0=1 g-0=1.995262',
    'para-equalizer-x16-stereo enabled=true ft-0=6 f-0=1000 q-0=2',
    'mb-compressor-stereo enabled=true mode=0 cbe-1=true sf-1=120 cbe-2=true sf-2=1000 cbe-3=true sf-3=5000 cbe-4=false cbe-5=false cbe-6=false cbe-7=false ce-0=false ce-1=false ce-2=false ce-3=false',
    'mb-compressor-stereo enabled=true mode=0 cbe-1=true sf-1=120 cbe-2=true sf-2=1000 cbe-3=true sf-3=5000 cbe-4=false cbe-5=false cbe-6=false cbe-7=false ce-0=true ce-1=true ce-2=true ce-3=true al-0=0.05 al-1=0.05 al-2=0.05 al-3=0.05 cr-0=4 cr-1=4 cr-2=4 cr-3=4',
]:
    x = tone(1000, .4, frames=48000)
    y = process('lsp-plug-in-plugins-lv2-' + description, x)
    print(json.dumps({'case': description, 'gain': rms(y)/rms(x), 'peak': max(abs(v) for v in y)}))
