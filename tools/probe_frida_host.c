/* Diagnostic host for self-owned, no-data fixtures ONLY.
 * NOT a strict Probe runner: Python preflight and controller ownership are not
 * bound to attachment. Never use this as a Probe entry point.
 * No discovery, launch, restart, reconnect, target signal or app installation.
 * The pinned Frida devkit already includes GLib and JSON-GLib.
 */
#include "frida-core.h"
#include <libproc.h>
#include <sys/proc_info.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static GMainLoop *loop;
static guint pid;
static unsigned long long birth_sec, birth_usec, expected_uid;
static const char *expected_exe;
static gint failed, ready;
static volatile sig_atomic_t stop_requested;

/* Cancellation is cooperative, not proof that remote work was undone.
 * Each synchronous stage has an independent watchdog; cleanup gets a fresh
 * cancellable. An unresponsive library never permits signalling the target.
 */
typedef struct {
  GMutex mutex;
  GCond condition;
  GThread *thread;
  GCancellable *cancel;
  const char *name;
  gint64 deadline;
  gboolean complete, cancelled, cleanup;
} Stage;

static gpointer watch_stage(gpointer value) {
  Stage *stage = value;
  g_mutex_lock(&stage->mutex);
  while (!stage->complete) {
    gint64 now = g_get_monotonic_time();
    if (now >= stage->deadline || (stop_requested && !stage->cleanup)) {
      stage->cancelled = TRUE;
      g_atomic_int_set(&failed, 1);
      g_mutex_unlock(&stage->mutex);
      fprintf(stderr, "%s: cancellation requested (%s); outcome not assumed clean\n",
              stage->name, now >= stage->deadline ? "deadline" : "signal");
      g_cancellable_cancel(stage->cancel);
      return NULL;
    }
    g_cond_wait_until(&stage->condition, &stage->mutex,
                     MIN(stage->deadline, now + 100 * 1000));
  }
  g_mutex_unlock(&stage->mutex);
  return NULL;
}

static void begin_stage(Stage *stage, const char *name, gint milliseconds,
                        gboolean cleanup) {
  memset(stage, 0, sizeof(*stage));
  g_mutex_init(&stage->mutex);
  g_cond_init(&stage->condition);
  stage->name = name;
  stage->cleanup = cleanup;
  stage->cancel = g_cancellable_new();
  stage->deadline = g_get_monotonic_time() + (gint64) milliseconds * 1000;
  stage->thread = g_thread_new("fixture-deadline", watch_stage, stage);
}

static gboolean end_stage(Stage *stage, GError **error) {
  g_mutex_lock(&stage->mutex);
  /* A heavily delayed watchdog must not turn an over-budget call into a pass. */
  if (g_get_monotonic_time() >= stage->deadline || (stop_requested && !stage->cleanup))
    stage->cancelled = TRUE;
  stage->complete = TRUE;
  g_cond_signal(&stage->condition);
  g_mutex_unlock(&stage->mutex);
  g_thread_join(stage->thread);
  gboolean ok = !stage->cancelled && *error == NULL;
  if (!ok) {
    g_atomic_int_set(&failed, 1);
    fprintf(stderr, "%s failed: %s%s\n", stage->name,
            *error ? (*error)->message : "deadline/signal cancellation",
            stage->cleanup ? "; cleanup unverified" : "");
  }
  g_clear_error(error);
  g_object_unref(stage->cancel);
  g_cond_clear(&stage->condition);
  g_mutex_clear(&stage->mutex);
  return ok;
}

static int matches(void) {
  struct proc_bsdinfo info = {0};
  char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
  return proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) == sizeof(info)
      && proc_pidpath(pid, path, sizeof(path)) > 0
      && info.pbi_pid == pid && info.pbi_uid == expected_uid
      && info.pbi_start_tvsec == birth_sec && info.pbi_start_tvusec == birth_usec
      && strcmp(path, expected_exe) == 0;
}
static void request_stop(int signo) {
  (void) signo;
  /* Async-signal-safe: watchdog/main loop stops only our session. */
  stop_requested = 1;
}
static gboolean tick(gpointer unused) {
  (void) unused;
  if (stop_requested || !matches()) g_atomic_int_set(&failed, 1);
  if (g_atomic_int_get(&failed)) g_main_loop_quit(loop);
  return G_SOURCE_CONTINUE;
}
static gboolean stop(gpointer unused) {
  (void) unused;
  g_main_loop_quit(loop);
  return G_SOURCE_CONTINUE;
}

