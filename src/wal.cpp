#include "wal.h"
#include <cstring>
#include <sys/stat.h>

// CRC32 lookup table (standard polynomial 0xEDB88320)
static uint32_t crc32_table[256];
static bool crc32_table_init = false;

static void init_crc32_table() {
    if (crc32_table_init) return;
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int j = 0; j < 8; j++) {
            c = (c & 1) ? (0xEDB88320 ^ (c >> 1)) : (c >> 1);
        }
        crc32_table[i] = c;
    }
    crc32_table_init = true;
}

uint32_t WAL::crc32(const void *data, size_t len) {
    init_crc32_table();
    const uint8_t *buf = static_cast<const uint8_t*>(data);
    uint32_t c = 0xFFFFFFFF;
    for (size_t i = 0; i < len; i++) {
        c = crc32_table[(c ^ buf[i]) & 0xFF] ^ (c >> 8);
    }
    return c ^ 0xFFFFFFFF;
}

WAL::WAL(const std::string& path) : path_(path) {
    fp_ = fopen(path_.c_str(), "ab+");
    if (!fp_) {
        fprintf(stderr, "WAL: failed to open %s: %s\n", path_.c_str(), strerror(errno));
    }
}

WAL::~WAL() {
    if (fp_) fclose(fp_);
}

bool WAL::append(const std::string& data) {
    std::lock_guard<std::mutex> lock(mu_);
    if (!fp_) return false;

    uint32_t len = static_cast<uint32_t>(data.size());
    uint32_t checksum = crc32(data.data(), data.size());

    // Write: [len][crc32][data]
    if (fwrite(&len, sizeof(len), 1, fp_) != 1 ||
        fwrite(&checksum, sizeof(checksum), 1, fp_) != 1 ||
        fwrite(data.data(), 1, len, fp_) != len) {
        fprintf(stderr, "WAL: write error: %s\n", strerror(errno));
        return false;
    }
    fflush(fp_);
    records_written_++;
    return true;
}

std::vector<std::string> WAL::read_all() {
    std::lock_guard<std::mutex> lock(mu_);
    std::vector<std::string> records;

    // Reopen for reading from beginning
    FILE *rfp = fopen(path_.c_str(), "rb");
    if (!rfp) return records;

    while (true) {
        uint32_t len, checksum;
        if (fread(&len, sizeof(len), 1, rfp) != 1) break;
        if (fread(&checksum, sizeof(checksum), 1, rfp) != 1) break;

        if (len > 1024 * 1024) {  // sanity check: 1MB max record
            fprintf(stderr, "WAL: corrupt record (len=%u), stopping\n", len);
            break;
        }

        std::string data(len, '\0');
        if (fread(data.data(), 1, len, rfp) != len) {
            fprintf(stderr, "WAL: truncated record\n");
            break;
        }

        uint32_t actual_crc = crc32(data.data(), data.size());
        if (actual_crc != checksum) {
            fprintf(stderr, "WAL: CRC mismatch (expected %08x, got %08x), skipping\n",
                    checksum, actual_crc);
            continue;  // skip corrupt record, try next
        }

        records.push_back(std::move(data));
    }

    fclose(rfp);
    return records;
}

void WAL::truncate() {
    std::lock_guard<std::mutex> lock(mu_);
    if (fp_) {
        fclose(fp_);
    }
    // Reopen in write mode (truncates)
    fp_ = fopen(path_.c_str(), "wb+");
    if (!fp_) {
        fprintf(stderr, "WAL: truncate failed: %s\n", strerror(errno));
    }
    records_written_ = 0;
}

uint64_t WAL::file_size() const {
    struct stat st;
    if (stat(path_.c_str(), &st) == 0) {
        return static_cast<uint64_t>(st.st_size);
    }
    return 0;
}
