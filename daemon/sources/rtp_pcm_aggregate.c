/* RTP L16 depay and this element run on the same push-mode streaming thread. */
#include <gst/gst.h>
#include <gmodule.h>

#define BLOCK_TIME (40 * GST_MSECOND)
#define MAX_AGE (100 * GST_MSECOND)
#define L16_CAPS "audio/x-raw, format=(string)S16BE, layout=(string)interleaved, " \
                 "rate=(int)[25,MAX], channels=(int)[1,8]"

typedef struct {
    GstBuffer *buffer;
    GstClockTime arrival;
    gsize size, offset;
} Fragment;

typedef struct {
    GstElement parent;
    GstPad *sink, *src, *upstream;
    gulong ingress_probe, output_probe;
    GQueue pending;
    gsize bytes, block_bytes;
    guint rate, frame_bytes;
    gboolean discont, push_expired;
    guint64 expired_frames, reset_frames;
} RtpPcmAggregate;

typedef GstElementClass RtpPcmAggregateClass;
G_DEFINE_TYPE(RtpPcmAggregate, rtp_pcm_aggregate, GST_TYPE_ELEMENT)

static GstStaticPadTemplate sink_template = GST_STATIC_PAD_TEMPLATE(
    "sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS(L16_CAPS));
static GstStaticPadTemplate src_template = GST_STATIC_PAD_TEMPLATE(
    "src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS(L16_CAPS));

enum { PROP_EXPIRED = 1, PROP_RESET };
static GQuark arrival_key;

static void consume(RtpPcmAggregate *self, gsize bytes)
{
    self->bytes -= bytes;
    while (bytes) {
        Fragment *first = g_queue_peek_head(&self->pending);
        gsize consumed = MIN(bytes, first->size - first->offset);
        bytes -= consumed;
        first->offset += consumed;
        if (first->offset == first->size) {
            g_queue_pop_head(&self->pending);
            gst_buffer_unref(first->buffer);
            g_free(first);
        }
    }
}

static void expire(RtpPcmAggregate *self, GstClockTime now)
{
    guint64 dropped = 0;
    Fragment *first;
    while ((first = g_queue_peek_head(&self->pending)) && now - first->arrival >= MAX_AGE) {
        gsize bytes = first->size - first->offset;
        dropped += bytes / self->frame_bytes;
        consume(self, bytes);
    }
    if (dropped) {
        self->discont = TRUE;
        GST_OBJECT_LOCK(self);
        self->expired_frames += dropped;
        GST_OBJECT_UNLOCK(self);
    }
}

static void reset_pending(RtpPcmAggregate *self)
{
    expire(self, gst_util_get_timestamp());
    if (self->bytes) {
        GST_OBJECT_LOCK(self);
        self->reset_frames += self->bytes / self->frame_bytes;
        GST_OBJECT_UNLOCK(self);
        consume(self, self->bytes);
    }
    self->discont = TRUE;
}

static GstClockTime frame_time(RtpPcmAggregate *self, gsize bytes)
{
    return gst_util_uint64_scale(bytes / self->frame_bytes, GST_SECOND, self->rate);
}

static GstClockTime shift_timestamp(GstClockTime stamp, GstClockTime offset)
{
    return GST_CLOCK_TIME_IS_VALID(stamp) && offset < GST_CLOCK_TIME_NONE - stamp
        ? stamp + offset : GST_CLOCK_TIME_NONE;
}

static GstFlowReturn push_pending(RtpPcmAggregate *self, gboolean drain)
{
    for (;;) {
        expire(self, gst_util_get_timestamp());
        if (!self->bytes || (!drain && self->bytes < self->block_bytes))
            return GST_FLOW_OK;

        gsize size = MIN(self->bytes, self->block_bytes);
        Fragment *first = g_queue_peek_head(&self->pending);
        GstBuffer *out = gst_buffer_new_allocate(NULL, size, NULL);
        GstMapInfo map;
        if (!out)
            return GST_FLOW_ERROR;
        if (!gst_buffer_map(out, &map, GST_MAP_WRITE)) {
            gst_buffer_unref(out);
            return GST_FLOW_ERROR;
        }
        gsize written = 0;
        for (GList *node = self->pending.head; written < size; node = node->next) {
            Fragment *part = node->data;
            gsize count = MIN(part->size - part->offset, size - written);
            if (gst_buffer_extract(part->buffer, part->offset, map.data + written, count) != count) {
                gst_buffer_unmap(out, &map);
                gst_buffer_unref(out);
                return GST_FLOW_ERROR;
            }
            if (GST_BUFFER_FLAG_IS_SET(part->buffer, GST_BUFFER_FLAG_CORRUPTED))
                GST_BUFFER_FLAG_SET(out, GST_BUFFER_FLAG_CORRUPTED);
            written += count;
        }
        gst_buffer_unmap(out, &map);
        GstClockTime offset = frame_time(self, first->offset);
        GST_BUFFER_PTS(out) = shift_timestamp(GST_BUFFER_PTS(first->buffer), offset);
        GST_BUFFER_DTS(out) = shift_timestamp(GST_BUFFER_DTS(first->buffer), offset);
        GST_BUFFER_DURATION(out) = frame_time(self, size);
        if (self->discont)
            GST_BUFFER_FLAG_SET(out, GST_BUFFER_FLAG_DISCONT);

        self->push_expired = FALSE;
        GstFlowReturn result = gst_pad_push(self->src, out);
        if (result != GST_FLOW_OK)
            return result;
        if (self->push_expired)
            continue;
        consume(self, size);
        self->discont = FALSE;
    }
}

/* Last age check at the output pad, after assembling a candidate. If the
 * head expired, rebuild from the still-fresh fragments on this same thread. */
static GstPadProbeReturn check_output_age(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
    (void)pad;
    (void)info;
    RtpPcmAggregate *self = data;
    Fragment *first = g_queue_peek_head(&self->pending);
    if (gst_util_get_timestamp() - first->arrival < MAX_AGE)
        return GST_PAD_PROBE_OK;
    self->push_expired = TRUE;
    return GST_PAD_PROBE_DROP;
}

static gboolean stamp_buffer(GstBuffer **buffer, guint index, gpointer data)
{
    (void)index;
    GstClockTime *arrival = g_new(GstClockTime, 1);
    *arrival = *(GstClockTime *)data;
    gst_mini_object_set_qdata(GST_MINI_OBJECT(*buffer), arrival_key, arrival, g_free);
    return TRUE;
}

static GstPadProbeReturn stamp_arrival(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
    (void)pad;
    (void)data;
    GstClockTime now = gst_util_get_timestamp();
    if (GST_PAD_PROBE_INFO_TYPE(info) & GST_PAD_PROBE_TYPE_BUFFER_LIST) {
        gst_buffer_list_foreach(GST_PAD_PROBE_INFO_BUFFER_LIST(info), stamp_buffer, &now);
    } else {
        GstBuffer *buffer = GST_PAD_PROBE_INFO_BUFFER(info);
        stamp_buffer(&buffer, 0, &now);
    }
    return GST_PAD_PROBE_OK;
}

static GstFlowReturn chain(GstPad *pad, GstObject *parent, GstBuffer *buffer)
{
    (void)pad;
    RtpPcmAggregate *self = (RtpPcmAggregate *)parent;
    const GstClockTime *arrival = gst_mini_object_get_qdata(GST_MINI_OBJECT(buffer), arrival_key);
    gsize size = gst_buffer_get_size(buffer);
    if (!self->frame_bytes || !arrival || size % self->frame_bytes) {
        gst_buffer_unref(buffer);
        return GST_FLOW_ERROR;
    }
    if (GST_BUFFER_FLAG_IS_SET(buffer, GST_BUFFER_FLAG_DISCONT) ||
        GST_BUFFER_FLAG_IS_SET(buffer, GST_BUFFER_FLAG_RESYNC)) {
        GstFlowReturn result = push_pending(self, TRUE);
        if (result != GST_FLOW_OK) {
            gst_buffer_unref(buffer);
            return result;
        }
        self->discont = TRUE;
    }
    if (size) {
        Fragment *fragment = g_new(Fragment, 1);
        *fragment = (Fragment){buffer, *arrival, size, 0};
        g_queue_push_tail(&self->pending, fragment);
        self->bytes += size;
    } else {
        gst_buffer_unref(buffer);
    }
    return push_pending(self, FALSE);
}

static gboolean sink_event(GstPad *pad, GstObject *parent, GstEvent *event)
{
    RtpPcmAggregate *self = (RtpPcmAggregate *)parent;
    GstEventType type = GST_EVENT_TYPE(event);
    /* FLUSH_START remains asynchronous and takes no streaming-state lock.
     * FLUSH_STOP and these stream boundaries are serialized with chain(). */
    if (type == GST_EVENT_FLUSH_STOP) {
        reset_pending(self);
    } else if (type == GST_EVENT_CAPS || type == GST_EVENT_STREAM_START ||
               type == GST_EVENT_SEGMENT || type == GST_EVENT_GAP || type == GST_EVENT_EOS) {
        if (push_pending(self, TRUE) != GST_FLOW_OK) {
            gst_event_unref(event);
            return FALSE;
        }
        self->discont = TRUE;
    }
    if (type == GST_EVENT_CAPS) {
        GstCaps *caps;
        gint rate, channels;
        gst_event_parse_caps(event, &caps);
        const GstStructure *format = gst_caps_get_structure(caps, 0);
        if (!gst_structure_get_int(format, "rate", &rate) || rate < 25 ||
            !gst_structure_get_int(format, "channels", &channels) || channels < 1 || channels > 8) {
            gst_event_unref(event);
            return FALSE;
        }
        self->rate = rate;
        self->frame_bytes = channels * 2;
        self->block_bytes = (gsize)(rate / 25) * self->frame_bytes;
    }
    return gst_pad_event_default(pad, parent, event);
}

static gboolean src_query(GstPad *pad, GstObject *parent, GstQuery *query)
{
    RtpPcmAggregate *self = (RtpPcmAggregate *)parent;
    if (GST_QUERY_TYPE(query) != GST_QUERY_LATENCY)
        return gst_pad_query_default(pad, parent, query);
    if (!gst_pad_peer_query(self->sink, query))
        return FALSE;
    GstClockTime min, max;
    gboolean live;
    gst_query_parse_latency(query, &live, &min, &max);
    gst_query_set_latency(query, live, min + BLOCK_TIME, shift_timestamp(max, BLOCK_TIME));
    return TRUE;
}

static void detach_ingress(RtpPcmAggregate *self)
{
    if (self->ingress_probe) {
        gst_pad_remove_probe(self->upstream, self->ingress_probe);
        self->ingress_probe = 0;
    }
    gst_clear_object(&self->upstream);
}

static GstStateChangeReturn change_state(GstElement *element, GstStateChange transition)
{
    RtpPcmAggregate *self = (RtpPcmAggregate *)element;
    if (transition == GST_STATE_CHANGE_READY_TO_PAUSED) {
        self->upstream = gst_pad_get_peer(self->sink);
        if (!self->upstream)
            return GST_STATE_CHANGE_FAILURE;
        self->ingress_probe = gst_pad_add_probe(self->upstream,
            GST_PAD_PROBE_TYPE_BUFFER | GST_PAD_PROBE_TYPE_BUFFER_LIST, stamp_arrival, NULL, NULL);
    }
    GstStateChangeReturn result = GST_ELEMENT_CLASS(rtp_pcm_aggregate_parent_class)->change_state(element, transition);
    if (transition == GST_STATE_CHANGE_PAUSED_TO_READY ||
        (transition == GST_STATE_CHANGE_READY_TO_PAUSED && result == GST_STATE_CHANGE_FAILURE)) {
        detach_ingress(self);
        reset_pending(self);
    }
    return result;
}

static void finalize(GObject *object)
{
    RtpPcmAggregate *self = (RtpPcmAggregate *)object;
    detach_ingress(self);
    consume(self, self->bytes);
    G_OBJECT_CLASS(rtp_pcm_aggregate_parent_class)->finalize(object);
}

static void dispose(GObject *object)
{
    RtpPcmAggregate *self = (RtpPcmAggregate *)object;
    /* A caller may retain the source pad after its element is destroyed. */
    if (self->output_probe) {
        gst_pad_remove_probe(self->src, self->output_probe);
        self->output_probe = 0;
    }
    G_OBJECT_CLASS(rtp_pcm_aggregate_parent_class)->dispose(object);
}

static void get_property(GObject *object, guint id, GValue *value, GParamSpec *spec)
{
    RtpPcmAggregate *self = (RtpPcmAggregate *)object;
    if (id == PROP_EXPIRED || id == PROP_RESET) {
        GST_OBJECT_LOCK(self);
        g_value_set_uint64(value, id == PROP_EXPIRED ? self->expired_frames : self->reset_frames);
        GST_OBJECT_UNLOCK(self);
    } else {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, spec);
    }
}

static void rtp_pcm_aggregate_class_init(RtpPcmAggregateClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);
    arrival_key = g_quark_from_static_string("fmheadend-rtp-depay-arrival");
    object_class->get_property = get_property;
    object_class->dispose = dispose;
    object_class->finalize = finalize;
    g_object_class_install_property(object_class, PROP_EXPIRED, g_param_spec_uint64(
        "expired-frames", "Expired frames", "Frames discarded at the aggregation age limit",
        0, G_MAXUINT64, 0, G_PARAM_READABLE | G_PARAM_STATIC_STRINGS));
    g_object_class_install_property(object_class, PROP_RESET, g_param_spec_uint64(
        "reset-frames", "Reset frames", "Fresh frames discarded on flush or stop",
        0, G_MAXUINT64, 0, G_PARAM_READABLE | G_PARAM_STATIC_STRINGS));
    klass->change_state = change_state;
    gst_element_class_set_static_metadata(klass, "FMheadend RTP PCM aggregation", "Filter/Audio",
        "40 ms L16 blocks with a monotonic residence limit", "FMheadend");
    gst_element_class_add_static_pad_template(klass, &sink_template);
    gst_element_class_add_static_pad_template(klass, &src_template);
}

static void rtp_pcm_aggregate_init(RtpPcmAggregate *self)
{
    self->discont = TRUE;
    g_queue_init(&self->pending);
    self->sink = gst_pad_new_from_static_template(&sink_template, "sink");
    self->src = gst_pad_new_from_static_template(&src_template, "src");
    GST_PAD_SET_PROXY_CAPS(self->sink);
    GST_PAD_SET_PROXY_CAPS(self->src);
    self->output_probe = gst_pad_add_probe(self->src, GST_PAD_PROBE_TYPE_BUFFER, check_output_age, self, NULL);
    gst_pad_set_chain_function(self->sink, chain);
    gst_pad_set_event_function(self->sink, sink_event);
    gst_pad_set_query_function(self->src, src_query);
    gst_element_add_pad(GST_ELEMENT(self), self->sink);
    gst_element_add_pad(GST_ELEMENT(self), self->src);
}

G_MODULE_EXPORT gboolean fmheadend_register_rtp_pcm_aggregate(void)
{
    return gst_element_register(NULL, "fmrtppcmaggregate", GST_RANK_NONE, rtp_pcm_aggregate_get_type());
}
