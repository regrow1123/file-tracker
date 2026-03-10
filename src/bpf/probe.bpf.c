// SPDX-License-Identifier: GPL-2.0
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

#define MAX_DEPTH    20
#define MAX_NAME_LEN 256
#define EVENT_DELETE  0
#define EVENT_MTIME   1

struct file_event {
    __u64 ts_ns;
    __u32 event_type;
    __u32 depth;
    char  names[MAX_DEPTH][MAX_NAME_LEN];
};

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 16 * 1024 * 1024);
} events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct file_event);
} scratch SEC(".maps");

static __always_inline int emit_event(struct dentry *dentry, __u32 event_type)
{
    __u32 zero = 0;
    struct file_event *evt = bpf_map_lookup_elem(&scratch, &zero);
    if (!evt) return 0;

    evt->ts_ns = bpf_ktime_get_ns();
    evt->event_type = event_type;
    evt->depth = 0;

    struct dentry *d = dentry;

    #pragma unroll
    for (int i = 0; i < MAX_DEPTH; i++) {
        struct dentry *parent = BPF_CORE_READ(d, d_parent);
        if (d == parent) break;

        unsigned int len = BPF_CORE_READ(d, d_name.len);
        if (len == 0) break;

        const unsigned char *name = BPF_CORE_READ(d, d_name.name);
        bpf_probe_read_kernel_str(evt->names[i], MAX_NAME_LEN, name);

        evt->depth = i + 1;
        d = parent;
    }

    if (evt->depth == 0) return 0;

    // /home filtering is done in userspace
    bpf_ringbuf_output(&events, evt, sizeof(*evt), 0);
    return 0;
}

SEC("kprobe/vfs_unlink")
int BPF_KPROBE(trace_vfs_unlink, void *idmap_or_userns,
               struct inode *dir, struct dentry *dentry)
{
    return emit_event(dentry, EVENT_DELETE);
}

SEC("kprobe/vfs_write")
int BPF_KPROBE(trace_vfs_write, struct file *file)
{
    struct dentry *dentry = BPF_CORE_READ(file, f_path.dentry);
    return emit_event(dentry, EVENT_MTIME);
}

SEC("kprobe/do_truncate")
int BPF_KPROBE(trace_do_truncate, void *idmap_or_userns,
               struct dentry *dentry)
{
    return emit_event(dentry, EVENT_MTIME);
}

SEC("kprobe/vfs_utimes")
int BPF_KPROBE(trace_vfs_utimes, const struct path *path)
{
    struct dentry *dentry = BPF_CORE_READ(path, dentry);
    return emit_event(dentry, EVENT_MTIME);
}

char LICENSE[] SEC("license") = "GPL";