typedef enum { EVENT_OTHER, EVENT_READY, EVENT_ERROR, EVENT_INVALID } Event;

static const gchar *string_member(JsonObject *object, const gchar *name) {
  JsonNode *node = json_object_get_member(object, name);
  return node && JSON_NODE_HOLDS_VALUE(node) && json_node_get_value_type(node) == G_TYPE_STRING
      ? json_node_get_string(node) : NULL;
}
static Event classify_message(const gchar *value, guint expected_pid) {
  JsonParser *parser = json_parser_new();
  GError *error = NULL;
  Event result = EVENT_INVALID;
  if (!json_parser_load_from_data(parser, value, -1, &error)) goto done;
  JsonNode *root = json_parser_get_root(parser);
  if (!root || !JSON_NODE_HOLDS_OBJECT(root)) goto done;
  JsonObject *object = json_node_get_object(root);
  const gchar *type = string_member(object, "type");
  if (!type) goto done;
  if (!strcmp(type, "error")) { result = EVENT_ERROR; goto done; }
  result = EVENT_OTHER;
  if (strcmp(type, "send")) goto done;
  JsonNode *payload = json_object_get_member(object, "payload");
  if (!payload || !JSON_NODE_HOLDS_OBJECT(payload)) goto done;
  JsonObject *body = json_node_get_object(payload);
  const gchar *kind = string_member(body, "kind");
  if (!kind) goto done;
  if (!strcmp(kind, "read_error")) { result = EVENT_ERROR; goto done; }
  if (!strcmp(kind, "metadata_verified") || !strcmp(kind, "fixture_metadata")) {
    JsonNode *identity = json_object_get_member(body, "pid");
    result = identity && JSON_NODE_HOLDS_VALUE(identity)
        && json_node_get_value_type(identity) == G_TYPE_INT64
        && json_node_get_int(identity) == expected_pid ? EVENT_READY : EVENT_ERROR;
  }
done:
  g_clear_error(&error);
  g_object_unref(parser);
  return result;
}
static void accept_event(Event event) {
  if (event == EVENT_READY) g_atomic_int_set(&ready, 1);
  if (event == EVENT_ERROR || event == EVENT_INVALID) {
    g_atomic_int_set(&failed, 1);
    if (loop) g_main_loop_quit(loop);
  }
}
static void message(FridaScript *script, const gchar *value, GBytes *data, gpointer unused) {
  (void) script; (void) data; (void) unused;
  accept_event(classify_message(value, pid));
  puts(value);
  fflush(stdout);
}
static void detached(FridaSession *session, FridaSessionDetachReason reason,
                     FridaCrash *crash, gpointer unused) {
  (void) session; (void) reason; (void) crash; (void) unused;
  g_atomic_int_set(&failed, 1);
  g_main_loop_quit(loop);
}
static gboolean successful(void) {
  return g_atomic_int_get(&ready) && !g_atomic_int_get(&failed) && !stop_requested;
}
static gboolean number(const char *arg, unsigned long long *result) {
  char *end = NULL;
  errno = 0;
  *result = strtoull(arg, &end, 10);
  return !errno && *arg >= '0' && *arg <= '9' && !*end;
}

