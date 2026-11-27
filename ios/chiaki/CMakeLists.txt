# Dilmaç (iOS): libchiaki (chiaki-ng) + the Dilmaç bridge as one static library, for one Apple platform slice.
# Driven by ios/scripts/build-chiaki.sh; not meant to be configured by hand.
#
# Unlike chiaki-ng's own CMake, this leaves out Remote Play over the internet (holepunch: curl, json-c, miniupnpc,
# libevent) and AIA certificate fetching (curl); see ios_stubs.c. Only OpenSSL (libcrypto) and opus are needed.
cmake_minimum_required(VERSION 3.20)
project(dilmac_chiaki C)

set(CHIAKI_SOURCE "" CACHE PATH "chiaki-ng source folder (with nanopb, jerasure, gf-complete submodules)")
set(OPENSSL_PREFIX "" CACHE PATH "OpenSSL install prefix for this slice (include/, lib/libcrypto.a)")
set(OPUS_PREFIX "" CACHE PATH "opus install prefix for this slice (include/opus/, lib/libopus.a)")
set(BRIDGE_DIR "" CACHE PATH "folder with bridge.c / bridge.h (pc/chiaki_bridge)")

foreach(p CHIAKI_SOURCE OPENSSL_PREFIX OPUS_PREFIX BRIDGE_DIR)
	if(NOT EXISTS "${${p}}")
		message(FATAL_ERROR "${p} must be set to an existing folder")
	endif()
endforeach()

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(LIB "${CHIAKI_SOURCE}/lib")
set(TP "${CHIAKI_SOURCE}/third-party")
file(STRINGS "${CHIAKI_SOURCE}/CMakeLists.txt" _ver REGEX "set\\(CHIAKI_VERSION_(MAJOR|MINOR|PATCH) [0-9]+\\)")
foreach(line ${_ver})
	string(REGEX MATCH "CHIAKI_VERSION_(MAJOR|MINOR|PATCH) ([0-9]+)" _m "${line}")
	set(CHIAKI_VERSION_${CMAKE_MATCH_1} ${CMAKE_MATCH_2})
endforeach()

# ---- takion.proto -> nanopb C sources (the generator runs protoc from grpcio-tools)
set(PB_DIR "${CMAKE_CURRENT_BINARY_DIR}/pb")
add_custom_command(
	OUTPUT "${PB_DIR}/takion.pb.c" "${PB_DIR}/takion.pb.h"
	COMMAND "${CMAKE_COMMAND}" -E make_directory "${PB_DIR}"
	COMMAND "${Python3_EXECUTABLE}" "${TP}/nanopb/generator/nanopb_generator.py"
		-I "${LIB}/protobuf" -D "${PB_DIR}" "${LIB}/protobuf/takion.proto"
	DEPENDS "${LIB}/protobuf/takion.proto"
	VERBATIM)

# ---- config.h (same switches chiaki's own CMake would produce here)
set(CHIAKI_LIB_ENABLE_OPUS 1)
set(CHIAKI_LIB_ENABLE_PI_DECODER 0)
set(CHIAKI_LIB_ENABLE_FFMPEG_DECODER 0)
configure_file("${LIB}/config.h.in" "${CMAKE_CURRENT_BINARY_DIR}/include/chiaki/config.h")

# ---- sources
file(GLOB CHIAKI_SRC "${LIB}/src/*.c" "${LIB}/src/remote/*.c")
list(FILTER CHIAKI_SRC EXCLUDE REGEX "/(aia|aia_openssl|ffmpegdecoder|pidecoder|holepunch|holepunch-test)\\.c$")

set(NANOPB_SRC "${TP}/nanopb/pb_common.c" "${TP}/nanopb/pb_decode.c" "${TP}/nanopb/pb_encode.c")

set(GF_SRC gf gf_wgen gf_w4 gf_w8 gf_w16 gf_w32 gf_w64 gf_w128 gf_rand gf_general gf_cpu)
list(TRANSFORM GF_SRC PREPEND "${TP}/gf-complete/src/")
list(TRANSFORM GF_SRC APPEND ".c")

set(JERASURE_SRC galois jerasure reed_sol cauchy liberation)
list(TRANSFORM JERASURE_SRC PREPEND "${TP}/jerasure/src/")
list(TRANSFORM JERASURE_SRC APPEND ".c")

add_library(dilmac_chiaki STATIC
	${CHIAKI_SRC}
	"${PB_DIR}/takion.pb.c"
	${NANOPB_SRC}
	${GF_SRC}
	${JERASURE_SRC}
	"${CMAKE_CURRENT_SOURCE_DIR}/ios_stubs.c"
	"${BRIDGE_DIR}/bridge.c")

target_include_directories(dilmac_chiaki PRIVATE
	"${LIB}/include"
	"${LIB}/src"
	"${CMAKE_CURRENT_BINARY_DIR}/include"
	"${PB_DIR}"
	"${TP}/nanopb"
	"${TP}/gf-complete/include"
	"${TP}/jerasure/include"
	"${OPENSSL_PREFIX}/include"
	"${OPUS_PREFIX}/include"
	"${CMAKE_CURRENT_SOURCE_DIR}/compat"
	"${BRIDGE_DIR}")

target_compile_definitions(dilmac_chiaki PRIVATE
	PB_C99_STATIC_ASSERT
	CHIAKI_VERSION_MAJOR=${CHIAKI_VERSION_MAJOR}
	CHIAKI_VERSION_MINOR=${CHIAKI_VERSION_MINOR}
	CHIAKI_VERSION_PATCH=${CHIAKI_VERSION_PATCH}
	CHIAKI_VERSION="${CHIAKI_VERSION_MAJOR}.${CHIAKI_VERSION_MINOR}.${CHIAKI_VERSION_PATCH}")

target_compile_options(dilmac_chiaki PRIVATE -Wall -Wno-unused-function -Wno-unused-variable)
set_target_properties(dilmac_chiaki PROPERTIES C_STANDARD 11)
