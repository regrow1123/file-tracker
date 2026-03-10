#pragma once
#include <cstdint>
#include <string>

#define MAX_DEPTH    20
#define MAX_NAME_LEN 256
#define EVENT_DELETE  0
#define EVENT_MTIME   1

struct file_event {
    uint64_t ts_ns;
    uint32_t event_type;
    uint32_t depth;
    char     names[MAX_DEPTH][MAX_NAME_LEN];
};

// Reassemble path from dentry components (stored leaf-first)
inline std::string build_path(const file_event *evt) {
    std::string path;
    for (int i = static_cast<int>(evt->depth) - 1; i >= 0; i--) {
        path += '/';
        path += evt->names[i];
    }
    return path.empty() ? "/" : path;
}
