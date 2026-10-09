/* Serialize Lilv's shared library cache, not audio processing.
 *
 * gst-lv2 uses one LilvWorld across all elements. Lilv 0.24.26 modifies
 * world->libs and LilvLib.refs without a mutex in instantiate/instance_free.
 * These entry points run on different Gst streaming/control threads. Install
 * this global symbol provider BEFORE gst-lv2 is loaded. No Python callbacks,
 * GIL, per-buffer locks, or changes to LV2 run/activate/control ports.
 *
 * Opaque ABI declarations avoid requiring Lilv or Python development headers.
 * The platform-tagged library is loaded with ctypes, never Python import.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct LilvPluginImpl LilvPlugin;
typedef struct LilvInstanceImpl LilvInstance;
typedef struct LV2_Feature LV2_Feature;

static pthread_mutex_t lifecycle = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;
static LilvInstance *(*real_instantiate)(const LilvPlugin *, double,
                                        const LV2_Feature *const *);
static void (*real_free)(LilvInstance *);

LilvInstance *lilv_plugin_instantiate(const LilvPlugin *, double,
                                     const LV2_Feature *const *);
void lilv_instance_free(LilvInstance *);

const char *fmheadend_lv2_guard_install(void)
{
    /* A previously loaded local gst-lv2 may already have bound to Lilv.
     * Reject late installation, including local (non-global) dependencies.
     * Merely loading this guard does not load Lilv or any audio plugin.
     */
    void *existing = dlopen("liblilv-0.so.0", RTLD_NOW | RTLD_NOLOAD);
    if (existing) {
        dlclose(existing);
        return "Lilv already loaded before lifecycle guard";
    }
    if (dlsym(RTLD_DEFAULT, "lilv_plugin_instantiate") != (void *)lilv_plugin_instantiate ||
        dlsym(RTLD_DEFAULT, "lilv_instance_free") != (void *)lilv_instance_free)
        return "Lilv lifecycle symbols are already interposed by another library";
    return NULL;
}

static void resolve(void)
{
    /* Explicit handle: RTLD_NEXT cannot find a plugin's RTLD_LOCAL dependency.
     * Retain this handle permanently so native function pointers stay valid.
     */
    void *library = dlopen("liblilv-0.so.0", RTLD_NOW | RTLD_LOCAL);
    if (library) {
        real_instantiate = dlsym(library, "lilv_plugin_instantiate");
        real_free = dlsym(library, "lilv_instance_free");
    }
    if (!real_instantiate || !real_free) {
        fputs("fmheadend: cannot resolve Lilv lifecycle ABI\n", stderr);
        abort(); /* Impossible after gst-lv2 successfully links these symbols. */
    }
}

LilvInstance *lilv_plugin_instantiate(const LilvPlugin *plugin, double rate,
                                     const LV2_Feature *const *features)
{
    pthread_once(&resolve_once, resolve);
    pthread_mutex_lock(&lifecycle);
    LilvInstance *instance = real_instantiate(plugin, rate, features);
    pthread_mutex_unlock(&lifecycle);
    return instance;
}

void lilv_instance_free(LilvInstance *instance)
{
    if (!instance)
        return;
    pthread_once(&resolve_once, resolve);
    pthread_mutex_lock(&lifecycle);
    real_free(instance);
    pthread_mutex_unlock(&lifecycle);
}
