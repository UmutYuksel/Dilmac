#!/usr/bin/env bash
# Builds chiaki_bridge.dll and copies it with the MSYS2 DLLs it needs into OUTPUT_DIR.
# In an MSYS2 UCRT64 shell:  bash build.sh CHIAKI_SOURCE BUILD_DIR OUTPUT_DIR
# Requires (pacman, mingw-w64-ucrt-x86_64-): gcc cmake ninja pkgconf openssl json-c miniupnpc libevent opus
# protobuf python python-protobuf
# The build runs in an ASCII-only folder: CMake/Ninja can trip over the 'ı' in the project path.
set -euo pipefail
SOURCE="$1"
BUILD="$2"
OUTPUT="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$BUILD/bridge"
cp "$HERE/bridge.c" "$HERE/bridge.h" "$HERE/CMakeLists.txt" "$BUILD/bridge/"
# CMAKE_POLICY_VERSION_MINIMUM: submodules' old cmake_minimum_required must not fail under CMake 4.
cmake -S "$BUILD/bridge" -B "$BUILD/b2" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHIAKI_SOURCE="$SOURCE" \
	-DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build "$BUILD/b2" --target bridge

mkdir -p "$OUTPUT"
cp "$BUILD/b2/chiaki_bridge.dll" "$OUTPUT/"
# The DLLs it pulls from UCRT64 (not Windows' own).
ldd "$OUTPUT/chiaki_bridge.dll" | awk '$3 ~ /ucrt64/ {print $3}' | sort -u | while read -r dll; do
	cp "$dll" "$OUTPUT/"
done
ls -la "$OUTPUT"
