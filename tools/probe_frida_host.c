/* Explicit-identity fixture host and profile-validated isolated Probe host.
 * No discovery, target launch/restart/reconnect/signal or app installation.
 * Only our own read-only validator child may be terminated on cancellation.
 * The pinned Frida devkit already includes GLib and JSON-GLib.
 */
#include "frida-core.h"
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/resource.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PROBE_VALIDATOR_PATH
#error "Build must bind PROBE_VALIDATOR_PATH to this checkout's validator"
#endif

typedef enum { MODE_FIXTURE, MODE_PROBE } Mode;
static Mode mode = MODE_FIXTURE;

static GMainLoop *loop;
static guint pid;
static unsigned long long birth_sec, birth_usec, expected_uid;
static const char *expected_exe;
static gint failed, ready;
static volatile sig_atomic_t stop_requested;
static gboolean validator_cleanup_verified = TRUE;
typedef struct {
  guint pid;
  unsigned long long uid, birth_sec, birth_usec;
  const gchar *executable;
} OwnerIdentity;
static OwnerIdentity expected_supervisor, expected_debugger;

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

static int kernel_record_matches(const struct proc_bsdinfo *info, const gchar *path,
                                 guint process_pid, unsigned long long uid,
                                 unsigned long long seconds, unsigned long long microseconds,
                                 const gchar *executable, guint required_parent) {
  return executable && info->pbi_pid == process_pid && info->pbi_uid == uid
      && info->pbi_start_tvsec == seconds && info->pbi_start_tvusec == microseconds
      && (info->pbi_status == 2 || info->pbi_status == 3 || info->pbi_status == 4)
      && (!required_parent || info->pbi_ppid == required_parent)
      && strcmp(path, executable) == 0;
}
static int kernel_matches(guint process_pid, unsigned long long uid,
                          unsigned long long seconds, unsigned long long microseconds,
                          const gchar *executable, guint required_parent) {
  struct proc_bsdinfo info = {0};
  char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
  return executable && proc_pidinfo(process_pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) == sizeof(info)
      && proc_pidpath(process_pid, path, sizeof(path)) > 0
      && kernel_record_matches(&info, path, process_pid, uid, seconds, microseconds,
                               executable, required_parent);
}
static int matches(void) {
  if (!kernel_matches(pid, expected_uid, birth_sec, birth_usec, expected_exe, 0)) return 0;
  if (mode != MODE_PROBE) return 1;
  return kernel_matches(expected_supervisor.pid, expected_supervisor.uid,
             expected_supervisor.birth_sec, expected_supervisor.birth_usec,
             expected_supervisor.executable, 0)
      && kernel_matches(expected_debugger.pid, expected_debugger.uid,
             expected_debugger.birth_sec, expected_debugger.birth_usec,
             expected_debugger.executable, expected_supervisor.pid);
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
static gboolean integer_member(JsonObject *object, const gchar *name, gint64 *value) {
  JsonNode *node = json_object_get_member(object, name);
  if (!node || !JSON_NODE_HOLDS_VALUE(node) || json_node_get_value_type(node) != G_TYPE_INT64)
    return FALSE;
  *value = json_node_get_int(node);
  return TRUE;
}
static gboolean true_member(JsonObject *object, const gchar *name) {
  JsonNode *node = json_object_get_member(object, name);
  return node && JSON_NODE_HOLDS_VALUE(node) && json_node_get_value_type(node) == G_TYPE_BOOLEAN
      && json_node_get_boolean(node);
}
static gboolean owner_identity_valid(JsonObject *object, const gchar *name, gint64 expected_pid) {
  JsonNode *node = json_object_get_member(object, name);
  if (!node || !JSON_NODE_HOLDS_OBJECT(node)) return FALSE;
  JsonObject *body = json_node_get_object(node);
  gint64 owner_pid, uid, sec, usec;
  const gchar *exe = string_member(body, "executable_path");
  return json_object_get_size(body) == 5
      && integer_member(body, "pid", &owner_pid) && owner_pid == expected_pid
      && integer_member(body, "uid", &uid) && uid == getuid()
      && integer_member(body, "start_seconds", &sec) && sec > 0
      && integer_member(body, "start_microseconds", &usec) && usec >= 0 && usec < 1000000
      && exe && exe[0] == '/';
}

/* Return a retained, fully typed target, never a partially populated identity.
 * Reject new/unknown target fields until this binding has been reviewed.
 */
static JsonNode *parse_validator_output(const gchar *value) {
  if (!value || strlen(value) > 65536) return NULL;
  JsonParser *parser = json_parser_new();
  json_parser_set_strict(parser, TRUE);
  GError *error = NULL;
  JsonNode *result = NULL;
  if (!json_parser_load_from_data(parser, value, -1, &error)) goto done;
  JsonNode *root = json_parser_get_root(parser);
  if (!root || !JSON_NODE_HOLDS_OBJECT(root)) goto done;
  JsonObject *object = json_node_get_object(root);
  if (json_object_get_size(object) != 2 || !true_member(object, "ok")) goto done;
  JsonNode *target = json_object_get_member(object, "target");
  if (!target || !JSON_NODE_HOLDS_OBJECT(target)) goto done;
  JsonObject *body = json_node_get_object(target);
  if (json_object_get_size(body) != 14 || !true_member(body, "controller_verified")) goto done;
  gint64 target_pid, uid, sec, usec, supervisor, debugger;
  if (!integer_member(body, "pid", &target_pid) || target_pid < 1 || target_pid > 2147483647
      || !integer_member(body, "uid", &uid) || uid != getuid()
      || !integer_member(body, "start_seconds", &sec) || sec < 1
      || !integer_member(body, "start_microseconds", &usec) || usec < 0 || usec >= 1000000
      || !integer_member(body, "supervisor_pid", &supervisor) || supervisor < 1 || supervisor > 2147483647
      || !integer_member(body, "debugger_pid", &debugger) || debugger < 1 || debugger > 2147483647
      || target_pid == supervisor || target_pid == debugger || supervisor == debugger) goto done;
  if (!owner_identity_valid(body, "supervisor_identity", supervisor)
      || !owner_identity_valid(body, "debugger_identity", debugger)) goto done;
  const gchar *app = string_member(body, "app_path");
  const gchar *exe = string_member(body, "executable_path");
  const gchar *module = string_member(body, "module_path");
  const gchar *module_uuid = string_member(body, "arm64_uuid");
  const gchar *hash = string_member(body, "arm64_sha256");
  if (!app || app[0] != '/' || !exe || exe[0] != '/' || !module || module[0] != '/'
      || !module_uuid || strcmp(module_uuid, "9CEB6CCF-81A5-3B7F-8FB5-B4256FE67EEB")
      || !hash || strcmp(hash, "6d3feb56043cb63d2ff3f77aa32115abbfe7a2f28b3ab1ab59886ee625703e4b"))
    goto done;
  result = json_node_copy(target);
done:
  g_clear_error(&error);
  g_object_unref(parser);
  return result;
}

static JsonNode *validate_profile(const gchar *profile) {
  if (!profile || profile[0] != '/' || PROBE_VALIDATOR_PATH[0] != '/') return NULL;
  const gchar *arguments[] = {
    "/usr/bin/python3", "-I", PROBE_VALIDATOR_PATH, "--profile", profile, NULL
  };
  GError *error = NULL;
  gchar *output = NULL, *diagnostic = NULL;
  JsonNode *target = NULL;
  Stage stage;
  begin_stage(&stage, "profile validator", 10000, FALSE);
  GSubprocess *child = g_subprocess_newv(arguments,
      G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error);
  gboolean communicated = child && g_subprocess_communicate_utf8(
      child, NULL, stage.cancel, &output, &diagnostic, &error);
  gboolean stage_ok = end_stage(&stage, &error);
  if (!communicated || !stage_ok) {
    if (child) {
      /* This is ONLY the validator child created above. Never a target/owner
       * PID. Reap with a fresh cleanup cancellable after cooperative timeout. */
      g_subprocess_force_exit(child);
      begin_stage(&stage, "reap own validator", 2000, TRUE);
      g_subprocess_wait(child, stage.cancel, &error);
      if (!end_stage(&stage, &error)) validator_cleanup_verified = FALSE;
    }
    goto done;
  }
  if (!g_subprocess_get_if_exited(child) || g_subprocess_get_exit_status(child) != 0) {
    fprintf(stderr, "validator termination: exited=%d status=%d signal=%d\n",
            g_subprocess_get_if_exited(child),
            g_subprocess_get_if_exited(child) ? g_subprocess_get_exit_status(child) : -1,
            g_subprocess_get_if_signaled(child) ? g_subprocess_get_term_sig(child) : 0);
    fprintf(stderr, "validator child rejected: %.1024s %.1024s\n",
            output ? output : "", diagnostic ? diagnostic : "no stderr");
    goto done;
  }
  target = parse_validator_output(output);
  if (!target) fprintf(stderr, "validator schema rejected (%zu bytes)\n", output ? strlen(output) : 0UL);
done:
  if (!target) {
    g_atomic_int_set(&failed, 1);
    fputs("explicit Probe profile validation rejected; no attachment authorized\n", stderr);
  }
  g_free(output);
  g_free(diagnostic);
  g_clear_object(&child);
  return target;
}

static void bind_owner(JsonObject *target, const gchar *field, OwnerIdentity *owner) {
  JsonObject *body = json_object_get_object_member(target, field);
  owner->pid = (guint) json_object_get_int_member(body, "pid");
  owner->uid = (unsigned long long) json_object_get_int_member(body, "uid");
  owner->birth_sec = (unsigned long long) json_object_get_int_member(body, "start_seconds");
  owner->birth_usec = (unsigned long long) json_object_get_int_member(body, "start_microseconds");
  owner->executable = string_member(body, "executable_path");
}
static void bind_target(JsonNode *target) {
  JsonObject *body = json_node_get_object(target);
  pid = (guint) json_object_get_int_member(body, "pid");
  expected_uid = (unsigned long long) json_object_get_int_member(body, "uid");
  birth_sec = (unsigned long long) json_object_get_int_member(body, "start_seconds");
  birth_usec = (unsigned long long) json_object_get_int_member(body, "start_microseconds");
  expected_exe = string_member(body, "executable_path");
  bind_owner(body, "supervisor_identity", &expected_supervisor);
  bind_owner(body, "debugger_identity", &expected_debugger);
}

static Event classify_message(const gchar *value, guint expected_pid, Mode expected_mode) {
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
    const gchar *required = expected_mode == MODE_PROBE ? "metadata_verified" : "fixture_metadata";
    if (strcmp(kind, required)) { result = EVENT_ERROR; goto done; }
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
  accept_event(classify_message(value, pid, mode));
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
static gboolean fixture_path_allowed(const gchar *path) {
  /* Synthetic fixtures are plain Mach-O executables. Neither the isolated
   * Probe nor any everyday .app may bypass profile validation via this mode. */
  if (!path || path[0] != '/') return FALSE;
  gchar *folded = g_ascii_strdown(path, -1);
  gboolean allowed = !strstr(folded, ".app/") && !g_str_has_suffix(folded, ".app");
  g_free(folded);
  return allowed;
}

static gboolean validator_parser_self_test(void) {
  gchar *valid = g_strdup_printf(
      "{\"ok\":true,\"target\":{\"pid\":7,\"uid\":%u,"
      "\"start_seconds\":100,\"start_microseconds\":123,"
      "\"app_path\":\"/synthetic/WeChat-Main-Probe.app\","
      "\"executable_path\":\"/synthetic/WeChat-Main-Probe.app/Contents/MacOS/WeChat\","
      "\"module_path\":\"/synthetic/WeChat-Main-Probe.app/Contents/Resources/wechat.dylib\","
      "\"arm64_uuid\":\"9CEB6CCF-81A5-3B7F-8FB5-B4256FE67EEB\","
      "\"arm64_sha256\":\"6d3feb56043cb63d2ff3f77aa32115abbfe7a2f28b3ab1ab59886ee625703e4b\","
      "\"supervisor_pid\":8,\"debugger_pid\":9,\"controller_verified\":true,"
      "\"supervisor_identity\":{\"pid\":8,\"uid\":%u,\"start_seconds\":50,"
      "\"start_microseconds\":100,\"executable_path\":\"/synthetic/supervisor\"},"
      "\"debugger_identity\":{\"pid\":9,\"uid\":%u,\"start_seconds\":60,"
      "\"start_microseconds\":200,\"executable_path\":\"/synthetic/debugger\"}}}",
      getuid(), getuid(), getuid());
  JsonNode *first = parse_validator_output(valid);
  JsonNode *second = parse_validator_output(valid);
  gboolean ok = first && second && json_node_equal(first, second);
  if (first) json_node_free(first);
  if (second) json_node_free(second);
  static const gchar *fields[] = { "pid", "uid", "start_seconds", "start_microseconds",
      "app_path", "executable_path", "module_path", "arm64_uuid", "arm64_sha256",
      "supervisor_pid", "debugger_pid", "controller_verified",
      "supervisor_identity", "debugger_identity" };
  for (guint i = 0; ok && i < G_N_ELEMENTS(fields); i++) {
    JsonParser *parser = json_parser_new();
    ok = json_parser_load_from_data(parser, valid, -1, NULL);
    JsonNode *root = json_parser_get_root(parser);
    JsonObject *body = json_object_get_object_member(json_node_get_object(root), "target");
    json_object_set_boolean_member(body, fields[i], FALSE);
    gchar *invalid = json_to_string(root, FALSE);
    JsonNode *unexpected = parse_validator_output(invalid);
    ok = ok && unexpected == NULL;
    if (unexpected) json_node_free(unexpected);
    g_free(invalid);
    g_object_unref(parser);
  }
  static const gchar *owners[] = { "supervisor_identity", "debugger_identity" };
  static const gchar *owner_fields[] = { "pid", "uid", "start_seconds",
      "start_microseconds", "executable_path" };
  for (guint i = 0; ok && i < G_N_ELEMENTS(owners); i++) {
    for (guint j = 0; ok && j < G_N_ELEMENTS(owner_fields); j++) {
      JsonParser *parser = json_parser_new();
      ok = json_parser_load_from_data(parser, valid, -1, NULL);
      JsonNode *root = json_parser_get_root(parser);
      JsonObject *body = json_object_get_object_member(json_node_get_object(root), "target");
      JsonObject *owner = json_object_get_object_member(body, owners[i]);
      json_object_set_boolean_member(owner, owner_fields[j], FALSE);
      gchar *invalid = json_to_string(root, FALSE);
      JsonNode *unexpected = parse_validator_output(invalid);
      ok = ok && unexpected == NULL;
      if (unexpected) json_node_free(unexpected);
      g_free(invalid);
      g_object_unref(parser);
    }
  }
  static const gchar *invalid_roots[] = { "{}", "[]", "not json", "{\"ok\":false}",
      "{\"ok\":true,\"target\":[]}", "{\"ok\":1,\"target\":{}}" };
  for (guint i = 0; ok && i < G_N_ELEMENTS(invalid_roots); i++) {
    JsonNode *unexpected = parse_validator_output(invalid_roots[i]);
    ok = unexpected == NULL;
    if (unexpected) json_node_free(unexpected);
  }
  JsonParser *parser = json_parser_new();
  if (ok && json_parser_load_from_data(parser, valid, -1, NULL)) {
    JsonNode *root = json_parser_get_root(parser);
    JsonObject *body = json_object_get_object_member(json_node_get_object(root), "target");
    json_object_set_int_member(body, "start_microseconds", 1000000);
    gchar *invalid = json_to_string(root, FALSE);
    JsonNode *unexpected = parse_validator_output(invalid);
    ok = unexpected == NULL;
    if (unexpected) json_node_free(unexpected);
    g_free(invalid);
    json_object_set_int_member(body, "start_microseconds", 124);
    gchar *changed = json_to_string(root, FALSE);
    first = parse_validator_output(valid);
    second = parse_validator_output(changed);
    ok = ok && first && second && !json_node_equal(first, second);
    if (first) json_node_free(first);
    if (second) json_node_free(second);
    g_free(changed);
    json_object_set_int_member(body, "start_microseconds", 123);
    JsonObject *owner = json_object_get_object_member(body, "debugger_identity");
    json_object_set_int_member(owner, "start_microseconds", 201);
    changed = json_to_string(root, FALSE);
    first = parse_validator_output(valid);
    second = parse_validator_output(changed);
    ok = ok && first && second && !json_node_equal(first, second);
    if (first) json_node_free(first);
    if (second) json_node_free(second);
    g_free(changed);
  } else ok = FALSE;
  g_object_unref(parser);
  g_free(valid);
  return ok;
}

/* Pure message/argument tests plus cancellable watchdogs; no Frida attachment. */
static int self_test(void) {
  static const struct { const char *json; Mode mode; Event event; } cases[] = {
    {"{\"type\":\"error\",\"description\":\"synthetic\"}", MODE_FIXTURE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"read_error\"}}", MODE_FIXTURE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"metadata_verified\",\"pid\":7}}", MODE_PROBE, EVENT_READY},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\",\"pid\":7}}", MODE_FIXTURE, EVENT_READY},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"metadata_verified\",\"pid\":7}}", MODE_FIXTURE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\",\"pid\":7}}", MODE_PROBE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\",\"pid\":8}}", MODE_FIXTURE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"metadata_verified\",\"pid\":8}}", MODE_PROBE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"fixture_metadata\"}}", MODE_FIXTURE, EVENT_ERROR},
    {"{\"type\":\"send\",\"payload\":{\"kind\":\"observer_ready\"}}", MODE_FIXTURE, EVENT_OTHER},
    {"{\"type\":\"log\",\"payload\":\"ready\"}", MODE_FIXTURE, EVENT_OTHER},
    {"{}", MODE_FIXTURE, EVENT_INVALID}, {"not json", MODE_FIXTURE, EVENT_INVALID},
    {"[]", MODE_FIXTURE, EVENT_INVALID}
  };
  for (guint i = 0; i < G_N_ELEMENTS(cases); i++)
    if (classify_message(cases[i].json, 7, cases[i].mode) != cases[i].event) return 1;
  if (!validator_parser_self_test()) return 1;
  struct proc_bsdinfo owner = {0};
  owner.pbi_pid = 9; owner.pbi_uid = getuid(); owner.pbi_ppid = 8;
  owner.pbi_start_tvsec = 100; owner.pbi_start_tvusec = 123;
  owner.pbi_status = 4; owner.pbi_flags = PROC_FLAG_TRACED;
  if (!kernel_record_matches(&owner, "/synthetic/owner", 9, getuid(), 100, 123, "/synthetic/owner", 8)
      || kernel_record_matches(&owner, "/synthetic/owner", 9, getuid(), 100, 124, "/synthetic/owner", 8)
      || kernel_record_matches(&owner, "/synthetic/owner", 9, getuid(), 100, 123, "/synthetic/owner", 7)
      || kernel_record_matches(&owner, "/synthetic/replaced", 9, getuid(), 100, 123, "/synthetic/owner", 8))
    return 1;
  owner.pbi_status = 5;
  if (kernel_record_matches(&owner, "/synthetic/owner", 9, getuid(), 100, 123, "/synthetic/owner", 8))
    return 1;
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
  if (fixture_path_allowed("/synthetic/WeChat-Main-Probe.app/Contents/MacOS/WeChat")
      || fixture_path_allowed("/Applications/WeChat.app/Contents/MacOS/WeChat")
      || fixture_path_allowed("/Applications/WeChat.APP/Contents/MacOS/WeChat")
      || fixture_path_allowed("/synthetic/app.app")
      || fixture_path_allowed("relative-fixture")
      || !fixture_path_allowed("/synthetic/plain-mach-o-fixture")) return 1;
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
  puts("host self-test passed: mode-bound envelopes, typed validator JSON, immutable target, arguments, cancellation; no attachment");
  return 0;
}

