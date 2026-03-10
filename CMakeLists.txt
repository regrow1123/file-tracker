cmake_minimum_required(VERSION 3.20)
project(file-tracker LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Find required packages
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBBPF REQUIRED libbpf)
pkg_check_modules(RDKAFKA REQUIRED rdkafka)

# Paths
set(BPF_SRC ${CMAKE_SOURCE_DIR}/src/bpf/probe.bpf.c)
set(BPF_OBJ ${CMAKE_BINARY_DIR}/probe.bpf.o)
set(BPF_SKEL ${CMAKE_BINARY_DIR}/probe.skel.h)
set(VMLINUX_H ${CMAKE_SOURCE_DIR}/include/vmlinux.h)

# Find tools
find_program(CLANG clang REQUIRED)
find_program(BPFTOOL bpftool PATHS /usr/sbin REQUIRED)

# Compile BPF program
add_custom_command(
    OUTPUT ${BPF_OBJ}
    COMMAND ${CLANG} -g -O2 -target bpf
            -D__TARGET_ARCH_x86
            -I${CMAKE_SOURCE_DIR}/include
            -c ${BPF_SRC}
            -o ${BPF_OBJ}
    DEPENDS ${BPF_SRC} ${VMLINUX_H}
    COMMENT "Compiling BPF program"
)

# Generate BPF skeleton
add_custom_command(
    OUTPUT ${BPF_SKEL}
    COMMAND ${BPFTOOL} gen skeleton ${BPF_OBJ} > ${BPF_SKEL}
    DEPENDS ${BPF_OBJ}
    COMMENT "Generating BPF skeleton"
)

# Main executable
add_executable(file-tracker
    src/main.cpp
    src/config.cpp
    src/debouncer.cpp
    src/kafka_producer.cpp
    src/wal.cpp
)

target_include_directories(file-tracker PRIVATE
    ${CMAKE_BINARY_DIR}
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/third_party
    ${LIBBPF_INCLUDE_DIRS}
    ${RDKAFKA_INCLUDE_DIRS}
)

target_link_libraries(file-tracker PRIVATE
    ${LIBBPF_LIBRARIES}
    ${RDKAFKA_LIBRARIES}
    elf
    z
    pthread
)

# Ensure skeleton is generated before compiling main
add_custom_target(bpf_skel DEPENDS ${BPF_SKEL})
add_dependencies(file-tracker bpf_skel)
