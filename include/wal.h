#pragma once
#include <string>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <vector>

// Write-Ahead Log: append-only file with CRC32 checksums.
// Records: [uint32_t len][uint32_t crc32][char data[len]]
class WAL {
public:
    explicit WAL(const std::string& path);
    ~WAL();

    WAL(const WAL&) = delete;
    WAL& operator=(const WAL&) = delete;

    // Append a record. Thread-safe.
    bool append(const std::string& data);

    // Read all records from the WAL file. Returns records in order.
    std::vector<std::string> read_all();

    // Truncate the WAL (after all records have been successfully replayed).
    void truncate();

    // Number of records written since last truncate.
    uint64_t records_written() const { return records_written_; }

    // File size in bytes.
    uint64_t file_size() const;

private:
    std::string path_;
    FILE *fp_ = nullptr;
    mutable std::mutex mu_;
    uint64_t records_written_ = 0;

    static uint32_t crc32(const void *data, size_t len);
};
