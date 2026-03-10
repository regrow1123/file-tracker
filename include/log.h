#pragma once
#include <cstdio>
#include <cstdarg>
#include <cstring>

enum class LogLevel : int {
    ERROR = 0,
    WARN  = 1,
    INFO  = 2,
    DEBUG = 3,
};

class Log {
public:
    static void set_level(LogLevel level) { level_ = level; }
    static LogLevel level() { return level_; }

    static void set_level_from_string(const char *s) {
        if (!s) return;
        if (strcmp(s, "error") == 0) level_ = LogLevel::ERROR;
        else if (strcmp(s, "warn") == 0) level_ = LogLevel::WARN;
        else if (strcmp(s, "info") == 0) level_ = LogLevel::INFO;
        else if (strcmp(s, "debug") == 0) level_ = LogLevel::DEBUG;
    }

    static void error(const char *fmt, ...) {
        if (level_ < LogLevel::ERROR) return;
        va_list ap; va_start(ap, fmt);
        log_msg("ERROR", fmt, ap);
        va_end(ap);
    }

    static void warn(const char *fmt, ...) {
        if (level_ < LogLevel::WARN) return;
        va_list ap; va_start(ap, fmt);
        log_msg("WARN", fmt, ap);
        va_end(ap);
    }

    static void info(const char *fmt, ...) {
        if (level_ < LogLevel::INFO) return;
        va_list ap; va_start(ap, fmt);
        log_msg("INFO", fmt, ap);
        va_end(ap);
    }

    static void debug(const char *fmt, ...) {
        if (level_ < LogLevel::DEBUG) return;
        va_list ap; va_start(ap, fmt);
        log_msg("DEBUG", fmt, ap);
        va_end(ap);
    }

private:
    static inline LogLevel level_ = LogLevel::INFO;

    static void log_msg(const char *prefix, const char *fmt, va_list ap) {
        fprintf(stderr, "[%s] ", prefix);
        vfprintf(stderr, fmt, ap);
        fprintf(stderr, "\n");
    }
};
