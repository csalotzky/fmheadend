"""Native deterministic channel tests; also runnable with stdlib unittest on Linux."""
import struct
import unittest

from fmheadendd.sources.alsa_capture import link_alsa_capture


class AlsaChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
        except (ImportError, ValueError) as exc:
            raise unittest.SkipTest(f'Native GStreamer unavailable: {exc}')
        Gst.init(None)
        for name in ('appsrc', 'audioconvert', 'audioresample', 'capsfilter', 'appsink'):
            if Gst.ElementFactory.find(name) is None:
                raise unittest.SkipTest(f'Missing GStreamer element: {name}')
        cls.Gst = Gst

    def run_audio(self, mode, channels):
        Gst = self.Gst
        pipeline = Gst.Pipeline.new(None)
        def make(factory, **props):
            element = Gst.ElementFactory.make(factory)
            for key, value in props.items():
                element.set_property(key.replace('_', '-'), value)
            pipeline.add(element)
            return element
        def link(*elements):
            for left, right in zip(elements, elements[1:]):
                self.assertTrue(left.link(right))
        caps = f'audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels={channels}'
        if channels == 2:
            caps += ',channel-mask=(bitmask)0x3'
        source = make('appsrc', caps=Gst.Caps.from_string(caps), format=Gst.Format.TIME)
        target = make('audioconvert', dithering=0)
        link_alsa_capture(Gst, make, link, source, target, mode)
        resample = make('audioresample')
        sink = make('appsink', sync=False, caps=Gst.Caps.from_string(
            'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2'))
        link(target, resample, sink)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            inputs = (-0.75, 0.25) if channels == 2 else (-0.75,)
            payload = struct.pack('<' + 'f' * len(inputs), *inputs) * 512
            buf = Gst.Buffer.new_allocate(None, len(payload), None)
            buf.fill(0, payload)
            buf.pts = 0
            buf.duration = 512 * Gst.SECOND // 48000
            self.assertEqual(source.emit('push-buffer', buf), Gst.FlowReturn.OK)
            source.emit('end-of-stream')
            sample = sink.emit('try-pull-sample', 3 * Gst.SECOND)
            if sample is None:
                error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                self.fail(str(error.parse_error()) if error else 'No PCM')
            text = sample.get_caps().to_string()
            self.assertIn('rate=(int)48000', text)
            self.assertIn('channels=(int)2', text)
            self.assertIn(f'channels=(int){channels}', source.get_static_pad('src').get_current_caps().to_string())
            output = sample.get_buffer().extract_dup(0, sample.get_buffer().get_size())
            values = struct.unpack('<' + 'h' * (len(output) // 2), output)
            expected = {'stereo': (-24576, 8192), 'mono': (-8192, -8192),
                        'left': (-24576, -24576), 'right': (8192, 8192)}[mode]
            if channels == 1:
                expected = (-24576, -24576)
            for index, value in enumerate(values):
                self.assertAlmostEqual(value, expected[index % 2], delta=1)
        finally:
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_state(3 * Gst.SECOND)

    def test_stereo(self): self.run_audio('stereo', 2)
    def test_mono_mix(self): self.run_audio('mono', 2)
    def test_left(self): self.run_audio('left', 2)
    def test_right(self): self.run_audio('right', 2)
    def test_mono_input_stereo(self): self.run_audio('stereo', 1)
    def test_mono_input_mix(self): self.run_audio('mono', 1)
    def test_mono_input_left(self): self.run_audio('left', 1)
    def test_mono_input_right(self): self.run_audio('right', 1)

    def test_full_source_pipeline_accepts_mono_only_capture(self):
        from unittest.mock import patch
        from fmheadendd.models.configuration import InputSourceConfig
        from fmheadendd.sources.gst_pipeline import GstPipeline
        Gst = self.Gst
        for mode in ('stereo', 'mono', 'left', 'right'):
            with self.subTest(mode=mode):
                source = InputSourceConfig(name='Mono capture', type='ALSA',
                    config=dict(device='test-only', channel_selection=mode))
                pipeline = GstPipeline(source, timeout_s=3, reconnect=False)
                original_make = pipeline._make
                def make(factory, **properties):
                    if factory != 'alsasrc':
                        return original_make(factory, **properties)
                    element = Gst.parse_bin_from_description(
                        'audiotestsrc is-live=true ! capsfilter caps="audio/x-raw,rate=48000,channels=1"', True)
                    pipeline._pipeline.add(element)
                    return element
                with patch('fmheadendd.sources.gst_pipeline.load_gst', return_value=Gst), patch.object(pipeline, '_make', make):
                    try:
                        pipeline.start()
                        result = pipeline.probe_result()
                        self.assertTrue(result.ok, result.message)
                        self.assertEqual(result.channels, 1)
                        self.assertEqual(result.sample_rate, 48000)
                        self.assertIn('channels=(int)2', result.output_caps)
                    finally:
                        pipeline.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
