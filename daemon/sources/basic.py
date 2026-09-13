"""Small native DSP chain, after common gain; no Python sample processing."""
PROCESSING_CAPS = "audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved"


def basic_stages(make, config):
    """Return ordered native elements; bypassed optional stages are not created."""
    stages = []
    if config.highpass_enabled:
        stages.append(make("audiocheblimit", mode=1, type=1, cutoff=config.highpass_hz, ripple=0, poles=4))
    stages.append(make("equalizer-3bands", band0=config.eq_low_db, band1=config.eq_mid_db, band2=config.eq_high_db))
    if config.compressor_enabled:
        stages.append(make("audiodynamic", mode=0, characteristics=1,
                           threshold=10 ** (config.compressor_threshold_dbfs / 20),
                           ratio=1 / config.compressor_ratio))
    if config.lowpass_enabled:
        stages.append(make("audiocheblimit", mode=0, type=1, cutoff=config.lowpass_hz, ripple=0, poles=4))
    if config.limiter_enabled:
        stages.append(make("rglimiter", enabled=True))
    return stages
