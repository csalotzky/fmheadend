"""Standalone, bounded CPU-only native DSP verification. No headend/hardware I/O.

Run with python3 on the target with GI installed. JSON output is test evidence,
not an RF measurement. One short stereo buffer is processed at a time.
"""
import array
import json
import math
import resource
import time

from fmheadendd.sources.gst_pipeline import load_gst

Gst = load_gst()
RATE = 48000


def process(description, samples, configure=None):
    if isinstance(description, str):
        pipeline = Gst.parse_launch(
            'appsrc name=input format=time caps="audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved" ! '
            + description + ' ! appsink name=output sync=false')
    else:
        pipeline = Gst.Pipeline.new(None)
        source = Gst.ElementFactory.make('appsrc', 'input')
        source.set_property('format', Gst.Format.TIME)
        source.set_property('caps', Gst.Caps.from_string('audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved'))
        sink = Gst.ElementFactory.make('appsink', 'output')
        sink.set_property('sync', False)
        elements = [source, *description, sink]
        for element in elements: pipeline.add(element)
        for left, right in zip(elements, elements[1:]): assert left.link(right)
    try:
        if configure:
            configure(pipeline)
        source, sink = pipeline.get_by_name('input'), pipeline.get_by_name('output')
        pipeline.set_state(Gst.State.PLAYING)
        raw = array.array('f', samples).tobytes()
        buffer = Gst.Buffer.new_allocate(None, len(raw), None)
        buffer.fill(0, raw)
        buffer.pts = 0
        buffer.duration = int(len(samples) / 2 / RATE * Gst.SECOND)
        assert source.emit('push-buffer', buffer) == Gst.FlowReturn.OK
        source.emit('end-of-stream')
        result = array.array('f')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            sample = sink.emit('try-pull-sample', Gst.SECOND // 10)
            if sample:
                caps = sample.get_caps().to_string()
                assert 'F32LE' in caps and 'channels=(int)2' in caps and 'rate=(int)48000' in caps, caps
                buf = sample.get_buffer()
                result.frombytes(buf.extract_dup(0, buf.get_size()))
            elif sink.get_property('eos'):
                break
            message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if message:
                raise RuntimeError(str(message.parse_error()))
        else:
            raise RuntimeError('Native processing timed out')
        assert result
        return result
    finally:
        pipeline.set_state(Gst.State.NULL)


def tone(frequency, amplitude=.2, frames=14400):
    return [x for n in range(frames) for x in (
        amplitude * math.sin(2*math.pi*frequency*n/RATE),
        amplitude/2 * math.sin(2*math.pi*frequency*n/RATE))]


def rms(samples, channel=0):
    values = samples[len(samples)//2:][channel::2]
    return math.sqrt(sum(v*v for v in values)/len(values))


def main():
    inventory = {}
    for factory in ('volume', 'audiocheblimit'):
        element = Gst.ElementFactory.make(factory)
        if element is None:
            inventory[factory] = None
            continue
        objects = [element]
        inventory[factory] = []
        for obj in objects:
            properties = {}
            for p in obj.list_properties():
                if p.name in ('name','parent'): continue
                enum = getattr(getattr(p, 'enum_class', None), '__enum_values__', {})
                properties[p.name] = dict(type=p.value_type.name,
                    minimum=getattr(p,'minimum',None), maximum=getattr(p,'maximum',None),
                    default=str(getattr(p,'default_value',None)),
                    enums={int(k): v.value_nick for k,v in enum.items()})
            inventory[factory].append(properties)
    evidence = {}
    start, cpu = time.monotonic(), time.process_time()
    signal = tone(1000)
    reference = rms(signal)
    out = process('volume volume=0.5', signal)
    assert abs(rms(out)/reference - .5) < .0001
    assert abs(rms(out, 1)/rms(out) - .5) < .0001
    evidence['gain_stereo_ratio'] = rms(out)/reference
    for factory, extra in [('audiocheblimit','type=1 ripple=0 poles=4')]:
        low = tone(1000); high = tone(20000)
        a = rms(process(f'{factory} mode=0 cutoff=15000 {extra}', low))/rms(low)
        b = rms(process(f'{factory} mode=0 cutoff=15000 {extra}', high))/rms(high)
        assert a > .9 and b < .2, (factory,a,b)
        evidence[factory + '_lpf_1k_20k'] = [a,b]
    low = tone(10); high = tone(1000)
    a = rms(process('audiocheblimit mode=1 cutoff=30 type=1 ripple=0 poles=4', low))/rms(low)
    b = rms(process('audiocheblimit mode=1 cutoff=30 type=1 ripple=0 poles=4', high))/rms(high)
    assert a < .2 and b > .9
    evidence['hpf_10hz_1k'] = [a,b]
    # Final common PCM negotiation is tested separately from float signal analysis.
    p = Gst.parse_launch('audiotestsrc num-buffers=8 ! audio/x-raw,format=F32LE,rate=48000,channels=2 ! '
                         'audioconvert ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! fakesink sync=false')
    p.set_state(Gst.State.PLAYING)
    msg = p.get_bus().timed_pop_filtered(5*Gst.SECOND,Gst.MessageType.EOS|Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    assert msg and msg.type == Gst.MessageType.EOS
    print(json.dumps(dict(gstreamer=Gst.version_string(), inventory=inventory, results=evidence,
        elapsed_s=time.monotonic()-start, cpu_s=time.process_time()-cpu,
        max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), indent=2))


if __name__ == '__main__':
    main()