/* Pure message/argument tests plus cancellable watchdogs; no Frida attachment. */
static int self_test(void) {
  static const struct { const char *json; Event event; } cases[] = {
    {"{\"type\":\"error\",\"description\":\"synthetic\"}", EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"read_error\"}}", EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"metadata_verified\",\"pid\":7}}", EVENT_READY},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\",\"pid\":7}}", EVENT_READY},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\",\"pid\":8}}", EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\"}}", EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"observer_ready\"}}", EVENT_OTHER},
    {"{\"type\":\"log\",\"payload\":\"ready\"}", EVENT_OTHER},
    {"{}", EVENT_INVALID}, {"not json", EVENT_INVALID}, {"[]", EVENT_INVALID}
  };
  for (guint i = 0; i < G_N_ELEMENTS(cases); i++)
    if (classify_message(cases[i].json, 7) != cases[i].event) return 1;
  g_atomic_int_set(&failed, 0); g_atomic_int_set(&ready, 0);
  if (successful()) return 1; /* No ready cannot pass. */
  accept_event(EVENT_READY);
  if (!successful()) return 1;
  accept_event(EVENT_ERROR);
  if (successful()) return 1;
  accept_event(EVENT_READY);
  if (successful()) return 1; /* Ready cannot erase a previous error. */
  unsigned long long value;
  if (number("-1", &value) || number(" 1", &value) || number("1x", &value)
      || number("", &value) || number("18446744073709551616", &value)
      || !number("7", &value) || value != 7) return 1;
  g_atomic_int_set(&failed, 0);
  Stage stage;
  GError *error = NULL;
  begin_stage(&stage, "self-test deadline", 10, FALSE);
  for (int i = 0; i < 100 && !g_cancellable_is_cancelled(stage.cancel); i++) g_usleep(10000);
  gboolean cancelled = g_cancellable_is_cancelled(stage.cancel);
  if (end_stage(&stage, &error) || !cancelled || !g_atomic_int_get(&failed)) return 1;
  g_atomic_int_set(&failed, 0);
  stop_requested = 1;
  begin_stage(&stage, "self-test signal", 1000, FALSE);
  for (int i = 0; i < 100 && !g_cancellable_is_cancelled(stage.cancel); i++) g_usleep(10000);
  cancelled = g_cancellable_is_cancelled(stage.cancel);
  if (end_stage(&stage, &error) || !cancelled) return 1;
  begin_stage(&stage, "self-test cleanup after signal", 1000, TRUE);
  if (!end_stage(&stage, &error)) return 1;
  stop_requested = 0;
  g_atomic_int_set(&failed, 0);
  begin_stage(&stage, "self-test cleanup failure", 1000, TRUE);
  error = g_error_new_literal(g_quark_from_static_string("fixture-self-test"), 1,
                              "synthetic detach failure");
  if (end_stage(&stage, &error) || error != NULL || !g_atomic_int_get(&failed)) return 1;
  puts("host self-test passed: envelopes, ready/failure, arguments, cancellation; no attachment");
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 2 && !strcmp(argv[1], "--self-test")) {
    /* The embedded GLib/JSON-GLib runtime is initialized by Frida. This does
     * not create a device manager, enumerate devices or attach to a process. */
    frida_init();
    int result = self_test();
    frida_deinit();
    return result;
  }
  if (argc != 9 || strcmp(argv[1], "--fixture-only")) {
    fputs("fixture only; not a Probe runner\nusage: host --fixture-only PID UID BIRTH_SEC BIRTH_USEC EXACT_EXE SCRIPT SECONDS\n", stderr);
    return 2;
  }
  unsigned long long raw_pid, seconds;
  if (!number(argv[2], &raw_pid) || !raw_pid || raw_pid > 2147483647ULL
      || !number(argv[3], &expected_uid) || expected_uid != getuid()
      || !number(argv[4], &birth_sec) || !birth_sec
      || !number(argv[5], &birth_usec) || birth_usec >= 1000000
      || !number(argv[8], &seconds) || seconds < 1 || seconds > 1800) return 2;
  pid = (guint) raw_pid;
  expected_exe = argv[6];
  if (expected_exe[0] != '/' || !matches()) {
    fputs("identity rejected before attach\n", stderr); return 2;
  }
  gchar *source = NULL;
  GError *error = NULL;
  if (!g_file_get_contents(argv[7], &source, NULL, &error)) {
    fprintf(stderr, "script read failed: %s\n", error->message);
    g_clear_error(&error);
    return 2;
  }
  struct sigaction action = {0}, previous_term, previous_int;
  action.sa_handler = request_stop;
  sigemptyset(&action.sa_mask);
  sigaction(SIGTERM, &action, &previous_term);
  sigaction(SIGINT, &action, &previous_int);
  frida_init();
  loop = g_main_loop_new(NULL, FALSE);
  FridaDeviceManager *manager = frida_device_manager_new();
  FridaDevice *device = NULL;
  FridaSession *session = NULL;
  FridaScript *script = NULL;
  Stage stage;
  begin_stage(&stage, "device", 5000, FALSE);
  device = frida_device_manager_get_device_by_type_sync(
      manager, FRIDA_DEVICE_TYPE_LOCAL, 0, stage.cancel, &error);
  if (!end_stage(&stage, &error) || !device || !matches() || stop_requested) {
    g_atomic_int_set(&failed, 1); goto cleanup;
  }
  begin_stage(&stage, "attach", 10000, FALSE);
  session = frida_device_attach_sync(device, pid, NULL, stage.cancel, &error);
  if (!end_stage(&stage, &error) || !session) { g_atomic_int_set(&failed, 1); goto cleanup; }
  g_signal_connect(session, "detached", G_CALLBACK(detached), NULL);
  if (!matches() || stop_requested) { g_atomic_int_set(&failed, 1); goto cleanup; }
  FridaScriptOptions *options = frida_script_options_new();
  frida_script_options_set_name(options, "fixture-explicit-target");
  frida_script_options_set_runtime(options, FRIDA_SCRIPT_RUNTIME_QJS);
  begin_stage(&stage, "create script", 10000, FALSE);
  script = frida_session_create_script_sync(session, source, options, stage.cancel, &error);
  g_clear_object(&options);
  if (!end_stage(&stage, &error) || !script) { g_atomic_int_set(&failed, 1); goto cleanup; }
  g_signal_connect(script, "message", G_CALLBACK(message), NULL);
  begin_stage(&stage, "load script", 10000, FALSE);
  frida_script_load_sync(script, stage.cancel, &error);
  if (!end_stage(&stage, &error) || g_atomic_int_get(&failed) || stop_requested) goto cleanup;
  guint timeout_id = g_timeout_add_seconds((guint) seconds, stop, NULL);
  guint tick_id = g_timeout_add(100, tick, NULL);
  /* tick covers an error delivered between this check and loop entry. */
  if (!g_atomic_int_get(&failed) && !stop_requested) g_main_loop_run(loop);
  g_source_remove(timeout_id);
  g_source_remove(tick_id);
