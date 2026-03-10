#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <string>
#include <unistd.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>

#include "probe.skel.h"

#define MAX_DEPTH    20
#define MAX_NAME_LEN 256

#define EVENT_DELETE 0
#define EVENT_MTIME  1

static volatile bool running = true;

struct file_event {
    uint64_t ts_ns;
    uint32_t event_type;
    uint32_t depth;
    char     names[MAX_DEPTH][MAX_NAME_LEN];
};

static void sig_handler(int sig) {
    running = false;
}

// Reassemble path from components (stored leaf-first)
static std::string build_path(const struct file_event *evt) {
    std::string path;
    // Components are stored leaf-first, so reverse
    for (int i = static_cast<int>(evt->depth) - 1; i >= 0; i--) {
        path += '/';
        path += evt->names[i];
    }
    return path.empty() ? "/" : path;
}

static int handle_event(void *ctx, void *data, size_t data_sz) {
    auto *evt = static_cast<struct file_event *>(data);
    std::string path = build_path(evt);

    // Userspace /home filter
    if (path.substr(0, 5) != "/home") return 0;

    const char *type_str = (evt->event_type == EVENT_DELETE) ? "DELETE" : "MTIME";
    printf("[%llu] %-6s %s\n",
           (unsigned long long)evt->ts_ns,
           type_str,
           path.c_str());
    fflush(stdout);
    return 0;
}

int main(int argc, char **argv) {
    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    struct probe_bpf *skel = probe_bpf__open_and_load();
    if (!skel) {
        fprintf(stderr, "Failed to open and load BPF skeleton\n");
        return 1;
    }

    int err = probe_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "Failed to attach BPF programs: %d\n", err);
        probe_bpf__destroy(skel);
        return 1;
    }

    struct ring_buffer *rb = ring_buffer__new(
        bpf_map__fd(skel->maps.events),
        handle_event,
        nullptr,
        nullptr
    );
    if (!rb) {
        fprintf(stderr, "Failed to create ring buffer\n");
        probe_bpf__destroy(skel);
        return 1;
    }

    printf("file-tracker started. Watching /home for changes...\n");

    while (running) {
        err = ring_buffer__poll(rb, 100);
        if (err == -EINTR) break;
        if (err < 0) {
            fprintf(stderr, "ring_buffer__poll error: %d\n", err);
            break;
        }
    }

    printf("Shutting down...\n");
    ring_buffer__free(rb);
    probe_bpf__destroy(skel);
    return 0;
}