int main(int argc, char **argv) {
  /* The bundled GLib fd walker uses stack space proportional to RLIMIT_NOFILE.
   * Some terminal hosts inherit an unlimited soft limit; cap only this host's
   * unused descriptor budget before it spawns its own validator. No system or
   * target policy changes, and no descriptors are closed by this operation. */
  struct rlimit descriptor_limit;
  if (getrlimit(RLIMIT_NOFILE, &descriptor_limit) != 0) return 2;
  if (descriptor_limit.rlim_cur > 8192) {
    descriptor_limit.rlim_cur = 8192;
    if (setrlimit(RLIMIT_NOFILE, &descriptor_limit) != 0) return 2;
  }
  if (argc == 3 && !strcmp(argv[1], "--check-profile")) {
    frida_init();
    JsonNode *checked = validate_profile(argv[2]);
    gboolean ok = checked != NULL;
    if (checked) json_node_free(checked);
    frida_deinit();
    puts(ok ? "profile binding passed; no attachment" : "profile binding failed; no attachment");
    return ok ? 0 : 2;
  }
  if (argc == 2 && !strcmp(argv[1], "--self-test")) {
    /* The embedded GLib/JSON-GLib runtime is initialized by Frida. This does
     * not create a device manager, enumerate devices or attach to a process. */
    frida_init();
    int result = self_test();
    frida_deinit();
    return result;
  }
  const gchar *profile = NULL, *script_path = NULL;
  unsigned long long raw_pid = 0, seconds = 0;
  if (argc == 9 && !strcmp(argv[1], "--fixture-only")) {
    if (!number(argv[2], &raw_pid) || !raw_pid || raw_pid > 2147483647ULL
        || !number(argv[3], &expected_uid) || expected_uid != getuid()
        || !number(argv[4], &birth_sec) || !birth_sec
        || !number(argv[5], &birth_usec) || birth_usec >= 1000000
        || !number(argv[8], &seconds)) return 2;
    pid = (guint) raw_pid;
    expected_exe = argv[6];
    script_path = argv[7];
    if (!fixture_path_allowed(expected_exe)) return 2;
    gchar *canonical_exe = realpath(expected_exe, NULL);
    gboolean canonical = canonical_exe && !strcmp(canonical_exe, expected_exe)
        && fixture_path_allowed(canonical_exe);
    free(canonical_exe);
    if (!canonical) return 2;
  } else if (argc == 5 && !strcmp(argv[1], "--probe-profile")) {
    mode = MODE_PROBE;
    profile = argv[2];
    script_path = argv[3];
    if (profile[0] != '/' || !number(argv[4], &seconds)) return 2;
  } else {
    fputs("usage: host --fixture-only PID UID BIRTH_SEC BIRTH_USEC EXACT_EXE SCRIPT SECONDS\n"
          "       host --probe-profile ABS_PROFILE SCRIPT SECONDS\n", stderr);
    return 2;
  }
  if (seconds < 1 || seconds > 1800) return 2;
  gchar *source = NULL;
  GError *error = NULL;
  struct sigaction action = {0}, previous_term, previous_int;
  action.sa_handler = request_stop;
  sigemptyset(&action.sa_mask);
  sigaction(SIGTERM, &action, &previous_term);
  sigaction(SIGINT, &action, &previous_int);
  frida_init();
  loop = g_main_loop_new(NULL, FALSE);
  FridaDeviceManager *manager = NULL;
  FridaDevice *device = NULL;
  FridaSession *session = NULL;
  FridaScript *script = NULL;
  JsonNode *pinned_target = NULL;
  gboolean cleanup_verified = TRUE;
  Stage stage;
  if (mode == MODE_PROBE) {
    pinned_target = validate_profile(profile);
    if (!pinned_target) goto cleanup;
    bind_target(pinned_target);
  }
  if (!matches() || stop_requested) {
    fputs("identity rejected before device access\n", stderr);
    g_atomic_int_set(&failed, 1); goto cleanup;
  }
  if (!g_file_get_contents(script_path, &source, NULL, &error)) {
    fprintf(stderr, "script read failed: %s\n", error->message);
    g_clear_error(&error);
    g_atomic_int_set(&failed, 1); goto cleanup;
  }
  manager = frida_device_manager_new();
  begin_stage(&stage, "device", 5000, FALSE);
  device = frida_device_manager_get_device_by_type_sync(
      manager, FRIDA_DEVICE_TYPE_LOCAL, 0, stage.cancel, &error);
  if (!end_stage(&stage, &error) || !device || !matches() || stop_requested) {
    g_atomic_int_set(&failed, 1); goto cleanup;
  }
  if (mode == MODE_PROBE) {
    JsonNode *fresh_target = validate_profile(profile);
    gboolean unchanged = fresh_target && json_node_equal(pinned_target, fresh_target);
    if (fresh_target) json_node_free(fresh_target);
    if (!unchanged) {
      fputs("Probe target/controller changed; refusing rebind\n", stderr);
      g_atomic_int_set(&failed, 1); goto cleanup;
    }
  }
  if (!matches() || stop_requested) { g_atomic_int_set(&failed, 1); goto cleanup; }
  begin_stage(&stage, "attach", 10000, FALSE);
  session = frida_device_attach_sync(device, pid, NULL, stage.cancel, &error);
  if (!end_stage(&stage, &error) || !session) { g_atomic_int_set(&failed, 1); goto cleanup; }
  g_signal_connect(session, "detached", G_CALLBACK(detached), NULL);
  if (!matches() || stop_requested) { g_atomic_int_set(&failed, 1); goto cleanup; }
  FridaScriptOptions *options = frida_script_options_new();
  frida_script_options_set_name(options,
      mode == MODE_PROBE ? "probe-profile-verified-target" : "fixture-explicit-target");
  frida_script_options_set_runtime(options, FRIDA_SCRIPT_RUNTIME_QJS);
  begin_stage(&stage, "create script", 10000, FALSE);
  script = frida_session_create_script_sync(session, source, options, stage.cancel, &error);
  g_clear_object(&options);
  if (!end_stage(&stage, &error) || !script) { g_atomic_int_set(&failed, 1); goto cleanup; }
  g_signal_connect(script, "message", G_CALLBACK(message), NULL);
  if (!matches() || stop_requested) { g_atomic_int_set(&failed, 1); goto cleanup; }
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
    if (!end_stage(&stage, &error)) cleanup_verified = FALSE;
    g_signal_handlers_disconnect_by_func(script, G_CALLBACK(message), NULL);
    frida_unref(script);
  }
  if (session) {
    begin_stage(&stage, "detach own session", 5000, TRUE);
    frida_session_detach_sync(session, stage.cancel, &error);
    if (!end_stage(&stage, &error)) cleanup_verified = FALSE;
    frida_unref(session);
  }
  if (device) frida_unref(device);
  if (manager) {
    begin_stage(&stage, "close manager", 5000, TRUE);
    frida_device_manager_close_sync(manager, stage.cancel, &error);
    if (!end_stage(&stage, &error)) cleanup_verified = FALSE;
    frida_unref(manager);
  }
  g_free(source);
  if (pinned_target) json_node_free(pinned_target);
  g_main_loop_unref(loop);
  loop = NULL;
  frida_deinit();
  sigaction(SIGTERM, &previous_term, NULL);
  sigaction(SIGINT, &previous_int, NULL);
  printf("{\"host_lifecycle\":\"complete\",\"cleanup_verified\":%s,\"mode\":\"%s\","
         "\"pid\":%u,\"metadata_ready\":%s,\"ok\":%s}\n",
         cleanup_verified && validator_cleanup_verified ? "true" : "false",
         mode == MODE_PROBE ? "probe" : "fixture", pid,
         g_atomic_int_get(&ready) ? "true" : "false", successful() ? "true" : "false");
  fflush(stdout);
  if (!successful()) {
    fputs("host failed; no runtime/cleanup success claim\n", stderr);
    return 4;
  }
  return 0;
}