cleanup:
  if (!g_atomic_int_get(&ready)) {
    fputs("required metadata readiness event not observed\n", stderr);
    g_atomic_int_set(&failed, 1);
  }
  /* Only OUR script/session. Cleanup cancellation never signals the target. */
  if (session) g_signal_handlers_disconnect_by_func(session, G_CALLBACK(detached), NULL);
  if (script) {
    begin_stage(&stage, "unload script", 5000, TRUE);
    frida_script_unload_sync(script, stage.cancel, &error);
    end_stage(&stage, &error);
    g_signal_handlers_disconnect_by_func(script, G_CALLBACK(message), NULL);
    frida_unref(script);
  }
  if (session) {
    begin_stage(&stage, "detach own session", 5000, TRUE);
    frida_session_detach_sync(session, stage.cancel, &error);
    end_stage(&stage, &error);
    frida_unref(session);
  }
  if (device) frida_unref(device);
  begin_stage(&stage, "close manager", 5000, TRUE);
  frida_device_manager_close_sync(manager, stage.cancel, &error);
  end_stage(&stage, &error);
  frida_unref(manager);
  g_free(source);
  g_main_loop_unref(loop);
  loop = NULL;
  frida_deinit();
  sigaction(SIGTERM, &previous_term, NULL);
  sigaction(SIGINT, &previous_int, NULL);
  if (!successful()) {
    fputs("fixture host failed; no runtime/cleanup success claim\n", stderr);
    return 4;
  }
  puts("fixture host completed with metadata and no reported lifecycle errors; target/owner health requires independent evidence");
  return 0;
}
